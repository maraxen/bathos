"""Placeholder payloads for freshly scaffolded documents.

This is bathos policy -- which fields a new claim prompts for, and the wording of those
prompts -- expressed as a payload rather than as a template string. It imports only
:mod:`bathos.authoring.models`, so the generic render/model layer stays liftable.

The ``REQUIRED:`` / ``Optional:`` prefixes are load-bearing: ``_PLACEHOLDER_LABEL_RE`` in
``bathos.claim`` matches on ``REQUIRED:`` to tell an unfilled scaffold apart from a real
label, and the scaffold round-trip test treats them as the expected validation gaps.
"""

from __future__ import annotations

from bathos.authoring.models import (
    AssumptionPayload,
    ClaimPayload,
    ConfoundPayload,
    DiscriminabilityPayload,
    HypothesisPayload,
    UnionGateClausePayload,
)

PRIMARY_HYPOTHESIS_ID = "H_information_symmetry"
NULL_HYPOTHESIS_ID = "H_null_misspec"


def scaffold_claim_payload() -> ClaimPayload:
    """The placeholder claim a fresh ``bth claim scaffold`` writes.

    Two hypotheses (a primary and a null/misspecification alternative) because
    ``validate_claim`` requires at least two, and requires one whose id names ``null``
    or ``misspec``. Two confounds because the two confound sub-blocks --
    ``reference_parity`` and ``synthetic_recovery`` -- are independent concerns and a
    single example carrying both would misrepresent how they are used.
    """
    return ClaimPayload(
        headline="REQUIRED: One-sentence summary of what this campaign tests",
        kill_condition=(
            "REQUIRED: Under what conditions would the result contradict the hypothesis?"
        ),
        kill_condition_satisfiable_by_null=False,
        regime="Optional: Parameter ranges or conditions claimed to be covered",
        hypotheses=[
            HypothesisPayload(
                id=PRIMARY_HYPOTHESIS_ID,
                label="REQUIRED: Descriptive label for primary hypothesis",
                predicted_signature="Optional: Expected metric fingerprint",
            ),
            HypothesisPayload(
                id=NULL_HYPOTHESIS_ID,
                label="REQUIRED: Null or misspecification hypothesis",
                predicted_signature=(
                    "Optional: Expected metric fingerprint if null hypothesis is true"
                ),
            ),
        ],
        assumptions=[
            AssumptionPayload(
                id="A_measurement_valid",
                label="REQUIRED: Descriptive assumption label",
            )
        ],
        confounds=[
            ConfoundPayload(
                id="C_topology_coupling",
                label="REQUIRED: Confound label",
                reference_parity_paper="Optional: Citation if baseline from literature",
                reference_parity_metric="Optional: metric key in baseline run",
                reference_parity_value=1.0,
                reference_parity_equivalence_bound=0.05,
            ),
            ConfoundPayload(
                id="C_pipeline_soundness",
                label=("REQUIRED: which pipeline component this campaign's runs depend on"),
                synthetic_recovery_gate_name=(
                    "REQUIRED: a name for the known-answer invariant test that proves "
                    "this component sound"
                ),
                synthetic_recovery_guards=[
                    "REQUIRED: source paths whose change invalidates a recorded green stamp"
                ],
            ),
        ],
        discriminability=[
            DiscriminabilityPayload(
                hypothesis_a=PRIMARY_HYPOTHESIS_ID,
                hypothesis_b=NULL_HYPOTHESIS_ID,
                planned_run_label="outcome_1",
                predicted_outcome="??  # EDIT: assign expected outcome if run exists",
            )
        ],
        union_gate_clauses=[
            UnionGateClausePayload(
                id="C_main",
                description="REQUIRED: What does this clause discriminate?",
                hypothesis_ids=[PRIMARY_HYPOTHESIS_ID, NULL_HYPOTHESIS_ID],
            )
        ],
    )
