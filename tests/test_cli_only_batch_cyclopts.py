"""CLI-level tests for the "CLI-only batch" (backlog #4702 Milestone 2,
sequencing step 5): 22 commands with no MCP-tool equivalent, hand-ported
directly from `bathos.cli`'s Typer bodies onto the preview cyclopts app
(`bathos.cli_cyclopts.app`, `bth-preview`) via the `CyclopticRunner` shim.

Mirrors the two prior CLI-level test files this session
(`test_registry_group_cli_cyclopts.py`, `test_extraction_batch_cli_cyclopts.py`):
one happy-path + one error-path per command where a natural error case exists.
Several fixtures/mocking strategies are ported directly from the equivalent
Typer-CLI tests (`test_bth_submit.py`, `test_blast_radius_cli.py`,
`test_cli.py`'s report-emit smoke test) -- reusing proven setups rather than
inventing new ones.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from bathos.catalog import init_catalog, write_run
from bathos.cli_cyclopts import app
from bathos.compact import compact
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


def _rev(path: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=path, text=True, capture_output=True, check=True
    ).stdout.strip()


def _write_project_toml(tmp_path: Path) -> None:
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n'
    )


# ---------------------------------------------------------------------------
# remote
# ---------------------------------------------------------------------------


class TestRemoteCyclopts:
    def test_add_then_list_round_trips(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_project_toml(tmp_path)

        result = runner.invoke(app, ["remote", "add", "engaging", "engaging:~/projects/x"])
        assert result.exit_code == 0, result.output
        assert "Remote 'engaging' added" in result.output

        result = runner.invoke(app, ["remote", "list"])
        assert result.exit_code == 0, result.output
        assert "engaging" in result.output

    def test_add_invalid_url_errors(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_project_toml(tmp_path)

        result = runner.invoke(app, ["remote", "add", "engaging", "no-colon-here"])
        assert result.exit_code == 1, result.output

    def test_list_with_no_bth_toml_errors(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["remote", "list"])
        assert result.exit_code == 1, result.output

    def test_remove_unknown_remote_errors(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_project_toml(tmp_path)
        result = runner.invoke(app, ["remote", "remove", "nope"])
        assert result.exit_code == 1, result.output

    def test_remove_removes_configured_remote(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_project_toml(tmp_path)
        runner.invoke(app, ["remote", "add", "engaging", "engaging:~/x"])

        result = runner.invoke(app, ["remote", "remove", "engaging"])
        assert result.exit_code == 0, result.output
        assert "removed" in result.output

    def test_test_reports_success(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_project_toml(tmp_path)
        runner.invoke(app, ["remote", "add", "engaging", "engaging:~/x"])

        from bathos.remote import TestResult

        with patch(
            "bathos.remote.test_remote",
            return_value=TestResult(success=True, latency_ms=42.0, error=""),
        ):
            result = runner.invoke(app, ["remote", "test", "engaging"])
        assert result.exit_code == 0, result.output
        assert "ok" in result.output

    def test_test_reports_unreachable_as_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_project_toml(tmp_path)
        runner.invoke(app, ["remote", "add", "engaging", "engaging:~/x"])

        from bathos.remote import TestResult

        with patch(
            "bathos.remote.test_remote",
            return_value=TestResult(success=False, latency_ms=None, error="timed out"),
        ):
            result = runner.invoke(app, ["remote", "test", "engaging"])
        assert result.exit_code == 1, result.output
        assert "unreachable" in result.output


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _concluded_campaign(catalog: Path) -> str:
    from bathos.campaigns import add_run_to_campaign, create_campaign
    from bathos.compact import (
        _CAMPAIGN_RUNS_TABLE_SCHEMA,
        _CAMPAIGNS_TABLE_SCHEMA,
        _RUNS_TABLE_SCHEMA,
    )

    init_catalog(catalog)
    db = duckdb.connect(str(catalog / "bathos.db"))
    try:
        db.execute(_RUNS_TABLE_SCHEMA)
        db.execute(_CAMPAIGNS_TABLE_SCHEMA)
        db.execute(_CAMPAIGN_RUNS_TABLE_SCHEMA)

        campaign = create_campaign(db, "testproj", "test-campaign", mode="exploration")
        campaign_id = campaign.id

        run = Run(
            project_slug="testproj",
            command="echo test",
            argv=["echo", "test"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            status="completed",
            exit_code=0,
        )
        db.execute(
            """
            INSERT INTO runs (
                id, project_slug, command, argv, git_hash, git_branch, git_dirty, timestamp,
                status, exit_code, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run.id,
                run.project_slug,
                run.command,
                json.dumps(run.argv),
                run.git_hash,
                run.git_branch,
                run.git_dirty,
                run.timestamp,
                run.status,
                run.exit_code,
                "7",
            ],
        )
        add_run_to_campaign(db, campaign_id, run.id)
        db.execute(
            "UPDATE campaigns SET conclusion = ?, status = 'concluded' WHERE id = ?",
            ["Test passed", campaign_id],
        )
    finally:
        db.close()
    return campaign_id


