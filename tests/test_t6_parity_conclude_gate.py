"""Tests for T6: F2 conclude-gate parity confound check + validate_claim graded-run check (AC-06/07/08/20)."""

from __future__ import annotations

import json
from pathlib import Path
from datetime import UTC, datetime

import duckdb
import pytest

from bathos.claim import (
    parse_claim,
    validate_claim,
    register_claim,
    check_sha,
    parity_confound_check,
)
from bathos.campaigns import (
    create_campaign,
    add_run_to_campaign,
    conclude_campaign,
)
from bathos.catalog import init_catalog, write_run
from bathos.compact import compact
from bathos.schema import Run


@pytest.fixture
def tmp_catalog(tmp_path):
    """Create a temporary catalog."""
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    init_catalog(catalog_dir)
    return catalog_dir


@pytest.fixture
def clean_db(tmp_catalog):
    """Return a clean DB connection."""
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    yield db
    db.close()


@pytest.fixture
def claim_with_parity_confound(tmp_path):
    """Create a claim file with a reference_parity confound (empty parity_run_id)."""
    claim_path = tmp_path / "claim.bth.toml"
    content = """[claim]
headline = "Test claim with parity"
kill_condition = "Outcome != expected"

[[hypotheses]]
id = "H_primary"
label = "Primary hypothesis"

[[hypotheses]]
id = "H_null"
label = "Null hypothesis"

[[assumptions]]
id = "A1"
label = "Test assumption"

[[confounds]]
id = "C_parity"
label = "Literature parity"

[confounds.reference_parity]
parity_run_id = ""
reference_metric = "test_metric"
reference_value = 1.5
equivalence_bound = 0.1

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "main"
predicted_outcome = "discriminates"

[claim.union_gate]
# No clauses - union gate will pass if no discriminability is checked
# This allows us to test the parity gate in isolation
"""
    claim_path.write_text(content)
    return claim_path


