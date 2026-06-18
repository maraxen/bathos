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
from bathos.linter import check_single_cell_gate
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

    # Create runs table for union gate and baseline parity tests.
    # Include outcome and parity_run_type (v9 schema) so the graded-path query
    # in validate_claim (claim.py AC-13 block) works correctly.
    db.execute("""
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            campaign_id TEXT,
            claim_discriminates TEXT,
            outcome TEXT,
            parity_run_type TEXT,
            metadata TEXT
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


class TestClaimCoverageReport:
    """Tests for AC-12: claim-coverage JSON report emission."""

    def test_ac12_coverage_report_emitted(self, temp_claim_file, tmp_path):
        """AC-12: emit_claim_coverage_report writes JSON to correct path."""
        from bathos.campaigns import emit_claim_coverage_report

        claim = parse_claim(temp_claim_file)
        campaign_id = "test_campaign"
        catalog_dir = str(tmp_path / "catalog")
        uncovered_clauses = []
        verdict = "covered"
        bypass_reason = None

        # Create report
        emit_claim_coverage_report(
            db=None,
            catalog_dir=catalog_dir,
            campaign_id=campaign_id,
            verdict=verdict,
            uncovered_clauses=uncovered_clauses,
            claim=claim,
            bypass_reason=bypass_reason,
        )

        # Verify file exists at correct path
        report_path = Path(catalog_dir) / "sidecars" / campaign_id / f"claim_coverage_{campaign_id}.json"
        assert report_path.exists()

        # Verify JSON structure
        with open(report_path) as f:
            data = json.load(f)
        assert "coverage_fraction" in data
        assert "covered_clauses" in data
        assert "uncovered_clauses" in data
        assert "verdict_blocked" in data
        assert "bypass_reason" in data

    def test_ac12_coverage_fraction_full(self, temp_claim_file, tmp_path):
        """AC-12: coverage_fraction == 1.0 when all clauses covered."""
        from bathos.campaigns import emit_claim_coverage_report

        claim = parse_claim(temp_claim_file)
        campaign_id = "test_campaign"
        catalog_dir = str(tmp_path / "catalog")
        uncovered_clauses = []  # No uncovered clauses

        emit_claim_coverage_report(
            db=None,
            catalog_dir=catalog_dir,
            campaign_id=campaign_id,
            verdict="covered",
            uncovered_clauses=uncovered_clauses,
            claim=claim,
            bypass_reason=None,
        )

        report_path = Path(catalog_dir) / "sidecars" / campaign_id / f"claim_coverage_{campaign_id}.json"
        with open(report_path) as f:
            data = json.load(f)

        # With 1 clause and 0 uncovered, coverage should be 1.0
        assert data["coverage_fraction"] == 1.0

    def test_ac12_coverage_fraction_half(self, temp_claim_file, tmp_path):
        """AC-12: coverage_fraction == 0.5 when 1 of 2 clauses uncovered."""
        from bathos.campaigns import emit_claim_coverage_report

        # Create claim with 2 clauses
        claim_path = tmp_path / "test_2clause.claim.toml"
        claim_path.write_text("""[claim]
headline = "Test claim"
kill_condition = "Outcome != expected"

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
predicted_outcome = "pass"

[claim.union_gate]
[[claim.union_gate.clauses]]
id = "C_main"
description = "Main clause"
hypothesis_ids = ["H_primary"]

[[claim.union_gate.clauses]]
id = "C_secondary"
description = "Secondary clause"
hypothesis_ids = ["H_null"]
""")
        claim = parse_claim(claim_path)
        campaign_id = "test_campaign"
        catalog_dir = str(tmp_path / "catalog")
        uncovered_clauses = ["C_secondary"]  # 1 of 2 uncovered

        emit_claim_coverage_report(
            db=None,
            catalog_dir=catalog_dir,
            campaign_id=campaign_id,
            verdict="confounded",
            uncovered_clauses=uncovered_clauses,
            claim=claim,
            bypass_reason=None,
        )

        report_path = Path(catalog_dir) / "sidecars" / campaign_id / f"claim_coverage_{campaign_id}.json"
        with open(report_path) as f:
            data = json.load(f)

        assert data["coverage_fraction"] == 0.5
        assert len(data["uncovered_clauses"]) == 1
        assert "C_secondary" in data["uncovered_clauses"]

    def test_ac12_no_clauses_fraction_one(self, tmp_path):
        """AC-12: coverage_fraction == 1.0 when claim has no union_gate clauses."""
        from bathos.campaigns import emit_claim_coverage_report

        # Create claim with no clauses
        claim_path = tmp_path / "no_clauses.claim.toml"
        claim_path.write_text("""[claim]
headline = "Test claim"
kill_condition = "Outcome != expected"

[[hypotheses]]
id = "H_primary"
label = "Primary"

[[hypotheses]]
id = "H_null"
label = "Null"

[claim.union_gate]
""")
        claim = parse_claim(claim_path)
        campaign_id = "test_campaign"
        catalog_dir = str(tmp_path / "catalog")

        emit_claim_coverage_report(
            db=None,
            catalog_dir=catalog_dir,
            campaign_id=campaign_id,
            verdict="covered",
            uncovered_clauses=[],
            claim=claim,
            bypass_reason=None,
        )

        report_path = Path(catalog_dir) / "sidecars" / campaign_id / f"claim_coverage_{campaign_id}.json"
        with open(report_path) as f:
            data = json.load(f)

        assert data["coverage_fraction"] == 1.0

    def test_ac12_bypass_reason_in_report(self, temp_claim_file, tmp_path):
        """AC-12: bypass_reason appears in JSON when provided."""
        from bathos.campaigns import emit_claim_coverage_report

        claim = parse_claim(temp_claim_file)
        campaign_id = "test_campaign"
        catalog_dir = str(tmp_path / "catalog")
        bypass_reason = "force_verdict flag"

        emit_claim_coverage_report(
            db=None,
            catalog_dir=catalog_dir,
            campaign_id=campaign_id,
            verdict="confounded",
            uncovered_clauses=["C_main"],
            claim=claim,
            bypass_reason=bypass_reason,
        )

        report_path = Path(catalog_dir) / "sidecars" / campaign_id / f"claim_coverage_{campaign_id}.json"
        with open(report_path) as f:
            data = json.load(f)

        assert data["bypass_reason"] == bypass_reason


class TestBaselineParity:
    """Tests for AC-13: [baseline_parity] sub-block validation."""

    def test_ac13_parity_run_id_empty_warning(self, tmp_path, temp_db):
        """AC-13: confound with empty parity_run_id → WARNING."""
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

[[confounds]]
id = "C_1"
label = "Test confound"
[confounds.reference_parity]
parity_run_id = ""
reference_metric = "metric1"
reference_value = 100.0
equivalence_bound = 5.0
""")
        claim = parse_claim(claim_path)
        result = validate_claim(claim, db=None)

        # Should have error/warning
        assert not result.ok
        assert any("baseline admissibility" in str(e.message).lower() for e in result.errors)

    def test_ac13_metric_missing_is_error(self, tmp_path, temp_db):
        """AC-13: parity_run_id set but metric key missing from metadata → ERROR."""
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

