"""Adversarial and Mutation-Resilience Test Suite for Task 261027_step-back-reviews.

Executed in isolated sandbox with temporary catalog.
Tests strictly target worktree code.

Covers:
1. Human Chair Invariance (FastMCP & CLI refusal)
2. Evidence Laundering (Cross-campaign citation rejection)
3. Boundary Extremes & Singularities (N=0,1, B=0, zero variance, NaN/None)
4. Decision D1 Conclude Gate (Automatic downgrade to 'confounded')
5. DuckDB Compaction (Cool to warm tier ingestion & idempotency)
6. Reviewer Independence & Attribution Protection (Self-review prevention)
7. Floor Veto & Flaw Capping in Rubrics (Inviolable veto invariants)
8. Mandatory Recommendation in Path B (Schema strictness)
9. Automatic Escalation on Panel Dissent (Unresolved minority protection)
10. Anti-Rubber-Stamp Meta-Signals 15-18 (Surveillance detection)
11. Mutation-Resilience & Security Invariants (Bypass resistance)
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb

from bathos.errors import (
    EXCEPTION_TO_CODE,
    BathosErrorCode,
    StepBackAgentOverrideRefusedError,
    StepBackCitationInvalidError,
    StepBackIndependenceViolationError,
    StepBackInsufficientEvidenceError,
    StepBackInvalidSchemaError,
    StepBackUnauthorizedChairError,
)
from bathos.stepback.analysis import (
    check_reviewer_independence,
    validate_campaign_evidence,
    validate_open_analysis,
)
from bathos.stepback.catalog import (
    get_escalation_state,
    ingest_stepback_fragments,
    list_reviews,
    save_escalation_cool,
    save_review_cool,
)
from bathos.stepback.engine import (
    record_stepback_review,
    resolve_stepback,
)
from bathos.stepback.escalation import (
    AGENT_ENV_VARS,
    is_agent_environment,
    transition_escalation,
)
from bathos.stepback.rubrics import (
    evaluate_rubric,
)
from bathos.stepback.schema import (
    DissentRecord,
    EscalationRecord,
    EscalationState,
    OpenAnalysis,
    StepBackReview,
)
from bathos.stepback.triggers import (
    evaluate_all_triggers,
    evaluate_campaign_staleness,
    evaluate_entropy_collapse,
    evaluate_metric_plateau,
    evaluate_near_bound_hits,
    evaluate_repeated_marginal,
    evaluate_scale_up,
    evaluate_threshold_edit,
)


class MockCampaign:
    def __init__(self, camp_id: str, mode: str = "confirmatory", claim_path: str | None = None, creator: str = "lead_researcher"):
        self.id = camp_id
        self.campaign_id = camp_id
        self.mode = mode
        self.claim_path = claim_path
        self.creator = creator
        self.status = "open"
        self.project_slug = "test_project"
        self.created_at = "2026-10-27T10:00:00Z"
        self.runs = []


class TestHumanChairInvariance(unittest.TestCase):
    """Harness 1: Human Chair Invariance.

    Verifies that Level 3 escalations can NEVER be resolved by autonomous agents
    via FastMCP or CLI, and that non-interactive/non-token requests are strictly refused.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cat_dir = Path(self.tmp_dir.name)
        self.camp_id = "camp_l3_invariance"

        # Create active Level 3 escalation in catalog
        self.esc_rec = EscalationRecord(
            escalation_id="esc_l3_test",
            campaign_id=self.camp_id,
            project_slug="test_proj",
            level=3,
            status="active",
            opened_at="2026-10-27T12:00:00Z",
            opened_by_kind="system",
            trigger_reason="Severe confound detected",
        )
        save_escalation_cool(self.esc_rec, self.cat_dir)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_fastmcp_resolve_level3_unconditionally_refused(self):
        """FastMCP stepback_resolve must unconditionally refuse Level 3 resolution."""
        import asyncio

        from bathos.mcp import stepback_resolve as mcp_stepback_resolve

        # 1. Under engine with strict_agent_refusal=True: raises StepBackAgentOverrideRefusedError
        with self.assertRaises(StepBackAgentOverrideRefusedError) as ctx:
            resolve_stepback(
                escalation_id="esc_l3_test",
                campaign_id=self.camp_id,
                deescalate_to=0,
                catalog_dir=self.cat_dir,
                strict_agent_refusal=True,
            )
        self.assertIn("strictly forbidden", str(ctx.exception).lower())

        # 2. Under FastMCP wrapper: returns structured error envelope
        with patch.dict(os.environ, {"BTH_CATALOG_DIR": str(self.cat_dir)}):
            res = asyncio.run(
                mcp_stepback_resolve(
                    escalation_id="esc_l3_test",
                    campaign_id=self.camp_id,
                    deescalate_to=0,
                    as_human_chair=True,  # Even if agent spoofingly sets as_human_chair=True
                )
            )
            self.assertFalse(res["ok"])
            self.assertEqual(res["error_code"], "stepback_agent_override_refused")
            self.assertIn("strictly forbidden", res["error"].lower())

            # Verify escalation state remains active Level 3
            state = get_escalation_state(self.camp_id, catalog_dir=self.cat_dir)
            self.assertEqual(state.current_level, 3)

    def test_cli_resolve_in_agent_environment_refused(self):
        """CLI resolve must refuse when agent environment variables are present."""
        for agent_var in ("CLAUDE_AGENT", "BTH_AGENT_MODE", "AGENT_NAME", "ANTIGRAVITY_AGENT"):
            with patch.dict(os.environ, {agent_var: "true"}):
                self.assertTrue(is_agent_environment())
                with self.assertRaises(StepBackAgentOverrideRefusedError) as ctx:
                    resolve_stepback(
                        escalation_id="esc_l3_test",
                        campaign_id=self.camp_id,
                        deescalate_to=0,
                        as_human_chair=True,
                        interactive_tty=False,
                        catalog_dir=self.cat_dir,
                        strict_agent_refusal=False,
                    )
                self.assertIn("autonomous agents are strictly forbidden", str(ctx.exception).lower())

    def test_cli_resolve_non_tty_no_token_refused(self):
        """CLI resolve without TTY and without valid token must raise UnauthorizedChair."""
        # Clean environment without agent env vars
        clean_env = {v: "" for v in AGENT_ENV_VARS}
        with patch.dict(os.environ, clean_env, clear=False):
            # 1. Without as_human_chair
            with self.assertRaises(StepBackUnauthorizedChairError):
                resolve_stepback(
                    escalation_id="esc_l3_test",
                    campaign_id=self.camp_id,
                    deescalate_to=0,
                    as_human_chair=False,
                    interactive_tty=False,
                    chair_token=None,
                    catalog_dir=self.cat_dir,
                    strict_agent_refusal=False,
                )

            # 2. With as_human_chair=True but no TTY and invalid token
            with self.assertRaises(StepBackUnauthorizedChairError):
                resolve_stepback(
                    escalation_id="esc_l3_test",
                    campaign_id=self.camp_id,
                    deescalate_to=0,
                    as_human_chair=True,
                    interactive_tty=False,
                    chair_token="invalid_token_12345",
                    catalog_dir=self.cat_dir,
                    strict_agent_refusal=False,
                )

    def test_human_chair_authorized_resolution_succeeds(self):
        """Human Chair with valid token or TTY can successfully de-escalate Level 3."""
        clean_env = {v: "" for v in AGENT_ENV_VARS}
        with patch.dict(os.environ, clean_env, clear=False):
            # Test with interactive_tty=True and as_human_chair=True
            res = resolve_stepback(
                escalation_id="esc_l3_test",
                campaign_id=self.camp_id,
                deescalate_to=0,
                as_human_chair=True,
                interactive_tty=True,
                note="Human PI reviewed and approved resolution",
                catalog_dir=self.cat_dir,
                strict_agent_refusal=False,
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["status"], "resolved")
            self.assertEqual(res["deescalated_to"], 0)

            # Check new escalation state
            state = get_escalation_state(self.camp_id, catalog_dir=self.cat_dir)
            self.assertEqual(state.current_level, 0)
            self.assertEqual(state.status, "nominal")

    def test_non_level3_escalation_resolvable_by_reviewer(self):
        """Level 1 or Level 2 escalations can be resolved without human chair gate."""
        esc_l2 = EscalationRecord(
            escalation_id="esc_l2_test",
            campaign_id="camp_l2",
            project_slug="test_proj",
            level=2,
            status="active",
            opened_at="2026-10-27T12:00:00Z",
            opened_by_kind="system",
            trigger_reason="Repeated marginal outcomes",
        )
        save_escalation_cool(esc_l2, self.cat_dir)

        res = resolve_stepback(
            escalation_id="esc_l2_test",
            campaign_id="camp_l2",
            deescalate_to=1,
            note="De-escalating to warning after pivot",
            catalog_dir=self.cat_dir,
            strict_agent_refusal=False,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["deescalated_to"], 1)


