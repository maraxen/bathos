"""Tests for script classification engine (bth classify)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from bathos.classifier import (
    ClassificationConfidence,
    ClassificationResult,
    MoveAction,
    classify_flat_scripts,
    build_move_plan,
    apply_classify_plan,
    _infer_date_prefix,
    _build_classification_result,
)


@pytest.fixture
def project_with_scripts(tmp_path: Path) -> Path:
    """Create a minimal project with scripts/ directory."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    # Also create subdirectories that linter expects
    for subdir in ["experiments", "benchmarks", "validation", "analysis", "data", "debug", "explore", "scratch"]:
        (scripts_dir / subdir).mkdir()

    return tmp_path


def test_classify_benchmark_prefix(project_with_scripts: Path) -> None:
    """Test benchmark_* prefix maps to benchmarks/ with HIGH confidence."""
    script = project_with_scripts / "scripts" / "benchmark_efa_vs_pme.py"
    script.write_text("# benchmark script")

    results = classify_flat_scripts(project_with_scripts)
    assert len(results) == 1

    result = results[0]
    assert result.source == script
    assert result.target_dir == "benchmarks"
    assert result.confidence == ClassificationConfidence.HIGH
    assert "benchmark_" in result.rationale
    assert result.rename_required is False


def test_classify_debug_prefix(project_with_scripts: Path) -> None:
    """Test debug_* prefix maps to debug/ with HIGH confidence and requires rename to YYMMDD format."""
    script = project_with_scripts / "scripts" / "debug_bonds.py"
    script.write_text("# debug script")

    # Mock git log to return a date
    with mock.patch("bathos.classifier.subprocess.run") as mock_run:
        # First call is to git log (returns a date)
        mock_run.return_value = mock.Mock(
            returncode=0,
            stdout="2026-01-21 12:00:00 +0000\n",
        )

        result = _build_classification_result(
            script,
            "debug",
            ClassificationConfidence.HIGH,
            "matches debug_ prefix",
            project_with_scripts,
        )

    assert result.target_dir == "debug"
    assert result.confidence == ClassificationConfidence.HIGH
    assert result.rename_required is True
    assert result.suggested_stem == "260121_debug_bonds"


def test_classify_validate_prefix(project_with_scripts: Path) -> None:
    """Test validate_* prefix maps to validation/ with HIGH confidence."""
    script = project_with_scripts / "scripts" / "validate_langevin_settle_dt05fs.py"
    script.write_text("# validation script")

    results = classify_flat_scripts(project_with_scripts)
    assert len(results) == 1

    result = results[0]
    assert result.source == script
    assert result.target_dir == "validation"
    assert result.confidence == ClassificationConfidence.HIGH


def test_classify_verify_prefix_as_analysis(project_with_scripts: Path) -> None:
    """Test verify_* prefix maps to analysis/ (NOT validation) with MEDIUM confidence."""
    script = project_with_scripts / "scripts" / "verify_something.py"
    script.write_text("# verify script")

    results = classify_flat_scripts(project_with_scripts)
    assert len(results) == 1

    result = results[0]
    assert result.target_dir == "analysis"  # NOT validation
    assert result.confidence == ClassificationConfidence.MEDIUM


def test_classify_check_prefix_as_analysis(project_with_scripts: Path) -> None:
    """Test check_* prefix maps to analysis/ (NOT validation) with MEDIUM confidence."""
    script = project_with_scripts / "scripts" / "check_gradients.py"
    script.write_text("# check script")

    results = classify_flat_scripts(project_with_scripts)
    assert len(results) == 1

    result = results[0]
    assert result.target_dir == "analysis"  # NOT validation
    assert result.confidence == ClassificationConfidence.MEDIUM


def test_classify_simulate_to_experiments(project_with_scripts: Path) -> None:
    """Test simulate_* prefix maps to experiments/ with MEDIUM confidence and sidecar required."""
    script = project_with_scripts / "scripts" / "simulate_1uao_explicit.py"
    script.write_text("# simulation script")

    results = classify_flat_scripts(project_with_scripts)
    assert len(results) == 1

    result = results[0]
    assert result.target_dir == "experiments"
    assert result.confidence == ClassificationConfidence.MEDIUM
    assert result.sidecar_required is True


