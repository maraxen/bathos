"""Repair tools for bathos catalog corruption recovery.

This module provides repair actions for cool-tier sentinels (.tmp, .bak),
corrupt fragments, and warm-tier database corruption. Repair actions are
scanned from verify.py findings and executed with dry-run safety gates.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from bathos.config import default_catalog_dir
from bathos.index import catalog_readable, connect_read
from bathos.verify import verify_cool, verify_warm

logger = logging.getLogger(__name__)


@dataclass
class RepairAction:
    """A single repair action to be taken.

    Attributes:
        action: Action type: "delete_tmp", "quarantine_bak", "quarantine_corrupt",
                "backup_warm", "rebuild_warm", "reexport_from_warm",
                "quarantine_archive", "reconcile_campaign_runs"
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

        from bathos.runlog.mode import is_log_mode

        if is_log_mode(catalog_dir):
            from bathos.index import index_db_path
            from bathos.verify import _orphaned_campaign_run_findings

            orphan_findings = _orphaned_campaign_run_findings(catalog_dir)
            if orphan_findings:
                backfill_cands = _collect_campaign_backfill_candidates(catalog_dir)
                detail = (
                    f"Reconcile {len(orphan_findings)} orphaned/prefix-mismatched "
                    "campaign_runs row(s) in index.db"
                )
                if backfill_cands:
                    detail += f" ({len(backfill_cands)} backfillable from event logs)"
                actions.append(
                    RepairAction(
                        action="reconcile_campaign_runs",
                        path=str(index_db_path(catalog_dir)),
                        detail=detail,
                    )
                )
        elif warm_result.stats.get("orphaned_campaign_runs", 0) > 0:
            n_orphans = warm_result.stats["orphaned_campaign_runs"]
            actions.append(
                RepairAction(
                    action="reconcile_campaign_runs",
                    path=str(catalog_dir / "bathos.db"),
                    detail=f"Reconcile {n_orphans} orphaned campaign_runs row(s) in bathos.db",
                )
            )

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
                age_s = datetime.now(UTC).timestamp() - mtime_unix
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
                age_s = datetime.now(UTC).timestamp() - mtime_unix
                if age_s < 60:
                    warning = (
                        f"Skipped .bak file (in-flight migration): {path_part} (age {age_s:.0f}s)"
                    )
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

    from bathos.runlog.mode import is_log_mode

    if is_log_mode(catalog_dir):
        # Debt #2819 (7): bathos.db is frozen after cut-over and compact refuses
        # force_rebuild there, so a rebuild_warm action could only fail at apply.
        if result.errors:
            warnings.append(
                f"Warm database {db_path} reported errors but is frozen in log mode; not "
                "rebuilding it. index.db is the live read tier (delete it and run "
                "`bth compact` to rebuild it from the logs)."
            )
        return actions, warnings

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
    if (
        not actions
        and "db_exists" in result.stats
        and result.stats["db_exists"]
        and db_path.exists()
    ):
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
    actions: list[RepairAction] = []
    warnings: list[str] = []

    db_path = catalog_dir / "bathos.db"
    if not catalog_readable(catalog_dir):
        # No warm DB and no folded index; nothing to re-export
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
        con = connect_read(catalog_dir, read_only=True)
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
        db_queryable = False
        postmortem_count = 0
        output_metadata_count = 0
        if catalog_readable(catalog_dir):
            try:
                con = connect_read(catalog_dir, read_only=True)
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
                    db_queryable = True
                finally:
                    con.close()
            except Exception as e:
                logger.debug(f"Could not query warm DB for at-risk data: {e}")
                # db_queryable remains False — treat unreadable DB as data-at-risk

        # Gate fires when: DB unreadable (cannot verify safety) OR known warm-only data at risk
        if not db_queryable or postmortem_count > 0 or output_metadata_count > 0:
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
            # DB is queryable and no warm-only data at risk; proceed safely
            logger.info(
                "Warm database rebuild will not lose any warm-only data (postmortem_count=0, output_metadata_count=0); proceeding"
            )

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
        _handle_backup_warm(action, catalog_dir, manifest)

    elif action.action == "rebuild_warm":
        _handle_rebuild_warm(action, catalog_dir)

    elif action.action == "quarantine_archive":
        _handle_quarantine_archive(action, catalog_dir, archive_root)

    elif action.action == "reexport_from_warm":
        _handle_reexport_from_warm(action, catalog_dir, manifest)

    elif action.action == "reconcile_campaign_runs":
        _handle_reconcile_campaign_runs(action, catalog_dir)

    else:
        raise ValueError(f"Unknown repair action: {action.action}")


