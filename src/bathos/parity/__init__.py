"""Bathos parity-evidence package and legacy literature-parity v1 re-exports.

Provides Ring 0 parity-evidence tooling (``store``, ``schema``, ``declare``,
``npy_stdlib``, ``evidence``, ``metrics``, ``sealed_dump``, ``input_store``,
``axes``, ``grader``, ``badge``, ``report``) alongside re-exports from
``bathos.parity.legacy`` for existing literature-parity callers.
"""

from __future__ import annotations

from bathos.parity.legacy import (
    _CLAUSE_PARITY_FAIL_THRESHOLD,
    _CLAUSE_PARITY_PARTIAL_THRESHOLD,
    ParityEvidence,
    ParityGradeResult,
    check_parity_confounds_for_submit,
    compute_grade,
    evidence_from_result,
    parse_parity_toml,
)

legacy_literature_grade = compute_grade

__all__ = [
    "ParityEvidence",
    "ParityGradeResult",
    "_CLAUSE_PARITY_FAIL_THRESHOLD",
    "_CLAUSE_PARITY_PARTIAL_THRESHOLD",
    "check_parity_confounds_for_submit",
    "compute_grade",
    "evidence_from_result",
    "legacy_literature_grade",
    "parse_parity_toml",
]
