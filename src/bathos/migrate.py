from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from bathos.schema import COOL_SCHEMA


@dataclass
class MigrateResult:
    scanned: int
    already_current: int
    migrated: int
    dry_run: bool


def migrate_catalog(catalog_dir: Path, dry_run: bool = False) -> MigrateResult:
    """Scan cool-tier fragments and rewrite any missing COOL_SCHEMA columns."""
    runs_dir = catalog_dir / "runs"
    if not runs_dir.exists():
        return MigrateResult(scanned=0, already_current=0, migrated=0, dry_run=dry_run)

    fragments = list(runs_dir.glob("run_*.parquet"))
    current_names = {f.name for f in COOL_SCHEMA}
    migrated = 0

    for frag in fragments:
        tbl = pq.read_table(frag)
        existing = set(tbl.schema.names)
        missing = current_names - existing
        if not missing:
            continue
        migrated += 1
        if dry_run:
            continue
        for field in COOL_SCHEMA:
            if field.name not in existing:
                tbl = tbl.append_column(field, _default_array(field.type, len(tbl)))
        tbl = tbl.select([f.name for f in COOL_SCHEMA]).cast(COOL_SCHEMA)
        tmp = frag.with_suffix(".tmp")
        pq.write_table(tbl, tmp)
        tmp.replace(frag)

    already_current = len(fragments) - migrated
    return MigrateResult(
        scanned=len(fragments),
        already_current=already_current,
        migrated=migrated,
        dry_run=dry_run,
    )


def _default_array(arrow_type: pa.DataType, n: int) -> pa.Array:
    if pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type):
        return pa.array([""] * n, type=arrow_type)
    if pa.types.is_boolean(arrow_type):
        return pa.array([False] * n, type=arrow_type)
    if pa.types.is_integer(arrow_type):
        return pa.array([0] * n, type=arrow_type)
    if pa.types.is_floating(arrow_type):
        return pa.array([0.0] * n, type=arrow_type)
    if pa.types.is_list(arrow_type):
        return pa.array([[]] * n, type=arrow_type)
    if pa.types.is_timestamp(arrow_type):
        return pa.array([datetime.now(UTC)] * n, type=arrow_type)
    return pa.array([None] * n, type=arrow_type)
