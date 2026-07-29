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
    assert any(e.field == "CONTROLS_LABEL_NOT_FOUND" for e in result.errors)


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
    assert any(e.field == "CONTROLS_LABEL_NOT_FOUND" for e in result.errors)


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
    # All errors should have field='CONTROLS_LABEL_NOT_FOUND'
    assert all(e.field == "CONTROLS_LABEL_NOT_FOUND" for e in nonexistent_errors)


def _fake_claim(hypotheses=None, confounds=None):
    """A minimal ClaimFile for #3719 unit tests -- no DB/campaign needed at this layer."""
    from bathos.claim import ClaimFile

    return ClaimFile(
        headline="Test claim",
        kill_condition="Outcome != expected",
        regime=None,
        hypotheses=hypotheses if hypotheses is not None else [
            {"id": "H_primary", "label": "Primary"},
            {"id": "H_null", "label": "Null"},
        ],
        assumptions=[],
        confounds=confounds if confounds is not None else [{"id": "C_batch_effect", "label": "Batch"}],
        discriminability=[],
        union_gate_clauses=[],
        path=Path("test.claim.toml"),
        sha256="deadbeef",
    )


def test_claim_discriminability_catches_the_3717_authoring_mistake(tmp_path):
    """#3719: the exact #3717 root cause -- claim_discriminates set to OUTCOME LABELS instead
    of hypothesis ids -- must now fail validate-sidecar when a claim is supplied, instead of
    silently passing and only surfacing at `bth campaign conclude` time."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "Test hypothesis"
        claim_discriminates = ["beyond_nj_regime_found", "caps_at_nj_no_beyond_nj_regime"]
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
    claim = _fake_claim()

    # Without a claim, this sidecar validates fine -- the whole point is it's indistinguishable
    # from a correct one until something supplies the claim to check against.
    assert validate_sidecar(sidecar).ok is True

    result = validate_sidecar(sidecar, claim=claim)
    assert result.ok is False
    bad = [e for e in result.errors if e.field == "claim_discriminates"]
    assert len(bad) == 2
    assert "beyond_nj_regime_found" in bad[0].message
    assert "H_primary" in bad[0].message and "H_null" in bad[0].message  # lists the valid ids


def test_claim_discriminability_passes_with_real_hypothesis_ids(tmp_path):
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "Test hypothesis"
        claim_discriminates = ["H_primary", "H_null"]
        claim_isolates = ["C_batch_effect"]
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
    result = validate_sidecar(sidecar, claim=_fake_claim())
    assert result.ok is True


def test_claim_discriminability_skips_non_experiment_sidecars(tmp_path):
    """claim_discriminates is experiment-only; a benchmark sidecar has no such field to check.

    Asserts on the claim-discriminability check specifically (no claim_discriminates/
    claim_isolates error), not on the overall result: a bare [benchmark] sidecar fails
    validate_sidecar's unrelated, pre-existing "No [outcomes] section found" check regardless
    (BENCHMARK-kind sidecars never populate `outcomes` at all) -- orthogonal to this change.
    """
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_claim_discriminability, validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [benchmark]
        baseline_ref = "v1.0"
        metric = "latency_ms"
        regression_threshold = 10.0
        regression_threshold_basis = "arbitrary"
        target = "some_function"
        [result_schema]
        latency_ms = "float"
    """,
    )
    sidecar = parse_sidecar(path)
    assert validate_claim_discriminability(sidecar, _fake_claim()) == []

    result = validate_sidecar(sidecar, claim=_fake_claim())
    claim_field_errors = [e for e in result.errors if e.field in ("claim_discriminates", "claim_isolates")]
    assert claim_field_errors == []


def _differential_sidecar_toml(differential_block: str) -> str:
    return f"""
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
        {differential_block}
    """


def test_differential_block_valid_passes(tmp_path):
    """[differential] with knob/off/on/expect/metric/min_effect all consistent passes."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        _differential_sidecar_toml("""
        [differential]
        knob = "sidechain_conditioning"
        off = "0.0"
        on = "1.0"
        expect = "differs"
        metric = "signal"
        min_effect = 0.05
        """),
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is True
    assert len(result.errors) == 0


def test_differential_block_absent_passes(tmp_path):
    """No [differential] block is valid — nothing to check."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(tmp_path, _differential_sidecar_toml(""))
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is True


def test_differential_block_empty_knob_fails(tmp_path):
    """knob must be non-empty."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        _differential_sidecar_toml("""
        [differential]
        off = "0.0"
        on = "1.0"
        """),
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any(e.field == "differential.knob" for e in result.errors)


def test_differential_block_off_equals_on_fails(tmp_path):
    """off == on gives no invariant to check — must fail validation."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        _differential_sidecar_toml("""
        [differential]
        knob = "some_knob"
        off = "0.0"
        on = "0.0"
        """),
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any(e.field == "differential" for e in result.errors)


def test_differential_block_invalid_expect_fails(tmp_path):
    """expect must be 'differs' or 'identical'."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        _differential_sidecar_toml("""
        [differential]
        knob = "some_knob"
        off = "0.0"
        on = "1.0"
        expect = "maybe"
        """),
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any(e.field == "differential.expect" for e in result.errors)


def test_differential_block_metric_not_in_result_schema_fails(tmp_path):
    """metric must name a declared result_schema key."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        _differential_sidecar_toml("""
        [differential]
        knob = "some_knob"
        off = "0.0"
        on = "1.0"
        metric = "nonexistent_field"
        min_effect = 0.05
        """),
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any(e.field == "differential.metric" for e in result.errors)


def test_differential_block_missing_min_effect_for_numeric_metric_fails(tmp_path):
    """min_effect is required when metric names a numeric result_schema field."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        _differential_sidecar_toml("""
        [differential]
        knob = "some_knob"
        off = "0.0"
        on = "1.0"
        metric = "signal"
        """),
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any(e.field == "differential.min_effect" for e in result.errors)


def test_differential_block_min_effect_without_metric_fails(tmp_path):
    """min_effect requires metric — whole-dict comparison has no well-defined effect size."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        _differential_sidecar_toml("""
        [differential]
        knob = "some_knob"
        off = "0.0"
        on = "1.0"
        min_effect = 0.05
        """),
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any(e.field == "differential.min_effect" for e in result.errors)


def test_differential_block_whole_dict_mode_without_min_effect_passes(tmp_path):
    """No metric, no min_effect — whole-dict-diff mode, valid."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        _differential_sidecar_toml("""
        [differential]
        knob = "some_knob"
        off = "0.0"
        on = "1.0"
        """),
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is True


def test_reserved_outcome_label_invalid_measurement_fails(tmp_path):
    """Declaring [outcomes.invalid_measurement] yourself is a validation error (debt #1071)."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "Test hypothesis"
        [outcomes.invalid_measurement]
        condition = "TRUE"
        decision = "should not be allowed"
        reasoning = "reserved"
        is_residual = true
        [result_schema]
        value = "float"
        """,
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any(e.field == "outcomes.invalid_measurement" for e in result.errors)


def test_unreserved_outcome_label_unknown_still_passes(tmp_path):
    """[outcomes.unknown] remains a legitimate, non-reserved user label (regression guard)."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "Test hypothesis"
        [outcomes.unknown]
        condition = "value IS NOT NULL"
        decision = "investigate"
        reasoning = "Catch-all"
        is_residual = true
        [result_schema]
        value = "float"
        """,
    )
    sidecar = parse_sidecar(path)
    result = validate_sidecar(sidecar)
    assert result.ok is True
