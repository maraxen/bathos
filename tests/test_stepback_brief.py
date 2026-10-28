"""Tests for Advisor-Ready Brief and Project Status Matrix Integration (spec 261027).

Verifies:
1. `format_project_status_entry` for all 4 escalation levels (L0, L1, L2, L3)
2. `render_advisor_brief` 8 standard sections:
   - Stakes & Gap (Question, Hypothesis, Mode, Status)
   - Trajectory & Outcomes (Run count, pass rate, outcome distribution, trajectory)
   - Threshold Basis & Drift (Threshold, drift warning)
   - Alternative Hypotheses & Dissents (Preserved minority dissents, alternative narratives)
   - Exit Criteria & Sunk Cost (Exit condition, duration, compute spend)
   - Escalation Status & Chair Disposition (Level 0-3, chair intervention)
   - Project Status Matrix Entry (Direct snippet)
3. CLI `bth stepback brief <campaign_id>` and `bth stepback show <campaign_id> --format brief`
4. FastMCP tool `stepback_brief`
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from bathos.campaigns import create_campaign
from bathos.cli_cyclopts import app
from bathos.mcp import stepback_brief
from bathos.stepback.brief import format_project_status_entry, render_advisor_brief
from bathos.stepback.catalog import save_escalation_cool, save_review_cool
from bathos.stepback.schema import DissentRecord, EscalationRecord, OpenAnalysis, StepBackReview
from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()


@pytest.fixture
def brief_test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir(parents=True)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(cat_dir))

    # Clear agent vars
    for var in (
        "BTH_AGENT_MODE",
        "CLAUDE_AGENT",
        "CLAUDE_AGENT_ID",
        "AGENT_NAME",
        "SUBAGENT_ID",
        "ANTIGRAVITY_AGENT",
    ):
        monkeypatch.delenv(var, raising=False)

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
            seq_position INTEGER
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
            duration_s REAL,
            metadata TEXT
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
            review_epoch INTEGER,
            pivot_anchor_run_id TEXT,
            resolved_at TEXT,
            resolved_by TEXT,
            resolved_kind TEXT,
            resolution_note TEXT,
            deescalated_to INTEGER,
            metadata TEXT
        );
        CREATE TABLE IF NOT EXISTS stepback_reviews (
            id TEXT PRIMARY KEY,
            campaign_id TEXT,
            project_slug TEXT,
            created_at TEXT,
            reviewer_id TEXT,
            reviewer_kind TEXT,
            is_independent BOOLEAN,
            path_kind TEXT,
            input_snapshot_sha256 TEXT,
            input_snapshot TEXT,
            rubric_id TEXT,
            rubric_version TEXT,
            rubric_sha256 TEXT,
            scores_json TEXT,
            composite_score REAL,
            open_analysis_mode TEXT,
            open_analysis_json TEXT,
            cited_evidence_json TEXT,
            recommendation TEXT,
            escalation_level INTEGER,
            is_dissent BOOLEAN,
            parent_panel_id TEXT,
            dissents_json TEXT,
            metadata TEXT
        );
        """
    )
    con.close()
    return cat_dir




def _sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()

def _stamp_runs(con, cat_dir: Path, run_ids: list[str]) -> None:
    """Give fixture runs the recorded time and on-disk sidecar that real runs carry."""
    for i, rid in enumerate(run_ids):
        sidecar = cat_dir / f"{rid}.bth.toml"
        sidecar.write_text('[experiment]\nhypothesis = "fixture"\n')
        con.execute(
            "UPDATE runs SET started_at = ?, sidecar_path = ?, sidecar_sha256 = ? WHERE id = ?",
            [f"2026-10-27T{10 + i:02d}:00:00Z", str(sidecar), _sha256_file(sidecar), rid],
        )

def test_format_project_status_entry_l0(brief_test_env: Path):
    cat_dir = brief_test_env
    db_path = cat_dir / "bathos.db"
    con = duckdb.connect(str(db_path))

    camp = create_campaign(
        con,
        name="L0 Test",
        project_slug="test_proj",
        mode="exploration",
        catalog_dir=cat_dir,
    )

    # Add 4 runs: 3 pass, 1 fail
    con.execute(
        """
        INSERT INTO runs (id, outcome, duration_s) VALUES
        ('r1', 'pass', 12.5),
        ('r2', 'pass', 15.0),
        ('r3', 'pass', 10.0),
        ('r4', 'fail', 8.0)
        """
    )
    _stamp_runs(con, cat_dir, ["r1", "r2", "r3", "r4"])
    con.execute(
        """
        INSERT INTO campaign_runs (campaign_id, run_id) VALUES
        (?, 'r1'), (?, 'r2'), (?, 'r3'), (?, 'r4')
        """,
        [camp.id, camp.id, camp.id, camp.id],
    )
    con.close()

    entry = format_project_status_entry(camp.id, catalog_dir=cat_dir)
    # 4 runs, 3 pass -> pass rate 0.75
    assert entry == "[NOMINAL: 4 runs, pass rate 0.75]"


