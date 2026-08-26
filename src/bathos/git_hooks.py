"""Wrapped-core.hooksPath git-hook installer (backlog #4555).

Blast-radius-agnostic: this module knows nothing about bathos.blast_radius. It is a
generic mechanism for installing hook scripts via a bathos-managed `core.hooksPath`
directory WITHOUT clobbering whatever hooks were already active -- either individual
scripts under the default `.git/hooks/` or a pre-existing custom `core.hooksPath` set by
another tool (husky, the `pre-commit` framework, etc.).

Design (spec .praxia/docs/specs/260826_blast-radius-shadow-trigger.md, Decision Log #3/#4):

- `core.hooksPath` (repo-local, not --global) is the git-native mechanism for this --
  chosen over writing directly into `.git/hooks/<name>` specifically so an existing hook
  of the same name is never silently overwritten.
- For a hook name bathos wants to install, if a hook of that name already exists at the
  PREVIOUSLY-active hooks location, the installed script CHAINS to it (execs the original
  first, `|| true` so a failing original hook can never suppress bathos's own logic --
  post-commit's exit code is ignored by git anyway, so this is safe for that hook type).
- For every OTHER hook name present at the previously-active location, a symlink is
  created pointing at the original file, so it keeps firing completely unchanged.
- The previously-active hooks location is recorded in `<managed_dir>/.bathos_state.json`
  BEFORE `core.hooksPath` is repointed, so `uninstall_managed_hooks` can restore it
  exactly, and so a second `install_managed_hooks` call (idempotent re-run, e.g. after
  editing the hook's own script content) re-derives from the ORIGINAL prior state rather
  than bathos's own already-installed wrapper (which would double-chain).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

_STATE_FILENAME = ".bathos_state.json"
_SAMPLE_SUFFIX = ".sample"  # git ships *.sample hooks by default; never executable, skip


def _get_explicit_hooks_path(repo_root: Path) -> str | None:
    """The raw `core.hooksPath` config value, or None if it was never explicitly
    set. Kept separate from the RESOLVED hooks directory (see
    `_resolve_hooks_dir`) because "unset" and "explicitly set to the default
    .git/hooks path" must round-trip differently on uninstall (SAC-3): the
    former must be restored by `--unset`, not by writing the literal default
    path back into config."""
    result = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def _resolve_hooks_dir(repo_root: Path, explicit_hooks_path: str | None) -> Path:
    """The hooks directory to treat as authoritative for `explicit_hooks_path`:
    that path if given (resolved relative to repo_root per git's own
    convention), else the TRUE default `<git-common-dir>/hooks`. Used to locate
    pre-existing hook files to chain/symlink -- NOT recorded as the rollback
    value; see `_get_explicit_hooks_path` for that.

    Deliberately uses `git rev-parse --git-common-dir` (worktree-aware: hooks
    are shared across worktrees, not per-worktree) rather than
    `git rev-parse --git-path hooks` for the default case -- the latter
    resolves THROUGH the CURRENT `core.hooksPath`, which is exactly wrong here:
    on an idempotent re-install, `explicit_hooks_path=None` means "the ORIGINAL
    config had no hooksPath", and by the time this runs again core.hooksPath
    already points at bathos's own managed_dir from the first install. Using
    `--git-path hooks` here previously resolved back to managed_dir itself,
    causing every hook to chain to its own already-installed wrapper --
    caught by test_installing_twice_does_not_double_chain, which found the
    marker file with 75000+ lines (recursive self-invocation until some
    resource limit killed it) instead of exactly one."""
    if explicit_hooks_path:
        path = Path(explicit_hooks_path)
        return path if path.is_absolute() else (repo_root / path)

    git_common_dir = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    git_dir = Path(git_common_dir)
    if not git_dir.is_absolute():
        git_dir = repo_root / git_dir
    return git_dir / "hooks"


def _read_state(managed_dir: Path) -> dict | None:
    state_path = managed_dir / _STATE_FILENAME
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text())


def install_managed_hooks(
    repo_root: Path, managed_dir: Path, hook_scripts: dict[str, str]
) -> None:
    """Point `core.hooksPath` at `managed_dir`, preserving whatever hooks were
    already active.

    Args:
        repo_root: Repository root (where `git config` commands run).
        managed_dir: Directory bathos owns for its wrapped hooks.
        hook_scripts: Hook name -> script content bathos wants installed for
            that hook (e.g. `{"post-commit": "#!/bin/sh\\n..."}`). Every other
            hook name found at the previously-active location is symlinked
            through unchanged.

    Idempotent: if `managed_dir` already has a recorded prior state (a previous
    install), that recorded state -- not the current `core.hooksPath`, which
    already points at `managed_dir` -- is used as the source for chaining and
    symlinking, so re-running this never chains to bathos's own prior wrapper.
    """
    existing_state = _read_state(managed_dir)
    # previous_explicit_value is the raw rollback value for `git config core.hooksPath`
    # (or None, meaning "was unset" -- restore via --unset, not by writing the literal
    # default path back into config; see _get_explicit_hooks_path's docstring).
    if existing_state is not None:
        previous_explicit_value = existing_state["previous_hooks_path"]
    else:
        previous_explicit_value = _get_explicit_hooks_path(repo_root)

    # chain_source_dir is always a real, resolved directory to look for pre-existing
    # hook files in -- defaults to .git/hooks when core.hooksPath was never set.
    chain_source_dir = _resolve_hooks_dir(repo_root, previous_explicit_value)

    managed_dir.mkdir(parents=True, exist_ok=True)

    # Record state BEFORE repointing core.hooksPath, so a crash mid-install never
    # leaves git pointed at a directory with no recorded rollback path.
    state_path = managed_dir / _STATE_FILENAME
    state_path.write_text(json.dumps({"previous_hooks_path": previous_explicit_value}))

    preexisting_names = set()
    if chain_source_dir.exists():
        preexisting_names = {
            f.name
            for f in chain_source_dir.iterdir()
            if f.is_file() and not f.name.endswith(_SAMPLE_SUFFIX) and f.name != _STATE_FILENAME
        }

    for name, script in hook_scripts.items():
        target = managed_dir / name
        original = chain_source_dir / name
        if original.exists() and name in preexisting_names:
            content = f'#!/bin/sh\n"{original}" "$@" || true\n{_strip_shebang(script)}'
        else:
            content = script
        target.write_text(content)
        target.chmod(0o755)

    for name in preexisting_names - set(hook_scripts):
        original = chain_source_dir / name
        link = managed_dir / name
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(original.resolve())

    subprocess.run(
        ["git", "config", "core.hooksPath", str(managed_dir)],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )


def _strip_shebang(script: str) -> str:
    """Drop a leading `#!...` line so a chained script doesn't declare the
    interpreter twice (the chain prefix already opens with its own shebang)."""
    lines = script.splitlines(keepends=True)
    if lines and lines[0].startswith("#!"):
        return "".join(lines[1:])
    return script


def uninstall_managed_hooks(repo_root: Path, managed_dir: Path) -> None:
    """Restore `core.hooksPath` to its pre-install value (or unset it) and
    remove `managed_dir`.

    Raises:
        FileNotFoundError: `managed_dir` has no recorded install state (never
            installed, or already uninstalled).
    """
    state = _read_state(managed_dir)
    if state is None:
        raise FileNotFoundError(
            f"{managed_dir} has no recorded bathos hook state -- not installed "
            "(or already uninstalled)"
        )

    previous = state["previous_hooks_path"]
    if previous:
        subprocess.run(
            ["git", "config", "core.hooksPath", previous],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
    else:
        subprocess.run(
            ["git", "config", "--unset", "core.hooksPath"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )

    shutil.rmtree(managed_dir)
