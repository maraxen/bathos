"""Worktree-aware workspace resolution.

`resolve_workspace(cwd)` returns a `WorkspaceContext` that distinguishes:

- **identity** (`slug`, `identity_root`) — stable across all worktrees of one
  repo, sourced from `.bth.toml` / `BTH_PROJECT_SLUG`. NEVER derived from the
  filesystem root (catalog identity must not fragment across worktrees).
- **filesystem root** (`fs_root`) — the LIVE directory for workspace-relative
  file operations (postmortem asset validation/scan), resolved via the ladder
  `BTH_WORKSPACE_ROOT -> git toplevel -> recorded [project] root -> cwd`.

See `.praxia/docs/specs/260611_worktree-workspace-resolution.md`.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspaceContext:
    slug: str | None
    identity_root: Path | None  # recorded [project] root; identity/display only
    fs_root: Path  # live filesystem root for workspace-relative file ops (never None)
    is_worktree: bool
    worktree_name: str | None
    source: str  # which rung resolved fs_root: "env" | "git" | "config" | "cwd"


def _git_anchors(cwd: Path) -> tuple[Path, bool] | None:
    """Return (toplevel, is_worktree) from a single `git rev-parse`, or None.

    One subprocess yields the live worktree top (`--show-toplevel`) and the two
    git-dir variants used to detect a linked worktree. The call is all-or-nothing:
    if any flag fails (e.g. `--show-toplevel` in a bare repo) the whole command
    exits non-zero and we fall through to the next resolution rung.
    """
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel", "--git-common-dir", "--git-dir"],
            cwd=cwd,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError):
        return None

    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if len(lines) < 3:
        return None
    toplevel_s, git_common_s, git_dir_s = lines[0], lines[1], lines[2]

    def _abs(p: str) -> Path:
        # --git-common-dir / --git-dir may be relative to cwd on some git versions.
        pp = Path(p)
        return pp if pp.is_absolute() else (cwd / pp)

    try:
        is_worktree = _abs(git_dir_s).resolve() != _abs(git_common_s).resolve()
    except OSError:
        is_worktree = False

    return Path(toplevel_s), is_worktree


def resolve_workspace(cwd: Path | None = None) -> WorkspaceContext:
    """Resolve workspace identity + live filesystem root for the given cwd."""
    cwd = cwd or Path.cwd()

    # --- Identity (slug + recorded root): from env / .bth.toml field, NEVER fs_root ---
    from bathos.config import find_project_config, load_project_config

    slug: str | None = os.environ.get("BTH_PROJECT_SLUG")
    identity_root: Path | None = None
    project_root: Path | None = None
    cfg_path = find_project_config(cwd)
    if cfg_path is not None:
        try:
            pc = load_project_config(cfg_path)
            identity_root = pc.root
            project_root = pc.root
            if slug is None:
                slug = pc.slug
        except Exception:
            # A malformed .bth.toml must not crash resolution; identity stays partial.
            pass

    # --- Worktree detection (single git call, independent of which rung wins) ---
    git = _git_anchors(cwd)
    is_worktree = git[1] if git is not None else False

    # --- fs_root precedence ladder: env -> git toplevel -> recorded root -> cwd ---
    env_root = os.environ.get("BTH_WORKSPACE_ROOT")
    if env_root:
        fs_root = Path(env_root).expanduser().resolve()
        source = "env"
    elif git is not None:
        fs_root = git[0]
        source = "git"
    elif project_root is not None:
        fs_root = project_root
        source = "config"
    else:
        fs_root = cwd
        source = "cwd"

    worktree_name = fs_root.name if is_worktree else None

    return WorkspaceContext(
        slug=slug,
        identity_root=identity_root,
        fs_root=fs_root,
        is_worktree=is_worktree,
        worktree_name=worktree_name,
        source=source,
    )
