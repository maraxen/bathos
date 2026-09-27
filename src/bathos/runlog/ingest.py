"""Generation-swap ingest (spec "Index ingest (generation swap)").

Reads new bytes out of every discovered log root, validates and dedups by
`eid`, appends to a disposable COPY of `index.db`, re-folds every affected
run entity from its complete history, then atomically swaps the copy onto
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
from bathos.runlog.fold_runs import fold_run
from bathos.runlog.index import RUNS_COLUMNS, index_db_path, init_index_schema
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


def _insert_run_row(con: duckdb.DuckDBPyConnection, row: dict) -> None:
    values = []
    for col in RUNS_COLUMNS:
        v = row.get(col)
        if isinstance(v, (list, dict)):
            v = json.dumps(v)
        values.append(v)
    placeholders = ", ".join("?" for _ in RUNS_COLUMNS)
    con.execute(
        f"INSERT INTO runs ({', '.join(RUNS_COLUMNS)}) VALUES ({placeholders})",
        values,
    )


def _refold_run(con: duckdb.DuckDBPyConnection, run_id: str) -> None:
    rows = con.execute(
        "SELECT eid, kind, entity, ts, project, project_id, writer, seq, main_root, "
        "worktree_root, origin, data FROM events WHERE entity_key = ?",
        [json.dumps([run_id])],
    ).fetchall()
    events = [
        {
            "eid": r[0],
            "kind": r[1],
            "entity": json.loads(r[2]),
            "ts": r[3],
            "project": r[4],
            "project_id": r[5],
            "writer": r[6],
            "seq": r[7],
            "main_root": r[8],
            "worktree_root": r[9],
            "origin": r[10],
            "data": json.loads(r[11]),
        }
        for r in rows
    ]
    con.execute("DELETE FROM runs WHERE id = ?", [run_id])
    if not events:
        return
    _insert_run_row(con, fold_run(events))


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
                    if obj["kind"] in _RUN_KINDS and len(obj["entity"]) == 1:
                        affected_run_ids.add(obj["entity"][0])
                if new_offset != prev_offset:
                    watermarks[wm_key] = new_offset
                    _upsert_watermark(con, root_kind, root_id, seg.name, size, new_offset)

        for run_id in affected_run_ids:
            _refold_run(con, run_id)

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