class TestEvidenceLaundering(unittest.TestCase):
    """Harness 2: Evidence Laundering.

    Verifies that review registration strictly rejects citations of runs,
    sidecars, and claim clauses belonging to unrelated campaigns.
    """

    def setUp(self):
        self.camp_alpha = MockCampaign("camp_alpha", claim_path=".bth/claims/alpha.toml")
        self.camp_beta = MockCampaign("camp_beta", claim_path=".bth/claims/beta.toml")

        self.runs_alpha = [
            {
                "run_id": "run_alpha_001_abc12345",
                "script": "scripts/experiments/test_alpha.py",
                "sidecar_path": "scripts/experiments/test_alpha.bth.toml",
                "git_commit": "abcdef1234567890",
                "author": "dev_alpha",
                "agent_mode": "agent_alpha",
            },
            {
                "run_id": "run_alpha_002_def67890",
                "script": "scripts/experiments/test_alpha.py",
                "sidecar_path": "scripts/experiments/test_alpha.bth.toml",
                "git_commit": "abcdef1234567890",
                "author": "dev_alpha",
                "agent_mode": "agent_alpha",
            },
        ]
        self.runs_beta = [
            {
                "run_id": "run_beta_001_xyz98765",
                "script": "scripts/experiments/test_beta.py",
                "sidecar_path": "scripts/experiments/test_beta.bth.toml",
                "git_commit": "123456abcdef7890",
                "author": "dev_beta",
                "agent_mode": "agent_beta",
            }
        ]

    def test_foreign_run_citation_rejected(self):
        """Citing a run from an unrelated campaign raises StepBackCitationInvalidError."""
        foreign_evidence = [
            "run:run_alpha_001_abc12345",
            "run:run_beta_001_xyz98765",  # foreign run from camp_beta
        ]
        with self.assertRaises(StepBackCitationInvalidError) as ctx:
            validate_campaign_evidence(
                foreign_evidence,
                self.camp_alpha,
                self.runs_alpha,
                min_citations=2,
                raise_on_invalid=True,
            )
        self.assertIn("does not belong to campaign", str(ctx.exception))

    def test_foreign_sidecar_citation_rejected(self):
        """Citing a sidecar from an unrelated campaign is rejected."""
        foreign_sidecar_ev = [
            "run:run_alpha_001_abc12345",
            "sidecar:scripts/experiments/test_beta.bth.toml#outcomes.pass",
        ]
        with self.assertRaises(StepBackCitationInvalidError) as ctx:
            validate_campaign_evidence(
                foreign_sidecar_ev,
                self.camp_alpha,
                self.runs_alpha,
                min_citations=2,
                raise_on_invalid=True,
            )
        self.assertIn("not associated with any member runs", str(ctx.exception))

    def test_foreign_claim_clause_citation_rejected(self):
        """Citing a claim clause not registered to the campaign is rejected."""
        foreign_clause_ev = [
            "run:run_alpha_001_abc12345",
            "clause:.bth/claims/unrelated_foreign.toml#G1",
        ]
        with self.assertRaises(StepBackCitationInvalidError) as ctx:
            validate_campaign_evidence(
                foreign_clause_ev,
                self.camp_alpha,
                self.runs_alpha,
                min_citations=2,
                raise_on_invalid=True,
            )
        self.assertIn("does not match campaign claim_path", str(ctx.exception))

    def test_insufficient_valid_evidence_rejected(self):
        """Reviews with < 2 valid citations are rejected."""
        one_citation = ["run:run_alpha_001_abc12345"]
        with self.assertRaises(StepBackInsufficientEvidenceError) as ctx:
            validate_campaign_evidence(
                one_citation,
                self.camp_alpha,
                self.runs_alpha,
                min_citations=2,
                raise_on_invalid=True,
            )
        self.assertIn("at least 2 valid primary domain citations", str(ctx.exception))

    def test_valid_campaign_domain_citations_accepted(self):
        """Citations belonging to the campaign domain pass validation."""
        valid_evidence = [
            "run:run_alpha_001_abc12345",
            "run:run_alpha_002_def67890",
            "sidecar:scripts/experiments/test_alpha.bth.toml#outcomes.pass",
            "clause:.bth/claims/alpha.toml#G1",
            "git:abcdef1234567890",
        ]
        valid, invalid = validate_campaign_evidence(
            valid_evidence,
            self.camp_alpha,
            self.runs_alpha,
            min_citations=2,
            raise_on_invalid=True,
        )
        self.assertEqual(len(valid), 5)
        self.assertEqual(len(invalid), 0)


