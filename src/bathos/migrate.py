from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from bathos.schema import COOL_SCHEMA, CURRENT_SCHEMA_VERSION

logger = logging.getLogger(__name__)


@dataclass
class MigrateResult:
    scanned: int
    already_current: int
    migrated: int
    dry_run: bool
    corrupt: list[Path] = field(default_factory=list)


def migrate_catalog(
    catalog_dir: Path,
    dry_run: bool = False,
    project: str | None = None,
) -> MigrateResult:
    """Scan cool-tier fragments and rewrite any missing COOL_SCHEMA columns.

    Args:
        catalog_dir: catalog root (contains runs/<project_slug>/run_*.parquet).
        dry_run: if True, report what would change without writing.
        project: if given, scope the scan to runs/<project>/ only, instead of
            every project in the catalog (260827_bathos-catalog-robustness --
            an unscoped scan on a large catalog offers no way to migrate just
            the handful of fragments an operator actually added). Assumes the
            runs/<project_slug>/ subdirectory layout written by write_run() /
            migrate_to_project_subdirs(); a catalog never migrated to that
            layout has no per-project fragments to scope to. Default (None)
            preserves the prior all-projects behaviour -- callers that print
            the result should make that scope explicit (the CLI does).

    Corrupt or unreadable fragments are skipped rather than aborting the whole
    scan (260827_bathos-catalog-robustness) -- see MigrateResult.corrupt for
    the paths skipped, which are never auto-deleted or auto-quarantined by
    this function (use `bth repair --tier cool` for that, with its own
    explicit dry-run/--apply gate).
    """
    runs_dir = catalog_dir / "runs"
    if project is not None:
        runs_dir = runs_dir / project
    if not runs_dir.exists():
        return MigrateResult(scanned=0, already_current=0, migrated=0, dry_run=dry_run)

    fragments = sorted(runs_dir.rglob("run_*.parquet"))
    current_names = {f.name for f in COOL_SCHEMA}
    migrated = 0
    corrupt: list[Path] = []

    for frag in fragments:
        try:
            tbl = pq.read_table(frag)
        except Exception as exc:
            logger.warning("Skipping corrupt cool-tier fragment %s: %s", frag, exc)
            corrupt.append(frag)
            continue
        existing = set(tbl.schema.names)
        missing = current_names - existing
        if not missing:
            continue
        migrated += 1
        if dry_run:
            continue
        for col_field in COOL_SCHEMA:
            if col_field.name not in existing:
                tbl = tbl.append_column(
                    col_field, _default_array(col_field.type, len(tbl), col_field.name)
                )
        # Stamp schema_version to current on all upgraded fragments
        schema_version_idx = tbl.schema.get_field_index("schema_version")
        if schema_version_idx >= 0:
            tbl = tbl.set_column(
                schema_version_idx,
                "schema_version",
                pa.array([CURRENT_SCHEMA_VERSION] * len(tbl), type=pa.string()),
            )
        tbl = tbl.select([f.name for f in COOL_SCHEMA]).cast(COOL_SCHEMA)

        # Fix 3: Pre-migration backup + restore on failure
        bak = frag.with_suffix(".bak")
        tmp_path = frag.with_suffix(".tmp")
        shutil.copy2(frag, bak)
        try:
            pq.write_table(tbl, tmp_path)
            tmp_path.replace(frag)
            bak.unlink(missing_ok=True)
        except Exception:
            # Remove partial tmp if it exists
            tmp_path.unlink(missing_ok=True)
            # Restore original from backup
            if bak.exists():
                try:
                    bak.replace(frag)
                except Exception as restore_exc:
                    logger.critical(
                        "MANUAL RECOVERY REQUIRED: original fragment at %s could not be "
                        "restored from backup at %s. Both paths may be in an indeterminate "
                        "state. Error: %s",
                        frag,
                        bak,
                        restore_exc,
                    )
                    raise RuntimeError(
                        f"Original at {bak} could not be restored to {frag}; "
                        f"manual recovery required."
                    ) from restore_exc
            raise

    already_current = len(fragments) - migrated - len(corrupt)
    return MigrateResult(
        scanned=len(fragments),
        already_current=already_current,
        migrated=migrated,
        dry_run=dry_run,
        corrupt=corrupt,
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


def _default_array(arrow_type: pa.DataType, n: int, field_name: str = "") -> pa.Array:
    # Special case: stage_name, claim_discriminates, claim_isolates, parity_run_type, stdout_sha256,
    # component_id, component_sidecar_sha256 must be null (not empty string) for JSON-array and
    # optional semantic
    if field_name in (
        "stage_name",
        "claim_discriminates",
        "claim_isolates",
        "parity_run_type",
        "stdout_sha256",
        "component_id",
        "component_sidecar_sha256",
        "differential_status",
        "differential_off_value",
        "differential_on_value",
        "dependency_lock_sha256",
        "git_dirty_content_id",
        "git_provenance_source",
    ) and (pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type)):
        return pa.array([None] * n, type=arrow_type)
    # B2-02: seed/baseline_hpo_trials/baseline_hpo_compute_budget/differential_effect must
    # backfill as null, not 0 — a fragment written before this field existed has no recorded
    # value, which is a different fact from "0 was the value" (0 is a valid seed/effect size).
    if field_name in (
        "seed",
        "baseline_hpo_trials",
        "baseline_hpo_compute_budget",
        "differential_effect",
    ) and (pa.types.is_integer(arrow_type) or pa.types.is_floating(arrow_type)):
        return pa.array([None] * n, type=arrow_type)
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
