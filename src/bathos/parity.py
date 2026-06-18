"""Literature-parity v1: schema, validator, and cap-lattice grader.

This module provides:
- parse_parity_toml(): Parses and validates parity.bth.toml files
- ParityEvidence: Dataclass for evidence dimensions
- ParityGradeResult: Result of grading (PARITY/PARTIAL/FAIL)
- compute_grade(): X1 cap-lattice grader implementation
- evidence_from_result(): Builds evidence from a result dict
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ParityEvidence:
    """Evidence dimensions for the X1 cap-lattice grader.

    Fields:
        clause_parity_pct: Fraction of clauses matching between paper and impl (0-1).
        adversarial_survived: Whether the implementation survived refutation attacks.
        invariant_pass: Whether the formal invariant held (hard floor if False).
        reproduction_rung: One of "R0", "R1", "R2", "R3", "R4" (worse = more severe).
        ambiguity_load: One of "none", "non_load_bearing", "load_bearing".
    """

    clause_parity_pct: float
    adversarial_survived: bool
    invariant_pass: bool
    reproduction_rung: str
    ambiguity_load: str


@dataclass
class ParityGradeResult:
    """Result of parity grading.

    Fields:
        grade: Final grade: "PARITY", "PARTIAL", or "FAIL".
        ceilings: Dict of per-dimension ceilings for auditability.
    """

    grade: str
    ceilings: dict[str, str] = field(default_factory=dict)


# Ceiling thresholds (documented as provisional, tunable)
# These encode the cap-lattice logic: each dimension has a ceiling that caps the final grade.
_CLAUSE_PARITY_FAIL_THRESHOLD = 0.5  # < 0.5 -> FAIL ceiling
_CLAUSE_PARITY_PARTIAL_THRESHOLD = 1.0  # < 1.0 -> PARTIAL ceiling
_ADVERSARIAL_FAIL_CEILING = True  # adversarial_survived=False -> FAIL ceiling


def compute_grade(evidence: ParityEvidence) -> ParityGradeResult:
    """Compute parity grade using X1 cap-lattice.

    Implements the cap-lattice grading logic (Design doc X1 section):
    - invariant_pass=False -> FAIL (hard floor)
    - clause_parity_pct thresholds -> cap ceilings
    - adversarial_survived=False -> FAIL or PARTIAL ceiling
    - reproduction_rung worse than R1 (R2/R3/R4) -> max PARTIAL ceiling
    - ambiguity_load="load_bearing" -> max PARTIAL ceiling
    - Final grade = MIN over all ceilings (most severe)

    Args:
        evidence: ParityEvidence with all dimensions

    Returns:
        ParityGradeResult with grade (PARITY/PARTIAL/FAIL) and ceilings dict
    """
    ceilings = {}

    # Dimension 1: Invariant (hard floor)
    if not evidence.invariant_pass:
        ceilings["invariant"] = "FAIL"
    else:
        ceilings["invariant"] = "PARITY"

    # Dimension 2: Clause Parity
    if evidence.clause_parity_pct < _CLAUSE_PARITY_FAIL_THRESHOLD:
        ceilings["clause_parity"] = "FAIL"
    elif evidence.clause_parity_pct < _CLAUSE_PARITY_PARTIAL_THRESHOLD:
        ceilings["clause_parity"] = "PARTIAL"
    else:
        ceilings["clause_parity"] = "PARITY"

    # Dimension 3: Adversarial Survival
    if not evidence.adversarial_survived:
        # Refutation that lands is serious; treat as FAIL ceiling (conservative)
        ceilings["adversarial"] = "FAIL"
    else:
        ceilings["adversarial"] = "PARITY"

    # Dimension 4: Reproduction Rung (R0/R1 OK, worse -> PARTIAL cap)
    if evidence.reproduction_rung in ("R0", "R1"):
        ceilings["reproduction_rung"] = "PARITY"
    elif evidence.reproduction_rung in ("R2", "R3", "R4"):
        ceilings["reproduction_rung"] = "PARTIAL"
    else:
        # Unknown rung: default to PARTIAL (conservative)
        ceilings["reproduction_rung"] = "PARTIAL"

    # Dimension 5: Ambiguity Load
    if evidence.ambiguity_load == "load_bearing":
        ceilings["ambiguity_load"] = "PARTIAL"
    else:
        ceilings["ambiguity_load"] = "PARITY"

    # Final grade = minimum (most severe) ceiling
    ceiling_order = {"FAIL": 0, "PARTIAL": 1, "PARITY": 2}
    min_ceiling = min(ceiling_order[c] for c in ceilings.values())
    grade_map = {0: "FAIL", 1: "PARTIAL", 2: "PARITY"}
    final_grade = grade_map[min_ceiling]

    return ParityGradeResult(grade=final_grade, ceilings=ceilings)


def evidence_from_result(result: dict) -> ParityEvidence:
    """Build ParityEvidence from a parity run result dict.

    Tolerant of missing keys; uses fail-safe defaults.

    Args:
        result: Dict-like object with parity run results

    Returns:
        ParityEvidence with all required fields
    """
    return ParityEvidence(
        clause_parity_pct=result.get("clause_parity_pct", 0.0),
        adversarial_survived=result.get("adversarial_survived", False),
        invariant_pass=result.get("invariant_pass", False),
        reproduction_rung=result.get("reproduction_rung", "R4"),
        ambiguity_load=result.get("ambiguity_load", "load_bearing"),
    )


def parse_parity_toml(path: Path) -> dict:
    """Parse and validate a parity.bth.toml file.

    Reads the [parity] section and validates required fields.
    Fills in defaults for optional fields.

    Schema (fields with required indicator):
        paper_pdf (str, REQUIRED): Path to the paper PDF
        impl_paths (list[str], REQUIRED): List of implementation file paths
        reference_code (str|None, optional): URL or ref to reference implementation
        citation_note (str, optional): Citation or note about the paper
        recon_lenses (list[str], default): List of reconnaissance lenses
        attack_lenses (list[str], default): List of attack lenses
        hypotheses (list[str], optional): List of mechanism hypotheses
        equivalence_bound (float|None, optional): Numeric equivalence threshold
        N (int, default 3): Recon phase N value (tunable)
        M (int, default 3): Attack phase M value (tunable)

    Args:
        path: Path to parity.bth.toml file

    Returns:
        Dict with parsed and validated fields

    Raises:
        ValueError: If required fields are missing
        FileNotFoundError: If file does not exist
    """
    if not path.exists():
        raise FileNotFoundError(f"Parity file not found at {path}")

    try:
        with open(path, "rb") as f:
            content = f.read()
            data = tomllib.loads(content.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"Failed to parse parity TOML at {path}: {e}") from e

    parity_section = data.get("parity", {})

    # Validate required fields
    paper_pdf = parity_section.get("paper_pdf")
    if not paper_pdf or not isinstance(paper_pdf, str):
        raise ValueError("parity.bth.toml: paper_pdf is required (string)")

    impl_paths = parity_section.get("impl_paths")
    if not impl_paths or not isinstance(impl_paths, list):
        raise ValueError("parity.bth.toml: impl_paths is required (list of strings)")

    # Parse optional fields with defaults
    return {
        "paper_pdf": paper_pdf,
        "impl_paths": impl_paths,
        "reference_code": parity_section.get("reference_code"),
        "citation_note": parity_section.get("citation_note", ""),
        "recon_lenses": parity_section.get("recon_lenses", ["math", "algo", "protocol"]),
        "attack_lenses": parity_section.get("attack_lenses", ["stats", "hyper", "struct"]),
        "hypotheses": parity_section.get("hypotheses", []),
        "equivalence_bound": parity_section.get("equivalence_bound"),
        "N": parity_section.get("N", 3),
        "M": parity_section.get("M", 3),
    }
