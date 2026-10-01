"""`~/.bth/catalog/index.db` schema and the read API (spec "Index ingest
(generation swap)" and "Reads (G3)").

This is the module the spec names `bathos.index` (AC-18): the one flag-gated
read entry point (`connect_read`) plus the legacy-database opener
(`connect_legacy`). Everything else in `src/bathos/` that needs to read a
catalog database goes through one of these two functions; the only other
exemption is the ingest path itself (`bathos.runlog.ingest`), which writes
`index.db` directly.

`bathos.runlog.index` re-exports this module unchanged (a small number of
pre-existing call sites -- `bathos.runlog.ingest` and a few runlog tests --
import it from there); this file is the implementation's real home so a
fresh reader lands on the name the spec uses.

This module owns the disposable index's DuckDB schema (`events`,
`quarantine`, `ingest_watermarks`, and the folded `runs` table) plus
`connect_read()`, the one flag-gated entry point for queries.

Scope (delivery step 3, wave a): only the `runs` fold is materialized.
Campaign/edge/anchor/ledger tables (the spec's other "Authoritative writes"
entities) are not created here -- a later wave adds them alongside their own
fold functions. `connect_read()`'s flag-on branch is therefore a MINIMAL
stand-in for the spec's per-table read views: it attaches the on-disk
`index.db` read-only and exposes its tables directly, rather than the full
"enumerate roots afresh, re-fold the unindexed delta, UNION ALL over a view"
mechanism the spec describes for zero-latency reads (AC-3). Freshness in this
wave still requires an explicit `run_ingest()` call (e.g. via `bth compact`);
true read-time re-folding is left to a later wave, noted here rather than
half-built.

Scope (delivery step 3, wave d): `connect_read()` grows `read_only`/`missing`
parameters so its flag-off pass-through can reproduce every existing call
site's exact behaviour (spec "Reads": "opens `bathos.db` with exactly the
arguments that call site uses today"), and `connect_legacy()` is added (AC-18)
for the legacy-database opens the migration importer and `bth verify` will
make in step 4 -- this wave adds the function; step 4 wires its callers.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from bathos.config import default_catalog_dir

EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS events (
    eid VARCHAR PRIMARY KEY,
    v INTEGER,
    kind VARCHAR,
    entity_key VARCHAR,
    entity VARCHAR,
    ts VARCHAR,
    project VARCHAR,
    project_id VARCHAR,
    writer VARCHAR,
    seq INTEGER,
    main_root VARCHAR,
    worktree_root VARCHAR,
    origin VARCHAR,
    data VARCHAR
)
"""

QUARANTINE_DDL = """
CREATE TABLE IF NOT EXISTS quarantine (
    file VARCHAR,
    byte_offset BIGINT,
    reason VARCHAR,
    raw_line VARCHAR,
    detected_at VARCHAR
)
"""

WATERMARKS_DDL = """
CREATE TABLE IF NOT EXISTS ingest_watermarks (
    root_kind VARCHAR,
    root_id VARCHAR,
    path VARCHAR,
    size BIGINT,
    byte_offset BIGINT,
    PRIMARY KEY (root_kind, root_id, path)
)
"""

