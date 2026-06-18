"""Literature-parity v1: schema, validator, and cap-lattice grader.

This module provides:
- parse_parity_toml(): Parses and validates parity.bth.toml files
- ParityEvidence: Dataclass for evidence dimensions
- ParityGradeResult: Result of grading (PARITY/PARTIAL/FAIL)
- compute_grade(): X1 cap-lattice grader implementation
- evidence_from_result(): Builds evidence from a result dict
- check_parity_confounds_for_submit(): F3 submit-gate for parity prerequisite
"""

from __future__ import annotations

import json
import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


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
_CLAUSE_PARITY_PARTIAL_THRESHOLD = 1.0  # < 1.0 -> PARTIAL ceiling; intentional v1 conservatism — only perfect clause parity (==1.0) scores PARITY on this dimension; threshold is provisional/tunable


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
    # A landed refutation (adversarial_survived=False) is a FAIL ceiling by design: it negates a core claim.
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
            data = tomllib.load(f)
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


def check_parity_confounds_for_submit(sidecar, catalog_dir: Path) -> dict:
    """F3 submit-gate: check if required parity run exists.

    If the sidecar declares a parity prerequisite (requires_parity_stem),
    look for a PASSING parity run for that stem in the catalog.

    Uses warm-then-cool fallback pattern (mirroring prereg.py:178):
    - Warm path: DuckDB query on bathos.db (fast)
    - Cool-tier fallback: PyArrow glob scan of runs/**/*.parquet (slower)

    Returns a structured result dict:
        satisfied (bool|None): True if a passing parity run found; False if not found;
                               None if indeterminate (both tiers unavailable).
        tier_enforced (bool): True if hard-block should apply (validation/production tier
                             and prerequisite unmet); False otherwise (advisory or satisfied).

    AC-09/AC-10: tier logic—
        - validation/production + unmet + determinable -> tier_enforced=True (hard block)
        - exploration/calibration + unmet -> tier_enforced=False (advisory)
        - satisfied (regardless of tier) -> tier_enforced=False (no block)

    AC-22: If warm DB doesn't exist AND we can't search cool tier, return satisfied=None
           and tier_enforced=False (fail open, never hard-block).

    Args:
        sidecar: Parsed Sidecar object with optional reproduction block
        catalog_dir: Path to catalog directory (~/.bth/catalog)

    Returns:
        Dict with keys: satisfied (bool|None), tier_enforced (bool)
    """
    import duckdb
    import pyarrow.parquet as pq

    # If no reproduction block or no requires_parity_stem, gate is satisfied (no check needed)
    if not sidecar.reproduction or not sidecar.reproduction.requires_parity_stem:
        return {"satisfied": True, "tier_enforced": False}

    requires_parity_stem = sidecar.reproduction.requires_parity_stem
    stage_name = sidecar.stage_name or "exploration"

    # Determine if tier is enforced (validation/production = enforced, others = advisory)
    is_validation_or_production = stage_name in ("validation", "production")

    db_path = catalog_dir / "bathos.db"

    # Warm path: query DuckDB if available
    if db_path.exists():
        try:
            with duckdb.connect(str(db_path), read_only=True) as conn:
                # Query for a passing parity run matching the stem.
                # Use the parity_run_type COLUMN (not json_extract on metadata —
                # metadata JSON is NULL after cool→warm compaction, making the old
                # json_extract path always fail on compacted runs).
                rows = conn.execute(
                    "SELECT 1 FROM runs WHERE command LIKE ? AND outcome = 'pass' AND "
                    "parity_run_type = 'literature_parity' LIMIT 1",
                    [f"%{requires_parity_stem}%"]
                ).fetchall()
                if rows:
                    # Found a passing parity run
                    return {"satisfied": True, "tier_enforced": False}
                # Warm DB exists but no match found -> prerequisite unmet, determinable
                return {
                    "satisfied": False,
                    "tier_enforced": is_validation_or_production
                }
        except Exception as e:
            logger.warning(f"Warm tier parity prerequisite check failed: {e}")

    # Cool-tier fallback: scan Parquet files (only if warm DB not available).
    # AC-22 restructure: track fragments_read_ok to distinguish:
    #   - "no readable fragments" (unsearchable → fail open, satisfied=None)
    #   - "scanned clean, no match" (determinable → satisfied=False)
    # The old inner except…continue could not make this distinction.
    fragments_read_ok = 0
    try:
        runs_dir = catalog_dir / "runs"
        if runs_dir.exists():
            for parquet_file in sorted(runs_dir.glob("**/*.parquet")):
                try:
                    # Read parity_run_type as a first-class column (v9 schema).
                    # The metadata column does NOT exist in cool-tier fragments —
                    # attempting to read it raises on every real fragment (which was
                    # the spurious hard-block bug the AC-22 restructure fixes).
                    table = pq.read_table(
                        str(parquet_file),
                        columns=["command", "outcome", "parity_run_type"]
                    )
                    fragments_read_ok += 1
                    cmds = table.column("command").to_pylist()
                    outcomes = table.column("outcome").to_pylist()
                    parity_types = table.column("parity_run_type").to_pylist()

                    for cmd, outcome, parity_type in zip(cmds, outcomes, parity_types):
                        if (outcome == "pass"
                                and cmd
                                and requires_parity_stem in cmd
                                and parity_type == "literature_parity"):
                            return {"satisfied": True, "tier_enforced": False}
                except Exception as e:
                    logger.warning(f"Failed to read {parquet_file}: {e}")
                    continue
    except Exception as e:
        logger.warning(f"Cool tier parity prerequisite check failed: {e}")

    # AC-22: if no fragment could be read → unsearchable → fail open (advisory, never hard-block)
    if fragments_read_ok == 0:
        return {"satisfied": None, "tier_enforced": False}

    # ≥1 fragment read OK with no match → determinable, prerequisite unmet
    return {
        "satisfied": False,
        "tier_enforced": is_validation_or_production
    }
