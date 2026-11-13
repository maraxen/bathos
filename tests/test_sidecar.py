import textwrap
from pathlib import Path

import pytest


def _write_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "run_test.bth.toml"
    p.write_text(textwrap.dedent(content))
    return p


def test_parse_experiment_sidecar(tmp_path):
    from bathos.sidecar import SidecarKind, parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    s = parse_sidecar(path)
    assert s.kind == SidecarKind.EXPERIMENT
    assert s.hypothesis == "NVT maintains ±5K over 50ps"
    assert "pass" in s.outcomes
    assert s.outcomes["pass"].condition == "temp_std < 5"
    assert s.result_schema == {"temp_std": "float"}


def test_parse_benchmark_sidecar(tmp_path):
    from bathos.sidecar import SidecarKind, parse_sidecar

    path = _write_toml(
        tmp_path,
        """
        [benchmark]
        baseline_ref = "run_abc123"
        metric = "ns_per_day"
        regression_threshold = 0.05
        target = "> 50 ns/day"
        [result_schema]
        ns_per_day = "float"
    """,
    )
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
    from bathos.sidecar import evaluate_outcome, parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    s = parse_sidecar(path)
    label = evaluate_outcome(s, {"temp_std": 2.1})
    assert label == "pass"


def test_evaluate_outcome_no_match(tmp_path):
    from bathos.sidecar import evaluate_outcome, parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    s = parse_sidecar(path)
    # value that matches no condition (shouldn't happen in well-formed sidecars, but be safe)
    label = evaluate_outcome(s, {})
    assert label == "unknown"


def test_evaluate_outcome_bool_result(tmp_path):
    from bathos.sidecar import evaluate_outcome, parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    s = parse_sidecar(path)
    label = evaluate_outcome(s, {"reproduced": True})
    assert label == "reproduced"


def test_evaluate_outcome_none_field_does_not_crash(tmp_path):
    """A None-valued result field must not crash evaluation (regression: debt #478)."""
    from bathos.sidecar import evaluate_outcome, parse_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "h"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Catch-all"
        is_residual = true
        [result_schema]
        temp_std = "float"
        seed_override = "int"
    """,
    )
    s = parse_sidecar(path)
    label = evaluate_outcome(s, {"temp_std": 2.1, "seed_override": None})
    assert label == "pass"


def test_evaluate_outcome_nested_dict_field_does_not_crash(tmp_path):
    """A nested-dict result field (e.g. an 'args' blob) must not crash evaluation (regression: debt #478)."""
    from bathos.sidecar import evaluate_outcome, parse_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "h"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Catch-all"
        is_residual = true
        [result_schema]
        temp_std = "float"
    """,
    )
    s = parse_sidecar(path)
    label = evaluate_outcome(s, {"temp_std": 2.1, "args": {"accuracy": 0.9, "loss": None}})
    assert label == "pass"


def test_evaluate_outcome_string_with_apostrophe_does_not_crash(tmp_path):
    """A string result field containing an apostrophe must not break the generated SQL literal."""
    from bathos.sidecar import evaluate_outcome, parse_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "h"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Catch-all"
        is_residual = true
        [result_schema]
        temp_std = "float"
        note = "str"
    """,
    )
    s = parse_sidecar(path)
    label = evaluate_outcome(s, {"temp_std": 2.1, "note": "it's fine"})
    assert label == "pass"


def _outcome_toml_for_x():
    return """
        [experiment]
        hypothesis = "h"
        [outcomes.pass]
        condition = "x < 5"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fail]
        condition = "x >= 5"
        decision = "debug"
        reasoning = "Bad"
        is_residual = true
        [result_schema]
        x = "float"
    """


def test_evaluate_outcome_positive_infinity_is_false_not_error(tmp_path):
    """A bare `inf` result field must not crash SQL generation into an 'error' outcome
    (regression: debt #1941). inf < 5 is false under IEEE 754, so the outcome is 'fail'."""
    from bathos.sidecar import evaluate_outcome, parse_sidecar

    path = _write_toml(tmp_path, _outcome_toml_for_x())
    s = parse_sidecar(path)
    label = evaluate_outcome(s, {"x": float("inf")})
    assert label == "fail"


