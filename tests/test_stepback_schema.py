"""Tests for Step-Back Review and Escalation Data Model (spec 261027)."""

import json

from bathos.stepback.schema import (
    DissentRecord,
    EscalationLevel,
    EscalationRecord,
    EscalationState,
    EscalationStatus,
    OpenAnalysis,
    OpenAnalysisMode,
    Recommendation,
    ReviewPath,
    ReviewRubricScore,
    StepBackEvaluation,
    StepBackReview,
    StepBackVerdict,
    build_input_snapshot,
)


def test_review_rubric_score():
    score = ReviewRubricScore(dimension_id="question_validity", score=4, rationale="Strong hypothesis")
    d = score.to_dict()
    assert d == {"dimension_id": "question_validity", "score": 4, "rationale": "Strong hypothesis"}
    roundtrip = ReviewRubricScore.from_dict(d)
    assert roundtrip == score


def test_open_analysis_mode_normalization():
    assert OpenAnalysisMode.normalize("advisor_convo") == OpenAnalysisMode.ADVISOR_CONVO
    assert OpenAnalysisMode.normalize("advisor") == OpenAnalysisMode.ADVISOR_CONVO
    assert OpenAnalysisMode.normalize("pre_mortem") == OpenAnalysisMode.PRE_MORTEM
    assert OpenAnalysisMode.normalize("premortem") == OpenAnalysisMode.PRE_MORTEM
    assert OpenAnalysisMode.normalize("so_what") == OpenAnalysisMode.SO_WHAT
    assert OpenAnalysisMode.normalize("outsider") == OpenAnalysisMode.OUTSIDER
    assert OpenAnalysisMode.normalize("steelman") == OpenAnalysisMode.STEELMAN
    assert OpenAnalysisMode.normalize("steelman_the_stop") == OpenAnalysisMode.STEELMAN


def test_open_analysis_dataclass():
    oa = OpenAnalysis(
        mode=OpenAnalysisMode.PRE_MORTEM.value,
        sections={"failure_narrative": "Catastrophic parameter drift"},
    )
    d = oa.to_dict()
    assert d["mode"] == "pre_mortem"
    assert d["sections"]["failure_narrative"] == "Catastrophic parameter drift"
    roundtrip = OpenAnalysis.from_dict(d)
    assert roundtrip.mode == oa.mode
    assert roundtrip.sections == oa.sections


def test_dissent_record_roundtrip():
    dissent = DissentRecord(
        dissenting_reviewer_id="reviewer_beta",
        minority_recommendation="HOLD",
        minority_composite_score=2.2,
        dissent_summary="Disagrees with stability claim",
        cited_counter_evidence=["run:run_123"],
        majority_rebuttal="Outlier was discarded",
    )
    d = dissent.to_dict()
    assert d["dissenting_reviewer_id"] == "reviewer_beta"
    assert d["minority_recommendation"] == "HOLD"
    assert d["minority_composite_score"] == 2.2
    assert d["cited_counter_evidence"] == ["run:run_123"]

    roundtrip = DissentRecord.from_dict(d)
    assert roundtrip == dissent


def test_build_input_snapshot_deterministic():
    snapshot_json1, sha1 = build_input_snapshot(
        campaign_id="camp_01",
        evaluated_run_ids=["run_b", "run_a"],
        latest_run_sha="sha_latest",
        sidecar_shas={"sidecar.toml": "sha_sc"},
        git_head_commit="commit_123",
    )
    snapshot_json2, sha2 = build_input_snapshot(
        campaign_id="camp_01",
        evaluated_run_ids=["run_a", "run_b"],  # different input order
        latest_run_sha="sha_latest",
        sidecar_shas={"sidecar.toml": "sha_sc"},
        git_head_commit="commit_123",
    )
    # Sorted internally, so identical
    assert snapshot_json1 == snapshot_json2
    assert sha1 == sha2

    parsed = json.loads(snapshot_json1)
    assert parsed["campaign_id"] == "camp_01"
    assert parsed["evaluated_run_ids"] == ["run_a", "run_b"]