class TestBoundaryExtremesAndSingularities(unittest.TestCase):
    """Harness 3: Boundary Extremes & Singularities.

    Tests mathematical trigger engine against:
    - N = 0 (empty runs)
    - N = 1 (single run)
    - Boundary B = 0
    - Zero variance / constant metric series
    - Near-zero metric series (< 0.01)
    - Entropy collapse on homogeneous outcomes (0 log 0)
    - Missing / malformed / non-numeric metrics
    """

    def test_n_equals_zero_all_triggers(self):
        """All triggers must gracefully handle N=0 without crashing."""
        camp = MockCampaign("camp_empty")
        empty_runs = []

        res_marginal = evaluate_repeated_marginal(empty_runs)
        self.assertFalse(res_marginal["fired"])
        self.assertEqual(res_marginal["reason"], "insufficient_runs")

        res_near_bound = evaluate_near_bound_hits(empty_runs, bound=1.0)
        self.assertFalse(res_near_bound["fired"])
        self.assertEqual(res_near_bound["reason"], "insufficient_runs")

        res_entropy = evaluate_entropy_collapse(empty_runs)
        self.assertFalse(res_entropy["fired"])
        self.assertEqual(res_entropy["reason"], "insufficient_runs")

        res_plateau = evaluate_metric_plateau(empty_runs)
        self.assertFalse(res_plateau["fired"])
        self.assertEqual(res_plateau["reason"], "insufficient_runs")

        res_stale = evaluate_campaign_staleness(camp, empty_runs)
        self.assertIsInstance(res_stale["fired"], bool)

        res_thresh = evaluate_threshold_edit(empty_runs)
        self.assertFalse(res_thresh["fired"])
        self.assertEqual(res_thresh["reason"], "insufficient_runs")

        res_scale = evaluate_scale_up(camp, proposed_tasks=0)
        self.assertFalse(res_scale["fired"])

        all_res = evaluate_all_triggers(camp, empty_runs)
        self.assertIsInstance(all_res, dict)
        self.assertIn("triggers_evaluated", all_res)

    def test_n_equals_one_all_triggers(self):
        """All triggers must gracefully handle N=1 without crashing."""
        camp = MockCampaign("camp_single")
        single_run = [{"run_id": "run_01", "outcome": "marginal", "metric": 0.5}]

        res_marginal = evaluate_repeated_marginal(single_run)
        self.assertFalse(res_marginal["fired"])

        res_near_bound = evaluate_near_bound_hits(single_run, bound=0.5)
        self.assertFalse(res_near_bound["fired"])

        res_entropy = evaluate_entropy_collapse(single_run)
        self.assertFalse(res_entropy["fired"])

        res_plateau = evaluate_metric_plateau(single_run)
        self.assertFalse(res_plateau["fired"])

        res_thresh = evaluate_threshold_edit(single_run)
        self.assertFalse(res_thresh["fired"])

        all_res = evaluate_all_triggers(camp, single_run)
        self.assertFalse(all_res["fired"])

    def test_boundary_b_equals_zero(self):
        """Near-bound trigger with B=0 must scale margin by sample stddev and not divide by zero."""
        runs_b0 = [
            {"run_id": f"r_{i}", "metric": 1e-7}
            for i in range(5)
        ]
        # Should execute without ZeroDivisionError
        res = evaluate_near_bound_hits(runs_b0, bound=0.0)
        self.assertIsInstance(res, dict)
        self.assertGreater(res["margin"], 0.0)
        self.assertFalse(res.get("fired") is None)

    def test_zero_variance_plateau(self):
        """Zero variance constant metric series must be handled without division by zero."""
        runs_const = [
            {"run_id": f"r_{i}", "metric": 42.0}
            for i in range(6)
        ]
        res = evaluate_metric_plateau(runs_const)
        self.assertEqual(res["slope"], 0.0)
        self.assertEqual(res["stddev"], 0.0)
        self.assertTrue(res["fired"])  # Constant line is a flat plateau!

    def test_near_zero_metrics_plateau(self):
        """Metrics near zero (|y| < 0.01) must use absolute thresholds and not blow up relative slope."""
        runs_near_zero = [
            {"run_id": f"r_{i}", "metric": 0.00001}
            for i in range(6)
        ]
        res = evaluate_metric_plateau(runs_near_zero)
        self.assertTrue(res["is_near_zero"])
        self.assertTrue(res["fired"])

    def test_entropy_collapse_singularities(self):
        """100% homogeneous outcomes must evaluate Shannon entropy without MathDomainError."""
        # 100% pass outcomes -> entropy == 0.0
        runs_pass = [{"run_id": f"r_{i}", "outcome": "pass"} for i in range(5)]
        res_pass = evaluate_entropy_collapse(runs_pass)
        self.assertTrue(res_pass["fired"])
        self.assertEqual(res_pass["entropy_nats"], 0.0)
        self.assertEqual(res_pass["annotation"], "fixation_positive")

        # 100% fail outcomes -> entropy == 0.0
        runs_fail = [{"run_id": f"r_{i}", "outcome": "fail"} for i in range(5)]
        res_fail = evaluate_entropy_collapse(runs_fail)
        self.assertTrue(res_fail["fired"])
        self.assertEqual(res_fail["annotation"], "fixation_negative")

    def test_malformed_non_numeric_metrics(self):
        """Non-numeric values (None, 'NaN', 'unknown', True/False) must not crash plateau/near-bound."""
        bad_runs = [
            {"run_id": "r_1", "metric": "not_a_number"},
            {"run_id": "r_2", "metric": None},
            {"run_id": "r_3", "metric": True},  # bool should be ignored
            {"run_id": "r_4", "metric": "NaN"},
        ]
        res_nb = evaluate_near_bound_hits(bad_runs, bound=1.0)
        self.assertFalse(res_nb["fired"])
        self.assertIn(res_nb["reason"], ("insufficient_runs", "no_numeric_metric"))

        res_pl = evaluate_metric_plateau(bad_runs)
        self.assertFalse(res_pl["fired"])
        self.assertIn(res_pl["reason"], ("insufficient_runs", "no_numeric_metric"))


