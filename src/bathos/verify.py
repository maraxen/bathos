from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


@dataclass
class VerifyResult:
    """Result of a verify operation for one or more tiers."""

    tier: str  # "cool" | "warm" | "archive" | "all"
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def verify_cool(catalog_dir: Path) -> VerifyResult:
    """Verify cool-tier Parquet fragments.

    Checks:
    - Each run_*.parquet is readable by pyarrow
    - No .bak files are present (signals interrupted migration)
    - No .tmp files are present (signals interrupted write)
    - Row count in each fragment is >= 1
    """
    runs_dir = catalog_dir / "runs"
    errors = []
    warnings = []
    stats = {"fragments_checked": 0, "fragments_readable": 0}

    if not runs_dir.exists():
        return VerifyResult(tier="cool", ok=True, errors=[], warnings=[], stats=stats)

    # Check for .bak and .tmp files (signals of interrupted operations)
    bak_files = list(runs_dir.rglob("*.bak"))
    tmp_files = list(runs_dir.rglob("*.tmp"))

    for bak_file in bak_files:
        errors.append(f"Interrupted migration: backup file exists at {bak_file}")
        logger.error(f"Interrupted migration: {bak_file}")

    for tmp_file in tmp_files:
        errors.append(f"Interrupted write: temporary file exists at {tmp_file}")
        logger.error(f"Interrupted write: {tmp_file}")

    # Check each fragment
    fragments = list(runs_dir.rglob("run_*.parquet"))
    for frag in fragments:
        stats["fragments_checked"] += 1
        try:
            tbl = pq.read_table(str(frag))
            if len(tbl) > 0:
                stats["fragments_readable"] += 1
            else:
                errors.append(f"Empty fragment: {frag}")
                logger.error(f"Empty fragment: {frag}")
        except Exception as e:
            errors.append(f"Unreadable fragment {frag}: {e}")
            logger.error(f"Unreadable fragment {frag}: {e}")

    return VerifyResult(
        tier="cool",
        ok=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        stats=stats,
    )


def verify_warm(catalog_dir: Path) -> VerifyResult:
    """Verify warm-tier DuckDB database.

    Checks:
    - bathos.db exists
    - duckdb.connect() succeeds without IOException (header check)
    - _schema_meta table is accessible (structural check)
    - If cool-tier fragments exist AND runs table is empty:
        warn "Cool fragments exist but runs table is empty — run bth compact"
    - If no cool-tier fragments exist AND runs table is empty:
        do NOT warn (empty runs table is normal for a new installation)
    """
    from bathos.compact import CorruptDatabaseError, _open_db

    db_path = catalog_dir / "bathos.db"
    errors = []
    warnings = []
    stats = {"db_exists": db_path.exists()}

    if not db_path.exists():
        errors.append("bathos.db not found — run 'bth compact' first")
        logger.error("bathos.db not found")
        return VerifyResult(
            tier="warm",
            ok=False,
            errors=errors,
            warnings=warnings,
            stats=stats,
        )

    # Try to open database (checks header and _schema_meta)
    try:
        con = _open_db(db_path)
        stats["db_valid"] = True
        try:
            row_count = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            stats["runs_count"] = row_count
        except Exception as e:
            errors.append(f"Could not query runs table: {e}")
            logger.error(f"Could not query runs table: {e}")
        finally:
            con.close()
    except CorruptDatabaseError as e:
        errors.append(f"Database integrity check failed: {e}")
        logger.error(f"Database integrity check failed: {e}")
        return VerifyResult(
            tier="warm",
            ok=False,
            errors=errors,
            warnings=warnings,
            stats=stats,
        )
    except Exception as e:
        errors.append(f"Could not open database: {e}")
        logger.error(f"Could not open database: {e}")
        return VerifyResult(
            tier="warm",
            ok=False,
            errors=errors,
            warnings=warnings,
            stats=stats,
        )

    # Check if cool fragments exist but runs table is empty
    runs_dir = catalog_dir / "runs"
    cool_fragments = list(runs_dir.rglob("run_*.parquet")) if runs_dir.exists() else []

    if cool_fragments and stats.get("runs_count", 0) == 0:
        warnings.append(
            "Cool fragments exist but runs table is empty — run bth compact"
        )
        logger.warning("Cool fragments exist but runs table is empty")

    return VerifyResult(
        tier="warm",
        ok=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        stats=stats,
    )