class TestReportCyclopts:
    def test_emit_then_show_and_show_manifest(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        catalog = tmp_path / ".bth" / "catalog"
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
        monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
        _write_project_toml(tmp_path)
        campaign_id = _concluded_campaign(catalog)

        result = runner.invoke(app, ["report", "emit", campaign_id])
        assert result.exit_code == 0, result.output
        report_path = catalog / "sidecars" / campaign_id / "campaign_report.json"
        manifest_path = catalog / "sidecars" / campaign_id / "figure_manifest.json"
        assert report_path.exists()
        assert manifest_path.exists()

        show_result = runner.invoke(app, ["report", "show", campaign_id])
        assert show_result.exit_code == 0, show_result.output
        assert campaign_id in show_result.output

        manifest_result = runner.invoke(app, ["report", "show-manifest", campaign_id])
        assert manifest_result.exit_code == 0, manifest_result.output

    def test_emit_on_unconcluded_campaign_errors(self, tmp_path, monkeypatch):
        from bathos.campaigns import create_campaign

        monkeypatch.chdir(tmp_path)
        catalog = tmp_path / ".bth" / "catalog"
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
        monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
        _write_project_toml(tmp_path)
        init_catalog(catalog)

        from bathos.compact import _CAMPAIGNS_TABLE_SCHEMA

        db = duckdb.connect(str(catalog / "bathos.db"))
        try:
            db.execute(_CAMPAIGNS_TABLE_SCHEMA)
            campaign = create_campaign(db, "testproj", "open-campaign", mode="exploration")
        finally:
            db.close()

        result = runner.invoke(app, ["report", "emit", campaign.id])
        assert result.exit_code == 1, result.output

    def test_show_missing_sidecar_errors(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        catalog = tmp_path / ".bth" / "catalog"
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
        init_catalog(catalog)

        result = runner.invoke(app, ["report", "show", "nonexistent-campaign"])
        assert result.exit_code == 1, result.output


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


class TestProvenanceCyclopts:
    def test_show_reports_clean_pinned_run(self, repo, monkeypatch):
        from bathos.git_pin import pin_run

        monkeypatch.chdir(repo)
        head = _rev(repo, "HEAD")
        pin_run("run-abc", head, "main", dirty=False, cwd=repo)

        result = runner.invoke(app, ["provenance", "show", "run-abc"])
        assert result.exit_code == 0, result.output
        assert "run-abc" in result.output
        assert head in result.output

    def test_show_unknown_run_errors(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        result = runner.invoke(app, ["provenance", "show", "no-such-run"])
        assert result.exit_code == 1, result.output

    def test_diff_clean_tree_reports_exact(self, repo, monkeypatch):
        from bathos.git_pin import pin_run

        monkeypatch.chdir(repo)
        head = _rev(repo, "HEAD")
        pin_run("run-clean", head, "main", dirty=False, cwd=repo)

        result = runner.invoke(app, ["provenance", "diff", "run-clean"])
        assert result.exit_code == 0, result.output
        assert "clean tree" in result.output

    def test_diff_unknown_run_errors(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        result = runner.invoke(app, ["provenance", "diff", "no-such-run"])
        assert result.exit_code == 1, result.output

    def test_import_with_no_bundles_reports_none_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["provenance", "import"])
        assert result.exit_code == 0, result.output
        assert "no provenance bundles found" in result.output


# ---------------------------------------------------------------------------
# blast-radius hooks + query shadow-log
# ---------------------------------------------------------------------------


class TestBlastRadiusHooksAndShadowLogCyclopts:
    """Ported from tests/test_blast_radius_cli.py::TestBlastRadiusHookCmds."""

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
        from bathos.catalog import write_run

        pre_sha = _rev(repo, "HEAD")
        run = Run(
            project_slug="p",
            command="foo.py",
            argv=["foo.py"],
            git_hash=pre_sha,
            git_branch="main",
            git_dirty=False,
        )
        write_run(run, catalog_dir)

        (repo / "foo.py").write_text("a = 2\n")
        _git(["add", "foo.py"], repo)
        _git(["commit", "-m", "fix bug"], repo)
        fix_sha = _rev(repo, "HEAD")

        monkeypatch.chdir(repo)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
        result = runner.invoke(app, ["blast-radius", "shadow-check", fix_sha])
        assert result.exit_code == 0, result.output
        assert fold_blast_radius_state(catalog_dir, "shadow_trigger", fix_sha) == "shadow_only"

        log_result = runner.invoke(app, ["query", "shadow-log"])
        assert log_result.exit_code == 0, log_result.output
        assert fix_sha[:9] in log_result.output

    def test_shadow_check_no_parent_commit_does_not_error(self, repo, catalog_dir, monkeypatch):
        first_sha = _rev(repo, "HEAD")
        monkeypatch.chdir(repo)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
        result = runner.invoke(app, ["blast-radius", "shadow-check", first_sha])
        assert result.exit_code == 0, result.output

    def test_shadow_log_with_no_db_prints_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "empty-catalog"))
        result = runner.invoke(app, ["query", "shadow-log"])
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# top-level singletons with no MCP equivalent
# ---------------------------------------------------------------------------


class TestTopLevelCliOnlyCyclopts:
    def test_migrate_reports_nothing_to_migrate_on_empty_catalog(self, tmp_path, monkeypatch):
        catalog = tmp_path / "catalog"
        init_catalog(catalog)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))

        result = runner.invoke(app, ["migrate"])
        assert result.exit_code == 0, result.output
        assert "Nothing to migrate" in result.output

    def test_migrate_classify_errors_with_no_scripts_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["migrate", "--classify"])
        assert result.exit_code == 1, result.output

    def test_migrate_to_project_subdirs_reports_zero_moved(self, tmp_path, monkeypatch):
        catalog = tmp_path / "catalog"
        init_catalog(catalog)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))

        result = runner.invoke(app, ["migrate-to-project-subdirs"])
        assert result.exit_code == 0, result.output
        assert "0 run(s)" in result.output

    def test_classify_errors_with_no_scripts_dir(self, tmp_path):
        result = runner.invoke(app, ["classify", "--project", str(tmp_path)])
        assert result.exit_code == 1, result.output

    def test_classify_json_output_with_no_flat_scripts(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        result = runner.invoke(app, ["classify", "--project", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "No flat scripts found" in result.output

    def test_sprint_audit_reports_no_projects(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        result = runner.invoke(app, ["sprint-audit"])
        assert result.exit_code == 0, result.output

    def test_catalog_version_reports_schema(self, tmp_path, monkeypatch):
        catalog = tmp_path / "catalog"
        init_catalog(catalog)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))

        result = runner.invoke(app, ["catalog-version"])
        assert result.exit_code == 0, result.output
        assert "Current schema version" in result.output

    def test_catalog_version_reports_corrupt_fragment_warning(self, tmp_path, monkeypatch):
        """Regression (found while fixing PR #59 review finding #7):
        catalog_version_cmd dropped the corrupt-fragment WARNING entirely
        (not just its color) when the old Typer `catalog-version` command was
        ported to cyclopts -- restored, plain-text (uncolored, matching this
        batch's established JSON/plain-print direction)."""
        catalog = tmp_path / "catalog"
        init_catalog(catalog)
        runs_dir = catalog / "runs" / "proj"
        runs_dir.mkdir(parents=True)
        (runs_dir / "run_abc123.parquet").write_bytes(b"not parquet")
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))

        result = runner.invoke(app, ["catalog-version"])
        assert result.exit_code == 0, result.output
        assert "WARNING" in result.output
        assert "corrupt" in result.output.lower()

    def test_catalog_version_reports_warm_db_version(self, tmp_path, monkeypatch):
        """Regression (found while fixing PR #59 review finding #7):
        catalog_version_cmd dropped the entire "Warm DB version" reporting
        block (the _schema_migrations query) -- restored."""
        catalog = tmp_path / "catalog"
        init_catalog(catalog)
        write_run(
            Run(
                project_slug="proj",
                command="python run.py",
                argv=["python", "run.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                status="completed",
                exit_code=0,
            ),
            catalog,
        )
        compact(catalog)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))

        result = runner.invoke(app, ["catalog-version"])
        assert result.exit_code == 0, result.output
        assert "Warm DB version" in result.output

    def test_export_dry_run(self):
        result = runner.invoke(app, ["export", "--tool", "claude", "--level", "user", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "claude" in result.output.lower()
        assert "dry" in result.output.lower()

    def test_export_writes_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["export", "--tool", "claude", "--level", "workspace"])
        assert result.exit_code == 0, result.output
        target = tmp_path / ".claude" / "skills" / "using-bathos" / "SKILL.md"
        assert target.exists()

    def test_view_launches_server_with_expected_args(self, tmp_path, monkeypatch):
        pytest.importorskip("fastapi")
        catalog = tmp_path / "catalog"
        init_catalog(catalog)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))

        with patch("bathos.viz.server.run_server") as mock_run_server:
            result = runner.invoke(app, ["view", "--no-open", "--port", "9999"])
        assert result.exit_code == 0, result.output
        mock_run_server.assert_called_once()
        _, kwargs = mock_run_server.call_args
        assert kwargs["port"] == 9999
        assert kwargs["open_browser"] is False

    def test_submit_happy_path_no_wait(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
        (tmp_path / ".bth.toml").write_text(
            "[project]\n"
            'slug = "myproject"\n'
            f'root = "{tmp_path}"\n'
            "\n"
            "[slurm]\n"
            'remote = "engaging"\n'
            'preset = "gpu"\n'
        )

        submit_result = {
            "slurm_job_id": "12345",
            "script_path": "/tmp/x.sh",
            "preset_used": {},
            "job_name": "bth-submit",
        }
        with (
            patch("bathos.cluster.push_project") as mock_push,
            patch("bathos.cluster.submit_job", return_value=submit_result) as mock_submit,
        ):
            result = runner.invoke(
                app, ["submit", "--no-wait", "uv", "run", "python", "train.py"]
            )

        assert result.exit_code == 0, result.output
        assert "Submitted 12345 on engaging using preset gpu" in result.output
        mock_push.assert_called_once()
        mock_submit.assert_called_once()

    def test_submit_no_bth_toml_errors(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
        result = runner.invoke(app, ["submit", "--no-wait", "echo", "hi"])
        assert result.exit_code == 1, result.output
