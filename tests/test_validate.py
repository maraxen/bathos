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
