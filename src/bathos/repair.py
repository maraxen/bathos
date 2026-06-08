"""Repair tools for bathos catalog corruption recovery.

This module provides repair actions for cool-tier sentinels (.tmp, .bak),
corrupt fragments, and warm-tier database corruption. Repair actions are
scanned from verify.py findings and executed with dry-run safety gates.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from bathos.config import default_catalog_dir
from bathos.verify import verify_cool, verify_warm

logger = logging.getLogger(__name__)


@dataclass
class RepairAction:
    """A single repair action to be taken.

    Attributes:
        action: Action type: "delete_tmp", "quarantine_bak", "quarantine_corrupt",
                "backup_warm", "rebuild_warm", "reexport_from_warm"
        path: Source path of the file/database affected
        detail: Human-readable description of the action
        dry_run: Always True for scan(); can be False for repair()
    """

    action: str
    path: str
    detail: str
    dry_run: bool = True


@dataclass
class RepairManifest:
    """Result of a repair operation or dry-run scan.

    Attributes:
        run_ts: ISO8601 timestamp when repair/scan was initiated
        catalog_dir: Path to the catalog directory
        dry_run: True if no mutations were performed
        tier: Scope of repair: "cool", "warm", "archive", "all"
        actions: List of repair actions that were/would be taken
        warnings: List of warnings or issues during repair (e.g., schema mismatch)
    """

    run_ts: str
    catalog_dir: str
    dry_run: bool
    tier: str
    actions: list[RepairAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def scan(
    catalog_dir: Path | str | None = None,
    tier: str = "all",
    from_warm: bool = False,
    archive_root: Path | str | None = None,
) -> tuple[list[RepairAction], list[str]]:
    """Scan the catalog and identify repair actions needed.

    Calls verify.verify_all() (or tier-specific variant) and converts findings
    into RepairAction objects and warnings. This is a read-only operation; dry_run is always True.

    Args:
        catalog_dir: Catalog directory (default: ~/.bth/catalog/)
        tier: "cool", "warm", "archive", or "all"
        from_warm: If True, detect runs present in warm DB but missing from cool fragments
        archive_root: Archive root directory (default: ~/.bth/archive)

    Returns:
        Tuple of (list of RepairAction objects, list of warning strings), actions sorted by path
    """
    if catalog_dir is None:
        catalog_dir = default_catalog_dir()
    catalog_dir = Path(catalog_dir)

    actions: list[RepairAction] = []
    warnings: list[str] = []

    # Run verify based on tier
    if tier == "cool" or tier == "all":
        cool_result = verify_cool(catalog_dir)
        cool_actions, cool_warnings = _actions_from_cool_verify(cool_result)
        actions.extend(cool_actions)
        warnings.extend(cool_warnings)

    if tier == "warm" or tier == "all":
        warm_result = verify_warm(catalog_dir)
        warm_actions, warm_warnings = _actions_from_warm_verify(warm_result, catalog_dir)
        actions.extend(warm_actions)
        warnings.extend(warm_warnings)

    if tier == "archive" or tier == "all":
        if archive_root is None:
            archive_root_path = Path.home() / ".bth" / "archive"
        else:
            archive_root_path = Path(archive_root)
        from bathos.verify import verify_archive

        archive_result = verify_archive(archive_root_path)
        archive_actions, archive_warnings = _actions_from_archive_verify(archive_result)
        actions.extend(archive_actions)
        warnings.extend(archive_warnings)

    # Detect warm-only runs (present in warm DB but missing from cool fragments)
    if from_warm and (tier == "warm" or tier == "all"):
        warm_only_actions, warm_only_warnings = _actions_from_warm_only_runs(catalog_dir)
        actions.extend(warm_only_actions)
        warnings.extend(warm_only_warnings)

    # All actions from scan have dry_run=True
    for action in actions:
        action.dry_run = True

    # Sort by path for deterministic output
    actions.sort(key=lambda a: a.path)
    return actions, warnings


def _actions_from_cool_verify(result) -> tuple[list[RepairAction], list[str]]:
    """Convert verify_cool errors to RepairAction objects and warnings.

    Returns:
        Tuple of (list of RepairAction objects, list of warning strings)
    """
    actions: list[RepairAction] = []
    warnings: list[str] = []

    for error in result.errors:
        if "Interrupted write: temporary file exists" in error:
            # Extract path: "Interrupted write: temporary file exists at /path/to/.tmp"
            path_part = error.split(" at ", 1)[-1]
            path_obj = Path(path_part)

            # Check mtime guard: skip files with mtime < 60s (in-flight writes)
            try:
                mtime_unix = path_obj.stat().st_mtime
                age_s = (datetime.now(UTC).timestamp() - mtime_unix)
                if age_s < 60:
                    warning = f"Skipped .tmp file (in-flight write): {path_part} (age {age_s:.0f}s)"
                    warnings.append(warning)
                    continue
            except (OSError, ValueError):
                pass

            # Old .tmp files are always safe to delete
            detail = f"Delete orphaned .tmp file: {path_part}"
            actions.append(
                RepairAction(
                    action="delete_tmp",
                    path=path_part,
                    detail=detail,
                )
            )

        elif "Interrupted migration: backup file exists" in error:
            # Extract path
            path_part = error.split(" at ", 1)[-1]
            path_obj = Path(path_part)

            # Check mtime guard: skip files with mtime < 60s (migration in progress)
            try:
                mtime_unix = path_obj.stat().st_mtime
                age_s = (datetime.now(UTC).timestamp() - mtime_unix)
                if age_s < 60:
                    warning = f"Skipped .bak file (in-flight migration): {path_part} (age {age_s:.0f}s)"
                    warnings.append(warning)
                    continue
            except (OSError, ValueError):
                pass

            # Old .bak files should be quarantined
            detail = f"Quarantine .bak file: {path_part}"
            actions.append(
                RepairAction(
                    action="quarantine_bak",
                    path=path_part,
                    detail=detail,
                )
            )

        elif "Empty fragment:" in error:
            # Extract path: "Empty fragment: /path/to/run_*.parquet"
            path_part = error.split(": ", 1)[-1]
            detail = f"Delete empty fragment: {path_part}"
            actions.append(
                RepairAction(
                    action="delete_tmp",  # Empty fragments treated like incomplete writes
                    path=path_part,
                    detail=detail,
                )
            )

        elif "Unreadable fragment" in error:
            # Extract path and reason
            # "Unreadable fragment /path: <exception>"
            # Split on ": " to separate path from error message
            if ": " in error:
                path_part = error.split(": ", 1)[0].replace("Unreadable fragment ", "").strip()
                detail = f"Quarantine corrupt fragment: {path_part}"
                actions.append(
                    RepairAction(
                        action="quarantine_corrupt",
                        path=path_part,
                        detail=detail,
                    )
                )

    return actions, warnings


def _actions_from_warm_verify(result, catalog_dir: Path) -> tuple[list[RepairAction], list[str]]:
    """Convert verify_warm errors to RepairAction objects and warnings.

    Args:
        result: VerifyResult from verify_warm()
        catalog_dir: Path to catalog directory (used to locate bathos.db)

    Returns:
        Tuple of (list of RepairAction objects, list of warning strings)
    """
    actions: list[RepairAction] = []
    warnings: list[str] = []
    db_path = catalog_dir / "bathos.db"

    for error in result.errors:
        if "Database integrity check failed" in error or "Could not open database" in error:
            detail = f"Warm database corrupt; rebuild needed: {db_path}"
            actions.append(
                RepairAction(
                    action="rebuild_warm",
                    path=str(db_path),
                    detail=detail,
                )
            )

    # Also check if warm DB is present and would fail on open (preemptive detection)
    if not actions and "db_exists" in result.stats and result.stats["db_exists"] and db_path.exists():
        # Try to detect corruption by attempting to open DB
        from bathos.compact import CorruptDatabaseError, _open_db

        try:
            con = _open_db(db_path)
            con.close()
        except CorruptDatabaseError:
            detail = f"Warm database corrupt; rebuild needed: {db_path}"
            actions.append(
                RepairAction(
                    action="rebuild_warm",
                    path=str(db_path),
                    detail=detail,
                )
            )

    return actions, warnings


def _actions_from_archive_verify(result) -> tuple[list[RepairAction], list[str]]:
    """Convert verify_archive errors to RepairAction objects and warnings.

    Maps SHA256/row-count mismatches to quarantine_archive actions.
    Missing partition files are logged as warnings (data is already gone).

    Returns:
        Tuple of (list of RepairAction objects, list of warning strings)
    """
    actions: list[RepairAction] = []
    warnings: list[str] = []

    for error in result.errors:
        if "SHA256 mismatch in" in error:
            # Parse: "SHA256 mismatch in project=X/year=Y/month=Z: expected ABC, got DEF"
            parts = error.split("SHA256 mismatch in ", 1)
            if len(parts) == 2:
                remaining = parts[1]
                # Extract partition and checksums
                if ": expected " in remaining:
                    partition = remaining.split(": expected ", 1)[0]
                    checksums = remaining.split(": expected ", 1)[1]
                    detail = f"Quarantine archive partition with SHA256 mismatch: {partition} ({checksums})"
                    actions.append(
                        RepairAction(
                            action="quarantine_archive",
                            path=partition,  # Store partition path (relative to archive root)
                            detail=detail,
                        )
                    )

        elif "Row count mismatch in" in error:
            # Parse: "Row count mismatch in project=X/year=Y/month=Z: expected N, got M"
            parts = error.split("Row count mismatch in ", 1)
            if len(parts) == 2:
                remaining = parts[1]
                if ": expected " in remaining:
                    partition = remaining.split(": expected ", 1)[0]
                    counts = remaining.split(": expected ", 1)[1]
                    detail = f"Quarantine archive partition with row count mismatch: {partition} ({counts})"
                    actions.append(
                        RepairAction(
                            action="quarantine_archive",
                            path=partition,
                            detail=detail,
                        )
                    )

        elif "Missing archived Parquet:" in error:
            # Archive partition is missing; data is already lost, just warn
            partition = error.split("Missing archived Parquet: ", 1)[-1]
            warnings.append(f"Archive partition missing: {partition}")

        elif "Could not verify" in error:
            # Some other error reading the partition
            partition = error.split("Could not verify ", 1)[-1].split(": ")[0]
            detail = f"Quarantine archive partition (read error): {partition}"
            actions.append(
                RepairAction(
                    action="quarantine_archive",
                    path=partition,
                    detail=detail,
                )
            )

    return actions, warnings


def _actions_from_warm_only_runs(catalog_dir: Path) -> tuple[list[RepairAction], list[str]]:
    """Detect runs present in warm DB but missing from cool fragments.

    Returns RepairAction objects with action="reexport_from_warm" for each warm-only run.

    Returns:
        Tuple of (list of RepairAction objects, list of warning strings)
    """
    import duckdb

    actions: list[RepairAction] = []
    warnings: list[str] = []

    db_path = catalog_dir / "bathos.db"
    if not db_path.exists():
        # No warm DB; nothing to re-export
        return [], []

    # Read all cool fragment UUIDs
    try:
        from bathos.catalog import read_runs

        cool_runs = read_runs(catalog_dir)
        cool_uuids = {run.id for run in cool_runs}
    except Exception as e:
        warnings.append(f"Could not read cool fragments for warm-only detection: {e}")
        return [], warnings

    # Query warm DB for all run UUIDs
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            warm_rows = con.execute("SELECT id FROM runs").fetchall()
            warm_uuids = {row[0] for row in warm_rows}
        finally:
            con.close()
    except Exception as e:
        warnings.append(f"Could not query warm DB for run UUIDs: {e}")
        return [], warnings

    # Find warm-only UUIDs
    warm_only_uuids = warm_uuids - cool_uuids

    if not warm_only_uuids:
        return [], []

    # Create an action for each warm-only run
    for run_uuid in sorted(warm_only_uuids):
        detail = f"Re-export warm run {run_uuid} to cool fragments"
        actions.append(
            RepairAction(
                action="reexport_from_warm",
                path=str(db_path),
                detail=detail,
            )
        )

    return actions, warnings


def repair(
    catalog_dir: Path | str | None = None,
    tier: str = "all",
    dry_run: bool = False,
    acknowledge_warm_loss: bool = False,
    from_warm: bool = False,
    archive_root: Path | str | None = None,
) -> RepairManifest:
    """Execute repair actions on the catalog.

    If any RepairAction has action="rebuild_warm" and acknowledge_warm_loss is False,
    raises SystemExit(1) with a message listing affected run IDs (warm-only data).

    In dry_run=True mode, returns a manifest with dry_run=True and no filesystem mutations.

    Args:
        catalog_dir: Catalog directory (default: ~/.bth/catalog/)
        tier: "cool", "warm", "archive", or "all"
        dry_run: If True, plan actions but don't execute them
        acknowledge_warm_loss: If True and warm rebuild is needed, proceed anyway
        from_warm: If True, detect and re-export runs present in warm DB but missing from cool
        archive_root: Archive root directory (default: ~/.bth/archive)

    Returns:
        RepairManifest with actions taken and any warnings

    Raises:
        SystemExit(1): If warm DB rebuild is needed but acknowledge_warm_loss is False
    """
    if catalog_dir is None:
        catalog_dir = default_catalog_dir()
    catalog_dir = Path(catalog_dir)

    # Convert archive_root to Path if provided
    archive_root_path = None
    if archive_root is not None:
        archive_root_path = Path(archive_root)

    run_ts = datetime.now(UTC).isoformat()

    # Scan for actions needed
    actions, scan_warnings = scan(
        catalog_dir, tier, from_warm=from_warm, archive_root=archive_root_path
    )

    # Check for warm rebuild without acknowledgment
    warm_rebuild_actions = [a for a in actions if a.action == "rebuild_warm"]
    if warm_rebuild_actions and not acknowledge_warm_loss:
        # Check if warm DB has postmortem annotations or output_metadata
        postmortem_count = 0
        output_metadata_count = 0
        db_path = catalog_dir / "bathos.db"
        if db_path.exists():
            try:
                import duckdb

                con = duckdb.connect(str(db_path), read_only=True)
                try:
                    # Count postmortem annotations (any postmortem_status != 'unassigned')
                    pm_result = con.execute(
                        "SELECT COUNT(*) FROM runs WHERE postmortem_status != 'unassigned'"
                    ).fetchone()
                    postmortem_count = pm_result[0] if pm_result else 0

                    # Count output_metadata entries (non-empty JSON arrays)
                    om_result = con.execute(
                        "SELECT COUNT(*) FROM runs WHERE output_metadata IS NOT NULL AND output_metadata != '[]'"
                    ).fetchone()
                    output_metadata_count = om_result[0] if om_result else 0
                finally:
                    con.close()
            except Exception as e:
                logger.debug(f"Could not query warm DB for at-risk data: {e}")

        # Only raise gate if there's actual warm-only data to lose
        if postmortem_count > 0 or output_metadata_count > 0:
            # Get list of affected run UUIDs from cool fragments for context
            from bathos.catalog import read_runs

            try:
                cool_runs = read_runs(catalog_dir)
                run_uuids = [run.id for run in cool_runs]
            except Exception:
                run_uuids = []

            # Print the warning message with concrete counts
            warn_msg = (
                f"WARNING: Warm database rebuild will destroy warm-only data:\n"
                f"  - {postmortem_count} postmortem annotation(s)\n"
                f"  - {output_metadata_count} output_metadata entry(ies)\n"
                f"  - {len(run_uuids)} run(s) affected: {', '.join(run_uuids[:5])}"
            )
            if len(run_uuids) > 5:
                warn_msg += f" ... and {len(run_uuids) - 5} more"

            warn_msg += "\n\nTo proceed, pass --acknowledge-warm-loss"
            print(warn_msg, file=sys.stderr)
            logger.error(warn_msg)
            raise SystemExit(1)
        else:
            # No warm-only data at risk; proceed silently
            logger.info("Warm database rebuild will not lose any warm-only data (postmortem_count=0, output_metadata_count=0); proceeding")

    manifest = RepairManifest(
        run_ts=run_ts,
        catalog_dir=str(catalog_dir),
        dry_run=dry_run,
        tier=tier,
        actions=actions,
        warnings=scan_warnings,
    )

    # If dry_run, return manifest without executing
    if dry_run:
        return manifest

    # Execute each action
    for action in actions:
        try:
            _execute_repair_action(action, catalog_dir, manifest, archive_root_path)
            action.dry_run = False  # Mark action as executed
        except NotImplementedError as e:
            manifest.warnings.append(str(e))
            logger.warning(f"Action {action.action} not yet implemented: {e}")

    # Write structured post-action log
    log_path = catalog_dir / f"repair_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as f:
        for action in actions:
            f.write(json.dumps(asdict(action)) + "\n")
    logger.info(f"Repair log written to {log_path}")

    return manifest


def _handle_quarantine_bak(action: RepairAction, catalog_dir: Path) -> None:
    """Quarantine a .bak file (orphaned migration backup).

    Moves the .bak file to .bth/quarantine/<slug>/YYMMDD_HHMMSS_<basename>
    and appends a JSON manifest entry.
    """
    path = Path(action.path)
    if not path.exists():
        logger.warning(f"Quarantine target not found: {path}")
        return

    # Extract project slug from path: catalog_dir/runs/<slug>/...
    try:
        slug = path.parent.name
    except Exception:
        slug = "unknown"

    # Create quarantine directory
    quarantine_dir = catalog_dir / "quarantine" / slug
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    # Generate timestamped quarantine filename
    ts_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    quarantine_path = quarantine_dir / f"{ts_str}_{path.name}"

    # Move the file
    try:
        stat = path.stat()
        path.rename(quarantine_path)
        logger.info(f"Quarantined {path} → {quarantine_path}")

        # Append manifest entry
        manifest_entry = {
            "ts": datetime.now(UTC).isoformat(),
            "tier": "cool",
            "action": "quarantine_bak",
            "original_path": str(path),
            "moved_to": str(quarantine_path),
            "mtime_s": stat.st_mtime,
            "size_bytes": stat.st_size,
            "slug": slug,
        }
        _append_quarantine_manifest(catalog_dir, slug, manifest_entry)
    except Exception as e:
        logger.error(f"Failed to quarantine {path}: {e}")
        raise


def _handle_quarantine_corrupt(action: RepairAction, catalog_dir: Path) -> None:
    """Quarantine a corrupt fragment (non-empty, unreadable Parquet file).

    Moves the corrupt fragment to .bth/quarantine/<slug>/YYMMDD_HHMMSS_<basename>
    and appends a JSON manifest entry. Attempts to re-read the file after move;
    if it succeeds, sets transient=True (likely a transient filesystem error).
    """
    import pyarrow.parquet as pq

    path = Path(action.path)
    if not path.exists():
        logger.warning(f"Quarantine target not found: {path}")
        return

    # Extract project slug from path: catalog_dir/runs/<slug>/...
    try:
        slug = path.parent.name
    except Exception:
        slug = "unknown"

    # Create quarantine directory
    quarantine_dir = catalog_dir / "quarantine" / slug
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    # Generate timestamped quarantine filename
    ts_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    quarantine_path = quarantine_dir / f"{ts_str}_{path.name}"

    # Capture original file stat before move
    try:
        stat = path.stat()
        original_size = stat.st_size
        original_mtime = stat.st_mtime
    except Exception:
        original_size = -1
        original_mtime = -1

    # Move the file
    transient = False
    error_type = "unknown"
    error_msg = ""

    try:
        path.rename(quarantine_path)
        logger.info(f"Quarantined {path} → {quarantine_path}")

        # Try to re-read after move; if it succeeds, mark as transient
        try:
            pq.read_table(str(quarantine_path))
            transient = True
            error_type = "transient_filesystem"
            logger.info(f"Re-read successful after move; marking {quarantine_path} as transient")
        except Exception as reread_error:
            transient = False
            error_type = type(reread_error).__name__
            error_msg = str(reread_error)
            logger.debug(f"Re-read failed: {error_type}: {error_msg}")

        # Append manifest entry
        manifest_entry = {
            "ts": datetime.now(UTC).isoformat(),
            "tier": "cool",
            "action": "quarantine_corrupt",
            "original_path": str(path),
            "moved_to": str(quarantine_path),
            "mtime_s": original_mtime,
            "size_bytes": original_size,
            "slug": slug,
            "error_type": error_type,
            "error_msg": error_msg,
            "schema_valid": transient,  # Schema is valid iff error was transient (re-read succeeded)
            "transient": transient,
        }
        _append_quarantine_manifest(catalog_dir, slug, manifest_entry)
    except Exception as e:
        logger.error(f"Failed to quarantine {path}: {e}")
        raise


def _append_quarantine_manifest(catalog_dir: Path, slug: str, entry: dict) -> None:
    """Append a JSON line to the quarantine manifest for a slug."""
    manifest_path = catalog_dir / "quarantine" / slug / "manifest.jsonl"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(manifest_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        logger.info(f"Appended manifest entry to {manifest_path}")
    except Exception as e:
        logger.error(f"Failed to append manifest entry: {e}")
        raise


def _execute_repair_action(
    action: RepairAction,
    catalog_dir: Path,
    manifest: RepairManifest | None = None,
    archive_root: Path | None = None,
) -> None:
    """Execute a single repair action.

    Raises NotImplementedError for actions not yet implemented in tracks B/C/D.

    Args:
        action: The repair action to execute
        catalog_dir: Catalog directory
        manifest: Optional RepairManifest to collect warnings and messages
        archive_root: Archive root directory for archive-tier repairs (default: ~/.bth/archive)
    """
    if action.action == "delete_tmp":
        path = Path(action.path)
        if path.exists():
            path.unlink()
            logger.info(f"Deleted: {path}")

    elif action.action == "quarantine_bak":
        _handle_quarantine_bak(action, catalog_dir)

    elif action.action == "quarantine_corrupt":
        _handle_quarantine_corrupt(action, catalog_dir)

    elif action.action == "backup_warm":
        raise NotImplementedError(
            "TODO: implemented in downstream track D — backup_warm action handler"
        )

    elif action.action == "rebuild_warm":
        _handle_rebuild_warm(action, catalog_dir)

    elif action.action == "quarantine_archive":
        _handle_quarantine_archive(action, catalog_dir, archive_root)

    elif action.action == "reexport_from_warm":
        _handle_reexport_from_warm(action, catalog_dir, manifest)

    else:
        raise ValueError(f"Unknown repair action: {action.action}")


def _handle_rebuild_warm(action: RepairAction, catalog_dir: Path) -> None:
    """Rebuild warm database (bathos.db) from cool fragments.

    Calls compact(catalog_dir, force_rebuild=True), which creates a backup
    of bathos.db before deletion and rebuilds the database from cool fragments.
    """
    from bathos.compact import compact

    db_path = Path(action.path)
    logger.info(f"Rebuilding warm database: {db_path}")

    try:
        result = compact(catalog_dir, force_rebuild=True)
        logger.info(
            f"Warm database rebuilt: {result.ingested} ingested, {result.skipped} skipped"
        )
        action.detail = f"Warm database rebuilt from cool fragments: {result.ingested} rows ingested"
    except Exception as e:
        logger.error(f"Failed to rebuild warm database: {e}")
        raise


def _handle_reexport_from_warm(action: RepairAction, catalog_dir: Path, manifest: RepairManifest | None = None) -> None:
    """Re-export warm runs back to cool fragments.

    Scans warm DB (bathos.db) for runs missing from cool fragments, reconstructs
    Run dataclass objects from warm rows, and writes them back to cool-tier Parquet
    fragments using catalog.write_run().

    Skips runs with NULL metadata or other warm-only fields, logging warnings.
    Does not overwrite existing cool fragments (checks first).

    Args:
        action: RepairAction with action="reexport_from_warm"
        catalog_dir: Catalog directory
        manifest: Optional RepairManifest to collect warnings about NULL-metadata runs
    """
    import duckdb

    from bathos.catalog import read_runs, write_run
    from bathos.schema import Run

    db_path = Path(action.path)
    logger.info(f"Re-exporting warm runs to cool fragments from {db_path}")

    if not db_path.exists():
        logger.warning(f"Warm database not found: {db_path}")
        return

    # Read all cool fragment UUIDs
    try:
        cool_runs = read_runs(catalog_dir)
        cool_uuids = {run.id for run in cool_runs}
    except Exception as e:
        logger.error(f"Could not read cool fragments for re-export: {e}")
        raise

    # Get column names for reconstruction (to handle schema variations)
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            col_info = con.execute("PRAGMA table_info(runs)").fetchall()
            col_names = [col[1] for col in col_info]  # col[1] is the column name
        finally:
            con.close()
    except Exception as e:
        logger.error(f"Could not get warm DB column names: {e}")
        raise

    # Open warm DB and query all runs
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            # Fetch all columns from runs table
            warm_rows = con.execute(
                f"SELECT {', '.join(col_names)} FROM runs ORDER BY timestamp DESC"
            ).fetchall()
        finally:
            con.close()
    except Exception as e:
        logger.error(f"Could not query warm DB for re-export: {e}")
        raise

    reexported = 0
    skipped_existing = 0
    skipped_null_metadata = 0
    null_metadata_warnings: list[str] = []

    # Process each warm run
    for warm_row in warm_rows:
        # Create dict from row
        row_dict = dict(zip(col_names, warm_row))
        run_uuid = row_dict.get("id")

        # Skip runs already in cool tier
        if run_uuid in cool_uuids:
            skipped_existing += 1
            continue

        # Skip runs with NULL metadata (warm-only data that can't be safely re-exported)
        if row_dict.get("metadata") is None:
            skipped_null_metadata += 1
            warning_msg = f"Skipped run {run_uuid}: NULL metadata (warm-only data)"
            logger.warning(warning_msg)
            null_metadata_warnings.append(run_uuid)
            continue

        # Reconstruct Run object from warm row
        # Only include fields that exist in cool schema (exclude metadata and output_metadata)
        try:
            run = Run(
                id=run_uuid,
                project_slug=row_dict.get("project_slug", ""),
                command=row_dict.get("command", ""),
                argv=row_dict.get("argv", []),
                git_hash=row_dict.get("git_hash", ""),
                git_branch=row_dict.get("git_branch", ""),
                git_dirty=bool(row_dict.get("git_dirty", False)),
                timestamp=row_dict.get("timestamp") or datetime.now(UTC),
                duration_s=float(row_dict.get("duration_s", 0.0)),
                exit_code=int(row_dict.get("exit_code", -1)),
                status=row_dict.get("status", ""),
                output_paths=row_dict.get("output_paths", []),
                tags=row_dict.get("tags", []),
                schema_version=row_dict.get("schema_version", "6"),
                slurm_job_id=row_dict.get("slurm_job_id", ""),
                slurm_array_task_id=row_dict.get("slurm_array_task_id", ""),
                hostname=row_dict.get("hostname", ""),
                outcome=row_dict.get("outcome", ""),
                sidecar_sha256=row_dict.get("sidecar_sha256", ""),
                sidecar_path=row_dict.get("sidecar_path", ""),
                parent_run_id=row_dict.get("parent_run_id", ""),
                agent_mode=row_dict.get("agent_mode", ""),
                sidecar_mode=row_dict.get("sidecar_mode", ""),
                outcome_is_residual=bool(row_dict.get("outcome_is_residual", False)),
                skill_sha256=row_dict.get("skill_sha256", ""),
                campaign_id=row_dict.get("campaign_id", ""),
                script_sha256=row_dict.get("script_sha256", ""),
                postmortem_status=row_dict.get("postmortem_status", "unassigned"),
                postmortem_override=row_dict.get("postmortem_override", "none"),
                postmortem_verdict_override=row_dict.get("postmortem_verdict_override", "none"),
                postmortem_author=row_dict.get("postmortem_author", ""),
                postmortem_path=row_dict.get("postmortem_path", ""),
                postmortem_hypothesis_status=row_dict.get("postmortem_hypothesis_status", "unassigned"),
                postmortem_has_anomalies=bool(row_dict.get("postmortem_has_anomalies", False)),
                postmortem_summary=row_dict.get("postmortem_summary", ""),
                postmortem_asset_links=row_dict.get("postmortem_asset_links", "{}"),
                manifest_sha256=row_dict.get("manifest_sha256", ""),
                manifest_path=row_dict.get("manifest_path", ""),
                outcome_error_reason=row_dict.get("outcome_error_reason", ""),
                adversarial_check_status=row_dict.get("adversarial_check_status", ""),
            )

            # Check if cool fragment already exists (safety check)
            cool_fragment_path = catalog_dir / "runs" / run.project_slug / f"run_{run.id}.parquet"
            if cool_fragment_path.exists():
                logger.warning(f"Cool fragment already exists for run {run.id}; skipping")
                skipped_existing += 1
                continue

            # Write the run to cool tier
            write_run(run, catalog_dir)
            logger.info(f"Re-exported run {run.id} to {cool_fragment_path}")
            reexported += 1

        except Exception as e:
            logger.error(f"Failed to re-export run {run_uuid}: {e}")
            # Continue processing other runs on error

    # Append NULL-metadata UUIDs to manifest warnings
    if null_metadata_warnings and manifest:
        for uuid in null_metadata_warnings:
            manifest.warnings.append(f"Skipped re-export of run {uuid}: NULL metadata (warm-only data)")

    # Update action detail
    action.detail = (
        f"Re-exported {reexported} warm runs to cool fragments "
        f"(skipped {skipped_existing} existing, {skipped_null_metadata} with null metadata)"
    )
    logger.info(action.detail)


def _handle_quarantine_archive(
    action: RepairAction, catalog_dir: Path, archive_root: Path | None = None
) -> None:
    """Quarantine an archive partition file with SHA256 or row-count mismatch.

    Moves the partition file to .bth/quarantine/archive/<year>/<month>/<basename>
    (path structure extracted from the partition path), appends a JSON manifest entry,
    and atomically updates the archive manifest.json to mark the entry as "quarantined": true.

    Args:
        action: RepairAction with action="quarantine_archive" and path as partition path
                (e.g., "project=foo/year=2026/month=06")
        catalog_dir: Catalog directory (used to locate .bth/quarantine/)
        archive_root: Archive root directory (default: ~/.bth/archive)
    """
    if archive_root is None:
        archive_root = Path.home() / ".bth" / "archive"
    partition_path = action.path  # Relative path like "project=foo/year=2026/month=06"

    # Construct full path to the partition's runs.parquet file
    parquet_path = archive_root / partition_path / "runs.parquet"

    if not parquet_path.exists():
        logger.warning(f"Archive partition file not found: {parquet_path}")
        return

    # Extract year and month from partition path for quarantine directory
    # Path format: "project=X/year=Y/month=Z"
    try:
        parts = partition_path.split("/")
        month = None
        year = None
        for part in parts:
            if part.startswith("year="):
                year = part.split("=", 1)[1]
            elif part.startswith("month="):
                month = part.split("=", 1)[1]

        if not year or not month:
            # Fallback: use current date
            now = datetime.now(UTC)
            year = str(now.year)
            month = f"{now.month:02d}"
    except Exception:
        now = datetime.now(UTC)
        year = str(now.year)
        month = f"{now.month:02d}"

    # Create quarantine directory: .bth/quarantine/archive/<year>/<month>/
    quarantine_dir = catalog_dir / "quarantine" / "archive" / year / month
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    # Generate timestamped quarantine filename with run_uuid if available
    ts_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    quarantine_path = quarantine_dir / f"{ts_str}_{parquet_path.name}"

    # Capture file stats before move
    try:
        stat = parquet_path.stat()
        original_size = stat.st_size
        original_mtime = stat.st_mtime
    except Exception:
        original_size = -1
        original_mtime = -1

    # Move the file
    try:
        parquet_path.rename(quarantine_path)
        logger.info(f"Quarantined archive partition: {parquet_path} → {quarantine_path}")
    except Exception as e:
        logger.error(f"Failed to move archive partition {parquet_path}: {e}")
        raise

    # Compute actual SHA256 of the quarantined file
    from bathos.verify import _sha256_file

    actual_sha256 = _sha256_file(quarantine_path)

    # Read archive manifest to extract expected SHA256
    expected_sha256 = ""
    manifest_json_path = archive_root / "manifest.json"
    if manifest_json_path.exists():
        try:
            with open(manifest_json_path, "r") as f:
                manifest_data = json.load(f)
            for entry in manifest_data.get("entries", []):
                if entry.get("partition") == partition_path:
                    expected_sha256 = entry.get("sha256", "")
                    break
        except Exception as e:
            logger.debug(f"Could not extract expected SHA256 from manifest: {e}")

    # Append manifest entry to .bth/quarantine/archive/manifest.jsonl
    manifest_entry = {
        "ts": datetime.now(UTC).isoformat(),
        "tier": "archive",
        "action": "quarantine_archive",
        "original_path": str(parquet_path),
        "moved_to": str(quarantine_path),
        "partition": partition_path,
        "mtime_s": original_mtime,
        "size_bytes": original_size,
        "expected_sha256": expected_sha256,
        "actual_sha256": actual_sha256,
    }
    quarantine_manifest_path = catalog_dir / "quarantine" / "archive" / "manifest.jsonl"
    quarantine_manifest_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(quarantine_manifest_path, "a") as f:
            f.write(json.dumps(manifest_entry) + "\n")
        logger.info(f"Appended manifest entry to {quarantine_manifest_path}")
    except Exception as e:
        logger.error(f"Failed to append quarantine manifest entry: {e}")
        raise

    # Update archive manifest.json to mark entry as "quarantined": true
    manifest_json_path = archive_root / "manifest.json"
    if manifest_json_path.exists():
        try:
            with open(manifest_json_path, "r") as f:
                manifest = json.load(f)

            # Find the entry matching this partition and mark it quarantined
            updated = False
            for entry in manifest.get("entries", []):
                if entry.get("partition") == partition_path:
                    entry["quarantined"] = True
                    updated = True
                    break

            if updated:
                # Atomic write: write to .tmp, then rename
                tmp_path = manifest_json_path.parent / f".{manifest_json_path.name}.tmp"
                with open(tmp_path, "w") as f:
                    json.dump(manifest, f, indent=2)
                tmp_path.rename(manifest_json_path)
                logger.info(f"Updated archive manifest.json to mark {partition_path} as quarantined")
            else:
                logger.warning(f"Partition {partition_path} not found in archive manifest.json")

        except Exception as e:
            logger.error(f"Failed to update archive manifest.json: {e}")
            # Non-fatal: the file is already quarantined; manifest update is nice-to-have
            raise
