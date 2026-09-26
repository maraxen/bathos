from __future__ import annotations

import hashlib
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
    *,
    project: str | None = None,
    limit: int = 50,
    runs: list[Run] | None = None,
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
        project: Optional project_slug filter, passed through to list_runs().
        limit: Passed through to list_runs() (default 50, matching list_runs'
            own default -- unchanged for existing callers). list_runs() has no
            ORDER BY, so on a catalog with more than `limit` runs this silently
            drops an arbitrary subset, not "the most recent". Callers that need
            every run considered (e.g. bathos.blast_radius, where a dropped run
            is a false "not affected") must pass an explicit larger limit.
        runs: Optional preloaded run list, used instead of calling list_runs()
            again. For a caller (e.g. bathos.blast_radius.assess_blast_radius)
            that already scanned the same catalog/project/limit via its own
            list_runs() call and would otherwise double-scan the catalog for
            the exact same rows (debt #1486). `project`/`limit` are ignored
            when `runs` is given -- the caller is responsible for having
            fetched the equivalent set. Omit to fetch as before -- default
            behavior is unchanged.

    Returns:
        List of CheckResult objects
    """
    # Get current git state
    current_state = capture_git_state(project_root)
    current_hash = current_state.hash

    # Get all runs from catalog (or reuse a caller-supplied, already-scanned list)
    all_runs = runs if runs is not None else list_runs(catalog_dir, project=project, limit=limit)

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


def hash_dependency_lock(workspace_root: Path) -> str | None:
    """SHA256 of <workspace_root>/uv.lock, or None if it doesn't exist (debt #1071).

    Captured unconditionally on every run (runner.py) -- cheap, and the incident this
    guards against (a dependency re-pin silently invalidating every prior differential/SC
    result) is a general provenance gap, not something specific to differential runs.
    """
    lock_path = workspace_root / "uv.lock"
    if not lock_path.exists():
        return None
    try:
        h = hashlib.sha256()
        with open(lock_path, "rb") as f:
            while chunk := f.read(8192):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


#: Sentinel distinguishing "no current_sha256 supplied" (compute it) from "caller
#: already computed it and it happens to be None" (no uv.lock present) -- see
#: check_dependency_lock_drift's `current_sha256` param (debt #1487).
_UNSET = object()


def check_dependency_lock_drift(
    recorded_sha256: str | None,
    workspace_root: Path,
    *,
    current_sha256: str | None | object = _UNSET,
) -> bool:
    """Return True iff the current uv.lock hash differs from recorded_sha256 (debt #1071).

    Fails open (False, "no drift") if recorded_sha256 is falsy (e.g. a run predating this
    field) or if the current lockfile is itself absent -- nothing to compare against, and
    a missing baseline shouldn't be treated as evidence of change.

    Args:
        current_sha256: Optional precomputed `hash_dependency_lock(workspace_root)` result,
            for a caller (e.g. bathos.blast_radius.assess_blast_radius) that already hashed
            the lockfile once and would otherwise re-hash it on every call in a per-run loop
            (debt #1487). Omit to hash lazily as before -- default behavior is unchanged.
    """
    if not recorded_sha256:
        return False
    current = hash_dependency_lock(workspace_root) if current_sha256 is _UNSET else current_sha256
    if current is None:
        return False
    return current != recorded_sha256


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
