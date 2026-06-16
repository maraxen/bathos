"""Tests for claim.py — claim validation, registration, and Union Gate logic."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from datetime import UTC, datetime

import duckdb
import pytest

from bathos.claim import (
    ClaimFile,
    ValidationError,
    ValidationResult,
    parse_claim,
    validate_claim,
    register_claim,
    check_sha,
    run_union_gate,
)
from bathos.compact import compact
from bathos.catalog import write_run
from bathos.schema import Run


@pytest.fixture
def temp_claim_file(tmp_path):
    """Create a minimal valid claim.bth.toml file."""
    claim_path = tmp_path / "test.claim.toml"
    content = """[claim]
headline = "Test claim"
kill_condition = "Outcome != expected"
regime = "param=1.0..2.0"

[[hypotheses]]
id = "H_primary"
label = "Primary hypothesis"
predicted_signature = "metric=100"

[[hypotheses]]
id = "H_null"
label = "Null hypothesis"
predicted_signature = "metric=50"

[[assumptions]]
id = "A1"
label = "Test assumption"

[[confounds]]
id = "C1"
label = "Test confound"

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


@pytest.fixture
def invalid_claim_file(tmp_path):
    """Create an invalid claim.bth.toml file (missing kill_condition)."""
    claim_path = tmp_path / "invalid.claim.toml"
    content = """[claim]
headline = "Invalid claim"

[[hypotheses]]
id = "H1"
label = "Only one hypothesis"
"""
    claim_path.write_text(content)
    return claim_path


@pytest.fixture
def temp_db(tmp_path):
    """Create a temporary DuckDB database with campaigns table."""
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

    # Create runs table for union gate tests
    db.execute("""
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            campaign_id TEXT,
            claim_discriminates TEXT
        )
    """)

    # Create campaign_runs table for union gate tests
    db.execute("""
        CREATE TABLE campaign_runs (
            campaign_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            evalue REAL,
            seq_position INTEGER,
            PRIMARY KEY (campaign_id, run_id)
        )
    """)

    db.commit()
    yield db
    db.close()


