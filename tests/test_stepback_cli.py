"""Tests for Step-Back Reviews Cyclopts CLI Commands (spec 261027 Section 7).

Verifies:
1. `bth stepback status`
2. `bth stepback run` and `bth stepback trigger`
3. `bth stepback record` (Path A rubric and Path B open analysis)
4. `bth stepback escalate`
5. `bth stepback show`
6. `bth stepback resolve` (Level 1/2 de-escalation)
7. Human Chair Invariance on Level 3:
   - Refused without interactive TTY or chair token
   - Allowed when valid chair token is supplied
   - Unconditionally refused when agent environment is detected
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from bathos.campaigns import create_campaign
from bathos.cli_cyclopts import app
from bathos.stepback.catalog import (
    get_escalation_state,
    save_escalation_cool,
)
from bathos.stepback.schema import EscalationRecord
from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()


@pytest.fixture
def cli_test_env(tmp_path: Path, monkeypatch):
    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir(parents=True)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(cat_dir))

    # Clean agent environment variables for baseline test
    for var in ("BTH_AGENT_MODE", "CLAUDE_AGENT", "CLAUDE_AGENT_ID", "AGENT_NAME", "SUBAGENT_ID", "ANTIGRAVITY_AGENT"):
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

    camp = create_campaign(
        con,
        "camp_cli_test",
        mode="confirmation",
        question="Does CLI parity work?",
        hypothesis="CLI parity",
        project_slug="testproj",
        catalog_dir=cat_dir,
    )

    con.execute(
        """
        INSERT INTO runs (id, started_at, status, author, agent_mode, outcome, tentative_outcome, sidecar_path, sidecar_sha256, metadata)
        VALUES ('run_cli_1', '2026-10-27T10:00:00Z', 'completed', 'exp_author', '', 'pass', NULL, '', '', '{"author": "exp_author"}'),
               ('run_cli_2', '2026-10-27T11:00:00Z', 'completed', 'exp_author', '', 'pass', NULL, '', '', '{"author": "exp_author"}');
        INSERT INTO campaign_runs (campaign_id, run_id)
        VALUES (?, 'run_cli_1'), (?, 'run_cli_2');
        """,
        [camp.id, camp.id],
    )
    con.commit()
    con.close()

    return {"catalog_dir": cat_dir, "campaign_id": camp.id, "tmp_path": tmp_path}


def test_cli_stepback_status_and_triggers(cli_test_env):
    camp_id = cli_test_env["campaign_id"]
    cat_dir = str(cli_test_env["catalog_dir"])

    res = runner.invoke(app, ["stepback", "status", camp_id, "--catalog-dir", cat_dir])
    assert res.exit_code == 0
    assert "Step-Back Status: Campaign" in res.output
    assert "Escalation Level: 0 (Nominal)" in res.output

    res_json = runner.invoke(app, ["stepback", "status", camp_id, "--catalog-dir", cat_dir, "--json-output"])
    assert res_json.exit_code == 0
    data = json.loads(res_json.output)
    assert data["campaign_id"] == camp_id
    assert data["escalation_level"] == 0

    res_run = runner.invoke(app, ["stepback", "run", camp_id, "--catalog-dir", cat_dir])
    assert res_run.exit_code == 0
    assert "Event Trigger Evaluation" in res_run.output

    res_trig = runner.invoke(app, ["stepback", "trigger", camp_id, "--catalog-dir", cat_dir])
    assert res_trig.exit_code == 0


def test_cli_stepback_record_rubric(cli_test_env):
    camp_id = cli_test_env["campaign_id"]
    cat_dir = str(cli_test_env["catalog_dir"])

    scores = json.dumps({
        "question_validity": 5,
        "empirical_progress": 5,
        "theoretical_grounding": 5,
        "resource_efficiency": 5,
        "risk_confound_surface": 5,
    })

    res = runner.invoke(
        app,
        [
            "stepback",
            "record",
            camp_id,
            "--path-kind",
            "rubric",
            "--scores",
            scores,
            "--reviewer-id",
            "external_expert",
            "--catalog-dir",
            cat_dir,
        ],
    )
    assert res.exit_code == 0
    assert "Recorded step-back review rev_" in res.output
    assert "recommendation=CONTINUE" in res.output


def test_cli_stepback_record_open_analysis(cli_test_env):
    camp_id = cli_test_env["campaign_id"]
    cat_dir = str(cli_test_env["catalog_dir"])

    narrative = json.dumps({
        "the_case_for_stopping": "The mechanism is already refuted in external literature.",
        "sunk_cost_fallacies_exposed": "Spending compute on further seeds will not alter thermodynamic bounds.",
        "superior_resource_reallocation": "Allocate GPU hours to Project B instead.",
        "retained_learnings": "Negative control bounds established.",
    })
    evidence = "run:run_cli_1,run:run_cli_2"

    res = runner.invoke(
        app,
        [
            "stepback",
            "review",
            camp_id,
            "--path-kind",
            "open_analysis",
            "--mode",
            "steelman_the_stop",
            "--narrative",
            narrative,
            "--evidence",
            evidence,
            "--recommendation",
            "HOLD",
            "--reviewer-id",
            "external_critic",
            "--catalog-dir",
            cat_dir,
        ],
    )
    assert res.exit_code == 0
    assert "Recorded step-back review rev_" in res.output
    assert "recommendation=HOLD" in res.output

    state = get_escalation_state(camp_id, catalog_dir=Path(cat_dir))
    assert state.current_level == 2


def test_cli_stepback_escalate_and_show(cli_test_env):
    camp_id = cli_test_env["campaign_id"]
    cat_dir = str(cli_test_env["catalog_dir"])

    res_esc = runner.invoke(
        app,
        ["stepback", "escalate", camp_id, "2", "--reason", "Manual Hold", "--catalog-dir", cat_dir],
    )
    assert res_esc.exit_code == 0
    assert "Escalated campaign" in res_esc.output
    assert "Level 2" in res_esc.output

    res_show = runner.invoke(app, ["stepback", "show", "--campaign", camp_id, "--catalog-dir", cat_dir])
    assert res_show.exit_code == 0
    assert '"ok": true' in res_show.output


def test_cli_stepback_resolve_level2(cli_test_env):
    camp_id = cli_test_env["campaign_id"]
    cat_dir = str(cli_test_env["catalog_dir"])

    # Escalate to Level 2
    runner.invoke(app, ["stepback", "escalate", camp_id, "2", "--reason", "Hold", "--catalog-dir", cat_dir])

    # Resolve from Level 2 to 0
    res_res = runner.invoke(
        app,
        ["stepback", "resolve", camp_id, "--deescalate-to", "0", "--note", "Mitigation applied", "--catalog-dir", cat_dir],
    )
    assert res_res.exit_code == 0
    assert "Resolved escalation" in res_res.output
    assert "deescalated_to=0" in res_res.output

    state = get_escalation_state(camp_id, catalog_dir=Path(cat_dir))
    assert state.current_level == 0


def test_cli_stepback_resolve_level3_refused_without_tty_or_token(cli_test_env, monkeypatch):
    """Level 3 resolution refused in non-interactive session without chair token."""
    camp_id = cli_test_env["campaign_id"]
    cat_dir = Path(cli_test_env["catalog_dir"])

    # Escalate to Level 3
    esc = EscalationRecord(
        escalation_id="esc_cli_level3",
        campaign_id=camp_id,
        project_slug="testproj",
        level=3,
        status="active",
        opened_at="2026-10-27T12:00:00Z",
        opened_by_kind="system",
        trigger_reason="Severe confound",
    )
    save_escalation_cool(esc, cat_dir)

    # Force stdin isatty() to False (simulating non-interactive agent / batch script)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    res = runner.invoke(
        app,
        ["stepback", "resolve", camp_id, "--deescalate-to", "0", "--catalog-dir", str(cat_dir)],
    )
    assert res.exit_code != 0
    assert "chair" in res.output.lower() or "refused" in res.output.lower() or "token" in res.output.lower()


def test_cli_stepback_resolve_level3_allowed_with_chair_token(cli_test_env, monkeypatch):
    """Level 3 resolution allowed when chair token is supplied in non-agent environment."""
    camp_id = cli_test_env["campaign_id"]
    cat_dir = Path(cli_test_env["catalog_dir"])
    tmp_path = cli_test_env["tmp_path"]

    esc = EscalationRecord(
        escalation_id="esc_cli_level3_tok",
        campaign_id=camp_id,
        project_slug="testproj",
        level=3,
        status="active",
        opened_at="2026-10-27T12:00:00Z",
        opened_by_kind="system",
        trigger_reason="Severe confound",
    )
    save_escalation_cool(esc, cat_dir)

    # Set up chair token file
    token_file = tmp_path / "chair_token"
    token_file.write_text("secret_chair_token_12345")
    monkeypatch.setattr("bathos.stepback.escalation.CHAIR_TOKEN_PATH", token_file)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    res = runner.invoke(
        app,
        [
            "stepback",
            "resolve",
            camp_id,
            "--deescalate-to",
            "0",
            "--chair-token",
            "secret_chair_token_12345",
            "--as-human-chair",
            "--catalog-dir",
            str(cat_dir),
        ],
    )
    assert res.exit_code == 0
    assert "Resolved escalation" in res.output

    state = get_escalation_state(camp_id, catalog_dir=cat_dir)
    assert state.current_level == 0


def test_cli_stepback_resolve_level3_refused_in_agent_environment(cli_test_env, monkeypatch):
    """Even with token or as_human_chair, agent environment variables trigger unconditional refusal."""
    camp_id = cli_test_env["campaign_id"]
    cat_dir = Path(cli_test_env["catalog_dir"])
    tmp_path = cli_test_env["tmp_path"]

    esc = EscalationRecord(
        escalation_id="esc_cli_level3_agent",
        campaign_id=camp_id,
        project_slug="testproj",
        level=3,
        status="active",
        opened_at="2026-10-27T12:00:00Z",
        opened_by_kind="system",
        trigger_reason="Severe confound",
    )
    save_escalation_cool(esc, cat_dir)

    token_file = tmp_path / "chair_token"
    token_file.write_text("secret_chair_token_12345")
    monkeypatch.setattr("bathos.stepback.escalation.CHAIR_TOKEN_PATH", token_file)

    # Export agent environment variable
    monkeypatch.setenv("BTH_AGENT_MODE", "1")

    res = runner.invoke(
        app,
        [
            "stepback",
            "resolve",
            camp_id,
            "--deescalate-to",
            "0",
            "--chair-token",
            "secret_chair_token_12345",
            "--as-human-chair",
            "--catalog-dir",
            str(cat_dir),
        ],
    )
    assert res.exit_code != 0
    assert "forbidden" in res.output.lower() or "agent" in res.output.lower()
