"""Tests for attest_parity() and parity_confound_check() — T5 implementation."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from datetime import UTC, datetime

import duckdb
import pytest

from bathos.claim import (
    parse_claim,
    attest_parity,
    parity_confound_check,
    check_sha,
)


@pytest.fixture
def temp_db(tmp_path):
    """Create a temporary DuckDB database with campaigns and runs tables."""
    db_path = tmp_path / "test.db"
    db = duckdb.connect(str(db_path))

    # Create campaigns table
    db.execute("""
        CREATE TABLE campaigns (
            id TEXT PRIMARY KEY,
            project_slug TEXT NOT NULL,
            name TEXT NOT NULL,
            mode TEXT NOT NULL,
            question TEXT,
            hypothesis TEXT,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            concluded_at TEXT,
            conclusion TEXT,
            outcome_label TEXT,
            parent_campaign_id TEXT,
            stopping_threshold REAL,
            claim_path TEXT,
            claim_sha256 TEXT,
            claim_mode TEXT
        )
    """)

    # Create runs table with metadata column for parity_run_type
    db.execute("""
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            campaign_id TEXT,
            claim_discriminates TEXT,
            outcome TEXT,
            metadata TEXT
        )
    """)

    db.commit()
    yield db
    db.close()


@pytest.fixture
def temp_claim_file_with_parity(tmp_path):
    """Create a claim.bth.toml file with a reference_parity confound."""
    claim_path = tmp_path / "test_parity.claim.toml"
    content = """[claim]
headline = "Test claim with parity baseline"
kill_condition = "Outcome != expected"
regime = "standard"

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
label = "Literature parity confound"
[confounds.reference_parity]
reference_paper = "Example 2026"
reference_metric = "metric_key"
reference_value = 1.0
equivalence_bound = 0.05
parity_run_id = ""

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "main"
predicted_outcome = "discriminates"

