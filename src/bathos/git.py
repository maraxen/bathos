from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GitState:
    hash: str
    branch: str
    dirty: bool


_UNKNOWN = GitState(hash="unknown", branch="unknown", dirty=False)


def capture_git_state(cwd: Path = Path.cwd()) -> GitState:
    try:
        hash_ = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=cwd, text=True, stderr=subprocess.DEVNULL
        ).strip()
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=cwd,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
        return GitState(hash=hash_, branch=branch, dirty=dirty)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return _UNKNOWN


def paths_changed_since(sha: str, paths: list[str], cwd: Path = Path.cwd()) -> bool:
    """True if any of `paths` differs between `sha` and the working tree.

    Compares the given commit against the index+worktree (no --cached), so this
    also catches uncommitted edits, not just committed-since-sha changes.

    Fail-safe: a blank sha/paths, or any git error (unknown sha, not a repo),
    counts as "changed" — never silently reports unchanged on an error.
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