def _handle_backup_warm(
    action: RepairAction,
    catalog_dir: Path,
    manifest: RepairManifest | None = None,
) -> None:
    """Create a timestamped backup of the warm database and rotate old backups (max 3)."""
    db_path = Path(action.path) if action.path else (catalog_dir / "bathos.db")
    if not db_path.exists():
        msg = f"Warm database not found, skipped backup: {db_path}"
        logger.warning(msg)
        action.detail = msg
        if manifest is not None:
            manifest.warnings.append(msg)
        return

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.parent / f"{db_path.name}.bak-{timestamp}"
    suffix = 1
    while backup_path.exists():
        backup_path = db_path.parent / f"{db_path.name}.bak-{timestamp}-{suffix}"
        suffix += 1

    tmp_path = db_path.parent / f".{backup_path.name}.tmp"
    try:
        shutil.copy2(db_path, tmp_path)
        tmp_path.replace(backup_path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise

    bak_files = sorted(
        db_path.parent.glob(f"{db_path.name}.bak-*"),
        key=lambda p: (p.stat().st_mtime, p.name),
    )
    max_warm_backups = 3
    if len(bak_files) > max_warm_backups:
        for old_backup in bak_files[:-max_warm_backups]:
            old_backup.unlink()
            logger.info(f"Rotated out old backup: {old_backup}")

    action.detail = f"Backed up warm database to {backup_path}"
    logger.info(action.detail)


_CAMPAIGN_BACKFILL_KINDS = frozenset(
    {
        "campaign.created",
        "campaign.imported",
        "campaign.concluded",
        "campaign.claim_bound",
        "campaign.threshold_set",
        "campaign.claim_bypassed",
        "campaign.run_added",
        "campaign_run.imported",
        "run.started",
        "run.imported",
    }
)
_CAMPAIGN_CREATION_KINDS = frozenset({"campaign.created", "campaign.imported"})
_CAMPAIGN_CREATED_FIELDS = (
    "id",
    "project_slug",
    "name",
    "mode",
    "status",
    "started_at",
    "question",
    "hypothesis",
    "parent_campaign_id",
    "stopping_threshold",
    "claim_path",
    "claim_sha256",
    "claim_mode",
    "negative_check",
)


def _match_campaign_event(ev: dict, missing_cids: set[str]) -> str | None:
    """Return the missing campaign id (or prefix) an event relates to, else None."""
    kind = ev.get("kind")
    if kind not in _CAMPAIGN_BACKFILL_KINDS:
        return None
    entity = ev.get("entity") or []
    data = ev.get("data") or {}
    if not isinstance(data, dict):
        data = {}

    if kind in ("run.started", "run.imported"):
        cid = data.get("campaign_id")
        return cid if isinstance(cid, str) and cid in missing_cids else None

    if not entity or not isinstance(entity[0], str):
        return None
    ent_cid = entity[0]
    if ent_cid in missing_cids:
        return ent_cid
    if kind in _CAMPAIGN_CREATION_KINDS:
        for cid in missing_cids:
            if len(cid) >= 4 and ent_cid.startswith(cid):
                return cid
    return None


def _resolve_backfill_cwd(
    catalog_dir: Path, events: list[dict], project_slug: str
) -> Path:
    """Pick a project root for `emit_event` from the matched events or registered roots."""
    for ev in events:
        main_root = ev.get("main_root")
        if main_root:
            root_path = Path(str(main_root))
            if root_path.is_dir() and (
                (root_path / ".bth.toml").is_file() or (root_path / ".git").exists()
            ):
                return root_path

    if project_slug and project_slug != "unknown":
        from bathos.config import load_project_config
        from bathos.runlog.project_id import list_registered_roots

        for root in list_registered_roots():
            cfg_path = root / ".bth.toml"
            if not cfg_path.is_file():
                continue
            with contextlib.suppress(Exception):
                if load_project_config(cfg_path).slug == project_slug:
                    return root

    return catalog_dir


def _collect_campaign_backfill_candidates(catalog_dir: Path) -> list[dict]:
    """Identify phantom/missing campaigns that have backing events in `events` or raw logs (#2213)."""
    from bathos.index import index_db_path
    from bathos.runlog.ingest import discover_roots

    if not index_db_path(catalog_dir).exists():
        return []

    con = connect_read(catalog_dir)
    try:
        valid_cids = {
            r[0]
            for r in con.execute("SELECT id FROM campaigns WHERE mode IS NOT NULL").fetchall()
            if r[0]
        }
        phantom_cids = {
            r[0]
            for r in con.execute("SELECT id FROM campaigns WHERE mode IS NULL").fetchall()
            if r[0]
        }
        orphan_cr_cids = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT cr.campaign_id FROM campaign_runs cr "
                "LEFT JOIN campaigns c ON cr.campaign_id = c.id "
                "WHERE c.id IS NULL OR c.mode IS NULL"
            ).fetchall()
            if r[0]
        }
        bad_run_cids = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT r.campaign_id FROM runs r "
                "LEFT JOIN campaigns c ON r.campaign_id = c.id "
                "WHERE COALESCE(r.campaign_id, '') <> '' AND (c.id IS NULL OR c.mode IS NULL)"
            ).fetchall()
            if r[0]
        }

        missing_cids = {
            cid
            for cid in (phantom_cids | orphan_cr_cids | bad_run_cids)
            if not (
                len(cid) >= 4
                and len([v for v in valid_cids if v.startswith(cid)]) == 1
            )
        }
        if not missing_cids:
            return []

        db_eids: set[str] = set()
        events_by_cid: dict[str, list[dict]] = {cid: [] for cid in missing_cids}
        placeholders = ", ".join("?" for _ in _CAMPAIGN_BACKFILL_KINDS)
        rows = con.execute(
            f"SELECT eid, kind, entity, ts, project, main_root, data FROM events "  # noqa: S608
            f"WHERE kind IN ({placeholders})",
            list(_CAMPAIGN_BACKFILL_KINDS),
        ).fetchall()
        for eid, kind, raw_entity, ts, project, main_root, raw_data in rows:
            if eid:
                db_eids.add(str(eid))
            entity = json.loads(raw_entity) if isinstance(raw_entity, str) else (raw_entity or [])
            data = json.loads(raw_data) if isinstance(raw_data, str) else (raw_data or {})
            ev = {
                "eid": eid,
                "kind": kind,
                "entity": entity,
                "ts": ts or "",
                "project": project,
                "main_root": main_root,
                "data": data,
                "source": "db",
            }
            matched = _match_campaign_event(ev, missing_cids)
            if matched is not None:
                events_by_cid[matched].append(ev)
    finally:
        con.close()

    # Also scan raw JSONL event logs on disk (covers un-ingested segments or
    # events in segments whose watermark already moved past them).
    seen_eids_per_cid: dict[str, set[str]] = {
        cid: {ev["eid"] for ev in evs if ev.get("eid")}
        for cid, evs in events_by_cid.items()
    }
    for _root_kind, _root_id, log_dir in discover_roots(catalog_dir):
        if not log_dir.is_dir():
            continue
        for seg in sorted(log_dir.glob("*.jsonl")):
            try:
                text = seg.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for raw_line in text.splitlines():
                if not raw_line.strip():
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                matched = _match_campaign_event(obj, missing_cids)
                if matched is None:
                    continue
                eid = obj.get("eid")
                if eid and eid in seen_eids_per_cid[matched]:
                    continue
                if eid:
                    seen_eids_per_cid[matched].add(eid)
                events_by_cid[matched].append(
                    {
                        "eid": eid,
                        "kind": obj.get("kind"),
                        "entity": obj.get("entity") or [],
                        "ts": obj.get("ts") or "",
                        "project": obj.get("project"),
                        "main_root": obj.get("main_root"),
                        "data": obj.get("data") or {},
                        "source": "disk",
                    }
                )

    candidates: list[dict] = []
    for cid in sorted(missing_cids):
        evs = sorted(events_by_cid[cid], key=lambda e: (e.get("ts") or "", str(e.get("eid") or "")))
        if not evs:
            continue

        creation_evs = [e for e in evs if e.get("kind") in _CAMPAIGN_CREATION_KINDS]
        target_cid = cid
        has_uningested = any(
            e.get("source") == "disk" and e.get("eid") not in db_eids for e in evs
        )

        if creation_evs:
            # Prefer an unambiguous full campaign ID if creation event came from a prefix match.
            full_ids = {
                e["entity"][0]
                for e in creation_evs
                if e.get("entity") and isinstance(e["entity"][0], str)
            }
            if len(full_ids) == 1:
                only_id = next(iter(full_ids))
                if only_id not in valid_cids:
                    target_cid = only_id

            payload: dict = {}
            for ev in creation_evs:
                data = ev.get("data") or {}
                for field_name in _CAMPAIGN_CREATED_FIELDS:
                    val = data.get(field_name)
                    if val is not None and val != "":
                        payload[field_name] = val
            payload["id"] = target_cid
        else:
            payload = {"id": target_cid}

        if not payload.get("mode"):
            is_seq = any(
                e.get("kind") == "campaign.threshold_set"
                or (
                    e.get("kind") == "campaign_run.imported"
                    and (e.get("data") or {}).get("evalue") is not None
                )
                or (
                    e.get("kind") in ("run.started", "run.imported")
                    and isinstance((e.get("data") or {}).get("sidecar"), dict)
                    and (e.get("data") or {})["sidecar"].get("popper_stopping_threshold")
                    is not None
                )
                for e in evs
            )
            is_conf = any(
                e.get("kind") in ("campaign.claim_bound", "campaign.claim_bypassed")
                for e in evs
            )
            payload["mode"] = "sequential" if is_seq else ("confirmation" if is_conf else "exploration")

        if not payload.get("project_slug"):
            slug = next(
                (
                    (e.get("data") or {}).get("project_slug") or e.get("project")
                    for e in evs
                    if (e.get("data") or {}).get("project_slug") or e.get("project")
                ),
                None,
            )
            payload["project_slug"] = slug or "unknown"

        if not payload.get("name"):
            name = next(
                (
                    (e.get("data") or {}).get("name")
                    for e in evs
                    if (e.get("data") or {}).get("name")
                ),
                None,
            )
            payload["name"] = name or f"backfilled-{target_cid[:8]}"

        if not payload.get("status"):
            payload["status"] = "open"

        if not payload.get("started_at"):
            earliest_ts = next((e.get("ts") for e in evs if e.get("ts")), None)
            payload["started_at"] = earliest_ts or datetime.now(UTC).isoformat()

        candidates.append(
            {
                "phantom_cid": cid,
                "target_cid": target_cid,
                "payload": payload,
                "cwd": _resolve_backfill_cwd(catalog_dir, evs, str(payload["project_slug"])),
                "has_uningested": has_uningested,
            }
        )

    return candidates