[claim.union_gate]
[[claim.union_gate.clauses]]
id = "C_main"
description = "Main clause"
hypothesis_ids = ["H_primary", "H_null"]
"""
    claim_path.write_text(content)
    return claim_path


class TestAttestParityBasic:
    """Tests for attest_parity() basic functionality and AC-11."""

    def test_ac11_attest_parity_binds_passing_parity_run(
        self, temp_db, tmp_path, temp_claim_file_with_parity
    ):
        """
        AC-11: attest_parity binds a valid passing PARITY run's id into the claim
        and the claim SHA is re-anchored consistently (file SHA == DB SHA afterward).
        """
        campaign_id = "test_campaign"
        campaign_name = "test_campaign"

        # Create campaign
        temp_db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at, claim_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [campaign_id, "test", campaign_name, "confirmation", "open",
             datetime.now(UTC).isoformat(), str(Path("test_parity.claim.toml"))]
        )

        # Create a passing PARITY run with proper metadata
        parity_run_id = "run_parity_001"
        parity_metadata = json.dumps({
            "metric_key": 1.0,
            "parity_run_type": "literature_parity"
        })
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, outcome, metadata) VALUES (?, ?, ?, ?)",
            [parity_run_id, campaign_id, "pass", parity_metadata]
        )

        # Register the initial claim (empty parity_run_id)
        from bathos.claim import register_claim
        register_claim(
            Path("test_parity.claim.toml"),
            campaign_id,
            temp_db,
            tmp_path,
            force=False
        )

        temp_db.commit()

        # Get the initial claim SHA from DB
        initial_row = temp_db.execute(
            "SELECT claim_sha256 FROM campaigns WHERE id = ?", [campaign_id]
        ).fetchone()
        initial_sha = initial_row[0]

        # Now call attest_parity to bind the parity run
        attest_parity(
            campaign_id=campaign_id,
            parity_run_id=parity_run_id,
            db=temp_db,
            workspace_root=tmp_path
        )

        # Verify that the claim file has been updated with parity_run_id
        claim = parse_claim(tmp_path / "test_parity.claim.toml")
        parity_confound = claim.confounds[0]
        ref_parity = parity_confound.get("reference_parity", {})
        assert ref_parity.get("parity_run_id") == parity_run_id, \
            "parity_run_id should be bound in the claim file"

        # Verify that the SHA has been re-anchored
        new_sha = claim.sha256
        assert new_sha != initial_sha, "SHA should change after binding parity_run_id"

        # Verify that the DB SHA matches the file SHA
        db_row = temp_db.execute(
            "SELECT claim_sha256 FROM campaigns WHERE id = ?", [campaign_id]
        ).fetchone()
        db_sha = db_row[0]
        assert db_sha == new_sha, "DB SHA should match file SHA after attest_parity"

        # Verify check_sha does NOT raise
        check_sha(
            path_relative="test_parity.claim.toml",
            registered_sha=db_sha,
            workspace_root=tmp_path
        )


class TestAttestParityValidation:
    """Tests for attest_parity() validation (AC-12, AC-13)."""

    def test_ac12_attest_parity_rejects_run_missing_parity_run_type(
        self, temp_db, tmp_path, temp_claim_file_with_parity
    ):
        """
        AC-12: attest_parity REJECTS a run whose metadata lacks parity_run_type
        (or it's not 'literature_parity').
        """
        campaign_id = "test_campaign"
        campaign_name = "test_campaign"

        # Create campaign
        temp_db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at, claim_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [campaign_id, "test", campaign_name, "confirmation", "open",
             datetime.now(UTC).isoformat(), str(Path("test_parity.claim.toml"))]
        )

        # Create a passing run WITHOUT parity_run_type in metadata
        bad_run_id = "run_bad_001"
        bad_metadata = json.dumps({"metric_key": 1.0})  # No parity_run_type
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, outcome, metadata) VALUES (?, ?, ?, ?)",
            [bad_run_id, campaign_id, "pass", bad_metadata]
        )

        # Register the initial claim
        from bathos.claim import register_claim
        register_claim(
            Path("test_parity.claim.toml"),
            campaign_id,
            temp_db,
            tmp_path,
            force=False
        )

        temp_db.commit()

        # Attempt to attest with the bad run — should raise ValueError
        with pytest.raises(ValueError, match="parity_run_type"):
            attest_parity(
                campaign_id=campaign_id,
                parity_run_id=bad_run_id,
                db=temp_db,
                workspace_root=tmp_path
            )

        # Verify claim file is UNCHANGED
        claim = parse_claim(tmp_path / "test_parity.claim.toml")
        ref_parity = claim.confounds[0].get("reference_parity", {})
        assert ref_parity.get("parity_run_id") == "", \
            "parity_run_id should remain empty after failed attest_parity"

    def test_ac12_attest_parity_rejects_run_with_wrong_parity_type(
        self, temp_db, tmp_path, temp_claim_file_with_parity
    ):
        """
        AC-12: attest_parity REJECTS a run where parity_run_type is not 'literature_parity'.
        """
        campaign_id = "test_campaign"
        campaign_name = "test_campaign"

        # Create campaign
        temp_db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at, claim_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [campaign_id, "test", campaign_name, "confirmation", "open",
             datetime.now(UTC).isoformat(), str(Path("test_parity.claim.toml"))]
        )

        # Create a run with WRONG parity_run_type
        bad_run_id = "run_bad_002"
        bad_metadata = json.dumps({
            "metric_key": 1.0,
            "parity_run_type": "partial_parity"  # Wrong type
        })
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, outcome, metadata) VALUES (?, ?, ?, ?)",
            [bad_run_id, campaign_id, "pass", bad_metadata]
        )

        # Register the initial claim
        from bathos.claim import register_claim
        register_claim(
            Path("test_parity.claim.toml"),
            campaign_id,
            temp_db,
            tmp_path,
            force=False
        )

        temp_db.commit()

        # Attempt to attest with wrong parity type — should raise ValueError
        with pytest.raises(ValueError, match="literature_parity"):
            attest_parity(
                campaign_id=campaign_id,
                parity_run_id=bad_run_id,
                db=temp_db,
                workspace_root=tmp_path
            )

    def test_ac13_attest_parity_partial_run_sets_controlled_by_protocol(
        self, temp_db, tmp_path, temp_claim_file_with_parity
    ):
        """
        AC-13: attest_parity on a PARTIAL parity run sets the confound status
        to 'controlled-by-protocol' (inferred from run metadata).
        """
        campaign_id = "test_campaign"
        campaign_name = "test_campaign"

        # Create campaign
        temp_db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at, claim_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [campaign_id, "test", campaign_name, "confirmation", "open",
             datetime.now(UTC).isoformat(), str(Path("test_parity.claim.toml"))]
        )

        # Create a PARTIAL parity run (outcome='partial')
        partial_run_id = "run_partial_001"
        partial_metadata = json.dumps({
            "metric_key": 1.0,
            "parity_run_type": "literature_parity"
        })
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, outcome, metadata) VALUES (?, ?, ?, ?)",
            [partial_run_id, campaign_id, "partial", partial_metadata]
        )

        # Register the initial claim
        from bathos.claim import register_claim
        register_claim(
            Path("test_parity.claim.toml"),
            campaign_id,
            temp_db,
            tmp_path,
            force=False
        )

        temp_db.commit()

        # Attest with the PARTIAL run
        attest_parity(
            campaign_id=campaign_id,
            parity_run_id=partial_run_id,
            db=temp_db,
            workspace_root=tmp_path
        )

        # Verify the binding is recorded (status will be inferred from run)
        claim = parse_claim(tmp_path / "test_parity.claim.toml")
        ref_parity = claim.confounds[0].get("reference_parity", {})
        assert ref_parity.get("parity_run_id") == partial_run_id, \
            "parity_run_id should be bound for PARTIAL runs"

        # Now check via parity_confound_check that status is inferred as controlled-by-protocol
        check_result = parity_confound_check(tmp_path / "test_parity.claim.toml", temp_db)
        assert check_result["confounds"][0]["status"] == "controlled-by-protocol", \
            "PARTIAL run should infer status as controlled-by-protocol"


class TestAtomicity:
    """Tests for atomic write guarantee (AC-21)."""

    def test_ac21_attest_parity_rollback_on_db_failure(
        self, temp_db, tmp_path, temp_claim_file_with_parity
    ):
        """
        AC-21 (Real Failure Injection): attest_parity rolls back the file on DB-update failure,
        ensuring file SHA == DB SHA (never diverged).

        This test injects a RuntimeError during the DB UPDATE to simulate a crash after os.replace.
        It verifies:
        1. attest_parity raises (propagates the DB error)
        2. On-disk file is rolled back to original content
        3. File SHA == original DB SHA (no divergence)
        4. check_sha does NOT raise after rollback (proving consistency)
        """
        from unittest.mock import patch, MagicMock
        import bathos.claim

        campaign_id = "test_campaign"
        campaign_name = "test_campaign"

        # Create campaign
        temp_db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at, claim_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [campaign_id, "test", campaign_name, "confirmation", "open",
             datetime.now(UTC).isoformat(), str(Path("test_parity.claim.toml"))]
        )

        # Create a valid PARITY run
        parity_run_id = "run_parity_001"
        parity_metadata = json.dumps({
            "metric_key": 1.0,
            "parity_run_type": "literature_parity"
        })
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, outcome, metadata) VALUES (?, ?, ?, ?)",
            [parity_run_id, campaign_id, "pass", parity_metadata]
        )

        # Register the initial claim
        from bathos.claim import register_claim
        register_claim(
            Path("test_parity.claim.toml"),
            campaign_id,
            temp_db,
            tmp_path,
            force=False
        )

        temp_db.commit()

        # Get initial state: file content and DB SHA
        original_file_content = (tmp_path / "test_parity.claim.toml").read_bytes()
        original_file_sha = __import__("hashlib").sha256(original_file_content).hexdigest()

        initial_row = temp_db.execute(
            "SELECT claim_sha256 FROM campaigns WHERE id = ?", [campaign_id]
        ).fetchone()
        initial_db_sha = initial_row[0]

        assert original_file_sha == initial_db_sha, \
            "Initial file and DB SHAs should match"

        # Create a wrapper DB that injects failure
        class FailingDB:
            def __init__(self, real_db):
                self._db = real_db
                self.fail_on_update = True

            def execute(self, query, params=None):
                # Fail only on the UPDATE campaigns SET claim_sha256 call
                if self.fail_on_update and "UPDATE campaigns SET claim_sha256" in query:
                    raise RuntimeError("Simulated DB update failure (crash during attest_parity)")
                return self._db.execute(query, params)

            def __getattr__(self, name):
                return getattr(self._db, name)

        failing_db = FailingDB(temp_db)

        # Attempt attest_parity with injected failure
        with pytest.raises(RuntimeError, match="Simulated DB update failure"):
            attest_parity(
                campaign_id=campaign_id,
                parity_run_id=parity_run_id,
                db=failing_db,
                workspace_root=tmp_path
            )

        # VERIFY ROLLBACK: file should be rolled back to original content
        file_content_after_failure = (tmp_path / "test_parity.claim.toml").read_bytes()
        file_sha_after_failure = __import__("hashlib").sha256(file_content_after_failure).hexdigest()

        assert file_content_after_failure == original_file_content, \
            "File should be rolled back to original content after DB failure"
        assert file_sha_after_failure == original_file_sha, \
            "File SHA should match original SHA after rollback"

        # VERIFY NO DIVERGENCE: file SHA == DB SHA (DB was never updated due to error)
        db_row = temp_db.execute(
            "SELECT claim_sha256 FROM campaigns WHERE id = ?", [campaign_id]
        ).fetchone()
        db_sha_after_failure = db_row[0]

        assert file_sha_after_failure == db_sha_after_failure, \
            f"After rollback, file SHA ({file_sha_after_failure}) should match DB SHA ({db_sha_after_failure}) — no divergence"

        # VERIFY check_sha does NOT raise (proving consistency is maintained)
        check_sha(
            path_relative="test_parity.claim.toml",
            registered_sha=db_sha_after_failure,
            workspace_root=tmp_path
        )

    def test_ac21_attest_parity_recovery_by_rerun(
        self, temp_db, tmp_path, temp_claim_file_with_parity
    ):
        """
        AC-21 (Recovery): After a crashed attest_parity with rollback, re-running
        attest_parity (without the injected failure) succeeds and binds the run,
        proving the crash is recoverable.
        """
        campaign_id = "test_campaign"
        campaign_name = "test_campaign"

        # Create campaign
        temp_db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at, claim_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [campaign_id, "test", campaign_name, "confirmation", "open",
             datetime.now(UTC).isoformat(), str(Path("test_parity.claim.toml"))]
        )

        # Create a valid PARITY run
        parity_run_id = "run_parity_001"
        parity_metadata = json.dumps({
            "metric_key": 1.0,
            "parity_run_type": "literature_parity"
        })
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, outcome, metadata) VALUES (?, ?, ?, ?)",
            [parity_run_id, campaign_id, "pass", parity_metadata]
        )

        # Register the initial claim
        from bathos.claim import register_claim
        register_claim(
            Path("test_parity.claim.toml"),
            campaign_id,
            temp_db,
            tmp_path,
            force=False
        )

        temp_db.commit()

        # Create a wrapper DB that injects failure on first call, then succeeds
        class FailingDBOnce:
            def __init__(self, real_db):
                self._db = real_db
                self.fail_once = True

            def execute(self, query, params=None):
                # Fail only on the first UPDATE campaigns SET claim_sha256 call
                if self.fail_once and "UPDATE campaigns SET claim_sha256" in query:
                    self.fail_once = False  # Disable for next call
                    raise RuntimeError("Simulated DB update failure")
                return self._db.execute(query, params)

            def __getattr__(self, name):
                return getattr(self._db, name)

        failing_db = FailingDBOnce(temp_db)

        # First attempt: inject failure
        with pytest.raises(RuntimeError, match="Simulated DB update failure"):
            attest_parity(
                campaign_id=campaign_id,
                parity_run_id=parity_run_id,
                db=failing_db,
                workspace_root=tmp_path
            )

        # RECOVERY: Re-run attest_parity without injected failure — should succeed
        attest_parity(
            campaign_id=campaign_id,
            parity_run_id=parity_run_id,
            db=failing_db,
            workspace_root=tmp_path
        )

        # Verify the binding succeeded
        claim = parse_claim(tmp_path / "test_parity.claim.toml")
        ref_parity = claim.confounds[0].get("reference_parity", {})
        assert ref_parity.get("parity_run_id") == parity_run_id, \
            "After recovery, parity_run_id should be bound in the claim"

        # Verify file and DB are in sync
        file_sha = claim.sha256
        db_row = temp_db.execute(
            "SELECT claim_sha256 FROM campaigns WHERE id = ?", [campaign_id]
        ).fetchone()
        db_sha = db_row[0]

        assert file_sha == db_sha, \
            "After recovery, file and DB SHAs should be synchronized"

        # check_sha should NOT raise
        check_sha(
            path_relative="test_parity.claim.toml",
            registered_sha=db_sha,
            workspace_root=tmp_path
        )


class TestParityConfoundCheck:
    """Tests for parity_confound_check() function."""

    def test_parity_confound_check_infers_controlled_from_passing_parity_run(
        self, temp_db, tmp_path, temp_claim_file_with_parity
    ):
        """
        parity_confound_check should infer status 'controlled' from a PARITY run.
        """
        campaign_id = "test_campaign"

        # Create campaign
        temp_db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at, claim_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [campaign_id, "test", "test_campaign", "confirmation", "open",
             datetime.now(UTC).isoformat(), str(Path("test_parity.claim.toml"))]
        )

        # Create a passing PARITY run
        parity_run_id = "run_parity_pass"
        parity_metadata = json.dumps({
            "parity_run_type": "literature_parity"
        })
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, outcome, metadata) VALUES (?, ?, ?, ?)",
            [parity_run_id, campaign_id, "pass", parity_metadata]
        )

        # Manually update the claim file with parity_run_id bound
        claim_content = (tmp_path / "test_parity.claim.toml").read_text()
        claim_content = claim_content.replace('parity_run_id = ""', f'parity_run_id = "{parity_run_id}"')
        (tmp_path / "test_parity.claim.toml").write_text(claim_content)

        temp_db.commit()

        # Call parity_confound_check
        result = parity_confound_check(tmp_path / "test_parity.claim.toml", temp_db)

        # Should return a dict with confounds list
        assert "confounds" in result
        assert len(result["confounds"]) > 0
        parity_confound = result["confounds"][0]
        assert parity_confound.get("id") == "C_parity"
        assert parity_confound.get("status") == "controlled", \
            "PARITY passing run should infer status as controlled"

    def test_parity_confound_check_infers_controlled_by_protocol_from_partial(
        self, temp_db, tmp_path, temp_claim_file_with_parity
    ):
        """
        parity_confound_check should infer status 'controlled-by-protocol' from PARTIAL run.
        """
        campaign_id = "test_campaign"

        # Create campaign
        temp_db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at, claim_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [campaign_id, "test", "test_campaign", "confirmation", "open",
             datetime.now(UTC).isoformat(), str(Path("test_parity.claim.toml"))]
        )

        # Create a PARTIAL run
        partial_run_id = "run_partial"
        partial_metadata = json.dumps({
            "parity_run_type": "literature_parity"
        })
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, outcome, metadata) VALUES (?, ?, ?, ?)",
            [partial_run_id, campaign_id, "partial", partial_metadata]
        )

        # Manually update the claim file
        claim_content = (tmp_path / "test_parity.claim.toml").read_text()
        claim_content = claim_content.replace('parity_run_id = ""', f'parity_run_id = "{partial_run_id}"')
        (tmp_path / "test_parity.claim.toml").write_text(claim_content)

        temp_db.commit()

        # Call parity_confound_check
        result = parity_confound_check(tmp_path / "test_parity.claim.toml", temp_db)

        parity_confound = result["confounds"][0]
        assert parity_confound.get("status") == "controlled-by-protocol", \
            "PARTIAL run should infer status as controlled-by-protocol"


class TestScaffoldClaimParity:
    """Tests for scaffold_claim() adding parity_run_id field."""

    def test_scaffold_claim_includes_parity_run_id_field(self, temp_db, tmp_path):
        """
        scaffold_claim should include 'parity_run_id = ""' in the [confounds.reference_parity]
        template, but NOT a 'parity_status' field.
        """
        from bathos.claim import scaffold_claim

        campaign_id = "test_campaign"
        campaign_name = "test_campaign"

        # Create campaign
        temp_db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [campaign_id, "test", campaign_name, "confirmation", "open",
             datetime.now(UTC).isoformat()]
        )
        temp_db.commit()

        # Scaffold the claim
        claim_path = scaffold_claim(campaign_id, temp_db, tmp_path)

        # Read the generated file
        content = claim_path.read_text()

        # Verify parity_run_id field is present
        assert 'parity_run_id = ""' in content, \
            "Scaffolded claim should include parity_run_id field"

        # Verify parity_status is NOT present
        assert 'parity_status' not in content, \
            "Scaffolded claim should NOT include parity_status field (inferred only)"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
