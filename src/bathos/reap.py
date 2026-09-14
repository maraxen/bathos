"""Reaper for orphaned bathos runs: mark abandoned without deleting."""

import json
import socket
import subprocess
import time
from dataclasses import dataclass, asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

import duckdb

from bathos.catalog import read_runs, write_run
from bathos.schema import Run
from bathos.telemetry import event


SACCT_NO_RECORD_REAP_H = 336  # 14 days


class ReapError(Exception):
    """Error during reap operation."""
    pass


@dataclass
class ReapStats:
    """Statistics from a reap operation."""
    total_candidates: int
    reaped_count: int
    skipped_count: int


def _query_slurm_job(job_id: str) -> tuple[Optional[str], Optional[str]]:
    """Query SLURM job state using sacct -X.

    Args:
        job_id: SLURM job ID

    Returns:
        Tuple of (state, error_or_none) where:
        - state is the job state (e.g., "RUNNING", "COMPLETED") or None if not found
        - error_or_none is None if successful, or an error type if the query failed
    """
    try:
        result = subprocess.run(
            ["sacct", "-X", "-j", job_id, "--format=State", "--noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            # Non-zero exit = error
            return None, "sacct_error"
        state = result.stdout.strip()
        if not state:
            # Empty output = no record found
            return None, "sacct_no_record"
        return state, None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None, "sacct_error"


def _is_live_process_on_host(hostname: str, command: str, argv: list[str]) -> bool:
    """Check if a live process matching argv exists on this host.

    Args:
        hostname: Recorded hostname from run
        command: Recorded command
        argv: Recorded argv

    Returns:
        True if a matching live process exists
    """
    this_host = socket.gethostname()
    if hostname != this_host:
        return False

    if not argv:
        return False

    # Match by argv prefix (first element of command)
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.split("\n"):
            if argv[0] in line:
                # Avoid matching our own process
                if "ps aux" not in line:
                    return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return False


def reap_runs(
    catalog_dir: Path,
    older_than_h: float = 24,
    dry_run: bool = True,
    apply: bool = False,
    revert: bool = False,
    revert_ids: Optional[list[str]] = None,
) -> tuple[list[Run], list[tuple[Run, str]]]:
    """Reap orphaned runs by marking them abandoned.

    Args:
        catalog_dir: Catalog directory path
        older_than_h: Minimum age in hours to reap (default 24)
        dry_run: If True, only list candidates without writing
        apply: If True, actually write reaped runs
        revert: If True, restore prior_status instead of marking abandoned
        revert_ids: List of run IDs to revert

    Returns:
        (candidates_list, skipped_list) where skipped_list has (run, skip_reason) tuples
    """
    # Enforce floor
    if apply and older_than_h < 24:
        raise ReapError(f"Floor: --older-than-h must be >= 24 (given {older_than_h})")

    # Read cool tier
    all_runs = read_runs(catalog_dir)

    now = datetime.now(UTC)
    cutoff_time = now - timedelta(hours=older_than_h)

    candidates = []
    skipped = []

    for run in all_runs:
        # Handle revert case
        if revert and revert_ids and run.id in revert_ids:
            # Restore prior_status from metadata.reaped
            try:
                metadata = json.loads(run.metadata or "{}")
                reaped = metadata.get("reaped", {})
                prior_status = reaped.get("prior_status")
                if prior_status:
                    run.status = prior_status
                    # Remove reaped object from metadata
                    metadata.pop("reaped", None)
                    run.metadata = json.dumps(metadata)
                    if apply:
                        write_run(run, catalog_dir)
                    candidates.append(run)
            except (json.JSONDecodeError, ValueError):
                pass
            continue

        # Skip non-running
        if run.status != "running":
            skipped.append((run, "not_running"))
            continue

        # Check age
        if run.timestamp >= cutoff_time:
            skipped.append((run, "too_recent"))
            continue

        # Check SLURM liveness if job ID present
        if run.slurm_job_id:
            state, error = _query_slurm_job(run.slurm_job_id)
            if error:
                # Error or no record
                if error == "sacct_error":
                    skipped.append((run, "sacct_error"))
                elif error == "sacct_no_record":
                    age_hours = (now - run.timestamp).total_seconds() / 3600
                    if age_hours >= SACCT_NO_RECORD_REAP_H:
                        # Old enough to reap
                        candidates.append(run)
                    else:
                        skipped.append((run, "sacct_no_record"))
            elif state in ("RUNNING", "PENDING", "CONFIGURING", "RESIZING"):
                # Non-terminal
                skipped.append((run, "slurm_nonterminal"))
            else:
                # Terminal state: COMPLETED, FAILED, CANCELLED*, TIMEOUT, etc.
                candidates.append(run)
        else:
            # No SLURM job, check for live process
            if _is_live_process_on_host(run.hostname, run.command, run.argv):
                skipped.append((run, "same_host_live_process"))
            else:
                candidates.append(run)

    # Apply reaping if requested
    if apply:
        if revert:
            # Revert path: reconcile warm tier after rewrites
            reconcile_warm_tier(catalog_dir)
        else:
            # Reap path: write reaped runs and reconcile warm tier
            for run in candidates:
                # Mark as abandoned
                run.status = "abandoned"
                # Add reaped metadata
                try:
                    metadata = json.loads(run.metadata or "{}")
                except (json.JSONDecodeError, ValueError):
                    metadata = {}

                metadata["reaped"] = {
                    "reaped_at": datetime.now(UTC).isoformat(),
                    "reason": "orphan_window_exceeded",
                    "window_h": older_than_h,
                    "prior_status": "running",
                }
                run.metadata = json.dumps(metadata)

                # Write back (atomic)
                write_run(run, catalog_dir)
                event("catalog.reap_run", run_id=run.id, reason="orphan_window")

            # Reconcile warm tier after writing all reaped runs
            reconcile_warm_tier(catalog_dir)

    return candidates, skipped


def reconcile_warm_tier(catalog_dir: Path) -> None:
    """Reconcile warm tier with cool tier after reaping.

    Backs up and force-rebuilds the warm tier from cool fragments.

    Args:
        catalog_dir: Catalog directory path
    """
    from bathos.compact import _backup_warm_db, compact

    db_path = catalog_dir / "bathos.db"
    if not db_path.exists():
        return

    # Backup before rebuild
    _backup_warm_db(db_path)

    # Force rebuild (will remove old DB and rebuild from scratch)
    compact(catalog_dir, force_rebuild=True)
