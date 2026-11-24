"""Tests for #1179 staged handoff reconciliation (advisory -> automatic)."""

from __future__ import annotations

import pytest

from bathos.handoff_reconciliation import (
    AgreementSample,
    HandoffReconciliationTracker,
    ReconciliationMode,
    RolloutPolicy,
    compare_handoff_to_catalog,
    compute_agreement_metrics,
    evaluate_agreement_rate,
    evaluate_rollout_gate,
    wilson_score_lower_bound,
)


def test_policy_validation_rejects_invalid_bounds() -> None:
    with pytest.raises(ValueError, match="Thresholds"):
        RolloutPolicy(promotion_threshold=0.80, demotion_threshold=0.90)
    with pytest.raises(ValueError, match="min_samples_for_promotion"):
        RolloutPolicy(min_samples_for_promotion=0)
    with pytest.raises(ValueError, match="window_size"):
        RolloutPolicy(window_size=0)
    with pytest.raises(ValueError, match="min_samples_for_promotion"):
        RolloutPolicy(min_samples_for_promotion=11, window_size=10)
    with pytest.raises(ValueError, match="min_samples_for_demotion"):
        RolloutPolicy(min_samples_for_promotion=5, min_samples_for_demotion=11, window_size=10)


def test_compare_handoff_to_catalog_handles_matches_tolerances_and_mismatches() -> None:
    handoff = {
        "run_id": "run-1",
        "outcome": "pass",
        "status": "completed",
        "metrics": {"loss": 0.1250001},
    }
    catalog = {
        "run_id": "run-1",
        "outcome": "pass",
        "status": "completed",
        "metrics": {"loss": 0.125},
    }
    match_sample = compare_handoff_to_catalog(handoff, catalog, numeric_tolerance=1e-5)
    assert match_sample.agreed is True
    assert match_sample.discrepancies == ()

    mismatch_sample = compare_handoff_to_catalog(
        {"run_id": "run-2", "outcome": "pass"},
        {"run_id": "run-2", "outcome": "fail"},
    )
    assert mismatch_sample.agreed is False
    assert len(mismatch_sample.discrepancies) == 1
    assert mismatch_sample.discrepancies[0].reason == "value_mismatch"

    missing_sample = compare_handoff_to_catalog({"run_id": "run-3", "outcome": "pass"}, None)
    assert missing_sample.agreed is False
    assert missing_sample.discrepancies[0].reason == "missing_in_catalog"


def test_staged_promotion_requires_min_samples_and_threshold() -> None:
    policy = RolloutPolicy(
        promotion_threshold=0.80,
        demotion_threshold=0.70,
        min_samples_for_promotion=16,
        min_samples_for_demotion=4,
        window_size=20,
        max_consecutive_disagreements=3,
    )
    tracker = HandoffReconciliationTracker(policy=policy)
    assert tracker.mode == ReconciliationMode.ADVISORY

    # 15 agreeing samples < min_samples_for_promotion=16 -> stays advisory
    for i in range(15):
        trans = tracker.record_sample(AgreementSample(sample_id=f"r{i}", agreed=True))
        assert trans.new_mode == ReconciliationMode.ADVISORY
        assert trans.transition == "unchanged"
        assert trans.auto_apply_allowed is False

    # 16th agreeing sample reaches min_samples and Wilson 95% lower bound (~0.806) >= 0.80 -> promotes
    promoted = tracker.record_sample(AgreementSample(sample_id="r15", agreed=True))
    assert promoted.previous_mode == ReconciliationMode.ADVISORY
    assert promoted.new_mode == ReconciliationMode.AUTOMATIC
    assert promoted.transition == "promoted"
    assert promoted.auto_apply_allowed is True
    assert tracker.mode == ReconciliationMode.AUTOMATIC


def test_safe_fallback_demotion_on_rolling_drop_and_consecutive_disagreements() -> None:
    policy = RolloutPolicy(
        promotion_threshold=0.80,
        demotion_threshold=0.70,
        min_samples_for_promotion=16,
        min_samples_for_demotion=4,
        window_size=20,
        max_consecutive_disagreements=2,
    )
    tracker = HandoffReconciliationTracker(policy=policy)
    for i in range(16):
        tracker.record_sample(AgreementSample(sample_id=f"ok-{i}", agreed=True))
    assert tracker.mode == ReconciliationMode.AUTOMATIC

    # Single disagreement keeps automatic (rate 16/17 = 0.941 >= 0.70, consecutive=1 < 2)
    t1 = tracker.record_sample(AgreementSample(sample_id="bad-1", agreed=False))
    assert t1.new_mode == ReconciliationMode.AUTOMATIC

    # Second consecutive disagreement triggers circuit-breaker demotion -> advisory
    t2 = tracker.record_sample(AgreementSample(sample_id="bad-2", agreed=False))
    assert t2.previous_mode == ReconciliationMode.AUTOMATIC
    assert t2.new_mode == ReconciliationMode.ADVISORY
    assert t2.transition == "demoted"
    assert t2.auto_apply_allowed is False

    # Also verify non-consecutive rolling agreement drop below demotion_threshold demotes
    metrics_drop = compute_agreement_metrics(
        [
            AgreementSample("a1", True),
            AgreementSample("a2", False),
            AgreementSample("a3", True),
            AgreementSample("a4", False),
            AgreementSample("a5", True),
        ],
        window_size=5,
    )
    assert evaluate_agreement_rate([AgreementSample("x", True), AgreementSample("y", False)]) == 0.5
    gate = evaluate_rollout_gate(ReconciliationMode.AUTOMATIC, metrics_drop, policy)
    assert gate.new_mode == ReconciliationMode.ADVISORY
    assert gate.transition == "demoted"


