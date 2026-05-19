from pathlib import Path
import pytest
from typer.testing import CliRunner

runner = CliRunner()


def test_creates_script_and_sidecar(tmp_path):
    from bathos.new_experiment import scaffold_experiment
    result = scaffold_experiment("run_nvt", tmp_path)
    assert result.script.exists()
    assert result.sidecar.exists()
    assert result.script.name == "run_nvt.py"
    assert result.sidecar.name == "run_nvt.bth.toml"
    assert "run_nvt" in result.script.read_text()
    assert "[experiment]" in result.sidecar.read_text()


def test_creates_experiments_dir_if_missing(tmp_path):
    from bathos.new_experiment import scaffold_experiment
    scaffold_experiment("run_test", tmp_path)
    assert (tmp_path / "scripts" / "experiments").is_dir()


def test_refuses_overwrite_without_force(tmp_path):
    from bathos.new_experiment import scaffold_experiment
    scaffold_experiment("run_test", tmp_path)
    with pytest.raises(FileExistsError):
        scaffold_experiment("run_test", tmp_path, force=False)


def test_force_overwrites(tmp_path):
    from bathos.new_experiment import scaffold_experiment
    scaffold_experiment("run_test", tmp_path)
    result = scaffold_experiment("run_test", tmp_path, force=True)
    assert result.script.exists()


def test_warns_on_bad_name_style(tmp_path):
    from bathos.new_experiment import scaffold_experiment
    result = scaffold_experiment("RunNVT", tmp_path)
    assert result.name_warning != ""
    assert result.script.exists()


def test_cli_new_experiment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from bathos.cli import app
    result = runner.invoke(app, ["new-experiment", "run_smoke"])
    assert result.exit_code == 0
    assert "run_smoke.py" in result.output
    assert "run_smoke.bth.toml" in result.output
    assert (tmp_path / "scripts" / "experiments" / "run_smoke.py").exists()


def test_cli_new_experiment_refuses_overwrite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from bathos.cli import app
    runner.invoke(app, ["new-experiment", "run_smoke"])
    result = runner.invoke(app, ["new-experiment", "run_smoke"])
    assert result.exit_code == 1
    assert "Error" in result.output


def test_cli_new_experiment_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from bathos.cli import app
    runner.invoke(app, ["new-experiment", "run_smoke"])
    result = runner.invoke(app, ["new-experiment", "run_smoke", "--force"])
    assert result.exit_code == 0
