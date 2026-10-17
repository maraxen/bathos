"""Git provenance capture -- thin shim over cisternal.provenance.channels.

`GitState`/`capture_git_state()` (env-var / sidecar-JSON / live-git
precedence, per design `260820_bathos-git-provenance-sidecar-spec.md` D6)
moved to `cisternal.provenance.channels`, shared with myxcel (the writer
half of the same protocol) so the two are never independently-maintained
copies of the same schema. Re-exported here unchanged so every existing
caller (`decorators.py`, `runner.py`, `checker.py`, `postmortem.py`,
`artifact_archive.py`, `mcp.py`, `cli.py`, and this module's own test suite)
keeps working with no signature or behavior change.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path
from typing import Any

from cisternal.provenance.channels import GitState, capture_git_state
from cisternal.provenance.channels import (
    _same_root as _same_root,  # re-exported: test_git.py imports it directly
)
from cisternal.provenance.channels import (
    _sidecar_channel as _sidecar_channel,  # re-exported: test_git.py imports it directly
)

__all__ = [
    "GitState",
    "capture_git_state",
    "capture_run_git_state",
    "git_state_as_dict",
    "paths_changed_since",
]

# Repo-location env vars that would make `git -C <dir>` report an ambient repo instead of
# the one at <dir>. Mirrors cisternal's stripped set so the work-tree probe below agrees with
# the capture that follows it.
_GIT_REPO_LOCATION_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
)


def _inside_git_work_tree(path: Path) -> bool:
    """True iff `path` resolves to a live git work tree (never raises)."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REPO_LOCATION_ENV_VARS}
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def capture_run_git_state(script_path: Path | None, cwd: Path) -> GitState:
    """Git provenance for a run, from the checkout the EXECUTED script lives in.

    A run's `cwd` is not necessarily the checkout its code comes from: a script run out of a
    linked worktree may be launched with the primary checkout as cwd (or with a stale cwd
    default in a long-lived process). Resolving git from `cwd` then records the wrong HEAD
    in the run record and the lock file (backlog #5609). So when the script sits inside a
    git work tree, its own directory is the authority; otherwise (no script bound, or a
    script outside any repo) this falls back to the run's `cwd`, the pre-existing behaviour.
    """
    if script_path is not None and _inside_git_work_tree(script_path.parent):
        return capture_git_state(script_path.parent)
    return capture_git_state(cwd)


def git_state_as_dict(state: GitState) -> dict[str, Any]:
    """JSON-ready GitState for event payloads, with `verification` summarised.

    cisternal>=0.1.1a8 attaches the full tree-manifest verification to GitState,
    including every mismatched/missing/extra path -- unbounded for a stale remote.
    Events keep the verdict and the counts; the path lists stay out of the log.
    """
    data = dataclasses.asdict(state)
    verification = getattr(state, "verification", None)
    if verification is not None:
        data["verification"] = {
            "verified": verification.verified,
            "n_files": verification.n_files,
            "n_match": verification.n_match,
            "n_mismatched": len(verification.mismatched),
            "n_missing": len(verification.missing),
            "n_extra": len(verification.extra_untracked_under_declared_dirs),
            "error": verification.error,
        }
    return data


def paths_changed_since(sha: str, paths: list[str], cwd: Path = Path.cwd()) -> bool:
    """True if any of `paths` differs between `sha` and the working tree.

    Compares the given commit against the index+worktree (no --cached), so this
    also catches uncommitted edits, not just committed-since-sha changes.

    Fail-safe: a blank sha/paths, or any git error (unknown sha, not a repo),
    counts as "changed" -- never silently reports unchanged on an error.

    Bathos-specific (not part of the shared multi-channel provenance concern),
    so it stays here rather than moving to cisternal.
    """
    if not sha or not paths:
        return True
    try:
        result = subprocess.run(
            ["git", "diff", "--quiet", sha, "--", *paths],
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return True
    return result.returncode != 0
