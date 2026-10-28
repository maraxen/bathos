"""Tests for Step-Back Reviews FastMCP Tool Suite (spec 261027 Section 7).

Verifies:
1. All 6 FastMCP tools (+ aliases): status, run/trigger, record/review, escalate, show, resolve.
2. Structured error envelopes: ok, error_code, error, resolution_hint.
3. Path A rubric review scoring and persistence.
4. Path B open analysis validation and primary evidence citation verification.
5. Independence check: reviewer conflict of interest refused.
6. INVIOLABLE HUMAN CHAIR INVARIANCE: FastMCP strictly and unconditionally refuses
   to resolve Level 3 escalations (error_code='stepback_agent_override_refused').
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from bathos.campaigns import create_campaign
from bathos.mcp import (
    stepback_escalate,
    stepback_record,
    stepback_resolve,
    stepback_review,
    stepback_run,
    stepback_show,
    stepback_status,
    stepback_trigger,
)
from bathos.stepback.catalog import (
    get_escalation_state,
    get_review,
    save_escalation_cool,
)
from bathos.stepback.schema import EscalationRecord


def _stamp_runs(con, cat_dir: Path, run_ids: list[str]) -> None:
    """Give fixture runs the recorded time and on-disk sidecar that real runs carry."""
    for i, rid in enumerate(run_ids):
        sidecar = cat_dir / f"{rid}.bth.toml"
        sidecar.write_text('[experiment]\nhypothesis = "fixture"\n')
        con.execute(
            "UPDATE runs SET started_at = ?, sidecar_path = ? WHERE id = ?",
            [f"2026-10-27T{10 + i:02d}:00:00Z", str(sidecar), rid],
        )


@pytest.fixture
def mcp_test_env(tmp_path: Path, monkeypatch):
    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir(parents=True)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(cat_dir))

    db_path = cat_dir / "bathos.db"
    con = duckdb.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE campaigns (
            id TEXT PRIMARY KEY,
            project_slug TEXT,
            name TEXT,
            mode TEXT,
            question TEXT,
            hypothesis TEXT,
            status TEXT,
            started_at TEXT,
            concluded_at TEXT,
            conclusion TEXT,
            outcome_label TEXT,
            parent_campaign_id TEXT,
            stopping_threshold REAL,
            negative_check TEXT,
            claim_path TEXT,
            claim_sha256 TEXT,
            claim_mode TEXT
        );
        CREATE TABLE campaign_runs (
            campaign_id TEXT,
            run_id TEXT
        );
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            started_at TEXT,
            status TEXT,
            author TEXT,
            agent_mode TEXT,
            outcome TEXT,
            tentative_outcome TEXT,
            sidecar_path TEXT,
            sidecar_sha256 TEXT,
            metadata TEXT
        );
        CREATE TABLE IF NOT EXISTS stepback_escalations (
            escalation_id TEXT PRIMARY KEY,
            campaign_id TEXT,
            project_slug TEXT,
            level INTEGER,
            status TEXT,
            opened_at TEXT,
            opened_by_kind TEXT,
            trigger_reason TEXT,
            review_epoch INTEGER,
            pivot_anchor_run_id TEXT,
            resolved_at TEXT,
            resolved_by TEXT,
            resolved_kind TEXT,
            resolution_note TEXT,
            deescalated_to INTEGER
        );
        CREATE TABLE IF NOT EXISTS stepback_reviews (
            review_id TEXT PRIMARY KEY,
            campaign_id TEXT,
            project_slug TEXT,
            created_at TEXT,
            reviewer_id TEXT,
            reviewer_kind TEXT,
            is_independent BOOLEAN,
            path_kind TEXT,
            input_snapshot_sha256 TEXT,
            input_snapshot TEXT,
            recommendation TEXT,
            escalation_level INTEGER,
            cited_evidence TEXT,
            rubric_id TEXT,
            rubric_version TEXT,
            rubric_sha256 TEXT,
            rubric_scores TEXT,
            composite_score REAL,
            open_analysis TEXT,
            dissents TEXT,
            dispatched_at TEXT
        );
        """
    )

    # Create test campaign and member runs
    camp = create_campaign(
        con,
        "camp_mcp_test",
        mode="confirmation",
        question="Is MCP integration working?",
        hypothesis="MCP tools work",
        project_slug="testproj",
        catalog_dir=cat_dir,
    )

    # Insert a couple of runs
    con.execute(
        """
        INSERT INTO runs (id, started_at, status, author, agent_mode, outcome, tentative_outcome, sidecar_path, sidecar_sha256, metadata)
        VALUES ('run_mcp_1', '2026-10-27T10:00:00Z', 'completed', 'author_a', 'agent_worker', 'pass', NULL, '', '', '{"author": "author_a", "agent_mode": "agent_worker"}'),
               ('run_mcp_2', '2026-10-27T11:00:00Z', 'completed', 'author_a', 'agent_worker', 'pass', NULL, '', '', '{"author": "author_a", "agent_mode": "agent_worker"}');
        INSERT INTO campaign_runs (campaign_id, run_id)
        VALUES (?, 'run_mcp_1'), (?, 'run_mcp_2');
        """,
        [camp.id, camp.id],
    )
    _stamp_runs(con, cat_dir, ["run_mcp_1", "run_mcp_2"])
    con.commit()
    con.close()

    return {"catalog_dir": cat_dir, "campaign_id": camp.id}