# The folded `runs` table (delivery step 3, wave a scope): one row per run
# entity, rebuilt by delete + re-insert on every re-fold (`ingest._refold_run`).
# Column set mirrors the legacy warm `runs` row for the fields the run fold
# (`fold_runs.fold_run`) actually produces; `seq_position`/`evalue` are
# always NULL here (campaign fold, later wave).
RUNS_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    id VARCHAR PRIMARY KEY,
    project_slug VARCHAR,
    command VARCHAR,
    argv VARCHAR,
    git_hash VARCHAR,
    git_branch VARCHAR,
    git_dirty BOOLEAN,
    timestamp VARCHAR,
    duration_s DOUBLE,
    exit_code INTEGER,
    status VARCHAR,
    output_paths VARCHAR,
    tags VARCHAR,
    outcome VARCHAR,
    outcome_error_reason VARCHAR,
    outcome_is_residual BOOLEAN,
    adversarial_check_status VARCHAR,
    adversarial_check_result VARCHAR,
    differential_status VARCHAR,
    differential_off_value VARCHAR,
    differential_on_value VARCHAR,
    differential_effect DOUBLE,
    sidecar_sha256 VARCHAR,
    sidecar_path VARCHAR,
    parent_run_id VARCHAR,
    agent_mode VARCHAR,
    sidecar_mode VARCHAR,
    campaign_id VARCHAR,
    script_sha256 VARCHAR,
    stage_name VARCHAR,
    claim_discriminates VARCHAR,
    claim_isolates VARCHAR,
    parity_run_type VARCHAR,
    slurm_job_id VARCHAR,
    slurm_array_task_id VARCHAR,
    hostname VARCHAR,
    git_dirty_content_id VARCHAR,
    git_provenance_source VARCHAR,
    dependency_lock_sha256 VARCHAR,
    component_id VARCHAR,
    component_sidecar_sha256 VARCHAR,
    seed BIGINT,
    baseline_hpo_trials BIGINT,
    baseline_hpo_compute_budget DOUBLE,
    stdout_sha256 VARCHAR,
    manifest_sha256 VARCHAR,
    manifest_path VARCHAR,
    skill_sha256 VARCHAR,
    schema_version VARCHAR,
    metadata VARCHAR,
    output_metadata VARCHAR,
    postmortem_status VARCHAR,
    postmortem_override VARCHAR,
    postmortem_verdict_override VARCHAR,
    postmortem_author VARCHAR,
    postmortem_path VARCHAR,
    postmortem_hypothesis_status VARCHAR,
    postmortem_has_anomalies BOOLEAN,
    postmortem_summary VARCHAR,
    postmortem_asset_links VARCHAR,
    seq_position BIGINT,
    evalue DOUBLE
)
"""

RUNS_COLUMNS: list[str] = [
    line.strip().split()[0]
    for line in RUNS_DDL.strip().splitlines()
    if line.strip() and not line.strip().startswith(("CREATE", ")"))
]

# --- Delivery step 3, wave b: the campaign fold + the other simple
# "Authoritative writes" folds (edges, anchors, ledgers, submit provenance).
# Column sets mirror the legacy warm shape (compact.py's _CAMPAIGNS_TABLE_
# SCHEMA / _CAMPAIGN_RUNS_TABLE_SCHEMA / _CAMPAIGN_EDGES_TABLE_SCHEMA /
# _RUN_EDGES_TABLE_SCHEMA, anchor.py's _ANCHORS_TABLE_SCHEMA, blast_radius.py
# / trust_ledger.py / archived_items.py's _*_TABLE_SCHEMA) for the fields
# those write sites' event `data` actually carries; `submits` has no legacy
# warm precedent (sprint_audit.py reads the Parquet fragments directly), so
# its column set instead mirrors catalog.write_submit_provenance's Parquet
# schema plus the `slurm_job_id` field the event additionally carries.

CAMPAIGNS_DDL = """
CREATE TABLE IF NOT EXISTS campaigns (
    id VARCHAR PRIMARY KEY,
    project_slug VARCHAR,
    name VARCHAR,
    mode VARCHAR,
    question VARCHAR,
    hypothesis VARCHAR,
    status VARCHAR,
    started_at VARCHAR,
    concluded_at VARCHAR,
    conclusion VARCHAR,
    outcome_label VARCHAR,
    parent_campaign_id VARCHAR,
    stopping_threshold DOUBLE,
    claim_path VARCHAR,
    claim_sha256 VARCHAR,
    claim_mode VARCHAR,
    negative_check VARCHAR
)
"""

CAMPAIGN_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS campaign_runs (
    campaign_id VARCHAR,
    run_id VARCHAR,
    evalue DOUBLE,
    seq_position BIGINT,
    PRIMARY KEY (campaign_id, run_id)
)
"""