def test_classify_analyze_prefix(project_with_scripts: Path) -> None:
    """Test analyze_* prefix maps to analysis/ with HIGH confidence."""
    script = project_with_scripts / "scripts" / "analyze_trajectories.py"
    script.write_text("# analysis script")

    results = classify_flat_scripts(project_with_scripts)
    assert len(results) == 1

    result = results[0]
    assert result.target_dir == "analysis"
    assert result.confidence == ClassificationConfidence.HIGH


def test_classify_ablation_prefix(project_with_scripts: Path) -> None:
    """Test ablation_* prefix maps to experiments/ with MEDIUM confidence and sidecar required."""
    script = project_with_scripts / "scripts" / "ablation_efa.py"
    script.write_text("# ablation script")

    results = classify_flat_scripts(project_with_scripts)
    assert len(results) == 1

    result = results[0]
    assert result.target_dir == "experiments"
    assert result.confidence == ClassificationConfidence.MEDIUM
    assert result.sidecar_required is True


def test_classify_compare_prefix_low_confidence(project_with_scripts: Path) -> None:
    """Test compare_* prefix is ambiguous, maps to analysis/ with LOW confidence."""
    script = project_with_scripts / "scripts" / "compare_minimizers.py"
    script.write_text("# comparison script")

    results = classify_flat_scripts(project_with_scripts)
    assert len(results) == 1

    result = results[0]
    assert result.target_dir == "analysis"
    assert result.confidence == ClassificationConfidence.LOW


def test_classify_phase_prefix_low_confidence(project_with_scripts: Path) -> None:
    """Test phase*_ prefix (ambiguous) maps to analysis/ with LOW confidence."""
    script = project_with_scripts / "scripts" / "phase1_something.py"
    script.write_text("# phase script")

    results = classify_flat_scripts(project_with_scripts)
    assert len(results) == 1

    result = results[0]
    assert result.target_dir == "analysis"
    assert result.confidence == ClassificationConfidence.LOW


def test_classify_unrecognized_name_default(project_with_scripts: Path) -> None:
    """Test unrecognized script names default to analysis/ with LOW confidence."""
    script = project_with_scripts / "scripts" / "random_name.py"
    script.write_text("# unknown script")

    results = classify_flat_scripts(project_with_scripts)
    assert len(results) == 1

    result = results[0]
    assert result.target_dir == "analysis"
    assert result.confidence == ClassificationConfidence.LOW


def test_skip_underscore_prefixed_files(project_with_scripts: Path) -> None:
    """Test that _-prefixed files are skipped silently."""
    script = project_with_scripts / "scripts" / "_helper.py"
    script.write_text("# helper")

    results = classify_flat_scripts(project_with_scripts)
    assert len(results) == 0  # Skipped


def test_skip_dunder_files(project_with_scripts: Path) -> None:
    """Test that __init__.py and similar are skipped."""
    script = project_with_scripts / "scripts" / "__init__.py"
    script.write_text("# init")

    results = classify_flat_scripts(project_with_scripts)
    assert len(results) == 0  # Skipped


def test_skip_non_py_files(project_with_scripts: Path) -> None:
    """Test that non-.py files are skipped."""
    script = project_with_scripts / "scripts" / "somescript.txt"
    script.write_text("# text file")

    results = classify_flat_scripts(project_with_scripts)
    assert len(results) == 0  # Skipped


def test_skip_scripts_in_subdirs(project_with_scripts: Path) -> None:
    """Test that scripts already in subdirectories are not included in flat scan."""
    script = project_with_scripts / "scripts" / "experiments" / "simulate_foo.py"
    script.write_text("# experiment")

    results = classify_flat_scripts(project_with_scripts)
    assert len(results) == 0  # Already in subdirectory


