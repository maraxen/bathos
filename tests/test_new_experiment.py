import pytest

from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()


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
    from bathos.cli_cyclopts import app

    result = runner.invoke(app, ["new-experiment", "run_smoke"])
    assert result.exit_code == 0
    assert "run_smoke.py" in result.output
    assert "run_smoke.bth.toml" in result.output
    assert (tmp_path / "scripts" / "experiments" / "run_smoke.py").exists()


def test_cli_new_experiment_refuses_overwrite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from bathos.cli_cyclopts import app

    runner.invoke(app, ["new-experiment", "run_smoke"])
    result = runner.invoke(app, ["new-experiment", "run_smoke"])
    assert result.exit_code == 1
    assert "Error" in result.output


def test_cli_new_experiment_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from bathos.cli_cyclopts import app

    runner.invoke(app, ["new-experiment", "run_smoke"])
    result = runner.invoke(app, ["new-experiment", "run_smoke", "--force"])
    assert result.exit_code == 0


def test_scaffold_passes_validate_sidecar(tmp_path):
    """The default (explore) scaffold passes validate_sidecar without modification."""
    from bathos.new_experiment import scaffold_experiment
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    result = scaffold_experiment("run_test", tmp_path)
    assert result.sidecar.exists()

    sidecar = parse_sidecar(result.sidecar)
    validation = validate_sidecar(sidecar, sidecar_path=result.sidecar)
    assert validation.ok, f"Scaffold validation failed: {validation.errors}"
    assert validation.errors == []

    assert sidecar.stage_name == "exploration", (
        f"Expected stage_name='exploration', got {sidecar.stage_name!r}"
    )
    assert sidecar.novel is False, f"Expected novel=False, got {sidecar.novel!r}"
    assert sidecar.result_schema, "result_schema is empty"
    assert "metric" in sidecar.result_schema, "result_schema does not contain 'metric' field"


def test_explore_scaffold_is_exploratory_and_has_observe(tmp_path):
    from bathos.new_experiment import scaffold_experiment
    from bathos.sidecar import parse_sidecar, resolve_epistemic_class

    result = scaffold_experiment("run_explore", tmp_path)
    assert result.mode == "explore"
    assert result.needs_filling == []
    assert result.epistemic_class == "exploratory"

    sidecar = parse_sidecar(result.sidecar)
    assert resolve_epistemic_class(sidecar, stem_has_prior_runs=False) == "exploratory"
    assert sidecar.question, "question must be non-empty"
    assert sidecar.observe is not None
    assert sidecar.observe.outputs == ["results.json"]
    # The script template really writes the file [observe].outputs declares.
    assert "results.json" in result.script.read_text()
    # No uncommented [outcomes] section; the optional one is commented out.
    raw_lines = result.sidecar.read_text().splitlines()
    assert not [ln for ln in raw_lines if ln.lstrip().startswith("[outcomes")]
    assert any(ln.lstrip().startswith("# [outcomes.pass]") for ln in raw_lines)
    assert "a changed exploratory outcome is not a deviation" in result.sidecar.read_text()
    assert "source = \"provisional:<why" in result.sidecar.read_text()


def test_confirm_scaffold_is_confirmatory_with_placeholder_errors_only(tmp_path):
    from bathos.new_experiment import scaffold_experiment
    from bathos.sidecar import parse_sidecar, resolve_epistemic_class
    from bathos.validate import validate_sidecar

    result = scaffold_experiment("run_confirm", tmp_path, mode="confirm")
    assert result.mode == "confirm"
    assert result.epistemic_class == "confirmatory"

    sidecar = parse_sidecar(result.sidecar)
    assert resolve_epistemic_class(sidecar, stem_has_prior_runs=False) == "confirmatory"
    assert sidecar.stage_name == "validation"

    validation = validate_sidecar(sidecar, sidecar_path=result.sidecar)
    assert not validation.ok
    # Every error is an unfilled ${...} placeholder, and each one is reported as needs_filling.
    assert validation.errors
    assert all("${" in e.message or "${" in e.field for e in validation.errors)
    # Unfilled sources, plus the conditions and adversarial_check that still hold ${THRESHOLD}
    # (DuckDB cannot parse those until filled). All of them are placeholder states.
    assert len(result.needs_filling) == len(validation.errors)
    fields = {e.field for e in validation.errors}
    assert {"outcomes.pass.source", "outcomes.marginal.source"} <= fields


