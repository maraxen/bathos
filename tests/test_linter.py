import pytest
from pathlib import Path
from typer.testing import CliRunner

runner = CliRunner()


def _make_script(base: Path, subdir: str, name: str) -> Path:
    d = base / "scripts" / subdir
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("# script")
    return p


def _make_sidecar(script: Path) -> Path:
    s = script.with_suffix(".bth.toml")
    s.write_text("[experiment]\nhypothesis='h'\n[result_schema]\n")
    return s


def test_clean_project_returns_no_issues(tmp_path):
    from bathos.linter import lint_project
    s = _make_script(tmp_path, "experiments", "run_nvt.py")
    _make_sidecar(s)
    issues = lint_project(tmp_path)
    assert issues == []


def test_bad_name_in_experiments(tmp_path):
    from bathos.linter import lint_project
    s = _make_script(tmp_path, "experiments", "RunNVT.py")
    _make_sidecar(s)
    issues = lint_project(tmp_path)
    assert any(i.issue == "naming" for i in issues)
    assert any("RunNVT.py" in str(i.path) for i in issues)


def test_missing_sidecar_is_error(tmp_path):
    from bathos.linter import lint_project
    _make_script(tmp_path, "experiments", "run_nvt.py")
    issues = lint_project(tmp_path)
    assert any(i.issue == "missing_sidecar" for i in issues)


def test_validation_missing_sidecar_is_warning(tmp_path):
    from bathos.linter import lint_project, IssueSeverity
    _make_script(tmp_path, "validation", "check_energy.py")
    issues = lint_project(tmp_path)
    sidecar_issues = [i for i in issues if i.issue == "missing_sidecar"]
    assert all(i.severity == IssueSeverity.WARNING for i in sidecar_issues)


def test_debug_yymmdd_pattern(tmp_path):
    from bathos.linter import lint_project
    _make_script(tmp_path, "debug", "260519_nan_forces.py")
    issues = lint_project(tmp_path)
    assert issues == []


def test_debug_bad_name(tmp_path):
    from bathos.linter import lint_project
    _make_script(tmp_path, "debug", "nan_forces.py")
    issues = lint_project(tmp_path)
    assert any(i.issue == "naming" for i in issues)


def test_slurm_pattern(tmp_path):
    from bathos.linter import lint_project
    _make_script(tmp_path, "slurm", "run_md.slurm")
    issues = lint_project(tmp_path)
    assert issues == []


def test_slurm_bad_extension(tmp_path):
    from bathos.linter import lint_project
    _make_script(tmp_path, "slurm", "run_md.sh")
    issues = lint_project(tmp_path)
    assert any(i.issue == "naming" for i in issues)


def test_missing_scripts_dir_returns_empty(tmp_path):
    from bathos.linter import lint_project
    issues = lint_project(tmp_path)
    assert issues == []


def test_cli_lint_exits_0_clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = _make_script(tmp_path, "experiments", "run_nvt.py")
    _make_sidecar(s)
    from bathos.cli import app
    result = runner.invoke(app, ["lint"])
    assert result.exit_code == 0
    assert "No issues" in result.output


def test_cli_lint_exits_1_with_issues(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_script(tmp_path, "experiments", "RunNVT.py")
    from bathos.cli import app
    result = runner.invoke(app, ["lint"])
    assert result.exit_code == 1
    assert "error" in result.output.lower() or "issue" in result.output.lower()
