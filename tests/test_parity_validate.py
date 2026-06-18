"""Tests for scripts/validation/parity_validate.py runner.

Tests the parity validation runner script that emits grade results,
validates against parity.bth.toml, and produces the SHA-anchored triple.

AC-19: parity_validate.py writes metadata.parity_run_type='literature_parity'
AC-15: parity_validate.py registers SHA-anchored triple in output_paths
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# These are imported from the parity module (already exists)
from bathos.parity import (
    ParityEvidence,
    compute_grade,
    evidence_from_result,
)


@pytest.fixture
def temp_dir() -> Path:
    """Temporary directory for test artifacts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_parity_toml(temp_dir: Path) -> Path:
    """Create a minimal valid parity.bth.toml for testing."""
    toml_path = temp_dir / "parity.bth.toml"
    toml_content = """
[parity]
paper_pdf = "https://example.com/paper.pdf"
impl_paths = ["src/impl.py", "src/utils.py"]
citation_note = "Doe et al. 2025"
equivalence_bound = 0.01
"""
    toml_path.write_text(toml_content)
    return toml_path


@pytest.fixture
def sample_evidence() -> dict:
    """Create a minimal valid evidence dict."""
    return {
        "clause_parity_pct": 1.0,
        "adversarial_survived": True,
        "invariant_pass": True,
        "reproduction_rung": "R1",
        "ambiguity_load": "none",
    }


class TestParityValidateMetadataWrite:
    """AC-19: parity_validate.py writes metadata.parity_run_type='literature_parity'."""

    def test_metadata_contains_parity_run_type(self, sample_evidence: dict, temp_dir: Path):
        """Verify result JSON contains metadata.parity_run_type='literature_parity'."""
        # Simulate the result that would be emitted by parity_validate.py
        # The script should populate this in the metadata JSON blob
        evidence = evidence_from_result(sample_evidence)
        grade_result = compute_grade(evidence)

        # Build the result dict as parity_validate would
        result = {
            "grade": grade_result.grade,
            "ceilings": grade_result.ceilings,
            "metadata": {
                "parity_run_type": "literature_parity",
            },
        }

        # Verify the structure
        assert "metadata" in result
        assert result["metadata"]["parity_run_type"] == "literature_parity"
        assert result["grade"] == "PARITY"

    def test_metadata_is_json_serializable(self, sample_evidence: dict):
        """Verify metadata can be JSON-serialized (for bth run integration)."""
        evidence = evidence_from_result(sample_evidence)
        grade_result = compute_grade(evidence)

        result = {
            "grade": grade_result.grade,
            "ceilings": grade_result.ceilings,
            "metadata": {
                "parity_run_type": "literature_parity",
            },
        }

        # Must be JSON-serializable
        json_str = json.dumps(result)
        parsed = json.loads(json_str)
        assert parsed["metadata"]["parity_run_type"] == "literature_parity"


class TestParityValidateSHATriple:
    """AC-15: parity_validate.py registers SHA-anchored triple in output_paths."""

    def test_triple_paths_registered(self, sample_evidence: dict, temp_dir: Path):
        """Verify result registers three artifact paths with SHA256 hashes."""
        evidence = evidence_from_result(sample_evidence)
        grade_result = compute_grade(evidence)

        # Create mock artifact files
        checklist_path = temp_dir / "clause_checklist.json"
        verdict_path = temp_dir / "parity_verdict.md"
        invariant_path = temp_dir / "parity_invariant_test.py"

        checklist_path.write_text(json.dumps({"clauses": []}))
        verdict_path.write_text("# Parity Verdict\n\nGrade: PARITY")
        invariant_path.write_text("def test_invariant():\n    pass")

        # Compute SHAs
        def compute_sha(p: Path) -> str:
            return hashlib.sha256(p.read_bytes()).hexdigest()

        checklist_sha = compute_sha(checklist_path)
        verdict_sha = compute_sha(verdict_path)
        invariant_sha = compute_sha(invariant_path)

        # Build result as parity_validate would
        result = {
            "grade": grade_result.grade,
            "ceilings": grade_result.ceilings,
            "metadata": {
                "parity_run_type": "literature_parity",
            },
            "output_paths": [
                str(checklist_path),
                str(verdict_path),
                str(invariant_path),
            ],
            "output_shas": {
                str(checklist_path): checklist_sha,
                str(verdict_path): verdict_sha,
                str(invariant_path): invariant_sha,
            },
        }

        # Verify the triple is registered
        assert len(result["output_paths"]) == 3
        assert len(result["output_shas"]) == 3
        for path in result["output_paths"]:
            assert path in result["output_shas"]
            assert len(result["output_shas"][path]) == 64  # SHA256 is 64 hex chars

    def test_sha_mismatch_detectable(self, sample_evidence: dict, temp_dir: Path):
        """Verify SHA drift can be detected (for AC-20: bth check integration)."""
        # Create an artifact
        artifact_path = temp_dir / "parity_verdict.md"
        artifact_path.write_text("# Parity Verdict\n\nGrade: PARITY")

        # Compute initial SHA
        initial_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()

        # Record in result
        result = {
            "output_shas": {
                str(artifact_path): initial_sha,
            },
        }

        # Simulate drift: modify file
        artifact_path.write_text("# MODIFIED\n\nGrade: PARTIAL")
        new_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()

        # Verify drift is detectable
        assert new_sha != initial_sha
        assert result["output_shas"][str(artifact_path)] != new_sha


