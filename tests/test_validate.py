import textwrap
from pathlib import Path

import pytest


def _write_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "run_test.bth.toml"
    p.write_text(textwrap.dedent(content))
    return p


def test_valid_sidecar_passes(tmp_path):
    """Complete sidecar with all fields + residual branch should pass validation."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "NVT maintains ±5K over 50ps"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "proceed to NPT validation"
        reasoning = "Temperature stability meets requirements"
        [outcomes.marginal]
        condition = "temp_std >= 5 AND temp_std < 10"
        decision = "tune Langevin gamma, re-run"
        reasoning = "Temperature stability marginal, tuning needed"
        [outcomes.fail]
        condition = "temp_std >= 10"
        decision = "debug thermostat, open issue"
        reasoning = "Temperature instability indicates thermostat problem"
        is_residual = true
        [result_schema]
        temp_std = "float"
        n_steps = "int"
    """,
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is True
    assert len(result.errors) == 0


def test_missing_outcomes_fails(tmp_path):
    """Sidecar with no [outcomes] section should fail."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "Test hypothesis"
        [result_schema]
        value = "float"
    """,
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any("No [outcomes]" in e.message for e in result.errors)


def test_missing_condition_fails(tmp_path):
    """Outcome without condition should fail."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "h"
        [outcomes.pass]
        decision = "proceed"
        reasoning = "All good"
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
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any("Missing 'condition'" in e.message for e in result.errors)


def test_missing_decision_fails(tmp_path):
    """Outcome without decision should fail."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "h"
        [outcomes.pass]
        condition = "value > 0"
        reasoning = "Good value"
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
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any("Missing 'decision'" in e.message for e in result.errors)


def test_missing_reasoning_fails(tmp_path):
    """Outcome without reasoning should fail."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "h"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
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
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any("Missing 'reasoning'" in e.message for e in result.errors)


def test_invalid_duckdb_sql_fails(tmp_path):
    """Outcome with invalid DuckDB SQL should fail."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "h"
        [outcomes.pass]
        condition = "SELECT * FROM nonexistent WHERE invalid syntax"
        decision = "proceed"
        reasoning = "Bad SQL"
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
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any("DuckDB parse error" in e.message for e in result.errors)


def test_no_result_schema_field_referenced_fails(tmp_path):
    """If no result_schema fields are referenced in conditions, should fail."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "h"
        [outcomes.pass]
        condition = "other_field > 0"
        decision = "proceed"
        reasoning = "Other field is good"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        my_result = "float"
        another_result = "int"
    """,
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any("No result_schema fields referenced" in e.message for e in result.errors)


def test_no_residual_branch_fails(tmp_path):
    """Sidecar without is_residual=true fallback branch should fail."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "h"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fail]
        condition = "temp_std >= 5"
        decision = "debug"
        reasoning = "Bad"
        [result_schema]
        temp_std = "float"
    """,
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any("No fallback branch with is_residual=true" in e.message for e in result.errors)


def test_valid_with_residual_branch_passes(tmp_path):
    """Sidecar with is_residual=true fallback should pass."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "Test"
        [outcomes.success]
        condition = "result = TRUE"
        decision = "ship it"
        reasoning = "Test passed"
        [outcomes.unknown]
        condition = "TRUE"
        decision = "investigate"
        reasoning = "Catch-all for unexpected outcomes"
        is_residual = true
        [result_schema]
        result = "bool"
    """,
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is True
    assert len(result.errors) == 0


def test_valid_reproduction_block_passes(tmp_path):
    """Sidecar with valid [reproduction] block should pass."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "Test"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fail]
        condition = "TRUE"
        decision = "debug"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
        [reproduction]
        reproduces_paper = "10.1234/test.doi"
        reproduces_run = "12345678-1234-5678-1234-567812345678"
        tolerance_pct = 5.0
        requires_pass_stem = "baseline"
    """,
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is True
    assert len(result.errors) == 0


