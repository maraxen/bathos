"""`~/.bth/catalog/index.db` schema and the read API (spec "Index ingest
(generation swap)" and "Reads (G3)").

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
) -> duckdb.DuckDBPyConnection:
    """The one flag-gated read entry point (spec `bathos.index.connect_read`).

    Flag OFF (before cut-over): a pass-through. Opens `bathos.db` read-only,
    exactly as today's ad hoc call sites do (spec "Reads": "creates no views
    and does not use `connect_legacy`"). Missing `bathos.db` returns an
    in-memory connection with nothing attached, matching `run_sql`'s existing
    fallback to `""` when the file is absent.

    Flag ON (after cut-over): ATTACHes `index.db` read-only for this call
    only (never held across calls) and exposes `runs`/`events` as views over
    it. If `index.db` does not exist yet, the views are the empty tables
    from a freshly-initialized in-memory schema (spec: "the views are built
    from the events alone").

    Every call is independent -- the connection is never cached or held by a
    caller across calls, so a long-lived process (the MCP server) never pins
    a stale generation.
    """
    from bathos.runlog.mode import is_log_mode

    cd = catalog_dir or default_catalog_dir()
    if not is_log_mode(cd):
        db_path = cd / "bathos.db"
        if not db_path.exists():
            return duckdb.connect("")
        return duckdb.connect(str(db_path), read_only=True)

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
    "connect_read",
    "index_db_path",
    "init_index_schema",
]