def test_evaluate_outcome_negative_infinity_is_true(tmp_path):
    """-inf < 5 is true under IEEE 754, so the outcome is 'pass' (regression: debt #1941)."""
    from bathos.sidecar import evaluate_outcome, parse_sidecar

    path = _write_toml(tmp_path, _outcome_toml_for_x())
    s = parse_sidecar(path)
    label = evaluate_outcome(s, {"x": float("-inf")})
    assert label == "pass"


def test_evaluate_outcome_nan_is_deterministic_not_error(tmp_path):
    """A NaN result field must evaluate deterministically rather than raising or landing on
    'error' (regression: debt #1941). NaN is projected as NULL, so no comparison matches and
    the outcome is 'unknown' -- never a pass or fail it did not earn."""
    from bathos.sidecar import evaluate_outcome, parse_sidecar

    path = _write_toml(tmp_path, _outcome_toml_for_x())
    s = parse_sidecar(path)
    label = evaluate_outcome(s, {"x": float("nan")})
    assert label == "unknown"


def test_evaluate_outcome_nan_never_passes_greater_than(tmp_path):
    """Negative control: DuckDB orders 'nan'::DOUBLE above +inf, so a NaN rendered as a
    DOUBLE would satisfy `x > 0.9`. A broken measurement must not record 'pass'."""
    from bathos.sidecar import evaluate_outcome, parse_sidecar

    path = _write_toml(
        tmp_path,
        '[experiment]\nhypothesis = "h"\n\n'
        '[outcomes.pass]\ncondition = "x > 0.9"\ndecision = "d"\n\n'
        '[outcomes.fail]\ncondition = "x <= 0.9"\ndecision = "d"\n\n'
        '[result_schema]\nx = "float"\n',
    )
    s = parse_sidecar(path)
    assert evaluate_outcome(s, {"x": float("nan")}) == "unknown"
    assert evaluate_outcome(s, {"x": 0.95}) == "pass"


def test_sidecar_agent_mode_parsed(tmp_path):
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.agent_mode == "autonomous"


def test_sidecar_agent_mode_default(tmp_path):
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.agent_mode == ""


