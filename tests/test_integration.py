"""End-to-end: init → run → ls → show → find."""
import sys
from pathlib import Path
from typer.testing import CliRunner
from bathos.cli import app
from bathos.catalog import read_runs, init_catalog

runner = CliRunner()


def test_full_workflow(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "intproj")

    # 1. init
    r = runner.invoke(app, ["init", "--slug", "intproj"])
    assert r.exit_code == 0
    assert (tmp_path / ".bth.toml").exists()
    assert (tmp_path / "scripts" / "experiments").is_dir()

    # 2. run a passing script
    r = runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])
    assert r.exit_code == 0

    # 3. run a failing script
    r = runner.invoke(app, ["run", sys.executable, "--", "-c", "raise SystemExit(1)"])
    assert r.exit_code == 1

    # 4. ls shows both runs
    r = runner.invoke(app, ["ls"])
    assert r.exit_code == 0
    assert "intproj" in r.output
    lines = [l for l in r.output.splitlines() if "intproj" in l]
    assert len(lines) == 2

    # 5. find by status
    r = runner.invoke(app, ["find", "--status", "failed"])
    assert r.exit_code == 0
    assert "failed" in r.output

    # 6. show run detail
    init_catalog(catalog)
    runs = read_runs(catalog)
    run_id = runs[0].id
    r = runner.invoke(app, ["show", run_id])
    assert r.exit_code == 0
    assert run_id in r.output
    assert "intproj" in r.output

    # 7. sql escape hatch
    glob = str(catalog / "runs" / "run_*.parquet")
    r = runner.invoke(app, ["sql", f"SELECT count(*) FROM read_parquet('{glob}')"])
    assert r.exit_code == 0
    assert "2" in r.output
