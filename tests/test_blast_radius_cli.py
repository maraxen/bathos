"""Blast-radius CLI tests (backlog #4551). Uses typer.testing.CliRunner.

Mirrors tests/test_attestation_cli.py conventions (BTH_CATALOG_DIR env override +
monkeypatch.chdir into a real git repo for resolve_workspace()'s git-toplevel rung).
"""

from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

from bathos.catalog import init_catalog, write_run
from bathos.cli import app
from bathos.schema import Run

runner = CliRunner()


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
        assert "Flagged 1 run" in result.output

        status = runner.invoke(app, ["query", "blast-status", "run", run.id])
        assert status.output.strip() == "affected"

    def test_assess_requires_an_anchor(self, repo, catalog_dir, monkeypatch):
        monkeypatch.chdir(repo)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
        result = runner.invoke(app, ["blast-radius", "assess"])
        assert result.exit_code != 0


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
