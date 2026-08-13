"""Boundary-aware project-tree walking.

A bare `root.rglob(pattern)` has no concept of repo boundaries. In a project with
vendored submodules (each an independent git checkout) and agent-managed worktrees
under `.claude/worktrees/` (which may themselves contain nested, possibly-stale
worktrees of those same submodules), it visits the same logical file once per
nested checkout, plus files that belong to other projects entirely.

The consequences differ by caller, and the quieter one is the more dangerous:

- `linter.py` collected duplicate findings -- noisy, but visible (fixed in PR #45).
- `compact.py` and `postmortem.py` build lookups keyed by run/campaign id, so a
  foreign or stale copy silently *overwrites* the live entry. `compact()` feeds a
  postmortem's `verdict_override` into the run's recorded outcome, so a stale
  worktree copy could rewrite a result with no signal at all (backlog #4233).

A directory is a nested git boundary iff it has its own ".git" entry -- a directory
for a normal repo/submodule checkout, or a file for a worktree. That is the same
test git itself uses, and it excludes both without special-casing either.
"""

from __future__ import annotations

from pathlib import Path

from bathos.telemetry import event

# Directory names that never hold a project's own tracked content, so a walk
# should neither descend into them nor report their contents.
PRUNE_DIRS = frozenset(
    {
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".git",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
    }
)


def iter_project_files(
    root: Path,
    suffix: str,
    *,
    event_name: str = "walk.boundary_pruned",
) -> list[Path]:
    """Find every file at or below `root` whose name ends with `suffix`.

    Does not cross into a nested git boundary (submodule or worktree checkout) or
    any `PRUNE_DIRS` directory. Results are sorted, so callers get a stable order
    without each remembering to sort.

    `root` itself is never treated as a boundary: the caller has explicitly asked
    for this subtree, so a scan rooted inside a worktree scans that worktree rather
    than immediately halting on its own ".git". Callers wanting a narrower scope
    should pass the narrower directory as `root` rather than filtering afterwards --
    that avoids walking a tree only to discard most of it.

    Pruning is scope-*reducing*, so it emits `event_name` when it fires. A silently
    dropped file is indistinguishable from a clean project, which is the worse
    direction of error for a provenance tool.

    Note: symlinked directories are not followed (`Path.walk()` default), so a file
    reachable only through a symlinked directory is not returned.

    Args:
        root: Directory to walk. Never treated as a boundary itself.
        suffix: Matched with `str.endswith`, not glob. Pass the full compound
            extension (e.g. ".bth.postmortem.toml") -- note that
            "x.bth.postmortem.toml".endswith(".bth.toml") is False, so compound
            suffixes in this codebase do not collide.
        event_name: Telemetry event emitted when at least one boundary is pruned.

    Returns:
        Sorted list of matching paths.
    """
    found: list[Path] = []
    pruned = 0
    for dirpath, dirnames, filenames in root.walk():
        if dirpath != root and (dirpath / ".git").exists():
            pruned += 1
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
        found.extend(dirpath / f for f in filenames if f.endswith(suffix))
    if pruned:
        event(event_name, root=str(root), suffix=suffix, pruned_boundaries=pruned)
    return sorted(found)