def test_format_project_status_entry_l1(brief_test_env: Path):
    cat_dir = brief_test_env
    db_path = cat_dir / "bathos.db"
    con = duckdb.connect(str(db_path))

    camp = create_campaign(
        con,
        name="L1 Test",
        project_slug="test_proj",
        mode="exploration",
        catalog_dir=cat_dir,
    )

    # Save L1 active escalation
    esc = EscalationRecord(
        escalation_id="esc_l1_001",
        campaign_id=camp.id,
        project_slug="test_proj",
        level=1,
        status="active",
        opened_at=datetime.now(UTC).isoformat(),
        opened_by_kind="system",
        trigger_name="repeated_marginal",
        trigger_reason="3 consecutive marginal runs",
    )
    save_escalation_cool(esc, cat_dir)
    con.close()

    entry = format_project_status_entry(camp.id, catalog_dir=cat_dir)
    assert entry == "[ADVISORY L1: repeated_marginal active; review recommended]"


def test_format_project_status_entry_l2(brief_test_env: Path):
    cat_dir = brief_test_env
    db_path = cat_dir / "bathos.db"
    con = duckdb.connect(str(db_path))

    camp = create_campaign(
        con,
        name="L2 Test",
        project_slug="test_proj",
        mode="confirmation",
        catalog_dir=cat_dir,
    )

    # Save review with HOLD recommendation
    rev = StepBackReview(
        review_id="rev_test_001",
        campaign_id=camp.id,
        project_slug="test_proj",
        created_at=datetime.now(UTC).isoformat(),
        reviewer_id="reviewer_ext",
        reviewer_kind="agent",
        is_independent=True,
        path_kind="rubric",
        input_snapshot_sha256="abc123hash",
        input_snapshot="{}",
        recommendation="HOLD",
        escalation_level=2,
    )
    save_review_cool(rev, cat_dir)

    # Save L2 escalation
    esc = EscalationRecord(
        escalation_id="esc_l2_001",
        campaign_id=camp.id,
        project_slug="test_proj",
        level=2,
        status="active",
        opened_at=datetime.now(UTC).isoformat(),
        opened_by_kind="review",
        opened_by_review_id="rev_test_001",
        trigger_reason="Outcome entropy collapse",
    )
    save_escalation_cool(esc, cat_dir)
    con.close()

    entry = format_project_status_entry(camp.id, catalog_dir=cat_dir)
    assert entry == "[HOLD L2: Outcome entropy collapse; review rev_test_001 recommends HOLD]"


def test_format_project_status_entry_l3(brief_test_env: Path):
    cat_dir = brief_test_env
    db_path = cat_dir / "bathos.db"
    con = duckdb.connect(str(db_path))

    camp = create_campaign(
        con,
        name="L3 Test",
        project_slug="test_proj",
        mode="confirmation",
        catalog_dir=cat_dir,
    )

    esc = EscalationRecord(
        escalation_id="esc_l3_001",
        campaign_id=camp.id,
        project_slug="test_proj",
        level=3,
        status="active",
        opened_at=datetime.now(UTC).isoformat(),
        opened_by_kind="review",
        trigger_reason="Severe unmitigated confound in primary assay",
    )
    save_escalation_cool(esc, cat_dir)
    con.close()

    entry = format_project_status_entry(camp.id, catalog_dir=cat_dir)
    assert (
        entry
        == "[ESCALATE L3: Human Chair disposition required; Severe unmitigated confound in primary assay]"
    )


