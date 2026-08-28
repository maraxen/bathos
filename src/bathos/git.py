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

import subprocess
from pathlib import Path

from cisternal.provenance.channels import GitState, capture_git_state
from cisternal.provenance.channels import (
    _same_root as _same_root,  # re-exported: test_git.py imports it directly
)
from cisternal.provenance.channels import (
    _sidecar_channel as _sidecar_channel,  # re-exported: test_git.py imports it directly
)

__all__ = ["GitState", "capture_git_state", "paths_changed_since"]


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