def test_evaluate_outcome_raises_sidecar_error_on_bad_sql(tmp_path):
    """When outcome condition has invalid SQL, evaluate_outcome raises SidecarError."""
    from bathos.sidecar import SidecarError, evaluate_outcome, parse_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "test"
        [outcomes.bad]
        condition = "INVALID SQL ][["
        decision = "bad"
        reasoning = "This condition has invalid SQL"
        is_residual = true
        [result_schema]
        x = "float"
    """,
    )
    s = parse_sidecar(path)
    # Should raise SidecarError because SQL is invalid
    with pytest.raises(SidecarError):
        evaluate_outcome(s, {"x": 1.0})


def test_parse_experiment_sidecar_with_stage_name(tmp_path):
    """Parse stage_name from [experiment] block."""
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.stage_name == "validation"


def test_parse_experiment_sidecar_stage_name_default(tmp_path):
    """When stage_name absent, default to 'exploration'."""
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.stage_name == "exploration"


def test_parse_experiment_sidecar_stage_name_invalid_coerces_to_exploration(tmp_path, caplog):
    """Invalid stage_name coerces to 'exploration' with warning."""
    import logging

    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )

    with caplog.at_level(logging.WARNING, logger="bathos.sidecar"):
        sidecar = parse_sidecar(path)

    # Should coerce to exploration
    assert sidecar.stage_name == "exploration"

    # Assert warning was logged
    assert any("invalid-stage-with-numbers-123" in record.message for record in caplog.records)


def test_parse_experiment_sidecar_with_novel(tmp_path):
    """Parse novel field from [experiment] block."""
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.novel is True


def test_parse_experiment_sidecar_novel_default(tmp_path):
    """When novel absent, default to False."""
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.novel is False


def test_parse_experiment_sidecar_with_reproduction_block(tmp_path):
    """Parse [reproduction] block with all four fields."""
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.reproduction is not None
    assert sidecar.reproduction.reproduces_paper == "10.1234/example.doi"
    assert sidecar.reproduction.reproduces_run == "12345678-1234-5678-1234-567812345678"
    assert sidecar.reproduction.tolerance_pct == 5.0
    assert sidecar.reproduction.requires_pass_stem == "baseline_run"


def test_parse_experiment_sidecar_reproduction_block_partial(tmp_path):
    """[reproduction] block with only some fields set."""
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.reproduction is not None
    assert sidecar.reproduction.reproduces_paper == "Smith et al. 2023"
    assert sidecar.reproduction.reproduces_run == ""
    assert sidecar.reproduction.tolerance_pct is None
    assert sidecar.reproduction.requires_pass_stem == ""


def test_parse_experiment_sidecar_reproduction_block_absent(tmp_path):
    """When [reproduction] absent, reproduction is None."""
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.reproduction is None


def test_parse_experiment_sidecar_reproduction_block_unknown_key_warning(tmp_path, caplog):
    """Unknown keys in [reproduction] emit WARNING."""
    import logging

    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    with caplog.at_level(logging.WARNING, logger="bathos.sidecar"):
        sidecar = parse_sidecar(path)

    assert sidecar.reproduction is not None
    assert any("Unknown key in [reproduction]" in record.message for record in caplog.records)


def test_parse_experiment_sidecar_controls_block(tmp_path):
    """[controls] block with positive_outcome and negative_outcome is parsed correctly."""
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.controls is not None
    assert sidecar.controls.positive_outcome == ["ctrl_pass"]
    assert sidecar.controls.negative_outcome == ["ctrl_fail"]


def test_parse_experiment_sidecar_controls_block_absent(tmp_path):
    """When [controls] absent, controls is None."""
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.controls is None


def test_parse_experiment_sidecar_controls_block_empty(tmp_path):
    """Empty [controls] block creates ControlsBlock with empty lists."""
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.controls is not None
    assert sidecar.controls.positive_outcome == []
    assert sidecar.controls.negative_outcome == []


def test_parse_experiment_sidecar_controls_block_unknown_key_warning(tmp_path, caplog):
    """Unknown keys in [controls] emit WARNING."""
    import logging

    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    with caplog.at_level(logging.WARNING, logger="bathos.sidecar"):
        sidecar = parse_sidecar(path)

    assert sidecar.controls is not None
    assert any("Unknown key in [controls]" in record.message for record in caplog.records)


def test_parse_experiment_sidecar_differential_block(tmp_path):
    """[differential] block with all fields is parsed correctly (debt #1071)."""
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "Test hypothesis"
        [outcomes.pass]
        condition = "signal > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        signal = "float"
        [differential]
        knob = "sidechain_conditioning"
        off = "0.0"
        on = "1.0"
        expect = "differs"
        metric = "signal"
        min_effect = 0.05
    """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.differential is not None
    assert sidecar.differential.knob == "sidechain_conditioning"
    assert sidecar.differential.off == "0.0"
    assert sidecar.differential.on == "1.0"
    assert sidecar.differential.expect == "differs"
    assert sidecar.differential.metric == "signal"
    assert sidecar.differential.min_effect == 0.05


def test_parse_experiment_sidecar_differential_block_absent(tmp_path):
    """When [differential] absent, differential is None."""
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.differential is None


def test_parse_experiment_sidecar_differential_block_defaults(tmp_path):
    """[differential] with only knob/off/on set defaults expect='differs', no metric/min_effect."""
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
        [differential]
        knob = "some_knob"
        off = "0"
        on = "1"
    """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.differential is not None
    assert sidecar.differential.expect == "differs"
    assert sidecar.differential.metric == ""
    assert sidecar.differential.min_effect is None


def test_parse_experiment_sidecar_differential_block_unknown_key_warning(tmp_path, caplog):
    """Unknown keys in [differential] emit WARNING."""
    import logging

    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
        [differential]
        knob = "some_knob"
        off = "0"
        on = "1"
        unknown_field = "should warn"
    """,
    )
    with caplog.at_level(logging.WARNING, logger="bathos.sidecar"):
        sidecar = parse_sidecar(path)

    assert sidecar.differential is not None
    assert any("Unknown key in [differential]" in record.message for record in caplog.records)


def test_parse_experiment_sidecar_status_block(tmp_path):
    """[status] block with stale/stale_reason/superseded_by is parsed correctly."""
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
        [status]
        stale = true
        stale_reason = "wt-summary-json default points at a mismatched input"
        superseded_by = "run_abc123"
    """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.status is not None
    assert sidecar.status.stale is True
    assert sidecar.status.stale_reason == ("wt-summary-json default points at a mismatched input")
    assert sidecar.status.superseded_by == "run_abc123"


def test_parse_experiment_sidecar_status_block_absent(tmp_path):
    """When [status] absent, status is None."""
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
    """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.status is None


def test_parse_experiment_sidecar_status_block_defaults(tmp_path):
    """[status] present but empty defaults stale=False and empty strings."""
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
        [status]
    """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.status is not None
    assert sidecar.status.stale is False
    assert sidecar.status.stale_reason == ""
    assert sidecar.status.superseded_by == ""


def test_parse_experiment_sidecar_status_block_unknown_key_warning(tmp_path, caplog):
    """Unknown keys in [status] emit WARNING."""
    import logging

    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
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
        [status]
        stale = true
        unknown_field = "should warn"
    """,
    )
    with caplog.at_level(logging.WARNING, logger="bathos.sidecar"):
        sidecar = parse_sidecar(path)

    assert sidecar.status is not None
    assert any("Unknown key in [status]" in record.message for record in caplog.records)


def test_parse_benchmark_sidecar_status_block(tmp_path):
    """[status] applies to non-experiment sidecar kinds too (e.g. benchmark)."""
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
        [benchmark]
        baseline_ref = "run_xyz"
        metric = "ns_per_day"
        [result_schema]
        ns_per_day = "float"
        [status]
        stale = true
        stale_reason = "baseline script superseded"
    """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.status is not None
    assert sidecar.status.stale is True
    assert sidecar.status.stale_reason == "baseline script superseded"


def test_parse_question_observe_calibrate_blocks(tmp_path):
    from bathos.sidecar import CalibrateBlock, ObserveBlock, parse_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        stage_name = "exploration"
        question = "How does MI distribute across the loop?"
        [observe]
        outputs = ["mi_heatmap.png", "per_residue_mi.csv"]
        looking_for = "hubs and asymmetry"
        sanity = ["n_frames_pooled > 0"]
        planned_tests = 2
        [result_schema]
        n_frames_pooled = "int"
    """,
    )
    s = parse_sidecar(path)
    assert s.question == "How does MI distribute across the loop?"
    assert s.observe == ObserveBlock(
        outputs=["mi_heatmap.png", "per_residue_mi.csv"],
        looking_for="hubs and asymmetry",
        sanity=["n_frames_pooled > 0"],
        planned_tests=2,
    )
    assert s.calibrate is None
    assert s.stage_name_declared is True
    assert s.stage_name_raw == "exploration"

    (tmp_path / "cal").mkdir()
    cal_path = _write_toml(
        tmp_path / "cal",
        """
        [experiment]
        stage_name = "calibration"
        [calibrate]
        quantity = "noise floor of mean MI"
        [result_schema]
        mi_noise = "float"
    """,
    )
    c = parse_sidecar(cal_path)
    assert c.calibrate == CalibrateBlock(quantity="noise floor of mean MI")
    assert c.observe is None


def test_parse_stage_name_absent_is_not_declared(tmp_path):
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "legacy"
        [outcomes.pass]
        condition = "x > 0"
        decision = "ok"
        [result_schema]
        x = "float"
    """,
    )
    s = parse_sidecar(path)
    assert s.stage_name_declared is False
    assert s.stage_name_raw is None
    assert s.stage_name == "exploration"  # back-compat coercion default is unchanged


