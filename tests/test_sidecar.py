import textwrap
from pathlib import Path
import pytest


def _write_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "run_test.bth.toml"
    p.write_text(textwrap.dedent(content))
    return p


def test_parse_experiment_sidecar(tmp_path):
    from bathos.sidecar import parse_sidecar, SidecarKind
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "NVT maintains ±5K over 50ps"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "proceed"
        reasoning = "Temperature within tolerance"
        [outcomes.fail]
        condition = "temp_std >= 5"
        decision = "debug"
        reasoning = "Temperature stability issue"
        is_residual = true
        [result_schema]
        temp_std = "float"
    """)
    s = parse_sidecar(path)
    assert s.kind == SidecarKind.EXPERIMENT
    assert s.hypothesis == "NVT maintains ±5K over 50ps"
    assert "pass" in s.outcomes
    assert s.outcomes["pass"].condition == "temp_std < 5"
    assert s.result_schema == {"temp_std": "float"}


def test_parse_benchmark_sidecar(tmp_path):
    from bathos.sidecar import parse_sidecar, SidecarKind
    path = _write_toml(tmp_path, """
        [benchmark]
        baseline_ref = "run_abc123"
        metric = "ns_per_day"
        regression_threshold = 0.05
        target = "> 50 ns/day"
        [result_schema]
        ns_per_day = "float"
    """)
    s = parse_sidecar(path)
    assert s.kind == SidecarKind.BENCHMARK
    assert s.baseline_ref == "run_abc123"
    assert s.regression_threshold == 0.05


def test_parse_sidecar_invalid_toml(tmp_path):
    from bathos.sidecar import SidecarError
    path = tmp_path / "run_test.bth.toml"
    path.write_text("not valid toml ][[[")
    with pytest.raises(SidecarError, match="Failed to parse"):
        from bathos.sidecar import parse_sidecar
        parse_sidecar(path)


def test_find_sidecar_found(tmp_path):
    from bathos.sidecar import find_sidecar
    script = tmp_path / "run_nvt.py"
    script.touch()
    sidecar = tmp_path / "run_nvt.bth.toml"
    sidecar.write_text("[experiment]\nhypothesis='h'\n[result_schema]\n")
    assert find_sidecar(script) == sidecar


def test_find_sidecar_missing(tmp_path):
    from bathos.sidecar import find_sidecar
    script = tmp_path / "run_nvt.py"
    script.touch()
    assert find_sidecar(script) is None


def test_is_in_enforced_dir_true(tmp_path):
    from bathos.sidecar import is_in_enforced_dir
    script = tmp_path / "scripts" / "experiments" / "run_nvt.py"
    script.parent.mkdir(parents=True)
    script.touch()
    assert is_in_enforced_dir(script) is True


def test_is_in_enforced_dir_false(tmp_path):
    from bathos.sidecar import is_in_enforced_dir
    script = tmp_path / "scripts" / "scratch" / "explore_data.py"
    script.parent.mkdir(parents=True)
    script.touch()
    assert is_in_enforced_dir(script) is False


def test_evaluate_outcome_pass(tmp_path):
    from bathos.sidecar import parse_sidecar, evaluate_outcome
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "h"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "proceed"
        reasoning = "Good stability"
        [outcomes.fail]
        condition = "temp_std >= 5"
        decision = "debug"
        reasoning = "Poor stability"
        is_residual = true
        [result_schema]
        temp_std = "float"
    """)
    s = parse_sidecar(path)
    label = evaluate_outcome(s, {"temp_std": 2.1})
    assert label == "pass"