class TestParseAndValidate:
    """Tests for parse_claim and validate_claim."""

    def test_parse_claim_valid(self, temp_claim_file):
        """Parse a valid claim file."""
        claim = parse_claim(temp_claim_file)
        assert claim.headline == "Test claim"
        assert claim.kill_condition == "Outcome != expected"
        assert claim.regime == "param=1.0..2.0"
        assert len(claim.hypotheses) == 2
        assert claim.hypotheses[0]["id"] == "H_primary"

    def test_parse_claim_missing_file(self, tmp_path):
        """Parse raises FileNotFoundError for missing file."""
        missing_path = tmp_path / "missing.claim.toml"
        with pytest.raises(FileNotFoundError):
            parse_claim(missing_path)

    def test_parse_claim_computes_sha256(self, temp_claim_file):
        """Parse computes SHA256 hash of file content."""
        claim = parse_claim(temp_claim_file)
        assert claim.sha256
        assert len(claim.sha256) == 64  # SHA256 hex string length

    def test_validate_claim_blank_kill_condition(self, tmp_path):
        """AC-03: blank kill_condition raises ERROR."""
        claim_path = tmp_path / "test.claim.toml"
        claim_path.write_text("""[claim]
headline = "Test"
kill_condition = ""

[[hypotheses]]
id = "H1"
label = "Test"

[[hypotheses]]
id = "H2"
label = "Test2"
""")
        claim = parse_claim(claim_path)
        result = validate_claim(claim)
        assert not result.ok
        assert any("kill_condition" in e.message for e in result.errors)

    def test_validate_claim_missing_headline(self, tmp_path):
        """AC-03: missing headline raises ERROR."""
        claim_path = tmp_path / "test.claim.toml"
        claim_path.write_text("""[claim]
kill_condition = "test"

[[hypotheses]]
id = "H1"
label = "Test"

[[hypotheses]]
id = "H2"
label = "Test2"
""")
        claim = parse_claim(claim_path)
        result = validate_claim(claim)
        assert not result.ok
        assert any("headline" in e.message for e in result.errors)

    def test_validate_claim_fewer_than_2_hypotheses(self, tmp_path):
        """AC-03: fewer than 2 hypotheses raises ERROR."""
        claim_path = tmp_path / "test.claim.toml"
        claim_path.write_text("""[claim]
headline = "Test"
kill_condition = "test"

[[hypotheses]]
id = "H1"
label = "Only one hypothesis"
""")
        claim = parse_claim(claim_path)
        result = validate_claim(claim)
        assert not result.ok
        assert any("2" in e.message for e in result.errors)

    def test_validate_claim_no_null_or_misspec(self, tmp_path):
        """AC-03: no null/misspec hypothesis raises WARNING."""
        claim_path = tmp_path / "test.claim.toml"
        claim_path.write_text("""[claim]
headline = "Test"
kill_condition = "test"

[[hypotheses]]
id = "H_primary"
label = "Primary"

[[hypotheses]]
id = "H_alternative"
label = "Alternative"
""")
        claim = parse_claim(claim_path)
        result = validate_claim(claim)
        assert not result.ok
        assert any("null" in e.message.lower() for e in result.errors)

    def test_validate_claim_opaque_id_without_label(self, tmp_path):
        """AC-03: opaque ID (matching /^[A-Z][0-9]+$/) without label raises WARNING."""
        claim_path = tmp_path / "test.claim.toml"
        claim_path.write_text("""[claim]
headline = "Test"
kill_condition = "test"

[[hypotheses]]
id = "H_primary"
label = "Primary"

[[hypotheses]]
id = "H1"

[[confounds]]
id = "C1"
""")
        claim = parse_claim(claim_path)
        result = validate_claim(claim)
        # Should have errors for opaque IDs without labels
        assert not result.ok

    def test_validate_claim_missing_predicted_outcome(self, tmp_path):
        """AC-03: discriminability entry missing predicted_outcome raises ERROR."""
        claim_path = tmp_path / "test.claim.toml"
        claim_path.write_text("""[claim]
headline = "Test"
kill_condition = "test"

[[hypotheses]]
id = "H_primary"
label = "Primary"

[[hypotheses]]
id = "H_null"
label = "Null"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "main"
""")
        claim = parse_claim(claim_path)
        result = validate_claim(claim)
        assert not result.ok
        assert any("predicted_outcome" in e.message for e in result.errors)

    def test_validate_claim_valid(self, temp_claim_file):
        """AC-03: valid claim passes validation."""
        claim = parse_claim(temp_claim_file)
        result = validate_claim(claim)
        assert result.ok


class TestRegisterClaim:
    """Tests for claim registration."""

    def test_register_claim_success(self, temp_claim_file, temp_db, tmp_path):
        """AC-02: register claim writes path and SHA256 to campaigns table."""
        campaign_id = "test_campaign_id"

        # Insert campaign
        temp_db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [campaign_id, "test", "test_campaign", "confirmation", "open", datetime.now(UTC).isoformat()]
        )
        temp_db.commit()

        # Register claim with relative path
        relative_path = Path("test.claim.toml")
        register_claim(relative_path, campaign_id, temp_db, tmp_path, force=False)

        # Verify stored
        row = temp_db.execute(
            "SELECT claim_path, claim_sha256 FROM campaigns WHERE id=?",
            [campaign_id]
        ).fetchone()
        assert row is not None
        assert row[0] == str(relative_path)
        assert len(row[1]) == 64  # SHA256

    def test_register_claim_rejects_absolute_path(self, temp_claim_file, temp_db, tmp_path):
        """AC-02: register rejects absolute paths that escape workspace."""
        campaign_id = "test_campaign_id"
        temp_db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [campaign_id, "test", "test_campaign", "confirmation", "open", datetime.now(UTC).isoformat()]
        )
        temp_db.commit()

        # Create a file outside workspace
        other_dir = Path("/tmp/outside_workspace")
        other_dir.mkdir(exist_ok=True)
        outside_file = other_dir / "outside.claim.toml"
        outside_file.write_text("[claim]\nheadline='test'")

        # Try to register with absolute path that escapes workspace
        with pytest.raises(RuntimeError, match="escapes"):
            register_claim(outside_file, campaign_id, temp_db, tmp_path, force=False)

    def test_register_claim_file_not_found(self, temp_db, tmp_path):
        """register raises FileNotFoundError for missing file."""
        campaign_id = "test_campaign_id"
        temp_db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [campaign_id, "test", "test_campaign", "confirmation", "open", datetime.now(UTC).isoformat()]
        )
        temp_db.commit()

        with pytest.raises(FileNotFoundError):
            register_claim(Path("missing.claim.toml"), campaign_id, temp_db, tmp_path, force=False)


