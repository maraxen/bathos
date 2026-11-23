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
        kill_condition_satisfiable_by_null=False,
        regime=None,
        hypotheses=hypotheses
        if hypotheses is not None
        else [
            {"id": "H_primary", "label": "Primary"},
            {"id": "H_null", "label": "Null"},
        ],
        assumptions=[],
        confounds=confounds
        if confounds is not None
        else [{"id": "C_batch_effect", "label": "Batch"}],
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
    claim_field_errors = [
        e for e in result.errors if e.field in ("claim_discriminates", "claim_isolates")
    ]
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



def test_stoichiometry_warn_only_override_and_gate_blocking(tmp_path: Path, monkeypatch) -> None:
    import hashlib

    from bathos.catalog import init_catalog
    from bathos.prereg import GateErrorCode, SidecarBundle, gate_check
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".bth.toml").write_text('[project]\nslug = "testproj"\n', encoding="utf-8")
    catalog = tmp_path / ".bth" / "catalog"
    init_catalog(catalog)
    exp_dir = tmp_path / "scripts" / "experiments"
    exp_dir.mkdir(parents=True)
    script_path = exp_dir / "run_stoich.py"
    script_path.write_text("print('ok')\n", encoding="utf-8")
    sidecar_path = exp_dir / "run_stoich.bth.toml"
    sidecar_path.write_text(
        textwrap.dedent(
            """
            [experiment]
            hypothesis = "Unmet collision floor blocks gate unless warn-only rollback set"
            [outcomes.pass]
            condition = "val > 0"
            decision = "go"
            reasoning = "ok"
            adversarial_check = "val >= 0"
            [outcomes.fail]
            condition = "val <= 0"
            decision = "stop"
            reasoning = "bad"
            is_residual = true
            [result_schema]
            val = "float"

            [stoichiometry]
            barcoding_mode = "pre_barcoded_vector"
            max_collision_prob = 0.05

            [[stoichiometry.stages]]
            name = "dna_in"
            kind = "input_molecules"
            molecules = 1e9

            [[stoichiometry.stages]]
            name = "colonies"
            kind = "colony_cfu"
            depends_on = ["dna_in"]
            colony_cfu = 1500000.0

            [[stoichiometry.stages]]
            name = "bc"
            kind = "barcode_collision"
            depends_on = ["colonies"]
            barcode_diversity = 5000000
            """
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("BTH_STOICHIOMETRY_WARN_ONLY", raising=False)
    sidecar = parse_sidecar(sidecar_path)
    blocked = validate_sidecar(sidecar, sidecar_path=sidecar_path)
    assert blocked.ok is False

    bundle = SidecarBundle(
        path=sidecar_path,
        sha256=hashlib.sha256(sidecar_path.read_bytes()).hexdigest(),
        found=True,
    )
    gate_blocked = gate_check(
        script_path=script_path,
        bundle=bundle,
        mode="autonomous",
        catalog_dir=catalog,
    )
    assert gate_blocked.ok is False
    assert gate_blocked.error_payload is not None
    assert gate_blocked.error_payload.error_code == GateErrorCode.SIDECAR_INVALID

    monkeypatch.setenv("BTH_STOICHIOMETRY_WARN_ONLY", "1")
    captured_events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "bathos.validate.event",
        lambda name, **kw: captured_events.append((name, kw)),
    )
    warned = validate_sidecar(sidecar, sidecar_path=sidecar_path)
    assert warned.ok is True
    assert warned.errors == []
    assert any(w.startswith("[stoichiometry warn-only] ") for w in warned.warnings)
    assert any(name == "stoichiometry.warn_only_downgrade" for name, _ in captured_events)

    gate_allowed = gate_check(
        script_path=script_path,
        bundle=bundle,
        mode="autonomous",
        catalog_dir=catalog,
    )
    assert gate_allowed.ok is True


def test_stoichiometry_validated_when_outcomes_missing(tmp_path: Path) -> None:
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "Missing outcomes still evaluates stoichiometry"
        [result_schema]
        val = "float"

        [stoichiometry]
        barcoding_mode = "primer_pcr"

        [[stoichiometry.stages]]
        name = "dna_in"
        kind = "input_molecules"
        molecules = 1e8
        species = ["methylated_circular_vector"]

        [[stoichiometry.stages]]
        name = "colonies"
        kind = "colony_cfu"
        depends_on = ["dna_in"]
        colony_cfu = 1e5
        """,
    )
    res = validate_sidecar(parse_sidecar(path))
    assert res.ok is False
    fields = {err.field for err in res.errors}
    assert "outcomes" in fields
    assert "stoichiometry.stages.colonies.species" in fields


def test_reagents_amortization_exact_decimal_boundaries_pass(tmp_path: Path) -> None:
    from bathos.sidecar import parse_sidecar
    from bathos.stoichiometry import evaluate_reagents
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "Exact Decimal reagent amortization passes tight boundaries"
        [outcomes.pass]
        condition = "val > 0"
        decision = "go"
        reasoning = "Within bounds"
        is_residual = true
        [result_schema]
        val = "float"

        [reagents.exact_waste]
        pack_rxns = 3
        experiment_rxns = 0.9
        max_waste_fraction = 0.7

        [reagents.exact_unused]
        pack_rxns = 10
        experiment_rxns = 7.1
        max_unused_rxns = 2.9
        max_waste_fraction = 0.29

        [reagents.exact_total_cost]
        pack_rxns = 96
        experiment_rxns = 200
        pack_cost_usd = 0.4
        max_total_cost_usd = 1.2

        [reagents.exact_per_rxn_cost]
        pack_rxns = 1
        experiment_rxns = 0.7
        pack_cost_usd = 0.07
        max_cost_per_rxn_usd = 0.1

        [reagents.unbounded_ceil]
        pack_rxns = 96
        experiment_rxns = 100
        """,
    )
    sidecar = parse_sidecar(path)
    evaluation = evaluate_reagents(sidecar.reagents)
    assert evaluation.errors == ()
    assert evaluation.reports["unbounded_ceil"].packs_required == 2
    assert evaluation.reports["unbounded_ceil"].purchased_rxns == 192
    assert evaluation.reports["unbounded_ceil"].unused_rxns == 92.0
    assert evaluation.reports["exact_total_cost"].packs_required == 3
    res = validate_sidecar(sidecar)
    assert res.ok is True, res.errors