def verify_archive(archive_root: Path) -> VerifyResult:
    """Verify cold-tier archive Parquet files against manifest.json checksums.

    Checks:
    - manifest.json exists and is valid JSON
    - manifest has schema_version >= "2" (contains sha256 checksums)
    - For each manifest entry: Parquet file exists, SHA256 matches, row count matches
    - Warns (does not error) on manifests with schema_version < "2"
    """
    manifest_path = archive_root / "manifest.json"
    errors = []
    warnings = []
    stats = {"manifest_exists": manifest_path.exists()}

    if not manifest_path.exists():
        return VerifyResult(
            tier="archive",
            ok=True,
            errors=[],
            warnings=["No archive found"],
            stats=stats,
        )

    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except Exception as e:
        errors.append(f"Could not read manifest.json: {e}")
        logger.error(f"Could not read manifest.json: {e}")
        return VerifyResult(
            tier="archive",
            ok=False,
            errors=errors,
            warnings=warnings,
            stats=stats,
        )

    schema_version = manifest.get("schema_version", "1")
    stats["manifest_schema_version"] = schema_version

    # Check schema version
    if schema_version < "2":
        warnings.append(
            f"Manifest schema version {schema_version} does not include checksums — "
            f"run 'bth archive' to upgrade"
        )
        logger.warning(f"Manifest schema version {schema_version} is old")

    # If no entries or old schema, just warn/return
    entries = manifest.get("entries", [])
    if not entries:
        return VerifyResult(
            tier="archive",
            ok=True,
            errors=[],
            warnings=warnings,
            stats=stats,
        )

    # Check each entry
    stats["entries_checked"] = len(entries)
    stats["entries_valid"] = 0

    for entry in entries:
        partition = entry.get("partition")
        expected_rows = entry.get("rows")
        expected_sha256 = entry.get("sha256", "")

        if not partition:
            continue

        # Reconstruct path
        parquet_path = archive_root / partition / "runs.parquet"

        if not parquet_path.exists():
            errors.append(f"Missing archived Parquet: {partition}/runs.parquet")
            logger.error(f"Missing archived Parquet: {partition}/runs.parquet")
            continue

        try:
            tbl = pq.read_table(str(parquet_path))
            actual_rows = len(tbl)

            if actual_rows != expected_rows:
                errors.append(
                    f"Row count mismatch in {partition}: expected {expected_rows}, got {actual_rows}"
                )
                logger.error(
                    f"Row count mismatch in {partition}: expected {expected_rows}, got {actual_rows}"
                )
                continue

            # If schema_version >= 2, check SHA256
            if schema_version >= "2" and expected_sha256:
                actual_sha256 = _sha256_file(parquet_path)
                if actual_sha256 != expected_sha256:
                    errors.append(
                        f"SHA256 mismatch in {partition}: "
                        f"expected {expected_sha256}, got {actual_sha256}"
                    )
                    logger.error(
                        f"SHA256 mismatch in {partition}: "
                        f"expected {expected_sha256}, got {actual_sha256}"
                    )
                    continue

            stats["entries_valid"] += 1
        except Exception as e:
            errors.append(f"Could not verify {partition}: {e}")
            logger.error(f"Could not verify {partition}: {e}")

    return VerifyResult(
        tier="archive",
        ok=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        stats=stats,
    )


def _sha256_file(path: Path) -> str:
    """Compute SHA256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def verify_all(
    catalog_dir: Path, archive_root: Path | None = None
) -> list[VerifyResult]:
    """Run verify_cool, verify_warm, and verify_archive; return all results."""
    if archive_root is None:
        archive_root = Path.home() / ".bth" / "archive"

    return [
        verify_cool(catalog_dir),
        verify_warm(catalog_dir),
        verify_archive(archive_root),
    ]
