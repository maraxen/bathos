"""Tests for 5-Dimension Scored Rubric Evaluator (spec 261027 Section 3.1)."""

import itertools
from pathlib import Path

import pytest

from bathos.errors import (
    RubricInvalidError,
    RubricNotFoundError,
    StepBackInvalidSchemaError,
)
from bathos.stepback.rubrics import (
    CANONICAL_DIMENSIONS,
    DEFAULT_WEIGHTS,
    evaluate_rubric,
    load_rubric,
    resolve_rubric_recommendation,
)


def test_load_default_rubric():
    data, sha = load_rubric()
    assert data["id"] == "stepback_v1"
    assert len(sha) == 64
    assert len(data["dimensions"]) == 5
    for dim in CANONICAL_DIMENSIONS:
        assert dim in data["dimensions"]
        assert "weight" in data["dimensions"][dim]
        assert "anchors" in data["dimensions"][dim]


def test_load_rubric_missing_file(tmp_path: Path):
    missing = tmp_path / "nonexistent.toml"
    with pytest.raises(RubricNotFoundError):
        load_rubric(missing)


def test_load_rubric_invalid_weights(tmp_path: Path):
    bad_toml = tmp_path / "bad.toml"
    bad_toml.write_text("""
    [dimensions.d1]
    weight = 0.5
    [dimensions.d2]
    weight = 0.1
    [dimensions.d3]
    weight = 0.1
    [dimensions.d4]
    weight = 0.1
    [dimensions.d5]
    weight = 0.1
    """)  # sums to 0.9, not 1.0
    with pytest.raises(RubricInvalidError, match="must sum to 1.0"):
        load_rubric(bad_toml)


def test_hard_floor_veto_rule():
    """Any dimension scored 1 forces HOLD and Escalation Level 2."""
    for dim_vetoed in CANONICAL_DIMENSIONS:
        scores = {dim: 5 for dim in CANONICAL_DIMENSIONS}
        scores[dim_vetoed] = 1

        composite, rec, level, _ = evaluate_rubric(scores)
        assert rec == "HOLD", f"Dimension {dim_vetoed} score 1 did not trigger hard-floor veto"
        assert level == 2


def test_flaw_capping_rule():
    """A score of 2 caps recommendation at PIVOT even if composite >= 4.2."""
    # 4 dimensions scored 5, 1 dimension scored 2 (e.g. theoretical_grounding=2, weight 0.20)
    # composite = 4*0.80 + 2*0.20 = 4.0 + 0.4 = 4.4 >= 4.2
    scores = {
        "question_validity": 5,
        "empirical_progress": 5,
        "theoretical_grounding": 2,
        "resource_efficiency": 5,
        "risk_confound_surface": 5,
    }
    composite, rec, level, _ = evaluate_rubric(scores)
    assert composite >= 4.2
    assert rec == "PIVOT"
    assert level == 1


def test_nominal_continue_and_thresholds():
    # Perfect score: all 5s -> CONTINUE, Level 0
    all_fives = {dim: 5 for dim in CANONICAL_DIMENSIONS}
    composite, rec, level, _ = evaluate_rubric(all_fives)
    assert composite == 5.0
    assert rec == "CONTINUE"
    assert level == 0

    # All 4s -> composite 4.0 -> PIVOT (since >= 3.4 and < 4.2), Level 1
    all_fours = {dim: 4 for dim in CANONICAL_DIMENSIONS}
    composite, rec, level, _ = evaluate_rubric(all_fours)
    assert composite == 4.0
    assert rec == "PIVOT"
    assert level == 1

    # All 3s -> composite 3.0 -> HOLD (since >= 2.6 and < 3.4), Level 2
    all_threes = {dim: 3 for dim in CANONICAL_DIMENSIONS}
    composite, rec, level, _ = evaluate_rubric(all_threes)
    assert composite == 3.0
    assert rec == "HOLD"
    assert level == 2


def test_schema_validations():
    # Missing dimension
    incomplete = {"question_validity": 4, "empirical_progress": 4}
    with pytest.raises(StepBackInvalidSchemaError, match="Missing required rubric dimension"):
        evaluate_rubric(incomplete)

    # Score out of bounds (< 1)
    out_of_bounds_low = {dim: 4 for dim in CANONICAL_DIMENSIONS}
    out_of_bounds_low["question_validity"] = 0
    with pytest.raises(StepBackInvalidSchemaError, match="out of valid range"):
        evaluate_rubric(out_of_bounds_low)

    # Score out of bounds (> 5)
    out_of_bounds_high = {dim: 4 for dim in CANONICAL_DIMENSIONS}
    out_of_bounds_high["question_validity"] = 6
    with pytest.raises(StepBackInvalidSchemaError, match="out of valid range"):
        evaluate_rubric(out_of_bounds_high)


def test_exhaustive_gap_free_partition_coverage():
    """Verify all 3,125 score combinations map deterministically with zero gaps."""
    allowed_recs = {"CONTINUE", "PIVOT", "HOLD", "ESCALATE", "CONCLUDE_ABANDON"}
    allowed_levels = {0, 1, 2}

    weights = [DEFAULT_WEIGHTS[dim] for dim in CANONICAL_DIMENSIONS]

    count = 0
    for score_tuple in itertools.product([1, 2, 3, 4, 5], repeat=5):
        count += 1
        scores_dict = dict(zip(CANONICAL_DIMENSIONS, score_tuple))
        composite = sum(w * s for w, s in zip(weights, score_tuple))

        rec, level = resolve_rubric_recommendation(scores_dict, composite)

        assert rec in allowed_recs
        assert level in allowed_levels

        # Invariant 1: Any score == 1 must be HOLD and Level 2
        if 1 in score_tuple:
            assert rec == "HOLD"
            assert level == 2

        # Invariant 2: Any score == 2 prohibits CONTINUE
        if 2 in score_tuple:
            assert rec != "CONTINUE"

    assert count == 3125