@pytest.mark.parametrize(
    ("reagent_toml", "expected_field"),
    [
        (
            """
            [reagents.kit]
            pack_rxns = 96
            experiment_rxns = 100
            max_waste_fraction = 0.4
            """,
            "reagents.kit.max_waste_fraction",
        ),
        (
            """
            [reagents.kit]
            pack_rxns = 96
            experiment_rxns = 100
            max_unused_rxns = 50.0
            """,
            "reagents.kit.max_unused_rxns",
        ),
        (
            """
            [reagents.kit]
            pack_rxns = 96
            experiment_rxns = 200
            pack_cost_usd = 0.4
            max_total_cost_usd = 1.19
            """,
            "reagents.kit.max_total_cost_usd",
        ),
        (
            """
            [reagents.kit]
            pack_rxns = 1
            experiment_rxns = 0.7
            pack_cost_usd = 0.08
            max_cost_per_rxn_usd = 0.1
            """,
            "reagents.kit.max_cost_per_rxn_usd",
        ),
        (
            """
            [reagents.kit]
            pack_rxns = 96
            experiment_rxns = 100
            max_total_cost_usd = 50.0
            """,
            "reagents.kit.pack_cost_usd",
        ),
        (
            """
            [reagents.kit]
            pack_rxns = 96
            experiment_rxns = 100
            max_cost_per_rxn_usd = 1.0
            """,
            "reagents.kit.pack_cost_usd",
        ),
    ],
)
def test_reagents_amortization_violations_fail(
    tmp_path: Path, reagent_toml: str, expected_field: str
) -> None:
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _write_toml(
        tmp_path,
        f"""
        [experiment]
        hypothesis = "Reagent amortization violation fails"
        [outcomes.pass]
        condition = "val > 0"
        decision = "go"
        reasoning = "Within bounds"
        is_residual = true
        [result_schema]
        val = "float"
        {reagent_toml}
        """,
    )
    res = validate_sidecar(parse_sidecar(path))
    assert res.ok is False
    assert any(err.field == expected_field for err in res.errors)


# ── tech_debt #3327: nested keys, = null, shadowed outcomes, result JSON checks ─────────────


def test_nested_result_schema_keys_validate_and_reject_unknown_subfield(tmp_path: Path):
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    good = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "nested schema validates"
        [outcomes.pass]
        condition = "metrics.loss < 0.1"
        decision = "keep"
        reasoning = "low loss"
        [outcomes.fail]
        condition = "metrics.loss >= 0.1"
        decision = "stop"
        reasoning = "high loss"
        is_residual = true
        [result_schema.metrics]
        loss = "float"
    """,
    )
    assert validate_sidecar(parse_sidecar(good)).ok is True

    bad = tmp_path / "bad_nested.bth.toml"
    bad.write_text(good.read_text().replace("metrics.loss < 0.1", "metrics.missing_col < 0.1"))
    res_bad = validate_sidecar(parse_sidecar(bad))
    assert res_bad.ok is False
    assert any("DuckDB parse error" in e.message for e in res_bad.errors)


def test_sql_null_equality_operator_is_rejected_while_is_null_passes(tmp_path: Path):
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    bad_eq = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "null eq"
        [outcomes.pass]
        condition = "temp_std = null"
        decision = "go"
        reasoning = "r"
        [outcomes.fail]
        condition = "temp_std >= 0"
        decision = "stop"
        reasoning = "r"
        is_residual = true
        [result_schema]
        temp_std = "float"
    """,
    )
    res_eq = validate_sidecar(parse_sidecar(bad_eq))
    assert res_eq.ok is False
    assert any("IS NULL" in e.message for e in res_eq.errors)

    bad_neq = tmp_path / "bad_neq.bth.toml"
    bad_neq.write_text(bad_eq.read_text().replace("temp_std = null", "NULL != temp_std"))
    res_neq = validate_sidecar(parse_sidecar(bad_neq))
    assert res_neq.ok is False
    assert any("IS NULL" in e.message for e in res_neq.errors)

    good_is_null = tmp_path / "good_is_null.bth.toml"
    good_is_null.write_text(bad_eq.read_text().replace("temp_std = null", "temp_std IS NULL"))
    assert validate_sidecar(parse_sidecar(good_is_null)).ok is True