class TestCheckSha:
    """Tests for SHA256 integrity checks."""

    def test_check_sha_match(self, temp_claim_file, tmp_path):
        """AC-11: check_sha passes when SHA256 matches."""
        claim = parse_claim(temp_claim_file)
        relative_path = temp_claim_file.relative_to(tmp_path)
        check_sha(str(relative_path), claim.sha256, tmp_path)  # Should not raise

    def test_check_sha_mismatch(self, temp_claim_file, tmp_path):
        """AC-11: check_sha raises ValueError on mismatch."""
        relative_path = temp_claim_file.relative_to(tmp_path)
        wrong_sha = "0" * 64
        with pytest.raises(ValueError, match="modified"):
            check_sha(str(relative_path), wrong_sha, tmp_path)

    def test_check_sha_file_not_found(self, tmp_path):
        """check_sha raises FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            check_sha("missing.claim.toml", "0" * 64, tmp_path)


class TestUnionGate:
    """Tests for Union Gate discriminability checking."""

    def test_run_union_gate_all_covered(self, temp_claim_file, temp_db):
        """Union Gate returns covered when all clauses have covering runs."""
        claim = parse_claim(temp_claim_file)
        campaign_id = "test_campaign"

        # Insert runs with discriminates covering the clause hypotheses
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, claim_discriminates) VALUES (?, ?, ?)",
            ["run1", campaign_id, json.dumps(["H_primary", "H_null"])]
        )
        # Add to campaign_runs
        temp_db.execute(
            "INSERT INTO campaign_runs (campaign_id, run_id) VALUES (?, ?)",
            [campaign_id, "run1"]
        )
        temp_db.commit()

        verdict, uncovered = run_union_gate(temp_db, campaign_id, claim)
        assert verdict == "covered"
        assert uncovered == []

    def test_run_union_gate_uncovered(self, temp_claim_file, temp_db):
        """Union Gate returns confounded when clause is not covered."""
        claim = parse_claim(temp_claim_file)
        campaign_id = "test_campaign"

        # Insert run without the required hypothesis IDs
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, claim_discriminates) VALUES (?, ?, ?)",
            ["run1", campaign_id, json.dumps(["H_primary"])]  # Missing H_null
        )
        # Add to campaign_runs
        temp_db.execute(
            "INSERT INTO campaign_runs (campaign_id, run_id) VALUES (?, ?)",
            [campaign_id, "run1"]
        )
        temp_db.commit()

        verdict, uncovered = run_union_gate(temp_db, campaign_id, claim)
        assert verdict == "confounded"
        assert len(uncovered) > 0

    def test_run_union_gate_no_runs(self, temp_claim_file, temp_db):
        """Union Gate returns confounded when no runs exist."""
        claim = parse_claim(temp_claim_file)
        campaign_id = "test_campaign"

        verdict, uncovered = run_union_gate(temp_db, campaign_id, claim)
        assert verdict == "confounded"
        assert len(uncovered) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