class TestDecisionD1ConcludeGate(unittest.TestCase):
    """Harness 4: Decision D1 Conclude Gate.

    Verifies that concluding a campaign with an active Level 2 Hold or Level 3
    Escalation automatically downgrades the conclusion verdict to 'confounded'
    in confirmatory/sequential campaigns, unless bypassed via --force-verdict.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cat_dir = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_conclude_downgrade_under_level2_hold(self):
        """Campaign in Level 2 hold concluded without force_verdict must be downgraded to 'confounded'."""
        from bathos.stepback.escalation import check_conclude_posture

        state = EscalationState(
            campaign_id="camp_confirmatory",
            current_level=2,
            status="hold",
            active_escalation_id="esc_hold_01",
        )
        camp = MockCampaign("camp_confirmatory", mode="confirmatory")

        # 1. Normal conclude: downgrade triggered
        should_downgrade, verdict, status = check_conclude_posture(camp, state, force_verdict=False)
        self.assertTrue(should_downgrade)
        self.assertEqual(verdict, "confounded")
        self.assertEqual(status, "held")

        # 2. Conclude with force_verdict: downgrade suppressed, logged as bypassed
        should_downgrade, verdict, status = check_conclude_posture(camp, state, force_verdict=True)
        self.assertFalse(should_downgrade)
        self.assertIsNone(verdict)
        self.assertEqual(status, "bypassed")

    def test_conclude_downgrade_under_level3_escalation(self):
        """Campaign in Level 3 escalation concluded without force_verdict must downgrade."""
        from bathos.stepback.escalation import check_conclude_posture

        state = EscalationState(
            campaign_id="camp_sequential",
            current_level=3,
            status="escalated",
            active_escalation_id="esc_l3_01",
        )
        camp = MockCampaign("camp_sequential", mode="sequential")

        should_downgrade, verdict, status = check_conclude_posture(camp, state, force_verdict=False)
        self.assertTrue(should_downgrade)
        self.assertEqual(verdict, "confounded")

    def test_conclude_exploration_mode_no_downgrade(self):
        """Campaign in exploration mode emits warning but does not downgrade verdict."""
        from bathos.stepback.escalation import check_conclude_posture

        state = EscalationState(
            campaign_id="camp_exploratory",
            current_level=2,
            status="hold",
        )
        camp = MockCampaign("camp_exploratory", mode="exploration")

        should_downgrade, verdict, status = check_conclude_posture(camp, state, force_verdict=False)
        self.assertFalse(should_downgrade)
        self.assertEqual(status, "warning_unaddressed")

    def test_conclude_nominal_level0_level1_no_downgrade(self):
        """Campaigns at Level 0 or Level 1 conclude nominally without downgrade."""
        from bathos.stepback.escalation import check_conclude_posture

        camp = MockCampaign("camp_nominal", mode="confirmatory")
        for lvl in (0, 1):
            state = EscalationState(campaign_id="camp_nominal", current_level=lvl)
            should_downgrade, verdict, status = check_conclude_posture(camp, state)
            self.assertFalse(should_downgrade)
            self.assertEqual(status, "nominal")


class TestDuckDBCompaction(unittest.TestCase):
    """Harness 5: DuckDB Compaction.

    Verifies that cool-tier review and escalation JSON fragments compact
    cleanly into DuckDB tables without schema corruption or duplicate keys,
    and that queries across cool and warm tiers reconstruct exact records.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cat_dir = Path(self.tmp_dir.name)
        self.db_path = self.cat_dir / "bathos.db"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_cool_fragments_compaction_and_idempotency(self):
        """Reviews and escalations in cool tier compact into DuckDB tables idempotently."""
        camp_id = "camp_compact_test"

        # 1. Create a Path A rubric review
        rev_a = StepBackReview(
            review_id="rev_test_path_a",
            campaign_id=camp_id,
            project_slug="test_proj",
            created_at="2026-10-27T14:00:00Z",
            reviewer_id="reviewer_01",
            reviewer_kind="agent",
            is_independent=True,
            path_kind="rubric",
            input_snapshot_sha256="sha_snap_a",
            input_snapshot='{"test": 1}',
            recommendation="CONTINUE",
            escalation_level=0,
            rubric_id="stepback_v1",
            rubric_version="1.0.0",
            rubric_sha256="sha_rubric",
            rubric_scores={"question_validity": 4, "empirical_progress": 4, "theoretical_grounding": 4, "resource_efficiency": 4, "risk_confound_surface": 4},
            composite_score=4.0,
        )
        save_review_cool(rev_a, self.cat_dir)

        # 2. Create a Path B open analysis review with dissents
        dissent = DissentRecord(
            dissenting_reviewer_id="dissenter_01",
            minority_recommendation="HOLD",
            minority_composite_score=2.2,
            dissent_summary="Thermostat drift observed",
            cited_counter_evidence=["run:run_drift_01"],
            majority_rebuttal="Acceptable in equilibration",
        )
        rev_b = StepBackReview(
            review_id="rev_test_path_b",
            campaign_id=camp_id,
            project_slug="test_proj",
            created_at="2026-10-27T15:00:00Z",
            reviewer_id="reviewer_02",
            reviewer_kind="panel",
            is_independent=True,
            path_kind="open_analysis",
            input_snapshot_sha256="sha_snap_b",
            input_snapshot='{"test": 2}',
            recommendation="HOLD",
            escalation_level=2,
            cited_evidence=["run:run_01", "sidecar:test.bth.toml"],
            open_analysis=OpenAnalysis(
                mode="pre_mortem",
                sections={
                    "failure_narrative": "Simulation collapsed after step 1000",
                    "overlooked_red_flags": "Replicate pooling confounded",
                    "present_day_symptoms": "Marginal streak",
                    "preventative_mitigations": "Pause and recalibrate",
                },
            ),
            dissents=[dissent],
        )
        save_review_cool(rev_b, self.cat_dir)

        # 3. Create Escalation records
        esc1 = EscalationRecord(
            escalation_id="esc_01",
            campaign_id=camp_id,
            project_slug="test_proj",
            level=2,
            status="active",
            opened_at="2026-10-27T14:30:00Z",
            opened_by_kind="review",
            opened_by_review_id="rev_test_path_b",
            trigger_reason="Panel dissent forced Level 2 Hold",
        )
        save_escalation_cool(esc1, self.cat_dir)

        esc2 = EscalationRecord(
            escalation_id="esc_02",
            campaign_id=camp_id,
            project_slug="test_proj",
            level=3,
            status="active",
            opened_at="2026-10-27T16:00:00Z",
            opened_by_kind="chair",
            trigger_reason="Fatal confound detected by committee",
        )
        save_escalation_cool(esc2, self.cat_dir)

        # Connect to DuckDB and run ingest_stepback_fragments
        con = duckdb.connect(str(self.db_path))
        rev_count, esc_count = ingest_stepback_fragments(con, self.cat_dir)

        self.assertEqual(rev_count, 2)
        self.assertEqual(esc_count, 2)

        # Verify DuckDB rows and schemas
        r_rows = con.execute("SELECT id, campaign_id, recommendation, composite_score, open_analysis_mode FROM stepback_reviews ORDER BY created_at").fetchall()
        self.assertEqual(len(r_rows), 2)
        self.assertEqual(r_rows[0][0], "rev_test_path_a")
        self.assertEqual(r_rows[0][2], "CONTINUE")
        self.assertEqual(r_rows[0][3], 4.0)

        self.assertEqual(r_rows[1][0], "rev_test_path_b")
        self.assertEqual(r_rows[1][2], "HOLD")
        self.assertEqual(r_rows[1][4], "pre_mortem")

        e_rows = con.execute("SELECT id, level, status, trigger_reason FROM stepback_escalations ORDER BY opened_at").fetchall()
        self.assertEqual(len(e_rows), 2)
        self.assertEqual(e_rows[0][1], 2)
        self.assertEqual(e_rows[1][1], 3)

        # 4. Test IDEMPOTENCY: Run ingest a second time
        rev_count_2, esc_count_2 = ingest_stepback_fragments(con, self.cat_dir)
        self.assertEqual(rev_count_2, 0, "Second compaction should skip already ingested reviews")
        self.assertEqual(esc_count_2, 0, "Second compaction should skip already ingested escalations")

        total_r = con.execute("SELECT count(*) FROM stepback_reviews").fetchone()[0]
        total_e = con.execute("SELECT count(*) FROM stepback_escalations").fetchone()[0]
        self.assertEqual(total_r, 2)
        self.assertEqual(total_e, 2)

        # 5. Test reconstruction via list_reviews using warm DB
        warm_reviews = list_reviews(campaign_id=camp_id, catalog_dir=self.cat_dir, db=con)
        self.assertEqual(len(warm_reviews), 2)
        self.assertEqual(warm_reviews[1].open_analysis.mode, "pre_mortem")
        self.assertEqual(len(warm_reviews[1].dissents), 1)
        self.assertEqual(warm_reviews[1].dissents[0].minority_recommendation, "HOLD")

        con.close()


