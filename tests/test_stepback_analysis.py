"""Tests for Qualitative Open Analysis and Evidence Citation Validation (spec 261027 Section 3.2 & 3.3)."""

import pytest

from bathos.errors import (
    StepBackCitationInvalidError,
    StepBackIndependenceViolationError,
    StepBackInsufficientEvidenceError,
    StepBackInvalidSchemaError,
)
from bathos.stepback.analysis import (
    check_reviewer_independence,
    evaluate_downstream_outcomes,
    get_mode_prompt,
    parse_citation,
    validate_campaign_evidence,
    validate_open_analysis,
)


def test_parse_citation_prefixes():
    assert parse_citation("run:8f3a9b1c-1234") == ("run", "8f3a9b1c-1234")
    assert parse_citation("sidecar:scripts/nvt.bth.toml#outcomes.pass") == (
        "sidecar",
        "scripts/nvt.bth.toml#outcomes.pass",
    )
    assert parse_citation("clause:.bth/claims/claim.toml#G1") == ("clause", ".bth/claims/claim.toml#G1")
    assert parse_citation("git:7a8b9c0d") == ("git", "7a8b9c0d")
    assert parse_citation("outcome:8f3a9b1c:pass") == ("outcome", "8f3a9b1c:pass")
    assert parse_citation("invalid_citation") == ("unknown", "invalid_citation")


def test_validate_open_analysis_all_modes_complete():
    # Advisor
    adv = validate_open_analysis(
        {
            "mode": "advisor_convo",
            "sections": {
                "premise_challenge": "Is Langevin gamma physically reasonable?",
                "simplest_decisive_test": "Run 10ps NVE simulation with zero thermostat coupling",
                "strategic_advice": "Check energy drift before tuning friction",
            },
        },
        recommendation="PIVOT",
    )
    assert adv.mode == "advisor_convo"

    # Pre-Mortem
    pm = validate_open_analysis(
        {
            "mode": "pre_mortem",
            "sections": {
                "failure_narrative": "The campaign failed because solvent effects were omitted",
                "overlooked_red_flags": "Initial density was 15% too low",
                "present_day_symptoms": "System drifts monotonically",
                "preventative_mitigations": "Switch to explicit water model",
            },
        },
        recommendation="HOLD",
    )
    assert pm.mode == "pre_mortem"

    # So-What
    sw = validate_open_analysis(
        {
            "mode": "so_what",
            "sections": {
                "best_case_scenario": "Confirms known bulk property",
                "practical_utility": "Minimal beyond textbook confirmation",
                "decision_impact": "Does not change downstream drug design",
                "opportunity_cost": "Consuming 4 H100 GPUs that could screen binders",
            },
        },
        recommendation="CONCLUDE_ABANDON",
    )
    assert sw.mode == "so_what"

    # Outsider
    out = validate_open_analysis(
        {
            "mode": "outsider",
            "sections": {
                "jargon_free_summary": "Testing if heating molecules shakes them apart",
                "common_sense_vulnerabilities": "Box boundary might be too small",
                "alternative_explanations": "Thermostat algorithm bug",
            },
        },
        recommendation="CONTINUE",
    )
    assert out.mode == "outsider"

    # Steelman
    stl = validate_open_analysis(
        {
            "mode": "steelman",
            "sections": {
                "the_case_for_stopping": "10 consecutive runs show zero binding affinity signal",
                "sunk_cost_fallacies_exposed": "Spending another 2 weeks will not fix affinity",
                "superior_resource_reallocation": "Pivot to computational binder library",
                "retained_learnings": "Negative result eliminates peptide scaffold A",
            },
        },
        recommendation="CONCLUDE_ABANDON",
    )
    assert stl.mode == "steelman"


def test_validate_open_analysis_missing_or_empty_sections():
    # Missing required section
    with pytest.raises(StepBackInvalidSchemaError, match="missing required sections"):
        validate_open_analysis(
            {
                "mode": "advisor_convo",
                "sections": {"premise_challenge": "Test"},
            }
        )

    # Empty required section
    with pytest.raises(StepBackInvalidSchemaError, match="empty required sections"):
        validate_open_analysis(
            {
                "mode": "advisor_convo",
                "sections": {
                    "premise_challenge": "Valid premise",
                    "simplest_decisive_test": "",
                    "strategic_advice": "Advice",
                },
            }
        )


def test_validate_open_analysis_invalid_mode_or_recommendation():
    with pytest.raises(StepBackInvalidSchemaError, match="Invalid OpenAnalysisMode"):
        validate_open_analysis({"mode": "invalid_mode", "sections": {}})

    with pytest.raises(StepBackInvalidSchemaError, match="Invalid recommendation"):
        validate_open_analysis(
            {
                "mode": "advisor_convo",
                "sections": {
                    "premise_challenge": "A",
                    "simplest_decisive_test": "B",
                    "strategic_advice": "C",
                },
            },
            recommendation="INVALID_VERDICT",
        )


