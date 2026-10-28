"""Regression tests for the PR #149 step-back review findings.

Each test pins one remediated defect so that reintroducing it fails here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bathos.errors import (
    StepBackCitationInvalidError,
    StepBackInsufficientEvidenceError,
    StepBackInvalidSchemaError,
)
from bathos.stepback.analysis import validate_campaign_evidence
from bathos.stepback.catalog import save_escalation_cool
from bathos.stepback.engine import (
    _check_canonical_rubric,
    compute_input_snapshot,
    member_runs_chronological,
)
from bathos.stepback.rubrics import find_default_rubric_path, load_rubric
from bathos.stepback.schema import EscalationRecord
from bathos.stepback.triggers import _resolve_numeric_metric

RUN_ID = "0123456789abcdef0123456789abcdef"


def _campaign(claim_path: str | None = None):
    return SimpleNamespace(id="camp_rem", claim_path=claim_path)


# --- Finding 3: citations ------------------------------------------------------------------


def test_short_run_prefix_is_not_evidence():
    members = [SimpleNamespace(id=RUN_ID, sidecar_path="", git_commit=None)]
    with pytest.raises(StepBackCitationInvalidError):
        validate_campaign_evidence(["run:0"], _campaign(), members, min_citations=1)


def test_repeated_citation_counts_once():
    members = [SimpleNamespace(id=RUN_ID, sidecar_path="", git_commit=None)]
    with pytest.raises(StepBackInsufficientEvidenceError):
        validate_campaign_evidence(
            [f"run:{RUN_ID}", f"run:{RUN_ID}"], _campaign(), members, min_citations=2
        )


def test_git_citation_needs_a_member_commit():
    members = [SimpleNamespace(id=RUN_ID, sidecar_path="", git_commit=None)]
    with pytest.raises(StepBackCitationInvalidError):
        validate_campaign_evidence(["git:deadbeef"], _campaign(), members, min_citations=1)


def test_clause_needs_a_registered_claim():
    with pytest.raises(StepBackCitationInvalidError):
        validate_campaign_evidence(
            ["clause:anything.toml#c1"], _campaign(claim_path=None), [], min_citations=1
        )


def test_postmortem_must_exist_and_name_the_obligation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(tmp_path))
    with pytest.raises(StepBackCitationInvalidError):
        validate_campaign_evidence(
            [f"postmortem:{tmp_path / 'missing.bth.postmortem.toml'}#ob-1"], _campaign(), [], min_citations=1
        )
    pm = tmp_path / "x.bth.postmortem.toml"
    pm.write_text('campaign_id = "camp_rem"\n\n[obligations."ob-1"]\nstatus = "discharged"\n')
    valid, _ = validate_campaign_evidence(
        [f"postmortem:{pm}#ob-1"], _campaign(), [], min_citations=1
    )
    assert valid == [f"postmortem:{pm}#ob-1"]


def test_postmortem_for_another_campaign_is_not_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(tmp_path))
    pm = tmp_path / "other.bth.postmortem.toml"
    pm.write_text('campaign_id = "camp_other"\n\n[obligations."ob-1"]\nstatus = "discharged"\n')
    with pytest.raises(StepBackCitationInvalidError):
        validate_campaign_evidence(
            [f"postmortem:{pm}#ob-1"], _campaign(), [], min_citations=1
        )


def test_postmortem_outside_the_workspace_is_not_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(workspace))
    outside = tmp_path / "elsewhere.bth.postmortem.toml"
    outside.write_text('campaign_id = "camp_rem"\n\n[obligations."ob-1"]\nstatus = "discharged"\n')
    with pytest.raises(StepBackCitationInvalidError):
        validate_campaign_evidence(
            [f"postmortem:{outside}#ob-1"], _campaign(), [], min_citations=1
        )


def test_signal17_attributes_a_cool_only_campaign(tmp_path: Path):
    """An uncompacted campaign lives only in cool JSON; its open hold must still be charged."""
    from bathos.campaigns import Campaign, write_campaign_cool
    from bathos.sprint_audit import signal_stepback_ignored_escalations

    write_campaign_cool(
        Campaign(id="camp_cool_only", project_slug="proj", name="cool", mode="confirmation"), tmp_path
    )
    save_escalation_cool(
        EscalationRecord(
            escalation_id="esc_cool_only",
            campaign_id="camp_cool_only",
            project_slug="",
            level=2,
            status="active",
            opened_at=(datetime.now(UTC) - timedelta(days=10)).isoformat(),
            opened_by_kind="system",
            trigger_reason="hold",
        ),
        tmp_path,
    )

    result = signal_stepback_ignored_escalations("proj", tmp_path / "bathos.db", catalog_dir=tmp_path)

    assert result.value == 1.0
    assert result.level == "WARNING"


def test_respellings_of_one_run_count_once():
    members = [SimpleNamespace(id=RUN_ID, sidecar_path="", git_commit=None)]
    with pytest.raises(StepBackInsufficientEvidenceError):
        validate_campaign_evidence(
            [f"run:{RUN_ID}", f"run:{RUN_ID[:8]}", f"outcome:{RUN_ID}:pass"],
            _campaign(),
            members,
            min_citations=2,
        )


# --- Finding 4: rubric is an identifier, not a path ----------------------------------------


def test_rubric_id_must_be_the_canonical_rubric(tmp_path: Path):
    _check_canonical_rubric("stepback_v1")  # canonical id accepted
    _check_canonical_rubric(None)
    with pytest.raises(StepBackInvalidSchemaError):
        _check_canonical_rubric(str(tmp_path / "weights.toml"))


def test_default_rubric_not_shadowed_by_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    shadow = tmp_path / "agent_assets" / "rubrics"
    shadow.mkdir(parents=True)
    (shadow / "stepback_v1.toml").write_text('id = "shadow"\n')
    monkeypatch.chdir(tmp_path)

    path = find_default_rubric_path()

    assert tmp_path not in path.parents
    assert load_rubric()[0]["id"] == "stepback_v1"


# --- Finding 8: snapshot binds sidecar content and is ordered ------------------------------


def test_snapshot_changes_when_a_member_sidecar_changes():
    camp = SimpleNamespace(id="camp_rem")
    before = [SimpleNamespace(id=RUN_ID, sidecar_sha256="aaa", git_commit=None)]
    after = [SimpleNamespace(id=RUN_ID, sidecar_sha256="bbb", git_commit=None)]
    assert compute_input_snapshot(camp, before)[1] != compute_input_snapshot(camp, after)[1]


# --- Finding 5: chronological run order ----------------------------------------------------


def test_member_runs_ordered_by_recorded_time(tmp_path: Path):
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    runs = {
        "b": SimpleNamespace(id="b"),
        "a": SimpleNamespace(id="a"),
    }
    stamps = {"a": t0 + timedelta(hours=2), "b": t0 + timedelta(hours=1)}
    with (
        patch("bathos.stepback.engine.fetch_member_runs", return_value=runs),
        patch("bathos.stepback.engine.is_log_mode", return_value=False),
        patch("bathos.stepback.engine._run_timestamps", return_value=stamps),
    ):
        ordered = member_runs_chronological(None, ["a", "b"], tmp_path)
    assert [r.id for r in ordered] == ["b", "a"]


# --- Finding 11: trigger metric never picks a count or seed column -------------------------


def test_trigger_metric_skips_int_columns():
    sidecar = {"result_schema": {"n_atoms": "int", "ns_per_day": "float"}}
    run = SimpleNamespace(results={"n_atoms": 1000, "ns_per_day": 52.5})
    assert _resolve_numeric_metric(run, None, sidecar) == 52.5


def test_trigger_metric_refuses_to_guess_between_floats():
    sidecar = {"result_schema": {"a": "float", "b": "float"}}
    run = SimpleNamespace(results={"a": 1.0, "b": 2.0})
    assert _resolve_numeric_metric(run, None, sidecar) is None


# --- Finding 13: superseded holds are not open holds ---------------------------------------


def test_resolved_hold_is_not_counted_as_ignored(tmp_path: Path):
    from bathos.sprint_audit import signal_stepback_ignored_escalations

    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    save_escalation_cool(
        EscalationRecord(
            escalation_id="esc_old_hold",
            campaign_id="camp_sup",
            project_slug="proj",
            level=2,
            status="active",
            opened_at=old,
            opened_by_kind="system",
            trigger_reason="hold",
        ),
        tmp_path,
    )
    save_escalation_cool(
        EscalationRecord(
            escalation_id="esc_resolved",
            campaign_id="camp_sup",
            project_slug="proj",
            level=0,
            status="resolved",
            opened_at=datetime.now(UTC).isoformat(),
            opened_by_kind="reviewer",
            trigger_reason="resolved",
        ),
        tmp_path,
    )

    result = signal_stepback_ignored_escalations("proj", tmp_path / "bathos.db", catalog_dir=tmp_path)

    assert result.value == 0.0
    assert result.level == "OK"


# --- Engine-level regressions: review recording -------------------------------------------

# Composite 5.0 maps to CONTINUE (Tier 3 needs >= 4.2); 4s would map to PIVOT.
CONTINUE_SCORES = {
    "question_validity": 5,
    "empirical_progress": 5,
    "theoretical_grounding": 5,
    "resource_efficiency": 5,
    "risk_confound_surface": 5,
}


def _review_campaign(camp_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=camp_id, project_slug="proj", mode="confirmatory", claim_path=None, creator="other_user"
    )


def test_minority_pivot_dissent_does_not_force_a_hold(tmp_path: Path):
    from bathos.stepback.engine import record_stepback_review

    camp = _review_campaign("camp_pivot_dissent")
    dissent = {"dissenting_reviewer_id": "skeptic", "minority_recommendation": "PIVOT"}
    with (
        patch("bathos.stepback.engine.get_campaign", return_value=camp),
        patch("bathos.stepback.engine.fetch_member_runs", return_value=[]),
    ):
        res = record_stepback_review(
            campaign_id=camp.id,
            path_kind="rubric",
            scores=CONTINUE_SCORES,
            reviewer_id="panel_chair",
            dissents=[dissent],
            catalog_dir=tmp_path,
        )
    assert res["escalation_level"] == 0


def test_level3_review_under_agent_is_advice_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from bathos.stepback.catalog import get_escalation_state, list_reviews
    from bathos.stepback.engine import record_stepback_review

    camp = _review_campaign("camp_refused_l3")
    save_escalation_cool(
        EscalationRecord(
            escalation_id="esc_refused_l3",
            campaign_id=camp.id,
            project_slug="proj",
            level=3,
            status="active",
            opened_at="2026-10-27T12:00:00Z",
            opened_by_kind="system",
            trigger_reason="severe confound",
        ),
        tmp_path,
    )
    monkeypatch.setenv("CLAUDE_AGENT", "1")
    with (
        patch("bathos.stepback.engine.get_campaign", return_value=camp),
        patch("bathos.stepback.engine.fetch_member_runs", return_value=[]),
    ):
        record_stepback_review(
            campaign_id=camp.id,
            path_kind="rubric",
            scores=CONTINUE_SCORES,
            reviewer_id="panel_chair",
            catalog_dir=tmp_path,
        )
    # Recorded as advice; an agent cannot lower Level 3 (spec 9.2).
    assert len(list_reviews(campaign_id=camp.id, catalog_dir=tmp_path)) == 1
    assert get_escalation_state(camp.id, catalog_dir=tmp_path).current_level == 3


def test_agent_session_cannot_file_review_as_human(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from bathos.stepback.catalog import list_reviews
    from bathos.stepback.engine import record_stepback_review

    camp = _review_campaign("camp_agent_kind")
    monkeypatch.setenv("CLAUDE_AGENT", "1")
    with (
        patch("bathos.stepback.engine.get_campaign", return_value=camp),
        patch("bathos.stepback.engine.fetch_member_runs", return_value=[]),
    ):
        record_stepback_review(
            campaign_id=camp.id,
            path_kind="rubric",
            scores=CONTINUE_SCORES,
            reviewer_id="panel_chair",
            reviewer_kind="human",
            catalog_dir=tmp_path,
        )
    assert list_reviews(campaign_id=camp.id, catalog_dir=tmp_path)[0].reviewer_kind == "agent"


def _save_escalation(tmp_path: Path, camp_id: str, level: int) -> None:
    save_escalation_cool(
        EscalationRecord(
            escalation_id=f"esc_{camp_id}_{level}",
            campaign_id=camp_id,
            project_slug="proj",
            level=level,
            status="active",
            opened_at="2026-10-27T12:00:00Z",
            opened_by_kind="system",
            trigger_reason="test",
        ),
        tmp_path,
    )


def _workspace_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str) -> None:
    (tmp_path / ".bth.toml").write_text(f'[stepback]\nagent_hold_discharge = "{policy}"\n')
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(tmp_path))


def test_agent_review_lifts_a_hold_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from bathos.stepback.catalog import get_escalation_state
    from bathos.stepback.engine import record_stepback_review

    camp = _review_campaign("camp_hold_default")
    _save_escalation(tmp_path, camp.id, 2)
    monkeypatch.setenv("CLAUDE_AGENT", "1")
    # A workspace config without the key keeps the default policy ("agentic").
    (tmp_path / ".bth.toml").write_text("[stepback]\n")
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(tmp_path))
    with (
        patch("bathos.stepback.engine.get_campaign", return_value=camp),
        patch("bathos.stepback.engine.fetch_member_runs", return_value=[]),
    ):
        record_stepback_review(
            campaign_id=camp.id,
            path_kind="rubric",
            scores=CONTINUE_SCORES,
            reviewer_id="independent_agent",
            catalog_dir=tmp_path,
        )
    assert get_escalation_state(camp.id, catalog_dir=tmp_path).current_level == 0


def test_unknown_hold_policy_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from bathos.stepback.engine import _hold_discharge_policy

    _workspace_policy(tmp_path, monkeypatch, "anarchic")
    with pytest.raises(StepBackInvalidSchemaError):
        _hold_discharge_policy()


def test_missing_sidecar_is_named_not_guessed():
    from bathos.stepback.engine import member_sidecar

    with pytest.raises(StepBackInvalidSchemaError, match="no recoverable sidecar"):
        member_sidecar([SimpleNamespace(id=RUN_ID, sidecar_path="")])


def test_missing_timestamp_is_refused(tmp_path: Path):
    with (
        patch("bathos.stepback.engine.fetch_member_runs", return_value={"r1": SimpleNamespace(id="r1")}),
        patch("bathos.stepback.engine.is_log_mode", return_value=False),
        patch("bathos.stepback.engine._run_timestamps", return_value={}),
        pytest.raises(StepBackInvalidSchemaError, match="no recorded timestamp"),
    ):
        member_runs_chronological(None, ["r1"], tmp_path)


def test_agent_filed_review_cannot_lift_a_level2_hold_when_human_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from bathos.errors import StepBackUnauthorizedChairError
    from bathos.stepback.catalog import list_reviews
    from bathos.stepback.engine import record_stepback_review

    camp = _review_campaign("camp_hold_agent")
    _save_escalation(tmp_path, camp.id, 2)
    _workspace_policy(tmp_path, monkeypatch, "human_only")
    monkeypatch.setenv("CLAUDE_AGENT", "1")
    with (
        patch("bathos.stepback.engine.get_campaign", return_value=camp),
        patch("bathos.stepback.engine.fetch_member_runs", return_value=[]),
        pytest.raises(StepBackUnauthorizedChairError),
    ):
        record_stepback_review(
            campaign_id=camp.id,
            path_kind="rubric",
            scores=CONTINUE_SCORES,
            reviewer_id="any_name_the_agent_claims",
            catalog_dir=tmp_path,
        )
    assert list_reviews(campaign_id=camp.id, catalog_dir=tmp_path) == []


def test_level3_review_is_recorded_as_advice_without_lowering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from bathos.stepback.catalog import get_escalation_state, list_reviews
    from bathos.stepback.engine import record_stepback_review

    camp = _review_campaign("camp_l3_advice")
    _save_escalation(tmp_path, camp.id, 3)
    for var in ("BTH_AGENT_MODE", "CLAUDE_AGENT", "CLAUDE_AGENT_ID", "AGENT_NAME", "SUBAGENT_ID", "ANTIGRAVITY_AGENT"):
        monkeypatch.delenv(var, raising=False)
    with (
        patch("bathos.stepback.engine.get_campaign", return_value=camp),
        patch("bathos.stepback.engine.fetch_member_runs", return_value=[]),
    ):
        record_stepback_review(
            campaign_id=camp.id,
            path_kind="rubric",
            scores=CONTINUE_SCORES,
            reviewer_id="panel_chair",
            catalog_dir=tmp_path,
        )
    assert len(list_reviews(campaign_id=camp.id, catalog_dir=tmp_path)) == 1
    assert get_escalation_state(camp.id, catalog_dir=tmp_path).current_level == 3


def test_force_agent_overrides_a_claimed_human_reviewer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from bathos.stepback.catalog import list_reviews
    from bathos.stepback.engine import record_stepback_review

    camp = _review_campaign("camp_force_agent")
    for var in ("BTH_AGENT_MODE", "CLAUDE_AGENT", "CLAUDE_AGENT_ID", "AGENT_NAME", "SUBAGENT_ID", "ANTIGRAVITY_AGENT"):
        monkeypatch.delenv(var, raising=False)
    with (
        patch("bathos.stepback.engine.get_campaign", return_value=camp),
        patch("bathos.stepback.engine.fetch_member_runs", return_value=[]),
    ):
        record_stepback_review(
            campaign_id=camp.id,
            path_kind="rubric",
            scores=CONTINUE_SCORES,
            reviewer_id="panel_chair",
            reviewer_kind="human",
            catalog_dir=tmp_path,
            force_agent=True,
        )
    assert list_reviews(campaign_id=camp.id, catalog_dir=tmp_path)[0].reviewer_kind == "agent"


def test_hybrid_rejects_an_unknown_recommendation(tmp_path: Path):
    from bathos.stepback.engine import record_stepback_review

    camp = _review_campaign("camp_hybrid_bad")
    with (
        patch("bathos.stepback.engine.get_campaign", return_value=camp),
        patch("bathos.stepback.engine.fetch_member_runs", return_value=[]),
        pytest.raises(StepBackInvalidSchemaError),
    ):
        record_stepback_review(
            campaign_id=camp.id,
            path_kind="hybrid",
            scores=CONTINUE_SCORES,
            recommendation="FOO",
            reviewer_id="panel_chair",
            catalog_dir=tmp_path,
        )