def test_evaluate_outcome_no_match(tmp_path):
    from bathos.sidecar import parse_sidecar, evaluate_outcome
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "h"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Catch-all for unclassified results"
        is_residual = true
        [result_schema]
        temp_std = "float"
    """)
    s = parse_sidecar(path)
    # value that matches no condition (shouldn't happen in well-formed sidecars, but be safe)
    label = evaluate_outcome(s, {})
    assert label == "unknown"


def test_evaluate_outcome_bool_result(tmp_path):
    from bathos.sidecar import parse_sidecar, evaluate_outcome
    path = _write_toml(tmp_path, """
        [debug]
        symptom = "NaN forces"
        suspected_cause = "PME grid"
        verification = "compare box sizes"
        [outcomes.reproduced]
        condition = "reproduced = TRUE"
        decision = "confirmed bug"
        reasoning = "Issue reproducible with smaller box"
        [outcomes.not_reproduced]
        condition = "reproduced = FALSE"
        decision = "environment issue"
        reasoning = "Cannot reproduce with current setup"
        is_residual = true
        [verdict_schema]
        reproduced = "bool"
    """)
    s = parse_sidecar(path)
    label = evaluate_outcome(s, {"reproduced": True})
    assert label == "reproduced"


def test_sidecar_agent_mode_parsed(tmp_path):
    from bathos.sidecar import parse_sidecar
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "Test hypothesis"
        agent_mode = "autonomous"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good value"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Catch-all"
        is_residual = true
        [result_schema]
        value = "float"
    """)
    sidecar = parse_sidecar(path)
    assert sidecar.agent_mode == "autonomous"


def test_sidecar_agent_mode_default(tmp_path):
    from bathos.sidecar import parse_sidecar
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "Test hypothesis"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
    """)
    sidecar = parse_sidecar(path)
    assert sidecar.agent_mode == ""


def test_evaluate_outcome_raises_sidecar_error_on_bad_sql(tmp_path):
    """When outcome condition has invalid SQL, evaluate_outcome raises SidecarError."""
    from bathos.sidecar import parse_sidecar, evaluate_outcome, SidecarError
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "test"
        [outcomes.bad]
        condition = "INVALID SQL ][["
        decision = "bad"
        reasoning = "This condition has invalid SQL"
        is_residual = true
        [result_schema]
        x = "float"
    """)
    s = parse_sidecar(path)
    # Should raise SidecarError because SQL is invalid
    with pytest.raises(SidecarError):
        evaluate_outcome(s, {"x": 1.0})


def test_parse_experiment_sidecar_with_stage_name(tmp_path):
    """Parse stage_name from [experiment] block."""
    from bathos.sidecar import parse_sidecar
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "Test hypothesis"
        stage_name = "validation"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good value"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Catch-all"
        is_residual = true
        [result_schema]
        value = "float"
    """)
    sidecar = parse_sidecar(path)
    assert sidecar.stage_name == "validation"


def test_parse_experiment_sidecar_stage_name_default(tmp_path):
    """When stage_name absent, default to 'exploration'."""
    from bathos.sidecar import parse_sidecar
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "Test hypothesis"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
    """)
    sidecar = parse_sidecar(path)
    assert sidecar.stage_name == "exploration"


def test_parse_experiment_sidecar_stage_name_invalid_coerces_to_exploration(tmp_path, caplog):
    """Invalid stage_name coerces to 'exploration' with warning."""
    import logging
    from bathos.sidecar import parse_sidecar

    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "Test hypothesis"
        stage_name = "invalid-stage-with-numbers-123"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
    """)

    with caplog.at_level(logging.WARNING, logger="bathos.sidecar"):
        sidecar = parse_sidecar(path)

    # Should coerce to exploration
    assert sidecar.stage_name == "exploration"

    # Assert warning was logged
    assert any("invalid-stage-with-numbers-123" in record.message for record in caplog.records)


def test_parse_experiment_sidecar_with_novel(tmp_path):
    """Parse novel field from [experiment] block."""
    from bathos.sidecar import parse_sidecar
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "Test hypothesis"
        novel = true
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
    """)
    sidecar = parse_sidecar(path)
    assert sidecar.novel is True


def test_parse_experiment_sidecar_novel_default(tmp_path):
    """When novel absent, default to False."""
    from bathos.sidecar import parse_sidecar
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "Test hypothesis"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
    """)
    sidecar = parse_sidecar(path)
    assert sidecar.novel is False


def test_parse_experiment_sidecar_with_reproduction_block(tmp_path):
    """Parse [reproduction] block with all four fields."""
    from bathos.sidecar import parse_sidecar
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "Test hypothesis"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
        [reproduction]
        reproduces_paper = "10.1234/example.doi"
        reproduces_run = "12345678-1234-5678-1234-567812345678"
        tolerance_pct = 5.0
        requires_pass_stem = "baseline_run"
    """)
    sidecar = parse_sidecar(path)
    assert sidecar.reproduction is not None
    assert sidecar.reproduction.reproduces_paper == "10.1234/example.doi"
    assert sidecar.reproduction.reproduces_run == "12345678-1234-5678-1234-567812345678"
    assert sidecar.reproduction.tolerance_pct == 5.0
    assert sidecar.reproduction.requires_pass_stem == "baseline_run"