CAMPAIGN_EDGES_DDL = """
CREATE TABLE IF NOT EXISTS campaign_edges (
    child_campaign_id VARCHAR,
    parent_campaign_id VARCHAR,
    PRIMARY KEY (child_campaign_id, parent_campaign_id)
)
"""

RUN_EDGES_DDL = """
CREATE TABLE IF NOT EXISTS run_edges (
    child_run_id VARCHAR,
    parent_run_id VARCHAR,
    PRIMARY KEY (child_run_id, parent_run_id)
)
"""

SIDECAR_ANCHORS_DDL = """
CREATE TABLE IF NOT EXISTS sidecar_anchors (
    id VARCHAR PRIMARY KEY,
    path VARCHAR,
    sha256 VARCHAR,
    kind VARCHAR,
    label VARCHAR,
    content_hash VARCHAR,
    campaign_id VARCHAR,
    anchored_at VARCHAR
)
"""

BLAST_RADIUS_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS blast_radius_ledger (
    id VARCHAR PRIMARY KEY,
    entity_type VARCHAR,
    entity_id VARCHAR,
    from_state VARCHAR,
    to_state VARCHAR,
    anchor_kind VARCHAR,
    anchor_value VARCHAR,
    matched_files VARCHAR,
    matched_clauses VARCHAR,
    shadow_verdict VARCHAR,
    match_reason VARCHAR,
    reason VARCHAR,
    amended_at VARCHAR
)
"""

TRUST_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS trust_ledger (
    id VARCHAR PRIMARY KEY,
    run_id VARCHAR,
    output_path VARCHAR,
    content_hash VARCHAR,
    from_state VARCHAR,
    to_state VARCHAR,
    attestation_ref VARCHAR,
    amended_at VARCHAR,
    reason VARCHAR
)
"""

ARCHIVED_ITEMS_DDL = """
CREATE TABLE IF NOT EXISTS archived_items (
    id VARCHAR,
    project_slug VARCHAR,
    event VARCHAR,
    kind VARCHAR,
    paths VARCHAR,
    pre_archive_sha VARCHAR,
    stub_commit_sha VARCHAR,
    verdict VARCHAR,
    reason VARCHAR,
    superseded_by VARCHAR,
    bundle_sha256 VARCHAR,
    bundle_path VARCHAR,
    archived_by VARCHAR,
    recorded_at VARCHAR,
    record_id VARCHAR PRIMARY KEY
)
"""

SUBMITS_DDL = """
CREATE TABLE IF NOT EXISTS submits (
    id VARCHAR PRIMARY KEY,
    project_slug VARCHAR,
    command VARCHAR,
    sidecar_sha256 VARCHAR,
    bth_submit_version VARCHAR,
    submitted_at VARCHAR,
    myxcel_job_id VARCHAR,
    slurm_job_id VARCHAR,
    stage_name VARCHAR
)
"""