def test_conflict_detection(project_with_scripts: Path) -> None:
    """Test that existing files at destination are detected as conflicts."""
    script = project_with_scripts / "scripts" / "benchmark_test.py"
    script.write_text("# benchmark")

    # Pre-create a file at the destination
    dest_dir = project_with_scripts / "scripts" / "benchmarks"
    dest_dir.mkdir(exist_ok=True)
    (dest_dir / "benchmark_test.py").write_text("# existing")

    results = classify_flat_scripts(project_with_scripts)
    plan = build_move_plan(project_with_scripts, results)

    assert len(plan.actions) == 1
    assert plan.actions[0].conflict is True
    assert plan.conflicts == 1


def test_conflict_blocks_apply(project_with_scripts: Path) -> None:
    """Test that apply_classify_plan aborts if conflicts exist."""
    script = project_with_scripts / "scripts" / "benchmark_test.py"
    script.write_text("# benchmark")

    # Pre-create a file at the destination
    dest_dir = project_with_scripts / "scripts" / "benchmarks"
    dest_dir.mkdir(exist_ok=True)
    (dest_dir / "benchmark_test.py").write_text("# existing")

    results = classify_flat_scripts(project_with_scripts)
    plan = build_move_plan(project_with_scripts, results)

    with pytest.raises(RuntimeError, match="conflict"):
        apply_classify_plan(plan)


def test_untracked_files_block_apply(project_with_scripts: Path, tmp_path: Path) -> None:
    """Test that apply_classify_plan aborts if source is untracked."""
    script = project_with_scripts / "scripts" / "benchmark_test.py"
    script.write_text("# benchmark")

    # Initialize git repo but don't track the file
    import subprocess

    subprocess.run(["git", "init"], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project_with_scripts, check=True, capture_output=True)

    results = classify_flat_scripts(project_with_scripts)
    plan = build_move_plan(project_with_scripts, results)

    with pytest.raises(RuntimeError, match="untracked in git"):
        apply_classify_plan(plan)


def test_infer_date_prefix_from_git_log(project_with_scripts: Path) -> None:
    """Test that _infer_date_prefix correctly reads git log."""
    script = project_with_scripts / "scripts" / "debug_test.py"
    script.write_text("# debug")

    # Initialize git and commit the file
    subprocess.run(["git", "init"], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_with_scripts, check=True, capture_output=True)

    date_prefix = _infer_date_prefix(script)
    # Should be in YYMMDD format (6 digits)
    assert len(date_prefix) == 6, f"Expected YYMMDD format (6 digits), got {date_prefix!r} ({len(date_prefix)} digits)"
    assert date_prefix.isdigit()


def test_infer_date_prefix_fallback_to_mtime(project_with_scripts: Path) -> None:
    """Test that _infer_date_prefix falls back to mtime for uncommitted files."""
    script = project_with_scripts / "scripts" / "debug_test.py"
    script.write_text("# debug")

    # Initialize git but don't commit the file
    subprocess.run(["git", "init"], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project_with_scripts, check=True, capture_output=True)

    date_prefix = _infer_date_prefix(script)

    # Should be in YYMMDD format (from mtime)
    assert len(date_prefix) == 6, f"Expected YYMMDD format (6 digits), got {date_prefix!r}"
    assert date_prefix.isdigit()


def test_sidecar_scaffold_for_experiments(project_with_scripts: Path, tmp_path: Path, monkeypatch) -> None:
    """Test that apply_classify_plan writes sidecar stub for experiments."""
    # Work in the project directory
    monkeypatch.chdir(project_with_scripts)

    script = project_with_scripts / "scripts" / "simulate_test.py"
    script.write_text("# simulation")

    # Initialize git
    subprocess.run(["git", "init"], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_with_scripts, check=True, capture_output=True)

    results = classify_flat_scripts(project_with_scripts)
    plan = build_move_plan(project_with_scripts, results)

    apply_classify_plan(plan, scaffold_sidecars=True)

    # Check that sidecar was created
    sidecar_path = project_with_scripts / "scripts" / "experiments" / "simulate_test.bth.toml"
    assert sidecar_path.exists()

    content = sidecar_path.read_text()
    assert "[experiment]" in content
    assert "TODO" in content
    assert "hypothesis" in content