def test_reproduction_tolerance_pct_out_of_range_fails(tmp_path):
    """Reproduction with tolerance_pct outside [0, 100] should fail."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "Test"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fail]
        condition = "TRUE"
        decision = "debug"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
        [reproduction]
        tolerance_pct = 105.5
    """,
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any("tolerance_pct must be in [0.0, 100.0]" in e.message for e in result.errors)


def test_reproduction_tolerance_pct_negative_fails(tmp_path):
    """Reproduction with negative tolerance_pct should fail."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "Test"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fail]
        condition = "TRUE"
        decision = "debug"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
        [reproduction]
        tolerance_pct = -0.5
    """,
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any("tolerance_pct must be in [0.0, 100.0]" in e.message for e in result.errors)


def test_reproduction_reproduces_run_invalid_uuid_fails(tmp_path):
    """Reproduction with invalid UUID format should fail."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "Test"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fail]
        condition = "TRUE"
        decision = "debug"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
        [reproduction]
        reproduces_run = "not-a-valid-uuid"
    """,
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any("reproduces_run must be a valid UUID" in e.message for e in result.errors)


def test_reproduction_reproduces_run_valid_uuid_passes(tmp_path):
    """Reproduction with valid UUID format should pass."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "Test"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fail]
        condition = "TRUE"
        decision = "debug"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
        [reproduction]
        reproduces_run = "12345678-1234-5678-1234-567812345678"
    """,
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is True
    assert len(result.errors) == 0


def test_reproduction_reproduces_run_spec_uuid_passes(tmp_path):
    """Reproduction with spec UUID format should pass validation."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "Test"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fail]
        condition = "TRUE"
        decision = "debug"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
        [reproduction]
        reproduces_run = "a3f4e5c6-1234-5678-9abc-def012345678"
    """,
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is True
    assert len(result.errors) == 0


def test_reproduction_reproduces_run_empty_string_passes(tmp_path):
    """Reproduction with empty reproduces_run string should pass (not validated)."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "Test"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fail]
        condition = "TRUE"
        decision = "debug"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
        [reproduction]
        reproduces_paper = "test paper"
    """,
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is True
    assert len(result.errors) == 0


def test_controls_block_valid_positive_outcome(tmp_path):
    """[controls] block with valid positive_outcome label passes validation."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

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
    """,
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is True
    assert len(result.errors) == 0


def test_controls_block_valid_negative_outcome(tmp_path):
    """[controls] block with valid negative_outcome label passes validation."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "Test hypothesis"
        [outcomes.ctrl_fail]
        condition = "value <= 0"
        decision = "debug"
        reasoning = "Bad"
        is_residual = true
        [result_schema]
        value = "float"
        [controls]
        negative_outcome = ["ctrl_fail"]
    """,
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is True
    assert len(result.errors) == 0


def test_controls_block_valid_both_outcomes(tmp_path):
    """[controls] block with both positive_outcome and negative_outcome passes validation."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

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
    result = validate_sidecar(sidecar)
    assert result.ok is True
    assert len(result.errors) == 0


def test_controls_block_invalid_positive_outcome_label(tmp_path):
    """[controls] block with invalid positive_outcome label fails validation."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

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
        positive_outcome = ["nonexistent_label"]
    """,
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any("nonexistent_label" in e.message for e in result.errors)


def test_controls_block_invalid_negative_outcome_label(tmp_path):
    """[controls] block with invalid negative_outcome label fails validation."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

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
        negative_outcome = ["bad_label"]
    """,
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any("bad_label" in e.message for e in result.errors)


def test_controls_block_multiple_invalid_labels(tmp_path):
    """[controls] block with multiple invalid labels reports all errors."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

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
        positive_outcome = ["nonexistent1", "pass"]
        negative_outcome = ["nonexistent2"]
    """,
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is False
    # Should have 2 errors for nonexistent labels
    nonexistent_errors = [e for e in result.errors if "nonexistent" in e.message]
    assert len(nonexistent_errors) == 2
