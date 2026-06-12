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


def test_scaffold_passes_validate_sidecar(tmp_path):
    """Test that generated scaffold passes validate_sidecar without modification."""
    from bathos.new_experiment import scaffold_experiment
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    result = scaffold_experiment("run_test", tmp_path)
    assert result.sidecar.exists()

    # Parse the generated sidecar
    sidecar = parse_sidecar(result.sidecar)

    # Validate it
    validation = validate_sidecar(sidecar, sidecar_path=result.sidecar)
    assert validation.ok, f"Scaffold validation failed: {validation.errors}"

    # Verify stage_name and novel are set
    assert sidecar.stage_name == "exploration", f"Expected stage_name='exploration', got {sidecar.stage_name!r}"
    assert sidecar.novel is False, f"Expected novel=False, got {sidecar.novel!r}"

    # Verify all outcome branches have reasoning
    for label, spec in sidecar.outcomes.items():
        assert spec.reasoning, f"outcome '{label}' missing reasoning field"
        assert not spec.reasoning.startswith("TODO"), f"outcome '{label}' reasoning contains TODO"

    # Verify at least one outcome has is_residual=true
    assert any(spec.is_residual for spec in sidecar.outcomes.values()), "No outcome with is_residual=true"

    # Verify adversarial_check on pass branch is non-empty and not TODO
    pass_spec = sidecar.outcomes.get("pass")
    assert pass_spec is not None, "No 'pass' outcome defined"
    assert pass_spec.adversarial_check, "pass outcome missing adversarial_check"
    assert not pass_spec.adversarial_check.startswith("TODO"), "pass adversarial_check contains TODO"

    # Verify result_schema is not empty and contains metric
    assert sidecar.result_schema, "result_schema is empty"
    assert "metric" in sidecar.result_schema, "result_schema does not contain 'metric' field"

    # Verify commented blocks are present in raw TOML
    sidecar_text = result.sidecar.read_text()
    assert "[reproduction]" in sidecar_text, "[reproduction] block comment section missing"
    assert "[controls]" in sidecar_text, "[controls] block comment section missing"
