"""Tests for POPPER e-value sidecar parsing, compute_evalue, and validate_popper_block."""
import textwrap
from pathlib import Path

import pytest

from bathos.sidecar import Sidecar, SidecarKind, OutcomeSpec, parse_sidecar, compute_evalue
from bathos.validate import validate_popper_block, validate_sidecar, ValidationError


def _write_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "run_test.bth.toml"
    p.write_text(textwrap.dedent(content))
    return p


# ---------------------------------------------------------------------------
# Parsing tests
# ---------------------------------------------------------------------------


def test_parse_sidecar_with_popper_block(tmp_path):
    """Sidecar with [popper] block populates all popper fields correctly."""
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "NVT thermostat"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "proceed"
        reasoning = "ok"
        is_residual = false
        [outcomes.fail]
        condition = "temp_std >= 5"
        decision = "debug"
        reasoning = "bad"
        is_residual = true
        [result_schema]
        temp_std = "float"
        [popper]
        null_pass_rate = 0.30
        alt_pass_rate = 0.75
        stopping_threshold = 20.0
    """)
    s = parse_sidecar(path)
    assert s.popper_null_pass_rate == 0.30
    assert s.popper_alt_pass_rate == 0.75
    assert s.popper_stopping_threshold == 20.0
    assert s.popper_weights == {}


def test_parse_sidecar_without_popper_block(tmp_path):
    """Sidecar without [popper] block has None for popper fields."""
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "NVT thermostat"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "proceed"
        reasoning = "ok"
        [outcomes.fail]
        condition = "temp_std >= 5"
        decision = "debug"
        reasoning = "bad"
        is_residual = true
        [result_schema]
        temp_std = "float"
    """)
    s = parse_sidecar(path)
    assert s.popper_null_pass_rate is None
    assert s.popper_stopping_threshold is None
    assert s.popper_weights == {}