def test_parse_non_canonical_stage_name_keeps_raw_and_coerces(tmp_path):
    from bathos.sidecar import parse_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        stage_name = "validaton"
        [result_schema]
        x = "float"
    """,
    )
    s = parse_sidecar(path)
    # Other modules still see the coerced value, but the literal survives for class resolution.
    assert s.stage_name == "exploration"
    assert s.stage_name_declared is True
    assert s.stage_name_raw == "validaton"



def test_stoichiometry_and_reagents_parse_success_on_experiment(tmp_path: Path) -> None:
    from bathos.sidecar import SidecarKind, parse_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "Parse stoichiometry and reagents cleanly"
        [outcomes.pass]
        condition = "metric > 0"
        decision = "go"
        is_residual = true
        [result_schema]
        metric = "float"

        [stoichiometry]
        schema_version = 1
        barcoding_mode = "primer_pcr"
        min_group_size = 100

        [[stoichiometry.stages]]
        name = "dna_in"
        kind = "input_molecules"
        molecules = 1e8
        species = ["methylated_circular_vector"]

        [stoichiometry.subpools.main_pool]
        weight = 1.0

        [reagents.gibson_mix]
        pack_rxns = 96
        experiment_rxns = 100
        pack_cost_usd = 120.0
        max_total_cost_usd = 240.0
        """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.kind == SidecarKind.EXPERIMENT
    assert sidecar.stoichiometry is not None
    assert sidecar.stoichiometry.stages[0].name == "dna_in"
    assert sidecar.stoichiometry.subpools["main_pool"].weight == 1.0
    assert sidecar.reagents["gibson_mix"].pack_rxns == 96
    assert sidecar.reagents["gibson_mix"].experiment_rxns == 100


