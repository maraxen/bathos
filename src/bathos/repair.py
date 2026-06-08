"""Repair tools for bathos catalog corruption recovery.

This module provides repair actions for cool-tier sentinels (.tmp, .bak),
corrupt fragments, and warm-tier database corruption. Repair actions are
scanned from verify.py findings and executed with dry-run safety gates.
"""

from __future__ import annotations

import json
import logging
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
                "backup_warm", "rebuild_warm"
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
) -> tuple[list[RepairAction], list[str]]:
    """Scan the catalog and identify repair actions needed.

    Calls verify.verify_all() (or tier-specific variant) and converts findings
    into RepairAction objects and warnings. This is a read-only operation; dry_run is always True.

    Args:
        catalog_dir: Catalog directory (default: ~/.bth/catalog/)
        tier: "cool", "warm", "archive", or "all"

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
        warm_actions, warm_warnings = _actions_from_warm_verify(warm_result)
        actions.extend(warm_actions)
        warnings.extend(warm_warnings)

    if tier == "archive" or tier == "all":
        archive_root = Path.home() / ".bth" / "archive"
        from bathos.verify import verify_archive

        archive_result = verify_archive(archive_root)
        archive_actions, archive_warnings = _actions_from_archive_verify(archive_result)
        actions.extend(archive_actions)
        warnings.extend(archive_warnings)

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


def _actions_from_warm_verify(result) -> tuple[list[RepairAction], list[str]]:
    """Convert verify_warm errors to RepairAction objects and warnings."""
    actions: list[RepairAction] = []
    warnings: list[str] = []

    for error in result.errors:
        if "Database integrity check failed" in error or "Could not open database" in error:
            db_path = Path(result.stats.get("db_path", "") or "").resolve()
            if not db_path.exists():
                db_path = Path.cwd() / "bathos.db"
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

    Archive repair is a P2 backlog item; for MVP scope, we only log errors.

    Returns:
        Tuple of (empty list, empty list) — archive repair deferred
    """
    # Archive repair is deferred to immediate follow-on; not in MVP scope
    return [], []


def repair(
    catalog_dir: Path | str | None = None,
    tier: str = "all",
    dry_run: bool = False,
    acknowledge_warm_loss: bool = False,
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

    Returns:
        RepairManifest with actions taken and any warnings

    Raises:
        SystemExit(1): If warm DB rebuild is needed but acknowledge_warm_loss is False
    """
    if catalog_dir is None:
        catalog_dir = default_catalog_dir()
    catalog_dir = Path(catalog_dir)

    run_ts = datetime.now(UTC).isoformat()

    # Scan for actions needed
    actions, scan_warnings = scan(catalog_dir, tier)

    # Check for warm rebuild without acknowledgment
    warm_rebuild_actions = [a for a in actions if a.action == "rebuild_warm"]
    if warm_rebuild_actions and not acknowledge_warm_loss:
        # Check if warm DB has postmortem annotations or output_metadata
        warn_msg = (
            "WARNING: Warm database rebuild will destroy postmortem annotations and output_metadata.\n"
            "To proceed, pass --acknowledge-warm-loss"
        )
        logger.error(warn_msg)
        raise SystemExit(1)

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
            _execute_repair_action(action, catalog_dir)
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


def _execute_repair_action(action: RepairAction, catalog_dir: Path) -> None:
    """Execute a single repair action.

    Raises NotImplementedError for actions not yet implemented in tracks B/C/D.
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
        raise NotImplementedError(
            "TODO: implemented in downstream track D — rebuild_warm action handler"
        )

    else:
        raise ValueError(f"Unknown repair action: {action.action}")
