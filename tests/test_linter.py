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


def test_cli_lint_exits_0_with_canonical_warnings(tmp_path, monkeypatch):
    """Advisory lint warns on non-canonical stage_name but always exits 0."""
    from datetime import UTC, datetime
    from bathos.catalog import init_catalog, write_run
    from bathos.compact import compact
    from bathos.schema import Run

    monkeypatch.chdir(tmp_path)
    # Create catalog with advisory issue (non-canonical stage_name)
    catalog_dir = tmp_path / ".bth" / "catalog"
    catalog_dir.mkdir(parents=True)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

    init_catalog(catalog_dir)

    # Create a run with non-canonical stage_name
    r = Run(
        project_slug="test",
        command="python test.py",
        argv=["python", "test.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime.now(UTC),
        status="completed",
        exit_code=0,
        outcome="pass",
        stage_name="custom-stage",  # Non-canonical but valid format
    )
    write_run(r, catalog_dir)

    # Compact to create the bathos.db warm catalog
    compact(catalog_dir)

    # Also create a script to avoid naming issues
    s = _make_script(tmp_path, "experiments", "run_test.py")
    _make_sidecar(s)

    from bathos.cli import app
    result = runner.invoke(app, ["lint"])
    # Must exit 0 (advisory, never blocks)
    assert result.exit_code == 0
    # Must contain warning about canonical stage
    assert "warning" in result.output.lower() or "non_canonical_stage_name" in result.output.lower()


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


def test_check_canonical_stage_names_warns_non_canonical(tmp_path):
    """Test that check_canonical_stage_names warns on non-canonical stage_name values."""
    from datetime import UTC, datetime
    from bathos.catalog import init_catalog, write_run
    from bathos.compact import compact
    from bathos.linter import check_canonical_stage_names, IssueSeverity
    from bathos.schema import Run

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    init_catalog(catalog_dir)

    # Create runs with various stage_name values
    base_time = datetime.now(UTC)

    # Canonical stages (should not warn)
    for stage in ["exploration", "calibration", "validation", "ablation", "production"]:
        r = Run(
            project_slug="test",
            command="python test.py",
            argv=["python", "test.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            timestamp=base_time,
            status="completed",
            exit_code=0,
            stage_name=stage,
        )
        write_run(r, catalog_dir)

    # Non-canonical stages with VALID format (should warn on canonical, not format)
    for stage in ["exploration-phase", "custom-stage", "my-stage"]:
        r = Run(
            project_slug="test",
            command="python test.py",
            argv=["python", "test.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            timestamp=base_time,
            status="completed",
            exit_code=0,
            stage_name=stage,
        )
        write_run(r, catalog_dir)

    # Invalid format stages (should warn on format violation)
    for stage in ["VALIDATION", "validation_final"]:
        r = Run(
            project_slug="test",
            command="python test.py",
            argv=["python", "test.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            timestamp=base_time,
            status="completed",
            exit_code=0,
            stage_name=stage,
        )
        write_run(r, catalog_dir)

    # NULL stage_name (should not warn)
    r = Run(
        project_slug="test",
        command="python test.py",
        argv=["python", "test.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=base_time,
        status="completed",
        exit_code=0,
        stage_name=None,
    )
    write_run(r, catalog_dir)

    compact(catalog_dir)

    issues = check_canonical_stage_names(catalog_dir)
    assert len(issues) >= 5, f"Expected at least 5 warnings, got {len(issues)}"

    # Separate format violations from canonical violations
    format_issues = [i for i in issues if i.issue == "invalid_stage_name_format"]
    canonical_issues = [i for i in issues if i.issue == "non_canonical_stage_name"]

    # Should have 2 format violations (VALIDATION, validation_final)
    assert len(format_issues) == 2, f"Expected 2 format violations, got {len(format_issues)}"
    # Should have 3 canonical violations (exploration-phase, custom-stage, my-stage)
    assert len(canonical_issues) == 3, f"Expected 3 canonical violations, got {len(canonical_issues)}"
    assert all(i.severity == IssueSeverity.WARNING for i in issues)


def test_check_canonical_stage_names_all_advisory_passes(tmp_path):
    """Test that check_canonical_stage_names returns empty for canonical stages."""
    from datetime import UTC, datetime
    from bathos.catalog import init_catalog, write_run
    from bathos.compact import compact
    from bathos.linter import check_canonical_stage_names
    from bathos.schema import Run

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    init_catalog(catalog_dir)

    # Create runs with only canonical stage_name values
    base_time = datetime.now(UTC)
    for i, stage in enumerate(["exploration", "calibration", "validation", "ablation", "production"]):
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
            stage_name=stage,
        )
        write_run(r, catalog_dir)

    compact(catalog_dir)

    issues = check_canonical_stage_names(catalog_dir)
    assert issues == []


def test_check_canonical_stage_names_no_catalog(tmp_path):
    """Test that check_canonical_stage_names returns empty for missing catalog."""
    from bathos.linter import check_canonical_stage_names

    catalog_dir = tmp_path / "nonexistent_catalog"
    issues = check_canonical_stage_names(catalog_dir)
    assert issues == []


def test_check_baseline_ref_exists_finds_baseline(tmp_path):
    """Test that check_baseline_ref_exists detects when baseline_ref exists in catalog."""
    from datetime import UTC, datetime
    from bathos.catalog import init_catalog, write_run
    from bathos.compact import compact
    from bathos.linter import check_baseline_ref_exists, IssueSeverity
    from bathos.schema import Run

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    init_catalog(catalog_dir)

    # Create a baseline run with a known ID
    base_time = datetime.now(UTC)
    baseline_id = "abc12345-1234-1234-1234-123456789abc"
    baseline_run = Run(
        id=baseline_id,
        project_slug="test",
        command="python scripts/benchmarks/baseline.py",
        argv=["python", "scripts/benchmarks/baseline.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=base_time,
        status="completed",
        exit_code=0,
        outcome="pass",
    )
    write_run(baseline_run, catalog_dir)
    compact(catalog_dir)

    # Create a benchmark sidecar with baseline_ref pointing to the baseline run
    project_root = tmp_path / "project"
    project_root.mkdir()
    scripts_dir = project_root / "scripts" / "benchmarks"
    scripts_dir.mkdir(parents=True)
    sidecar_path = scripts_dir / "test_bench.bth.toml"
    sidecar_path.write_text(f"""[benchmark]
baseline_ref = "{baseline_id}"
metric = "ns_per_day"
regression_threshold = 0.05
target = "50 ns/day"

[result_schema]
ns_per_day = "float"
""")

    issues = check_baseline_ref_exists(project_root, catalog_dir, catalog_dir / "bathos.db")

    # Should find the baseline and emit INFO
    assert len(issues) > 0
    # Should have an issue mentioning the baseline_ref and it was found
    assert any("baseline_ref" in str(i.detail).lower() for i in issues)
    # Verify the severity is INFO (not WARNING)
    assert all(i.severity == IssueSeverity.INFO for i in issues)


def test_check_baseline_ref_exists_missing_baseline(tmp_path):
    """Test that check_baseline_ref_exists warns when baseline_ref is not found."""
    from bathos.catalog import init_catalog
    from bathos.compact import compact
    from bathos.linter import check_baseline_ref_exists, IssueSeverity

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    init_catalog(catalog_dir)
    compact(catalog_dir)

    # Create a benchmark sidecar with non-existent baseline_ref
    project_root = tmp_path / "project"
    project_root.mkdir()
    scripts_dir = project_root / "scripts" / "benchmarks"
    scripts_dir.mkdir(parents=True)
    sidecar_path = scripts_dir / "test_bench.bth.toml"
    sidecar_path.write_text("""[benchmark]
baseline_ref = "nonexistent-id"
metric = "ns_per_day"
regression_threshold = 0.05
target = "50 ns/day"

[result_schema]
ns_per_day = "float"
""")

    issues = check_baseline_ref_exists(project_root, catalog_dir, catalog_dir / "bathos.db")

    # Should emit a WARNING about missing baseline
    warning_issues = [i for i in issues if i.severity == IssueSeverity.WARNING]
    assert len(warning_issues) > 0
    assert any("not found" in str(i.detail).lower() for i in warning_issues)


def test_check_baseline_ref_exists_empty_baseline_ref(tmp_path):
    """Test that check_baseline_ref_exists skips empty baseline_ref."""
    from bathos.catalog import init_catalog
    from bathos.compact import compact
    from bathos.linter import check_baseline_ref_exists

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    init_catalog(catalog_dir)
    compact(catalog_dir)

    # Create a benchmark sidecar with empty baseline_ref
    project_root = tmp_path / "project"
    project_root.mkdir()
    scripts_dir = project_root / "scripts" / "benchmarks"
    scripts_dir.mkdir(parents=True)
    sidecar_path = scripts_dir / "test_bench.bth.toml"
    sidecar_path.write_text("""[benchmark]
baseline_ref = ""
metric = "ns_per_day"
regression_threshold = 0.05
target = "50 ns/day"

[result_schema]
ns_per_day = "float"
""")

    issues = check_baseline_ref_exists(project_root, catalog_dir, catalog_dir / "bathos.db")

    # Should return empty issues (empty baseline_ref is skipped)
    assert issues == []


def test_check_baseline_ref_exists_no_catalog(tmp_path):
    """Test that check_baseline_ref_exists skips silently if no warm DB."""
    from bathos.linter import check_baseline_ref_exists

    catalog_dir = tmp_path / "nonexistent_catalog"
    project_root = tmp_path / "project"
    project_root.mkdir()
    scripts_dir = project_root / "scripts" / "benchmarks"
    scripts_dir.mkdir(parents=True)
    sidecar_path = scripts_dir / "test_bench.bth.toml"
    sidecar_path.write_text("""[benchmark]
baseline_ref = "some-id"
metric = "ns_per_day"
regression_threshold = 0.05

[result_schema]
ns_per_day = "float"
""")

    issues = check_baseline_ref_exists(project_root, catalog_dir, catalog_dir / "bathos.db")

    # Should skip silently (no DB exists)
    assert issues == []


def test_check_baseline_ref_exists_prefix_match(tmp_path):
    """Test that check_baseline_ref_exists matches short UUID prefixes."""
    from datetime import UTC, datetime
    from bathos.catalog import init_catalog, write_run
    from bathos.compact import compact
    from bathos.linter import check_baseline_ref_exists, IssueSeverity
    from bathos.schema import Run

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    init_catalog(catalog_dir)

    # Create a baseline run with a known full UUID
    base_time = datetime.now(UTC)
    baseline_id = "abc12345-1234-1234-1234-123456789abc"
    baseline_run = Run(
        id=baseline_id,
        project_slug="test",
        command="python scripts/benchmarks/baseline.py",
        argv=["python", "scripts/benchmarks/baseline.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=base_time,
        status="completed",
        exit_code=0,
        outcome="pass",
    )
    write_run(baseline_run, catalog_dir)
    compact(catalog_dir)

    # Create a benchmark sidecar using only the prefix of the baseline_id
    project_root = tmp_path / "project"
    project_root.mkdir()
    scripts_dir = project_root / "scripts" / "benchmarks"
    scripts_dir.mkdir(parents=True)
    sidecar_path = scripts_dir / "test_bench.bth.toml"
    sidecar_path.write_text("""[benchmark]
baseline_ref = "abc123"
metric = "ns_per_day"
regression_threshold = 0.05
target = "50 ns/day"

[result_schema]
ns_per_day = "float"
""")

    issues = check_baseline_ref_exists(project_root, catalog_dir, catalog_dir / "bathos.db")

    # Should find the baseline by prefix match and emit INFO
    assert len(issues) == 1
    assert issues[0].severity == IssueSeverity.INFO
    assert "baseline_ref_ok" == issues[0].issue


def test_check_novel_or_reproduces_declared_passes_exploration(tmp_path):
    """AC-7: exploration stage experiments are not checked."""
    from bathos.linter import check_novel_or_reproduces_declared, IssueSeverity

    s = _make_script(tmp_path, "experiments", "run_test.py")
    sidecar_path = s.with_suffix(".bth.toml")
    sidecar_path.write_text("""[experiment]
hypothesis = "test"
stage_name = "exploration"
novel = false

[result_schema]
x = "float"

[outcomes.pass]
condition = "x > 0"
decision = "good"
""")

    issues = check_novel_or_reproduces_declared(tmp_path)
    assert all(i.issue != "NOVEL_OR_REPRODUCES_REQUIRED" for i in issues)


def test_check_novel_or_reproduces_declared_validation_requires_reproduction(tmp_path):
    """AC-7: validation stage experiments must declare [reproduction] or novel=true."""
    from bathos.linter import check_novel_or_reproduces_declared, IssueSeverity

    s = _make_script(tmp_path, "experiments", "run_test.py")
    sidecar_path = s.with_suffix(".bth.toml")
    sidecar_path.write_text("""[experiment]
hypothesis = "test"
stage_name = "validation"
novel = false

[result_schema]
x = "float"

[outcomes.pass]
condition = "x > 0"
decision = "good"
""")

    issues = check_novel_or_reproduces_declared(tmp_path)
    novel_issues = [i for i in issues if i.issue == "NOVEL_OR_REPRODUCES_REQUIRED"]
    assert len(novel_issues) == 1
    assert novel_issues[0].severity == IssueSeverity.ERROR


def test_check_novel_or_reproduces_declared_passes_with_novel_true(tmp_path):
    """AC-7: validation stage with novel=true passes."""
    from bathos.linter import check_novel_or_reproduces_declared

    s = _make_script(tmp_path, "experiments", "run_test.py")
    sidecar_path = s.with_suffix(".bth.toml")
    sidecar_path.write_text("""[experiment]
hypothesis = "test"
stage_name = "validation"
novel = true

[result_schema]
x = "float"

[outcomes.pass]
condition = "x > 0"
decision = "good"
""")

    issues = check_novel_or_reproduces_declared(tmp_path)
    novel_issues = [i for i in issues if i.issue == "NOVEL_OR_REPRODUCES_REQUIRED"]
    assert len(novel_issues) == 0


def test_check_novel_or_reproduces_declared_passes_with_reproduction_paper(tmp_path):
    """AC-7: validation stage with [reproduction].reproduces_paper passes."""
    from bathos.linter import check_novel_or_reproduces_declared

    s = _make_script(tmp_path, "experiments", "run_test.py")
    sidecar_path = s.with_suffix(".bth.toml")
    sidecar_path.write_text("""[experiment]
hypothesis = "test"
stage_name = "validation"
novel = false

[reproduction]
reproduces_paper = "10.1234/example"

[result_schema]
x = "float"

[outcomes.pass]
condition = "x > 0"
decision = "good"
""")

    issues = check_novel_or_reproduces_declared(tmp_path)
    novel_issues = [i for i in issues if i.issue == "NOVEL_OR_REPRODUCES_REQUIRED"]
    assert len(novel_issues) == 0


def test_check_novel_or_reproduces_declared_passes_with_reproduction_run(tmp_path):
    """AC-7: validation stage with [reproduction].reproduces_run passes."""
    from bathos.linter import check_novel_or_reproduces_declared

    s = _make_script(tmp_path, "experiments", "run_test.py")
    sidecar_path = s.with_suffix(".bth.toml")
    sidecar_path.write_text("""[experiment]
hypothesis = "test"
stage_name = "validation"
novel = false

[reproduction]
reproduces_run = "12345678-1234-1234-1234-123456789012"

[result_schema]
x = "float"

[outcomes.pass]
condition = "x > 0"
decision = "good"
""")

    issues = check_novel_or_reproduces_declared(tmp_path)
    novel_issues = [i for i in issues if i.issue == "NOVEL_OR_REPRODUCES_REQUIRED"]
    assert len(novel_issues) == 0


def test_check_novel_or_reproduces_declared_production_requires_reproduction(tmp_path):
    """AC-7: production stage experiments must declare [reproduction] or novel=true."""
    from bathos.linter import check_novel_or_reproduces_declared, IssueSeverity

    s = _make_script(tmp_path, "experiments", "run_test.py")
    sidecar_path = s.with_suffix(".bth.toml")
    sidecar_path.write_text("""[experiment]
hypothesis = "test"
stage_name = "production"
novel = false

[result_schema]
x = "float"

[outcomes.pass]
condition = "x > 0"
decision = "good"
""")

    issues = check_novel_or_reproduces_declared(tmp_path)
    novel_issues = [i for i in issues if i.issue == "NOVEL_OR_REPRODUCES_REQUIRED"]
    assert len(novel_issues) == 1
    assert novel_issues[0].severity == IssueSeverity.ERROR


def test_check_novel_or_reproduces_declared_ignores_benchmarks(tmp_path):
    """AC-7: benchmark sidecars are not checked (only experiments)."""
    from bathos.linter import check_novel_or_reproduces_declared

    s = _make_script(tmp_path, "experiments", "bench_test.py")
    sidecar_path = s.with_suffix(".bth.toml")
    sidecar_path.write_text("""[benchmark]
baseline_ref = "12345678"
metric = "ns_per_day"
target = "50 ns/day"

[result_schema]
ns_per_day = "float"
""")

    issues = check_novel_or_reproduces_declared(tmp_path)
    novel_issues = [i for i in issues if i.issue == "NOVEL_OR_REPRODUCES_REQUIRED"]
    assert len(novel_issues) == 0


def test_check_novel_or_reproduces_declared_ignores_validation_scripts(tmp_path):
    """AC-7: validation/ dir scripts (SidecarKind.VALIDATION) are not checked (only EXPERIMENT)."""
    from bathos.linter import check_novel_or_reproduces_declared

    s = _make_script(tmp_path, "validation", "check_energy.py")
    sidecar_path = s.with_suffix(".bth.toml")
    sidecar_path.write_text("""[validation]
property = "energy conservation"
reference = "+-1 kcal/mol"
tolerance = "1"

[result_schema]
energy_error = "float"
""")

    issues = check_novel_or_reproduces_declared(tmp_path)
    novel_issues = [i for i in issues if i.issue == "NOVEL_OR_REPRODUCES_REQUIRED"]
    assert len(novel_issues) == 0


def test_check_novel_or_reproduces_declared_fails_empty_reproduction_block(tmp_path):
    """AC-7: validation stage with empty string fields in [reproduction] block fails.

    Tests the case where a [reproduction] block is present but both reproduces_paper
    and reproduces_run are empty strings (falsy). This must still trigger
    NOVEL_OR_REPRODUCES_REQUIRED since empty strings are not valid declarations.
    """
    from bathos.linter import check_novel_or_reproduces_declared, IssueSeverity

    s = _make_script(tmp_path, "experiments", "run_test.py")
    sidecar_path = s.with_suffix(".bth.toml")
    sidecar_path.write_text("""[experiment]
hypothesis = "test"
stage_name = "validation"
novel = false

[reproduction]
reproduces_paper = ""
reproduces_run = ""

[result_schema]
x = "float"

[outcomes.pass]
condition = "x > 0"
decision = "good"
""")

    issues = check_novel_or_reproduces_declared(tmp_path)
    novel_issues = [i for i in issues if i.issue == "NOVEL_OR_REPRODUCES_REQUIRED"]
    assert len(novel_issues) == 1, f"Expected 1 issue, got {len(novel_issues)}: {novel_issues}"
    assert novel_issues[0].severity == IssueSeverity.ERROR


def test_check_novel_or_reproduces_declared_fails_completely_empty_reproduction_block(tmp_path):
    """AC-7: validation stage with completely empty [reproduction] block fails.

    Tests the case where a [reproduction] block exists but has no keys at all.
    This covers the falsy-dict path where sidecar.reproduction exists but all
    its fields are empty/default, making the has_reproduction check fail.
    """
    from bathos.linter import check_novel_or_reproduces_declared, IssueSeverity

    s = _make_script(tmp_path, "experiments", "run_test.py")
    sidecar_path = s.with_suffix(".bth.toml")
    sidecar_path.write_text("""[experiment]
hypothesis = "test"
stage_name = "validation"
novel = false

[reproduction]

[result_schema]
x = "float"

[outcomes.pass]
condition = "x > 0"
decision = "good"
""")

    issues = check_novel_or_reproduces_declared(tmp_path)
    novel_issues = [i for i in issues if i.issue == "NOVEL_OR_REPRODUCES_REQUIRED"]
    assert len(novel_issues) == 1, f"Expected 1 issue, got {len(novel_issues)}: {novel_issues}"
    assert novel_issues[0].severity == IssueSeverity.ERROR
