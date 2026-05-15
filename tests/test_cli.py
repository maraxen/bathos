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
