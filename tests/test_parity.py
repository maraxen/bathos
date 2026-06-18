"""Tests for literature-parity v1 schema and grader."""

import json
import tempfile
from pathlib import Path

import pytest

from bathos.parity import (
    ParityEvidence,
    ParityGradeResult,
    compute_grade,
    evidence_from_result,
    parse_parity_toml,
)


class TestParityTomlValidation:
    """AC-01 and AC-02: Validate parity.bth.toml schema."""

    def test_parse_rejects_missing_paper_pdf(self):
        """AC-01: Validator rejects missing required field paper_pdf."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(
                """
[parity]
impl_paths = ["src/impl.py"]
"""
            )
            f.flush()
            path = Path(f.name)

        try:
            with pytest.raises(ValueError, match="paper_pdf"):
                parse_parity_toml(path)
        finally:
            path.unlink()

    def test_parse_rejects_missing_impl_paths(self):
        """AC-01: Validator rejects missing required field impl_paths."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(
                """
[parity]
paper_pdf = "papers/zeinaty2026.pdf"
"""
            )
            f.flush()
            path = Path(f.name)

        try:
            with pytest.raises(ValueError, match="impl_paths"):
                parse_parity_toml(path)
        finally:
            path.unlink()

    def test_parse_passes_valid_minimal_file(self):
        """AC-02: Validator passes a fully-valid minimal parity.bth.toml."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(
                """
[parity]
paper_pdf = "papers/zeinaty2026.pdf"
impl_paths = ["src/impl.py"]
"""
            )
            f.flush()
            path = Path(f.name)

        try:
            result = parse_parity_toml(path)
            assert result["paper_pdf"] == "papers/zeinaty2026.pdf"
            assert result["impl_paths"] == ["src/impl.py"]
        finally:
            path.unlink()

    def test_parse_passes_valid_complete_file(self):
        """AC-02: Validator passes a fully-specified parity.bth.toml."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(
                """
[parity]
paper_pdf = "papers/zeinaty2026.pdf"
impl_paths = ["src/impl.py", "src/utils.py"]
reference_code = "https://github.com/example/ref"
citation_note = "Zeinaty et al. 2026, ASR domain"
recon_lenses = ["math", "algo", "protocol"]
attack_lenses = ["stats", "hyper", "struct"]
hypotheses = ["mechanism-A", "mechanism-B"]
equivalence_bound = 0.05
N = 3
M = 3
"""
            )
            f.flush()
            path = Path(f.name)

        try:
            result = parse_parity_toml(path)
            assert result["paper_pdf"] == "papers/zeinaty2026.pdf"
            assert result["impl_paths"] == ["src/impl.py", "src/utils.py"]
            assert result["reference_code"] == "https://github.com/example/ref"
            assert result["citation_note"] == "Zeinaty et al. 2026, ASR domain"
            assert result["recon_lenses"] == ["math", "algo", "protocol"]
            assert result["attack_lenses"] == ["stats", "hyper", "struct"]
            assert result["hypotheses"] == ["mechanism-A", "mechanism-B"]
            assert result["equivalence_bound"] == 0.05
            assert result["N"] == 3
            assert result["M"] == 3
        finally:
            path.unlink()

    def test_parse_fills_defaults(self):
        """Validator fills in default values for optional fields."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(
                """
[parity]
paper_pdf = "papers/zeinaty2026.pdf"
impl_paths = ["src/impl.py"]
"""
            )
            f.flush()
            path = Path(f.name)

        try:
            result = parse_parity_toml(path)
            assert result["reference_code"] is None
            assert result["citation_note"] == ""
            assert result["recon_lenses"] == ["math", "algo", "protocol"]
            assert result["attack_lenses"] == ["stats", "hyper", "struct"]
            assert result["hypotheses"] == []
            assert result["equivalence_bound"] is None
            assert result["N"] == 3
            assert result["M"] == 3
        finally:
            path.unlink()

    def test_parse_empty_parity_section_rejected(self):
        """Validator rejects file with empty [parity] section."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write("[parity]\n")
            f.flush()
            path = Path(f.name)

        try:
            with pytest.raises(ValueError, match="paper_pdf|impl_paths"):
                parse_parity_toml(path)
        finally:
            path.unlink()


