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

import contextlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from bathos.ledger import AppendOnlyLedger, now_iso

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
    record_id TEXT PRIMARY KEY,
    script_sha256 TEXT,
    run_ids TEXT
)
"""

_FRAGMENTS_DIRNAME = "archived_items"

_ARCHIVED_COLUMNS = (
    "id",
    "project_slug",
    "event",
    "kind",
    "paths",
    "pre_archive_sha",
    "stub_commit_sha",
    "verdict",
    "reason",
    "superseded_by",
    "bundle_sha256",
    "bundle_path",
    "archived_by",
    "recorded_at",
    "record_id",
    "script_sha256",
    "run_ids",
)
_ARCHIVED_LEGACY_COLUMNS = tuple(
    c for c in _ARCHIVED_COLUMNS if c not in ("script_sha256", "run_ids")
)

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
        pa.field("script_sha256", pa.string()),
        pa.field("run_ids", pa.string()),  # JSON-encoded list[str]
    ]
)


def _now_iso() -> str:
    return now_iso()


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
    script_sha256: str | None = None
    run_ids: list[str] = field(default_factory=list)


def _parse_json_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        return json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []


def _record_to_fragment_row(record: ArchivedItemRecord) -> dict[str, list[Any]]:
    return {
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
        "script_sha256": [record.script_sha256],
        "run_ids": [json.dumps(record.run_ids) if record.run_ids else None],
    }


def _record_to_warm_values(record: ArchivedItemRecord) -> list[Any]:
    return [
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
        record.script_sha256,
        json.dumps(record.run_ids) if record.run_ids else None,
    ]


def _record_from_values(values: Mapping[str, Any]) -> ArchivedItemRecord:
    return ArchivedItemRecord(
        record_id=values["record_id"],
        id=values["id"],
        project_slug=values["project_slug"],
        event=values["event"],
        kind=values.get("kind") or "",
        paths=_parse_json_list(values.get("paths")),
        pre_archive_sha=values.get("pre_archive_sha") or "",
        stub_commit_sha=values.get("stub_commit_sha") or "",
        verdict=values.get("verdict") or "",
        reason=values.get("reason") or "",
        superseded_by=values.get("superseded_by") or "",
        bundle_sha256=values.get("bundle_sha256") or "",
        bundle_path=values.get("bundle_path") or "",
        archived_by=values.get("archived_by") or "",
        recorded_at=values["recorded_at"],
        script_sha256=values.get("script_sha256") or None,
        run_ids=_parse_json_list(values.get("run_ids")),
    )


def _fragments_dir(catalog_dir: Path | str) -> Path:
    return _LEDGER.fragments_dir(catalog_dir)


def _ensure_table_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create ``archived_items`` if absent and migrate 15-column legacy tables."""
    con.execute(_TABLE_SCHEMA)
    for alter_sql in (
        "ALTER TABLE archived_items ADD COLUMN IF NOT EXISTS script_sha256 TEXT",
        "ALTER TABLE archived_items ADD COLUMN IF NOT EXISTS run_ids TEXT",
    ):
        with contextlib.suppress(Exception):
            con.execute(alter_sql)


def _connect(catalog_dir: Path | str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(Path(catalog_dir) / "bathos.db"))
    _ensure_table_schema(con)
    return con


_LEDGER: AppendOnlyLedger[ArchivedItemRecord] = AppendOnlyLedger(
    table_name="archived_items",
    fragments_dirname=_FRAGMENTS_DIRNAME,
    fragment_prefix="archived",
    pk_col="record_id",
    order_col="recorded_at",
    columns=_ARCHIVED_COLUMNS,
    legacy_columns=_ARCHIVED_LEGACY_COLUMNS,
    fragment_schema=_FRAGMENT_SCHEMA,
    runlog_kind="archived_item.recorded",
    telemetry_prefix="archived_items",
    connect_fn=_connect,
    record_from_values=_record_from_values,
    record_to_fragment_row=_record_to_fragment_row,
    record_to_warm_values=_record_to_warm_values,
    append_event_fields=lambda r: {
        "id": r.id,
        "event_type": r.event,
        "project_slug": r.project_slug,
    },
)


def write_archived_item_fragment(record: ArchivedItemRecord, catalog_dir: Path | str) -> None:
    """Write an immutable cool-tier Parquet fragment for one ledger record.

    One fragment per record, keyed by the record's own ``record_id`` -- never superseded on
    read-back, exactly like ``trust_ledger.write_ledger_fragment``.
    """
    _LEDGER.write_fragment(record, catalog_dir)


def read_archived_item_fragments(catalog_dir: Path | str) -> list[ArchivedItemRecord]:
    """Read every cool-tier fragment, unfolded (full history, no latest-wins collapse) --
    the append-only read path used by compact-time re-ingestion."""
    return _LEDGER.read_fragments(catalog_dir)


def _insert_warm_row(record: ArchivedItemRecord, catalog_dir: Path | str) -> None:
    _LEDGER.insert_warm_row(record, catalog_dir)


def _insert_row(con: duckdb.DuckDBPyConnection, record: ArchivedItemRecord) -> None:
    _LEDGER.insert_warm_row_using_conn(con, record)


def append_archived_item_record(
    record: ArchivedItemRecord, catalog_dir: Path | str, *, cwd: Path | None = None
) -> ArchivedItemRecord:
    """Durably append one ledger record: cool-tier fragment + warm-tier row.

    The single write path for both archiving and restoring -- there is no separate
    non-durable variant, matching ``trust_ledger.append_ledger_record``.

    AC-25: behind the runlog flag, emits `archived_item.recorded` (entity:
    `record_id`; `data.event` is `archived`/`restored`, `data.id` is the item)
    instead of the fragment + warm-row writes; flag off performs only those
    legacy writes, unchanged. `cwd` resolves the runlog project/root when the
    flag is on (defaults to `Path.cwd()` via `resolve_log_root`).
    """
    return _LEDGER.append_record(record, catalog_dir, cwd=cwd)


def latest_status(catalog_dir: Path | str, item_id: str) -> ArchivedItemRecord | None:
    """Return the latest-by-``recorded_at`` record for ``item_id``, or ``None`` if the item
    has never been archived. Folds "archived" -> possibly-later "restored", latest-wins,
    mirroring ``trust_ledger.latest_ledger_record``."""
    return _LEDGER.latest_record_log_aware(catalog_dir, [("id", item_id)])
