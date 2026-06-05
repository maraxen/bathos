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
    # Isolate from user's real catalog to prevent warm-tier warnings leaking in
    catalog_dir = tmp_path / ".bth" / "catalog"
    catalog_dir.mkdir(parents=True)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
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


def test_check_residual_rates_detects_high_rate(tmp_path):
    """Test that check_residual_rates detects high residual rates in uncampaigned runs."""
    from datetime import UTC, datetime
    import duckdb
    from bathos.catalog import init_catalog, write_run
    from bathos.compact import compact
    from bathos.linter import check_residual_rates
    from bathos.schema import Run

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    init_catalog(catalog_dir)

    # Create runs where some are residual
    base_time = datetime.now(UTC)
    for i in range(10):
        r = Run(
            project_slug="test",
            command="python test.py",
            argv=["python", "test.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            timestamp=base_time + __import__("datetime").timedelta(seconds=i),
            status="completed",
            exit_code=0,
            outcome="pass",
            outcome_is_residual=i < 2,  # 2/10 = 20% residual
        )
        write_run(r, catalog_dir)

    compact(catalog_dir)

    # Manually set outcome in warm DB to test the check
    db_path = catalog_dir / "bathos.db"
    db = duckdb.connect(str(db_path))
    db.execute("UPDATE runs SET outcome = 'pass' WHERE outcome IS NULL OR outcome = ''")
    db.close()

    issues = check_residual_rates(catalog_dir, threshold=0.10)
    assert len(issues) > 0
    assert any("high_residual_rate" in str(i.issue) for i in issues)


def test_check_bypass_trend_returns_empty_for_no_data(tmp_path):
    """Test that check_bypass_trend returns empty list for empty/missing catalog."""
    from bathos.linter import check_bypass_trend

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    issues = check_bypass_trend(catalog_dir)
    assert issues == []


def test_check_unfired_branches_detects_single_outcome(tmp_path):
    """Test that check_unfired_branches detects branches with single outcome."""
    from datetime import UTC, datetime
    import duckdb
    from bathos.catalog import init_catalog, write_run
    from bathos.compact import compact
    from bathos.linter import check_unfired_branches
    from bathos.schema import Run

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    init_catalog(catalog_dir)

    # Create 6 runs with same command, same sidecar_sha256, same outcome
    base_time = datetime.now(UTC)
    for i in range(6):
        r = Run(
            project_slug="test",
            command="python test_stable.py",
            argv=["python", "test_stable.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            timestamp=base_time + __import__("datetime").timedelta(seconds=i),
            status="completed",
            exit_code=0,
            outcome="pass",
            sidecar_sha256="sha256_abc",
        )
        write_run(r, catalog_dir)

    compact(catalog_dir)

    # Manually set outcome in warm DB (compact doesn't preserve it)
    db_path = catalog_dir / "bathos.db"
    db = duckdb.connect(str(db_path))
    db.execute("UPDATE runs SET outcome = 'pass'")
    db.close()

    issues = check_unfired_branches(catalog_dir, min_runs=5)
    assert len(issues) > 0
    assert any("single_outcome_branch_fired" in str(i.issue) for i in issues)


def test_check_ephemeral_output_paths_detects_tmp(tmp_path):
    """Test that check_ephemeral_output_paths detects /tmp output paths in catalog."""
    from datetime import UTC, datetime
    from bathos.catalog import init_catalog, write_run
    from bathos.compact import compact
    from bathos.linter import check_ephemeral_output_paths
    from bathos.schema import Run

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    init_catalog(catalog_dir)

    base_time = datetime.now(UTC)
    r = Run(
        project_slug="test",
        command="python train.py",
        argv=["python", "train.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=base_time,
        status="completed",
        exit_code=0,
        output_paths=["/tmp/result.json"],
    )
    write_run(r, catalog_dir)
    compact(catalog_dir)

    issues = check_ephemeral_output_paths(catalog_dir)
    assert len(issues) > 0
    assert any(i.issue == "ephemeral_output_path" for i in issues)


def test_check_ephemeral_output_paths_clean_for_persistent(tmp_path):
    """Test that check_ephemeral_output_paths passes for persistent output paths."""
    from datetime import UTC, datetime
    from bathos.catalog import init_catalog, write_run
    from bathos.compact import compact
    from bathos.linter import check_ephemeral_output_paths
    from bathos.schema import Run

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    init_catalog(catalog_dir)

    base_time = datetime.now(UTC)
    r = Run(
        project_slug="test",
        command="python train.py",
        argv=["python", "train.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=base_time,
        status="completed",
        exit_code=0,
        output_paths=["/home/user/projects/myproject/outputs/result.json"],
    )
    write_run(r, catalog_dir)
    compact(catalog_dir)

    issues = check_ephemeral_output_paths(catalog_dir)
    assert issues == []