def _backfill_phantom_campaigns_from_logs(catalog_dir: Path) -> int:
    """Backfill phantom `campaigns` rows (`mode IS NULL`) from event logs when present (#2213)."""
    from bathos.runlog.emit import emit_event, unit_of_work
    from bathos.runlog.ingest import run_ingest

    initial = _collect_campaign_backfill_candidates(catalog_dir)
    if not initial:
        return 0

    backfilled = 0
    if any(c["has_uningested"] for c in initial):
        run_ingest(catalog_dir)
        remaining = _collect_campaign_backfill_candidates(catalog_dir)
        resolved_by_ingest = len({c["phantom_cid"] for c in initial}) - len(
            {c["phantom_cid"] for c in remaining}
        )
        if resolved_by_ingest > 0:
            backfilled += resolved_by_ingest
    else:
        remaining = initial

    if remaining:
        with unit_of_work(catalog_dir):
            for cand in remaining:
                emit_event(
                    kind="campaign.created",
                    entity=[cand["target_cid"]],
                    data=cand["payload"],
                    cwd=cand["cwd"],
                    hard_fail=True,
                )
                backfilled += 1
        run_ingest(catalog_dir)

    return backfilled


def _handle_reconcile_campaign_runs(action: RepairAction, catalog_dir: Path) -> None:
    """Reconcile orphaned and prefix-mismatched `campaign_runs` rows."""
    from bathos.runlog.mode import is_log_mode

    if is_log_mode(catalog_dir):
        from bathos.runlog.ingest import reconcile_index_campaign_runs

        backfilled = _backfill_phantom_campaigns_from_logs(catalog_dir)
        counts = reconcile_index_campaign_runs(catalog_dir)
        action.detail = (
            f"Reconciled index.db campaign_runs: {backfilled} backfilled from event logs, "
            f"{counts['reassigned']} reassigned, "
            f"{counts['deleted_orphans']} orphan(s) removed, "
            f"{counts['deleted_phantom_campaigns']} phantom campaign(s) removed"
        )
        logger.info(action.detail)
        return

    from bathos.campaigns import connect_catalog_db

    db_path = Path(action.path) if action.path else (catalog_dir / "bathos.db")
    if not db_path.exists():
        action.detail = f"Warm database not found: {db_path}"
        return

    con = connect_catalog_db(db_path.parent, read_only=False)
    if con is None:
        action.detail = f"Warm database not found: {db_path}"
        return
    try:
        valid_cids = {
            r[0] for r in con.execute("SELECT id FROM campaigns").fetchall() if r[0]
        }
        orphans = con.execute(
            "SELECT cr.campaign_id, cr.run_id, cr.evalue, cr.seq_position "
            "FROM campaign_runs cr "
            "LEFT JOIN campaigns c ON cr.campaign_id = c.id "
            "WHERE c.id IS NULL"
        ).fetchall()
        reassigned = 0
        deleted = 0
        con.execute("BEGIN")
        for bad_cid, run_id, evalue, seq_pos in orphans:
            matches = (
                [c for c in valid_cids if c.startswith(bad_cid)]
                if bad_cid and len(bad_cid) >= 4
                else []
            )
            con.execute(
                "DELETE FROM campaign_runs WHERE campaign_id = ? AND run_id = ?",
                [bad_cid, run_id],
            )
            if len(matches) == 1:
                con.execute(
                    "INSERT INTO campaign_runs (campaign_id, run_id, evalue, seq_position) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    [matches[0], run_id, evalue, seq_pos],
                )
                reassigned += 1
            else:
                deleted += 1
        con.execute("COMMIT")
    except Exception:
        with contextlib.suppress(Exception):
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()

    action.detail = (
        f"Reconciled bathos.db campaign_runs: {reassigned} reassigned, {deleted} orphan(s) removed"
    )
    logger.info(action.detail)


