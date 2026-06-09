"""Integration tests for campaign_report emission (CAMPAIGN-REPORT-EMIT).

Tests the emit_campaign_report function that writes the campaign report JSON sidecar
at <catalog>/sidecars/<campaign_id>/campaign_report.json.
"""
import json
import tempfile
from pathlib import Path

import duckdb
import pytest

from bathos.campaigns import create_campaign, add_run_to_campaign, emit_campaign_report
from bathos.schema import COOL_SCHEMA, Run
from bathos.campaign_report import CampaignReport


@pytest.fixture
def temp_catalog():
    """Create a temporary catalog with DuckDB database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        catalog_dir = Path(tmpdir)
        db_path = catalog_dir / "bathos.db"

        # Create DuckDB connection and initialize schema
        db = duckdb.connect(str(db_path))
        db.execute("""
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
                stopping_threshold FLOAT
            )
        """)

        db.execute("""
            CREATE TABLE runs (
                id TEXT PRIMARY KEY,
                project_slug TEXT,
                command TEXT,
                argv TEXT,
                git_hash TEXT,
                git_branch TEXT,
                git_dirty BOOLEAN,
                timestamp TIMESTAMP,
                duration_s FLOAT,
                exit_code INTEGER,
                status TEXT,
                output_paths TEXT,
                tags TEXT,
                schema_version TEXT,
                slurm_job_id TEXT,
                slurm_array_task_id TEXT,
                hostname TEXT,
                metadata TEXT,
                output_metadata TEXT,
                outcome TEXT,
                sidecar_sha256 TEXT,
                sidecar_path TEXT,
                parent_run_id TEXT,
                agent_mode TEXT,
                sidecar_mode TEXT,
                outcome_is_residual BOOLEAN,
                skill_sha256 TEXT,
                campaign_id TEXT,
                script_sha256 TEXT,
                postmortem_status TEXT,
                postmortem_override TEXT,
                postmortem_verdict_override TEXT,
                postmortem_author TEXT,
                postmortem_path TEXT,
                postmortem_hypothesis_status TEXT,
                postmortem_has_anomalies BOOLEAN,
                postmortem_summary TEXT,
                postmortem_asset_links TEXT,
                manifest_sha256 TEXT,
                manifest_path TEXT,
                outcome_error_reason TEXT,
                adversarial_check_status TEXT,
                stage_name TEXT
            )
        """)

        db.execute("""
            CREATE TABLE campaign_runs (
                campaign_id TEXT,
                run_id TEXT,
                evalue FLOAT,
                seq_position INTEGER,
                PRIMARY KEY (campaign_id, run_id)
            )
        """)

        yield catalog_dir, db

        db.close()


class TestEmitCampaignReport:
    """Verify emit_campaign_report generates correct sidecar files."""

    def test_emit_campaign_report_basic(self, temp_catalog):
        """Given a campaign with runs, emit_campaign_report writes a valid JSON sidecar."""
        catalog_dir, db = temp_catalog

        # Create campaign
        campaign = create_campaign(
            db,
            name="test_campaign",
            project_slug="test_project",
            mode="exploration",
            question="Does X work?",
        )

        # Create and add a run
        run = Run(
            project_slug="test_project",
            command="python script.py",
            argv=["python", "script.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
        )
        run.outcome = "success"
        run.outcome_is_residual = False
        run.sidecar_mode = "normal"
        run.stage_name = "exploration"

        db.execute("""
            INSERT INTO runs (
                id, project_slug, command, argv, git_hash, git_branch, git_dirty, timestamp,
                outcome, outcome_is_residual, sidecar_mode, stage_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            run.id, run.project_slug, run.command, json.dumps(run.argv),
            run.git_hash, run.git_branch, run.git_dirty, run.timestamp,
            run.outcome, run.outcome_is_residual, run.sidecar_mode, run.stage_name
        ])

        # Add run to campaign
        add_run_to_campaign(db, campaign.id, run.id)

        # Set conclusion and emit report
        db.execute(
            "UPDATE campaigns SET conclusion = ?, status = 'concluded' WHERE id = ?",
            ["Test campaign completed successfully.", campaign.id]
        )

        emit_campaign_report(db, str(catalog_dir), campaign.id)

        # Verify report was written
        report_path = Path(catalog_dir) / "sidecars" / campaign.id / "campaign_report.json"
        assert report_path.exists(), f"Report not found at {report_path}"

        # Read and validate report
        report = CampaignReport.read_report(report_path)
        assert report.campaign_id == campaign.id
        assert report.total_runs == 1
        assert report.outcome_distribution == {"success": 1}
        assert report.stage_breakdown == {"exploration": 1}
        assert report.conclude == "Test campaign completed successfully."

    def test_emit_campaign_report_with_stage_breakdown(self, temp_catalog):
        """Given a campaign with multiple stages, stage_breakdown is correct."""
        catalog_dir, db = temp_catalog

        campaign = create_campaign(
            db,
            name="multi_stage",
            project_slug="test_project",
            mode="exploration",
        )

        # Create 3 runs with different stages
        for i, stage in enumerate(["exploration", "exploration", "validation"]):
            run = Run(
                project_slug="test_project",
                command="python script.py",
                argv=["python", "script.py"],
                git_hash=f"hash{i}",
                git_branch="main",
                git_dirty=False,
            )
            run.outcome = "success"
            run.outcome_is_residual = False
            run.sidecar_mode = "normal"
            run.stage_name = stage

            db.execute("""
                INSERT INTO runs (
                    id, project_slug, command, argv, git_hash, git_branch, git_dirty, timestamp,
                    outcome, outcome_is_residual, sidecar_mode, stage_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                run.id, run.project_slug, run.command, json.dumps(run.argv),
                run.git_hash, run.git_branch, run.git_dirty, run.timestamp,
                run.outcome, run.outcome_is_residual, run.sidecar_mode, run.stage_name
            ])

            add_run_to_campaign(db, campaign.id, run.id)

        db.execute("UPDATE campaigns SET status = 'concluded' WHERE id = ?", [campaign.id])
        emit_campaign_report(db, str(catalog_dir), campaign.id)

        # Verify stage breakdown
        report_path = Path(catalog_dir) / "sidecars" / campaign.id / "campaign_report.json"
        report = CampaignReport.read_report(report_path)
        assert report.stage_breakdown == {"exploration": 2, "validation": 1}

    def test_emit_campaign_report_with_null_stage(self, temp_catalog):
        """Given runs with no stage_name, null bucket captures them."""
        catalog_dir, db = temp_catalog

        campaign = create_campaign(
            db,
            name="null_stage",
            project_slug="test_project",
            mode="exploration",
        )

        # Create 2 runs: one with stage_name, one without
        for i, stage in enumerate([None, "exploration"]):
            run = Run(
                project_slug="test_project",
                command="python script.py",
                argv=["python", "script.py"],
                git_hash=f"hash{i}",
                git_branch="main",
                git_dirty=False,
            )
            run.outcome = "success"
            run.outcome_is_residual = False
            run.sidecar_mode = "normal"
            run.stage_name = stage

            db.execute("""
                INSERT INTO runs (
                    id, project_slug, command, argv, git_hash, git_branch, git_dirty, timestamp,
                    outcome, outcome_is_residual, sidecar_mode, stage_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                run.id, run.project_slug, run.command, json.dumps(run.argv),
                run.git_hash, run.git_branch, run.git_dirty, run.timestamp,
                run.outcome, run.outcome_is_residual, run.sidecar_mode, run.stage_name
            ])

            add_run_to_campaign(db, campaign.id, run.id)

        db.execute("UPDATE campaigns SET status = 'concluded' WHERE id = ?", [campaign.id])
        emit_campaign_report(db, str(catalog_dir), campaign.id)

        # Verify null stage is captured. Note: pydantic serializes None key as string "None"
        report_path = Path(catalog_dir) / "sidecars" / campaign.id / "campaign_report.json"
        report = CampaignReport.read_report(report_path)
        # After JSON roundtrip, None becomes "None" string
        assert report.stage_breakdown.get("None") == 1 or report.stage_breakdown.get(None) == 1
        assert report.stage_breakdown.get("exploration") == 1

    def test_emit_campaign_report_with_figure_manifest_ref(self, temp_catalog):
        """Given figure_manifest_ref, it is included in the report."""
        catalog_dir, db = temp_catalog

        campaign = create_campaign(
            db,
            name="with_manifest",
            project_slug="test_project",
            mode="exploration",
        )

        run = Run(
            project_slug="test_project",
            command="python script.py",
            argv=["python", "script.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
        )
        run.outcome = "success"
        run.outcome_is_residual = False
        run.sidecar_mode = "normal"

        db.execute("""
            INSERT INTO runs (
                id, project_slug, command, argv, git_hash, git_branch, git_dirty, timestamp,
                outcome, outcome_is_residual, sidecar_mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            run.id, run.project_slug, run.command, json.dumps(run.argv),
            run.git_hash, run.git_branch, run.git_dirty, run.timestamp,
            run.outcome, run.outcome_is_residual, run.sidecar_mode
        ])

        add_run_to_campaign(db, campaign.id, run.id)

        manifest_ref = f"sidecars/{campaign.id}/figure_manifest.json"
        emit_campaign_report(db, str(catalog_dir), campaign.id, figure_manifest_ref=manifest_ref)

        report_path = Path(catalog_dir) / "sidecars" / campaign.id / "campaign_report.json"
        report = CampaignReport.read_report(report_path)
        assert report.figure_manifest_ref == manifest_ref

    def test_emit_campaign_report_zero_runs_error(self, temp_catalog):
        """Given a campaign with zero runs, emit_campaign_report raises an error."""
        catalog_dir, db = temp_catalog

        campaign = create_campaign(
            db,
            name="empty_campaign",
            project_slug="test_project",
            mode="exploration",
        )

        from bathos.campaigns import CampaignError
        with pytest.raises(CampaignError, match="not found or has no runs"):
            emit_campaign_report(db, str(catalog_dir), campaign.id)

    def test_emit_campaign_report_missing_campaign_error(self, temp_catalog):
        """Given a nonexistent campaign, emit_campaign_report raises an error."""
        catalog_dir, db = temp_catalog

        from bathos.campaigns import CampaignError
        with pytest.raises(CampaignError, match="not found"):
            emit_campaign_report(db, str(catalog_dir), "nonexistent_campaign_id")
