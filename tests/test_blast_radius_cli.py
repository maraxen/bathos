"""Blast-radius CLI tests (backlog #4551). Uses typer.testing.CliRunner.

Mirrors tests/test_attestation_cli.py conventions (BTH_CATALOG_DIR env override +
monkeypatch.chdir into a real git repo for resolve_workspace()'s git-toplevel rung).
"""

from __future__ import annotations

import json
import subprocess

import pytest

from bathos.catalog import init_catalog, write_run
from bathos.cli_cyclopts import app
from bathos.schema import Run
from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init"], r)
    _git(["config", "user.email", "test@example.com"], r)
    _git(["config", "user.name", "Test"], r)
    (r / "foo.py").write_text("a = 1\n")
    _git(["add", "foo.py"], r)
    _git(["commit", "-m", "initial"], r)
    return r


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    init_catalog(cat)
    return cat


def _fix_commit(repo):
    (repo / "foo.py").write_text("a = 2\n")
    _git(["add", "foo.py"], repo)
    _git(["commit", "-m", "fix bug"], repo)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


class TestBlastRadiusAssessCmd:
    def test_assess_by_commit_flags_matching_run(self, repo, catalog_dir, monkeypatch):
        pre_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        run = Run(
            project_slug="p", command="foo.py", argv=["foo.py"],
            git_hash=pre_sha, git_branch="main", git_dirty=False,
        )
        write_run(run, catalog_dir)
        fix_sha = _fix_commit(repo)

        monkeypatch.chdir(repo)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
        result = runner.invoke(app, ["blast-radius", "assess", "--commit", fix_sha])

        assert result.exit_code == 0, result.output
        assert "foo.py" in result.output
        assert json.loads(result.output)["flagged_count"] == 1

        status = runner.invoke(app, ["query", "blast-status", "run", run.id])
        assert json.loads(status.output)["status"] == "affected"

    def test_assess_requires_an_anchor(self, repo, catalog_dir, monkeypatch):
        monkeypatch.chdir(repo)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
        result = runner.invoke(app, ["blast-radius", "assess"])
        assert result.exit_code != 0

    def test_assess_by_dependency_flag(self, repo, catalog_dir, monkeypatch):
        (repo / "uv.lock").write_text("old\n")
        from bathos.checker import hash_dependency_lock

        old_hash = hash_dependency_lock(repo)
        run = Run(
            project_slug="p", command="foo.py", argv=["foo.py"],
            git_hash="abc", git_branch="main", git_dirty=False,
            dependency_lock_sha256=old_hash,
        )
        write_run(run, catalog_dir)
        (repo / "uv.lock").write_text("new\n")

        monkeypatch.chdir(repo)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
        result = runner.invoke(app, ["blast-radius", "assess", "--dependency"])

        assert result.exit_code == 0, result.output
        assert '"anchor_kind": "dependency"' in result.output


class TestBlastRadiusClearCmd:
    def test_clear_requires_reason(self, catalog_dir, monkeypatch):
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
        result = runner.invoke(app, ["blast-radius", "clear", "run", "run-x"])
        assert result.exit_code != 0

    def test_clear_writes_record(self, catalog_dir, monkeypatch):
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
        result = runner.invoke(
            app, ["blast-radius", "clear", "run", "run-x", "--reason", "verified fine"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["to_state"] == "cleared"


class TestBlastRadiusHookCmds:
    """Backlog #4555 CLI surface: install-hook/uninstall-hook/shadow-check/shadow-log."""

    def test_install_then_uninstall_hook(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        result = runner.invoke(app, ["blast-radius", "install-hook"])
        assert result.exit_code == 0, result.output
        assert (repo / ".bth" / "hooks" / "post-commit").exists()

        result = runner.invoke(app, ["blast-radius", "uninstall-hook"])
        assert result.exit_code == 0, result.output
        assert not (repo / ".bth" / "hooks").exists()

    def test_uninstall_without_install_errors(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        result = runner.invoke(app, ["blast-radius", "uninstall-hook"])
        assert result.exit_code != 0

    def test_shadow_check_records_and_shadow_log_lists_it(self, repo, catalog_dir, monkeypatch):
        from bathos.blast_radius import fold_blast_radius_state

        pre_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        run = Run(
            project_slug="p", command="foo.py", argv=["foo.py"],
            git_hash=pre_sha, git_branch="main", git_dirty=False,
        )
        write_run(run, catalog_dir)

        (repo / "foo.py").write_text("a = 2\n")
        _git(["add", "foo.py"], repo)
        _git(["commit", "-m", "fix bug"], repo)
        fix_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()

        monkeypatch.chdir(repo)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
        result = runner.invoke(app, ["blast-radius", "shadow-check", fix_sha])
        assert result.exit_code == 0, result.output
        assert fold_blast_radius_state(catalog_dir, "shadow_trigger", fix_sha) == "shadow_only"

        log_result = runner.invoke(app, ["query", "shadow-log"])
        assert log_result.exit_code == 0, log_result.output
        assert fix_sha[:9] in log_result.output

    def test_shadow_check_no_parent_commit_does_not_error(self, repo, catalog_dir, monkeypatch):
        first_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        monkeypatch.chdir(repo)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
        result = runner.invoke(app, ["blast-radius", "shadow-check", first_sha])
        assert result.exit_code == 0, result.output