def test_parse_sidecar_with_popper_weights(tmp_path):
    """Sidecar with [popper.weights] sub-table populates popper_weights dict."""
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "NVT thermostat"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "proceed"
        reasoning = "ok"
        is_residual = false
        [outcomes.fail]
        condition = "temp_std >= 5"
        decision = "debug"
        reasoning = "bad"
        is_residual = true
        [result_schema]
        temp_std = "float"
        [popper]
        null_pass_rate = 0.30
        alt_pass_rate = 0.75
        stopping_threshold = 20.0
        [popper.weights]
        pass = 2.5
        marginal = 1.0
        fail = 0.4
    """)
    s = parse_sidecar(path)
    assert s.popper_weights == {"pass": 2.5, "marginal": 1.0, "fail": 0.4}


# ---------------------------------------------------------------------------
# compute_evalue tests
# ---------------------------------------------------------------------------

def _make_sidecar_with_popper(null=0.30, alt=0.75, threshold=20.0, outcomes=None, weights=None):
    """Helper to build a Sidecar with a [popper] block without touching disk."""
    if outcomes is None:
        outcomes = {
            "pass": OutcomeSpec(condition="temp_std < 5", decision="proceed", reasoning="ok", is_residual=False),
            "fail": OutcomeSpec(condition="temp_std >= 5", decision="debug", reasoning="bad", is_residual=True),
        }
    return Sidecar(
        kind=SidecarKind.EXPERIMENT,
        result_schema={"temp_std": "float"},
        outcomes=outcomes,
        hypothesis="test",
        popper_null_pass_rate=null,
        popper_alt_pass_rate=alt,
        popper_stopping_threshold=threshold,
        popper_weights=weights or {},
    )


def test_compute_evalue_pass():
    """E-value for 'pass' outcome is alt/null = 0.75/0.30 = 2.5."""
    sidecar = _make_sidecar_with_popper(null=0.30, alt=0.75)
    result = compute_evalue(sidecar, "pass")
    assert abs(result - 2.5) < 1e-9


def test_compute_evalue_fail():
    """E-value for the residual (fail) outcome is (1-alt)/(1-null) = 0.25/0.70."""
    sidecar = _make_sidecar_with_popper(null=0.30, alt=0.75)
    expected = (1 - 0.75) / (1 - 0.30)
    result = compute_evalue(sidecar, "fail")
    assert abs(result - expected) < 1e-9


def test_compute_evalue_marginal():
    """E-value for 'marginal' outcome is always 1.0 (hard default)."""
    outcomes = {
        "pass": OutcomeSpec(condition="x > 1", decision="proceed", reasoning="ok", is_residual=False),
        "marginal": OutcomeSpec(condition="x == 1", decision="review", reasoning="borderline", is_residual=False),
        "fail": OutcomeSpec(condition="x < 1", decision="debug", reasoning="bad", is_residual=True),
    }
    sidecar = _make_sidecar_with_popper(outcomes=outcomes)
    result = compute_evalue(sidecar, "marginal")
    assert result == 1.0


def test_compute_evalue_error():
    """E-value for 'error' outcome is always 1.0 (non-overridable)."""
    sidecar = _make_sidecar_with_popper()
    result = compute_evalue(sidecar, "error")
    assert result == 1.0


def test_compute_evalue_unknown():
    """E-value for 'unknown' outcome is always 1.0."""
    sidecar = _make_sidecar_with_popper()
    result = compute_evalue(sidecar, "unknown")
    assert result == 1.0


def test_compute_evalue_explicit_weight_override():
    """Explicit weight in [popper.weights] overrides the likelihood ratio formula."""
    sidecar = _make_sidecar_with_popper(weights={"pass": 3.0})
    result = compute_evalue(sidecar, "pass")
    assert result == 3.0


def test_compute_evalue_no_popper_block():
    """Sidecar without [popper] block returns 1.0 for any outcome."""
    sidecar = Sidecar(
        kind=SidecarKind.EXPERIMENT,
        result_schema={"x": "float"},
        outcomes={
            "pass": OutcomeSpec(condition="x > 0", decision="ok", reasoning="good", is_residual=False),
            "fail": OutcomeSpec(condition="x <= 0", decision="debug", reasoning="bad", is_residual=True),
        },
        hypothesis="no popper",
    )
    # popper_null_pass_rate is None by default
    assert compute_evalue(sidecar, "pass") == 1.0
    assert compute_evalue(sidecar, "fail") == 1.0
    assert compute_evalue(sidecar, "error") == 1.0


# ---------------------------------------------------------------------------
# validate_popper_block tests
# ---------------------------------------------------------------------------

def test_validate_popper_null_pass_rate_out_of_range(tmp_path):
    """null_pass_rate outside (0, 1) produces a validation error."""
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "test"
        [outcomes.pass]
        condition = "x > 0"
        decision = "ok"
        reasoning = "good"
        is_residual = false
        [outcomes.fail]
        condition = "x <= 0"
        decision = "debug"
        reasoning = "bad"
        is_residual = true
        [result_schema]
        x = "float"
        [popper]
        null_pass_rate = 1.5
        alt_pass_rate = 0.75
        stopping_threshold = 20.0
    """)
    s = parse_sidecar(path)
    errors = validate_popper_block(s)
    assert any(e.field == "popper.null_pass_rate" for e in errors)