class TestReviewerIndependence(unittest.TestCase):
    """Harness 6: Reviewer Independence & Attribution Protection."""

    def test_self_review_by_author_raises_conflict(self):
        """Author of member runs cannot conduct independent review when raise_on_conflict=True."""
        camp = MockCampaign("camp_self", creator="other_user")
        runs = [
            {"run_id": "r_1", "author": "alice", "agent_mode": "agent_alpha"},
            {"run_id": "r_2", "author": "bob", "agent_mode": "agent_beta"},
        ]

        # Alice attempts self-review
        with self.assertRaises(StepBackIndependenceViolationError):
            check_reviewer_independence("alice", camp, runs, raise_on_conflict=True)

        # Agent alpha attempts self-review
        with self.assertRaises(StepBackIndependenceViolationError):
            check_reviewer_independence("agent_alpha", camp, runs, raise_on_conflict=True)

        # Independent outsider charlie passes
        self.assertTrue(check_reviewer_independence("charlie", camp, runs, raise_on_conflict=True))

    def test_whitespace_reviewer_id_rejected(self):
        """Whitespace-only reviewer identity is rejected in check_reviewer_independence and record_stepback_review."""
        camp = MockCampaign("camp_ws", creator="other_user")
        runs = [{"run_id": "r_1", "author": "alice"}]

        self.assertFalse(check_reviewer_independence("   ", camp, runs))
        self.assertFalse(check_reviewer_independence("", camp, runs))
        with self.assertRaises(StepBackInvalidSchemaError):
            check_reviewer_independence("   ", camp, runs, raise_on_conflict=True)

        with (
            tempfile.TemporaryDirectory() as tmp_d,
            patch("bathos.stepback.engine.get_campaign", return_value=camp),
            patch("bathos.stepback.engine.fetch_member_runs", return_value=runs),
            self.assertRaises(StepBackInvalidSchemaError),
        ):
            record_stepback_review(
                campaign_id="camp_ws",
                path_kind="rubric",
                scores={"question_validity": 4, "empirical_progress": 4, "theoretical_grounding": 4, "resource_efficiency": 4, "risk_confound_surface": 4},
                reviewer_id="   ",
                catalog_dir=Path(tmp_d),
            )

    def test_git_author_name_and_email_forbidden(self):
        """Git author Name and email are both unpacked into forbidden authors."""
        camp = MockCampaign("camp_git", creator="other_user")
        runs = [{"run_id": "r_1", "git_author": "Diana Prince <diana@themyscira.io>"}]

        # Full string forbidden
        with self.assertRaises(StepBackIndependenceViolationError):
            check_reviewer_independence("Diana Prince <diana@themyscira.io>", camp, runs, raise_on_conflict=True)

        # Name only forbidden
        with self.assertRaises(StepBackIndependenceViolationError):
            check_reviewer_independence("Diana Prince", camp, runs, raise_on_conflict=True)

        # Email only forbidden
        with self.assertRaises(StepBackIndependenceViolationError):
            check_reviewer_independence("diana@themyscira.io", camp, runs, raise_on_conflict=True)

        # Independent party permitted
        self.assertTrue(check_reviewer_independence("Bruce Wayne", camp, runs, raise_on_conflict=True))


