"""Tests for T6: F2 conclude-gate parity confound check + validate_claim graded-run check (AC-06/07/08/20)."""

from __future__ import annotations

import json
import datetime as dt
from pathlib import Path
from datetime import UTC, datetime
from unittest.mock import patch

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
        self, tmp_catalog, tmp_path, clean_db, claim_with_parity_confound, capsys
    ):
        """Exploration campaign with uncontrolled parity confound → warns, verdict NOT downgraded.

        HARDENED: asserts that the advisory WARNING was actually emitted (gate code path executed),
        not just that no downgrade occurred.
        """
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
        run_time = campaign_time.replace(microsecond=0) + dt.timedelta(minutes=1)

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

        # HARDENED: verify the advisory WARNING was actually emitted — proves the gate ran,
        # not just that the default no-downgrade outcome was returned.
        captured = capsys.readouterr()
        combined_output = captured.out + captured.err
        assert "WARNING" in combined_output or "uncontrolled" in combined_output, (
            "Expected advisory WARNING about uncontrolled parity confound in output, "
            f"but got: {combined_output!r}"
        )


class TestAC08_ControlledParity:
    """AC-08: F2 passes when reference_parity confound is controlled.

    Uses the REAL column path: write a Run with parity_run_type set in the cool schema,
    compact() to warm, then query via parity_confound_check() — no metadata UPDATE hacks.
    """

    def _make_parity_run_with_column(self, tmp_catalog, project_slug, outcome, timestamp):
        """Create a parity run with parity_run_type set via the cool schema column (real path)."""
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
            parity_run_type="literature_parity",  # real column, not metadata JSON
        )
        write_run(run, tmp_catalog)
        return run.id

    def test_ac08_controlled_parity_outcome_pass(
        self, tmp_catalog, tmp_path, clean_db, claim_with_parity_confound
    ):
        """Confirmation campaign with controlled (outcome=pass) parity run → no downgrade.

        HARDENED: uses real cool→warm column path (no UPDATE metadata hack).
        Also asserts parity_confound_check returned status='controlled'.
        """
        db = clean_db

        # Create a parity run with outcome=pass via the real column path
        parity_run_id = self._make_parity_run_with_column(
            tmp_catalog, "test_proj", "pass",
            datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        )

        # Close DB before compact
        db.close()
        compact(tmp_catalog)
        db = duckdb.connect(str(tmp_catalog / "bathos.db"))

        # HARDENED: verify parity_confound_check returns 'controlled' via the column
        # (not merely that the campaign doesn't downgrade)
        claim_with_run_id = tmp_path / "claim_with_pass.bth.toml"
        content = claim_with_parity_confound.read_text()
        updated = content.replace('parity_run_id = ""', f'parity_run_id = "{parity_run_id}"')
        claim_with_run_id.write_text(updated)

        parity_result = parity_confound_check(claim_with_run_id, db)
        parity_confounds = parity_result.get("confounds", [])
        controlled_confounds = [c for c in parity_confounds if c["status"] == "controlled"]
        assert controlled_confounds, (
            f"Expected parity_confound_check to return status='controlled' for a pass run "
            f"with parity_run_type='literature_parity' set via the column path, "
            f"but got: {parity_confounds}"
        )

        # Create campaign
        campaign = create_campaign(
            db,
            name="Confirmation Controlled",
            project_slug="test_proj",
            mode="confirmation"
        )

        # Register claim
        register_claim(claim_with_run_id, campaign.id, db, tmp_path)

        # Create and add a test run
        campaign_time = datetime.fromisoformat(campaign.started_at)
        run_time = campaign_time.replace(microsecond=0) + dt.timedelta(minutes=1)

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
        """Confirmation campaign with controlled-by-protocol (outcome=partial) → not downgraded.

        HARDENED: uses real cool→warm column path.
        Also asserts parity_confound_check returned status='controlled-by-protocol'.
        """
        db = clean_db

        # Create a parity run with outcome=partial (controlled-by-protocol) via real column path
        parity_run_id = self._make_parity_run_with_column(
            tmp_catalog, "test_proj", "partial",
            datetime(2026, 6, 1, 13, 0, 0, tzinfo=UTC)
        )

        # Close DB before compact
        db.close()
        compact(tmp_catalog)
        db = duckdb.connect(str(tmp_catalog / "bathos.db"))

        # HARDENED: verify parity_confound_check returns 'controlled-by-protocol' via the column
        claim_with_run_id = tmp_path / "claim_with_partial.bth.toml"
        content = claim_with_parity_confound.read_text()
        updated = content.replace('parity_run_id = ""', f'parity_run_id = "{parity_run_id}"')
        claim_with_run_id.write_text(updated)

        parity_result = parity_confound_check(claim_with_run_id, db)
        parity_confounds = parity_result.get("confounds", [])
        protocol_confounds = [c for c in parity_confounds if c["status"] == "controlled-by-protocol"]
        assert protocol_confounds, (
            f"Expected parity_confound_check to return status='controlled-by-protocol' for a "
            f"partial run with parity_run_type='literature_parity' via column path, "
            f"but got: {parity_confounds}"
        )

        # Create campaign
        campaign = create_campaign(
            db,
            name="Confirmation Partial",
            project_slug="test_proj",
            mode="confirmation"
        )

        # Register claim
        register_claim(claim_with_run_id, campaign.id, db, tmp_path)

        # Create and add a test run
        campaign_time = datetime.fromisoformat(campaign.started_at)
        run_time = campaign_time.replace(microsecond=0) + dt.timedelta(minutes=1)

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


