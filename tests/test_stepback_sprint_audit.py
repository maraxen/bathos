"""Tests for Anti-Rubber-Stamp Meta-Signals (Signals 15-18) in sprint_audit.py (spec 261027)."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bathos.sprint_audit import (
    signal_stepback_ignored_escalations,
    signal_stepback_low_evidence_rate,
    signal_stepback_turnaround_anomalies,
    signal_stepback_unvarying_verdicts,
)
from bathos.stepback.catalog import save_escalation_cool, save_review_cool
from bathos.stepback.schema import (
    EscalationRecord,
    StepBackReview,
)


@pytest.fixture
def mock_catalog(tmp_path: Path) -> Path:
    cat = tmp_path / "catalog"
    cat.mkdir(parents=True, exist_ok=True)
    return cat


def test_signal_15_unvarying_verdicts(mock_catalog: Path):
    db_path = mock_catalog / "bathos.db"

    # 1. Less than 4 reviews -> INFO
    res = signal_stepback_unvarying_verdicts("testproj", db_path, catalog_dir=mock_catalog)
    assert res.level == "INFO"

    # 2. 4 reviews, all CONTINUE, identical scores -> WARNING
    for i in range(4):
        rev = StepBackReview(
            review_id=f"rev_{i}",
            campaign_id="camp1",
            project_slug="testproj",
            created_at=f"2026-10-27T10:0{i}:00Z",
            reviewer_id="agent_1",
            reviewer_kind="agent",
            is_independent=True,
            path_kind="rubric",
            input_snapshot_sha256="sha",
            input_snapshot="{}",
            recommendation="CONTINUE",
            escalation_level=0,
            composite_score=4.5,
        )
        save_review_cool(rev, mock_catalog)

    res_unvarying = signal_stepback_unvarying_verdicts("testproj", db_path, catalog_dir=mock_catalog)
    assert res_unvarying.level == "WARNING"
    assert "stepback_unvarying_verdicts detected" in res_unvarying.message

    # 3. Add varied reviews with varied recommendations and varied scores -> OK
    rev_pivot = StepBackReview(
        review_id="rev_4",
        campaign_id="camp1",
        project_slug="testproj",
        created_at="2026-10-27T10:05:00Z",
        reviewer_id="agent_2",
        reviewer_kind="agent",
        is_independent=True,
        path_kind="rubric",
        input_snapshot_sha256="sha",
        input_snapshot="{}",
        recommendation="PIVOT",
        escalation_level=1,
        composite_score=2.8,
    )
    rev_hold = StepBackReview(
        review_id="rev_5",
        campaign_id="camp1",
        project_slug="testproj",
        created_at="2026-10-27T10:06:00Z",
        reviewer_id="agent_3",
        reviewer_kind="agent",
        is_independent=True,
        path_kind="rubric",
        input_snapshot_sha256="sha",
        input_snapshot="{}",
        recommendation="HOLD",
        escalation_level=2,
        composite_score=1.5,
    )
    save_review_cool(rev_pivot, mock_catalog)
    save_review_cool(rev_hold, mock_catalog)

    res_varied = signal_stepback_unvarying_verdicts("testproj", db_path, catalog_dir=mock_catalog)
    assert res_varied.level == "OK"


def test_signal_16_low_evidence_rate(mock_catalog: Path):
    db_path = mock_catalog / "bathos.db"

    # Insufficient reviews (< 3)
    res_info = signal_stepback_low_evidence_rate("testproj", db_path, catalog_dir=mock_catalog)
    assert res_info.level == "INFO"

    # Add 4 reviews where 2 lack >= 2 valid citations (50% > 15%)
    r1 = StepBackReview(
        review_id="rev_ev_1",
        campaign_id="camp1",
        project_slug="testproj",
        created_at="2026-10-27T10:00:00Z",
        reviewer_id="r1",
        reviewer_kind="human",
        is_independent=True,
        path_kind="rubric",
        input_snapshot_sha256="sha",
        input_snapshot="{}",
        recommendation="CONTINUE",
        escalation_level=0,
        cited_evidence=["run:run_1", "run:run_2"],  # valid: 2
    )
    r2 = StepBackReview(
        review_id="rev_ev_2",
        campaign_id="camp1",
        project_slug="testproj",
        created_at="2026-10-27T10:01:00Z",
        reviewer_id="r2",
        reviewer_kind="human",
        is_independent=True,
        path_kind="rubric",
        input_snapshot_sha256="sha",
        input_snapshot="{}",
        recommendation="CONTINUE",
        escalation_level=0,
        cited_evidence=["run:run_3", "run:run_4"],  # valid: 2
    )
    r3 = StepBackReview(
        review_id="rev_ev_3",
        campaign_id="camp1",
        project_slug="testproj",
        created_at="2026-10-27T10:02:00Z",
        reviewer_id="r3",
        reviewer_kind="human",
        is_independent=True,
        path_kind="rubric",
        input_snapshot_sha256="sha",
        input_snapshot="{}",
        recommendation="CONTINUE",
        escalation_level=0,
        cited_evidence=["invalid_plain_string"],  # valid: 0 (< 2)
    )
    r4 = StepBackReview(
        review_id="rev_ev_4",
        campaign_id="camp1",
        project_slug="testproj",
        created_at="2026-10-27T10:03:00Z",
        reviewer_id="r4",
        reviewer_kind="human",
        is_independent=True,
        path_kind="rubric",
        input_snapshot_sha256="sha",
        input_snapshot="{}",
        recommendation="CONTINUE",
        escalation_level=0,
        cited_evidence=["run:run_5"],  # valid: 1 (< 2)
    )
    save_review_cool(r1, mock_catalog)
    save_review_cool(r2, mock_catalog)
    save_review_cool(r3, mock_catalog)
    save_review_cool(r4, mock_catalog)

    res_warn = signal_stepback_low_evidence_rate("testproj", db_path, catalog_dir=mock_catalog)
    assert res_warn.level == "WARNING"
    assert res_warn.value == 0.5
    assert "stepback_low_evidence_rate 50.0% > 15%" in res_warn.message


def test_signal_17_ignored_escalations(mock_catalog: Path):
    db_path = mock_catalog / "bathos.db"

    # No active escalations -> OK
    res_ok = signal_stepback_ignored_escalations("testproj", db_path, catalog_dir=mock_catalog)
    assert res_ok.level == "OK"

    # Add active Level 2 escalation opened 8 days ago (> 7d) -> WARNING
    old_opened = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    esc = EscalationRecord(
        escalation_id="esc_old",
        campaign_id="camp_old",
        project_slug="testproj",
        level=2,
        status="active",
        opened_at=old_opened,
        opened_by_kind="trigger",
        trigger_name="repeated_marginal",
        trigger_reason="streak=3",
    )
    save_escalation_cool(esc, mock_catalog)

    res_warn = signal_stepback_ignored_escalations("testproj", db_path, catalog_dir=mock_catalog)
    assert res_warn.level == "WARNING"
    assert res_warn.value == 1.0
    assert "stepback_ignored_escalations detected" in res_warn.message


def test_signal_18_turnaround_anomalies(mock_catalog: Path):
    db_path = mock_catalog / "bathos.db"

    # 1. Telemetry disabled -> INFO
    os.environ["BTH_NO_TELEMETRY"] = "1"
    try:
        res_dis = signal_stepback_turnaround_anomalies("testproj", db_path, catalog_dir=mock_catalog)
        assert res_dis.level == "INFO"
    finally:
        del os.environ["BTH_NO_TELEMETRY"]

    # 2. Agent review completed in 0.5s (< 2.0s) -> WARNING
    t_disp = "2026-10-27T12:00:00Z"
    t_creat = "2026-10-27T12:00:00.500Z"
    fast_rev = StepBackReview(
        review_id="rev_fast",
        campaign_id="camp1",
        project_slug="testproj",
        created_at=t_creat,
        dispatched_at=t_disp,
        reviewer_id="agent_fast",
        reviewer_kind="agent",
        is_independent=True,
        path_kind="rubric",
        input_snapshot_sha256="sha",
        input_snapshot="{}",
        recommendation="CONTINUE",
        escalation_level=0,
    )
    save_review_cool(fast_rev, mock_catalog)

    res_warn = signal_stepback_turnaround_anomalies("testproj", db_path, catalog_dir=mock_catalog)
    assert res_warn.level == "WARNING"
    assert res_warn.value == 1.0
    assert "stepback_turnaround_anomalies detected" in res_warn.message


def test_signal_16_invalid_citation_strings_trigger_warning(mock_catalog: Path):
    db_path = mock_catalog / "bathos.db"
    # Reviews with invalid citation strings like ["foo", "bar"] must have valid_citations == 0 (< 2)
    for i in range(4):
        r = StepBackReview(
            review_id=f"rev_bogus_{i}",
            campaign_id="camp_bogus",
            project_slug="testproj2",
            created_at=f"2026-10-27T10:0{i}:00Z",
            reviewer_id=f"r_{i}",
            reviewer_kind="human",
            is_independent=True,
            path_kind="rubric",
            input_snapshot_sha256="sha",
            input_snapshot="{}",
            recommendation="CONTINUE",
            escalation_level=0,
            cited_evidence=["foo", "bar"],
        )
        save_review_cool(r, mock_catalog)

    res = signal_stepback_low_evidence_rate("testproj2", db_path, catalog_dir=mock_catalog)
    assert res.level == "WARNING"
    assert res.value == 1.0
    assert "4/4 reviews lack >= 2 valid citations" in res.message