def test_stepback_review_signature_and_serialization():
    snapshot_json, snapshot_sha = build_input_snapshot("camp_01", ["run_1", "run_2"])
    dissent = DissentRecord(
        dissenting_reviewer_id="rev_dissenter",
        minority_recommendation=Recommendation.HOLD.value,
        dissent_summary="Minority concerns",
    )
    analysis = OpenAnalysis(
        mode=OpenAnalysisMode.ADVISOR_CONVO.value,
        sections={"premise_challenge": "Is this testable?"},
    )

    review = StepBackReview(
        review_id="rev_0123456789ab",
        campaign_id="camp_01",
        project_slug="test-slug",
        created_at="2026-10-27T12:00:00Z",
        reviewer_id="rev_alpha",
        reviewer_kind="agent",
        is_independent=True,
        path_kind=ReviewPath.HYBRID.value,
        input_snapshot_sha256=snapshot_sha,
        input_snapshot=snapshot_json,
        recommendation=Recommendation.PIVOT.value,
        escalation_level=EscalationLevel.WARNING.value,
        cited_evidence=["run:run_1", "sidecar:test.bth.toml"],
        rubric_scores={"question_validity": 3, "empirical_progress": 2},
        composite_score=3.1,
        open_analysis=analysis,
        dissents=[dissent],
        dispatched_at="2026-10-27T11:55:00Z",
    )

    # Signature automatically computed
    assert review.signature_sha256 is not None
    assert len(review.signature_sha256) == 64

    # Serialization roundtrip
    d = review.to_dict()
    roundtrip = StepBackReview.from_dict(d)
    assert roundtrip.review_id == review.review_id
    assert roundtrip.signature_sha256 == review.signature_sha256
    assert isinstance(roundtrip.open_analysis, OpenAnalysis)
    assert roundtrip.open_analysis.sections == analysis.sections
    assert len(roundtrip.dissents) == 1
    assert isinstance(roundtrip.dissents[0], DissentRecord)
    assert roundtrip.dissents[0].dissenting_reviewer_id == "rev_dissenter"


def test_signature_changes_on_content_tampering():
    snapshot_json, snapshot_sha = build_input_snapshot("camp_01", ["run_1"])
    review = StepBackReview(
        review_id="rev_01",
        campaign_id="camp_01",
        project_slug="slug",
        created_at="2026-10-27T12:00:00Z",
        reviewer_id="rev_alpha",
        reviewer_kind="human",
        is_independent=True,
        path_kind=ReviewPath.RUBRIC.value,
        input_snapshot_sha256=snapshot_sha,
        input_snapshot=snapshot_json,
        recommendation=Recommendation.CONTINUE.value,
        escalation_level=0,
    )
    sig1 = review.signature_sha256

    # Tampering with score
    review.composite_score = 4.8
    sig2 = review.compute_signature()
    assert sig1 != sig2


def test_escalation_record_roundtrip():
    rec = EscalationRecord(
        escalation_id="esc_0123456789ab",
        campaign_id="camp_01",
        project_slug="test-slug",
        level=EscalationLevel.HOLD.value,
        status=EscalationStatus.ACTIVE.value,
        opened_at="2026-10-27T12:00:00Z",
        opened_by_kind="trigger",
        trigger_name="repeated_marginal",
        trigger_reason="3 consecutive marginal runs",
        review_epoch=1,
        pivot_anchor_run_id="run_anchor_1",
    )
    d = rec.to_dict()
    assert d["level"] == 2
    assert d["status"] == "active"
    assert d["trigger_name"] == "repeated_marginal"
    assert d["review_epoch"] == 1
    assert d["pivot_anchor_run_id"] == "run_anchor_1"

    roundtrip = EscalationRecord.from_dict(d)
    assert roundtrip == rec


def test_escalation_state_defaults_and_roundtrip():
    state = EscalationState(campaign_id="camp_01")
    assert state.current_level == 0
    assert state.status == "nominal"
    assert state.review_epoch == 0
    assert state.pivot_anchor_run_id is None
    assert state.active_triggers == []

    state.current_level = 2
    state.status = "hold"
    state.active_triggers = ["repeated_marginal"]
    d = state.to_dict()
    roundtrip = EscalationState.from_dict(d)
    assert roundtrip == state


def test_stepback_verdict_roundtrip():
    verdict = StepBackVerdict(
        ok=False,
        hold_active=True,
        escalation_level=2,
        recommendation="HOLD",
        active_triggers=["near_bound"],
        message="Campaign is currently held pending review",
    )
    d = verdict.to_dict()
    roundtrip = StepBackVerdict.from_dict(d)
    assert roundtrip == verdict


def test_stepback_evaluation_roundtrip():
    evaluation = StepBackEvaluation(
        evaluation_id="eval_0123456789ab",
        review_id="rev_0123456789ab",
        campaign_id="camp_01",
        evaluated_at="2026-10-27T14:00:00Z",
        excess_spend_avoided=10.0,
        predictive_validity=True,
        trajectory_slope=0.15,
        vacuous_pass_intercepted=True,
        subsequent_runs_count=0,
        subsequent_outcomes=[],
        notes="Avoided full 10-run epoch after hold",
        metadata={"cost_model": "gpu_hours"},
    )
    d = evaluation.to_dict()
    roundtrip = StepBackEvaluation.from_dict(d)
    assert roundtrip == evaluation
    assert roundtrip.excess_spend_avoided == 10.0
    assert roundtrip.vacuous_pass_intercepted is True

