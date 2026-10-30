"""Tests for Step-Back Review and Escalation Storage and Compaction (spec 261027)."""

from pathlib import Path

import duckdb
import pytest

from bathos.stepback.catalog import (
    get_escalation_state,
    get_review,
    ingest_stepback_fragments,
    iter_cool_reviews,
    list_escalations,
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
    assert (
        saved_path
        == mock_catalog / "stepback" / "reviews" / "camp_test1" / "review_rev_112233445566.json"
    )

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
    rev_rows = con.execute(
        "SELECT id, campaign_id, recommendation FROM stepback_reviews"
    ).fetchall()
    assert len(rev_rows) == 1
    assert rev_rows[0] == ("rev_ingest_1", "camp_ingest", "CONTINUE")

    esc_rows = con.execute(
        "SELECT id, campaign_id, level, status FROM stepback_escalations"
    ).fetchall()
    assert len(esc_rows) == 1
    assert esc_rows[0] == ("esc_ingest_1", "camp_ingest", 1, "active")

    con.close()


def test_migrate_v18_to_v19_dict():
    """Verify fragment dictionary migration from v18 to v19 stamps version 19."""
    from bathos.compact import MIGRATIONS

    fragment_dict = {"schema_version": "18", "command": "run", "git_hash": "abc"}
    migrated = MIGRATIONS["18"](fragment_dict)
    assert migrated["schema_version"] == "19"
    assert migrated["command"] == "run"


def test_compact_v18_catalog_migration(tmp_path: Path):
    """Verify compact() upgrades an existing v18 catalog to v19 warm version and tables."""
    from bathos.compact import compact
    from bathos.schema import CURRENT_SCHEMA_VERSION

    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir(parents=True, exist_ok=True)
    db_path = cat_dir / "bathos.db"

    # Pre-create a v18 database
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE _schema_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute(
        "CREATE TABLE _schema_migrations (warm_version TEXT NOT NULL, migrated_at TIMESTAMPTZ DEFAULT now(), notes TEXT)"
    )
    con.execute("INSERT INTO _schema_meta (key, value) VALUES ('warm_version', '18')")
    con.execute("INSERT INTO _schema_migrations (warm_version, notes) VALUES ('18', 'initial')")
    con.close()

    # Create cool review and escalation to verify warm ingestion during compact()
    rev = StepBackReview(
        review_id="rev_migrated_1",
        campaign_id="camp_migrated",
        project_slug="p",
        created_at="2026-10-27T10:00:00Z",
        reviewer_id="human_reviewer",
        reviewer_kind="human",
        is_independent=True,
        path_kind="rubric",
        input_snapshot_sha256="sha1",
        input_snapshot="{}",
        recommendation="CONTINUE",
        escalation_level=0,
    )
    esc = EscalationRecord(
        escalation_id="esc_migrated_1",
        campaign_id="camp_migrated",
        project_slug="p",
        level=1,
        status="active",
        opened_at="2026-10-27T10:00:00Z",
        opened_by_kind="trigger",
        trigger_name="near_bound",
        trigger_reason="d=0.20",
    )
    save_review_cool(rev, cat_dir)
    save_escalation_cool(esc, cat_dir)

    # Run compact
    compact(cat_dir)

    # Verify DuckDB state after compaction
    con = duckdb.connect(str(db_path))
    warm_version = con.execute(
        "SELECT value FROM _schema_meta WHERE key = 'warm_version'"
    ).fetchone()
    assert warm_version is not None
    assert warm_version[0] == "19"
    assert warm_version[0] == CURRENT_SCHEMA_VERSION

    migrations = con.execute(
        "SELECT warm_version, notes FROM _schema_migrations ORDER BY migrated_at"
    ).fetchall()
    assert ("19", "compact") in [(m[0], m[1]) for m in migrations]

    # Tables exist and cool fragments were ingested
    rev_rows = con.execute(
        "SELECT id, campaign_id, recommendation FROM stepback_reviews"
    ).fetchall()
    assert len(rev_rows) == 1
    assert rev_rows[0] == ("rev_migrated_1", "camp_migrated", "CONTINUE")

    esc_rows = con.execute(
        "SELECT id, campaign_id, level, status FROM stepback_escalations"
    ).fetchall()
    assert len(esc_rows) == 1
    assert esc_rows[0] == ("esc_migrated_1", "camp_migrated", 1, "active")

    con.close()


def test_telemetry_events_emitted(mock_catalog: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify stepback.review_recorded and stepback.escalation_created telemetry events emit."""
    emitted_events: list[tuple[str, dict]] = []

    def mock_event(name: str, **fields):
        emitted_events.append((name, fields))

    monkeypatch.setattr("bathos.stepback.catalog.event", mock_event)

    rev = StepBackReview(
        review_id="rev_telem_1",
        campaign_id="camp_telem",
        project_slug="p",
        created_at="2026-10-27T10:00:00Z",
        reviewer_id="agent_reviewer",
        reviewer_kind="agent",
        is_independent=True,
        path_kind="rubric",
        input_snapshot_sha256="sha1",
        input_snapshot="{}",
        recommendation="PIVOT",
        escalation_level=1,
        cited_evidence=["run_1", "run_2"],
        dissents=[DissentRecord(dissenting_reviewer_id="d1", minority_recommendation="CONTINUE")],
    )
    save_review_cool(rev, mock_catalog)

    assert len(emitted_events) == 1
    name, fields = emitted_events[0]
    assert name == "stepback.review_recorded"
    assert fields["review_id"] == "rev_telem_1"
    assert fields["campaign_id"] == "camp_telem"
    assert fields["has_dissent"] is True
    assert fields["cited_evidence_count"] == 2
    assert fields["recommendation"] == "PIVOT"

    esc = EscalationRecord(
        escalation_id="esc_telem_1",
        campaign_id="camp_telem",
        project_slug="p",
        level=2,
        status="active",
        opened_at="2026-10-27T10:00:00Z",
        opened_by_kind="trigger",
        trigger_name="repeated_marginal",
        trigger_reason="streak=3",
    )
    save_escalation_cool(esc, mock_catalog)

    assert len(emitted_events) == 2
    esc_name, esc_fields = emitted_events[1]
    assert esc_name == "stepback.escalation_created"
    assert esc_fields["escalation_id"] == "esc_telem_1"
    assert esc_fields["level"] == 2
    assert esc_fields["status"] == "active"
    assert esc_fields["trigger_name"] == "repeated_marginal"


def test_escalation_state_snooze_preserves_level2_hold(mock_catalog: Path):
    """L2-F01: Snoozing a trigger pauses trigger firing but does NOT reset Level 2 hold to Level 0."""
    esc_hold = EscalationRecord(
        escalation_id="esc_h1",
        campaign_id="camp_snooze_test",
        project_slug="p",
        level=2,
        status="active",
        opened_at="2026-10-27T08:00:00Z",
        opened_by_kind="trigger",
        trigger_name="repeated_marginal",
        trigger_reason="streak=3",
    )
    save_escalation_cool(esc_hold, mock_catalog)

    state_before = get_escalation_state("camp_snooze_test", catalog_dir=mock_catalog)
    assert state_before.current_level == 2
    assert state_before.status == "hold"
    assert "repeated_marginal" in state_before.active_triggers

    # Now add a snooze record for 'repeated_marginal'
    esc_snooze = EscalationRecord(
        escalation_id="esc_s1",
        campaign_id="camp_snooze_test",
        project_slug="p",
        level=0,
        status="snoozed",
        opened_at="2026-10-27T09:00:00Z",
        opened_by_kind="reviewer",
        trigger_name="repeated_marginal",
        trigger_reason="Investigating margin bounds",
        metadata={"snooze_trigger_name": "repeated_marginal"},
    )
    save_escalation_cool(esc_snooze, mock_catalog)

    state_after = get_escalation_state("camp_snooze_test", catalog_dir=mock_catalog)
    # MUST still be level 2 and hold!
    assert state_after.current_level == 2
    assert state_after.status == "hold"
    # Trigger firing is paused for repeated_marginal
    assert "repeated_marginal" not in state_after.active_triggers


def test_list_reviews_and_escalations_merges_warm_and_cool(mock_catalog: Path):
    """L2-F02: list_reviews and list_escalations must merge warm DuckDB records with uncompacted cool fragments."""
    db_path = mock_catalog / "bathos.db"
    con = duckdb.connect(str(db_path))

    # Ingest r1 into warm DB
    r1 = StepBackReview(
        review_id="rev_merge_1",
        campaign_id="camp_merge",
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
    e1 = EscalationRecord(
        escalation_id="esc_merge_1",
        campaign_id="camp_merge",
        project_slug="p",
        level=1,
        status="active",
        opened_at="2026-10-27T10:00:00Z",
        opened_by_kind="trigger",
        trigger_name="near_bound",
        trigger_reason="d=0.20",
    )
    save_review_cool(r1, mock_catalog)
    save_escalation_cool(e1, mock_catalog)
    ingest_stepback_fragments(con, mock_catalog)

    # Now write new cool-only review and escalation (uncompacted)
    r2 = StepBackReview(
        review_id="rev_merge_2",
        campaign_id="camp_merge",
        project_slug="p",
        created_at="2026-10-27T11:00:00Z",
        reviewer_id="r2",
        reviewer_kind="agent",
        is_independent=True,
        path_kind="rubric",
        input_snapshot_sha256="sha2",
        input_snapshot="{}",
        recommendation="PIVOT",
        escalation_level=1,
    )
    e2 = EscalationRecord(
        escalation_id="esc_merge_2",
        campaign_id="camp_merge",
        project_slug="p",
        level=2,
        status="active",
        opened_at="2026-10-27T11:00:00Z",
        opened_by_kind="trigger",
        trigger_name="repeated_marginal",
        trigger_reason="streak=3",
    )
    save_review_cool(r2, mock_catalog)
    save_escalation_cool(e2, mock_catalog)

    # Calling list_reviews and list_escalations with BOTH db and catalog_dir
    reviews = list_reviews(campaign_id="camp_merge", catalog_dir=mock_catalog, db=con)
    escalations = list_escalations(campaign_id="camp_merge", catalog_dir=mock_catalog, db=con)

    assert len(reviews) == 2
    review_ids = [r.review_id for r in reviews]
    assert "rev_merge_1" in review_ids
    assert "rev_merge_2" in review_ids

    assert len(escalations) == 2
    esc_ids = [e.escalation_id for e in escalations]
    assert "esc_merge_1" in esc_ids
    assert "esc_merge_2" in esc_ids

    con.close()


def test_save_cool_telemetry_failure_isolation(mock_catalog: Path, monkeypatch: pytest.MonkeyPatch):
    """L3-F03: telemetry failure must never raise out of save_review_cool or save_escalation_cool."""
    def broken_event(*_args, **_kwargs):
        raise RuntimeError("Telemetry failure!")

    monkeypatch.setattr("bathos.stepback.catalog.event", broken_event)

    r = StepBackReview(
        review_id="rev_telem_fail",
        campaign_id="camp_tf",
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
    e = EscalationRecord(
        escalation_id="esc_telem_fail",
        campaign_id="camp_tf",
        project_slug="p",
        level=1,
        status="active",
        opened_at="2026-10-27T10:00:00Z",
        opened_by_kind="trigger",
        trigger_name="near_bound",
        trigger_reason="d=0.20",
    )

    # Both must succeed without error
    rev_path = save_review_cool(r, mock_catalog)
    assert rev_path.is_file()

    esc_path = save_escalation_cool(e, mock_catalog)
    assert esc_path.is_file()


def test_signature_sha256_duckdb_roundtrip(mock_catalog: Path):
    """L3-F04: signature_sha256 is persisted in DuckDB and recovered verbatim."""
    db_path = mock_catalog / "bathos.db"
    con = duckdb.connect(str(db_path))

    r = StepBackReview(
        review_id="rev_sig_1",
        campaign_id="camp_sig",
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
    orig_sig = r.signature_sha256
    assert orig_sig is not None
    assert len(orig_sig) == 64

    save_review_cool(r, mock_catalog)
    ingest_stepback_fragments(con, mock_catalog)

    reviews = list_reviews(campaign_id="camp_sig", db=con)
    assert len(reviews) == 1
    loaded = reviews[0]
    assert loaded.signature_sha256 == orig_sig
    assert loaded.compute_signature() == orig_sig

    con.close()


def test_escalation_resolved_telemetry_on_resolution_and_deescalation(mock_catalog: Path, monkeypatch: pytest.MonkeyPatch):
    """L3-F06: escalation resolution and de-escalation emit stepback.escalation_resolved."""
    events = []
    monkeypatch.setattr("bathos.stepback.catalog.event", lambda name, **fields: events.append((name, fields)))

    # Resolution (level 0)
    e_res = EscalationRecord(
        escalation_id="esc_r1",
        campaign_id="camp_res",
        project_slug="p",
        level=0,
        status="resolved",
        opened_at="2026-10-27T10:00:00Z",
        opened_by_kind="chair",
        resolved_at="2026-10-27T11:00:00Z",
        resolved_by="human_chair",
        resolution_note="Resolved",
        deescalated_to=0,
    )
    save_escalation_cool(e_res, mock_catalog)
    assert len(events) == 1
    assert events[0][0] == "stepback.escalation_resolved"
    assert events[0][1]["deescalated_to"] == 0

    # De-escalation (level 1 from 2)
    e_deesc = EscalationRecord(
        escalation_id="esc_d1",
        campaign_id="camp_res",
        project_slug="p",
        level=1,
        status="active",
        opened_at="2026-10-27T10:00:00Z",
        opened_by_kind="chair",
        resolved_at="2026-10-27T11:00:00Z",
        resolved_by="human_chair",
        resolution_note="De-escalated to warning",
        deescalated_to=1,
    )
    save_escalation_cool(e_deesc, mock_catalog)
    assert len(events) == 2
    assert events[1][0] == "stepback.escalation_resolved"
    assert events[1][1]["deescalated_to"] == 1