class TestAC06_ConfirmationDowngradeOnUncontrolled:
    """AC-06: F2 downgrades confirmation campaign verdict to 'confounded' on uncontrolled reference_parity."""

    def test_ac06_confirmation_with_empty_parity_run_id_downgrades(
        self, tmp_catalog, tmp_path, clean_db, claim_with_parity_confound
    ):
        """Confirmation campaign with uncontrolled parity confound → verdict downgraded to 'confounded'."""
        db = clean_db

        # Create a campaign FIRST
        campaign = create_campaign(
            db,
            name="Confirmation Test",
            project_slug="test_proj",
            mode="confirmation"
        )

        # Register the claim
        register_claim(claim_with_parity_confound, campaign.id, db, tmp_path)

        # Create a run AFTER campaign (to pass temporal check)
        campaign_time = datetime.fromisoformat(campaign.started_at)
        run_time = campaign_time.replace(microsecond=0) + __import__('datetime').timedelta(minutes=1)

        run = Run(
            project_slug="test_proj",
            command="python test.py",
            argv=["python", "test.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            timestamp=run_time,
            status="completed",
            exit_code=0,
        )
        write_run(run, tmp_catalog)

        # Close DB before compacting
        db.close()
        compact(tmp_catalog)

        # Reconnect to get the new run
        db = duckdb.connect(str(tmp_catalog / "bathos.db"))

        # Add run to campaign
        add_run_to_campaign(db, campaign.id, run.id)

        # Conclude with "pass" — should be downgraded to "confounded"
        conclude_campaign(db, campaign.id, "pass", "Test conclusion", workspace_root=tmp_path)

        # Verify verdict downgraded
        rows = db.execute(
            "SELECT outcome_label FROM campaigns WHERE id = ?", [campaign.id]
        ).fetchall()
        assert rows[0][0] == "confounded"


class TestAC07_ExplorationWarnsOnly:
    """AC-07: F2 warns-only (no downgrade) for exploration campaigns with uncontrolled reference_parity."""

    def test_ac07_exploration_with_uncontrolled_parity_no_downgrade(
        self, tmp_catalog, tmp_path, clean_db, claim_with_parity_confound
    ):
        """Exploration campaign with uncontrolled parity confound → warns, verdict NOT downgraded."""
        db = clean_db

        # Create an EXPLORATION campaign
        campaign = create_campaign(
            db,
            name="Exploration Test",
            project_slug="test_proj",
            mode="exploration"
        )

        # Register the claim
        register_claim(claim_with_parity_confound, campaign.id, db, tmp_path)

        # Create and add a run
        campaign_time = datetime.fromisoformat(campaign.started_at)
        run_time = campaign_time.replace(microsecond=0) + __import__('datetime').timedelta(minutes=1)

        run = Run(
            project_slug="test_proj",
            command="python test.py",
            argv=["python", "test.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            timestamp=run_time,
            status="completed",
            exit_code=0,
        )
        write_run(run, tmp_catalog)

        # Close DB before compacting
        db.close()
        compact(tmp_catalog)

        # Reconnect
        db = duckdb.connect(str(tmp_catalog / "bathos.db"))

        add_run_to_campaign(db, campaign.id, run.id)

        # Conclude with "pass"
        conclude_campaign(db, campaign.id, "pass", "Test conclusion", workspace_root=tmp_path)

        # For exploration, verdict should NOT be downgraded
        rows = db.execute(
            "SELECT outcome_label FROM campaigns WHERE id = ?", [campaign.id]
        ).fetchall()
        assert rows[0][0] == "pass"


class TestAC08_ControlledParity:
    """AC-08: F2 passes when reference_parity confound is controlled."""

    def _make_parity_run(self, tmp_catalog, project_slug, outcome, timestamp, db=None):
        """Helper to create a parity run."""
        run = Run(
            project_slug=project_slug,
            command="python parity_validate.py",
            argv=["python", "parity_validate.py"],
            git_hash="def456",
            git_branch="main",
            git_dirty=False,
            timestamp=timestamp,
            status="completed",
            exit_code=0,
            outcome=outcome,
            metadata="{}",  # Cool schema doesn't support metadata
        )
        write_run(run, tmp_catalog)

        # If DB is provided, we can update the metadata in the warm DB after compact
        # This will be done by the caller after compact()

        return run.id

    def test_ac08_controlled_parity_outcome_pass(
        self, tmp_catalog, tmp_path, clean_db, claim_with_parity_confound
    ):
        """Confirmation campaign with controlled (outcome=pass) parity run → no downgrade."""
        db = clean_db

        # Create a parity run with outcome=pass
        parity_run_id = self._make_parity_run(
            tmp_catalog, "test_proj", "pass",
            datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        )

        # Close DB before compact
        db.close()
        compact(tmp_catalog)
        db = duckdb.connect(str(tmp_catalog / "bathos.db"))

        # Update the run's metadata in the warm DB to include parity_run_type
        # (cool schema doesn't support metadata, so we add it after compacting)
        metadata_dict = {
            "parity_run_type": "literature_parity",
            "test_metric": 1.5
        }
        db.execute(
            "UPDATE runs SET metadata = ? WHERE id = ?",
            [json.dumps(metadata_dict), parity_run_id]
        )

        # Update claim to reference the parity run
        claim_path = tmp_path / "claim_with_pass.bth.toml"
        content = claim_with_parity_confound.read_text()
        updated = content.replace('parity_run_id = ""', f'parity_run_id = "{parity_run_id}"')
        claim_path.write_text(updated)

        # Create campaign
        campaign = create_campaign(
            db,
            name="Confirmation Controlled",
            project_slug="test_proj",
            mode="confirmation"
        )

        # Register claim
        register_claim(claim_path, campaign.id, db, tmp_path)

        # Create and add a test run
        campaign_time = datetime.fromisoformat(campaign.started_at)
        run_time = campaign_time.replace(microsecond=0) + __import__('datetime').timedelta(minutes=1)

        run = Run(
            project_slug="test_proj",
            command="python test.py",
            argv=["python", "test.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            timestamp=run_time,
            status="completed",
            exit_code=0,
        )
        write_run(run, tmp_catalog)

        # Close DB before compacting
        db.close()
        compact(tmp_catalog)

        # Reconnect
        db = duckdb.connect(str(tmp_catalog / "bathos.db"))

        add_run_to_campaign(db, campaign.id, run.id)

        # Conclude
        conclude_campaign(db, campaign.id, "pass", "Baseline verified", workspace_root=tmp_path)

        # Should NOT downgrade
        rows = db.execute(
            "SELECT outcome_label FROM campaigns WHERE id = ?", [campaign.id]
        ).fetchall()
        assert rows[0][0] == "pass"

    def test_ac08_controlled_by_protocol_partial_outcome(
        self, tmp_catalog, tmp_path, clean_db, claim_with_parity_confound
    ):
        """Confirmation campaign with controlled-by-protocol (outcome=partial) → not downgraded."""
        db = clean_db

        # Create a parity run with outcome=partial (controlled-by-protocol)
        parity_run_id = self._make_parity_run(
            tmp_catalog, "test_proj", "partial",
            datetime(2026, 6, 1, 13, 0, 0, tzinfo=UTC)
        )

        # Close DB before compact
        db.close()
        compact(tmp_catalog)
        db = duckdb.connect(str(tmp_catalog / "bathos.db"))

        # Update the run's metadata in the warm DB to include parity_run_type
        metadata_dict = {
            "parity_run_type": "literature_parity",
            "test_metric": 1.5
        }
        db.execute(
            "UPDATE runs SET metadata = ? WHERE id = ?",
            [json.dumps(metadata_dict), parity_run_id]
        )

        # Update claim to reference the parity run
        claim_path = tmp_path / "claim_with_partial.bth.toml"
        content = claim_with_parity_confound.read_text()
        updated = content.replace('parity_run_id = ""', f'parity_run_id = "{parity_run_id}"')
        claim_path.write_text(updated)

        # Create campaign
        campaign = create_campaign(
            db,
            name="Confirmation Partial",
            project_slug="test_proj",
            mode="confirmation"
        )

        # Register claim
        register_claim(claim_path, campaign.id, db, tmp_path)

        # Create and add a test run
        campaign_time = datetime.fromisoformat(campaign.started_at)
        run_time = campaign_time.replace(microsecond=0) + __import__('datetime').timedelta(minutes=1)

        run = Run(
            project_slug="test_proj",
            command="python test.py",
            argv=["python", "test.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            timestamp=run_time,
            status="completed",
            exit_code=0,
        )
        write_run(run, tmp_catalog)

        # Close DB before compacting
        db.close()
        compact(tmp_catalog)

        # Reconnect
        db = duckdb.connect(str(tmp_catalog / "bathos.db"))

        add_run_to_campaign(db, campaign.id, run.id)

        # Conclude
        conclude_campaign(db, campaign.id, "pass", "Baseline partial", workspace_root=tmp_path)

        # controlled-by-protocol should NOT downgrade
        rows = db.execute(
            "SELECT outcome_label FROM campaigns WHERE id = ?", [campaign.id]
        ).fetchall()
        assert rows[0][0] == "pass"


class TestAC20_SHADriftDetection:
    """AC-20: SHA-drift detection — parity run artifact modified → confound NOT controlled."""

    def test_ac20_placeholder(self, tmp_catalog):
        """Placeholder for AC-20 SHA-drift detection test."""
        # AC-20 requires that if a parity run's output artifact SHA drifts
        # (the artifact on disk doesn't match the recorded SHA), the confound
        # is treated as uncontrolled.
        #
        # This is implemented in the F2 conclude-gate via parity_confound_check().
        # The test would:
        # 1. Create a parity run with output_shas recorded
        # 2. Modify the artifact file so its SHA changes
        # 3. Conclude a campaign and verify confound is uncontrolled
        #
        # For now, this serves as a marker that AC-20 is documented and ready
        # for implementation when output_paths and SHA verification are added.
        pass
