"""Unit coverage for bathos.workspace.resolve_workspace (spec 260611).

Covers AC-1..7, AC-9 and the §5.3 sanity invariant (identity is never derived
from the filesystem root).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bathos import workspace
from bathos.workspace import resolve_workspace


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True, capture_output=True)
    (path / "seed.txt").write_text("seed")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def _write_bth_toml(root: Path, slug: str, recorded_root: Path) -> None:
    (root / ".bth.toml").write_text(
        f'[project]\nslug = "{slug}"\nroot = "{recorded_root}"\n'
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("BTH_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("BTH_PROJECT_SLUG", raising=False)


def test_main_checkout(tmp_path: Path):  # AC-2
    repo = tmp_path / "repo"
    _init_repo(repo)
    ctx = resolve_workspace(repo)
    assert ctx.fs_root.resolve() == repo.resolve()
    assert ctx.is_worktree is False
    assert ctx.source == "git"


def test_worktree_fs_root(tmp_path: Path):  # AC-1
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_bth_toml(repo, "proj", repo)  # recorded root = main checkout
    wt = tmp_path / "wt1"
    subprocess.run(["git", "worktree", "add", str(wt)], cwd=repo, check=True, capture_output=True)
    ctx = resolve_workspace(wt)
    assert ctx.fs_root.resolve() == wt.resolve()  # LIVE worktree, not recorded root
    assert ctx.is_worktree is True
    assert ctx.worktree_name == wt.name
    assert ctx.source == "git"


def test_identity_stable_across_nested_worktrees(tmp_path: Path):  # AC-3
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_bth_toml(repo, "proj", repo)
    wt1 = repo / "wts" / "wt1"
    wt2 = repo / "wts" / "wt2"
    subprocess.run(["git", "worktree", "add", str(wt1)], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "worktree", "add", str(wt2)], cwd=repo, check=True, capture_output=True)
    c_main = resolve_workspace(repo)
    c1 = resolve_workspace(wt1)
    c2 = resolve_workspace(wt2)
    # identity stable across all three (slug + recorded identity_root)
    assert c_main.slug == c1.slug == c2.slug == "proj"
    assert c1.identity_root == c2.identity_root == c_main.identity_root == repo
    # but the live fs_root differs per worktree
    assert c1.fs_root.resolve() == wt1.resolve()
    assert c2.fs_root.resolve() == wt2.resolve()
    assert c1.fs_root != c2.fs_root


def test_env_override_wins(tmp_path: Path, monkeypatch):  # AC-4
    repo = tmp_path / "repo"
    _init_repo(repo)
    target = tmp_path / "ws"
    target.mkdir()
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(target))
    ctx = resolve_workspace(repo)  # even inside a git repo, env wins
    assert ctx.fs_root == target.resolve()
    assert ctx.source == "env"


def test_env_override_relative_is_resolved(tmp_path: Path, monkeypatch):  # AC-4 / O10
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", "./ws")
    ctx = resolve_workspace(tmp_path)
    assert ctx.fs_root.is_absolute()
    assert ctx.fs_root == (tmp_path / "ws").resolve()


def test_not_in_git_uses_recorded_root(tmp_path: Path):  # AC-5
    work = tmp_path / "plain"
    work.mkdir()
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    _write_bth_toml(work, "proj", recorded)
    ctx = resolve_workspace(work)
    assert ctx.fs_root == recorded
    assert ctx.is_worktree is False
    assert ctx.source == "config"


def test_no_git_no_config_uses_cwd(tmp_path: Path):  # AC-6
    work = tmp_path / "bare"
    work.mkdir()
    ctx = resolve_workspace(work)
    assert ctx.fs_root == work
    assert ctx.source == "cwd"


def test_relative_git_common_dir_handled(tmp_path: Path, monkeypatch):  # AC-7
    # Simulate a git version returning RELATIVE --git-common-dir / --git-dir.
    def fake_check_output(_cmd, cwd=None, **_kwargs):
        # toplevel, --git-common-dir, --git-dir (relative)
        return f"{cwd}\n.git\n.git/worktrees/wt1\n"

    monkeypatch.setattr(workspace.subprocess, "check_output", fake_check_output)
    ctx = resolve_workspace(tmp_path)
    assert ctx.is_worktree is True  # .git != .git/worktrees/wt1 after abs-resolution
    assert ctx.source == "git"


def test_detached_head_worktree(tmp_path: Path):  # AC-9
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt = tmp_path / "det"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(wt)], cwd=repo, check=True, capture_output=True
    )
    ctx = resolve_workspace(wt)  # must not raise
    assert ctx.fs_root.resolve() == wt.resolve()
    assert ctx.is_worktree is True


def test_identity_not_derived_from_fs_root(tmp_path: Path, monkeypatch):  # §5.3 sanity invariant
    work = tmp_path / "plain"
    work.mkdir()
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    _write_bth_toml(work, "real-slug", recorded)
    # Point fs_root at a totally unrelated dir via env; identity must be unaffected.
    bogus = tmp_path / "bogus-fs-root"
    bogus.mkdir()
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(bogus))
    ctx = resolve_workspace(work)
    assert ctx.fs_root == bogus.resolve()  # fs_root followed the env
    assert ctx.slug == "real-slug"  # identity did NOT follow fs_root
    assert ctx.identity_root == recorded