class TestParityValidateGradePassthrough:
    """Verify parity_validate uses compute_grade and doesn't duplicate logic."""

    def test_grade_passthrough_parity(self, sample_evidence: dict):
        """Verify PARITY evidence -> grade='PARITY' (uses compute_grade)."""
        evidence = evidence_from_result(sample_evidence)
        grade_result = compute_grade(evidence)

        assert grade_result.grade == "PARITY"
        assert grade_result.ceilings["invariant"] == "PARITY"
        assert grade_result.ceilings["clause_parity"] == "PARITY"

    def test_grade_passthrough_fail(self):
        """Verify FAIL evidence -> grade='FAIL'."""
        evidence = ParityEvidence(
            clause_parity_pct=0.3,  # Below fail threshold
            adversarial_survived=True,
            invariant_pass=True,
            reproduction_rung="R1",
            ambiguity_load="none",
        )
        grade_result = compute_grade(evidence)

        assert grade_result.grade == "FAIL"
        assert grade_result.ceilings["clause_parity"] == "FAIL"

    def test_grade_passthrough_partial(self):
        """Verify PARTIAL evidence -> grade='PARTIAL'."""
        evidence = ParityEvidence(
            clause_parity_pct=0.8,  # Between partial and parity threshold
            adversarial_survived=True,
            invariant_pass=True,
            reproduction_rung="R1",
            ambiguity_load="none",
        )
        grade_result = compute_grade(evidence)

        assert grade_result.grade == "PARTIAL"
        assert grade_result.ceilings["clause_parity"] == "PARTIAL"

    def test_ceilings_auditable(self):
        """Verify ceilings dict is populated for audit."""
        evidence = ParityEvidence(
            clause_parity_pct=1.0,
            adversarial_survived=True,
            invariant_pass=True,
            reproduction_rung="R1",
            ambiguity_load="load_bearing",
        )
        grade_result = compute_grade(evidence)

        # All ceilings should be present and auditable
        assert "invariant" in grade_result.ceilings
        assert "clause_parity" in grade_result.ceilings
        assert "adversarial" in grade_result.ceilings
        assert "reproduction_rung" in grade_result.ceilings
        assert "ambiguity_load" in grade_result.ceilings

        # load_bearing ambiguity should cap to PARTIAL
        assert grade_result.ceilings["ambiguity_load"] == "PARTIAL"
        assert grade_result.grade == "PARTIAL"  # Min over all ceilings


class TestParityValidateParsing:
    """Verify parity_validate can parse the parity.bth.toml sidecar."""

    def test_parse_valid_sidecar(self, sample_parity_toml: Path):
        """Verify parsing a valid parity.bth.toml succeeds."""
        from bathos.parity import parse_parity_toml

        config = parse_parity_toml(sample_parity_toml)

        assert config["paper_pdf"] == "https://example.com/paper.pdf"
        assert config["impl_paths"] == ["src/impl.py", "src/utils.py"]
        assert config["citation_note"] == "Doe et al. 2025"
        assert config["equivalence_bound"] == 0.01
        assert config["N"] == 3  # Default
        assert config["M"] == 3  # Default

    def test_parse_missing_paper_pdf(self, temp_dir: Path):
        """Verify parsing fails when paper_pdf is missing."""
        from bathos.parity import parse_parity_toml

        bad_toml = temp_dir / "bad.bth.toml"
        bad_toml.write_text("[parity]\nimpl_paths = ['src/impl.py']")

        with pytest.raises(ValueError, match="paper_pdf is required"):
            parse_parity_toml(bad_toml)

    def test_parse_missing_impl_paths(self, temp_dir: Path):
        """Verify parsing fails when impl_paths is missing."""
        from bathos.parity import parse_parity_toml

        bad_toml = temp_dir / "bad.bth.toml"
        bad_toml.write_text('[parity]\npaper_pdf = "https://example.com/paper.pdf"')

        with pytest.raises(ValueError, match="impl_paths is required"):
            parse_parity_toml(bad_toml)


