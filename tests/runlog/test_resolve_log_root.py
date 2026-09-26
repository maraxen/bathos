"""AC-16: root resolution -- main checkout, linked worktree, bare main,
submodule, relative --git-common-dir, BTH_WORKSPACE_ROOT set, no git repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bathos.runlog.resolve import resolve_log_root

from .conftest import make_git_repo, write_bth_toml


def test_main_checkout(tmp_path: Path):
    repo = make_git_repo(tmp_path / "repo")
    res = resolve_log_root(repo)
    assert res.main_root == repo.resolve()
    assert res.worktree_root == repo.resolve()
    assert not res.unaffiliated
    assert res.warning is None


def test_linked_worktree_maps_to_main(tmp_path: Path):
    repo = make_git_repo(tmp_path / "repo")
    worktree = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-b", "feature", str(worktree)], cwd=repo, check=True)
    res = resolve_log_root(worktree)
    assert res.main_root == repo.resolve()
    assert res.worktree_root == worktree.resolve()
    assert not res.unaffiliated
    assert res.warning is None


def test_bare_main_falls_back_to_linked_root_with_warning(tmp_path: Path):
    # A bare repo with worktrees attached: the *first* `git worktree list`
    # entry is the bare repo itself, so resolution should fall back to the
    # linked worktree's own root and warn, per the spec's "if bare" clause.
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    worktree = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", str(worktree), "-b", "main"], cwd=bare, check=True)
    res = resolve_log_root(worktree)
    assert res.main_root == worktree.resolve()
    assert res.worktree_root == worktree.resolve()
    assert res.warning is not None
    assert "bare" in res.warning


def test_submodule_uses_its_own_root(tmp_path: Path):
    outer = make_git_repo(tmp_path / "outer")
    inner = make_git_repo(tmp_path / "inner-source")
    subprocess.run(
        ["git", "submodule", "add", str(inner), "sub"],
        cwd=outer,
        check=True,
        env={"GIT_ALLOW_PROTOCOL": "file", **_env()},
    )
    submodule_root = outer / "sub"
    res = resolve_log_root(submodule_root)
    assert res.main_root == submodule_root.resolve()
    assert res.worktree_root == submodule_root.resolve()
    assert not res.unaffiliated


def _env() -> dict:
    import os

    return dict(os.environ)


def test_relative_git_common_dir_still_resolves(tmp_path: Path):
    # resolve_workspace's own `_git_anchors` already normalizes a relative
    # --git-common-dir/--git-dir against cwd; this test exercises that path
    # end to end through resolve_log_root rather than re-testing
    # bathos.workspace directly.
    repo = make_git_repo(tmp_path / "repo")
    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature2", str(worktree)], cwd=repo, check=True
    )
    # Sanity: git itself may report either absolute or relative common-dir
    # depending on version/config; either way resolve_log_root must land on
    # the same main root.
    res = resolve_log_root(worktree)
    assert res.main_root == repo.resolve()


def test_no_git_repo_and_no_bth_toml_is_unaffiliated(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    res = resolve_log_root(plain)
    assert res.unaffiliated
    assert res.main_root == plain.resolve()


def test_no_git_repo_but_bth_toml_present_is_affiliated(tmp_path: Path):
    plain = tmp_path / "plain2"
    plain.mkdir()
    write_bth_toml(plain, slug="noGit")
    res = resolve_log_root(plain)
    assert not res.unaffiliated
    assert res.main_root == plain.resolve()


def test_bth_workspace_root_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = make_git_repo(tmp_path / "repo")
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(repo))
    res = resolve_log_root(other)
    assert res.main_root == repo.resolve()