class TestFloorVetoAndFlawCapping(unittest.TestCase):
    """Harness 7: Floor Veto & Flaw Capping in Rubrics."""

    def test_floor_veto_forces_hold_and_level_2(self):
        """A score of 1 in risk_confound_surface forces HOLD and Level 2 regardless of other 5s."""
        scores = {
            "question_validity": 5,
            "empirical_progress": 5,
            "theoretical_grounding": 5,
            "resource_efficiency": 5,
            "risk_confound_surface": 1,  # VETO!
        }
        composite, rec, lvl, sha = evaluate_rubric(scores)
        self.assertEqual(rec, "HOLD")
        self.assertEqual(lvl, 2)

    def test_question_validity_veto_forces_hold(self):
        """A score of 1 in question_validity forces HOLD and Level 2."""
        scores = {
            "question_validity": 1,  # VETO!
            "empirical_progress": 5,
            "theoretical_grounding": 5,
            "resource_efficiency": 5,
            "risk_confound_surface": 5,
        }
        composite, rec, lvl, sha = evaluate_rubric(scores)
        self.assertEqual(rec, "HOLD")
        self.assertEqual(lvl, 2)

    def test_flaw_capping_prohibits_continue(self):
        """A score of 2 in any dimension prohibits CONTINUE (capped at PIVOT)."""
        scores = {
            "question_validity": 5,
            "empirical_progress": 2,  # Flaw cap!
            "theoretical_grounding": 5,
            "resource_efficiency": 5,
            "risk_confound_surface": 5,
        }
        composite, rec, lvl, sha = evaluate_rubric(scores)
        self.assertNotEqual(rec, "CONTINUE")
        self.assertEqual(rec, "PIVOT")
        self.assertEqual(lvl, 1)

    def test_out_of_range_score_rejected(self):
        """Scores outside [1, 5] raise StepBackInvalidSchemaError or RubricInvalidError."""
        bad_scores = {
            "question_validity": 6,  # Invalid
            "empirical_progress": 5,
            "theoretical_grounding": 5,
            "resource_efficiency": 5,
            "risk_confound_surface": 5,
        }
        with self.assertRaises(Exception):
            evaluate_rubric(bad_scores)


class TestPathBMandatoryRecommendation(unittest.TestCase):
    """Harness 8: Mandatory Recommendation in Path B."""

    def test_missing_recommendation_in_open_analysis_rejected(self):
        """Path B payload without recommendation or with invalid recommendation raises StepBackInvalidSchemaError."""
        analysis = {
            "mode": "advisor_convo",
            "sections": {
                "premise_challenge": "Valid challenge",
                "simplest_decisive_test": "Valid test",
                "strategic_advice": "Valid advice",
            },
        }
        # Passing invalid recommendation to validate_open_analysis
        with self.assertRaises(StepBackInvalidSchemaError):
            validate_open_analysis(analysis, recommendation="INVALID_REC")

        # Passing no recommendation to record_stepback_review in Path B
        with (
            patch("bathos.stepback.engine.get_campaign", return_value=MockCampaign("camp_any")),
            self.assertRaises(StepBackInvalidSchemaError),
        ):
            record_stepback_review(
                campaign_id="camp_any",
                path_kind="open_analysis",
                mode="advisor_convo",
                narrative=analysis["sections"],
                evidence=["run:run_01", "run:run_02"],
                recommendation=None,  # Missing!
            )


class TestPanelDissentAutomaticEscalation(unittest.TestCase):
    """Harness 9: Automatic Escalation on Panel Dissent."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cat_dir = Path(self.tmp_dir.name)
        self.camp_id = "camp_dissent_auto_esc"
        self.camp = MockCampaign(self.camp_id, creator="other_user")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_dissent_with_hold_escalates_campaign_to_level_2(self):
        """When panel consensus is CONTINUE but minority dissents with HOLD, campaign must enter Level 2 Hold."""
        dissent = {
            "dissenting_reviewer_id": "minority_skeptic",
            "minority_recommendation": "HOLD",
            "minority_composite_score": 2.1,
            "dissent_summary": "Unchecked drift in runs indicates instability.",
            "cited_counter_evidence": ["run:r1"],
            "majority_rebuttal": "Majority deemed variance within tolerance.",
        }

        # Initial state is Level 0
        state = get_escalation_state(self.camp_id, catalog_dir=self.cat_dir)
        self.assertEqual(state.current_level, 0)

        with (
            patch("bathos.stepback.engine.get_campaign", return_value=self.camp),
            patch("bathos.stepback.engine.fetch_member_runs", return_value=[]),
        ):
            res = record_stepback_review(
                    campaign_id=self.camp_id,
                    path_kind="rubric",
                    scores={
                        "question_validity": 4,
                        "empirical_progress": 4,
                        "theoretical_grounding": 4,
                        "resource_efficiency": 4,
                        "risk_confound_surface": 4,
                    },
                    reviewer_id="panel_chair",
                    reviewer_kind="panel",
                    dissents=[dissent],
                    catalog_dir=self.cat_dir,
                )

        self.assertTrue(res["ok"])
        self.assertEqual(res["escalation_level"], 2)
        state_after = get_escalation_state(self.camp_id, catalog_dir=self.cat_dir)
        self.assertEqual(state_after.current_level, 2)
        # Check review was recorded
        rev = list_reviews(campaign_id=self.camp_id, catalog_dir=self.cat_dir)[0]
        self.assertEqual(len(rev.dissents), 1)
        self.assertEqual(rev.dissents[0].minority_recommendation, "HOLD")


class TestAntiRubberStampMetaSignals(unittest.TestCase):
    """Harness 10: Anti-Rubber-Stamp Meta-Signals (Signals 15-18)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cat_dir = Path(self.tmp_dir.name)
        self.db_path = self.cat_dir / "bathos.db"
        self.project_slug = "proj_surveillance"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_signal_15_detects_unvarying_verdicts(self):
        """Signal 15 fires WARNING on unvarying verdict streaks or zero variance in composite scores."""
        from bathos.sprint_audit import signal_stepback_unvarying_verdicts

        for i in range(4):
            rev = StepBackReview(
                review_id=f"rev_{i}",
                campaign_id="camp_unvarying",
                project_slug=self.project_slug,
                created_at=f"2026-10-27T10:0{i}:00Z",
                reviewer_id="agent_rubber_stamper",
                reviewer_kind="agent",
                is_independent=True,
                path_kind="rubric",
                input_snapshot_sha256="sha",
                input_snapshot="{}",
                recommendation="CONTINUE",
                escalation_level=0,
                composite_score=4.8,  # Exactly identical scores!
            )
            save_review_cool(rev, self.cat_dir)

        sig = signal_stepback_unvarying_verdicts(self.project_slug, self.db_path, catalog_dir=self.cat_dir)
        self.assertEqual(sig.level, "WARNING")
        self.assertIn("stepback_unvarying_verdicts detected", sig.message)

    def test_signal_16_detects_low_evidence_rate(self):
        """Signal 16 fires WARNING when > 15% of reviews lack primary cited evidence."""
        from bathos.sprint_audit import signal_stepback_low_evidence_rate

        for i in range(4):
            rev = StepBackReview(
                review_id=f"rev_low_{i}",
                campaign_id="camp_low_ev",
                project_slug=self.project_slug,
                created_at=f"2026-10-27T11:0{i}:00Z",
                reviewer_id="agent_lazy",
                reviewer_kind="agent",
                is_independent=True,
                path_kind="rubric",
                input_snapshot_sha256="sha",
                input_snapshot="{}",
                recommendation="CONTINUE",
                escalation_level=0,
                cited_evidence=[],  # ZERO evidence!
            )
            save_review_cool(rev, self.cat_dir)

        sig = signal_stepback_low_evidence_rate(self.project_slug, self.db_path, catalog_dir=self.cat_dir)
        self.assertEqual(sig.level, "WARNING")
        self.assertIn("stepback_low_evidence_rate", sig.message)

    def test_signal_17_detects_ignored_escalations(self):
        """Signal 17 fires WARNING when an escalation is open for > 7 days."""
        from datetime import UTC, datetime, timedelta

        from bathos.sprint_audit import signal_stepback_ignored_escalations

        old_date = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        esc = EscalationRecord(
            escalation_id="esc_ignored_01",
            campaign_id="camp_ignored",
            project_slug=self.project_slug,
            level=2,
            status="active",
            opened_at=old_date,
            opened_by_kind="system",
            trigger_reason="Entropy collapse",
        )
        save_escalation_cool(esc, self.cat_dir)

        sig = signal_stepback_ignored_escalations(self.project_slug, self.db_path, catalog_dir=self.cat_dir)
        self.assertEqual(sig.level, "WARNING")
        self.assertIn("stepback_ignored_escalations detected", sig.message)


