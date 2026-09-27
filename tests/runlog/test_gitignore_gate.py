"""D3 / AC-10: `.bth/log/` must be git-ignored before any local writer appends
to it. `bth run` fails with a structured error; `bth init` fixes the tree
instead of refusing it. With no git repository, the check is skipped."""

from __future__ import annotations

from pathlib import Path

import pytest

from bathos.errors import EXCEPTION_TO_CODE, RESOLUTION_HINTS, BathosErrorCode
from bathos.runlog.resolve import (
    LogNotIgnoredError,
    ensure_log_ignored,
    is_log_ignored,
    require_log_ignored,
)

from .conftest import make_git_repo


def test_not_ignored_raises_structured_error(tmp_path: Path):
    repo = make_git_repo(tmp_path / "repo")
    assert not is_log_ignored(repo)
    with pytest.raises(LogNotIgnoredError):
        require_log_ignored(repo)


def test_error_is_registered_with_a_resolution_hint():
    code = EXCEPTION_TO_CODE["LogNotIgnoredError"]
    assert code == BathosErrorCode.RUNLOG_NOT_IGNORED
    assert RESOLUTION_HINTS[code]


def test_ensure_log_ignored_adds_rule_and_fixes_the_gate(tmp_path: Path):
    repo = make_git_repo(tmp_path / "repo")
    assert ensure_log_ignored(repo) is True
    assert "/.bth/log/" in (repo / ".gitignore").read_text().splitlines()
    assert is_log_ignored(repo)
    require_log_ignored(repo)  # must not raise now


def test_ensure_log_ignored_idempotent(tmp_path: Path):
    repo = make_git_repo(tmp_path / "repo")
    assert ensure_log_ignored(repo) is True
    assert ensure_log_ignored(repo) is False
    content = (repo / ".gitignore").read_text()
    assert content.count("/.bth/log/") == 1


def test_ensure_log_ignored_appends_to_existing_gitignore(tmp_path: Path):
    repo = make_git_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text("*.pyc")  # no trailing newline
    assert ensure_log_ignored(repo) is True
    lines = (repo / ".gitignore").read_text().splitlines()
    assert "*.pyc" in lines
    assert "/.bth/log/" in lines


def test_no_git_repo_skips_the_check(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert is_log_ignored(plain)  # nothing to dirty -> check is skipped (True)
    require_log_ignored(plain)  # must not raise
    assert ensure_log_ignored(plain) is False  # nothing to add


def test_already_ignored_via_parent_pattern(tmp_path: Path):
    repo = make_git_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text(".bth/\n")
    import subprocess

    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "ignore .bth"], cwd=repo, check=True)
    assert is_log_ignored(repo)
    require_log_ignored(repo)  # must not raise
    assert ensure_log_ignored(repo) is False  # already covered, nothing added
