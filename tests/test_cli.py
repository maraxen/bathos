import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from bathos.catalog import init_catalog, write_run
from bathos.cli import app, _catalog_dir
from bathos.schema import Run

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "bathos" in result.output


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
    result = runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])
    assert result.exit_code == 0


def test_ls_shows_runs(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')
    runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])
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
    runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])
    from bathos.catalog import init_catalog, read_runs

    init_catalog(catalog)
    runs = read_runs(catalog)
    result = runner.invoke(app, ["show", runs[0].id])
    assert result.exit_code == 0
    # Rich panels show "Execution" and "Provenance" headers
    assert "Execution" in result.output


def test_compact_command_runs(tmp_path: Path, monkeypatch):
    """Test that bth compact command executes successfully and produces summary output."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')
    # Create a few runs
    runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])
    runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])

    result = runner.invoke(app, ["compact"])
    assert result.exit_code == 0
    assert "Compacted" in result.output
    assert "bathos.db" in result.output
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
        runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])

    # Verify no warm DB exists
    assert not (catalog / "bathos.db").exists()

    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    # Should show banner
    assert "bth compact" in result.output
    assert "uncompacted" in result.output


def test_ls_no_banner_below_threshold(tmp_path: Path, monkeypatch):
    """Test that ls does not show banner when fragment count <= 50."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    # Create 10 runs (< 50 threshold)
    for _ in range(10):
        runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])

    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    # Should NOT show banner
    assert "bth compact" not in result.output


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
        runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])

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
    runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])

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
    runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])

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
    assert "STALE" in result.output
    assert "Warning" in result.output or "stale" in result.output.lower()


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

    # Filter by STALE
    result = runner.invoke(app, ["check", "--status", "STALE"])
    assert result.exit_code == 1
    assert "STALE" in result.output
    assert "DIRTY_RUN" not in result.output

    # Filter by DIRTY_RUN
    result = runner.invoke(app, ["check", "--status", "DIRTY_RUN"])
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
    runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])
    runner.invoke(app, ["compact"])

    # Run archive command
    archive_dir = tmp_path / "archive"
    result = runner.invoke(app, ["archive", "--archive-dir", str(archive_dir)])

    assert result.exit_code == 0
    assert "Archived" in result.output
    assert "partitions" in result.output
    assert "Manifest" in result.output


def test_archive_command_dry_run(tmp_path: Path, monkeypatch):
    """Verify --dry-run flag prevents writing."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    # Create and compact a run
    runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])
    runner.invoke(app, ["compact"])

    # Run archive with --dry-run
    archive_dir = tmp_path / "archive"
    result = runner.invoke(app, ["archive", "--archive-dir", str(archive_dir), "--dry-run"])

    assert result.exit_code == 0
    assert "[dry-run]" in result.output
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
    runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])

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
    runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])
    runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])

    monkeypatch.setenv("BTH_PROJECT_SLUG", "proj2")
    runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])

    # Compact
    runner.invoke(app, ["compact"])

    # Archive only proj1
    archive_dir = tmp_path / "archive"
    result = runner.invoke(
        app, ["archive", "--project", "proj1", "--archive-dir", str(archive_dir)]
    )

    assert result.exit_code == 0
    assert "Archived" in result.output
    # Should archive 2 runs (only from proj1)
    assert "2" in result.output


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

    assert result.exit_code == 0
    assert "Output File Status" in result.output
    assert "present" in result.output
    assert str(output_file) in result.output


def test_export_dry_run_claude_user(monkeypatch):
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
    from bathos.schema import Run
    from bathos.catalog import write_run
    from bathos.compact import compact
    import duckdb

    r = Run(project_slug="proj", command="echo hi", argv=["echo", "hi"],
            git_hash="abc", git_branch="main", git_dirty=False,
            status="completed", exit_code=0)
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


def test_report_emit_cli_smoke_test(tmp_path: Path, monkeypatch):
    """CLI-level test: bth report emit on a concluded campaign creates both sidecar files.

    Validates the complete `bth report emit <campaign_id>` flow:
    - Creates a tmp catalog via BTH_CATALOG_DIR with DuckDB schema
    - Creates and concludes a campaign
    - Invokes `runner.invoke(app, ['report', 'emit', campaign_id])`
    - Asserts exit code == 0
    - Asserts both sidecar files exist at the pinned path
    """
    import duckdb
    import json

    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    # Initialize catalog with DuckDB schema
    from bathos.catalog import init_catalog
    from bathos.campaigns import create_campaign, add_run_to_campaign
    from bathos.compact import _RUNS_TABLE_SCHEMA, _CAMPAIGNS_TABLE_SCHEMA, _CAMPAIGN_RUNS_TABLE_SCHEMA

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
        db.execute("""
            INSERT INTO runs (
                id, project_slug, command, argv, git_hash, git_branch, git_dirty, timestamp,
                status, exit_code, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            run.id, run.project_slug, run.command, json.dumps(run.argv),
            run.git_hash, run.git_branch, run.git_dirty, run.timestamp,
            run.status, run.exit_code, "7"
        ])
        add_run_to_campaign(db, campaign_id, run.id)

        # Conclude the campaign
        db.execute(
            "UPDATE campaigns SET conclusion = ?, status = 'concluded' WHERE id = ?",
            ["Test passed", campaign_id]
        )
    finally:
        db.close()

    # Now invoke bth report emit via CLI
    result = runner.invoke(app, ["report", "emit", campaign_id])

    # Assertions
    assert result.exit_code == 0, f"Expected exit code 0, got {result.exit_code}. Output: {result.output}"

    # Both sidecar files must exist at the pinned path
    report_path = catalog / "sidecars" / campaign_id / "campaign_report.json"
    manifest_path = catalog / "sidecars" / campaign_id / "figure_manifest.json"

    assert report_path.exists(), f"Campaign report not found at {report_path}"
    assert manifest_path.exists(), f"Figure manifest not found at {manifest_path}"


def test_report_emit_idempotency(tmp_path: Path, monkeypatch):
    """Test that calling emit_campaign_report and emit_figure_manifest twice is idempotent.

    Verifies that a second run overwrites (not appends) the files, and content is stable.
    """
    import duckdb
    import json

    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')

    # Initialize catalog and create a concluded campaign
    from bathos.catalog import init_catalog
    from bathos.campaigns import (
        create_campaign,
        add_run_to_campaign,
        emit_campaign_report,
        emit_figure_manifest,
    )
    from bathos.compact import _RUNS_TABLE_SCHEMA, _CAMPAIGNS_TABLE_SCHEMA, _CAMPAIGN_RUNS_TABLE_SCHEMA

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
        db.execute("""
            INSERT INTO runs (
                id, project_slug, command, argv, git_hash, git_branch, git_dirty, timestamp,
                status, exit_code, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            run.id, run.project_slug, run.command, json.dumps(run.argv),
            run.git_hash, run.git_branch, run.git_dirty, run.timestamp,
            run.status, run.exit_code, "7"
        ])
        add_run_to_campaign(db, campaign_id, run.id)

        # Conclude the campaign
        db.execute(
            "UPDATE campaigns SET conclusion = ?, status = 'concluded' WHERE id = ?",
            ["Test passed", campaign_id]
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
        assert file_count_first == file_count_second == 2, f"Expected 2 files (not 4). First: {file_count_first}, Second: {file_count_second}"
        assert report_content_first == report_content_second, "Report content changed on second run"
        assert manifest_content_first == manifest_content_second, "Manifest content changed on second run"

        # Both files must be valid JSON
        json.loads(report_content_second)
        json.loads(manifest_content_second)
    finally:
        db.close()
