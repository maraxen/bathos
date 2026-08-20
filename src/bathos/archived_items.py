"""Durable ledger of archived script/output bundles (bathos artifact archival).

Mirrors ``bathos.trust_ledger`` exactly: an immutable cool-tier Parquet fragment per
append (SLURM-safe, atomic tmp-write + rename) plus a warm-tier DuckDB table re-derived
from those fragments on every ``bth compact`` (see ``compact._ingest_archived_item_fragments``,
wired in the same way ``_ingest_ledger_fragments`` is). Reusing this substrate rather than
inventing a new one is deliberate: ``trust_ledger.py`` already solves "durable, append-only,
SLURM-safe record with a warm query surface," which is exactly what archival needs.

Append-only: :func:`restore_archived_item` in ``bathos.artifact_archive`` never deletes or
mutates an existing record. It appends a NEW record with ``event="restored"`` for the same
``item_id`` — :func:`latest_status` folds these latest-wins by ``recorded_at``, the same
pattern ``trust_ledger.fold_trust_state`` uses for promotion state.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from bathos.telemetry import event

_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS archived_items (
    id TEXT NOT NULL,
    project_slug TEXT NOT NULL,
    event TEXT NOT NULL,
    kind TEXT,
    paths TEXT,
    pre_archive_sha TEXT,
    stub_commit_sha TEXT,
    verdict TEXT,
    reason TEXT,
    superseded_by TEXT,
    bundle_sha256 TEXT,
    bundle_path TEXT,
    archived_by TEXT,
    recorded_at TEXT NOT NULL,
    record_id TEXT PRIMARY KEY
)
"""

_FRAGMENTS_DIRNAME = "archived_items"

_FRAGMENT_SCHEMA = pa.schema(
    [
        pa.field("record_id", pa.string()),
        pa.field("id", pa.string()),
        pa.field("project_slug", pa.string()),
        pa.field("event", pa.string()),
        pa.field("kind", pa.string()),
        pa.field("paths", pa.string()),  # JSON-encoded list[str]
        pa.field("pre_archive_sha", pa.string()),
        pa.field("stub_commit_sha", pa.string()),
        pa.field("verdict", pa.string()),
        pa.field("reason", pa.string()),
        pa.field("superseded_by", pa.string()),
        pa.field("bundle_sha256", pa.string()),
        pa.field("bundle_path", pa.string()),
        pa.field("archived_by", pa.string()),
        pa.field("recorded_at", pa.string()),
    ]
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ArchivedItemRecord:
    """One append-only ledger entry for an archived (or later restored) item.

    ``event`` is ``"archived"`` or ``"restored"`` -- the ledger records the transition, not
    just a mutable current state, mirroring ``TrustLedgerRecord``'s ``from_state``/``to_state``
    shape (here collapsed to a single ``event`` column since there are only two states and no
    meaningful "from").
    """

    id: str  # stable across the archived -> restored lifecycle of one bundle
    project_slug: str
    event: str  # "archived" | "restored"
    kind: str = ""
    paths: list[str] = field(default_factory=list)
    pre_archive_sha: str = ""
    stub_commit_sha: str = ""
    verdict: str = ""
    reason: str = ""
    superseded_by: str = ""
    bundle_sha256: str = ""
    bundle_path: str = ""
    archived_by: str = ""
    recorded_at: str = field(default_factory=_now_iso)
    record_id: str = field(default_factory=lambda: str(uuid.uuid4()))


def _fragments_dir(catalog_dir: Path | str) -> Path:
    return Path(catalog_dir) / _FRAGMENTS_DIRNAME


def write_archived_item_fragment(record: ArchivedItemRecord, catalog_dir: Path | str) -> None:
    """Write an immutable cool-tier Parquet fragment for one ledger record.

    One fragment per record, keyed by the record's own ``record_id`` -- never superseded on
    read-back, exactly like ``trust_ledger.write_ledger_fragment``.
    """
    import json

    frag_dir = _fragments_dir(catalog_dir)
    frag_dir.mkdir(parents=True, exist_ok=True)
    target = frag_dir / f"archived_{record.record_id}.parquet"
    tmp = frag_dir / f"archived_{record.record_id}.tmp.parquet"

    t_start = time.monotonic()
    table = pa.table(
        {
            "record_id": [record.record_id],
            "id": [record.id],
            "project_slug": [record.project_slug],
            "event": [record.event],
            "kind": [record.kind],
            "paths": [json.dumps(record.paths)],
            "pre_archive_sha": [record.pre_archive_sha],
            "stub_commit_sha": [record.stub_commit_sha],
            "verdict": [record.verdict],
            "reason": [record.reason],
            "superseded_by": [record.superseded_by],
            "bundle_sha256": [record.bundle_sha256],
            "bundle_path": [record.bundle_path],
            "archived_by": [record.archived_by],
            "recorded_at": [record.recorded_at],
        },
        schema=_FRAGMENT_SCHEMA,
    )
    pq.write_table(table, tmp)
    tmp.rename(target)  # atomic on POSIX
    duration_ms = (time.monotonic() - t_start) * 1000

    event("archived_items.write_fragment", path=str(target), rows=1, duration_ms=int(duration_ms))


def read_archived_item_fragments(catalog_dir: Path | str) -> list[ArchivedItemRecord]:
    """Read every cool-tier fragment, unfolded (full history, no latest-wins collapse) --
    the append-only read path used by compact-time re-ingestion."""
    import json

    frag_dir = _fragments_dir(catalog_dir)
    if not frag_dir.exists():
        return []
    parquet_files = list(frag_dir.glob("archived_*.parquet"))
    if not parquet_files:
        return []

    tables = [pq.read_table(f) for f in parquet_files]
    combined = pa.concat_tables(tables, promote_options="permissive")
    pydict = combined.to_pydict()

    records = []
    for i in range(combined.num_rows):
        raw_paths = pydict["paths"][i]
        try:
            paths = json.loads(raw_paths) if raw_paths else []
        except (TypeError, ValueError):
            paths = []
        records.append(
            ArchivedItemRecord(
                record_id=pydict["record_id"][i],
                id=pydict["id"][i],
                project_slug=pydict["project_slug"][i],
                event=pydict["event"][i],
                kind=pydict["kind"][i] or "",
                paths=paths,
                pre_archive_sha=pydict["pre_archive_sha"][i] or "",
                stub_commit_sha=pydict["stub_commit_sha"][i] or "",
                verdict=pydict["verdict"][i] or "",
                reason=pydict["reason"][i] or "",
                superseded_by=pydict["superseded_by"][i] or "",
                bundle_sha256=pydict["bundle_sha256"][i] or "",
                bundle_path=pydict["bundle_path"][i] or "",
                archived_by=pydict["archived_by"][i] or "",
                recorded_at=pydict["recorded_at"][i],
            )
        )
    return records


def _connect(catalog_dir: Path | str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(Path(catalog_dir) / "bathos.db"))
    con.execute(_TABLE_SCHEMA)
    return con


def _insert_warm_row(record: ArchivedItemRecord, catalog_dir: Path | str) -> None:
    con = _connect(catalog_dir)
    try:
        existing = con.execute(
            "SELECT record_id FROM archived_items WHERE record_id = ?", [record.record_id]
        ).fetchone()
        if existing:
            return  # append-only: never update an existing ledger row
        _insert_row(con, record)
    finally:
        con.close()


def _insert_row(con: duckdb.DuckDBPyConnection, record: ArchivedItemRecord) -> None:
    import json

    con.execute(
        "INSERT INTO archived_items "
        "(id, project_slug, event, kind, paths, pre_archive_sha, stub_commit_sha, verdict, "
        "reason, superseded_by, bundle_sha256, bundle_path, archived_by, recorded_at, record_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            record.id,
            record.project_slug,
            record.event,
            record.kind,
            json.dumps(record.paths),
            record.pre_archive_sha,
            record.stub_commit_sha,
            record.verdict,
            record.reason,
            record.superseded_by,
            record.bundle_sha256,
            record.bundle_path,
            record.archived_by,
            record.recorded_at,
            record.record_id,
        ],
    )