class TestParityValidateScriptIntegration:
    """Integration tests for parity_validate.py script."""

    def test_script_produces_result_json(self, sample_parity_toml: Path, sample_evidence: dict, temp_dir: Path):
        """Verify parity_validate.py script produces valid result JSON."""
        import subprocess
        import sys

        # Create evidence JSON
        evidence_json = temp_dir / "evidence.json"
        evidence_json.write_text(json.dumps(sample_evidence))

        # Run script
        result_file = temp_dir / "result.json"
        cmd = [
            sys.executable,
            "scripts/validation/parity_validate.py",
            "--parity-toml",
            str(sample_parity_toml),
            "--evidence-json",
            str(evidence_json),
            "--out",
            str(result_file),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert result_file.exists()

        # Parse and verify result
        result_data = json.loads(result_file.read_text())
        assert result_data["grade"] == "PARITY"
        assert "metadata" in result_data
        assert result_data["metadata"]["parity_run_type"] == "literature_parity"
        assert "output_paths" in result_data
        assert "output_shas" in result_data
        assert len(result_data["output_paths"]) == 3

    def test_script_creates_triple_artifacts(self, sample_parity_toml: Path, sample_evidence: dict, temp_dir: Path):
        """Verify script creates the three artifact files."""
        import subprocess
        import sys

        evidence_json = temp_dir / "evidence.json"
        evidence_json.write_text(json.dumps(sample_evidence))

        result_file = temp_dir / "result.json"
        cmd = [
            sys.executable,
            "scripts/validation/parity_validate.py",
            "--parity-toml",
            str(sample_parity_toml),
            "--evidence-json",
            str(evidence_json),
            "--out",
            str(result_file),
        ]

        subprocess.run(cmd, capture_output=True, text=True, check=True)

        # Verify artifacts exist
        checklist = temp_dir / "clause_checklist.json"
        verdict = temp_dir / "parity_verdict.md"
        invariant = temp_dir / "parity_invariant_test.py"

        assert checklist.exists(), "Checklist artifact not created"
        assert verdict.exists(), "Verdict artifact not created"
        assert invariant.exists(), "Invariant artifact not created"

        # Verify artifact contents
        checklist_data = json.loads(checklist.read_text())
        assert "paper_pdf" in checklist_data
        assert "impl_paths" in checklist_data

        verdict_text = verdict.read_text()
        assert "PARITY" in verdict_text or "PARTIAL" in verdict_text or "FAIL" in verdict_text

        invariant_text = invariant.read_text()
        assert "def test_invariant" in invariant_text

    def test_script_registers_shas(self, sample_parity_toml: Path, sample_evidence: dict, temp_dir: Path):
        """Verify script registers correct SHA256 hashes for artifacts."""
        import subprocess
        import sys

        evidence_json = temp_dir / "evidence.json"
        evidence_json.write_text(json.dumps(sample_evidence))

        result_file = temp_dir / "result.json"
        cmd = [
            sys.executable,
            "scripts/validation/parity_validate.py",
            "--parity-toml",
            str(sample_parity_toml),
            "--evidence-json",
            str(evidence_json),
            "--out",
            str(result_file),
        ]

        subprocess.run(cmd, capture_output=True, text=True, check=True)

        result_data = json.loads(result_file.read_text())

        # Verify each registered path has a matching SHA
        for path in result_data["output_paths"]:
            assert path in result_data["output_shas"]
            sha = result_data["output_shas"][path]
            assert len(sha) == 64  # SHA256 hex is 64 chars

            # Verify SHA matches actual file
            actual_sha = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            assert sha == actual_sha, f"SHA mismatch for {path}"
