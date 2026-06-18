#!/usr/bin/env python3
"""Parity validation runner: grade parity evidence and emit SHA-anchored triple.

This script loads a parity.bth.toml, constructs a ParityEvidence object from
evidence inputs (or via JSON), computes a grade using the X1 cap-lattice grader,
and emits the result JSON with:
  - grade (PARITY/PARTIAL/FAIL)
  - ceilings (per-dimension auditable ceilings)
  - metadata.parity_run_type='literature_parity' (B1, AC-19)
  - output_paths and output_shas (SHA-anchored triple, AC-15)
  - three artifact files (checklist, verdict, invariant pytest)

Usage:
  uv run python scripts/validation/parity_validate.py \
    --parity-toml path/to/parity.bth.toml \
    --evidence-json path/to/evidence.json \
    --out path/to/result.json

The evidence JSON should have keys:
  - clause_parity_pct (float 0-1)
  - adversarial_survived (bool)
  - invariant_pass (bool)
  - reproduction_rung (str: R0-R4)
  - ambiguity_load (str: none/non_load_bearing/load_bearing)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path

from bathos.parity import (
    parse_parity_toml,
    evidence_from_result,
    compute_grade,
)

logger = logging.getLogger(__name__)


def _compute_sha256(path: Path) -> str:
    """Compute SHA256 hash of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_checklist_artifact(output_dir: Path, parity_config: dict) -> Path:
    """Create clause checklist JSON artifact.

    For v1, this is a stub artifact listing the implementation paths from config.
    In future, it would be populated by the 5-phase workflow.
    """
    checklist_path = output_dir / "clause_checklist.json"
    checklist = {
        "paper_pdf": parity_config.get("paper_pdf", ""),
        "impl_paths": parity_config.get("impl_paths", []),
        "citation_note": parity_config.get("citation_note", ""),
        "clauses": [],  # Populated by 5-phase workflow in v2+
    }
    checklist_path.write_text(json.dumps(checklist, indent=2))
    return checklist_path


def _create_verdict_artifact(output_dir: Path, grade: str, ceilings: dict) -> Path:
    """Create graded verdict markdown artifact."""
    verdict_path = output_dir / "parity_verdict.md"
    verdict_md = f"""# Literature-Parity Grading Verdict

## Final Grade

**{grade}**

## Dimension Ceilings

"""
    for dim, ceiling in sorted(ceilings.items()):
        verdict_md += f"- **{dim}**: {ceiling}\n"

    verdict_md += """
## Notes

This verdict was computed by the X1 cap-lattice grader using evidence from the
5-phase workflow (reconstruction → reconciliation → refutation → adjudication).
Final grade = minimum (most severe) ceiling across all dimensions.

Grade meanings:
- **PARITY**: Implementation matches paper method; suitable for baseline comparison.
- **PARTIAL**: Implementation has controlled differences; usable with caveats.
- **FAIL**: Implementation diverges from paper; not suitable for parity.
"""
    verdict_path.write_text(verdict_md)
    return verdict_path


def _create_invariant_artifact(output_dir: Path) -> Path:
    """Create invariant pytest stub for v1.

    For v1, this is a template that can be extended by users. In future,
    it would be auto-populated by the refutation phase.
    """
    invariant_path = output_dir / "parity_invariant_test.py"
    invariant_code = '''"""Invariant test for literature-parity validation.

This test verifies that the implementation preserves the formal invariant
defined in the paper. For v1, this is a stub that users should extend.

In future, the 5-phase workflow will auto-populate this based on the
refutation phase findings.
"""

import pytest


def test_invariant_placeholder():
    """Stub: replace with actual invariant from paper."""
    # TODO: Extract and implement the formal invariant from the paper
    # e.g., assert conservation_of_energy, numerical_stability, etc.
    pytest.skip("Invariant test not yet implemented for this paper")


def test_method_bounds():
    """Stub: verify method bounds from paper."""
    # TODO: Implement bounds checking from paper specification
    pytest.skip("Method bounds test not yet implemented")
'''
    invariant_path.write_text(invariant_code)
    return invariant_path


def main():
    """Main entry point for parity validation runner."""
    parser = argparse.ArgumentParser(
        description="Grade parity evidence and emit result JSON with SHA-anchored triple."
    )
    parser.add_argument(
        "--parity-toml",
        type=Path,
        required=True,
        help="Path to parity.bth.toml config file",
    )
    parser.add_argument(
        "--evidence-json",
        type=Path,
        help="Path to JSON file with evidence dimensions",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output path for result JSON. For SHA-anchored triple to be auto-registered "
             "by runner.py, place this inside $BTH_OUTPUT_DIR.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(name)s - %(levelname)s - %(message)s",
    )

    logger.info(f"Starting parity validation: {args.parity_toml}")

    # Parse parity configuration
    try:
        parity_config = parse_parity_toml(args.parity_toml)
        logger.info(f"Loaded parity config: {parity_config['paper_pdf']}")
    except (FileNotFoundError, ValueError) as e:
        logger.error(f"Failed to parse parity.bth.toml: {e}")
        return 1

    # Load evidence
    evidence_dict = {}
    if args.evidence_json:
        try:
            evidence_dict = json.loads(args.evidence_json.read_text())
            logger.info(f"Loaded evidence from {args.evidence_json}")
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load evidence JSON: {e}")
            return 1
    else:
        # Use all defaults (fail-safe)
        logger.warning("No evidence JSON provided; using fail-safe defaults")

    # Build evidence and compute grade
    evidence = evidence_from_result(evidence_dict)
    grade_result = compute_grade(evidence)

    logger.info(f"Grading complete: {grade_result.grade}")
    logger.debug(f"Ceilings: {grade_result.ceilings}")

    # Create output directory (use parent of --out)
    output_dir = args.out.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    # Create artifacts (the SHA-anchored triple)
    checklist_path = _create_checklist_artifact(output_dir, parity_config)
    verdict_path = _create_verdict_artifact(output_dir, grade_result.grade, grade_result.ceilings)
    invariant_path = _create_invariant_artifact(output_dir)

    # Compute SHAs
    checklist_sha = _compute_sha256(checklist_path)
    verdict_sha = _compute_sha256(verdict_path)
    invariant_sha = _compute_sha256(invariant_path)

    logger.info(f"Created artifacts: {checklist_path.name}, {verdict_path.name}, {invariant_path.name}")

    # Build result JSON with metadata.parity_run_type (AC-19, B1)
    result = {
        "grade": grade_result.grade,
        "ceilings": grade_result.ceilings,
        "metadata": {
            "parity_run_type": "literature_parity",
        },
        "output_paths": [
            str(checklist_path.resolve()),
            str(verdict_path.resolve()),
            str(invariant_path.resolve()),
        ],
        "output_shas": {
            str(checklist_path.resolve()): checklist_sha,
            str(verdict_path.resolve()): verdict_sha,
            str(invariant_path.resolve()): invariant_sha,
        },
    }

    # Write result JSON
    try:
        args.out.write_text(json.dumps(result, indent=2))
        logger.info(f"Result written to {args.out}")
    except (OSError, IOError) as e:
        logger.error(f"Failed to write result JSON: {e}")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
