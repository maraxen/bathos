"""Tests for Escalation Ladder and Human Chair Invariance (spec 261027 Section 5)."""

from pathlib import Path

import pytest

from bathos.errors import (
    EscalationLevelInvalidError,
    StepBackAgentOverrideRefusedError,
    StepBackUnauthorizedChairError,
)
from bathos.stepback.escalation import (
    can_transition,
    check_conclude_posture,
    get_execution_advisory,
    transition_escalation,
    validate_level,
)
from bathos.stepback.schema import EscalationState


def test_validate_level_bounds():
    assert validate_level(0) == 0
    assert validate_level(1) == 1
    assert validate_level(2) == 2
    assert validate_level(3) == 3

    with pytest.raises(EscalationLevelInvalidError):
        validate_level(-1)
    with pytest.raises(EscalationLevelInvalidError):
        validate_level(4)
    with pytest.raises(EscalationLevelInvalidError):
        validate_level("2")  # type: ignore[arg-type]


def test_transition_matrix_direct_paths():
    # 0 -> 1, 0 -> 2, 0 -> 3
    assert can_transition(0, 1)[0] is True
    assert can_transition(0, 2)[0] is True
    assert can_transition(0, 3)[0] is True

    # 1 -> 0, 1 -> 2, 1 -> 3
    assert can_transition(1, 0)[0] is True
    assert can_transition(1, 2)[0] is True
    assert can_transition(1, 3)[0] is True

    # 2 -> 1, 2 -> 3
    assert can_transition(2, 1)[0] is True
    assert can_transition(2, 3)[0] is True


def test_level_3_human_chair_invariance_agent_refusal(monkeypatch: pytest.MonkeyPatch):
    state = EscalationState(campaign_id="camp_01", current_level=3, status="escalated")

    # Set autonomous agent environment
    monkeypatch.setenv("CLAUDE_AGENT", "1")

    # Agent cannot de-escalate Level 3 even if claiming to be chair
    with pytest.raises(StepBackAgentOverrideRefusedError, match="Autonomous agents are strictly forbidden"):
        transition_escalation(
            state,
            target_level=0,
            reason="Agent resolved deadlock",
            as_human_chair=True,
            interactive_tty=True,
        )

    with pytest.raises(StepBackAgentOverrideRefusedError):
        transition_escalation(
            state,
            target_level=1,
            reason="Agent de-escalating",
            as_human_chair=False,
        )


def test_level_3_human_chair_authorization_gates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # Ensure agent env vars are unset
    for var in ("BTH_AGENT_MODE", "CLAUDE_AGENT", "CLAUDE_AGENT_ID", "AGENT_NAME", "SUBAGENT_ID", "ANTIGRAVITY_AGENT"):
        monkeypatch.delenv(var, raising=False)

    state = EscalationState(campaign_id="camp_01", current_level=3, status="escalated")

    # Non-TTY and no token -> refused
    with pytest.raises(StepBackUnauthorizedChairError, match="requires interactive TTY CLI or valid chair token"):
        transition_escalation(
            state,
            target_level=0,
            reason="Automated script resolution",
            as_human_chair=False,
            interactive_tty=False,
        )

    # With interactive TTY gate and as_human_chair -> succeeds
    new_state, record = transition_escalation(
        state,
        target_level=0,
        reason="Human PI signed off in terminal",
        as_human_chair=True,
        interactive_tty=True,
    )
    assert new_state.current_level == 0
    assert new_state.status == "nominal"
    assert record.deescalated_to == 0
    assert record.resolved_by == "human_chair"

    # With valid chair token -> succeeds
    token_file = tmp_path / "chair_token"
    token_file.write_text("secret_chair_token_123\n")
    monkeypatch.setattr("bathos.stepback.escalation.CHAIR_TOKEN_PATH", token_file)

    state3 = EscalationState(campaign_id="camp_01", current_level=3, status="escalated")
    new_state2, record2 = transition_escalation(
        state3,
        target_level=1,
        reason="Chair token authenticated",
        as_human_chair=True,
        chair_token="secret_chair_token_123",
        interactive_tty=False,
    )
    assert new_state2.current_level == 1
    assert record2.deescalated_to == 1


def test_decision_d1_execution_advisory(monkeypatch: pytest.MonkeyPatch):
    state0 = EscalationState(campaign_id="camp_01", current_level=0)
    assert get_execution_advisory("camp_01", state0) is None

    state2 = EscalationState(campaign_id="camp_01", current_level=2, active_triggers=["repeated_marginal"])
    adv = get_execution_advisory("camp_01", state2)
    assert adv is not None
    assert adv["level"] == 2
    assert "WARNING: Campaign 'camp_01' is at Step-Back COMMITTEE HOLD" in adv["banner"]
    assert adv["blocked"] is False  # advisory by default

    # Opt-in blocking via BTH_STEP_BACK_ENFORCE=1
    monkeypatch.setenv("BTH_STEP_BACK_ENFORCE", "1")
    adv_blocked = get_execution_advisory("camp_01", state2)
    assert adv_blocked is not None
    assert adv_blocked["blocked"] is True