@pytest.mark.anyio
async def test_mcp_stepback_status_and_triggers(mcp_test_env):
    camp_id = mcp_test_env["campaign_id"]

    # 1. Status query
    status_res = await stepback_status(campaign_id=camp_id, verbose=True)
    assert status_res["ok"] is True
    assert status_res["campaign_id"] == camp_id
    assert status_res["escalation_level"] == 0
    assert status_res["hold_active"] is False
    assert status_res["review_count"] == 0

    # 2. Trigger check
    trig_res = await stepback_trigger(campaign_id=camp_id, dry_run=True)
    assert trig_res["ok"] is True
    assert "triggers_evaluated" in trig_res
    assert isinstance(trig_res["active_triggers"], list)

    # Alias stepback_run
    run_res = await stepback_run(campaign_id=camp_id)
    assert run_res["ok"] is True


@pytest.mark.anyio
async def test_mcp_stepback_record_path_a_rubric(mcp_test_env):
    camp_id = mcp_test_env["campaign_id"]
    cat_dir = mcp_test_env["catalog_dir"]

    scores = {
        "question_validity": 5,
        "empirical_progress": 5,
        "theoretical_grounding": 5,
        "resource_efficiency": 5,
        "risk_confound_surface": 5,
    }

    # Record review with independent reviewer
    res = await stepback_record(
        campaign_id=camp_id,
        path_kind="rubric",
        scores=scores,
        reviewer_id="independent_reviewer_1",
    )
    assert res["ok"] is True
    assert res["review_id"].startswith("rev_")
    assert res["composite_score"] == 5.0
    assert res["recommendation"] == "CONTINUE"
    assert res["escalation_level"] == 0

    # Verify review persisted to cool storage
    rev = get_review(res["review_id"], catalog_dir=cat_dir)
    assert rev is not None
    assert rev.is_independent is True
    assert rev.composite_score == 5.0


@pytest.mark.anyio
async def test_mcp_stepback_record_path_b_open_analysis(mcp_test_env):
    camp_id = mcp_test_env["campaign_id"]
    cat_dir = mcp_test_env["catalog_dir"]

    # Valid Path B pre-mortem review
    narrative = {
        "failure_narrative": "Thermal fluctuations cause thermostat drift.",
        "overlooked_red_flags": "Initial standard deviation was near threshold.",
        "present_day_symptoms": "Marginal outcomes on step 50.",
        "preventative_mitigations": "Switch to Langevin dynamics with gamma=1.0.",
    }
    evidence = ["run:run_mcp_1", "run:run_mcp_2"]

    res = await stepback_review(
        campaign_id=camp_id,
        path_kind="open_analysis",
        mode="pre_mortem",
        narrative=narrative,
        evidence=evidence,
        recommendation="PIVOT",
        reviewer_id="independent_advisor",
    )
    assert res["ok"] is True
    assert res["recommendation"] == "PIVOT"
    assert res["escalation_level"] == 1

    # Check that escalation state updated to Warning (1)
    state = get_escalation_state(camp_id, catalog_dir=cat_dir)
    assert state.current_level == 1