@pytest.mark.parametrize("kind_header", ["[benchmark]", "[validation]", "[debug]"])
@pytest.mark.parametrize(
    "block_toml",
    [
        """
        [stoichiometry]
        [[stoichiometry.stages]]
        name = "dna_in"
        kind = "input_molecules"
        molecules = 1e6
        """,
        """
        [reagents.kit_a]
        pack_rxns = 10
        experiment_rxns = 5
        """,
    ],
)
def test_stoichiometry_and_reagents_rejected_on_non_experiment_kinds(
    tmp_path: Path, kind_header: str, block_toml: str
) -> None:
    from bathos.sidecar import SidecarError, parse_sidecar

    path = _write_toml(
        tmp_path,
        f"""
        {kind_header}
        [result_schema]
        x = "float"
        {block_toml}
        """,
    )
    with pytest.raises(SidecarError, match=r"only allowed on \[experiment\]"):
        parse_sidecar(path)


@pytest.mark.parametrize(
    "bad_block",
    [
        "[stoichiometry]\n",
        "[stoichiometry]\nstages = []\n",
        "[stoichiometry]\nunknown_field = 1\n[[stoichiometry.stages]]\nname = 'inp'\nkind = 'input_molecules'\nmolecules = 100\n",
        "[stoichiometry]\n[[stoichiometry.stages]]\nname = 'inp'\nkind = 'input_molecules'\nmolecules = 100\nextra_key = 1\n",
        "[stoichiometry]\n[[stoichiometry.stages]]\nname = 'inp'\nkind = 'input_molecules'\nmolecules = 100\n[stoichiometry.subpools]\n",
        "[stoichiometry]\n[[stoichiometry.stages]]\nname = 'inp'\nkind = 'input_molecules'\nmolecules = 100\n[stoichiometry.subpools.main]\nweight = 1.0\nbogus = 2\n",
        "[reagents]\n",
        "[reagents.kit]\npack_rxns = 10\nexperiment_rxns = 5\nbogus = 1\n",
    ],
)
def test_stoichiometry_and_reagents_reject_unknown_keys_and_empty_tables(
    tmp_path: Path, bad_block: str
) -> None:
    from bathos.sidecar import SidecarError, parse_sidecar

    path = _write_toml(
        tmp_path,
        f"""
        [experiment]
        hypothesis = "Reject bad structure"
        [result_schema]
        x = "float"
        {bad_block}
        """,
    )
    with pytest.raises(SidecarError):
        parse_sidecar(path)