class TestComputeGrade:
    """AC-03, AC-04, AC-05, AC-18: Test the X1 cap-lattice grader."""

    def test_compute_grade_fails_when_invariant_fails(self):
        """AC-03: compute_grade returns FAIL when invariant_pass=False."""
        evidence = ParityEvidence(
            clause_parity_pct=1.0,
            adversarial_survived=True,
            invariant_pass=False,
            reproduction_rung="R0",
            ambiguity_load="none",
        )
        result = compute_grade(evidence)
        assert result.grade == "FAIL"

    def test_compute_grade_partial_cap_for_load_bearing_ambiguity(self):
        """AC-04: compute_grade caps at PARTIAL when ambiguity_load='load_bearing'."""
        evidence = ParityEvidence(
            clause_parity_pct=1.0,
            adversarial_survived=True,
            invariant_pass=True,
            reproduction_rung="R0",
            ambiguity_load="load_bearing",
        )
        result = compute_grade(evidence)
        assert result.grade == "PARTIAL"

    def test_compute_grade_parity_on_all_clear(self):
        """AC-05: compute_grade returns PARITY on all-clear evidence."""
        evidence = ParityEvidence(
            clause_parity_pct=1.0,
            adversarial_survived=True,
            invariant_pass=True,
            reproduction_rung="R0",
            ambiguity_load="none",
        )
        result = compute_grade(evidence)
        assert result.grade == "PARITY"

    def test_compute_grade_partial_cap_for_rung_r2(self):
        """AC-18: compute_grade caps at PARTIAL when reproduction_rung='R2'."""
        evidence = ParityEvidence(
            clause_parity_pct=1.0,
            adversarial_survived=True,
            invariant_pass=True,
            reproduction_rung="R2",
            ambiguity_load="none",
        )
        result = compute_grade(evidence)
        assert result.grade == "PARTIAL"

    def test_compute_grade_partial_cap_for_rung_r3(self):
        """Rung R3 (worse than R1) also caps at PARTIAL."""
        evidence = ParityEvidence(
            clause_parity_pct=1.0,
            adversarial_survived=True,
            invariant_pass=True,
            reproduction_rung="R3",
            ambiguity_load="none",
        )
        result = compute_grade(evidence)
        assert result.grade == "PARTIAL"

    def test_compute_grade_partial_cap_for_rung_r4(self):
        """Rung R4 (worst) also caps at PARTIAL."""
        evidence = ParityEvidence(
            clause_parity_pct=1.0,
            adversarial_survived=True,
            invariant_pass=True,
            reproduction_rung="R4",
            ambiguity_load="none",
        )
        result = compute_grade(evidence)
        assert result.grade == "PARTIAL"

    def test_compute_grade_parity_for_r0_r1(self):
        """Rungs R0 and R1 do not cap the grade."""
        # R0
        evidence_r0 = ParityEvidence(
            clause_parity_pct=1.0,
            adversarial_survived=True,
            invariant_pass=True,
            reproduction_rung="R0",
            ambiguity_load="none",
        )
        result_r0 = compute_grade(evidence_r0)
        assert result_r0.grade == "PARITY"

        # R1
        evidence_r1 = ParityEvidence(
            clause_parity_pct=1.0,
            adversarial_survived=True,
            invariant_pass=True,
            reproduction_rung="R1",
            ambiguity_load="none",
        )
        result_r1 = compute_grade(evidence_r1)
        assert result_r1.grade == "PARITY"

    def test_compute_grade_fails_when_adversarial_not_survived(self):
        """Adversarial refutation failure (landed refutation) is a FAIL ceiling by design."""
        evidence = ParityEvidence(
            clause_parity_pct=1.0,
            adversarial_survived=False,
            invariant_pass=True,
            reproduction_rung="R0",
            ambiguity_load="none",
        )
        result = compute_grade(evidence)
        # Per design contract: landed refutation (adversarial_survived=False) negates a core claim → FAIL ceiling
        assert result.grade == "FAIL"

    def test_compute_grade_includes_ceiling_breakdown(self):
        """Result includes ceilings dict for auditability."""
        evidence = ParityEvidence(
            clause_parity_pct=0.8,
            adversarial_survived=True,
            invariant_pass=True,
            reproduction_rung="R0",
            ambiguity_load="non_load_bearing",
        )
        result = compute_grade(evidence)
        assert isinstance(result.ceilings, dict)
        assert len(result.ceilings) > 0

    def test_compute_grade_low_clause_parity_caps(self):
        """Low clause_parity_pct should cap toward PARTIAL/FAIL."""
        # Test threshold behavior: < 0.5 should cap more severely
        evidence_very_low = ParityEvidence(
            clause_parity_pct=0.3,
            adversarial_survived=True,
            invariant_pass=True,
            reproduction_rung="R0",
            ambiguity_load="none",
        )
        result_very_low = compute_grade(evidence_very_low)
        # Should not be PARITY with very low clause parity
        assert result_very_low.grade in ("FAIL", "PARTIAL")

    def test_compute_grade_minimum_over_dimensions(self):
        """Grade is the minimum (most severe) ceiling across all dimensions."""
        # Multiple failing conditions -> final grade is minimum
        evidence = ParityEvidence(
            clause_parity_pct=0.2,  # Very low -> FAIL ceiling
            adversarial_survived=True,
            invariant_pass=True,
            reproduction_rung="R2",  # -> PARTIAL ceiling
            ambiguity_load="none",
        )
        result = compute_grade(evidence)
        # Minimum of FAIL and PARTIAL is FAIL
        assert result.grade == "FAIL"


