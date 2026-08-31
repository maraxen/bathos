"""CLI-level smoke tests for the top-level singleton command batch
(backlog #4702 Milestone 2), exercised through the preview cyclopts app
(`bathos.cli_cyclopts.app`, `bth-preview`) via the `CyclopticRunner` shim.

None of these 19 commands had CLI-level tests before this file (they were
only reachable via the shipped `bth` typer CLI, tested in `test_cli.py`,
which stays untouched and green). One happy-path + (where a natural error
case exists) one error-path per command, matching the campaign pilot's
depth — exhaustive edge-case coverage already lives at the Python-API layer
(`test_*.py` files exercising the underlying `bathos.*` modules directly)
and is untouched by this migration.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from bathos.catalog import init_catalog, write_run
from bathos.cli_cyclopts import app
from bathos.compact import compact
from bathos.schema import Run
from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()


@pytest.fixture
def populated_catalog(tmp_catalog: Path) -> Path:
    """A catalog with one real run, for commands that need an existing run_id."""
    init_catalog(tmp_catalog)
    r = Run(
        project_slug="prolix",
        command="python run_0.py",
        argv=["python", "run_0.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC),
        status="completed",
        exit_code=0,
    )
    write_run(r, tmp_catalog)
    compact(tmp_catalog)
    return tmp_catalog


def _first_run_id(catalog_dir: Path) -> str:
    db = duckdb.connect(str(catalog_dir / "bathos.db"))
    try:
        return db.execute("SELECT id FROM runs LIMIT 1").fetchone()[0]
    finally:
        db.close()


class TestLsCyclopts:
    def test_happy_path(self, populated_catalog):
        result = runner.invoke(app, ["ls", "--catalog-dir", str(populated_catalog)])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["count"] >= 1


class TestFindCyclopts:
    def test_happy_path(self, populated_catalog):
        result = runner.invoke(app, ["find", "--catalog-dir", str(populated_catalog)])
        assert result.exit_code == 0, result.output


class TestShowCyclopts:
    def test_happy_path(self, populated_catalog):
        run_id = _first_run_id(populated_catalog)
        result = runner.invoke(
            app,
            ["show", "--catalog-dir", str(populated_catalog), "--run-id", run_id],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["id"] == run_id

    def test_not_found_is_clean_error(self, populated_catalog):
        result = runner.invoke(
            app,
            [
                "show",
                "--catalog-dir",
                str(populated_catalog),
                "--run-id",
                "does-not-exist",
            ],
        )
        assert result.exit_code == 1


class TestCiteCyclopts:
    def test_happy_path(self, populated_catalog):
        run_id = _first_run_id(populated_catalog)
        result = runner.invoke(
            app, ["cite", run_id, "--catalog-dir", str(populated_catalog)]
        )
        assert result.exit_code == 0, result.output
        assert "citation" in result.output

    def test_not_found_is_clean_error(self, populated_catalog):
        result = runner.invoke(
            app, ["cite", "does-not-exist", "--catalog-dir", str(populated_catalog)]
        )
        assert result.exit_code == 1
        assert "not found" in result.output.lower() or "error" in result.output.lower()


class TestLineageCyclopts:
    def test_happy_path(self, populated_catalog):
        run_id = _first_run_id(populated_catalog)
        result = runner.invoke(
            app, ["lineage", run_id, "--catalog-dir", str(populated_catalog)]
        )
        # A run with no derived_from ancestry still returns depth-0 PROV-JSON
        # for itself (empty wasDerivedFrom, not a missing-lineage error).
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "prov" in payload

    def test_not_found_is_clean_error(self, populated_catalog):
        result = runner.invoke(
            app, ["lineage", "does-not-exist", "--catalog-dir", str(populated_catalog)]
        )
        assert result.exit_code == 1


class TestSqlCyclopts:
    def test_happy_path(self, populated_catalog):
        result = runner.invoke(
            app,
            [
                "sql",
                "--catalog-dir",
                str(populated_catalog),
                "--sql",
                "SELECT COUNT(*) AS n FROM runs",
            ],
        )
        assert result.exit_code == 0, result.output


class TestVerifyCyclopts:
    def test_happy_path(self, populated_catalog):
        result = runner.invoke(app, ["verify", "--catalog-dir", str(populated_catalog)])
        assert result.exit_code == 0, result.output


class TestCheckCyclopts:
    def test_stale_run_exits_1(self, populated_catalog):
        """`populated_catalog`'s run carries a fake git_hash ("abc123") and runs
        outside any real git repo, so it's genuinely STALE under check_runs'
        real drift logic -- not a happy path. Regression: check_tool used to
        omit a singular "error" key entirely, so cli_render.render_or_exit
        never triggered exit(1) even with real drift detected (same bug class
        as lint_tool/compact_tool, found during the final cutover, backlog
        #4702) -- this test previously asserted exit_code == 0 against this
        same stale fixture, silently passing only because the bug masked it."""
        result = runner.invoke(app, ["check", "--catalog-dir", str(populated_catalog)])
        assert result.exit_code == 1, result.output
        assert "stale" in result.output.lower()

    def test_clean_catalog_exits_0(self, tmp_catalog):
        result = runner.invoke(app, ["check", "--catalog-dir", str(tmp_catalog)])
        assert result.exit_code == 0, result.output


class TestCompactCyclopts:
    def test_happy_path(self, tmp_catalog):
        init_catalog(tmp_catalog)
        result = runner.invoke(app, ["compact", "--catalog-dir", str(tmp_catalog)])
        assert result.exit_code == 0, result.output


class TestInitCyclopts:
    def test_happy_path(self, tmp_path):
        project_root = tmp_path / "new-project"
        project_root.mkdir()
        result = runner.invoke(
            app,
            [
                "init",
                "--project-root",
                str(project_root),
                "--catalog-dir",
                str(tmp_path / "catalog"),
                "--slug",
                "smoke-test-project",
            ],
        )
        assert result.exit_code == 0, result.output


class TestRepairCyclopts:
    def test_happy_path_dry_run(self, populated_catalog):
        result = runner.invoke(
            app, ["repair", "--catalog-dir", str(populated_catalog), "--dry-run"]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["dry_run"] is True


class TestNewExperimentCyclopts:
    def test_happy_path(self, tmp_path):
        result = runner.invoke(
            app,
            [
                "new-experiment",
                "smoke_test_experiment",
                "--workspace-root",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Scaffolded" in result.output

    def test_missing_name_is_clean_error(self):
        result = runner.invoke(app, ["new-experiment"])
        assert result.exit_code == 1


class TestValidateSidecarCyclopts:
    def test_not_found_is_clean_error(self, tmp_path):
        missing = tmp_path / "no_such.bth.toml"
        result = runner.invoke(app, ["validate-sidecar", str(missing)])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()


class TestArchiveCyclopts:
    def test_happy_path(self, populated_catalog):
        result = runner.invoke(
            app,
            ["archive", "--catalog-dir", str(populated_catalog), "--project", "prolix"],
        )
        assert result.exit_code == 0, result.output


class TestRestoreCyclopts:
    def test_missing_item_is_clean_error(self, populated_catalog):
        result = runner.invoke(
            app,
            [
                "restore",
                "--item-id",
                "does-not-exist",
                "--catalog-dir",
                str(populated_catalog),
            ],
        )
        assert result.exit_code == 1


class TestArchiveArtifactCyclopts:
    def test_missing_script_is_clean_error(self, tmp_path, populated_catalog):
        result = runner.invoke(
            app,
            [
                "archive-artifact",
                str(tmp_path / "does_not_exist.py"),
                "--catalog-dir",
                str(populated_catalog),
            ],
        )
        assert result.exit_code == 1


class TestSyncCyclopts:
    def test_no_remote_is_clean_error(self, populated_catalog):
        result = runner.invoke(
            app,
            [
                "sync",
                "--catalog-dir",
                str(populated_catalog),
                "--remote-name",
                "does-not-exist",
            ],
        )
        assert result.exit_code == 1


class TestRunCyclopts:
    def test_missing_script_propagates_subprocess_exit_code(self, populated_catalog):
        """A missing script is tracked as run provenance ({"success": false,
        "exit_code": ...}) AND its exit_code propagates to the `bth run`
        process's own exit code -- matching the shipped Typer `run` command's
        `raise typer.Exit(exit_code)`. Regression note (final cutover, backlog
        #4702): this test previously asserted exit_code == 0 here, describing
        that as "not a CLI error, matching the shipped bth run's own
        behavior" -- that was backwards. run_tool never propagated the
        script's real exit_code to the CLI process at all (a bug, not a
        design choice); run_cli_tool (mcp.py) now does, restoring parity with
        the original Typer command."""
        result = runner.invoke(
            app,
            [
                "run",
                "--script-path",
                "/no/such/script.py",
                "--catalog-dir",
                str(populated_catalog),
            ],
        )
        payload = json.loads(result.output)
        assert payload["success"] is False
        assert result.exit_code == payload["exit_code"]
        assert result.exit_code != 0

    def test_extra_args_passthrough(self, tmp_path, monkeypatch):
        """Regression (final cutover, backlog #4702): the old Typer `run`
        command used context_settings={"allow_extra_args": True,
        "ignore_unknown_options": True} to forward arbitrary flags to the
        wrapped script; run_cli_tool (mcp.py) lost this, so any unrecognized
        leading-hyphen token (e.g. --lr 0.01) exited 1 with "Unknown option"
        instead of reaching the script."""
        monkeypatch.chdir(tmp_path)
        catalog = tmp_path / ".bth" / "catalog"
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
        monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
        record = tmp_path / "argv.json"
        probe = tmp_path / "probe.py"
        probe.write_text(
            "import json, sys\n" f"json.dump(sys.argv[1:], open(r'{record}', 'w'))\n"
        )

        result = runner.invoke(app, ["run", str(probe), "--lr", "0.01", "--foo=bar"])

        assert result.exit_code == 0, result.output
        assert json.loads(record.read_text()) == ["--lr", "0.01", "--foo=bar"]

    def test_missing_project_slug_hard_fails(self, tmp_path, monkeypatch):
        """Regression (final cutover, backlog #4702): run_tool (mcp.py) fell
        back to _get_project_slug's silent project_slug="default" instead of
        hard-failing via cli_common.require_project_slug(), matching the
        pre-cutover Typer `run` command's _require_project_slug() and
        soft_project_slug()'s own docstring, which documents `run` as
        requiring a configured slug."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("BTH_PROJECT_SLUG", raising=False)
        probe = tmp_path / "probe.py"
        probe.write_text("pass\n")

        result = runner.invoke(app, ["run", str(probe)])

        assert result.exit_code == 1
        assert "No .bth.toml found" in result.output


class TestLintCyclopts:
    def test_happy_path(self, tmp_path):
        result = runner.invoke(app, ["lint", "--project-root", str(tmp_path)])
        assert result.exit_code == 0, result.output

    def test_naming_error_exits_1(self, tmp_path):
        """Regression: lint_tool used to omit a singular "error" key entirely, so
        cli_render.render_or_exit never triggered exit(1) even with real lint
        errors present -- found migrating test_linter.py's CLI tests onto
        cyclopts during the final cutover (backlog #4702)."""
        d = tmp_path / "scripts" / "experiments"
        d.mkdir(parents=True)
        (d / "BadName.py").write_text("# script")

        result = runner.invoke(app, ["lint", "--project-root", str(tmp_path)])
        assert result.exit_code == 1, result.output
