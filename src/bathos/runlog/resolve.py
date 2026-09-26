"""Log directory resolution (AC-16) and the D3 gitignore check (AC-10).

Built on the existing `resolve_workspace()` ladder (`bathos.workspace`), not a
new one -- see the spec's "Log directory resolution" section. This module adds
one further rung on top of `resolve_workspace()`: mapping a linked worktree
(or submodule) to its main checkout, per D1.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from bathos.workspace import resolve_workspace

from .mode import RunLogError


class LogNotIgnoredError(RunLogError):
    """`.bth/log/` is not git-ignored under the resolved main root (D3, AC-10).

    `bth run` fails with this rather than silently appending events into a
    tree git can see -- an append would either mark the tree dirty on every
    run (poisoning `capture_git_state`'s own dirty detection) or, worse, get
    committed. `bth init` should call `ensure_log_ignored()` instead, which
    fixes this by adding the rule rather than raising.
    """


@dataclass(frozen=True)
class LogRootResolution:
    """Where a project's log lives, per D1 + AC-16."""

    main_root: Path
    """`<main root>/.bth/log/` per D1 -- always the MAIN checkout, never a
    linked worktree, even when the caller's cwd is inside one."""

    worktree_root: Path
    """The live directory the caller is actually running from -- recorded on
    every event's `worktree_root` field, per the Line envelope."""

    unaffiliated: bool
    """True when there is no git repo and no `.bth.toml`: no project owns this
    log, so it goes to `~/.bth/log/unaffiliated/` instead of a project root."""

    warning: str | None = None
    """Set when a rung of the ladder could not be resolved as specified and a
    fallback was used instead (a bare main-worktree entry, or an
    unparseable `git worktree list`)."""


@dataclass(frozen=True)
class _WorktreeEntry:
    path: Path
    bare: bool


def _run_git(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _has_git_repo(cwd: Path) -> bool:
    return (
        _run_git(["rev-parse", "--is-inside-work-tree"], cwd) is not None
        or _run_git(["rev-parse", "--is-bare-repository"], cwd) is not None
    )


def _is_linked_worktree(root: Path) -> bool:
    """True if `root` is itself a linked worktree (its `--git-dir` differs
    from `--git-common-dir`). Computed fresh against `root`, deliberately NOT
    reusing `resolve_workspace()`'s own `is_worktree` flag: that flag is
    derived from the caller's raw cwd, which can differ from `root` once
    `BTH_WORKSPACE_ROOT` or the `.bth.toml` recorded root have redirected
    `fs_root` elsewhere (AC-16's "BTH_WORKSPACE_ROOT set" case)."""
    out = _run_git(["rev-parse", "--git-common-dir", "--git-dir"], root)
    if out is None:
        return False
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    common_s, dir_s = lines[0], lines[1]

    def _abs(p: str) -> Path:
        pp = Path(p)
        return pp if pp.is_absolute() else (root / pp)

    try:
        return _abs(dir_s).resolve() != _abs(common_s).resolve()
    except OSError:
        return False


def _git_worktree_list(cwd: Path) -> list[_WorktreeEntry] | None:
    """Parse `git worktree list --porcelain` into an ordered entry list.

    Each entry is separated by a blank line; a bare repository's entry has a
    `bare` line instead of `HEAD`/`branch`. Returns None if the command could
    not be run or produced nothing parseable.
    """
    out = _run_git(["worktree", "list", "--porcelain"], cwd)
    if out is None:
        return None
    entries: list[_WorktreeEntry] = []
    current_path: Path | None = None
    current_bare = False
    for line in out.splitlines():
        if line.startswith("worktree "):
            if current_path is not None:
                entries.append(_WorktreeEntry(path=current_path, bare=current_bare))
            current_path = Path(line[len("worktree ") :])
            current_bare = False
        elif line.strip() == "bare":
            current_bare = True
        elif line == "" and current_path is not None:
            entries.append(_WorktreeEntry(path=current_path, bare=current_bare))
            current_path = None
            current_bare = False
    if current_path is not None:
        entries.append(_WorktreeEntry(path=current_path, bare=current_bare))
    return entries or None


def resolve_log_root(cwd: Path | None = None) -> LogRootResolution:
    """Resolve the log directory's owning root for `cwd` (default: cwd).

    AC-16 cases covered: main checkout, linked worktree, bare main, submodule,
    relative `--git-common-dir` (handled inside `resolve_workspace`),
    `BTH_WORKSPACE_ROOT` set (ditto), no git repo.
    """
    cwd = (cwd or Path.cwd()).resolve()
    ws = resolve_workspace(cwd)
    fs_root = ws.fs_root.resolve()

    has_bth_toml = (fs_root / ".bth.toml").exists()
    has_git = _has_git_repo(fs_root)

    if not has_git and not has_bth_toml:
        return LogRootResolution(main_root=fs_root, worktree_root=fs_root, unaffiliated=True)

    if not _is_linked_worktree(fs_root):
        # Main checkout (or a submodule, which git does not consider a linked
        # worktree of its superproject -- resolve_workspace's own
        # `git rev-parse --git-dir`/`--git-common-dir` comparison already
        # reports False for a submodule root, so it lands here with its own
        # root, matching "a submodule is its own repository").
        return LogRootResolution(main_root=fs_root, worktree_root=fs_root, unaffiliated=False)

    entries = _git_worktree_list(fs_root)
    if not entries:
        return LogRootResolution(
            main_root=fs_root,
            worktree_root=fs_root,
            unaffiliated=False,
            warning=(
                "could not enumerate `git worktree list --porcelain` from a linked "
                "worktree; using the linked worktree's own root as the log root"
            ),
        )
    first = entries[0]
    if first.bare:
        return LogRootResolution(
            main_root=fs_root,
            worktree_root=fs_root,
            unaffiliated=False,
            warning=(
                f"main worktree entry {first.path} is bare; using the linked "
                "worktree's own root as the log root"
            ),
        )
    return LogRootResolution(
        main_root=first.path.resolve(), worktree_root=fs_root, unaffiliated=False
    )


def fallback_log_root(resolution: LogRootResolution) -> Path:
    """`~/.bth/log/fallback/<slug or _unaffiliated>/` (D6)."""
    from bathos.config import find_project_config, load_project_config

    slug: str | None = None
    if not resolution.unaffiliated:
        cfg_path = find_project_config(resolution.main_root)
        if cfg_path is not None:
            try:
                slug = load_project_config(cfg_path).slug
            except Exception:
                slug = None
    return Path.home() / ".bth" / "log" / "fallback" / (slug or "_unaffiliated")


# --- D3: the log must be gitignored -----------------------------------------


def _log_ignore_probe_path(main_root: Path) -> Path:
    return main_root / ".bth" / "log" / "x"


def is_log_ignored(main_root: Path) -> bool:
    """True if `.bth/log/` is git-ignored under `main_root`.

    With no git repository, there is nothing to dirty and the check is
    skipped (returns True): a `.bth.toml`-only project has no working tree
    for appends to mark dirty.
    """
    if not _has_git_repo(main_root):
        return True
    probe = _log_ignore_probe_path(main_root)
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(probe)],
            cwd=main_root,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Fail closed: an unreadable git state must not be silently treated
        # as "ignored" (which would let bth run start appending unignored
        # events into a tracked tree).
        return False
    return result.returncode == 0


def require_log_ignored(main_root: Path) -> None:
    """Raise `LogNotIgnoredError` unless `.bth/log/` is git-ignored.

    Called by `bth run` (2b) before any event append; `bth init` should call
    `ensure_log_ignored` instead, which fixes the tree rather than refusing it.
    """
    if not is_log_ignored(main_root):
        raise LogNotIgnoredError(
            f"{main_root}/.bth/log/ is not git-ignored. Add '/.bth/log/' to "
            f"{main_root}/.gitignore (or run `bth init` there) before recording "
            "runs, so appends never mark the tree dirty or get committed."
        )


_GITIGNORE_LOG_ENTRY = "/.bth/log/"


def ensure_log_ignored(main_root: Path) -> bool:
    """Add `/.bth/log/` to `.gitignore` if `.bth/log/` is not already ignored.

    Returns True if an entry was appended, False if it was already ignored
    (including the no-git-repo case, where there is nothing to add). Called by
    `bth init`.
    """
    if is_log_ignored(main_root):
        return False
    gitignore = main_root / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    if _GITIGNORE_LOG_ENTRY in existing.splitlines():
        return False
    with open(gitignore, "a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(_GITIGNORE_LOG_ENTRY + "\n")
    return True
