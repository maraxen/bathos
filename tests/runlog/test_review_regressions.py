"""Regressions for the step-2a review findings (registry fail-open, writer key, fallback slug)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bathos.runlog.project_id import (
    RegistryUnreadableError,
    assign_project_id,
    projects_registry,
    read_project_id,
    register_main_root,
)
from bathos.runlog.resolve import fallback_log_root, resolve_log_root
from bathos.runlog.writer import _writers, append_event, get_writer

from .conftest import make_git_repo, write_bth_toml

TORN = '[[roots]]\nroot = "/somewhere/else"\n[[roo'


def test_corrupt_registry_is_never_overwritten(tmp_path: Path):
    reg = projects_registry()
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(TORN)
    with pytest.raises(RegistryUnreadableError):
        register_main_root(make_git_repo(tmp_path / "repo"))
    assert reg.read_text() == TORN


def test_registry_failure_does_not_divert_event_to_fallback(tmp_path: Path):
    """The event is already durable in the project log; a registry failure must
    not re-append it to the fallback or leave the registry rewritten."""
    reg = projects_registry()
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(TORN)
    repo = make_git_repo(tmp_path / "repo")
    write_bth_toml(repo, slug="repo")
    assign_project_id(repo)
    res = resolve_log_root(repo)
    outcome = append_event(
        kind="test.recorded",
        entity=["e1"],
        data={},
        log_root=res,
        project="repo",
        project_id=read_project_id(repo / ".bth.toml"),
    )
    assert outcome.target == "project"
    assert not fallback_log_root(res).exists()
    assert reg.read_text() == TORN


def test_registry_preserves_other_roots_and_config_projects_key(tmp_path: Path):
    reg = projects_registry()
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text('[projects.other]\ncatalog_dir = "/x"\n\n[[roots]]\nroot = "/old/root"\n')
    assert register_main_root(make_git_repo(tmp_path / "repo"))
    text = reg.read_text()
    assert (
        "[projects.other]" in text
        and "/old/root" in text
        and str((tmp_path / "repo").resolve()) in text
    )


def test_get_writer_key_is_stable_across_mkdir_under_symlink(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    target = link / "logdir"
    w1 = get_writer(target, tmp_path / "m")
    target.mkdir()
    w2 = get_writer(target, tmp_path / "m")
    assert w1 is w2
    assert sum(1 for k in _writers if k.endswith("logdir")) == 1


def test_fallback_slug_ignores_ancestor_bth_toml(tmp_path: Path):
    write_bth_toml(tmp_path, slug="unrelated-parent")
    repo = make_git_repo(tmp_path / "child")
    res = resolve_log_root(repo)
    assert "unrelated-parent" not in str(fallback_log_root(res))