@pytest.mark.anyio
async def test_mcp_stepback_conflict_of_interest_refusal(mcp_test_env):
    camp_id = mcp_test_env["campaign_id"]

    # Reviewer who authored member runs ('author_a' or 'agent_worker')
    res = await stepback_record(
        campaign_id=camp_id,
        path_kind="rubric",
        scores={"question_validity": 5, "empirical_progress": 5, "theoretical_grounding": 5, "resource_efficiency": 5, "risk_confound_surface": 5},
        reviewer_id="author_a",
    )
    assert res["ok"] is False
    assert res["error_code"] in ("reviewer_conflict_of_interest", "stepback_independence_violation")
    assert "conflict of interest" in res["error"].lower()


@pytest.mark.anyio
async def test_mcp_stepback_insufficient_evidence_refusal(mcp_test_env):
    camp_id = mcp_test_env["campaign_id"]

    narrative = {
        "failure_narrative": "Failure.",
        "overlooked_red_flags": "Flags.",
        "present_day_symptoms": "Symptoms.",
        "preventative_mitigations": "Mitigations.",
    }
    # Only 1 citation when 2 are required
    res = await stepback_record(
        campaign_id=camp_id,
        path_kind="open_analysis",
        mode="pre_mortem",
        narrative=narrative,
        evidence=["run:run_mcp_1"],
        recommendation="HOLD",
        reviewer_id="independent_advisor",
    )
    assert res["ok"] is False
    assert res["error_code"] == "stepback_insufficient_evidence"


@pytest.mark.anyio
async def test_mcp_stepback_escalate_and_show(mcp_test_env):
    camp_id = mcp_test_env["campaign_id"]
    cat_dir = mcp_test_env["catalog_dir"]

    # Escalate to Level 2
    esc_res = await stepback_escalate(
        campaign_id=camp_id,
        target_level=2,
        reason="Repeated marginal outcomes and unaddressed confound",
    )
    assert esc_res["ok"] is True
    assert esc_res["current_level"] == 2
    assert esc_res["previous_level"] == 0

    state = get_escalation_state(camp_id, catalog_dir=cat_dir)
    assert state.current_level == 2

    # Show campaign reviews
    show_res = await stepback_show(campaign_id=camp_id)
    assert show_res["ok"] is True
    assert "reviews" in show_res


@pytest.mark.anyio
async def test_mcp_human_chair_invariance_unconditional_refusal(mcp_test_env):
    """INVIOLABLE HUMAN CHAIR INVARIANCE: FastMCP strictly forbids agent resolution of Level 3."""
    camp_id = mcp_test_env["campaign_id"]
    cat_dir = mcp_test_env["catalog_dir"]

    # Put campaign into Level 3 Human Chair Escalation
    esc = EscalationRecord(
        escalation_id="esc_level3_chair",
        campaign_id=camp_id,
        project_slug="testproj",
        level=3,
        status="active",
        opened_at="2026-10-27T12:00:00Z",
        opened_by_kind="system",
        trigger_reason="Severe confound requiring Human Chair intervention",
    )
    save_escalation_cool(esc, cat_dir)

    # Attempt to resolve via FastMCP
    res = await stepback_resolve(
        campaign_id=camp_id,
        escalation_id="esc_level3_chair",
        deescalate_to=0,
        note="Agent trying to resolve Level 3",
    )

    # MUST be refused with stepback_agent_override_refused
    assert res["ok"] is False
    assert res["error_code"] == "stepback_agent_override_refused"
    assert "forbidden" in res["error"].lower() or "chair" in res["error"].lower()

    # Verify state is STILL Level 3
    state = get_escalation_state(camp_id, catalog_dir=cat_dir)
    assert state.current_level == 3