class TestStep5_ValidateClaimGradedPath:
    """Step 5: validate_claim graded-parity-run check alongside the legacy equivalence path (F-1).

    When a reference_parity confound carries a parity_run_id, validate_claim should check
    the run's parity_run_type column and outcome to determine if the confound is controlled.
    This is the graded path — it fires BESIDE the legacy equivalence-bound path.
    """

    @pytest.fixture
    def claim_with_graded_parity(self, tmp_path):
        """Claim with a reference_parity confound using parity_run_id (graded path, no equivalence_bound)."""
        claim_path = tmp_path / "graded_claim.bth.toml"
        content = """[claim]
headline = "Claim with graded parity"
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
label = "Reference parity"

[confounds.reference_parity]
parity_run_id = "PLACEHOLDER"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "main"
predicted_outcome = "discriminates"

[claim.union_gate]
"""
        claim_path.write_text(content)
        return claim_path

    def test_step5_validate_claim_graded_pass(self, tmp_path, tmp_catalog, claim_with_graded_parity):
        """validate_claim marks confound controlled when parity_run_type='literature_parity' + outcome='pass'."""
        # Write a parity run with the column set via the real path
        run = Run(
            project_slug="test_proj",
            command="python parity_validate.py",
            argv=["python", "parity_validate.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            timestamp=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
            status="completed",
            exit_code=0,
            outcome="pass",
            parity_run_type="literature_parity",
        )
        write_run(run, tmp_catalog)
        compact(tmp_catalog)

        # Update claim to reference this run
        content = claim_with_graded_parity.read_text().replace("PLACEHOLDER", run.id)
        claim_path = tmp_path / "graded_claim_with_id.bth.toml"
        claim_path.write_text(content)

        db = duckdb.connect(str(tmp_catalog / "bathos.db"))

        claim_file = parse_claim(claim_path)
        result = validate_claim(claim_file, db=db)

        # The graded path should produce no errors (confound is controlled).
        # Check ALL errors — with the graded path, a literature_parity run with outcome='pass'
        # must be accepted. Without the graded path, the legacy path fires and produces errors
        # like "parity_metric key '' not found in baseline run metadata".
        assert result.ok, (
            f"Expected validate_claim to PASS (ok=True) for a claim whose parity confound "
            f"references a passing literature_parity run (graded path), "
            f"but got errors: {result.errors}"
        )

        db.close()

    def test_step5_validate_claim_graded_partial_controlled_by_protocol(
        self, tmp_path, tmp_catalog, claim_with_graded_parity
    ):
        """validate_claim marks confound controlled-by-protocol (no error) when parity_run_type='literature_parity' + outcome='partial'."""
        run = Run(
            project_slug="test_proj",
            command="python parity_validate.py",
            argv=["python", "parity_validate.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            timestamp=datetime(2026, 6, 1, 13, 0, 0, tzinfo=UTC),
            status="completed",
            exit_code=0,
            outcome="partial",
            parity_run_type="literature_parity",
        )
        write_run(run, tmp_catalog)
        compact(tmp_catalog)

        content = claim_with_graded_parity.read_text().replace("PLACEHOLDER", run.id)
        claim_path = tmp_path / "graded_claim_partial.bth.toml"
        claim_path.write_text(content)

        db = duckdb.connect(str(tmp_catalog / "bathos.db"))
        claim_file = parse_claim(claim_path)
        result = validate_claim(claim_file, db=db)

        # controlled-by-protocol should produce no errors (partial = controlled-by-protocol = accepted)
        assert result.ok, (
            f"Expected validate_claim to PASS (ok=True) for partial literature_parity run "
            f"(controlled-by-protocol), but got errors: {result.errors}"
        )
        db.close()

    def test_step5_validate_claim_graded_fail_when_run_not_parity_type(
        self, tmp_path, tmp_catalog, claim_with_graded_parity
    ):
        """validate_claim emits error when referenced run is not a literature_parity run (column is NULL)."""
        # Run with no parity_run_type set
        run = Run(
            project_slug="test_proj",
            command="python regular_run.py",
            argv=["python", "regular_run.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            timestamp=datetime(2026, 6, 1, 14, 0, 0, tzinfo=UTC),
            status="completed",
            exit_code=0,
            outcome="pass",
            parity_run_type=None,  # not a parity run
        )
        write_run(run, tmp_catalog)
        compact(tmp_catalog)

        content = claim_with_graded_parity.read_text().replace("PLACEHOLDER", run.id)
        claim_path = tmp_path / "graded_claim_non_parity.bth.toml"
        claim_path.write_text(content)

        db = duckdb.connect(str(tmp_catalog / "bathos.db"))
        claim_file = parse_claim(claim_path)
        result = validate_claim(claim_file, db=db)

        # Should produce an error because the run is not a literature_parity run
        assert result.errors, (
            "Expected validation error when referenced run has parity_run_type=None, "
            f"but got no errors. infos={result.infos}"
        )
        db.close()


class TestF2CoolToWarmRoundTrip:
    """F2 gate: parity_run_type column survives the cool→warm round trip.

    Verifies that parity_confound_check correctly classifies a run as 'controlled'
    when parity_run_type='literature_parity' is written into the cool schema,
    then compacted to warm — without any UPDATE metadata hacks.
    """

    def test_f2_controlled_via_real_column_roundtrip(self, tmp_path, tmp_catalog):
        """Write cool fragment with column, compact(), verify parity_confound_check → 'controlled'."""
        # Write a parity run using the real column path
        run = Run(
            project_slug="test_proj",
            command="python parity_validate.py",
            argv=["python", "parity_validate.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            timestamp=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
            status="completed",
            exit_code=0,
            outcome="pass",
            parity_run_type="literature_parity",
        )
        write_run(run, tmp_catalog)
        compact(tmp_catalog)

        # Build a claim referencing this run
        claim_path = tmp_path / "claim.bth.toml"
        claim_path.write_text(f"""[claim]
headline = "Test claim"
kill_condition = "fails"

[[hypotheses]]
id = "H1"
label = "H1"

[[hypotheses]]
id = "H0"
label = "H0"

[[assumptions]]
id = "A1"
label = "A1"

[[confounds]]
id = "C1"
label = "Parity confound"

[confounds.reference_parity]
parity_run_id = "{run.id}"

[[claim.discriminability]]
hypothesis_a = "H1"
hypothesis_b = "H0"
planned_run_label = "main"
predicted_outcome = "discriminates"

[claim.union_gate]
""")

        db = duckdb.connect(str(tmp_catalog / "bathos.db"))

        # Verify the column survived compaction
        row = db.execute(
            "SELECT parity_run_type, outcome FROM runs WHERE id = ?", [run.id]
        ).fetchone()
        assert row is not None, "Run not found in warm DB after compact"
        assert row[0] == "literature_parity", (
            f"parity_run_type column not preserved through cool→warm compaction; got: {row[0]!r}"
        )
        assert row[1] == "pass"

        # Now verify parity_confound_check reads the column and returns 'controlled'
        result = parity_confound_check(claim_path, db)
        confounds = result.get("confounds", [])
        assert confounds, "parity_confound_check returned no confounds"
        assert confounds[0]["status"] == "controlled", (
            f"Expected 'controlled' but got {confounds[0]['status']!r}. "
            f"Gate must use parity_run_type column (not metadata JSON)."
        )
        db.close()