[[confounds]]
id = "C_1"
label = "Test confound"
[confounds.reference_parity]
parity_run_id = "run_12345"
reference_metric = "nonexistent_metric"
reference_value = 100.0
equivalence_bound = 5.0
""")
        claim = parse_claim(claim_path)

        # Create a run in the DB without the parity_metric in metadata.
        # Include outcome and parity_run_type columns (v9 schema) so the graded-path
        # query succeeds; parity_run_type=NULL means this is a legacy run → graded path
        # skips and the legacy equivalence-bound path fires.
        temp_db.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                outcome TEXT,
                parity_run_type TEXT,
                metadata TEXT
            )
        """)
        temp_db.execute(
            "INSERT INTO runs (id, outcome, parity_run_type, metadata) VALUES (?, ?, ?, ?)",
            ["run_12345", "pass", None, json.dumps({"other_metric": 50.0})]
        )
        temp_db.commit()

        result = validate_claim(claim, db=temp_db)

        # Should have error for missing metric key
        assert not result.ok
        assert any("parity_metric key" in str(e.message) for e in result.errors)

    def test_ac13_parity_run_not_compacted(self, tmp_path, temp_db):
        """AC-13: parity_run_id set but run not in DB → WARNING."""
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

[[confounds]]
id = "C_1"
label = "Test confound"
[confounds.reference_parity]
parity_run_id = "run_not_in_db"
reference_metric = "metric1"
reference_value = 100.0
equivalence_bound = 5.0
""")
        claim = parse_claim(claim_path)

        result = validate_claim(claim, db=temp_db)

        # Should have error/warning about not being compacted
        assert not result.ok
        assert any("bth compact" in str(e.message) for e in result.errors)

    def test_ac13_parity_pass(self, tmp_path, temp_db):
        """AC-13: parity check passes when |result - ref| < bound."""
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

[[confounds]]
id = "C_1"
label = "Test confound"
[confounds.reference_parity]
parity_run_id = "baseline_run"
reference_metric = "metric1"
reference_value = 100.0
equivalence_bound = 10.0
""")
        claim = parse_claim(claim_path)

        # Create baseline run with matching metric within bound.
        # Include outcome and parity_run_type (v9 schema); NULL parity_run_type → legacy path.
        temp_db.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                outcome TEXT,
                parity_run_type TEXT,
                metadata TEXT
            )
        """)
        temp_db.execute(
            "INSERT INTO runs (id, outcome, parity_run_type, metadata) VALUES (?, ?, ?, ?)",
            ["baseline_run", "pass", None, json.dumps({"metric1": 102.0})]
        )
        temp_db.commit()

        result = validate_claim(claim, db=temp_db)

        # Should pass (have PASS in output) or be ok
        assert any("PASS" in str(e.message) for e in result.errors) or result.ok

    def test_ac13_parity_fail(self, tmp_path, temp_db):
        """AC-13: parity check fails when |result - ref| >= bound → WARNING."""
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

[[confounds]]
id = "C_1"
label = "Test confound"
[confounds.reference_parity]
parity_run_id = "baseline_run"
reference_metric = "metric1"
reference_value = 100.0
equivalence_bound = 5.0
""")
        claim = parse_claim(claim_path)

        # Create baseline run with metric outside bound.
        # Include outcome and parity_run_type (v9 schema); NULL parity_run_type → legacy path.
        temp_db.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                outcome TEXT,
                parity_run_type TEXT,
                metadata TEXT
            )
        """)
        temp_db.execute(
            "INSERT INTO runs (id, outcome, parity_run_type, metadata) VALUES (?, ?, ?, ?)",
            ["baseline_run", "pass", None, json.dumps({"metric1": 110.0})]
        )
        temp_db.commit()

        result = validate_claim(claim, db=temp_db)

        # Should have error/warning about equivalence bound
        assert not result.ok
        assert any("equivalence bound" in str(e.message) for e in result.errors)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

class TestAdvisoryLints:
    """Tests for AC-04, AC-05, AC-06 advisory lint checks."""

    def test_ac04_zero_power_single_label_warns(self, tmp_path):
        """AC-04: warn when all entries for a label have same predicted_outcome."""
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
predicted_outcome = "same_result"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "main"
predicted_outcome = "same_result"
""")
        claim = parse_claim(claim_path)
        result = validate_claim(claim)
        assert result.ok is True
        assert len(result.warnings) >= 1
        assert any("zero discriminative power" in w for w in result.warnings)

    def test_ac04_zero_power_no_fire_with_different_outcomes(self, tmp_path):
        """AC-04: no warn when entries for same label have different outcomes."""
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
predicted_outcome = "outcome_a"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "main"
predicted_outcome = "outcome_b"
""")
        claim = parse_claim(claim_path)
        result = validate_claim(claim)
        assert result.ok is True
        assert not any("zero discriminative power" in w for w in result.warnings)

    def test_ac04_zero_power_no_fire_single_entry_per_label(self, tmp_path):
        """AC-04: no warn when only 1 entry per label (guard fires)."""
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
predicted_outcome = "outcome_a"
""")
        claim = parse_claim(claim_path)
        result = validate_claim(claim)
        assert result.ok is True
        assert not any("zero discriminative power" in w for w in result.warnings)

    def test_ac05_positive_bias_warns(self, tmp_path):
        """AC-05: warn when all entries predict same outcome."""
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
planned_run_label = "run1"
predicted_outcome = "supports_primary"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "run2"
predicted_outcome = "supports_primary"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "run3"
predicted_outcome = "supports_primary"
""")
        claim = parse_claim(claim_path)
        result = validate_claim(claim)
        assert result.ok is True
        assert len(result.warnings) >= 1
        assert any("positive-testing bias" in w for w in result.warnings)

    def test_ac05_positive_bias_no_fire_single_entry(self, tmp_path):
        """AC-05: no warn with only 1 entry."""
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
planned_run_label = "run1"
predicted_outcome = "supports_primary"
""")
        claim = parse_claim(claim_path)
        result = validate_claim(claim)
        assert result.ok is True
        assert not any("positive-testing bias" in w for w in result.warnings)

    def test_ac05_no_fire_mixed_outcomes(self, tmp_path):
        """AC-05: no warn when entries have different outcomes."""
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
planned_run_label = "run1"
predicted_outcome = "outcome_a"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "run2"
predicted_outcome = "outcome_b"
""")
        claim = parse_claim(claim_path)
        result = validate_claim(claim)
        assert result.ok is True
        assert not any("positive-testing bias" in w for w in result.warnings)

    def test_ac06_single_cell_gate_warns(self, temp_db):
        """AC-06: warn if all runs in campaign share identical metadata values."""
        from bathos.linter import check_single_cell_gate

        campaign_id = "test_campaign"

        # Insert campaign
        temp_db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at) VALUES (?, ?, ?, ?, ?, ?)",
            [campaign_id, "test", "test_campaign", "confirmation", "open", "2026-01-01T00:00:00Z"]
        )

        # Insert 2 runs with identical metadata
        metadata = json.dumps({"temperature": "300K", "pressure": "1atm"})
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, metadata) VALUES (?, ?, ?)",
            ["run1", campaign_id, metadata]
        )
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, metadata) VALUES (?, ?, ?)",
            ["run2", campaign_id, metadata]
        )

        # Link to campaign
        temp_db.execute(
            "INSERT INTO campaign_runs (campaign_id, run_id) VALUES (?, ?)",
            [campaign_id, "run1"]
        )
        temp_db.execute(
            "INSERT INTO campaign_runs (campaign_id, run_id) VALUES (?, ?)",
            [campaign_id, "run2"]
        )
        temp_db.commit()

        claim_disc = []
        issues = check_single_cell_gate(claim_disc, campaign_id, temp_db)
        assert len(issues) >= 1
        assert any("single-cell-gate" in i.issue for i in issues)

    def test_ac06_no_fire_different_metadata(self, temp_db):
        """AC-06: no warn when runs have differing metadata values."""
        from bathos.linter import check_single_cell_gate

        campaign_id = "test_campaign"

        # Insert campaign
        temp_db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at) VALUES (?, ?, ?, ?, ?, ?)",
            [campaign_id, "test", "test_campaign", "confirmation", "open", "2026-01-01T00:00:00Z"]
        )

        # Insert 2 runs with different metadata values
        metadata1 = json.dumps({"temperature": "300K", "pressure": "1atm"})
        metadata2 = json.dumps({"temperature": "310K", "pressure": "1atm"})
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, metadata) VALUES (?, ?, ?)",
            ["run1", campaign_id, metadata1]
        )
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, metadata) VALUES (?, ?, ?)",
            ["run2", campaign_id, metadata2]
        )

        # Link to campaign
        temp_db.execute(
            "INSERT INTO campaign_runs (campaign_id, run_id) VALUES (?, ?)",
            [campaign_id, "run1"]
        )
        temp_db.execute(
            "INSERT INTO campaign_runs (campaign_id, run_id) VALUES (?, ?)",
            [campaign_id, "run2"]
        )
        temp_db.commit()

        claim_disc = []
        issues = check_single_cell_gate(claim_disc, campaign_id, temp_db)
        assert not any("single-cell-gate" in i.issue for i in issues)

    def test_ac06_no_fire_single_run(self, temp_db):
        """AC-06: no warn with only 1 run."""
        from bathos.linter import check_single_cell_gate

        campaign_id = "test_campaign"

        # Insert campaign
        temp_db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at) VALUES (?, ?, ?, ?, ?, ?)",
            [campaign_id, "test", "test_campaign", "confirmation", "open", "2026-01-01T00:00:00Z"]
        )

        # Insert only 1 run
        metadata = json.dumps({"temperature": "300K", "pressure": "1atm"})
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, metadata) VALUES (?, ?, ?)",
            ["run1", campaign_id, metadata]
        )

        # Link to campaign
        temp_db.execute(
            "INSERT INTO campaign_runs (campaign_id, run_id) VALUES (?, ?)",
            [campaign_id, "run1"]
        )
        temp_db.commit()

        claim_disc = []
        issues = check_single_cell_gate(claim_disc, campaign_id, temp_db)
        assert not any("single-cell-gate" in i.issue for i in issues)

    def test_warnings_dont_set_ok_false(self, tmp_path):
        """CRITICAL: AC-04 or AC-05 warnings must NOT set ok=False."""
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
planned_run_label = "run1"
predicted_outcome = "same"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "run1"
predicted_outcome = "same"
""")
        claim = parse_claim(claim_path)
        result = validate_claim(claim)
        assert result.ok is True
        assert len(result.warnings) > 0
        assert len(result.errors) == 0

    def test_ac04_three_plus_entries_warns(self, tmp_path):
        """AC-04: warn with 3+ entries all same outcome (not just 2)."""
        claim_path = tmp_path / "test.claim.toml"
        claim_path.write_text("""[claim]
headline = "Test"
kill_condition = "test"

[[hypotheses]]
id = "H_primary"
label = "Primary"

[[hypotheses]]
id = "H_alt"
label = "Alternative"

[[hypotheses]]
id = "H_null"
label = "Null"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "main"
predicted_outcome = "all_same"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_alt"
planned_run_label = "main"
predicted_outcome = "all_same"

[[claim.discriminability]]
hypothesis_a = "H_alt"
hypothesis_b = "H_null"
planned_run_label = "main"
predicted_outcome = "all_same"
""")
        claim = parse_claim(claim_path)
        result = validate_claim(claim)
        assert result.ok is True
        assert any("zero discriminative power" in w for w in result.warnings)
        assert any("all 3" in w for w in result.warnings)

    def test_ac04_ac05_simultaneous(self, tmp_path):
        """AC-04 and AC-05 fire simultaneously on same claim."""
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
planned_run_label = "run1"
predicted_outcome = "same"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "run1"
predicted_outcome = "same"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "run2"
predicted_outcome = "same"
""")
        claim = parse_claim(claim_path)
        result = validate_claim(claim)
        assert result.ok is True
        # Both AC-04 and AC-05 should fire
        assert any("zero discriminative power" in w for w in result.warnings)
        assert any("positive-testing bias" in w for w in result.warnings)
        assert len(result.warnings) >= 2

    def test_ac05_boundary_two_entries(self, tmp_path):
        """AC-05: boundary test with exactly 2 entries, same outcome."""
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
planned_run_label = "run1"
predicted_outcome = "outcome_x"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "run2"
predicted_outcome = "outcome_x"
""")
        claim = parse_claim(claim_path)
        result = validate_claim(claim)
        assert result.ok is True
        assert any("positive-testing bias" in w for w in result.warnings)

    def test_ac06_single_cell_db_exception_returns_empty(self, temp_db, tmp_path):
        """AC-06: DB exception returns empty list, doesn't crash."""
        campaign_id = "test_campaign_001"

        # Create broken DB without campaign_runs table
        bad_db = duckdb.connect(":memory:")
        bad_db.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, metadata TEXT)")

        claim_disc = []
        issues = check_single_cell_gate(claim_disc, campaign_id, bad_db)
        assert issues == []

    def test_ac06_single_cell_zero_metadata_runs(self, temp_db):
        """AC-06: no issue when runs have no metadata."""
        campaign_id = "test_campaign_002"
        temp_db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at) VALUES (?, ?, ?, ?, ?, ?)",
            [campaign_id, "test", "test_camp", "confirmation", "concluded", "2026-01-01T00:00:00"]
        )

        # Add runs with NULL metadata
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, claim_discriminates, metadata) VALUES (?, ?, ?, ?)",
            ["run1", campaign_id, None, None]
        )
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, claim_discriminates, metadata) VALUES (?, ?, ?, ?)",
            ["run2", campaign_id, None, None]
        )

        temp_db.execute(
            "INSERT INTO campaign_runs (campaign_id, run_id) VALUES (?, ?)",
            [campaign_id, "run1"]
        )
        temp_db.execute(
            "INSERT INTO campaign_runs (campaign_id, run_id) VALUES (?, ?)",
            [campaign_id, "run2"]
        )
        temp_db.commit()

        claim_disc = []
        issues = check_single_cell_gate(claim_disc, campaign_id, temp_db)
        # Should not flag as issue since metadata is missing
        assert not any("single-cell-gate" in i.issue for i in issues)

    def test_ac06_partial_metadata_coverage(self, temp_db):
        """AC-06: some runs have metadata, some don't."""
        campaign_id = "test_campaign_003"
        temp_db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at) VALUES (?, ?, ?, ?, ?, ?)",
            [campaign_id, "test", "test_camp3", "confirmation", "concluded", "2026-01-01T00:00:00"]
        )

        # Run 1 with metadata
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, metadata) VALUES (?, ?, ?)",
            ["run1", campaign_id, json.dumps({"param": 1.0, "metric": 100.0})]
        )
        # Run 2 without metadata
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, metadata) VALUES (?, ?, ?)",
            ["run2", campaign_id, None]
        )

        temp_db.execute(
            "INSERT INTO campaign_runs (campaign_id, run_id) VALUES (?, ?)",
            [campaign_id, "run1"]
        )
        temp_db.execute(
            "INSERT INTO campaign_runs (campaign_id, run_id) VALUES (?, ?)",
            [campaign_id, "run2"]
        )
        temp_db.commit()

        issues = check_single_cell_gate([], campaign_id, temp_db)
        # Only 1 run with metadata, so shouldn't detect single-cell (need >= 2)
        assert not any("single-cell-gate" in i.issue for i in issues)

    def test_ac04_multiple_labels_different_outcomes(self, tmp_path):
        """AC-04: different labels with different outcomes should not warn."""
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
planned_run_label = "label_a"
predicted_outcome = "outcome_1"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "label_b"
predicted_outcome = "outcome_2"
""")
        claim = parse_claim(claim_path)
        result = validate_claim(claim)
        assert result.ok is True
        # Each label has only 1 entry, so no zero-power warning
        assert not any("zero discriminative power" in w for w in result.warnings)
        # Only 2 entries total with different outcomes, so no positive-bias
        assert not any("positive-testing bias" in w for w in result.warnings)

    def test_ac06_differing_metadata_values(self, temp_db):
        """AC-06: runs with differing metadata values should not flag."""
        campaign_id = "test_campaign_004"
        temp_db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at) VALUES (?, ?, ?, ?, ?, ?)",
            [campaign_id, "test", "test_camp4", "confirmation", "concluded", "2026-01-01T00:00:00"]
        )

        # Run 1
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, metadata) VALUES (?, ?, ?)",
            ["run1", campaign_id, json.dumps({"param": 1.0, "metric": 100.0})]
        )
        # Run 2 with different param value
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, metadata) VALUES (?, ?, ?)",
            ["run2", campaign_id, json.dumps({"param": 2.0, "metric": 100.0})]
        )

        temp_db.execute(
            "INSERT INTO campaign_runs (campaign_id, run_id) VALUES (?, ?)",
            [campaign_id, "run1"]
        )
        temp_db.execute(
            "INSERT INTO campaign_runs (campaign_id, run_id) VALUES (?, ?)",
            [campaign_id, "run2"]
        )
        temp_db.commit()

        issues = check_single_cell_gate([], campaign_id, temp_db)
        # param differs, so not single-cell
        assert not any("single-cell-gate" in i.issue for i in issues)

    def test_ac03_opaque_id_requires_label(self, tmp_path):
        """AC-03: opaque hypothesis IDs like 'H1' require descriptive label."""
        claim_path = tmp_path / "test.claim.toml"
        claim_path.write_text("""[claim]