_DDL_NON_COLUMN_PREFIXES = ("CREATE", ")", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK")


def _columns_of(ddl: str) -> list[str]:
    return [
        line.strip().split()[0]
        for line in ddl.strip().splitlines()
        if line.strip() and not line.strip().startswith(_DDL_NON_COLUMN_PREFIXES)
    ]


CAMPAIGNS_COLUMNS: list[str] = _columns_of(CAMPAIGNS_DDL)
CAMPAIGN_RUNS_COLUMNS: list[str] = _columns_of(CAMPAIGN_RUNS_DDL)
CAMPAIGN_EDGES_COLUMNS: list[str] = _columns_of(CAMPAIGN_EDGES_DDL)
RUN_EDGES_COLUMNS: list[str] = _columns_of(RUN_EDGES_DDL)
SIDECAR_ANCHORS_COLUMNS: list[str] = _columns_of(SIDECAR_ANCHORS_DDL)
BLAST_RADIUS_LEDGER_COLUMNS: list[str] = _columns_of(BLAST_RADIUS_LEDGER_DDL)
TRUST_LEDGER_COLUMNS: list[str] = _columns_of(TRUST_LEDGER_DDL)
ARCHIVED_ITEMS_COLUMNS: list[str] = _columns_of(ARCHIVED_ITEMS_DDL)
SUBMITS_COLUMNS: list[str] = _columns_of(SUBMITS_DDL)


def index_db_path(catalog_dir: Path | None = None) -> Path:
    return (catalog_dir or default_catalog_dir()) / "index.db"


def init_index_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(EVENTS_DDL)
    con.execute(QUARANTINE_DDL)
    con.execute(WATERMARKS_DDL)
    con.execute(RUNS_DDL)
    con.execute(CAMPAIGNS_DDL)
    con.execute(CAMPAIGN_RUNS_DDL)
    con.execute(CAMPAIGN_EDGES_DDL)
    con.execute(RUN_EDGES_DDL)
    con.execute(SIDECAR_ANCHORS_DDL)
    con.execute(BLAST_RADIUS_LEDGER_DDL)
    con.execute(TRUST_LEDGER_DDL)
    con.execute(ARCHIVED_ITEMS_DDL)
    con.execute(SUBMITS_DDL)


def connect_read(
    catalog_dir: Path | None = None,
    *,
    read_only: bool = True,
    missing: str = "empty",
) -> duckdb.DuckDBPyConnection:
    """The one flag-gated read entry point (spec `bathos.index.connect_read`).

    Flag OFF (before cut-over): a pass-through. Opens `bathos.db` read-only,
    exactly as today's ad hoc call sites do (spec "Reads": "creates no views
    and does not use `connect_legacy`"). `read_only` and `missing` let a
    migrated call site reproduce its own pre-migration behaviour exactly:

    - `read_only` is passed straight through to `duckdb.connect()` and
      defaults to True, which every read call site now takes. It must stay
      that way: a read-WRITE DuckDB connection holds an EXCLUSIVE lock on the
      file, so a single read opened read-write shuts out every concurrent
      reader for as long as it runs. Read-only connections share the file
      freely. (Measured: two read-only opens coexist; while a read-write one
      is held, a second open of either kind fails with "Could not set lock on
      file ... Conflicting lock is held".)

      A set of call sites historically passed `read_only=False` while only
      ever executing SELECTs -- `query._warm_list_runs`/`_warm_get_run`/
      `_warm_find_runs`/`run_sql`, `postmortem.find_run_for_scaffold` and
      five `mcp.py` tools. That was the main source of catalog contention
      between concurrent `bth` invocations and long-lived MCP servers, and
      they now all open read-only. Genuine writers do not come through here
      at all; they use `campaigns.connect_catalog_db` (see its docstring).
    - `missing` controls what happens when `bathos.db` does not exist:
      `"empty"` (the default) returns `duckdb.connect("")`, an anonymous
      in-memory connection with nothing attached -- this matches both the
      pre-existing default behaviour of this function and `query.run_sql`'s
      explicit `str(db_path) if db_path.exists() else ""` fallback. Most
      other call sites this wave moved onto `connect_read` guard with
      `catalog_readable(catalog_dir)` before calling (see below), so
      `missing` is never exercised there; it exists so a call site with a
      different contract (or none at all -- `run_sql`, and any future
      caller) does not have to re-invent the pass-through. (An earlier
      revision of this docstring claimed EVERY migrated call site guards
      this way; a review caught one that didn't -- `viz/html.py`'s
      `_project_campaigns`, fixed alongside `catalog_readable` itself.)

    Flag ON (after cut-over): ATTACHes `index.db` read-only for this call
    only (never held across calls) and exposes `runs`/`events`/... as views
    over it. If `index.db` does not exist yet, the views are the empty tables
    from a freshly-initialized in-memory schema (spec: "the views are built
    from the events alone"). `read_only`/`missing` are flag-off-only knobs --
    once on, every read is through a fresh read-only ATTACH by construction
    and there is no "missing bathos.db" question to ask.

    Every call is independent -- the connection is never cached or held by a
    caller across calls, so a long-lived process (the MCP server) never pins
    a stale generation.
    """
    from bathos.runlog.mode import is_log_mode

    cd = catalog_dir or default_catalog_dir()
    if not is_log_mode(cd):
        db_path = cd / "bathos.db"
        if not db_path.exists():
            if missing == "empty":
                return duckdb.connect("")
            raise FileNotFoundError(f"catalog database not found: {db_path}")
        return duckdb.connect(str(db_path), read_only=read_only)

    idx_path = index_db_path(cd)
    con = duckdb.connect(":memory:")
    if idx_path.exists():
        # DuckDB's ATTACH does not accept a bound parameter for the path;
        # escape single quotes (SQL-string style) rather than parameterize.
        escaped = str(idx_path).replace("'", "''")
        con.execute(f"ATTACH '{escaped}' AS idx (READ_ONLY)")
        con.execute("CREATE VIEW runs AS SELECT * FROM idx.runs")
        con.execute("CREATE VIEW events AS SELECT * FROM idx.events")
        con.execute("CREATE VIEW quarantine AS SELECT * FROM idx.quarantine")
        con.execute("CREATE VIEW ingest_watermarks AS SELECT * FROM idx.ingest_watermarks")
        con.execute("CREATE VIEW campaigns AS SELECT * FROM idx.campaigns")
        con.execute("CREATE VIEW campaign_runs AS SELECT * FROM idx.campaign_runs")
        con.execute("CREATE VIEW campaign_edges AS SELECT * FROM idx.campaign_edges")
        con.execute("CREATE VIEW run_edges AS SELECT * FROM idx.run_edges")
        con.execute("CREATE VIEW sidecar_anchors AS SELECT * FROM idx.sidecar_anchors")
        con.execute("CREATE VIEW blast_radius_ledger AS SELECT * FROM idx.blast_radius_ledger")
        con.execute("CREATE VIEW trust_ledger AS SELECT * FROM idx.trust_ledger")
        con.execute("CREATE VIEW archived_items AS SELECT * FROM idx.archived_items")
        con.execute("CREATE VIEW submits AS SELECT * FROM idx.submits")
    else:
        init_index_schema(con)
    return con


def catalog_readable(catalog_dir: Path | None = None) -> bool:
    """True if there is something for `connect_read` to read.

    Review finding (AC-18, post-4727b872): about a dozen migrated readers
    guarded their `connect_read` call with a bare `(catalog_dir /
    "bathos.db").exists()` check carried over unchanged from the
    pre-migration code. That guard is correct before cut-over, but Migration
    step 4(d) renames `bathos.db` to `bathos.db.frozen`, so the guard goes
    false forever afterwards -- those readers would silently report "no
    catalog" instead of falling through to the folded index.

    The fix is this one helper, used in place of the bare `.exists()` check
    everywhere `connect_read` is guarded: true when the runlog flag is on
    (the index is authoritative regardless of whether `bathos.db` happens to
    still exist -- `connect_read`'s flag-on branch handles a missing
    `index.db` itself, by returning the empty schema, so "flag on" alone is
    always "something readable") or when the legacy `bathos.db` exists
    (flag off, today's behaviour, unchanged).
    """
    from bathos.runlog.mode import is_log_mode

    cd = catalog_dir or default_catalog_dir()
    return is_log_mode(cd) or (cd / "bathos.db").exists()


#: Substring of the `duckdb.IOException` message DuckDB raises when another
#: process holds the file's OS-level lock (verified 2026-09-25, duckdb
#: 1.5.2, against a real cross-process lock: "IO Error: Could not set lock
#: on file ...: Conflicting lock is held in ...").
_LOCK_ERROR_MARKER = "Could not set lock on file"

#: Substring of the `duckdb.IOException` message DuckDB raises when the file
#: exists but is not a DuckDB database at all -- corrupt, truncated, or a
#: stray non-DuckDB file at that path (verified 2026-09-26: "IO Error: The
#: file \"...\" exists, but it is not a valid DuckDB database file!").
_CORRUPT_ERROR_MARKER = "not a valid DuckDB database file"


def connect_legacy(
    path: Path | str,
) -> duckdb.DuckDBPyConnection | dict[str, str]:
    """Open a legacy catalog database read-only (spec AC-18).

    "The importer and `bth verify` open legacy databases (`bathos.db`,
    `bathos.db.frozen`) only through `bathos.index.connect_legacy(path)`,
    which opens read-only and, if the file is locked by another process
    (e.g. an older install), returns a structured `legacy_db_locked` result
    instead of raising; `bth migrate --to-log` aborts on it, a post-cut-over
    `--import-legacy` skips that source and reports it." AC-24 additionally
    names a `corrupt_legacy_source` finding as distinct from
    `legacy_db_locked` -- both are raised by DuckDB as the SAME exception
    type (`duckdb.IOException`) with different messages, so this function
    tells them apart by message rather than folding them into one result.

    Returns the read-only `duckdb.DuckDBPyConnection` on success, or one of
    two structured dicts instead of raising:

    - `{"status": "legacy_db_locked", "path": ..., "reason": ...}` when the
      file exists but a concurrent writer holds its OS-level lock (message
      contains `_LOCK_ERROR_MARKER`).
    - `{"status": "corrupt_legacy_source", "path": ..., "reason": ...}` when
      the file exists but is not a valid DuckDB database at all -- corrupt,
      truncated, or garbage at that path (message contains
      `_CORRUPT_ERROR_MARKER`).

    Callers (step 4's importer and `bth verify`, neither built yet) are
    expected to turn either dict into their own structured finding/abort
    rather than raising it themselves. Any other `duckdb.IOException` (a
    case neither marker matches) is re-raised unchanged -- this function
    only recognizes the two failure modes the spec names, not every
    possible I/O error.

    A genuinely missing file is a caller error, not a lock or corruption --
    callers decide whether a legacy source is expected to exist before
    opening it, so this raises `FileNotFoundError` rather than folding
    "missing" into either structured result (DuckDB's own message for a
    missing read-only open is also an `IOException`, so all three cases
    must be told apart before the connect attempt, not after).
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"legacy catalog database not found: {p}")
    try:
        return duckdb.connect(str(p), read_only=True)
    except duckdb.IOException as exc:
        msg = str(exc)
        if _LOCK_ERROR_MARKER in msg:
            return {"status": "legacy_db_locked", "path": str(p), "reason": msg}
        if _CORRUPT_ERROR_MARKER in msg:
            return {"status": "corrupt_legacy_source", "path": str(p), "reason": msg}
        raise


__all__ = [
    "ARCHIVED_ITEMS_COLUMNS",
    "ARCHIVED_ITEMS_DDL",
    "BLAST_RADIUS_LEDGER_COLUMNS",
    "BLAST_RADIUS_LEDGER_DDL",
    "CAMPAIGNS_COLUMNS",
    "CAMPAIGNS_DDL",
    "CAMPAIGN_EDGES_COLUMNS",
    "CAMPAIGN_EDGES_DDL",
    "CAMPAIGN_RUNS_COLUMNS",
    "CAMPAIGN_RUNS_DDL",
    "EVENTS_DDL",
    "QUARANTINE_DDL",
    "RUNS_COLUMNS",
    "RUNS_DDL",
    "RUN_EDGES_COLUMNS",
    "RUN_EDGES_DDL",
    "SIDECAR_ANCHORS_COLUMNS",
    "SIDECAR_ANCHORS_DDL",
    "SUBMITS_COLUMNS",
    "SUBMITS_DDL",
    "TRUST_LEDGER_COLUMNS",
    "TRUST_LEDGER_DDL",
    "WATERMARKS_DDL",
    "catalog_readable",
    "connect_legacy",
    "connect_read",
    "index_db_path",
    "init_index_schema",
]