def test_parse_experiment_sidecar_reproduction_block_partial(tmp_path):
    """[reproduction] block with only some fields set."""
    from bathos.sidecar import parse_sidecar
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "Test hypothesis"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
        [reproduction]
        reproduces_paper = "Smith et al. 2023"
    """)
    sidecar = parse_sidecar(path)
    assert sidecar.reproduction is not None
    assert sidecar.reproduction.reproduces_paper == "Smith et al. 2023"
    assert sidecar.reproduction.reproduces_run == ""
    assert sidecar.reproduction.tolerance_pct is None
    assert sidecar.reproduction.requires_pass_stem == ""


def test_parse_experiment_sidecar_reproduction_block_absent(tmp_path):
    """When [reproduction] absent, reproduction is None."""
    from bathos.sidecar import parse_sidecar
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "Test hypothesis"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
    """)
    sidecar = parse_sidecar(path)
    assert sidecar.reproduction is None


def test_parse_experiment_sidecar_reproduction_block_unknown_key_warning(tmp_path, caplog):
    """Unknown keys in [reproduction] emit WARNING."""
    import logging
    from bathos.sidecar import parse_sidecar
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "Test hypothesis"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
        [reproduction]
        reproduces_paper = "test"
        unknown_field = "should warn"
    """)
    with caplog.at_level(logging.WARNING, logger="bathos.sidecar"):
        sidecar = parse_sidecar(path)

    assert sidecar.reproduction is not None
    assert any("Unknown key in [reproduction]" in record.message for record in caplog.records)


def test_parse_experiment_sidecar_controls_block(tmp_path):
    """[controls] block with positive_outcome and negative_outcome is parsed correctly."""
    from bathos.sidecar import parse_sidecar
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "Test hypothesis"
        [outcomes.ctrl_pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.ctrl_fail]
        condition = "value <= 0"
        decision = "debug"
        reasoning = "Bad"
        is_residual = true
        [result_schema]
        value = "float"
        [controls]
        positive_outcome = ["ctrl_pass"]
        negative_outcome = ["ctrl_fail"]
    """)
    sidecar = parse_sidecar(path)
    assert sidecar.controls is not None
    assert sidecar.controls.positive_outcome == ["ctrl_pass"]
    assert sidecar.controls.negative_outcome == ["ctrl_fail"]


def test_parse_experiment_sidecar_controls_block_absent(tmp_path):
    """When [controls] absent, controls is None."""
    from bathos.sidecar import parse_sidecar
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "Test hypothesis"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
    """)
    sidecar = parse_sidecar(path)
    assert sidecar.controls is None


def test_parse_experiment_sidecar_controls_block_empty(tmp_path):
    """Empty [controls] block creates ControlsBlock with empty lists."""
    from bathos.sidecar import parse_sidecar
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "Test hypothesis"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
        [controls]
    """)
    sidecar = parse_sidecar(path)
    assert sidecar.controls is not None
    assert sidecar.controls.positive_outcome == []
    assert sidecar.controls.negative_outcome == []


def test_parse_experiment_sidecar_controls_block_unknown_key_warning(tmp_path, caplog):
    """Unknown keys in [controls] emit WARNING."""
    import logging
    from bathos.sidecar import parse_sidecar
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "Test hypothesis"
        [outcomes.ctrl_pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
        [controls]
        positive_outcome = ["ctrl_pass"]
        unknown_field = "should warn"
    """)
    with caplog.at_level(logging.WARNING, logger="bathos.sidecar"):
        sidecar = parse_sidecar(path)

    assert sidecar.controls is not None
    assert any("Unknown key in [controls]" in record.message for record in caplog.records)