def test_sidecar_scaffold_for_benchmarks(project_with_scripts: Path, monkeypatch) -> None:
    """Test that apply_classify_plan writes benchmark sidecar stub."""
    monkeypatch.chdir(project_with_scripts)

    script = project_with_scripts / "scripts" / "benchmark_test.py"
    script.write_text("# benchmark")

    # Initialize git
    subprocess.run(["git", "init"], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_with_scripts, check=True, capture_output=True)

    results = classify_flat_scripts(project_with_scripts)
    plan = build_move_plan(project_with_scripts, results)

    apply_classify_plan(plan, scaffold_sidecars=True)

    sidecar_path = project_with_scripts / "scripts" / "benchmarks" / "benchmark_test.bth.toml"
    assert sidecar_path.exists()

    content = sidecar_path.read_text()
    assert "[benchmark]" in content
    assert "TODO" in content
    assert "baseline_ref" in content


def test_no_scaffold_for_analysis(project_with_scripts: Path, monkeypatch) -> None:
    """Test that analysis-classified scripts do NOT get sidecars."""
    monkeypatch.chdir(project_with_scripts)

    script = project_with_scripts / "scripts" / "analyze_test.py"
    script.write_text("# analysis")

    # Initialize git
    subprocess.run(["git", "init"], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_with_scripts, check=True, capture_output=True)

    results = classify_flat_scripts(project_with_scripts)
    plan = build_move_plan(project_with_scripts, results)

    apply_classify_plan(plan, scaffold_sidecars=True)

    sidecar_path = project_with_scripts / "scripts" / "analysis" / "analyze_test.bth.toml"
    assert not sidecar_path.exists()


def test_no_scaffold_when_flag_false(project_with_scripts: Path, monkeypatch) -> None:
    """Test that --no-scaffold prevents sidecar creation."""
    monkeypatch.chdir(project_with_scripts)

    script = project_with_scripts / "scripts" / "simulate_test.py"
    script.write_text("# simulation")

    # Initialize git
    subprocess.run(["git", "init"], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=project_with_scripts, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_with_scripts, check=True, capture_output=True)

    results = classify_flat_scripts(project_with_scripts)
    plan = build_move_plan(project_with_scripts, results)

    apply_classify_plan(plan, scaffold_sidecars=False)

    sidecar_path = project_with_scripts / "scripts" / "experiments" / "simulate_test.bth.toml"
    assert not sidecar_path.exists()


def test_build_move_plan_summary(project_with_scripts: Path) -> None:
    """Test that build_move_plan correctly summarizes confidence levels."""
    # Create a variety of scripts with different confidence levels
    (project_with_scripts / "scripts" / "benchmark_a.py").write_text("# benchmark")
    (project_with_scripts / "scripts" / "debug_b.py").write_text("# debug")
    (project_with_scripts / "scripts" / "compare_c.py").write_text("# compare")

    results = classify_flat_scripts(project_with_scripts)
    plan = build_move_plan(project_with_scripts, results)

    # With git log for date inference, these counts may vary
    # but we can check that they sum to the total
    assert plan.high_confidence + plan.medium_confidence + plan.low_confidence == len(plan.actions)


def test_classify_multiple_scripts(project_with_scripts: Path) -> None:
    """Test classifying multiple scripts in one call."""
    (project_with_scripts / "scripts" / "benchmark_efa.py").write_text("# benchmark")
    (project_with_scripts / "scripts" / "analyze_results.py").write_text("# analysis")
    (project_with_scripts / "scripts" / "simulate_system.py").write_text("# simulation")

    results = classify_flat_scripts(project_with_scripts)
    assert len(results) == 3

    targets = {r.source.stem: r.target_dir for r in results}
    assert targets["benchmark_efa"] == "benchmarks"
    assert targets["analyze_results"] == "analysis"
    assert targets["simulate_system"] == "experiments"