headline = "Test"
kill_condition = "test"

[[hypotheses]]
id = "H1"
label = ""

[[hypotheses]]
id = "H_null"
label = "Null"
""")
        claim = parse_claim(claim_path)
        result = validate_claim(claim)
        assert not result.ok
        assert any("Opaque hypothesis id" in str(e.message) for e in result.errors)

    def test_ac06_all_identical_metadata_warns(self, temp_db):
        """AC-06: single-cell-gate warns when all runs have identical metadata values."""
        campaign_id = "test_campaign_005"
        temp_db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at) VALUES (?, ?, ?, ?, ?, ?)",
            [campaign_id, "test", "test_camp5", "confirmation", "concluded", "2026-01-01T00:00:00"]
        )

        # Both runs with identical metadata
        metadata_same = json.dumps({"param": 1.0, "metric": 100.0})
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, metadata) VALUES (?, ?, ?)",
            ["run1", campaign_id, metadata_same]
        )
        temp_db.execute(
            "INSERT INTO runs (id, campaign_id, metadata) VALUES (?, ?, ?)",
            ["run2", campaign_id, metadata_same]
        )

        temp_db.execute(
            "INSERT INTO campaign_runs (campaign_id, run_id) VALUES (?, ?)",
            [campaign_id, "run1"]
        )
        temp_db.execute(
            "INSERT INTO campaign_runs (campaign_id, run_id) VALUES (?, ?)",
            [campaign_id, "run2"]
        )
        temp_db.commit()

        issues = check_single_cell_gate([], campaign_id, temp_db)
        # Should flag single-cell-gate smell when all metadata values are identical
        assert any("single-cell-gate" in i.issue for i in issues)
        assert any(i.severity.value == "warning" for i in issues)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