def test_reconcile_batch_and_serialization_roundtrip() -> None:
    policy = RolloutPolicy(
        promotion_threshold=0.75,
        demotion_threshold=0.60,
        min_samples_for_promotion=12,
        min_samples_for_demotion=4,
        window_size=20,
        max_consecutive_disagreements=3,
    )
    tracker = HandoffReconciliationTracker(policy=policy)

    handoffs = [
        {"run_id": f"run-{i}", "outcome": "pass", "status": "completed"} for i in range(12)
    ]
    catalog = {
        f"run-{i}": {"run_id": f"run-{i}", "outcome": "pass", "status": "completed"}
        for i in range(12)
    }

    outcome = tracker.reconcile_batch(handoffs, catalog)
    assert outcome.mode == ReconciliationMode.AUTOMATIC
    assert outcome.auto_applied is True
    assert outcome.transition.transition == "promoted"

    # Batch with a discrepancy returns auto_applied=False and surfaces advisory
    outcome_mismatch = tracker.reconcile_batch(
        [{"run_id": "run-13", "outcome": "pass", "status": "completed"}],
        {"run-13": {"run_id": "run-13", "outcome": "fail", "status": "completed"}},
    )
    assert outcome_mismatch.auto_applied is False
    assert any("run-13" in adv for adv in outcome_mismatch.advisories)

    restored = HandoffReconciliationTracker.from_dict(tracker.to_dict())
    assert restored.mode == tracker.mode
    assert restored.policy == tracker.policy
    assert restored.history == tracker.history


def test_wilson_lower_bound_and_no_zero_sample_oscillation() -> None:
    # 19/20 = 0.95 raw has Wilson 95% lower bound ~0.764 < 0.95 -> must NOT promote
    assert wilson_score_lower_bound(19, 20) < 0.80
    strict_policy = RolloutPolicy(
        promotion_threshold=0.95,
        demotion_threshold=0.90,
        min_samples_for_promotion=20,
        min_samples_for_demotion=5,
        window_size=100,
        max_consecutive_disagreements=3,
    )
    samples_19_of_20 = [AgreementSample(f"s{i}", True) for i in range(19)] + [
        AgreementSample("s19", False)
    ]
    metrics_19 = compute_agreement_metrics(samples_19_of_20, window_size=100)
    assert evaluate_rollout_gate(ReconciliationMode.ADVISORY, metrics_19, strict_policy).new_mode == (
        ReconciliationMode.ADVISORY
    )

    # Zero-sample oscillation prevention after demotion
    tracker = HandoffReconciliationTracker(policy=strict_policy)
    for i in range(97):
        tracker.record_sample(AgreementSample(f"ok-{i}", True))
    assert tracker.mode == ReconciliationMode.AUTOMATIC

    for j in range(3):
        tracker.record_sample(AgreementSample(f"bad-{j}", False))
    assert tracker.mode == ReconciliationMode.ADVISORY

    # Calling evaluate_gate() with 0 new samples must stay ADVISORY (no oscillation!)
    re_eval = tracker.evaluate_gate()
    assert re_eval.new_mode == ReconciliationMode.ADVISORY
    assert re_eval.transition == "unchanged"

    # Fewer than min_samples_for_demotion clean samples must also stay ADVISORY
    for k in range(strict_policy.min_samples_for_demotion - 1):
        t = tracker.record_sample(AgreementSample(f"clean-{k}", True))
        assert t.new_mode == ReconciliationMode.ADVISORY


def test_from_dict_strict_validation_and_mode_recomputation() -> None:
    # Corrupted string "false" for agreed must raise ValueError (not coerce to True)
    with pytest.raises(ValueError, match="agreed"):
        HandoffReconciliationTracker.from_dict(
            {
                "mode": "advisory",
                "history": [{"sample_id": "s1", "agreed": "false"}],
            }
        )

    # Malformed shapes raise ValueError
    with pytest.raises(ValueError):
        HandoffReconciliationTracker.from_dict({"mode": "automatic", "history": "not-a-list"})
    with pytest.raises(ValueError):
        HandoffReconciliationTracker.from_dict({"mode": "invalid"})

    # Persisted mode="automatic" without qualifying history downgrades safely to ADVISORY
    downgraded = HandoffReconciliationTracker.from_dict({"mode": "automatic", "history": []})
    assert downgraded.mode == ReconciliationMode.ADVISORY