def test_shadowed_and_unsatisfiable_outcomes_are_rejected(tmp_path: Path):
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    # 1. Earlier tautology shadows subsequent outcome
    taut = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "tautology shadows"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "stop"
        reasoning = "catch-all first"
        is_residual = true
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "go"
        reasoning = "never reached"
        [result_schema]
        temp_std = "float"
    """,
    )
    res_taut = validate_sidecar(parse_sidecar(taut))
    assert res_taut.ok is False
    assert any("shadowed" in e.message for e in res_taut.errors)

    # 2. Subsumed numeric interval shadows later branch
    subsumed = tmp_path / "subsumed.bth.toml"
    subsumed.write_text(
        textwrap.dedent("""
        [experiment]
        hypothesis = "subsumed interval"
        [outcomes.marginal]
        condition = "temp_std < 10"
        decision = "tune"
        reasoning = "wide first"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "go"
        reasoning = "narrower second"
        [outcomes.fail]
        condition = "temp_std >= 10"
        decision = "stop"
        reasoning = "residual"
        is_residual = true
        [result_schema]
        temp_std = "float"
    """)
    )
    res_sub = validate_sidecar(parse_sidecar(subsumed))
    assert res_sub.ok is False
    assert any("shadowed" in e.message and "marginal" in e.message for e in res_sub.errors)

    # 3. Unsatisfiable conjunction
    unsat = tmp_path / "unsat.bth.toml"
    unsat.write_text(
        textwrap.dedent("""
        [experiment]
        hypothesis = "unsat"
        [outcomes.pass]
        condition = "temp_std > 10 AND temp_std < 5"
        decision = "go"
        reasoning = "impossible"
        [outcomes.fail]
        condition = "temp_std >= 0"
        decision = "stop"
        reasoning = "residual"
        is_residual = true
        [result_schema]
        temp_std = "float"
    """)
    )
    res_unsat = validate_sidecar(parse_sidecar(unsat))
    assert res_unsat.ok is False
    assert any("unsatisfiable" in e.message for e in res_unsat.errors)


def test_validate_sidecar_with_result_json_catches_schema_and_condition_errors(tmp_path: Path):
    import json

    from bathos.cli_cyclopts import app
    from bathos.mcp import validate_sidecar_tool
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar
    from tests._cyclopts_runner import CyclopticRunner

    sidecar_path = _write_toml(
        tmp_path,
        """
        [experiment]
        hypothesis = "result validation"
        [outcomes.pass]
        condition = "metrics.loss < 0.1 AND n_steps >= 10"
        decision = "go"
        reasoning = "good"
        [outcomes.fail]
        condition = "metrics.loss >= 0.1 AND n_steps >= 10"
        decision = "stop"
        reasoning = "bad"
        is_residual = true
        [result_schema]
        n_steps = "int"
        [result_schema.metrics]
        loss = "float"
    """,
    )
    sc = parse_sidecar(sidecar_path)

    # Valid result JSON passes and identifies matched outcome
    ok_json = tmp_path / "ok.json"
    ok_json.write_text(json.dumps({"n_steps": 20, "metrics": {"loss": 0.05}}))
    assert validate_sidecar(sc, sidecar_path=sidecar_path, result=ok_json).ok is True

    tool_ok = validate_sidecar_tool(path=str(sidecar_path), result=str(ok_json))
    assert tool_ok["validation_ok"] is True
    assert tool_ok["matched_outcome"] == "pass"

    # CLI --result works
    cli = CyclopticRunner()
    cli_ok = cli.invoke(app, ["validate-sidecar", str(sidecar_path), "--result", str(ok_json)])
    assert cli_ok.exit_code == 0

    # Null field fails
    res_null = validate_sidecar(
        sc, result={"n_steps": 20, "metrics": {"loss": None}}
    )
    assert res_null.ok is False
    assert any("null" in e.message for e in res_null.errors)

    # Bool masquerading as int fails
    res_bool = validate_sidecar(
        sc, result={"n_steps": True, "metrics": {"loss": 0.05}}
    )
    assert res_bool.ok is False
    assert any("expected int" in e.message for e in res_bool.errors)

    # Unmatched condition (would evaluate to unknown: n_steps < 10) fails
    bad_json = tmp_path / "unknown.json"
    bad_json.write_text(json.dumps({"n_steps": 2, "metrics": {"loss": 0.05}}))
    cli_bad = cli.invoke(app, ["validate-sidecar", str(sidecar_path), "--result", str(bad_json)])
    assert cli_bad.exit_code == 1
    assert "unknown" in cli_bad.output
