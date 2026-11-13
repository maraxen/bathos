import json
import subprocess
from pathlib import Path

import pytest

from bathos.catalog import init_catalog, write_run
from bathos.cli_common import catalog_dir as _catalog_dir
from bathos.cli_cyclopts import app
from bathos.mcp import _get_catalog_dir
from bathos.schema import Run
from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()


def _write_script(tmp_path: Path, name: str, body: str) -> Path:
    """Write a real .py script -- run_tool (mcp.py) executes script_path via
    THIS process's interpreter, unlike the retired Typer `run` command, which
    took a raw argv and executed it verbatim (e.g. `bth run <python> -- -c
    <code>`). See test_integration.py's test_full_workflow for the same fix."""
    script = tmp_path / name
    script.write_text(body)
    return script


def test_version():
    # cyclopts' built-in --version prints the bare package version (no "bathos"
    # prefix, unlike the retired Typer callback's `typer.echo(f"bathos {__version__}")`).
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    from bathos import __version__

    assert __version__ in result.output


def test_init_creates_dirs(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / ".bth" / "catalog"))
    result = runner.invoke(app, ["init", "--slug", "testproj"])
    assert result.exit_code == 0
    assert (tmp_path / "scripts" / "experiments").is_dir()
    assert (tmp_path / ".bth.toml").exists()