class TestMutationResilience(unittest.TestCase):
    """Harness 11: Mutation-Resilience & Security Invariance.

    Proves that:
    1. Monkeypatching/bypassing reviewer independence (setting is_independent=False,
       faking author identity, casing/whitespace spoofing, env var spoofing)
       causes review recording/validation to FAIL.
    2. Attempting to resolve, close, or de-escalate Level 3 Human Chair escalation
       without chair authorization fails with StepBackAgentOverrideRefusedError.
    3. Monkeypatching StepBackAgentOverrideRefusedError or bypassing the chair check
       fails mutation checks (mutants are detected and killed).
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cat_dir = Path(self.tmp_dir.name)
        self.camp_id = "camp_mutation_resilience"
        self.camp = MockCampaign(self.camp_id, creator="original_creator")
        self.runs = [
            {
                "run_id": "run_01_mut",
                "author": "run_author_alice",
                "agent_mode": "agent_worker_bob",
                "git_author": "committer_charlie",
            },
            {
                "run_id": "run_02_mut",
                "author": "run_author_david",
                "agent_mode": "agent_worker_eve",
            },
        ]

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_mutation_reviewer_independence_bypass_fails(self):
        """Mutation Test 1: Proves that faking author identity or setting is_independent=False causes review recording/validation to FAIL."""
        # 1. Direct author collision
        with self.assertRaises(StepBackIndependenceViolationError):
            check_reviewer_independence("run_author_alice", self.camp, self.runs, raise_on_conflict=True)

        # 2. Agent author collision
        with self.assertRaises(StepBackIndependenceViolationError):
            check_reviewer_independence("agent_worker_bob", self.camp, self.runs, raise_on_conflict=True)

        # 3. Campaign creator collision
        with self.assertRaises(StepBackIndependenceViolationError):
            check_reviewer_independence("original_creator", self.camp, self.runs, raise_on_conflict=True)

        # 4. Git author collision
        with self.assertRaises(StepBackIndependenceViolationError):
            check_reviewer_independence("committer_charlie", self.camp, self.runs, raise_on_conflict=True)

        # 5. Casing and whitespace faking attempt (e.g. " Run_Author_Alice ")
        with self.assertRaises(StepBackIndependenceViolationError):
            check_reviewer_independence("  RUN_AUTHOR_ALICE  ", self.camp, self.runs, raise_on_conflict=True)

        # 6. Attempting to record review with faked author identity via record_stepback_review
        with patch("bathos.stepback.engine.get_campaign", return_value=self.camp), \
             patch("bathos.stepback.engine.union_campaign_member_ids", return_value=["run_01_mut", "run_02_mut"]), \
             patch("bathos.stepback.engine.fetch_member_runs", return_value=self.runs):
            # 6a. Faked run author
            with self.assertRaises(StepBackIndependenceViolationError):
                record_stepback_review(
                    campaign_id=self.camp_id,
                    path_kind="rubric",
                    scores={
                        "question_validity": 4,
                        "empirical_progress": 4,
                        "theoretical_grounding": 4,
                        "resource_efficiency": 4,
                        "risk_confound_surface": 4,
                    },
                    reviewer_id="run_author_alice",  # Conflicted author!
                    catalog_dir=self.cat_dir,
                )

            # 6b. Campaign creator self-review
            with self.assertRaises(StepBackIndependenceViolationError):
                record_stepback_review(
                    campaign_id=self.camp_id,
                    path_kind="rubric",
                    scores={
                        "question_validity": 4,
                        "empirical_progress": 4,
                        "theoretical_grounding": 4,
                        "resource_efficiency": 4,
                        "risk_confound_surface": 4,
                    },
                    reviewer_id="original_creator",  # Conflicted creator!
                    catalog_dir=self.cat_dir,
                )

        # 7. Environment variable spoofing attempt: reviewer_id omitted, but AGENT_NAME set to author
        with (
            patch("bathos.stepback.engine.get_campaign", return_value=self.camp),
            patch("bathos.stepback.engine.union_campaign_member_ids", return_value=["run_01_mut", "run_02_mut"]),
            patch("bathos.stepback.engine.fetch_member_runs", return_value=self.runs),
            patch.dict(os.environ, {"AGENT_NAME": "agent_worker_bob"}),
            self.assertRaises(StepBackIndependenceViolationError),
        ):
            record_stepback_review(
                campaign_id=self.camp_id,
                path_kind="rubric",
                scores={
                    "question_validity": 4,
                    "empirical_progress": 4,
                    "theoretical_grounding": 4,
                    "resource_efficiency": 4,
                    "risk_confound_surface": 4,
                },
                reviewer_id=None,  # Fall back to env var
                catalog_dir=self.cat_dir,
            )

        # 8. Verify that is_independent evaluates to False when conflict exists
        is_indep = check_reviewer_independence("run_author_alice", self.camp, self.runs, raise_on_conflict=False)
        self.assertFalse(is_indep)

    def test_mutation_resolve_level3_without_chair_authorization_fails(self):
        """Mutation Test 2: Prove that attempting to resolve, close, or de-escalate Level 3 without chair authorization fails with StepBackAgentOverrideRefusedError."""
        # Setup active Level 3 escalation in catalog
        esc_l3 = EscalationRecord(
            escalation_id="esc_l3_resilience",
            campaign_id=self.camp_id,
            project_slug="test_proj",
            level=3,
            status="active",
            opened_at="2026-10-27T12:00:00Z",
            opened_by_kind="system",
            trigger_reason="Fatal confound",
        )
        save_escalation_cool(esc_l3, self.cat_dir)

        # 1. FastMCP/Strict agent refusal: Must fail with StepBackAgentOverrideRefusedError
        with self.assertRaises(StepBackAgentOverrideRefusedError):
            resolve_stepback(
                escalation_id="esc_l3_resilience",
                campaign_id=self.camp_id,
                deescalate_to=0,
                catalog_dir=self.cat_dir,
                strict_agent_refusal=True,
            )

        # 2. De-escalating to Level 1 or 2 as agent must also fail with StepBackAgentOverrideRefusedError
        for target_lvl in (1, 2):
            with self.assertRaises(StepBackAgentOverrideRefusedError):
                resolve_stepback(
                    escalation_id="esc_l3_resilience",
                    campaign_id=self.camp_id,
                    deescalate_to=target_lvl,
                    catalog_dir=self.cat_dir,
                    strict_agent_refusal=True,
                )

        # 3. Agent environment present (CLAUDE_AGENT, BTH_AGENT_MODE, etc.):
        for var in AGENT_ENV_VARS:
            with (
                patch.dict(os.environ, {var: "1"}),
                self.assertRaises(StepBackAgentOverrideRefusedError),
            ):
                resolve_stepback(
                    escalation_id="esc_l3_resilience",
                    campaign_id=self.camp_id,
                    deescalate_to=0,
                    as_human_chair=True,  # Even spoofing chair flag!
                    interactive_tty=False,
                    catalog_dir=self.cat_dir,
                    strict_agent_refusal=False,
                )

        # 4. Direct transition_escalation from level 3 with agent environment must raise
        state_l3 = EscalationState(
            campaign_id=self.camp_id,
            current_level=3,
            status="escalated",
            active_escalation_id="esc_l3_resilience",
        )
        with (
            patch.dict(os.environ, {"CLAUDE_AGENT": "true"}),
            self.assertRaises(StepBackAgentOverrideRefusedError),
        ):
            transition_escalation(state_l3, target_level=0, reason="Agent bypass")

        # 5. Non-agent caller without chair authorization: raises StepBackUnauthorizedChairError
        clean_env = {v: "" for v in AGENT_ENV_VARS}
        with (
            patch.dict(os.environ, clean_env, clear=False),
            self.assertRaises(StepBackUnauthorizedChairError),
        ):
            resolve_stepback(
                escalation_id="esc_l3_resilience",
                campaign_id=self.camp_id,
                deescalate_to=0,
                as_human_chair=False,
                interactive_tty=False,
                chair_token=None,
                catalog_dir=self.cat_dir,
                strict_agent_refusal=False,
            )

        # Verify state remains active Level 3
        persisted_state = get_escalation_state(self.camp_id, catalog_dir=self.cat_dir)
        self.assertEqual(persisted_state.current_level, 3)

    def test_mutation_bypassing_chair_check_fails_mutation_checks(self):
        """Mutation Test 3: Prove that monkeypatching StepBackAgentOverrideRefusedError or bypassing the chair check fails mutation checks."""
        esc_l3 = EscalationRecord(
            escalation_id="esc_l3_mut3",
            campaign_id=self.camp_id,
            project_slug="test_proj",
            level=3,
            status="active",
            opened_at="2026-10-27T12:00:00Z",
            opened_by_kind="system",
            trigger_reason="Confound",
        )
        save_escalation_cool(esc_l3, self.cat_dir)

        def mutation_runner(fn, *args, **kwargs) -> tuple[bool, type[BaseException] | None]:
            """Execute fn and return (killed, exception_type)."""
            try:
                fn(*args, **kwargs)
                return False, None  # Mutant survived (BAD)
            except BaseException as exc:
                return True, type(exc)  # Mutant killed (GOOD)

        # Mutant 1: Monkeypatch is_agent_environment to False (simulating agent disguise)
        # Defense-in-depth: Chair check must still kill this mutant via StepBackUnauthorizedChairError
        with patch("bathos.stepback.escalation.is_agent_environment", return_value=False), \
             patch("bathos.stepback.engine.is_agent_environment", return_value=False):
            killed, exc_type = mutation_runner(
                resolve_stepback,
                escalation_id="esc_l3_mut3",
                campaign_id=self.camp_id,
                deescalate_to=0,
                as_human_chair=False,
                interactive_tty=False,
                chair_token=None,
                catalog_dir=self.cat_dir,
            )
            self.assertTrue(killed, "Mutant disguised as non-agent must be killed")
            self.assertIs(exc_type, StepBackUnauthorizedChairError)

        # Mutant 2: Monkeypatch verify_chair_token to return True, but running inside an agent environment
        # Invariant: Autonomous agent status overrides token validity
        with patch("bathos.stepback.escalation.verify_chair_token", return_value=True), \
             patch("bathos.stepback.engine.verify_chair_token", return_value=True), \
             patch.dict(os.environ, {"CLAUDE_AGENT": "1"}):
            killed, exc_type = mutation_runner(
                resolve_stepback,
                escalation_id="esc_l3_mut3",
                campaign_id=self.camp_id,
                deescalate_to=0,
                as_human_chair=True,
                interactive_tty=False,
                chair_token="forged_token",
                catalog_dir=self.cat_dir,
            )
            self.assertTrue(killed, "Mutant with forged token inside agent env must be killed")
            self.assertIs(exc_type, StepBackAgentOverrideRefusedError)

        # Mutant 3: Verify that StepBackAgentOverrideRefusedError is mapped to structured BathosErrorCode
        self.assertEqual(
            EXCEPTION_TO_CODE["StepBackAgentOverrideRefusedError"],
            BathosErrorCode.STEPBACK_AGENT_OVERRIDE_REFUSED,
        )
        self.assertEqual(
            BathosErrorCode.STEPBACK_AGENT_OVERRIDE_REFUSED.value,
            "stepback_agent_override_refused",
        )

        # Mutant 4: If an attacker were to monkeypatch resolve_stepback to return ok=True (a rogue bypass),
        # verify that our mutation test harness correctly flags the survived mutant as a failure
        with patch("bathos.stepback.engine.resolve_stepback", return_value={"ok": True}):
            from bathos.stepback.engine import resolve_stepback as mutated_resolve
            killed, _ = mutation_runner(mutated_resolve, escalation_id="esc_l3_mut3", campaign_id=self.camp_id)
            self.assertFalse(killed, "Rogue bypass mutant must be detected as survived")


if __name__ == "__main__":
    unittest.main()