def test_decision_d1_conclusion_downgrade():
    state0 = EscalationState(campaign_id="camp_01", current_level=0)
    should_down, verdict, status = check_conclude_posture({"mode": "confirmatory"}, state0)
    assert should_down is False
    assert status == "nominal"

    state2 = EscalationState(campaign_id="camp_01", current_level=2)

    # Confirmatory campaign with active Level 2 hold -> automatically downgraded to 'confounded'
    should_down2, verdict2, status2 = check_conclude_posture({"mode": "confirmatory"}, state2)
    assert should_down2 is True
    assert verdict2 == "confounded"
    assert status2 == "held"

    # Confirmatory campaign with force_verdict -> bypasses downgrade, records 'bypassed'
    should_down_f, verdict_f, status_f = check_conclude_posture(
        {"mode": "confirmatory"}, state2, force_verdict=True
    )
    assert should_down_f is False
    assert status_f == "bypassed"

    # Exploration campaign -> emits warning without automatic conclusion downgrade
    should_down_e, verdict_e, status_e = check_conclude_posture({"mode": "exploration"}, state2)
    assert should_down_e is False
    assert status_e == "warning_unaddressed"


def test_lifecycle_warn_only_hook_on_add_run(tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch):
    import duckdb
    from bathos.campaigns import add_run_to_campaign, create_campaign, CampaignError
    from bathos.stepback.catalog import save_escalation_cool
    from bathos.stepback.schema import EscalationRecord

    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir(parents=True)
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
            run_id TEXT,
            evalue REAL,
            seq_position INTEGER,
            PRIMARY KEY (campaign_id, run_id)
        );
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            timestamp TEXT,
            outcome TEXT,
            sidecar_path TEXT
        );
        CREATE TABLE IF NOT EXISTS stepback_escalations (
            id TEXT PRIMARY KEY,
            campaign_id TEXT,
            project_slug TEXT,
            level INTEGER,
            status TEXT,
            opened_at TEXT,
            opened_by_kind TEXT,
            trigger_name TEXT,
            trigger_reason TEXT,
            resolution_note TEXT,
            review_epoch INTEGER,
            metadata TEXT
        );
        """
    )
    camp = create_campaign(
        con,
        name="Lifecycle Warn Test",
        project_slug="lifecycle_proj",
        mode="exploration",
        catalog_dir=cat_dir,
    )
    con.execute("INSERT INTO runs (id, timestamp, outcome, sidecar_path) VALUES ('run_001', '2026-10-27T12:00:00Z', 'pass', NULL)")
    con.execute("INSERT INTO runs (id, timestamp, outcome, sidecar_path) VALUES ('run_002', '2026-10-27T12:01:00Z', 'pass', NULL)")

    # 1. No escalation -> add_run succeeds silently without warning
    add_run_to_campaign(con, camp.id, "run_001", catalog_dir=cat_dir)
    captured = capsys.readouterr()
    assert "WARNING: Active step-back" not in captured.err

    # 2. Add Level 2 active escalation
    esc = EscalationRecord(
        escalation_id="esc_l2_warn",
        campaign_id=camp.id,
        project_slug="lifecycle_proj",
        level=2,
        status="active",
        opened_at="2026-10-27T12:05:00Z",
        opened_by_kind="system",
        trigger_reason="Repeated marginal outcomes",
    )
    save_escalation_cool(esc, cat_dir)

    # Adding run_002 emits non-blocking warning to stderr, but does NOT raise by default
    add_run_to_campaign(con, camp.id, "run_002", catalog_dir=cat_dir)
    captured = capsys.readouterr()
    assert "WARNING: Active step-back Step-Back Hold (Level 2)" in captured.err

    # Verify run_002 was actually added to campaign
    added = con.execute("SELECT run_id FROM campaign_runs WHERE campaign_id = ?", [camp.id]).fetchall()
    assert ('run_002',) in added

    # 3. Opt-in enforcement blocks adding run when level >= 2
    monkeypatch.setenv("BTH_STEP_BACK_ENFORCE", "1")
    con.execute("INSERT INTO runs (id, timestamp, outcome, sidecar_path) VALUES ('run_003', '2026-10-27T12:02:00Z', 'pass', NULL)")

    with pytest.raises(CampaignError, match="step-back enforcement enabled and campaign has active Step-Back Hold"):
        add_run_to_campaign(con, camp.id, "run_003", catalog_dir=cat_dir)