def test_confirm_scaffold_validates_once_placeholders_are_filled(tmp_path):
    from bathos.new_experiment import scaffold_experiment
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    result = scaffold_experiment("run_confirm_filled", tmp_path, mode="confirm")
    text = result.sidecar.read_text()
    filled = text.replace("${THRESHOLD}", "0.5").replace("${SOURCE}", "lit:doi:10.1/x")
    result.sidecar.write_text(filled)

    validation = validate_sidecar(parse_sidecar(result.sidecar), sidecar_path=result.sidecar)
    assert validation.ok, f"filled confirm scaffold failed: {validation.errors}"


def test_confirm_scaffold_keeps_reproduction_and_controls_comments(tmp_path):
    from bathos.new_experiment import scaffold_experiment

    text = scaffold_experiment("run_confirm_blocks", tmp_path, mode="confirm").sidecar.read_text()
    assert "[reproduction]" in text
    assert "[controls]" in text


def test_invalid_mode_raises(tmp_path):
    from bathos.new_experiment import scaffold_experiment

    with pytest.raises(ValueError, match="unknown mode 'bogus'"):
        scaffold_experiment("run_bad_mode", tmp_path, mode="bogus")
    assert not (tmp_path / "scripts" / "experiments" / "run_bad_mode.py").exists()


def test_mcp_tool_accepts_mode(tmp_path):
    from bathos.mcp import new_experiment_tool

    out = new_experiment_tool("run_mcp_confirm", workspace_root=str(tmp_path), mode="confirm")
    assert out["ok"] is True
    assert out["mode"] == "confirm"
    assert out["epistemic_class"] == "confirmatory"
    assert out["sidecar_validation"]["ok"] is False
    assert out["sidecar_validation"]["needs_filling"]
    assert out["sidecar_validation"]["errors"] == []

    out = new_experiment_tool("run_mcp_explore", workspace_root=str(tmp_path))
    assert out["mode"] == "explore"
    assert out["sidecar_validation"]["ok"] is True


def test_mcp_tool_rejects_bad_mode(tmp_path):
    from bathos.mcp import new_experiment_tool

    out = new_experiment_tool("run_mcp_bad", workspace_root=str(tmp_path), mode="bogus")
    assert out["ok"] is False
    assert "unknown mode" in out["error"]
    assert not (tmp_path / "scripts" / "experiments" / "run_mcp_bad.py").exists()


def test_cli_new_experiment_mode_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from bathos.cli_cyclopts import app

    result = runner.invoke(app, ["new-experiment", "run_cli_confirm", "--mode", "confirm"])
    assert result.exit_code == 0, result.output
    text = (tmp_path / "scripts" / "experiments" / "run_cli_confirm.bth.toml").read_text()
    assert 'stage_name = "validation"' in text


def test_scaffolded_script_runs_with_stdlib_only(tmp_path):
    """The scaffolded script must run in a bare interpreter (-I), dry-run or not.

    Regression for the typer defect: the template imported typer, which a fresh project does
    not have, so the first `bth run` died with ModuleNotFoundError.
    """
    import json
    import os
    import subprocess
    import sys

    from bathos.new_experiment import scaffold_experiment

    result = scaffold_experiment("run_scaffold_probe", tmp_path)
    assert "typer" not in result.script.read_text()

    for label, extra_args in (("dry", ["--dry-run"]), ("real", [])):
        run_dir = tmp_path / f"run_{label}"
        out_dir = run_dir / "outputs"
        out_dir.mkdir(parents=True)
        results_path = run_dir / "bth-results.json"
        env = os.environ.copy()
        env["BTH_RESULTS_PATH"] = str(results_path)
        env["BTH_OUTPUT_DIR"] = str(out_dir)

        proc = subprocess.run(
            [sys.executable, "-I", str(result.script), *extra_args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, f"{label}: {proc.stderr}"

        payload = json.loads(results_path.read_text())
        assert set(payload) >= {"metric", "n_samples"}, label
        assert json.loads((out_dir / "results.json").read_text()) == payload, label
        assert json.loads(proc.stdout) == payload, label