def _handle_rebuild_warm(action: RepairAction, catalog_dir: Path) -> None:
    """Rebuild warm database (bathos.db) from cool fragments.

    Calls compact(catalog_dir, force_rebuild=True), which creates a backup
    of bathos.db before deletion and rebuilds the database from cool fragments.
    """
    from bathos.compact import compact
    from bathos.runlog.mode import is_log_mode

    if is_log_mode(catalog_dir):
        raise RuntimeError(
            "rebuild_warm is not applicable in log mode: bathos.db is frozen and index.db "
            "is rebuilt from the logs (delete it and run `bth compact`)."
        )

    db_path = Path(action.path)
    logger.info(f"Rebuilding warm database: {db_path}")

    try:
        result = compact(catalog_dir, force_rebuild=True)
        logger.info(f"Warm database rebuilt: {result.ingested} ingested, {result.skipped} skipped")
        action.detail = (
            f"Warm database rebuilt from cool fragments: {result.ingested} rows ingested"
        )
    except Exception as e:
        logger.error(f"Failed to rebuild warm database: {e}")
        raise


def _handle_reexport_from_warm(
    action: RepairAction, catalog_dir: Path, manifest: RepairManifest | None = None
) -> None:
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
    from bathos.catalog import read_runs, write_run
    from bathos.schema import Run

    db_path = Path(action.path)
    logger.info(f"Re-exporting warm runs to cool fragments from {db_path}")

    if not catalog_readable(catalog_dir):
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
        con = connect_read(catalog_dir, read_only=True)
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
        con = connect_read(catalog_dir, read_only=True)
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
        raw_id = row_dict.get("id")
        run_uuid = raw_id if isinstance(raw_id, str) else str(raw_id or "")

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
                postmortem_hypothesis_status=row_dict.get(
                    "postmortem_hypothesis_status", "unassigned"
                ),
                postmortem_has_anomalies=bool(row_dict.get("postmortem_has_anomalies", False)),
                postmortem_summary=row_dict.get("postmortem_summary", ""),
                postmortem_asset_links=row_dict.get("postmortem_asset_links", "{}"),
                manifest_sha256=row_dict.get("manifest_sha256", ""),
                manifest_path=row_dict.get("manifest_path", ""),
                outcome_error_reason=row_dict.get("outcome_error_reason", ""),
                adversarial_check_status=row_dict.get("adversarial_check_status", ""),
                adversarial_check_result=row_dict.get("adversarial_check_result"),
                dependency_provenance=row_dict.get("dependency_provenance"),
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
            manifest.warnings.append(
                f"Skipped re-export of run {uuid}: NULL metadata (warm-only data)"
            )

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
            with open(manifest_json_path) as f:
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
            with open(manifest_json_path) as f:
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
                logger.info(
                    f"Updated archive manifest.json to mark {partition_path} as quarantined"
                )
            else:
                logger.warning(f"Partition {partition_path} not found in archive manifest.json")

        except Exception as e:
            logger.error(f"Failed to update archive manifest.json: {e}")
            # Non-fatal: the file is already quarantined; manifest update is nice-to-have
            raise