@pytest.mark.parametrize(
    "bad_stoich_or_reagent",
    [
        "[stoichiometry]\nschema_version = 2\n[[stoichiometry.stages]]\nname='a'\nkind='input_molecules'\nmolecules=10\n",
        "[stoichiometry]\nschema_version = true\n[[stoichiometry.stages]]\nname='a'\nkind='input_molecules'\nmolecules=10\n",
        "[stoichiometry]\nbarcoding_mode = 'bad_mode'\n[[stoichiometry.stages]]\nname='a'\nkind='input_molecules'\nmolecules=10\n",
        "[stoichiometry]\nmax_collision_prob = 1.0\n[[stoichiometry.stages]]\nname='a'\nkind='input_molecules'\nmolecules=10\n",
        "[stoichiometry]\nmin_consensus_reads = true\n[[stoichiometry.stages]]\nname='a'\nkind='input_molecules'\nmolecules=10\n",
        "[stoichiometry]\ntarget_reads = 10.5\n[[stoichiometry.stages]]\nname='a'\nkind='input_molecules'\nmolecules=10\n",
        "[stoichiometry]\n[[stoichiometry.stages]]\nname='BadName'\nkind='input_molecules'\nmolecules=10\n",
        "[stoichiometry]\n[[stoichiometry.stages]]\nname='a'\nkind='unknown_kind'\nmolecules=10\n",
        "[stoichiometry]\n[[stoichiometry.stages]]\nname='a'\nkind='input_molecules'\nmolecules=nan\n",
        "[stoichiometry]\n[[stoichiometry.stages]]\nname='a'\nkind='input_molecules'\nmolecules=inf\n",
        "[stoichiometry]\n[[stoichiometry.stages]]\nname='a'\nkind='input_molecules'\nmolecules=-1.0\n",
        "[stoichiometry]\n[[stoichiometry.stages]]\nname='a'\nkind='input_molecules'\n",
        "[stoichiometry]\n[[stoichiometry.stages]]\nname='a'\nkind='colony_cfu'\n",
        "[stoichiometry]\n[[stoichiometry.stages]]\nname='a'\nkind='colony_cfu'\ncolony_cfu=10\nefficiency=0.5\n",
        "[stoichiometry]\n[[stoichiometry.stages]]\nname='a'\nkind='barcode_collision'\nbarcode_diversity=10.5\n",
        "[stoichiometry]\n[[stoichiometry.stages]]\nname='a'\nkind='founder_inoculum'\nsampling_fraction=0.5\ninoculum_cells=100\n",
        "[stoichiometry]\n[[stoichiometry.stages]]\nname='a'\nkind='phix_adjusted_net_reads'\nraw_reads=100\nphix_fraction=1.0\n",
        "[stoichiometry]\n[[stoichiometry.stages]]\nname='dup'\nkind='input_molecules'\nmolecules=10\n[[stoichiometry.stages]]\nname='dup'\nkind='input_molecules'\nmolecules=20\n",
        "[reagents.kit]\npack_rxns = 0.4\nexperiment_rxns = 1\n",
        "[reagents.kit]\npack_rxns = true\nexperiment_rxns = 1\n",
        "[reagents.kit]\npack_rxns = 10\nexperiment_rxns = 0\n",
        "[reagents.kit]\npack_rxns = 10\nexperiment_rxns = inf\n",
        "[reagents.kit]\npack_rxns = 10\nexperiment_rxns = 5\npack_cost_usd = -1.0\n",
        "[reagents.kit]\npack_rxns = 10\nexperiment_rxns = 5\nmax_waste_fraction = 1.0\n",
        "[reagents.kit]\npack_rxns = 10\nexperiment_rxns = 5\nmax_unused_rxns = -0.1\n",
    ],
)
def test_stoichiometry_and_reagents_boundary_errors(
    tmp_path: Path, bad_stoich_or_reagent: str
) -> None:
    from bathos.sidecar import SidecarError, parse_sidecar

    path = _write_toml(
        tmp_path,
        f"""
        [experiment]
        hypothesis = "Boundary validation"
        [result_schema]
        x = "float"
        {bad_stoich_or_reagent}
        """,
    )
    with pytest.raises(SidecarError):
        parse_sidecar(path)