def append_archived_item_record(
    record: ArchivedItemRecord, catalog_dir: Path | str
) -> ArchivedItemRecord:
    """Durably append one ledger record: cool-tier fragment + warm-tier row.

    The single write path for both archiving and restoring -- there is no separate
    non-durable variant, matching ``trust_ledger.append_ledger_record``.
    """
    write_archived_item_fragment(record, catalog_dir)
    _insert_warm_row(record, catalog_dir)
    event(
        "archived_items.append",
        id=record.id,
        event_type=record.event,
        project_slug=record.project_slug,
    )
    return record


def latest_status(catalog_dir: Path | str, item_id: str) -> ArchivedItemRecord | None:
    """Return the latest-by-``recorded_at`` record for ``item_id``, or ``None`` if the item
    has never been archived. Folds "archived" -> possibly-later "restored", latest-wins,
    mirroring ``trust_ledger.latest_ledger_record``."""
    con = _connect(catalog_dir)
    try:
        row = con.execute(
            "SELECT id, project_slug, event, kind, paths, pre_archive_sha, stub_commit_sha, "
            "verdict, reason, superseded_by, bundle_sha256, bundle_path, archived_by, "
            "recorded_at, record_id FROM archived_items WHERE id = ? "
            "ORDER BY recorded_at DESC LIMIT 1",
            [item_id],
        ).fetchone()
        if row is None:
            return None
        import json

        try:
            paths = json.loads(row[4]) if row[4] else []
        except (TypeError, ValueError):
            paths = []
        return ArchivedItemRecord(
            id=row[0],
            project_slug=row[1],
            event=row[2],
            kind=row[3] or "",
            paths=paths,
            pre_archive_sha=row[5] or "",
            stub_commit_sha=row[6] or "",
            verdict=row[7] or "",
            reason=row[8] or "",
            superseded_by=row[9] or "",
            bundle_sha256=row[10] or "",
            bundle_path=row[11] or "",
            archived_by=row[12] or "",
            recorded_at=row[13],
            record_id=row[14],
        )
    finally:
        con.close()
