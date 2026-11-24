"""Tests for configurable `.bth.toml` `[lint.naming]` exemptions and rules (#2916)."""

from __future__ import annotations

from pathlib import Path

from bathos.linter import IssueSeverity, lint_project
from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()


def _make_script(base: Path, subdir: str, name: str) -> Path:
    d = base / "scripts" / subdir
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("# script\n")
    return p


def _make_sidecar(script: Path) -> Path:
    s = script.with_suffix(".bth.toml")
    s.write_text("[experiment]\nhypothesis = 'h'\n[result_schema]\n")
    return s


def test_global_exempt_supports_relpaths_and_globs(tmp_path):
    (tmp_path / ".bth.toml").write_text(
        '[project]\nslug = "test"\nroot = "."\n\n'
        "[lint.naming]\n"
        'exempt = ["scripts/experiments/LegacyRun.py", "analysis/HelperTool.py", "helper_*.py", "README.md"]\n'
    )
    exp_ok = _make_script(tmp_path, "experiments", "LegacyRun.py")
    _make_sidecar(exp_ok)
    _make_script(tmp_path, "experiments", "README.md")
    _make_script(tmp_path, "analysis", "HelperTool.py")
    _make_script(tmp_path, "data", "helper_ingest.py")

    assert lint_project(tmp_path) == []

    # Negative check: non-exempt badly named script still fails
    bad = _make_script(tmp_path, "experiments", "StillBad.py")
    _make_sidecar(bad)
    issues = lint_project(tmp_path)
    assert [i.path for i in issues if i.issue == "naming"] == [bad]


def test_exempt_py_in_experiments_still_enforces_missing_sidecar(tmp_path):
    (tmp_path / ".bth.toml").write_text(
        '[project]\nslug = "test"\nroot = "."\n\n'
        "[lint.naming]\n"
        'exempt = ["LegacyRun.py"]\n'
    )
    _make_script(tmp_path, "experiments", "LegacyRun.py")

    issues = lint_project(tmp_path)
    assert not any(i.issue == "naming" for i in issues)
    assert [i.issue for i in issues] == ["missing_sidecar"]
    assert issues[0].severity == IssueSeverity.ERROR


def test_exempt_dirs_disables_naming_for_bucket_only(tmp_path):
    (tmp_path / ".bth.toml").write_text(
        '[project]\nslug = "test"\nroot = "."\n\n'
        "[lint.naming]\n"
        'exempt_dirs = ["debug", "scripts/scratch"]\n'
    )
    _make_script(tmp_path, "debug", "badname.py")
    _make_script(tmp_path, "scratch", "notes.txt")
    assert lint_project(tmp_path) == []

    # Other directories remain enforced
    bad_exp = _make_script(tmp_path, "experiments", "BadName.py")
    _make_sidecar(bad_exp)
    issues = lint_project(tmp_path)
    assert [i.path for i in issues if i.issue == "naming"] == [bad_exp]


def test_per_dir_overrides_extensions_pattern_and_sidecar(tmp_path):
    (tmp_path / ".bth.toml").write_text(
        '[project]\nslug = "test"\nroot = "."\n\n'
        "[lint.naming.slurm]\n"
        'extra_extensions = [".sh"]\n'
        'exempt = ["legacy.sh"]\n\n'
        "[lint.naming.experiments]\n"
        'pattern = "^exp_\\\\d+_[a-z]+$"\n'
        'sidecar = "none"\n\n'
        "[lint.naming.analysis]\n"
        "enabled = false\n"
    )
    _make_script(tmp_path, "slurm", "run_job.sh")
    _make_script(tmp_path, "slurm", "legacy.sh")
    _make_script(tmp_path, "experiments", "exp_01_baseline.py")
    _make_script(tmp_path, "analysis", "AnythingGoes.py")
    assert lint_project(tmp_path) == []

    # Custom pattern rejects non-matching stem
    bad_exp = _make_script(tmp_path, "experiments", "run_nvt.py")
    issues = lint_project(tmp_path)
    assert len(issues) == 1
    assert issues[0].issue == "naming"
    assert issues[0].path == bad_exp
    assert "exp_" in issues[0].detail


def test_per_dir_extensions_replacement_rejects_old_extension(tmp_path):
    (tmp_path / ".bth.toml").write_text(
        '[project]\nslug = "test"\nroot = "."\n\n'
        "[lint.naming.slurm]\n"
        'extensions = [".sh"]\n'
    )
    bad = _make_script(tmp_path, "slurm", "run_job.slurm")
    issues = lint_project(tmp_path)
    assert len(issues) == 1
    assert issues[0].issue == "naming"
    assert issues[0].path == bad


def test_invalid_lint_naming_config_emits_error_issue(tmp_path):
    # Unknown key + invalid regex + unknown exempt_dirs
    (tmp_path / ".bth.toml").write_text(
        '[project]\nslug = "test"\nroot = "."\n\n'
        "[lint.naming]\n"
        'exmept = ["foo.py"]\n'
        'exempt_dirs = ["nonexistent_bucket"]\n\n'
        "[lint.naming.experiments]\n"
        'pattern = "[unclosed"\n'
        'sidecar = "bogus"\n'
    )
    issues = lint_project(tmp_path)
    cfg_issues = [i for i in issues if i.issue == "invalid_lint_config"]
    assert len(cfg_issues) == 4
    assert all(i.severity == IssueSeverity.ERROR for i in cfg_issues)


def test_malformed_bth_toml_with_lint_naming_emits_error(tmp_path):
    (tmp_path / ".bth.toml").write_text("[lint.naming\nbroken = [")
    issues = lint_project(tmp_path)
    assert [i.issue for i in issues] == ["invalid_lint_config"]
    assert issues[0].severity == IssueSeverity.ERROR


def test_cli_lint_honors_bth_toml_naming_exemptions(tmp_path, monkeypatch, bathos_test_home):
    from bathos.cli_cyclopts import app

    assert Path.home() == bathos_test_home
    catalog_dir = tmp_path / ".bth" / "catalog"
    catalog_dir.mkdir(parents=True)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
    monkeypatch.chdir(tmp_path)

    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "test"\nroot = "{tmp_path}"\n\n'
        "[lint.naming]\n"
        'exempt = ["scripts/experiments/LegacyRun.py"]\n'
    )
    s = _make_script(tmp_path, "experiments", "LegacyRun.py")
    _make_sidecar(s)

    res = runner.invoke(app, ["lint", "--project-root", str(tmp_path)])
    assert res.exit_code == 0
    assert "No issues" in res.output
