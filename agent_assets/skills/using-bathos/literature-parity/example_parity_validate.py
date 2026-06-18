#!/usr/bin/env python
"""
Example parity validation runner.

This illustrates the structure of a literature-parity validation workflow.
The real validator (planned in T4) lives in scripts/validation/parity_validate.py
and is more fully featured (orchestrates agents, computes grades, etc.).

This example shows:
1. How to load and parse parity.bth.toml
2. How to structure the 5-phase workflow
3. How to produce output suitable for a bathos run
"""

import json
import sys
from pathlib import Path
from typing import Any

from bathos.parity import ParityEvidence, compute_grade


def load_parity_config(config_path: str) -> dict[str, Any]:
    """Load parity.bth.toml configuration."""
    # In a real implementation, use tomllib or rtoml to parse the TOML file
    # For this example, we just show the structure
    example_config = {
        "paper_pdf": "path/to/paper.pdf",
        "impl_paths": ["src/method.py"],
        "recon_lenses": ["math", "algo", "protocol"],
        "attack_lenses": ["stats", "hyper", "struct"],
        "N": 3,  # reconstructors
        "M": 3,  # refutation attackers
        "equivalence_bound": 0.05,
        "hypotheses": ["core mechanism is faithful"],
    }
    return example_config


def phase1_blind_reconstruction(config: dict) -> list[dict]:
    """
    Phase 1: Orchestrate N independent reconstruction agents.

    Each agent reads paper_pdf ONLY and produces:
      - Mathematical formulation
      - Algorithmic detail
      - Experimental protocol
      - Ambiguities encountered
    """
    reconstructions = []
    for i in range(config["N"]):
        reconstruction = {
            "agent_id": f"reconstructor_{i+1}",
            "lens": config["recon_lenses"],
            "output": {
                "mathematical_formulation": "...",
                "algorithmic_detail": "...",
                "experimental_protocol": "...",
                "ambiguities": [],
            },
        }
        reconstructions.append(reconstruction)
    return reconstructions


def phase2_reconcile(config: dict, reconstructions: list[dict]) -> dict:
    """
    Phase 2: Compare reconstructions and map to code.

    Produces a clause checklist mapping each reconstructed element
    to code with a verdict: MATCH / DEVIATION / MISSING / AMBIGUOUS
    """
    checklist = {
        "clauses": [
            {
                "element": "core_mechanism",
                "source": "recon_1",
                "code_location": "src/method.py:42",
                "verdict": "MATCH",
                "notes": "",
            },
            {
                "element": "metric_readout",
                "source": "recon_1",
                "code_location": "src/method.py:150",
                "verdict": "AMBIGUOUS",
                "notes": "Paper does not specify exact metric computation",
            },
        ],
        "match_count": 1,
        "deviation_count": 0,
        "missing_count": 0,
        "ambiguous_count": 1,
        "total": 2,
    }
    return checklist


def phase3_adversarial_refutation(config: dict, checklist: dict) -> list[dict]:
    """
    Phase 3: Orchestrate M independent refutation agents.

    Each agent assumes a defect and tries to prove it using an attack lens:
      - statistical_correctness
      - hyperparameter_fidelity
      - algorithmic_structure
    """
    refutations = []
    for i in range(config["M"]):
        refutation = {
            "agent_id": f"attacker_{i+1}",
            "attack_lens": config["attack_lenses"][i % len(config["attack_lenses"])],
            "assumption": "Metric readout is invariant to core mechanism",
            "evidence": {
                "test_result": "metric unchanged when mechanism ablated",
                "confidence": "high",
            },
            "verdict": "DEFECT_SUSPECTED",
            "severity": "critical",
            "recommendation": "Investigate metric computation",
        }
        refutations.append(refutation)
    return refutations


def phase4_adjudicate(config: dict, refutations: list[dict]) -> dict:
    """
    Phase 4: Confirm findings and rank severity.

    - Tally how many attackers found the same defect
    - Collect hard evidence (runnable invariant tests)
    - Rank severity (critical / major / minor / accepted)
    """
    adjudication = {
        "defects_confirmed": [
            {
                "name": "metric_invariant_to_mechanism",
                "evidence": "3/3 attackers found this; orchestrator wrote test_invariants.py and confirmed",
                "severity": "critical",
                "recommendation": "Fix metric computation or accept as limitation",
            }
        ],
        "verdict_direction": "FAIL",
        "notes": "Core mechanism appears disconnected from reported metrics",
    }
    return adjudication


def phase5_graded_verdict(config: dict, checklist: dict, adjudication: dict) -> dict:
    """
    Phase 5: Compute grade and produce verdict.

    Uses the official cap-lattice grader (compute_grade) from bathos.parity.
    Demonstrates the correct API: build ParityEvidence from evidence dimensions,
    call compute_grade(), and extract grade and ceilings.
    """
    # Compute clause-parity as a fraction (0-1)
    match_pct_fraction = checklist["match_count"] / checklist["total"]

    # Determine adversarial survival from adjudication findings
    adversarial_survived = not (adjudication.get("verdict_direction") == "FAIL")

    # Build evidence and call the official grader
    evidence = ParityEvidence(
        clause_parity_pct=match_pct_fraction,
        adversarial_survived=adversarial_survived,
        invariant_pass=True,  # placeholder; would be set from orchestrator-run tests
        reproduction_rung="R0",  # placeholder; depends on context
        ambiguity_load="load_bearing" if checklist["ambiguous_count"] > 0 else "none",
    )
    grade_result = compute_grade(evidence)

    # Extract final grade and ceilings for auditability
    match_pct = match_pct_fraction * 100
    verdict = {
        "grade": grade_result.grade,
        "clause_parity_pct": match_pct,
        "ceilings": grade_result.ceilings,
        "defects_confirmed": adjudication.get("defects_confirmed", []),
        "invariant_tests": "tests/test_method_invariants.py (PASS)",
        "reproduce_plan": "See YYMMDD_paper-parity-verdict.md",
        "confounds_reference_parity_block": {
            "reference_paper": "Author YEAR",
            "reference_metric": "accuracy",
            "reference_value": 0.85,
            "equivalence_bound": 0.05,
            "parity_run_id": "<run_uuid>",  # assigned by bathos
        },
    }
    return verdict


def main():
    """
    Run the full 5-phase protocol.

    In practice, this would be orchestrated by the operator using agent prompts.
    This example shows the data flow and output structure.
    """

    # Load configuration
    config_path = "parity.bth.toml"
    config = load_parity_config(config_path)

    # Phase 1: Blind reconstructions
    reconstructions = phase1_blind_reconstruction(config)

    # Phase 2: Reconcile
    checklist = phase2_reconcile(config, reconstructions)

    # Phase 3: Adversarial refutation
    refutations = phase3_adversarial_refutation(config, checklist)

    # Phase 4: Adjudicate
    adjudication = phase4_adjudicate(config, refutations)

    # Phase 5: Graded verdict
    verdict = phase5_graded_verdict(config, checklist, adjudication)

    # Output result JSON (for bth run --out)
    result = {
        "parity_grade": verdict["grade"],
        "clause_parity_pct": verdict["clause_parity_pct"],
        "defects_confirmed": [d["name"] for d in verdict.get("defects_confirmed", [])],
        "invariant_tests": verdict["invariant_tests"],
        "success": verdict["grade"] in ["PARITY", "PARTIAL"],
    }

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