def test_campaign_evidence_verification_and_laundering_rejection():
    campaign = {"id": "camp_nvt", "claim_path": ".bth/claims/claim_nvt.toml"}
    member_runs = [
        {"run_id": "run_nvt_001", "script_path": "scripts/experiments/nvt.py", "git_commit": "abcdef123456"},
        {"run_id": "run_nvt_002", "sidecar_path": "scripts/experiments/nvt.bth.toml"},
    ]

    # Valid citations
    valid_cits = [
        "run:run_nvt_001",
        "sidecar:scripts/experiments/nvt.bth.toml#outcomes.pass",
        "clause:.bth/claims/claim_nvt.toml#G1",
    ]
    val, inval = validate_campaign_evidence(valid_cits, campaign, member_runs)
    assert len(val) == 3
    assert len(inval) == 0

    # Evidence laundering: citing run from another campaign
    with pytest.raises(StepBackCitationInvalidError, match="does not belong to campaign"):
        validate_campaign_evidence(
            ["run:run_foreign_999", "sidecar:scripts/experiments/nvt.bth.toml"],
            campaign,
            member_runs,
        )

    # Citing sidecar not in campaign
    with pytest.raises(StepBackCitationInvalidError, match="not associated with any member runs"):
        validate_campaign_evidence(
            ["run:run_nvt_001", "sidecar:scripts/benchmarks/foreign.bth.toml"],
            campaign,
            member_runs,
        )

    # Citing clause from mismatched claim file
    with pytest.raises(StepBackCitationInvalidError, match="does not match campaign claim_path"):
        validate_campaign_evidence(
            ["run:run_nvt_001", "clause:.bth/claims/other_claim.toml#G1"],
            campaign,
            member_runs,
        )


def test_insufficient_evidence_threshold():
    campaign = {"id": "camp_nvt"}
    member_runs = [{"run_id": "run_001", "script_path": "scripts/experiments/test.py"}]

    # Only 1 valid citation (< 2 required)
    with pytest.raises(StepBackInsufficientEvidenceError, match="at least 2 valid"):
        validate_campaign_evidence(["run:run_001"], campaign, member_runs)


def test_reviewer_independence():
    campaign = {"id": "camp_nvt", "creator": "alice"}
    member_runs = [
        {"run_id": "r1", "agent_mode": "agent_alpha", "metadata": {"author": "bob"}},
    ]

    # alice (creator) is forbidden
    assert not check_reviewer_independence("alice", campaign, member_runs)

    # agent_alpha is forbidden
    assert not check_reviewer_independence("agent_alpha", campaign, member_runs)

    # bob (run author) is forbidden
    assert not check_reviewer_independence("bob", campaign, member_runs)

    # Independent third party (charlie) is independent
    assert check_reviewer_independence("charlie", campaign, member_runs)

    # Conflict raises if requested
    with pytest.raises(StepBackIndependenceViolationError, match="conflict of interest"):
        check_reviewer_independence("alice", campaign, member_runs, raise_on_conflict=True)


def test_mode_prompts_replicate_and_unfalsifiable_inquiry():
    advisor_text = get_mode_prompt("advisor_convo")
    assert "statistical independence" in advisor_text
    assert "replicate" in advisor_text.lower()
    assert "FAIL" in advisor_text

    outsider_text = get_mode_prompt("outsider")
    assert "replicates" in outsider_text.lower()
    assert "cannot fail" in outsider_text.lower()

    pre_mortem_text = get_mode_prompt("pre_mortem")
    assert "replicate pooling" in pre_mortem_text.lower()
    assert "unfalsifiable" in pre_mortem_text.lower()


def test_evaluate_downstream_outcomes_hold_and_spend_avoided():
    review = {
        "review_id": "rev_hold_01",
        "campaign_id": "camp_plateau",
        "recommendation": "HOLD",
        "rubric_scores": {"question_validity": 1, "empirical_progress": 2},
    }
    # 0 subsequent runs -> halted successfully -> spend avoided = 10.0
    ev = evaluate_downstream_outcomes(review, subsequent_runs=[])
    assert ev.review_id == "rev_hold_01"
    assert ev.excess_spend_avoided == 10.0
    assert ev.predictive_validity is True
    assert ev.vacuous_pass_intercepted is True


def test_evaluate_downstream_outcomes_continue_trajectory():
    review = {
        "review_id": "rev_cont_01",
        "campaign_id": "camp_growth",
        "recommendation": "CONTINUE",
    }
    # Progressing subsequent runs
    good_runs = [
        {"outcome": "pass", "metric": 1.0},
        {"outcome": "pass", "metric": 2.0},
        {"outcome": "pass", "metric": 3.0},
    ]
    ev_good = evaluate_downstream_outcomes(review, subsequent_runs=good_runs)
    assert ev_good.predictive_validity is True
    assert ev_good.trajectory_slope is not None and ev_good.trajectory_slope > 0

    # Bad runs following continue recommendation
    bad_runs = [
        {"outcome": "fail", "metric": 0.5},
        {"outcome": "fail", "metric": 0.4},
        {"outcome": "fail", "metric": 0.2},
    ]
    ev_bad = evaluate_downstream_outcomes(review, subsequent_runs=bad_runs)
    assert ev_bad.predictive_validity is False
    assert ev_bad.trajectory_slope is not None and ev_bad.trajectory_slope < 0