def test_run_records_run(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')
    result = runner.invoke(app, ["run", str(_write_script(tmp_path, "noop.py", "pass\n"))])
    assert result.exit_code == 0


def test_ls_shows_runs(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')
    runner.invoke(app, ["run", str(_write_script(tmp_path, "noop.py", "pass\n"))])
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    # Rich table renders; check table title or known column header
    assert "Runs" in result.output or "testpr" in result.output


def test_show_displays_run_detail(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')
    runner.invoke(app, ["run", str(_write_script(tmp_path, "noop.py", "pass\n"))])
    from bathos.catalog import init_catalog, read_runs

    init_catalog(catalog)
    runs = read_runs(catalog)
    result = runner.invoke(app, ["show", runs[0].id])
    assert result.exit_code == 0
    assert runs[0].id in result.output


def test_compact_command_runs(tmp_path: Path, monkeypatch):
    """Test that bth compact command executes successfully and produces summary output."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')
    # Create a few runs
    runner.invoke(app, ["run", str(_write_script(tmp_path, "noop.py", "pass\n"))])
    runner.invoke(app, ["run", str(_write_script(tmp_path, "noop.py", "pass\n"))])

    result = runner.invoke(app, ["compact"])
    assert result.exit_code == 0
    assert json.loads(result.output)["ingested"] == 2
    # Verify warm DB was created
    assert (catalog / "bathos.db").exists()


def test_ls_shows_compact_banner_at_threshold(tmp_path: Path, monkeypatch):
    """Test that ls shows banner when fragment count > 50 and no warm DB."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    # Create 51 fragments by creating 51 runs
    for _ in range(51):
        runner.invoke(app, ["run", str(_write_script(tmp_path, "noop.py", "pass\n"))])

    # Verify no warm DB exists
    assert not (catalog / "bathos.db").exists()

    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    # list_runs_tool (registry-driven, JSON output) doesn't print the retired
    # Typer `ls` command's rich "uncompacted runs" banner -- check the
    # underlying recommendation directly instead.
    from bathos.compact import should_compact

    assert should_compact(catalog)


def test_ls_no_banner_below_threshold(tmp_path: Path, monkeypatch):
    """Test that ls does not show banner when fragment count <= 50."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    # Create 10 runs (< 50 threshold)
    for _ in range(10):
        runner.invoke(app, ["run", str(_write_script(tmp_path, "noop.py", "pass\n"))])

    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    from bathos.compact import should_compact

    assert not should_compact(catalog)


def test_ls_no_banner_when_warm_db_exists(tmp_path: Path, monkeypatch):
    """Test that ls does not show banner when warm DB already exists.

    Note: We verify the decision logic directly since find_runs warm tier
    is not yet implemented (Task A3). Once warm tier is available, this test
    can be expanded to actually invoke ls and check the output.
    """
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    # Create 51 runs
    for _ in range(51):
        runner.invoke(app, ["run", str(_write_script(tmp_path, "noop.py", "pass\n"))])

    # Compact to create warm DB
    runner.invoke(app, ["compact"])
    assert (catalog / "bathos.db").exists()

    # Verify the should_compact decision logic
    # If warm DB exists and fragment count > 50, should_compact returns False (no banner)
    from bathos.compact import _fragment_count, should_compact

    frag_count = _fragment_count(catalog)
    assert frag_count > 50  # We have 51 fragments
    assert (catalog / "bathos.db").exists()  # We compacted
    assert not should_compact(catalog)  # Banner should NOT appear


def test_sql_error_without_warm_db(tmp_path: Path, monkeypatch):
    """Test that bth sql raises clear error when warm DB is missing and query needs it."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    # Create a run to have Parquet files
    runner.invoke(app, ["run", str(_write_script(tmp_path, "noop.py", "pass\n"))])

    # Verify no warm DB exists
    assert not (catalog / "bathos.db").exists()

    # Try to query a table that requires warm DB (the runs table from compact)
    result = runner.invoke(app, ["sql", "SELECT COUNT(*) FROM runs"])
    assert result.exit_code != 0
    # Should show clear error message
    assert "No warm catalog" in result.output or "bth compact" in result.output


def test_sql_allows_arbitrary_queries(tmp_path: Path, monkeypatch):
    """Test that bth sql allows arbitrary queries (e.g., read_parquet) without warm DB."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    # Create a run to have Parquet files
    runner.invoke(app, ["run", str(_write_script(tmp_path, "noop.py", "pass\n"))])

    # Query using read_parquet should work without warm DB (runs are in per-project subdir)
    glob = str(catalog / "runs" / "testproj" / "run_*.parquet")
    result = runner.invoke(app, ["sql", f"SELECT COUNT(*) FROM read_parquet('{glob}')"])
    assert result.exit_code == 0
    assert "1" in result.output


def test_check_command_detects_stale_runs(tmp_path: Path, monkeypatch):
    """Test that bth check detects stale runs and exits with code 1."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "file.txt").write_text("content")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # Create a run with an old hash
    init_catalog(catalog)
    stale_run = Run(
        project_slug="testproj",
        command="python test.py",
        argv=["python", "test.py"],
        git_hash="olddeadbeef0000",
        git_branch="main",
        git_dirty=False,
    )
    write_run(stale_run, catalog)

    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    # check_tool signals failure via a singular "error" key (cli_render.render_or_exit),
    # which prints only the summary line, not the full results array -- same
    # convention as every other exit-1 registry-driven command this migration.
    assert "stale" in result.output.lower()


def test_check_command_shows_ok_runs(tmp_path: Path, monkeypatch):
    """Test that bth check shows OK status for runs at current HEAD."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "file.txt").write_text("content")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    current_hash = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()

    # Create a run with current hash
    init_catalog(catalog)
    ok_run = Run(
        project_slug="testproj",
        command="python test.py",
        argv=["python", "test.py"],
        git_hash=current_hash,
        git_branch="main",
        git_dirty=False,
    )
    write_run(ok_run, catalog)

    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0
    assert "OK" in result.output


def test_check_command_filters_by_status(tmp_path: Path, monkeypatch):
    """Test that bth check --status filter works."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "file.txt").write_text("content")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # Create both stale and dirty runs
    init_catalog(catalog)
    stale_run = Run(
        project_slug="testproj",
        command="python test1.py",
        argv=["python", "test1.py"],
        git_hash="olddeadbeef0000",
        git_branch="main",
        git_dirty=False,
    )
    dirty_run = Run(
        project_slug="testproj",
        command="python test2.py",
        argv=["python", "test2.py"],
        git_hash="anyhash",
        git_branch="main",
        git_dirty=True,
    )
    write_run(stale_run, catalog)
    write_run(dirty_run, catalog)

    # Filter by STALE (check_tool's CLI flag is --status-filter, not --status --
    # a cosmetic flag-name difference from the retired Typer command, accepted
    # during the final cutover). Exits 1 via the singular "error" key, which
    # prints only the summary line, not the itemized results.
    result = runner.invoke(app, ["check", "--status-filter", "STALE"])
    assert result.exit_code == 1
    assert "stale" in result.output.lower()

    # Filter by DIRTY_RUN
    result = runner.invoke(app, ["check", "--status-filter", "DIRTY_RUN"])
    assert result.exit_code == 0
    assert "DIRTY_RUN" in result.output
    assert "STALE" not in result.output


def test_archive_command_runs(tmp_path: Path, monkeypatch):
    """Verify bth archive command invokes archive() and reports success."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    # Create and compact a run
    runner.invoke(app, ["run", str(_write_script(tmp_path, "noop.py", "pass\n"))])
    runner.invoke(app, ["compact"])

    # Run archive command
    archive_dir = tmp_path / "archive"
    result = runner.invoke(app, ["archive", "--archive-dir", str(archive_dir)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["runs_archived"] == 1
    assert payload["partitions_created"] == 1


def test_archive_command_dry_run(tmp_path: Path, monkeypatch):
    """Verify --dry-run flag prevents writing."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    # Create and compact a run
    runner.invoke(app, ["run", str(_write_script(tmp_path, "noop.py", "pass\n"))])
    runner.invoke(app, ["compact"])

    # Run archive with --dry-run
    archive_dir = tmp_path / "archive"
    result = runner.invoke(app, ["archive", "--archive-dir", str(archive_dir), "--dry-run"])

    assert result.exit_code == 0, result.output
    # Archive directory should not be created in dry-run
    assert not archive_dir.exists()


def test_archive_command_without_warm_db(tmp_path: Path, monkeypatch):
    """Verify command errors clearly if warm DB missing."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    # Create a run but don't compact (so no warm DB)
    runner.invoke(app, ["run", str(_write_script(tmp_path, "noop.py", "pass\n"))])

    # Try archive without warm DB
    result = runner.invoke(app, ["archive"])

    assert result.exit_code != 0
    assert "No warm catalog" in result.output or "bth compact" in result.output


def test_archive_command_with_project_filter(tmp_path: Path, monkeypatch):
    """Verify --project filter works correctly."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "proj1"\nroot = "{tmp_path}"\n')

    # Create runs for two projects
    monkeypatch.setenv("BTH_PROJECT_SLUG", "proj1")
    runner.invoke(app, ["run", str(_write_script(tmp_path, "noop.py", "pass\n"))])
    runner.invoke(app, ["run", str(_write_script(tmp_path, "noop.py", "pass\n"))])

    monkeypatch.setenv("BTH_PROJECT_SLUG", "proj2")
    runner.invoke(app, ["run", str(_write_script(tmp_path, "noop.py", "pass\n"))])

    # Compact
    runner.invoke(app, ["compact"])

    # Archive only proj1
    archive_dir = tmp_path / "archive"
    result = runner.invoke(
        app, ["archive", "--project", "proj1", "--archive-dir", str(archive_dir)]
    )

    assert result.exit_code == 0, result.output
    # Should archive 2 runs (only from proj1)
    assert json.loads(result.output)["runs_archived"] == 2


def test_find_command_output_file_filter(tmp_path: Path, monkeypatch):
    """Verify bth find --output-file filter works."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    # Create and write runs with different output files
    init_catalog(catalog)
    run1 = Run(
        id="run1",
        project_slug="test",
        command="test1",
        argv=["test1"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        output_paths=["/tmp/analysis.json"],
    )
    run2 = Run(
        id="run2",
        project_slug="test",
        command="test2",
        argv=["test2"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        output_paths=["/tmp/report.csv"],
    )
    write_run(run1, catalog)
    write_run(run2, catalog)

    # Find with pattern
    result = runner.invoke(app, ["find", "--output-file", "*.json"])

    assert result.exit_code == 0
    # Should only show run1 with json output file
    assert "run1" in result.output
    assert "test1" in result.output
    # run2 should not appear
    assert "run2" not in result.output


def test_check_command_with_check_outputs(tmp_path: Path, monkeypatch):
    """Verify bth check --check-outputs verifies output files."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "file.txt").write_text("content")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    current_hash = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()

    # Create an output file
    output_file = tmp_path / "output.json"
    output_file.write_text('{"data": 123}')

    # Write a run with output file
    init_catalog(catalog)
    run = Run(
        id="test-run",
        project_slug="test",
        command="test",
        argv=["test"],
        git_hash=current_hash,
        git_branch="main",
        git_dirty=False,
        output_paths=[str(output_file)],
    )
    write_run(run, catalog)

    # Run check with output verification
    result = runner.invoke(app, ["check", "--check-outputs"])

    assert result.exit_code == 0, result.output
    assert "present" in result.output
    assert str(output_file) in result.output


def test_export_dry_run_claude_user(monkeypatch):  # noqa: ARG001 - pytest fixture
    """bth export --tool claude --level user --dry-run prints target path without writing."""
    result = runner.invoke(app, ["export", "--tool", "claude", "--level", "user", "--dry-run"])
    assert result.exit_code == 0
    assert "claude" in result.output.lower()
    assert "dry-run" in result.output.lower() or "dry run" in result.output.lower()


def test_export_writes_file(tmp_path, monkeypatch):
    """bth export --tool claude --level workspace writes skill to .claude/skills/."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["export", "--tool", "claude", "--level", "workspace"])
    assert result.exit_code == 0
    target = tmp_path / ".claude" / "skills" / "using-bathos" / "SKILL.md"
    assert target.exists()


def test_ls_shows_outcome_column(tmp_path, monkeypatch):
    """ls output includes OUTCOME column header."""
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "proj")
    import duckdb

    from bathos.catalog import write_run
    from bathos.compact import compact
    from bathos.schema import Run

    r = Run(
        project_slug="proj",
        command="echo hi",
        argv=["echo", "hi"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
    )
    write_run(r, tmp_path)
    compact(tmp_path)

    con = duckdb.connect(str(tmp_path / "bathos.db"))
    con.execute(f"UPDATE runs SET outcome = 'pass' WHERE id = '{r.id}'")
    con.close()

    result = runner.invoke(app, ["ls"])
    # Rich table uses title-case column headers
    assert "Outcome" in result.output or "pass" in result.output


def test_catalog_dir_reads_project_config(tmp_path: Path, monkeypatch):
    """_catalog_dir() must honor catalog_dir from .bth.toml, not just fall back to ~/.bth/catalog."""
    custom_catalog = tmp_path / "custom_catalog"
    cfg = tmp_path / ".bth.toml"
    cfg.write_text(
        f'[project]\nslug = "myproj"\nroot = "{tmp_path}"\ncatalog_dir = "{custom_catalog}"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BTH_CATALOG_DIR", raising=False)
    assert _catalog_dir() == custom_catalog


def test_catalog_dir_env_var_takes_precedence(tmp_path: Path, monkeypatch):
    """BTH_CATALOG_DIR env var overrides even a config-specified catalog_dir."""
    env_catalog = tmp_path / "env_catalog"
    custom_catalog = tmp_path / "custom_catalog"
    cfg = tmp_path / ".bth.toml"
    cfg.write_text(
        f'[project]\nslug = "myproj"\nroot = "{tmp_path}"\ncatalog_dir = "{custom_catalog}"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(env_catalog))
    assert _catalog_dir() == env_catalog


def test_catalog_dir_falls_back_to_default_when_no_config(tmp_path: Path, monkeypatch):
    """With no .bth.toml and no env var, _catalog_dir() returns the default ~/.bth/catalog."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BTH_CATALOG_DIR", raising=False)
    assert _catalog_dir() == Path.home() / ".bth" / "catalog"


def test_mcp_get_catalog_dir_reads_project_config(tmp_path: Path, monkeypatch):
    """Regression (PR #59 review): mcp.py's _get_catalog_dir -- the shared
    helper behind every registry-driven CLI/MCP tool -- must honor a
    .bth.toml catalog_dir override too, matching cli_common.catalog_dir()."""
    custom_catalog = tmp_path / "custom_catalog"
    cfg = tmp_path / ".bth.toml"
    cfg.write_text(
        f'[project]\nslug = "myproj"\nroot = "{tmp_path}"\ncatalog_dir = "{custom_catalog}"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BTH_CATALOG_DIR", raising=False)
    assert _get_catalog_dir(None) == custom_catalog


def test_report_emit_cli_smoke_test(tmp_path: Path, monkeypatch):
    """CLI-level test: bth report emit on a concluded campaign creates both sidecar files.

    Validates the complete `bth report emit <campaign_id>` flow:
    - Creates a tmp catalog via BTH_CATALOG_DIR with DuckDB schema
    - Creates and concludes a campaign
    - Invokes `runner.invoke(app, ['report', 'emit', campaign_id])`
    - Asserts exit code == 0
    - Asserts both sidecar files exist at the pinned path
    """
    import json

    import duckdb

    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    # Initialize catalog with DuckDB schema
    from bathos.campaigns import add_run_to_campaign, create_campaign
    from bathos.catalog import init_catalog
    from bathos.compact import (
        _CAMPAIGN_RUNS_TABLE_SCHEMA,
        _CAMPAIGNS_TABLE_SCHEMA,
        _RUNS_TABLE_SCHEMA,
    )

    init_catalog(catalog)
    db_path = catalog / "bathos.db"
    db = duckdb.connect(str(db_path))
    try:
        # Initialize schema tables
        db.execute(_RUNS_TABLE_SCHEMA)
        db.execute(_CAMPAIGNS_TABLE_SCHEMA)
        db.execute(_CAMPAIGN_RUNS_TABLE_SCHEMA)

        campaign = create_campaign(
            db,
            "testproj",
            "test-campaign",
            mode="exploration",
        )
        campaign_id = campaign.id

        # Create and add a run using SQL (since create_campaign and campaigns functions expect DB schema)
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

        # Conclude the campaign
        db.execute(
            "UPDATE campaigns SET conclusion = ?, status = 'concluded' WHERE id = ?",
            ["Test passed", campaign_id],
        )
    finally:
        db.close()

    # Now invoke bth report emit via CLI
    result = runner.invoke(app, ["report", "emit", campaign_id])

    # Assertions
    assert result.exit_code == 0, (
        f"Expected exit code 0, got {result.exit_code}. Output: {result.output}"
    )

    # Both sidecar files must exist at the pinned path
    report_path = catalog / "sidecars" / campaign_id / "campaign_report.json"
    manifest_path = catalog / "sidecars" / campaign_id / "figure_manifest.json"

    assert report_path.exists(), f"Campaign report not found at {report_path}"
    assert manifest_path.exists(), f"Figure manifest not found at {manifest_path}"


def test_report_emit_idempotency(tmp_path: Path, monkeypatch):
    """Test that calling emit_campaign_report and emit_figure_manifest twice is idempotent.

    Verifies that a second run overwrites (not appends) the files, and content is stable.
    """
    import json

    import duckdb

    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    # Initialize catalog and create a concluded campaign
    from bathos.campaigns import (
        add_run_to_campaign,
        create_campaign,
        emit_campaign_report,
        emit_figure_manifest,
    )
    from bathos.catalog import init_catalog
    from bathos.compact import (
        _CAMPAIGN_RUNS_TABLE_SCHEMA,
        _CAMPAIGNS_TABLE_SCHEMA,
        _RUNS_TABLE_SCHEMA,
    )

    init_catalog(catalog)
    db_path = catalog / "bathos.db"
    db = duckdb.connect(str(db_path))
    try:
        # Initialize schema tables
        db.execute(_RUNS_TABLE_SCHEMA)
        db.execute(_CAMPAIGNS_TABLE_SCHEMA)
        db.execute(_CAMPAIGN_RUNS_TABLE_SCHEMA)

        campaign = create_campaign(
            db,
            "testproj",
            "test-campaign",
            mode="exploration",
        )
        campaign_id = campaign.id

        # Create and add a run
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

        # Conclude the campaign
        db.execute(
            "UPDATE campaigns SET conclusion = ?, status = 'concluded' WHERE id = ?",
            ["Test passed", campaign_id],
        )

        # First emission
        manifest_ref = f"sidecars/{campaign_id}/figure_manifest.json"
        emit_figure_manifest(db, str(catalog), campaign_id)
        emit_campaign_report(db, str(catalog), campaign_id, figure_manifest_ref=manifest_ref)

        sidecar_dir = catalog / "sidecars" / campaign_id
        assert sidecar_dir.is_dir()
        file_count_first = len(list(sidecar_dir.iterdir()))

        # Read first emission content
        report_path = sidecar_dir / "campaign_report.json"
        manifest_path = sidecar_dir / "figure_manifest.json"
        report_content_first = report_path.read_text()
        manifest_content_first = manifest_path.read_text()

        # Second emission (should overwrite, not append)
        emit_figure_manifest(db, str(catalog), campaign_id)
        emit_campaign_report(db, str(catalog), campaign_id, figure_manifest_ref=manifest_ref)

        file_count_second = len(list(sidecar_dir.iterdir()))
        report_content_second = report_path.read_text()
        manifest_content_second = manifest_path.read_text()

        # Assertions
        assert file_count_first == file_count_second == 2, (
            f"Expected 2 files (not 4). First: {file_count_first}, Second: {file_count_second}"
        )
        assert report_content_first == report_content_second, "Report content changed on second run"
        assert manifest_content_first == manifest_content_second, (
            "Manifest content changed on second run"
        )

        # Both files must be valid JSON
        json.loads(report_content_second)
        json.loads(manifest_content_second)
    finally:
        db.close()


def test_lint_flags_unarchived_stale_script_without_warm_catalog(tmp_path: Path, monkeypatch):
    """Regression test: check_archival_candidates must fire through the `bth lint` CLI
    command even when no warm catalog (bathos.db) has ever been compacted -- it was
    previously wired inside the `if db_path.exists():` block alongside the warm-catalog-only
    checks, which silently suppressed it on every project that hadn't yet run `bth compact`."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    scripts_dir = tmp_path / "scripts" / "experiments"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "run_thing.py").write_text("print('hello')\n")
    (scripts_dir / "run_thing.bth.toml").write_text(
        "[experiment]\nhypothesis = 'h'\n[status]\nstale = true\nstale_reason = 'bad default'\n"
    )

    assert not catalog.exists()  # no compact has run -- db_path.exists() would be False
    result = runner.invoke(app, ["lint"])
    assert "unarchived_stale_script" in result.output



_PASSING_STOICHIOMETRY_TOML = """
[experiment]
hypothesis = "Passing 50M ONT library stoichiometry"
[outcomes.pass]
condition = "val > 0"
decision = "go"
reasoning = "ok"
[outcomes.fail]
condition = "val <= 0"
decision = "stop"
reasoning = "bad"
is_residual = true
[result_schema]
val = "float"

[stoichiometry]
barcoding_mode = "primer_pcr"
min_consensus_reads = 3.0
min_template_ratio = 10.0

[[stoichiometry.stages]]
name = "dna_in"
kind = "input_molecules"
molecules = 1e11
species = ["methylated_circular_vector", "linear_partial_ligations"]

[[stoichiometry.stages]]
name = "dpni"
kind = "enzymatic_species_cleanup"
depends_on = ["dna_in"]
enzyme = "DpnI"

[[stoichiometry.stages]]
name = "exo"
kind = "enzymatic_species_cleanup"
depends_on = ["dpni"]
enzyme = "linear_selective_nuclease"

[[stoichiometry.stages]]
name = "colonies"
kind = "colony_cfu"
depends_on = ["exo"]
colony_cfu = 1500000.0

[[stoichiometry.stages]]
name = "bulk_qc"
kind = "bulk_fluorometry"
depends_on = ["colonies"]

[[stoichiometry.stages]]
name = "pcr_pool"
kind = "pcr_template_molecules"
depends_on = ["bulk_qc"]
copies_per_cell = 1000.0

[[stoichiometry.stages]]
name = "reads"
kind = "phix_adjusted_net_reads"
depends_on = ["pcr_pool"]
raw_reads = 50000000
"""

_FAILING_STOICHIOMETRY_TOML = """
[experiment]
hypothesis = "Failing collision stoichiometry"
[outcomes.pass]
condition = "val > 0"
decision = "go"
reasoning = "ok"
[outcomes.fail]
condition = "val <= 0"
decision = "stop"
reasoning = "bad"
is_residual = true
[result_schema]
val = "float"

[stoichiometry]
barcoding_mode = "pre_barcoded_vector"
max_collision_prob = 0.05

[[stoichiometry.stages]]
name = "dna_in"
kind = "input_molecules"
molecules = 1e9

[[stoichiometry.stages]]
name = "colonies"
kind = "colony_cfu"
depends_on = ["dna_in"]
colony_cfu = 1500000.0

[[stoichiometry.stages]]
name = "bc"
kind = "barcode_collision"
depends_on = ["colonies"]
barcode_diversity = 5000000
"""


def _write_mixed_base_repo(tmp_path: Path, *, include_stoich: bool = True) -> Path:
    from bathos.artifact_archive import _STUB_MARKER

    catalog = tmp_path / ".bth" / "catalog"
    init_catalog(catalog)
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')
    (tmp_path / "scripts" / "experiments").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts" / "experiments" / ".bth.toml").write_text(
        '[experiment]\nhypothesis = "Directory experiment defaults"\n[result_schema]\nval = "float"\n'
    )
    (tmp_path / "scripts" / "benchmarks").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts" / "benchmarks" / ".bth.toml").write_text(
        '[benchmark]\nbaseline_ref = "ref"\nmetric = "m"\n[result_schema]\nval = "float"\n'
    )
    (tmp_path / "sub_prov").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sub_prov" / ".bth.toml").write_text('[provenance]\nsource = "test"\n')
    (tmp_path / "sub_bearings").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sub_bearings" / ".bth.toml").write_text(
        '[bearings]\nshadow_log_dir = "/tmp/outside_shadow"\n'
    )
    (tmp_path / "sub_instruments").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sub_instruments" / ".bth.toml").write_text(
        '[instruments.boltz]\nversion = "2.1"\n'
    )
    (tmp_path / "claim.bth.toml").write_text('[claim]\nstatement = "H1"\n')
    att_dir = tmp_path / ".bth" / "attestations"
    att_dir.mkdir(parents=True, exist_ok=True)
    (att_dir / "sample.attestation.bth.toml").write_text('[attestation]\nstatus = "pass"\n')
    (tmp_path / "parity.bth.toml").write_text('[parity]\nreference_paper = "10.1/x"\n')
    (tmp_path / "scripts" / "experiments" / "archived_exp.bth.toml").write_text(
        f"# {_STUB_MARKER} (item_id=item_001)\n# bathos:archived\n# reason: superseded\n"
    )
    (tmp_path / "scripts" / "experiments" / "submit_only.bth.toml").write_text(
        '[cluster]\npartition = "gpu"\n'
    )
    if include_stoich:
        (tmp_path / "scripts" / "experiments" / "valid_stoich.bth.toml").write_text(
            _PASSING_STOICHIOMETRY_TOML
        )
    return catalog


def test_check_stoichiometry_passes_on_clean_mixed_repository(tmp_path: Path, monkeypatch) -> None:
    catalog = _write_mixed_base_repo(tmp_path, include_stoich=True)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))

    result = runner.invoke(
        app,
        ["check", "--check-stoichiometry", "--project-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["stoichiometry_failure_count"] == 0
    statuses = {Path(item["path"]).name: item["status"] for item in payload["stoichiometry_results"]}
    assert statuses["valid_stoich.bth.toml"] == "ok"
    assert statuses["archived_exp.bth.toml"] == "skipped"
    assert statuses["submit_only.bth.toml"] == "skipped"
    assert statuses["claim.bth.toml"] == "skipped"
    assert statuses["parity.bth.toml"] == "skipped"


def test_check_stoichiometry_fails_on_zero_stoichiometry_projects(
    tmp_path: Path, monkeypatch
) -> None:
    from bathos.mcp import check_tool

    catalog = _write_mixed_base_repo(tmp_path, include_stoich=False)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))

    cli_res = runner.invoke(
        app,
        ["check", "--check-stoichiometry", "--project-root", str(tmp_path)],
    )
    assert cli_res.exit_code == 1
    direct = check_tool(
        catalog_dir=str(catalog),
        project_root=str(tmp_path),
        check_stoichiometry=True,
    )
    assert direct["stoichiometry_failure_count"] == 1
    assert "No sidecar files" in direct["error"]


@pytest.mark.parametrize(
    ("rel_path", "bad_toml"),
    [
        (
            "bad_proj/.bth.toml",
            '[project]\nslug = "x"\n[stoichiometry]\n[[stoichiometry.stages]]\nname="a"\nkind="input_molecules"\nmolecules=10\n',
        ),
        (
            "bad_bearings/.bth.toml",
            '[bearings]\nshadow_log_dir = "/tmp/s"\n[stoichiometry]\n[[stoichiometry.stages]]\nname="a"\nkind="input_molecules"\nmolecules=10\n',
        ),
        (
            "bad_instruments/.bth.toml",
            '[instruments.x]\nv = "1"\n[reagents.k]\npack_rxns = 10\nexperiment_rxns = 5\n',
        ),
        (
            "scripts/experiments/cluster_reag.bth.toml",
            '[cluster]\npartition = "gpu"\n[reagents.k]\npack_rxns = 10\nexperiment_rxns = 5\n',
        ),
        (
            "claim_reag/claim.bth.toml",
            '[claim]\nstatement = "H1"\n[reagents.k]\npack_rxns = 10\nexperiment_rxns = 5\n',
        ),
        (
            ".bth/attestations/bad.attestation.bth.toml",
            '[attestation]\nstatus = "pass"\n[stoichiometry]\n[[stoichiometry.stages]]\nname="a"\nkind="input_molecules"\nmolecules=10\n',
        ),
        (
            "parity_bad/parity.bth.toml",
            '[parity]\nref = "x"\n[reagents.k]\npack_rxns = 10\nexperiment_rxns = 5\n',
        ),
        (
            "scripts/benchmarks/bench_stoich.bth.toml",
            '[benchmark]\n[result_schema]\nx = "float"\n[stoichiometry]\n[[stoichiometry.stages]]\nname="a"\nkind="input_molecules"\nmolecules=10\n',
        ),
        (
            "scripts/experiments/stub_stoich.bth.toml",
            '# ARCHIVED by bathos artifact_archive (item_id=i1)\n[stoichiometry]\n[[stoichiometry.stages]]\nname="a"\nkind="input_molecules"\nmolecules=10\n',
        ),
        (
            "scripts/experiments/stem_bearings.bth.toml",
            '[bearings]\nshadow_log_dir = "/tmp/x"\n',
        ),
        (
            "scripts/experiments/stem_instruments.bth.toml",
            '[instruments.boltz]\nv = "1"\n',
        ),
        (
            "bad_cluster_dot/.bth.toml",
            '[cluster]\npartition = "cpu"\n',
        ),
        (
            "scripts/experiments/non_stub_empty.bth.toml",
            "# plain comment without archive stub marker\n",
        ),
        (
            "scripts/experiments/stub_typo.bth.toml",
            '# ARCHIVED by bathos artifact_archive (item_id=i2)\n[experment]\nhypothesis = "typo"\n',
        ),
        (
            "scripts/experiments/cluster_typo.bth.toml",
            '[cluster]\npartition = "gpu"\n[experment]\nhypothesis = "typo"\n',
        ),
        (
            "scripts/experiments/broken_kind.bth.toml",
            '[experment]\nhypothesis = "typo"\n',
        ),
    ],
)
def test_check_stoichiometry_fails_on_illegal_and_malformed_discovery_files(
    tmp_path: Path,
    monkeypatch,
    rel_path: str,
    bad_toml: str,
) -> None:
    catalog = _write_mixed_base_repo(tmp_path, include_stoich=True)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    target = tmp_path / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(bad_toml, encoding="utf-8")

    result = runner.invoke(
        app,
        ["check", "--check-stoichiometry", "--project-root", str(tmp_path)],
    )
    assert result.exit_code == 1, f"{rel_path} unexpectedly passed: {result.output}"


def test_check_stoichiometry_direct_sidecar_path_and_rollback_separation(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio

    from bathos.artifact_archive import _STUB_MARKER
    from bathos.mcp import mcp_check_tool

    catalog = _write_mixed_base_repo(tmp_path, include_stoich=False)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))

    good = tmp_path / "good.bth.toml"
    good.write_text(_PASSING_STOICHIOMETRY_TOML)
    bad = tmp_path / "bad.bth.toml"
    bad.write_text(_FAILING_STOICHIOMETRY_TOML)
    no_stoich = tmp_path / "no_stoich.bth.toml"
    no_stoich.write_text('[experiment]\nhypothesis = "h"\n[result_schema]\nv = "float"\n')
    stub_path = tmp_path / "archived_direct.bth.toml"
    stub_path.write_text(f"# {_STUB_MARKER} (item_id=x)\n")
    cluster_path = tmp_path / "cluster_direct.bth.toml"
    cluster_path.write_text('[cluster]\npartition = "gpu"\n')
    reag_bad = tmp_path / "reagent_bad.bth.toml"
    reag_bad.write_text(
        """
[experiment]
hypothesis = "Exceeded reagent waste bound"
[outcomes.pass]
condition = "v > 0"
decision = "go"
is_residual = true
[result_schema]
v = "float"
[reagents.kit]
pack_rxns = 96
experiment_rxns = 100
max_waste_fraction = 0.2
"""
    )

    assert (
        runner.invoke(
            app,
            ["check", "--check-stoichiometry", "--sidecar-path", str(good), "--project-root", str(tmp_path)],
        ).exit_code
        == 0
    )
    # Direct --sidecar-path ignores BTH_STOICHIOMETRY_WARN_ONLY
    monkeypatch.setenv("BTH_STOICHIOMETRY_WARN_ONLY", "1")
    for failing_target in (bad, no_stoich, stub_path, cluster_path, tmp_path / ".bth.toml", reag_bad):
        res = runner.invoke(
            app,
            [
                "check",
                "--check-stoichiometry",
                "--sidecar-path",
                str(failing_target),
                "--project-root",
                str(tmp_path),
            ],
        )
        assert res.exit_code == 1, f"{failing_target} unexpectedly passed"

    # MCP mcp_check_tool parity forwards check_outputs, check_stoichiometry, and sidecar_path
    mcp_res = asyncio.run(
        mcp_check_tool(
            catalog_dir=str(catalog),
            project_root=str(tmp_path),
            check_outputs=True,
            check_stoichiometry=True,
            sidecar_path=str(good),
        )
    )
    assert mcp_res["ok"] is True
    assert mcp_res["stoichiometry_failure_count"] == 0


def test_verify_citations_cli_pass_and_failure_modes(tmp_path: Path) -> None:
    import hashlib
    import json

    sources_dir = tmp_path / "sources"
    sources_dir.mkdir()
    src_bytes = (
        b"[Page 1, Line 1] First verified sentence.\n"
        b"[Page 2, Line 1] Second page sentence.\n"
    )
    (sources_dir / "src.numbered.txt").write_bytes(src_bytes)
    digest = hashlib.sha256(src_bytes).hexdigest()
    (sources_dir / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "sources": {"src.numbered.txt": {"sha256": digest}}}),
        encoding="utf-8",
    )

    ok_doc = tmp_path / "ok.md"
    ok_doc.write_text(
        f'> "First verified sentence." [source: src.numbered.txt, sha256: {digest}, Page 1, Line 1]\n',
        encoding="utf-8",
    )
    ok_cli = runner.invoke(
        app,
        ["verify-citations", "--doc-path", str(ok_doc), "--sources-dir", str(sources_dir)],
    )
    assert ok_cli.exit_code == 0, ok_cli.output

    oow_doc = tmp_path / "oow.md"
    oow_doc.write_text(
        f"> Second page sentence. [source: src.numbered.txt, sha256: {digest}, Page 1, Line 1]\n",
        encoding="utf-8",
    )
    assert (
        runner.invoke(
            app,
            ["verify-citations", "--doc-path", str(oow_doc), "--sources-dir", str(sources_dir)],
        ).exit_code
        == 1
    )
    assert (
        runner.invoke(
            app,
            [
                "verify-citations",
                "--doc-path",
                str(oow_doc),
                "--sources-dir",
                str(sources_dir),
                "--no-strict-coords",
            ],
        ).exit_code
        == 0
    )

    empty_doc = tmp_path / "empty_quote.md"
    empty_doc.write_text(
        f'> "" [source: src.numbered.txt, sha256: {digest}, Page 1, Line 1]\n',
        encoding="utf-8",
    )
    assert (
        runner.invoke(
            app,
            [
                "verify-citations",
                "--doc-path",
                str(empty_doc),
                "--sources-dir",
                str(sources_dir),
                "--no-strict-coords",
            ],
        ).exit_code
        == 1
    )
