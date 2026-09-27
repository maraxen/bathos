"""Generation-swap ingest (spec "Index ingest (generation swap)").

Reads new bytes out of every discovered log root, validates and dedups by
`eid`, appends to a disposable COPY of `index.db`, re-folds every affected
entity from its complete history, then atomically swaps the copy onto
the real `index.db` (`os.replace`). A crash at any point before that final
`os.replace` leaves the real `index.db` completely untouched -- ingest never
opens it for writing, only ever a `index.db.<gen>.tmp` copy.

Scope (delivery step 3, wave a): segment discovery covers the registered
project roots (`projects.toml`), the mirror (`~/.bth/log-mirror/`), the
fallback (`~/.bth/log/fallback/`), and `~/.bth/log/unaffiliated/` -- the
roots named in the task brief. The spec's cluster root kinds
(`remote-log`/`remote-fallback`/`remote-mirror`, populated by a `bth sync
--pull` this wave does not touch) and the migration-only `staging` root kind
are NOT enumerated here; both are later-wave work (Cluster, Migration).
Per-kind JSON Schema validation (spec: "`data` is validated against a
per-kind JSON Schema ... one file per kind") is scoped down to a structural
envelope check (required top-level fields present, `entity` a list, `data`
an object) -- a full schema file per kind is not built this wave; a
malformed envelope is quarantined the same way a schema violation would be.

Scope (delivery step 3, wave b): every other "Authoritative writes" entity
now folds alongside the run fold -- the campaign fold (`campaigns` +
`campaign_runs`, `bathos.runlog.fold_campaigns`), edges (`campaign_edges` /
`run_edges`, `fold_edges`), anchors (`sidecar_anchors`, `fold_anchors`), the
three simple ledgers plus submit provenance (`blast_radius_ledger` /
`trust_ledger` / `archived_items` / `submits`, `fold_ledgers`). The run fold's
own `seq_position`/`evalue` columns (stubbed to NULL in wave a) are filled
here too, by consulting the run's own campaign membership -- see
`_refold_run`'s docstring.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from bathos.config import default_catalog_dir
from bathos.runlog.fold_anchors import fold_anchor
from bathos.runlog.fold_campaigns import fold_campaign, resolve_run_campaign_id
from bathos.runlog.fold_edges import fold_edge
from bathos.runlog.fold_ledgers import (
    fold_archived_item,
    fold_blast_radius,
    fold_submit,
    fold_trust_ledger,
)
from bathos.runlog.fold_runs import fold_run
from bathos.runlog.index import (
    ARCHIVED_ITEMS_COLUMNS,
    BLAST_RADIUS_LEDGER_COLUMNS,
    CAMPAIGN_EDGES_COLUMNS,
    CAMPAIGN_RUNS_COLUMNS,
    CAMPAIGNS_COLUMNS,
    RUN_EDGES_COLUMNS,
    RUNS_COLUMNS,
    SIDECAR_ANCHORS_COLUMNS,
    SUBMITS_COLUMNS,
    TRUST_LEDGER_COLUMNS,
    index_db_path,
    init_index_schema,
)
from bathos.runlog.mode import RunLogError, is_log_mode
from bathos.runlog.project_id import list_registered_roots

_REQUIRED_ENVELOPE_KEYS = ("v", "eid", "kind", "entity", "ts", "data")

# Event kinds whose entity is a single run_id and which the run fold cares
# about. Anything else touching a run entity (there is none yet, wave a) is
# still refolded because the affected-entity set is derived from `entity`,
# not from a kind allow-list.
_RUN_KINDS = frozenset(
    {
        "run.started",
        "run.finished",
        "run.reaped",
        "run.reap_reverted",
        "run.outputs_hashed",
        "run.postmortem_applied",
        "run.imported",
        "run_reap.imported",
    }
)

# Direct campaign-entity kinds: entity == [campaign_id].
_CAMPAIGN_DIRECT_KINDS = frozenset(
    {
        "campaign.created",
        "campaign.claim_bound",
        "campaign.threshold_set",
        "campaign.claim_bypassed",
        "campaign.concluded",
    }
)
# Campaign-membership kinds: entity == [campaign_id, run_id, ...].
_CAMPAIGN_MEMBER_KINDS = frozenset({"campaign.run_added", "campaign_run.imported"})
_RUN_STARTED_KINDS = frozenset({"run.started", "run.imported"})


class IngestWalRemainsError(RunLogError):
    """AC-19: a `.wal` file remained after closing the staged copy. The swap
    is refused (the staged tmp file and its wal are deleted) rather than
    ever replacing the real `index.db` with a copy that might not have
    flushed cleanly."""


def ingest_lock_path(catalog_dir: Path | None = None) -> Path:
    return (catalog_dir or default_catalog_dir()) / "ingest.lock"


@contextlib.contextmanager
def ingest_lock(catalog_dir: Path | None = None):
    path = ingest_lock_path(catalog_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class IngestReport:
    new_events: int = 0
    skipped_existing: int = 0
    quarantined: int = 0
    roots_scanned: int = 0
    entities_refolded: int = 0
    ran: bool = True


def discover_roots() -> list[tuple[str, str, Path]]:
    """Every `(root_kind, root_id, log_dir)` this wave enumerates -- the
    registered project roots, the mirror, the fallback, and unaffiliated
    (spec "Discovery" + "Reads", narrowed per this module's docstring).

    `root_id` for a `project` root is the resolved main root's own path
    (spec: "Root ids use `main_root`, not `project_id`, wherever two roots
    can share an id, so every file belongs to exactly one root"). Every root
    here is resolved from global, HOME-anchored state (`projects.toml`,
    `~/.bth/log-mirror/`, `~/.bth/log/fallback/`, `~/.bth/log/unaffiliated/`)
    -- none of it is scoped to a particular `catalog_dir`, so this function
    takes none; a redirected `HOME` (AC-11) is what isolates it in tests.
    """
    roots: list[tuple[str, str, Path]] = []
    for main_root in list_registered_roots():
        if not main_root.exists():
            continue
        resolved = main_root.resolve()
        # Non-recursive glob below naturally excludes `.bth/log/remote/`.
        roots.append(("project", str(resolved), resolved / ".bth" / "log"))

    mirror_root_dir = Path.home() / ".bth" / "log-mirror"
    if mirror_root_dir.is_dir():
        for entry in sorted(mirror_root_dir.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name == "_null":
                for sub in sorted(entry.iterdir()):
                    if sub.is_dir():
                        roots.append(("mirror", f"_null/{sub.name}", sub))
            else:
                roots.append(("mirror", entry.name, entry))

    fallback_root_dir = Path.home() / ".bth" / "log" / "fallback"
    if fallback_root_dir.is_dir():
        for entry in sorted(fallback_root_dir.iterdir()):
            if entry.is_dir():
                roots.append(("fallback", entry.name, entry))

    unaffiliated_dir = Path.home() / ".bth" / "log" / "unaffiliated"
    if unaffiliated_dir.is_dir():
        roots.append(("unaffiliated", "_unaffiliated", unaffiliated_dir))

    return roots


def _read_new_lines(path: Path, prev_offset: int) -> tuple[list[tuple[int, str]], int]:
    """Bytes past `prev_offset` up to (and including) the file's last `\\n`.

    Returns `([(line_start_byte_offset, raw_line), ...], new_watermark)`.
    Bytes after the last `\\n` (an unterminated tail) are never returned and
    never advance the watermark (spec "Line envelope": "the watermark stops
    at the last `\\n`").
    """
    size = path.stat().st_size
    if size < prev_offset:
        # Shrunk or replaced since the last watermark -- reconciling via the
        # mirror/other copy and reporting via `bth verify` is later-wave
        # work; this pass simply does not read it (never rewinds).
        return [], prev_offset
    with open(path, "rb") as f:
        f.seek(prev_offset)
        chunk = f.read()
    last_nl = chunk.rfind(b"\n")
    if last_nl == -1:
        return [], prev_offset
    usable = chunk[: last_nl + 1]
    new_offset = prev_offset + len(usable)
    lines: list[tuple[int, str]] = []
    line_start = prev_offset
    for raw in usable.split(b"\n")[:-1]:
        lines.append((line_start, raw.decode("utf-8", errors="replace")))
        line_start += len(raw) + 1
    return lines, new_offset


def _validate_envelope(obj: object) -> str | None:
    if not isinstance(obj, dict):
        return "not_an_object"
    for key in _REQUIRED_ENVELOPE_KEYS:
        if key not in obj:
            return f"missing_field:{key}"
    if not isinstance(obj["entity"], list) or not obj["entity"]:
        return "entity_not_nonempty_list"
    if not isinstance(obj["data"], dict):
        return "data_not_object"
    if obj.get("origin", "live") not in ("live", "migration"):
        return "bad_origin"
    return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _load_existing_events(con: duckdb.DuckDBPyConnection) -> dict[str, tuple]:
    existing: dict[str, tuple] = {}
    for eid, kind, entity, data in con.execute(
        "SELECT eid, kind, entity, data FROM events"
    ).fetchall():
        existing[eid] = (kind, json.loads(entity), json.loads(data))
    return existing


def _load_watermarks(con: duckdb.DuckDBPyConnection) -> dict[tuple[str, str, str], int]:
    wm: dict[tuple[str, str, str], int] = {}
    for root_kind, root_id, path, _size, offset in con.execute(
        "SELECT root_kind, root_id, path, size, byte_offset FROM ingest_watermarks"
    ).fetchall():
        wm[(root_kind, root_id, path)] = offset
    return wm


def _upsert_watermark(
    con: duckdb.DuckDBPyConnection, root_kind: str, root_id: str, path: str, size: int, offset: int
) -> None:
    con.execute(
        "INSERT INTO ingest_watermarks (root_kind, root_id, path, size, byte_offset) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (root_kind, root_id, path) DO UPDATE SET "
        "size = EXCLUDED.size, byte_offset = EXCLUDED.byte_offset",
        [root_kind, root_id, path, size, offset],
    )


def _insert_quarantine(
    con: duckdb.DuckDBPyConnection, file: str, byte_offset: int, reason: str, raw_line: str
) -> None:
    con.execute(
        "INSERT INTO quarantine (file, byte_offset, reason, raw_line, detected_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [file, byte_offset, reason, raw_line, _now_iso()],
    )


def _insert_event(con: duckdb.DuckDBPyConnection, obj: dict) -> None:
    entity_json = json.dumps(obj["entity"])
    con.execute(
        "INSERT INTO events (eid, v, kind, entity_key, entity, ts, project, project_id, "
        "writer, seq, main_root, worktree_root, origin, data) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            obj["eid"],
            obj.get("v"),
            obj["kind"],
            entity_json,
            entity_json,
            obj["ts"],
            obj.get("project"),
            obj.get("project_id"),
            obj.get("writer"),
            obj.get("seq"),
            obj.get("main_root"),
            obj.get("worktree_root"),
            obj.get("origin", "live"),
            json.dumps(obj["data"]),
        ],
    )


def _insert_row(con: duckdb.DuckDBPyConnection, table: str, columns: list[str], row: dict) -> None:
    values = []
    for col in columns:
        v = row.get(col)
        if isinstance(v, (list, dict)):
            v = json.dumps(v)
        values.append(v)
    placeholders = ", ".join("?" for _ in columns)
    con.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",  # noqa: S608 -- table/columns are internal constants
        values,
    )


def _insert_run_row(con: duckdb.DuckDBPyConnection, row: dict) -> None:
    _insert_row(con, "runs", RUNS_COLUMNS, row)


# --- Event fetch helpers (shared by every refold below) --------------------


def _row_to_event(r: tuple) -> dict:
    (
        eid,
        kind,
        entity,
        ts,
        project,
        project_id,
        writer,
        seq,
        main_root,
        worktree_root,
        origin,
        data,
    ) = r
    return {
        "eid": eid,
        "kind": kind,
        "entity": json.loads(entity),
        "ts": ts,
        "project": project,
        "project_id": project_id,
        "writer": writer,
        "seq": seq,
        "main_root": main_root,
        "worktree_root": worktree_root,
        "origin": origin,
        "data": json.loads(data),
    }


_EVENT_SELECT = (
    "SELECT eid, kind, entity, ts, project, project_id, writer, seq, main_root, "
    "worktree_root, origin, data FROM events"
)


def _fetch_events_by_entity_key(con: duckdb.DuckDBPyConnection, key: list[str]) -> list[dict]:
    rows = con.execute(
        f"{_EVENT_SELECT} WHERE entity_key = ?",  # noqa: S608
        [json.dumps(key)],
    ).fetchall()
    return [_row_to_event(r) for r in rows]


def _fetch_events_by_kinds(con: duckdb.DuckDBPyConnection, kinds: frozenset[str]) -> list[dict]:
    placeholders = ", ".join("?" for _ in kinds)
    rows = con.execute(
        f"{_EVENT_SELECT} WHERE kind IN ({placeholders})",  # noqa: S608
        list(kinds),
    ).fetchall()
    return [_row_to_event(r) for r in rows]


# --- Run fold ----------------------------------------------------------


def _compute_campaign_fold(
    con: duckdb.DuckDBPyConnection, campaign_id: str
) -> tuple[dict, list[dict]]:
    """Pure computation (no writes) of one campaign's fold -- shared by
    `_refold_run` (to look up a single member's `seq_position`/`evalue`) and
    `_refold_campaign` (to persist the `campaigns`/`campaign_runs` tables).
    Deriving both call sites from the SAME fresh computation, rather than
    reading back whatever `_refold_campaign` last persisted, is what keeps
    `runs.seq_position`/`runs.evalue` consistent regardless of which of the
    two entities (the run, or its campaign) happens to be refolded first in
    a given ingest batch.
    """
    campaign_events = _fetch_events_by_entity_key(con, [campaign_id])
    run_added_events = [
        e
        for e in _fetch_events_by_kinds(con, frozenset({"campaign.run_added"}))
        if (e["entity"] or [None])[0] == campaign_id
    ]
    imported_member_events = [
        e
        for e in _fetch_events_by_kinds(con, frozenset({"campaign_run.imported"}))
        if (e["entity"] or [None])[0] == campaign_id
    ]
    linked_run_ids = {
        e["entity"][0]
        for e in _fetch_events_by_kinds(con, _RUN_STARTED_KINDS)
        if e.get("entity") and (e.get("data") or {}).get("campaign_id") == campaign_id
    }

    member_ids: set[str] = set(linked_run_ids)
    for ev in run_added_events + imported_member_events:
        entity = ev.get("entity") or []
        if len(entity) >= 2:
            member_ids.add(entity[1])
    member_run_events = {rid: _fetch_events_by_entity_key(con, [rid]) for rid in member_ids}

    return fold_campaign(
        campaign_id,
        campaign_events,
        run_added_events,
        imported_member_events,
        linked_run_ids,
        member_run_events,
    )


def _campaign_ids_for_run(con: duckdb.DuckDBPyConnection, run_id: str) -> set[str]:
    """Every campaign that currently counts `run_id` as a member (spec:
    "Any event on a member run marks its campaign(s) as affected") --
    reverse lookup of `_compute_campaign_fold`'s own membership sources.
    """
    ids: set[str] = set()
    for e in _fetch_events_by_kinds(con, _CAMPAIGN_MEMBER_KINDS):
        entity = e.get("entity") or []
        if len(entity) >= 2 and entity[1] == run_id:
            ids.add(entity[0])
    for e in _fetch_events_by_kinds(con, _RUN_STARTED_KINDS):
        entity = e.get("entity") or []
        if entity and entity[0] == run_id:
            cid = (e.get("data") or {}).get("campaign_id")
            if cid:
                ids.add(cid)
    return ids


def _campaign_member_run_ids(con: duckdb.DuckDBPyConnection, campaign_id: str) -> set[str]:
    """Every run id counted as a member of `campaign_id` right now (the same
    union `_compute_campaign_fold` derives, exposed separately so the ingest
    loop can mark every current member's run entity for refold whenever the
    CAMPAIGN side changes -- e.g. a new `campaign.run_added`/`campaign.
    concluded` with no new event on the run entity itself)."""
    ids: set[str] = set()
    for e in _fetch_events_by_kinds(con, _CAMPAIGN_MEMBER_KINDS):
        entity = e.get("entity") or []
        if len(entity) >= 2 and entity[0] == campaign_id:
            ids.add(entity[1])
    for e in _fetch_events_by_kinds(con, _RUN_STARTED_KINDS):
        entity = e.get("entity") or []
        if entity and (e.get("data") or {}).get("campaign_id") == campaign_id:
            ids.add(entity[0])
    return ids


def _run_added_events_for_run(con: duckdb.DuckDBPyConnection, run_id: str) -> list[dict]:
    return [
        e
        for e in _fetch_events_by_kinds(con, frozenset({"campaign.run_added"}))
        if len(e.get("entity") or []) >= 2 and e["entity"][1] == run_id
    ]


def _cached_campaign_fold(
    con: duckdb.DuckDBPyConnection,
    campaign_id: str,
    cache: dict[str, tuple[dict, list[dict]]],
) -> tuple[dict, list[dict]]:
    """`_compute_campaign_fold`, memoized per ingest batch (review finding,
    MEDIUM: computing one campaign's fold is O(members); before this cache,
    an ingest batch that refolded M members of one campaign recomputed the
    WHOLE campaign fold once per member INSIDE `_refold_run`, plus once more
    for the campaign's own persist step -- O(M) recomputations of an O(M)
    computation, i.e. O(M^2) per batch. `cache` is one plain dict created
    fresh in `_ingest_locked` and threaded through every `_refold_run`/
    `_refold_campaign` call in that batch, so each distinct campaign_id is
    computed at most once per batch, regardless of how many of its members
    or how many of its own direct events triggered a refold.
    """
    if campaign_id not in cache:
        cache[campaign_id] = _compute_campaign_fold(con, campaign_id)
    return cache[campaign_id]


def _refold_run(
    con: duckdb.DuckDBPyConnection,
    run_id: str,
    campaign_fold_cache: dict[str, tuple[dict, list[dict]]] | None = None,
) -> None:
    """Refold one run entity, then fill `campaign_id` (review finding, HIGH),
    `seq_position`, and `evalue` (wave a's stub) from that resolved
    campaign's fold, if any.

    `campaign_id` is NOT simply whatever `fold_run` (a single-entity, `run.
    started`/`run.finished`/`run.imported`-only fold) produced -- that fold
    never sees `campaign.run_added` events (entity `[campaign_id, run_id]`,
    a different entity key entirely), so it would silently ignore a later
    reassignment via `add_run_to_campaign` (spec BC-6). The resolved value
    here is the latest-by-`(ts, eid)` assignment across BOTH sources (see
    `fold_campaigns.resolve_run_campaign_id`), and `seq_position`/`evalue`
    are read from THAT campaign's fold -- not from whichever campaign the
    ingest loop happened to also touch this batch -- so the result is
    independent of refold order (AC-1/AC-20 extended to this cross-entity
    denormalization).

    `campaign_fold_cache` should be one dict shared across an entire ingest
    batch (see `_cached_campaign_fold`); omitted only for standalone/test
    callers, where a fresh, unshared cache is harmless.
    """
    if campaign_fold_cache is None:
        campaign_fold_cache = {}
    events = _fetch_events_by_entity_key(con, [run_id])
    con.execute("DELETE FROM runs WHERE id = ?", [run_id])
    if not events:
        return
    row = fold_run(events)
    row["campaign_id"] = resolve_run_campaign_id(events, _run_added_events_for_run(con, run_id))
    campaign_id = row.get("campaign_id")
    if campaign_id:
        _campaign_row, campaign_runs_rows = _cached_campaign_fold(
            con, campaign_id, campaign_fold_cache
        )
        for cr in campaign_runs_rows:
            if cr["run_id"] == run_id:
                row["seq_position"] = cr["seq_position"]
                row["evalue"] = cr["evalue"]
                break
    _insert_run_row(con, row)


def _refold_campaign(
    con: duckdb.DuckDBPyConnection,
    campaign_id: str,
    campaign_fold_cache: dict[str, tuple[dict, list[dict]]] | None = None,
) -> None:
    if campaign_fold_cache is None:
        campaign_fold_cache = {}
    row, campaign_runs_rows = _cached_campaign_fold(con, campaign_id, campaign_fold_cache)
    con.execute("DELETE FROM campaigns WHERE id = ?", [campaign_id])
    con.execute("DELETE FROM campaign_runs WHERE campaign_id = ?", [campaign_id])
    if not row.get("mode") and not campaign_runs_rows:
        # No campaign.created and no membership at all -- nothing to persist
        # (a stray reference to a campaign id that was never actually
        # created).
        return
    _insert_row(con, "campaigns", CAMPAIGNS_COLUMNS, row)
    for cr in campaign_runs_rows:
        _insert_row(con, "campaign_runs", CAMPAIGN_RUNS_COLUMNS, cr)


def _refold_edge(con: duckdb.DuckDBPyConnection, entity_key: list[str]) -> None:
    events = _fetch_events_by_entity_key(con, entity_key)
    folded = fold_edge(events)
    src, dst = entity_key[0], entity_key[1]
    etype = entity_key[2] if len(entity_key) > 2 else None
    if etype == "campaign":
        table, child_col, parent_col, columns = (
            "campaign_edges",
            "child_campaign_id",
            "parent_campaign_id",
            CAMPAIGN_EDGES_COLUMNS,
        )
    else:
        table, child_col, parent_col, columns = (
            "run_edges",
            "child_run_id",
            "parent_run_id",
            RUN_EDGES_COLUMNS,
        )
    con.execute(
        f"DELETE FROM {table} WHERE {child_col} = ? AND {parent_col} = ?",  # noqa: S608
        [src, dst],
    )
    if folded is None:
        return
    _insert_row(
        con,
        table,
        columns,
        {child_col: folded["src"], parent_col: folded["dst"]},
    )


def _refold_anchor(con: duckdb.DuckDBPyConnection, anchor_id: str) -> None:
    events = _fetch_events_by_entity_key(con, [anchor_id])
    con.execute("DELETE FROM sidecar_anchors WHERE id = ?", [anchor_id])
    folded = fold_anchor(events)
    if folded is None:
        return
    folded["id"] = anchor_id
    _insert_row(con, "sidecar_anchors", SIDECAR_ANCHORS_COLUMNS, folded)


def _refold_blast_radius(con: duckdb.DuckDBPyConnection, record_id: str) -> None:
    events = _fetch_events_by_entity_key(con, [record_id])
    con.execute("DELETE FROM blast_radius_ledger WHERE id = ?", [record_id])
    folded = fold_blast_radius(events)
    if folded is None:
        return
    _insert_row(con, "blast_radius_ledger", BLAST_RADIUS_LEDGER_COLUMNS, folded)


def _refold_trust_ledger(con: duckdb.DuckDBPyConnection, record_id: str) -> None:
    events = _fetch_events_by_entity_key(con, [record_id])
    con.execute("DELETE FROM trust_ledger WHERE id = ?", [record_id])
    folded = fold_trust_ledger(events)
    if folded is None:
        return
    _insert_row(con, "trust_ledger", TRUST_LEDGER_COLUMNS, folded)


def _refold_archived_item(con: duckdb.DuckDBPyConnection, record_id: str) -> None:
    events = _fetch_events_by_entity_key(con, [record_id])
    con.execute("DELETE FROM archived_items WHERE record_id = ?", [record_id])
    folded = fold_archived_item(events)
    if folded is None:
        return
    _insert_row(con, "archived_items", ARCHIVED_ITEMS_COLUMNS, folded)


def _refold_submit(con: duckdb.DuckDBPyConnection, submit_id: str) -> None:
    events = _fetch_events_by_entity_key(con, [submit_id])
    con.execute("DELETE FROM submits WHERE id = ?", [submit_id])
    folded = fold_submit(events)
    if folded is None:
        return
    _insert_row(con, "submits", SUBMITS_COLUMNS, folded)


def _check_no_wal(tmp_path: Path) -> None:
    """AC-19: refuse the swap when a `.wal` remains after `close()`."""
    wal_path = tmp_path.with_name(tmp_path.name + ".wal")
    if wal_path.exists():
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        with contextlib.suppress(OSError):
            wal_path.unlink()
        raise IngestWalRemainsError(
            f"{wal_path} still present after close; refusing the generation swap"
        )


def run_ingest(catalog_dir: Path | None = None) -> IngestReport:
    """Ingest every discovered root's new bytes into a fresh generation of
    `index.db`, then atomically swap it in.

    Skipped (returns `IngestReport(ran=False)`) on a cluster node
    (`SLURM_JOB_ID` set) or when `BTH_NO_INGEST=1`, and whenever the log-mode
    flag is off (spec: "`~/.bth/catalog/index.db` is never created before the
    switch").
    """
    if os.environ.get("SLURM_JOB_ID") or os.environ.get("BTH_NO_INGEST") == "1":
        return IngestReport(ran=False)
    cd = catalog_dir or default_catalog_dir()
    if not is_log_mode(cd):
        return IngestReport(ran=False)
    with ingest_lock(cd):
        return _ingest_locked(cd)


def _ingest_locked(cd: Path) -> IngestReport:
    cd.mkdir(parents=True, exist_ok=True)
    real_path = index_db_path(cd)
    tmp_path = cd / f"index.db.{uuid.uuid4().hex[:12]}.tmp"
    if real_path.exists():
        shutil.copy2(real_path, tmp_path)

    new_events = 0
    skipped = 0
    quarantined = 0
    affected_run_ids: set[str] = set()
    affected_campaign_ids: set[str] = set()
    affected_edge_keys: set[tuple[str, ...]] = set()
    affected_anchor_ids: set[str] = set()
    affected_blast_radius_ids: set[str] = set()
    affected_trust_ledger_ids: set[str] = set()
    affected_archived_item_ids: set[str] = set()
    affected_submit_ids: set[str] = set()
    roots = discover_roots()

    con = duckdb.connect(str(tmp_path))
    try:
        init_index_schema(con)
        existing = _load_existing_events(con)
        watermarks = _load_watermarks(con)

        for root_kind, root_id, log_dir in roots:
            if not log_dir.is_dir():
                continue
            for seg in sorted(log_dir.glob("*.jsonl")):
                wm_key = (root_kind, root_id, seg.name)
                prev_offset = watermarks.get(wm_key, 0)
                size = seg.stat().st_size
                if size < prev_offset:
                    continue
                lines, new_offset = _read_new_lines(seg, prev_offset)
                for byte_offset, raw in lines:
                    if not raw.strip():
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        _insert_quarantine(con, str(seg), byte_offset, "invalid_json", raw)
                        quarantined += 1
                        continue
                    err = _validate_envelope(obj)
                    if err:
                        _insert_quarantine(con, str(seg), byte_offset, err, raw)
                        quarantined += 1
                        continue
                    eid = obj["eid"]
                    candidate_key = (obj["kind"], obj["entity"], obj["data"])
                    prior = existing.get(eid)
                    if prior is not None:
                        if prior == candidate_key:
                            skipped += 1
                        else:
                            _insert_quarantine(con, str(seg), byte_offset, "eid_conflict", raw)
                            quarantined += 1
                        continue
                    _insert_event(con, obj)
                    existing[eid] = candidate_key
                    new_events += 1

                    kind = obj["kind"]
                    entity = obj["entity"]
                    if kind in _RUN_KINDS and len(entity) == 1:
                        affected_run_ids.add(entity[0])
                        if kind in _RUN_STARTED_KINDS:
                            cid = (obj["data"] or {}).get("campaign_id")
                            if cid:
                                affected_campaign_ids.add(cid)
                    elif (kind in _CAMPAIGN_DIRECT_KINDS and len(entity) == 1) or (
                        kind in _CAMPAIGN_MEMBER_KINDS and len(entity) >= 2
                    ):
                        affected_campaign_ids.add(entity[0])
                    elif kind == "edge.added" and len(entity) >= 2:
                        affected_edge_keys.add(tuple(entity))
                    elif kind == "anchor.recorded" and len(entity) == 1:
                        affected_anchor_ids.add(entity[0])
                    elif kind == "blast_radius.recorded" and len(entity) == 1:
                        affected_blast_radius_ids.add(entity[0])
                    elif kind == "trust_ledger.recorded" and len(entity) == 1:
                        affected_trust_ledger_ids.add(entity[0])
                    elif kind == "archived_item.recorded" and len(entity) == 1:
                        affected_archived_item_ids.add(entity[0])
                    elif kind == "submit.recorded" and len(entity) == 1:
                        affected_submit_ids.add(entity[0])
                if new_offset != prev_offset:
                    watermarks[wm_key] = new_offset
                    _upsert_watermark(con, root_kind, root_id, seg.name, size, new_offset)

        # "Any event on a member run marks its campaign(s) as affected" --
        # and symmetrically, any campaign-side change must refold every
        # current member run (so its seq_position/evalue denormalization
        # stays in sync even when the run entity itself got no new event
        # this batch).
        for run_id in list(affected_run_ids):
            affected_campaign_ids |= _campaign_ids_for_run(con, run_id)
        for campaign_id in list(affected_campaign_ids):
            affected_run_ids |= _campaign_member_run_ids(con, campaign_id)

        # Shared across every refold in this batch (review finding, MEDIUM):
        # see `_cached_campaign_fold` -- a campaign touched by several
        # members' refolds, or by both a member refold and its own direct
        # persist, is computed exactly once.
        campaign_fold_cache: dict[str, tuple[dict, list[dict]]] = {}
        for run_id in affected_run_ids:
            _refold_run(con, run_id, campaign_fold_cache=campaign_fold_cache)
        for campaign_id in affected_campaign_ids:
            _refold_campaign(con, campaign_id, campaign_fold_cache=campaign_fold_cache)
        for edge_key in affected_edge_keys:
            _refold_edge(con, list(edge_key))
        for anchor_id in affected_anchor_ids:
            _refold_anchor(con, anchor_id)
        for record_id in affected_blast_radius_ids:
            _refold_blast_radius(con, record_id)
        for record_id in affected_trust_ledger_ids:
            _refold_trust_ledger(con, record_id)
        for record_id in affected_archived_item_ids:
            _refold_archived_item(con, record_id)
        for submit_id in affected_submit_ids:
            _refold_submit(con, submit_id)

        con.execute("CHECKPOINT")
    finally:
        con.close()

    _check_no_wal(tmp_path)
    os.replace(tmp_path, real_path)

    return IngestReport(
        new_events=new_events,
        skipped_existing=skipped,
        quarantined=quarantined,
        roots_scanned=len(roots),
        entities_refolded=len(affected_run_ids),
    )


__all__ = [
    "IngestReport",
    "IngestWalRemainsError",
    "discover_roots",
    "ingest_lock",
    "ingest_lock_path",
    "run_ingest",
]
