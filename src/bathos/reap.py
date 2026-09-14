"""Reaper for orphaned bathos runs: mark abandoned without deleting."""

import json
import os
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


def _write_reap_ledger_entry(
    catalog_dir: Path, run_id: str, project_slug: str, record: dict
) -> None:
    """Write a reap ledger entry atomically.

    Args:
        catalog_dir: Catalog directory path
        run_id: Run ID (e.g., "run_test", already includes "run_" prefix)
        project_slug: Project slug
        record: Reap record dict with keys: reaped_at, reason, window_h, prior_status
    """
    ledger_dir = catalog_dir / "reaped" / project_slug
    ledger_dir.mkdir(parents=True, exist_ok=True)

    ledger_path = ledger_dir / f"{run_id}.json"
    tmp_path = ledger_path.with_suffix(".json.tmp")

    # Write to temp file first
    with open(tmp_path, "w") as f:
        json.dump(record, f)

    # Atomic rename
    os.replace(tmp_path, ledger_path)


def read_reap_ledger(catalog_dir: Path) -> dict[str, dict]:
    """Read all reap ledger entries.

    Args:
        catalog_dir: Catalog directory path

    Returns:
        Dictionary mapping run_id to reap record
    """
    ledger = {}
    reaped_dir = catalog_dir / "reaped"

    if not reaped_dir.exists():
        return ledger

    for project_dir in reaped_dir.iterdir():
        if not project_dir.is_dir():
            continue
        # Skip the reverted subdirectory
        if project_dir.name == "reverted":
            continue
        for ledger_file in project_dir.glob("*.json"):
            # Skip reverted files
            if ledger_file.parent.name == "reverted":
                continue
            try:
                with open(ledger_file) as f:
                    record = json.load(f)
                run_id = ledger_file.stem  # e.g., "run_test"
                ledger[run_id] = record
            except (json.JSONDecodeError, IOError):
                pass

    return ledger


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

    # Read cool tier and ledger
    all_runs = read_runs(catalog_dir)
    ledger = read_reap_ledger(catalog_dir) if revert else {}

    now = datetime.now(UTC)
    cutoff_time = now - timedelta(hours=older_than_h)

    candidates = []
    skipped = []

    for run in all_runs:
        # Handle revert case
        if revert and revert_ids and run.id in revert_ids:
            if run.id not in ledger:
                # No ledger entry: cannot revert
                skipped.append((run, "no_ledger_entry"))
                continue

            ledger_record = ledger[run.id]
            prior_status = ledger_record.get("prior_status")

            if prior_status and run.status == "abandoned":
                run.status = prior_status

                if apply:
                    write_run(run, catalog_dir)
                    # Move ledger to reverted/
                    ledger_dir = catalog_dir / "reaped" / run.project_slug
                    ledger_path = ledger_dir / f"{run.id}.json"
                    reverted_dir = ledger_dir / "reverted"
                    reverted_dir.mkdir(parents=True, exist_ok=True)
                    ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
                    reverted_path = reverted_dir / f"{run.id}.{ts}.json"
                    os.replace(ledger_path, reverted_path)

                candidates.append(run)
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
            # Reap path: write reaped runs with ledger entries
            # Track which runs were reaped via sacct_no_record
            sacct_no_record_runs = set()

            # First pass: identify sacct_no_record runs
            for run in all_runs:
                if run.id not in [c.id for c in candidates]:
                    continue
                if run.slurm_job_id:
                    state, error = _query_slurm_job(run.slurm_job_id)
                    if error == "sacct_no_record":
                        age_hours = (now - run.timestamp).total_seconds() / 3600
                        if age_hours >= SACCT_NO_RECORD_REAP_H:
                            sacct_no_record_runs.add(run.id)

            # Write reaped runs and ledger entries
            for run in candidates:
                # Determine reason
                reason = "orphan_window_exceeded"
                if run.id in sacct_no_record_runs:
                    reason = "sacct_no_record"

                # Mark as abandoned
                run.status = "abandoned"

                reaped_at = datetime.now(UTC).isoformat()

                # Write back (atomic) — BEFORE ledger entry
                write_run(run, catalog_dir)

                # Write ledger entry after successful write
                ledger_record = {
                    "run_id": run.id,
                    "project_slug": run.project_slug,
                    "reaped_at": reaped_at,
                    "reason": reason,
                    "window_h": older_than_h,
                    "prior_status": "running",
                }
                _write_reap_ledger_entry(catalog_dir, run.id, run.project_slug, ledger_record)

                event("catalog.reap_run", run_id=run.id, reason=reason)

            # Reconcile warm tier after writing all reaped runs
            reconcile_warm_tier(catalog_dir)

    return candidates, skipped


def reconcile_warm_tier(catalog_dir: Path) -> None:
    """Reconcile warm tier with cool tier after reaping.

    Backs up and force-rebuilds the warm tier from cool fragments,
    then merges reaped ledger entries into metadata.

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

    # After rebuild, merge reaped ledger entries into warm metadata
    ledger = read_reap_ledger(catalog_dir)

    if not ledger:
        return

    # Update warm tier metadata with reaped info
    conn = duckdb.connect(str(db_path))
    try:
        for run_id, ledger_record in ledger.items():
            # Get current metadata or empty object
            result = conn.execute(
                "SELECT metadata FROM runs WHERE id = ?", [run_id]
            ).fetchone()
            if result:
                current_metadata = result[0]
                try:
                    metadata = json.loads(current_metadata or "{}")
                except (json.JSONDecodeError, ValueError):
                    metadata = {}

                # Merge reaped info
                metadata["reaped"] = ledger_record

                # Update the record
                conn.execute(
                    "UPDATE runs SET metadata = ? WHERE id = ?",
                    [json.dumps(metadata), run_id],
                )
        conn.commit()
    finally:
        conn.close()
