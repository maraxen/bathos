from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from bathos.schema import COOL_SCHEMA, CURRENT_SCHEMA_VERSION


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

    fragments = list(runs_dir.rglob("run_*.parquet"))
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
        # Stamp schema_version to current on all upgraded fragments
        schema_version_idx = tbl.schema.get_field_index("schema_version")
        if schema_version_idx >= 0:
            tbl = tbl.set_column(
                schema_version_idx,
                "schema_version",
                pa.array([CURRENT_SCHEMA_VERSION] * len(tbl), type=pa.string()),
            )
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


@dataclass
class MigrateToSubdirsResult:
    moved: int
    skipped: int
    by_slug: dict
    dry_run: bool


def migrate_to_project_subdirs(catalog_dir: Path, dry_run: bool = False) -> MigrateToSubdirsResult:
    """Move flat cool-tier run parquets into per-project subdirectories.

    Reads project_slug from each parquet and moves it to runs/<slug>/run_<uuid>.parquet.
    Files already in a subdir are skipped. Idempotent.
    Falls back to 'default' for parquets without a readable project_slug.
    """
    from collections import Counter

    runs_dir = catalog_dir / "runs"
    if not runs_dir.exists():
        return MigrateToSubdirsResult(moved=0, skipped=0, by_slug={}, dry_run=dry_run)

    # Only consider directly-nested parquets (flat layout), not already-subdir'd ones
    flat_files = [f for f in runs_dir.glob("run_*.parquet")]

    moves = []
    for parquet in flat_files:
        slug = _read_project_slug(parquet) or "default"
        dst = runs_dir / slug / parquet.name
        moves.append((parquet, dst))

    if not dry_run:
        for src, dst in moves:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)

    by_slug = dict(Counter(dst.parent.name for _, dst in moves))
    return MigrateToSubdirsResult(
        moved=len(moves),
        skipped=len(flat_files) - len(moves),
        by_slug=by_slug,
        dry_run=dry_run,
    )


def _read_project_slug(parquet: Path) -> str | None:
    """Read project_slug from the first row of a parquet file."""
    try:
        tbl = pq.read_table(parquet, columns=["project_slug"])
        if tbl.num_rows > 0:
            return tbl["project_slug"][0].as_py()
    except Exception:
        pass
    return None


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
