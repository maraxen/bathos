"""Tests for Step-Back Review and Escalation Storage and Compaction (spec 261027)."""

from pathlib import Path

import duckdb
import pytest

from bathos.stepback.catalog import (
    get_escalation_state,
    get_review,
    ingest_stepback_fragments,
    iter_cool_reviews,
    list_reviews,
    load_escalation_cool,
    load_review_cool,
    save_escalation_cool,
    save_review_cool,
)
from bathos.stepback.schema import (
    DissentRecord,
    EscalationRecord,
    OpenAnalysis,
    StepBackReview,
)


@pytest.fixture
def mock_catalog(tmp_path: Path) -> Path:
    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir(parents=True, exist_ok=True)
    return cat_dir


def test_save_and_load_review_cool(mock_catalog: Path, tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()

    review = StepBackReview(
        review_id="rev_112233445566",
        campaign_id="camp_test1",
        project_slug="test_proj",
        created_at="2026-10-27T12:00:00Z",
        reviewer_id="human_expert",
        reviewer_kind="human",
        is_independent=True,
        path_kind="rubric",
        input_snapshot_sha256="abc123sha",
        input_snapshot="{}",
        recommendation="CONTINUE",
        escalation_level=0,
        cited_evidence=["run:run_1", "run:run_2"],
        rubric_id="stepback_v1",
        composite_score=4.2,
    )

    saved_path = save_review_cool(review, mock_catalog, workspace_root=ws)
    assert saved_path.exists()
    assert saved_path == mock_catalog / "stepback" / "reviews" / "camp_test1" / "review_rev_112233445566.json"

    # Also flat alias exists
    flat_path = mock_catalog / "stepback" / "reviews" / "camp_test1_rev_112233445566.json"
    assert flat_path.exists()

    # Mirrored to workspace
    ws_mirror = ws / ".bth" / "stepback" / "reviews" / "camp_test1" / "review_rev_112233445566.json"
    assert ws_mirror.exists()

    # Load back
    loaded = load_review_cool(saved_path)
    assert loaded.review_id == review.review_id
    assert loaded.campaign_id == review.campaign_id
    assert loaded.composite_score == 4.2
    assert loaded.recommendation == "CONTINUE"
    assert loaded.cited_evidence == ["run:run_1", "run:run_2"]


def test_save_and_load_escalation_cool(mock_catalog: Path):
    esc = EscalationRecord(
        escalation_id="esc_aabbccddeeff",
        campaign_id="camp_test1",
        project_slug="test_proj",
        level=2,
        status="active",
        opened_at="2026-10-27T12:00:00Z",
        opened_by_kind="trigger",
        trigger_name="repeated_marginal",
        trigger_reason="streak=3",
    )

    saved_path = save_escalation_cool(esc, mock_catalog)
    assert saved_path.exists()
    assert saved_path.name == "camp_test1_esc_aabbccddeeff.json"

    loaded = load_escalation_cool(saved_path)
    assert loaded.escalation_id == esc.escalation_id
    assert loaded.level == 2
    assert loaded.status == "active"
    assert loaded.trigger_name == "repeated_marginal"


def test_iter_and_list_reviews(mock_catalog: Path):
    r1 = StepBackReview(
        review_id="rev_0001",
        campaign_id="camp_a",
        project_slug="p",
        created_at="2026-10-27T10:00:00Z",
        reviewer_id="r1",
        reviewer_kind="agent",
        is_independent=True,
        path_kind="rubric",
        input_snapshot_sha256="sha1",
        input_snapshot="{}",
        recommendation="CONTINUE",
        escalation_level=0,
    )
    r2 = StepBackReview(
        review_id="rev_0002",
        campaign_id="camp_b",
        project_slug="p",
        created_at="2026-10-27T11:00:00Z",
        reviewer_id="r2",
        reviewer_kind="human",
        is_independent=True,
        path_kind="open_analysis",
        input_snapshot_sha256="sha2",
        input_snapshot="{}",
        recommendation="HOLD",
        escalation_level=2,
        open_analysis=OpenAnalysis(mode="pre_mortem", sections={"failure_narrative": "drift"}),
        dissents=[DissentRecord(dissenting_reviewer_id="d1", minority_recommendation="CONTINUE")],
    )

    save_review_cool(r1, mock_catalog)
    save_review_cool(r2, mock_catalog)

    cool_files = iter_cool_reviews(mock_catalog)
    # Deduplicated by review_id
    assert len(cool_files) == 2

    all_reviews = list_reviews(catalog_dir=mock_catalog)
    assert len(all_reviews) == 2

    camp_b_reviews = list_reviews(campaign_id="camp_b", catalog_dir=mock_catalog)
    assert len(camp_b_reviews) == 1
    assert camp_b_reviews[0].review_id == "rev_0002"
    assert camp_b_reviews[0].open_analysis is not None
    assert camp_b_reviews[0].open_analysis.mode == "pre_mortem"
    assert len(camp_b_reviews[0].dissents) == 1

    found = get_review("rev_0001", catalog_dir=mock_catalog)
    assert found is not None
    assert found.review_id == "rev_0001"

    missing = get_review("rev_nonexistent", catalog_dir=mock_catalog)
    assert missing is None


def test_escalation_history_and_state(mock_catalog: Path):
    esc1 = EscalationRecord(
        escalation_id="esc_01",
        campaign_id="camp_alpha",
        project_slug="p",
        level=1,
        status="active",
        opened_at="2026-10-27T08:00:00Z",
        opened_by_kind="trigger",
        trigger_name="near_bound",
        trigger_reason="d=0.20",
    )
    esc2 = EscalationRecord(
        escalation_id="esc_02",
        campaign_id="camp_alpha",
        project_slug="p",
        level=2,
        status="active",
        opened_at="2026-10-27T09:00:00Z",
        opened_by_kind="trigger",
        trigger_name="repeated_marginal",
        trigger_reason="streak=3",
    )
    save_escalation_cool(esc1, mock_catalog)
    save_escalation_cool(esc2, mock_catalog)

    state = get_escalation_state("camp_alpha", catalog_dir=mock_catalog)
    assert state.current_level == 2
    assert state.status == "hold"
    assert state.active_escalation_id == "esc_02"
    assert "near_bound" in state.active_triggers
    assert "repeated_marginal" in state.active_triggers

    # Resolve escalation
    esc3 = EscalationRecord(
        escalation_id="esc_03",
        campaign_id="camp_alpha",
        project_slug="p",
        level=0,
        status="resolved",
        opened_at="2026-10-27T10:00:00Z",
        opened_by_kind="review",
        resolved_at="2026-10-27T10:00:00Z",
        resolved_by="human_chair",
        resolved_kind="chair",
        deescalated_to=0,
    )
    save_escalation_cool(esc3, mock_catalog)

    state_resolved = get_escalation_state("camp_alpha", catalog_dir=mock_catalog)
    assert state_resolved.current_level == 0
    assert state_resolved.status == "nominal"
    assert state_resolved.active_escalation_id is None
    assert len(state_resolved.active_triggers) == 0


def test_duckdb_ingestion_and_compaction(mock_catalog: Path):
    db_path = mock_catalog / "bathos.db"
    con = duckdb.connect(str(db_path))

    rev = StepBackReview(
        review_id="rev_ingest_1",
        campaign_id="camp_ingest",
        project_slug="p",
        created_at="2026-10-27T10:00:00Z",
        reviewer_id="r1",
        reviewer_kind="agent",
        is_independent=True,
        path_kind="rubric",
        input_snapshot_sha256="sha1",
        input_snapshot="{}",
        recommendation="CONTINUE",
        escalation_level=0,
    )
    esc = EscalationRecord(
        escalation_id="esc_ingest_1",
        campaign_id="camp_ingest",
        project_slug="p",
        level=1,
        status="active",
        opened_at="2026-10-27T10:00:00Z",
        opened_by_kind="trigger",
        trigger_name="near_bound",
        trigger_reason="d=0.20",
    )

    save_review_cool(rev, mock_catalog)
    save_escalation_cool(esc, mock_catalog)

    r_ingested, e_ingested = ingest_stepback_fragments(con, mock_catalog)
    assert r_ingested == 1
    assert e_ingested == 1

    # Idempotent second ingestion
    r_ingested2, e_ingested2 = ingest_stepback_fragments(con, mock_catalog)
    assert r_ingested2 == 0
    assert e_ingested2 == 0

    # Query DuckDB
    rev_rows = con.execute("SELECT id, campaign_id, recommendation FROM stepback_reviews").fetchall()
    assert len(rev_rows) == 1
    assert rev_rows[0] == ("rev_ingest_1", "camp_ingest", "CONTINUE")

    esc_rows = con.execute("SELECT id, campaign_id, level, status FROM stepback_escalations").fetchall()
    assert len(esc_rows) == 1
    assert esc_rows[0] == ("esc_ingest_1", "camp_ingest", 1, "active")

    con.close()
