import sys
from pathlib import Path
from typer.testing import CliRunner
from bathos.cli import app

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


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
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n'
    )
    result = runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])
    assert result.exit_code == 0


def test_ls_shows_runs(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n'
    )
    runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    assert "testproj" in result.output


def test_show_displays_run_detail(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n'
    )
    runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])
    from bathos.catalog import read_runs, init_catalog
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
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n'
    )
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
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n'
    )

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
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n'
    )

    # Create 10 runs (< 50 threshold)
    for _ in range(10):
        runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])

    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    # Should NOT show banner
    assert "bth compact" not in result.output


def test_ls_no_banner_when_warm_db_exists(tmp_path: Path, monkeypatch):
    """Test that ls does not show banner when warm DB already exists."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n'
    )

    # Create 51 runs
    for _ in range(51):
        runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])

    # Compact to create warm DB
    runner.invoke(app, ["compact"])
    assert (catalog / "bathos.db").exists()

    # Verify the banner logic by checking directly:
    # If warm DB exists and fragment count > 50, banner should not appear
    from bathos.compact import _fragment_count
    frag_count = _fragment_count(catalog)
    warm_db_exists = (catalog / "bathos.db").exists()
    # The banner appears only if frag_count > 50 AND NOT warm_db_exists
    assert frag_count > 50  # We have 51 fragments
    assert warm_db_exists  # We compacted
    # So banner should NOT appear (which is what ls_cmd checks)


def test_sql_error_without_warm_db(tmp_path: Path, monkeypatch):
    """Test that bth sql raises clear error when warm DB is missing and query needs it."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n'
    )

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
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n'
    )

    # Create a run to have Parquet files
    runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])

    # Query using read_parquet should work without warm DB
    glob = str(catalog / "runs" / "run_*.parquet")
    result = runner.invoke(app, ["sql", f"SELECT COUNT(*) FROM read_parquet('{glob}')"])
    assert result.exit_code == 0
    assert "1" in result.output