def test_render_advisor_brief_full(brief_test_env: Path):
    cat_dir = brief_test_env
    db_path = cat_dir / "bathos.db"
    con = duckdb.connect(str(db_path))

    camp = create_campaign(
        con,
        name="Kinase Inhibitor Optimization",
        project_slug="kinase_opt",
        mode="sequential",
        question="Does compound K-42 show selective sub-nanomolar binding?",
        hypothesis="K-42 achieves Kd < 1nM while maintaining 100x selectivity over off-target kinase.",
        catalog_dir=cat_dir,
    )
    # Lock stopping threshold
    con.execute(
        "UPDATE campaigns SET stopping_threshold = 0.05 WHERE id = ?",
        [camp.id],
    )

    # Insert 5 runs
    con.execute(
        """
        INSERT INTO runs (id, outcome, duration_s) VALUES
        ('r10', 'pass', 120.0),
        ('r11', 'pass', 115.0),
        ('r12', 'marginal', 130.0),
        ('r13', 'marginal', 125.0),
        ('r14', 'marginal', 140.0)
        """
    )
    _stamp_runs(con, cat_dir, [f"r{n}" for n in range(10, 15)])
    con.execute(
        """
        INSERT INTO campaign_runs (campaign_id, run_id) VALUES
        (?, 'r10'), (?, 'r11'), (?, 'r12'), (?, 'r13'), (?, 'r14')
        """,
        [camp.id, camp.id, camp.id, camp.id, camp.id],
    )

    # Create review with OpenAnalysis and Dissent
    dissent = DissentRecord(
        dissenting_reviewer_id="reviewer_skeptic",
        minority_recommendation="PIVOT",
        dissent_summary="Replicate pooling conceals systematic variation between batches.",
        cited_counter_evidence=["run_r12", "run_r13"],
        majority_rebuttal="Batch effect is within pre-registered tolerance.",
    )
    oa = OpenAnalysis(
        mode="advisor_convo",
        sections={
            "alternatives": "Assay artifact could result from non-specific aggregation at high concentration.",
            "exit_criteria": "Stop if next 2 runs fail to achieve Kd < 5nM.",
        },
    )
    rev = StepBackReview(
        review_id="rev_kinase_01",
        campaign_id=camp.id,
        project_slug="kinase_opt",
        created_at=datetime.now(UTC).isoformat(),
        reviewer_id="advisor_panel",
        reviewer_kind="panel",
        is_independent=True,
        path_kind="hybrid",
        input_snapshot_sha256="kinasesnapsha",
        input_snapshot="{}",
        recommendation="HOLD",
        escalation_level=2,
        open_analysis=oa,
        dissents=[dissent],
    )
    save_review_cool(rev, cat_dir)

    # Save escalation record
    esc = EscalationRecord(
        escalation_id="esc_kinase_01",
        campaign_id=camp.id,
        project_slug="kinase_opt",
        level=2,
        status="active",
        opened_at=datetime.now(UTC).isoformat(),
        opened_by_kind="review",
        opened_by_review_id="rev_kinase_01",
        trigger_reason="Repeated marginal outcomes on selectivity ratio",
    )
    save_escalation_cool(esc, cat_dir)
    con.close()

    brief_md = render_advisor_brief(camp.id, catalog_dir=cat_dir)

    # Check that all 8 standard sections are present
    assert f"# Advisor Brief: Kinase Inhibitor Optimization ({camp.id})" in brief_md
    assert "## 1. Stakes & Scientific Question (Stakes & Gap)" in brief_md
    assert "## 2. Trajectory & Outcomes" in brief_md
    assert "## 3. Threshold Basis & Drift" in brief_md
    assert "## 4. Alternative Hypotheses & Dissents" in brief_md
    assert "## 5. Exit Criteria & Sunk Cost" in brief_md
    assert "## 6. Escalation Status & Chair Disposition" in brief_md
    assert "## 7. Project Status Matrix Entry" in brief_md

    # Check specific details
    assert "Does compound K-42 show selective sub-nanomolar binding?" in brief_md
    assert "K-42 achieves Kd < 1nM" in brief_md
    assert "**Total Runs Evaluated**: 5" in brief_md
    assert "Stopping Threshold" in brief_md
    assert "0.05" in brief_md
    assert "Assay artifact could result from non-specific aggregation" in brief_md
    assert "reviewer_skeptic" in brief_md
    assert "Replicate pooling conceals systematic variation" in brief_md
    assert "Batch effect is within pre-registered tolerance" in brief_md
    assert "Stop if next 2 runs fail to achieve Kd < 5nM" in brief_md
    assert "Committee Hold (Level 2)" in brief_md
    assert "[HOLD L2: Repeated marginal outcomes on selectivity ratio; review rev_kinase_01 recommends HOLD]" in brief_md


def test_cli_stepback_brief(brief_test_env: Path):
    cat_dir = brief_test_env
    db_path = cat_dir / "bathos.db"
    con = duckdb.connect(str(db_path))

    camp = create_campaign(
        con,
        name="CLI Brief Test",
        project_slug="cli_proj",
        mode="exploration",
        catalog_dir=cat_dir,
    )
    con.close()

    # Test bth stepback brief <campaign_id>
    res = runner.invoke(app, ["stepback", "brief", camp.id, "--catalog-dir", str(cat_dir)])
    assert res.exit_code == 0
    assert "# Advisor Brief: CLI Brief Test" in res.stdout
    assert "## 7. Project Status Matrix Entry" in res.stdout

    # Test bth stepback show <campaign_id> --format brief
    res_show = runner.invoke(
        app, ["stepback", "show", "--campaign", camp.id, "--format", "brief", "--catalog-dir", str(cat_dir)]
    )
    assert res_show.exit_code == 0
    assert "# Advisor Brief: CLI Brief Test" in res_show.stdout


def test_mcp_stepback_brief(brief_test_env: Path):
    cat_dir = brief_test_env
    db_path = cat_dir / "bathos.db"
    con = duckdb.connect(str(db_path))

    camp = create_campaign(
        con,
        name="MCP Brief Test",
        project_slug="mcp_proj",
        mode="exploration",
        catalog_dir=cat_dir,
    )
    con.close()

    # Call stepback_brief
    res = asyncio.run(stepback_brief(campaign_id=camp.id))
    assert res["ok"] is True
    assert res["campaign_id"] == camp.id
    assert "# Advisor Brief: MCP Brief Test" in res["brief_markdown"]
    assert "[NOMINAL: 0 runs, pass rate 0.00]" in res["project_status_entry"]
    assert res["advisor_status_entry"] == ""
    assert format_project_status_entry(camp.id, catalog_dir=cat_dir, include_nominal=False) == ""
