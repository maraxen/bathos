"""Tests for Step-Back Review Decision D1 gate at campaign conclude (spec 261027).

Verifies:
1. Active Level 2 hold on confirmation campaign downgrades verdict to 'confounded'.
2. Active Level 3 escalation on sequential campaign downgrades verdict to 'confounded'.
3. Concluding with force_verdict=True on confirmation campaign bypasses downgrade, preserving verdict.
4. Active Level 2 hold on exploration campaign emits warning banner without downgrade.
5. Nominal campaign (level 0) concludes normally with original verdict.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from bathos.campaigns import conclude_campaign, create_campaign, get_campaign
from bathos.stepback.catalog import save_escalation_cool
from bathos.stepback.schema import EscalationRecord


@pytest.fixture
def test_db_and_catalog(tmp_path: Path):
    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir(parents=True)
    db_path = cat_dir / "bathos.db"
    con = duckdb.connect(str(db_path))

    # Initialize minimal schema
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
        CREATE TABLE IF NOT EXISTS campaign_runs (
            campaign_id TEXT,
            run_id TEXT
        );
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            started_at TEXT,
            status TEXT
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
        """
    )
    yield con, cat_dir
    con.close()


def test_conclude_downgrades_on_active_level2_hold(test_db_and_catalog, capsys):
    db, cat_dir = test_db_and_catalog

    # Create confirmation campaign
    camp = create_campaign(
        db,
        "camp_conf_l2",
        mode="confirmation",
        question="Is stability maintained?",
        hypothesis="NVT stable",
        project_slug="testproj",
        catalog_dir=cat_dir,
    )

    # Put campaign into Level 2 Hold
    esc = EscalationRecord(
        escalation_id="esc_l2_hold",
        campaign_id=camp.id,
        project_slug="testproj",
        level=2,
        status="active",
        opened_at="2026-10-27T10:00:00Z",
        opened_by_kind="system",
        trigger_reason="Repeated marginal outcomes",
    )
    save_escalation_cool(esc, cat_dir)

    # Conclude with 'pass'
    conclude_campaign(db, camp.id, "pass", "Completed runs", catalog_dir=cat_dir)

    out = capsys.readouterr().out
    assert "Step-back review gate: Campaign" in out
    assert "verdict downgraded to 'confounded'" in out

    # Verify DuckDB campaign state
    refreshed = get_campaign(db, camp.id, catalog_dir=cat_dir)
    assert refreshed is not None
    assert refreshed.status == "concluded"
    assert refreshed.outcome_label == "confounded"


def test_conclude_downgrades_on_active_level3_escalation(test_db_and_catalog, capsys):
    db, cat_dir = test_db_and_catalog

    # Create sequential campaign
    camp = create_campaign(
        db,
        "camp_seq_l3",
        mode="sequential",
        question="Does sequential test pass?",
        hypothesis="Sequential test",
        project_slug="testproj",
        catalog_dir=cat_dir,
    )

    # Put campaign into Level 3 Human Chair Escalation
    esc = EscalationRecord(
        escalation_id="esc_l3_chair",
        campaign_id=camp.id,
        project_slug="testproj",
        level=3,
        status="active",
        opened_at="2026-10-27T10:00:00Z",
        opened_by_kind="system",
        trigger_reason="Severe confound detected",
    )
    save_escalation_cool(esc, cat_dir)

    # Conclude with 'pass'
    conclude_campaign(db, camp.id, "pass", "Completed runs", catalog_dir=cat_dir)

    out = capsys.readouterr().out
    assert "Human Chair Escalation (Level 3)" in out
    assert "verdict downgraded to 'confounded'" in out

    refreshed = get_campaign(db, camp.id, catalog_dir=cat_dir)
    assert refreshed is not None
    assert refreshed.outcome_label == "confounded"


def test_conclude_force_verdict_bypasses_downgrade(test_db_and_catalog, capsys):
    db, cat_dir = test_db_and_catalog

    camp = create_campaign(
        db,
        "camp_conf_force",
        mode="confirmation",
        question="Is stability maintained?",
        hypothesis="NVT stable",
        project_slug="testproj",
        catalog_dir=cat_dir,
    )

    esc = EscalationRecord(
        escalation_id="esc_l2_force",
        campaign_id=camp.id,
        project_slug="testproj",
        level=2,
        status="active",
        opened_at="2026-10-27T10:00:00Z",
        opened_by_kind="system",
        trigger_reason="Repeated marginal outcomes",
    )
    save_escalation_cool(esc, cat_dir)

    # Conclude with force_verdict=True
    conclude_campaign(
        db,
        camp.id,
        "pass",
        "Forced completion",
        catalog_dir=cat_dir,
        force_verdict=True,
    )

    out = capsys.readouterr().out
    assert "WARNING: Step-back review Hold (Level 2) is active" in out
    assert "Bypassed via --force-verdict" in out

    refreshed = get_campaign(db, camp.id, catalog_dir=cat_dir)
    assert refreshed is not None
    assert refreshed.status == "concluded"
    assert refreshed.outcome_label == "pass"


def test_conclude_exploration_mode_warning_only(test_db_and_catalog, capsys):
    db, cat_dir = test_db_and_catalog

    camp = create_campaign(
        db,
        "camp_explore_hold",
        mode="exploration",
        question="Exploring parameter space",
        hypothesis="Exploration",
        project_slug="testproj",
        catalog_dir=cat_dir,
    )

    esc = EscalationRecord(
        escalation_id="esc_l2_explore",
        campaign_id=camp.id,
        project_slug="testproj",
        level=2,
        status="active",
        opened_at="2026-10-27T10:00:00Z",
        opened_by_kind="system",
        trigger_reason="Plateau detected",
    )
    save_escalation_cool(esc, cat_dir)

    # Conclude with 'pass'
    conclude_campaign(db, camp.id, "pass", "Exploration done", catalog_dir=cat_dir)

    out = capsys.readouterr().out
    assert "WARNING: Campaign" in out
    assert "has an active step-back Hold (Level 2) at conclusion (exploration mode, no downgrade)" in out

    refreshed = get_campaign(db, camp.id, catalog_dir=cat_dir)
    assert refreshed is not None
    assert refreshed.status == "concluded"
    assert refreshed.outcome_label == "pass"


def test_conclude_nominal_campaign_no_downgrade(test_db_and_catalog, capsys):
    db, cat_dir = test_db_and_catalog

    camp = create_campaign(
        db,
        "camp_nominal",
        mode="confirmation",
        question="Nominal test",
        hypothesis="Nominal hypothesis",
        project_slug="testproj",
        catalog_dir=cat_dir,
    )

    # Conclude with 'pass'
    conclude_campaign(db, camp.id, "pass", "Nominal conclusion", catalog_dir=cat_dir)

    out = capsys.readouterr().out
    assert "Step-back review gate" not in out

    refreshed = get_campaign(db, camp.id, catalog_dir=cat_dir)
    assert refreshed is not None
    assert refreshed.status == "concluded"
    assert refreshed.outcome_label == "pass"