def test_validate_popper_null_equals_alt(tmp_path):
    """null_pass_rate == alt_pass_rate produces a 'popper' error (no test power)."""
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "test"
        [outcomes.pass]
        condition = "x > 0"
        decision = "ok"
        reasoning = "good"
        is_residual = false
        [outcomes.fail]
        condition = "x <= 0"
        decision = "debug"
        reasoning = "bad"
        is_residual = true
        [result_schema]
        x = "float"
        [popper]
        null_pass_rate = 0.5
        alt_pass_rate = 0.5
        stopping_threshold = 20.0
    """)
    s = parse_sidecar(path)
    errors = validate_popper_block(s)
    assert any(e.field == "popper" for e in errors)


def test_validate_popper_low_threshold_warning(tmp_path):
    """stopping_threshold < 10.0 produces a WARNING-level message."""
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "test"
        [outcomes.pass]
        condition = "x > 0"
        decision = "ok"
        reasoning = "good"
        is_residual = false
        [outcomes.fail]
        condition = "x <= 0"
        decision = "debug"
        reasoning = "bad"
        is_residual = true
        [result_schema]
        x = "float"
        [popper]
        null_pass_rate = 0.3
        alt_pass_rate = 0.75
        stopping_threshold = 5.0
    """)
    s = parse_sidecar(path)
    errors = validate_popper_block(s)
    warning_errors = [e for e in errors if e.message.startswith("WARNING:")]
    assert len(warning_errors) >= 1


def test_validate_popper_threshold_10_no_warning(tmp_path):
    """stopping_threshold == 10.0 does not produce a WARNING entry."""
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "test"
        [outcomes.pass]
        condition = "x > 0"
        decision = "ok"
        reasoning = "good"
        is_residual = false
        [outcomes.fail]
        condition = "x <= 0"
        decision = "debug"
        reasoning = "bad"
        is_residual = true
        [result_schema]
        x = "float"
        [popper]
        null_pass_rate = 0.3
        alt_pass_rate = 0.75
        stopping_threshold = 10.0
    """)
    s = parse_sidecar(path)
    errors = validate_popper_block(s)
    warning_errors = [e for e in errors if e.message.startswith("WARNING:")]
    assert len(warning_errors) == 0


def test_validate_popper_error_weight_not_1(tmp_path):
    """Weight for 'error' != 1.0 produces a popper.weights.error error."""
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "test"
        [outcomes.pass]
        condition = "x > 0"
        decision = "ok"
        reasoning = "good"
        is_residual = false
        [outcomes.fail]
        condition = "x <= 0"
        decision = "debug"
        reasoning = "bad"
        is_residual = true
        [result_schema]
        x = "float"
        [popper]
        null_pass_rate = 0.3
        alt_pass_rate = 0.75
        stopping_threshold = 20.0
        [popper.weights]
        error = 2.0
    """)
    s = parse_sidecar(path)
    errors = validate_popper_block(s)
    assert any(e.field == "popper.weights.error" for e in errors)


def test_validate_popper_unknown_weight_key(tmp_path):
    """Weight for an undeclared outcome label produces a popper.weights.<key> error."""
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "test"
        [outcomes.pass]
        condition = "x > 0"
        decision = "ok"
        reasoning = "good"
        is_residual = false
        [outcomes.fail]
        condition = "x <= 0"
        decision = "debug"
        reasoning = "bad"
        is_residual = true
        [result_schema]
        x = "float"
        [popper]
        null_pass_rate = 0.3
        alt_pass_rate = 0.75
        stopping_threshold = 20.0
        [popper.weights]
        great = 2.0
    """)
    s = parse_sidecar(path)
    errors = validate_popper_block(s)
    assert any(e.field == "popper.weights.great" for e in errors)


def test_validate_popper_on_benchmark_sidecar(tmp_path):
    """[popper] block on a [benchmark] sidecar produces a 'popper' validation error."""
    # benchmark sidecars don't have [outcomes] so we build a Sidecar directly
    sidecar = Sidecar(
        kind=SidecarKind.BENCHMARK,
        result_schema={"ns_per_day": "float"},
        baseline_ref="run_abc123",
        metric="ns_per_day",
        regression_threshold=0.05,
        # Inject popper fields
        popper_null_pass_rate=0.3,
        popper_alt_pass_rate=0.75,
        popper_stopping_threshold=20.0,
    )
    errors = validate_popper_block(sidecar)
    assert any(e.field == "popper" for e in errors)
