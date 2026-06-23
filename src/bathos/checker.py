from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from bathos.git import capture_git_state
from bathos.query import list_runs
from bathos.schema import Run


@dataclass
class CheckResult:
    """Result of checking a single run's git-drift validity."""

    run_id: str
    status: Literal["OK", "STALE", "DIRTY_RUN", "UNKNOWN_CODE"]
    run_git_hash: str
    current_hash: str


@dataclass
class OutputCheckResult:
    """Result of checking a single output file."""

    path: str
    status: str  # "present", "missing", "unreadable"
    size_bytes: int = 0


@dataclass
class OutputShaDriftResult:
    """Result of comparing catalogued output SHA256 to the file on disk."""

    path: str
    recorded_sha256: str
    current_sha256: str | None
    status: Literal["OK", "DRIFT", "MISSING", "UNRECORDED", "UNREADABLE"]


def check_runs(
    catalog_dir: Path,
    project_root: Path,
    status_filter: str | None = None,
) -> list[CheckResult]:
    """Check all runs in catalog for git-drift validity.

    For each run:
    - STALE: run's git_hash != current HEAD and run's git_dirty was False
    - DIRTY_RUN: run's git_dirty was True
    - UNKNOWN_CODE: run's git_hash == "unknown"
    - OK: otherwise (hash matches current or dirty was True)

    Args:
        catalog_dir: Path to catalog directory
        project_root: Path to project root (used to get current git state)
        status_filter: Optional filter; return only results with this status

    Returns:
        List of CheckResult objects
    """
    # Get current git state
    current_state = capture_git_state(project_root)
    current_hash = current_state.hash

    # Get all runs from catalog
    all_runs = list_runs(catalog_dir)

    results = []
    for run in all_runs:
        if run.git_hash == "unknown":
            status = "UNKNOWN_CODE"
        elif run.git_dirty:
            status = "DIRTY_RUN"
        elif run.git_hash != current_hash:
            status = "STALE"
        else:
            status = "OK"

        result = CheckResult(
            run_id=run.id,
            status=status,
            run_git_hash=run.git_hash,
            current_hash=current_hash,
        )
        results.append(result)

    # Apply filter if provided
    if status_filter:
        results = [r for r in results if r.status == status_filter]

    return results


def check_output_files(run: Run) -> list[OutputCheckResult]:
    """Verify output files exist and are readable.

    Args:
        run: Run object with output_paths

    Returns:
        List of OutputCheckResult for each file
    """
    from bathos.compact import _collect_output_metadata

    results = []
    for path in run.output_paths:
        meta = _collect_output_metadata(path)
        results.append(
            OutputCheckResult(
                path=path,
                status=meta["status"],
                size_bytes=meta.get("size_bytes", 0),
            )
        )

    return results


def check_output_sha_drift(run: Run) -> list[OutputShaDriftResult]:
    """Compare warm-tier output_metadata SHA256 hashes to on-disk files.

    Args:
        run: Run object with output_metadata JSON (warm tier)

    Returns:
        List of OutputShaDriftResult for each catalogued output with a recorded hash
    """
    from bathos.compact import _collect_output_metadata

    if not run.output_metadata or run.output_metadata == "[]":
        return []

    try:
        entries = json.loads(run.output_metadata)
    except (json.JSONDecodeError, TypeError):
        return []

    if not isinstance(entries, list):
        return []

    results: list[OutputShaDriftResult] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path", "")
        recorded = entry.get("sha256")
        if not path:
            continue
        if not recorded:
            results.append(
                OutputShaDriftResult(
                    path=path,
                    recorded_sha256="",
                    current_sha256=None,
                    status="UNRECORDED",
                )
            )
            continue

        fresh = _collect_output_metadata(path)
        if fresh["status"] == "missing":
            results.append(
                OutputShaDriftResult(
                    path=path,
                    recorded_sha256=recorded,
                    current_sha256=None,
                    status="MISSING",
                )
            )
        elif fresh["status"] == "unreadable":
            results.append(
                OutputShaDriftResult(
                    path=path,
                    recorded_sha256=recorded,
                    current_sha256=None,
                    status="UNREADABLE",
                )
            )
        else:
            current = fresh.get("sha256")
            if current and current != recorded:
                results.append(
                    OutputShaDriftResult(
                        path=path,
                        recorded_sha256=recorded,
                        current_sha256=current,
                        status="DRIFT",
                    )
                )
            elif current and current == recorded:
                results.append(
                    OutputShaDriftResult(
                        path=path,
                        recorded_sha256=recorded,
                        current_sha256=current,
                        status="OK",
                    )
                )
            else:
                results.append(
                    OutputShaDriftResult(
                        path=path,
                        recorded_sha256=recorded,
                        current_sha256=None,
                        status="UNREADABLE",
                    )
                )

    return results


def output_metadata_has_sha_drift(output_metadata_json: str | None) -> bool:
    """Return True if any catalogued output SHA no longer matches disk."""
    if not output_metadata_json or output_metadata_json == "[]":
        return False
    return any(
        r.status in ("DRIFT", "MISSING", "UNREADABLE")
        for r in check_output_sha_drift(
            Run(
                project_slug="",
                command="",
                argv=[],
                git_hash="",
                git_branch="",
                git_dirty=False,
                output_metadata=output_metadata_json,
            )
        )
    )