class TestEvidenceFromResult:
    """Test building evidence from a parity run result JSON."""

    def test_evidence_from_result_with_all_fields(self):
        """evidence_from_result tolerantly builds evidence from a result dict."""
        result = {
            "clause_parity_pct": 0.95,
            "adversarial_survived": True,
            "invariant_pass": True,
            "reproduction_rung": "R0",
            "ambiguity_load": "none",
        }
        evidence = evidence_from_result(result)
        assert evidence.clause_parity_pct == 0.95
        assert evidence.adversarial_survived is True
        assert evidence.invariant_pass is True
        assert evidence.reproduction_rung == "R0"
        assert evidence.ambiguity_load == "none"

    def test_evidence_from_result_with_missing_keys(self):
        """evidence_from_result uses sane defaults for missing keys."""
        result = {
            "clause_parity_pct": 0.8,
            # other keys missing
        }
        evidence = evidence_from_result(result)
        assert evidence.clause_parity_pct == 0.8
        # Defaults for missing keys:
        assert evidence.adversarial_survived is False  # Fail-safe
        assert evidence.invariant_pass is False  # Fail-safe
        assert evidence.reproduction_rung == "R4"  # Worst rung
        assert evidence.ambiguity_load == "load_bearing"  # Most conservative

    def test_evidence_from_result_from_json_string(self):
        """evidence_from_result correctly maps flat dict keys to evidence fields."""
        # Test the flat-dict → evidence mapping with a subset of keys
        metadata_json = json.dumps(
            {
                "clause_parity_pct": 0.92,
                "adversarial_survived": True,
                "invariant_pass": True,
                "reproduction_rung": "R1",
                "ambiguity_load": "non_load_bearing",
            }
        )
        parity_data = json.loads(metadata_json)
        evidence = evidence_from_result(parity_data)
        # Verify that all fields map correctly
        assert evidence.clause_parity_pct == 0.92
        assert evidence.adversarial_survived is True
        assert evidence.invariant_pass is True
        assert evidence.reproduction_rung == "R1"
        assert evidence.ambiguity_load == "non_load_bearing"

    def test_evidence_from_result_empty_dict(self):
        """evidence_from_result handles completely empty result."""
        result = {}
        evidence = evidence_from_result(result)
        # All should be defaults (fail-safe)
        assert evidence.clause_parity_pct == 0.0
        assert evidence.adversarial_survived is False
        assert evidence.invariant_pass is False
        assert evidence.reproduction_rung == "R4"
        assert evidence.ambiguity_load == "load_bearing"


class TestParityGradeResult:
    """Test ParityGradeResult dataclass."""

    def test_parity_grade_result_structure(self):
        """ParityGradeResult has grade and ceilings fields."""
        result = ParityGradeResult(grade="PARITY", ceilings={"dimension1": "PARITY"})
        assert result.grade == "PARITY"
        assert isinstance(result.ceilings, dict)
