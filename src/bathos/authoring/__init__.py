"""Structured authoring for bathos pre-registration documents.

Agents author claims and sidecars by supplying a typed payload rather than hand-editing
TOML. The payload is rendered to canonical TOML, re-parsed by the same reader the read
path uses, and validated before anything reaches disk -- so a document that would fail
validation is never written at all.

Public surface:
    :mod:`bathos.authoring.models`  -- payload models (the MCP/CLI input shape)
    :mod:`bathos.authoring.render`  -- payload -> canonical TOML

``models`` and ``render`` deliberately import nothing from ``bathos``: the mechanism is
generic and is written to be liftable into cisternal later, following the precedent set
by ``bathos.git`` / ``bathos.git_pin`` shimming over ``cisternal.provenance``.
"""

from __future__ import annotations

from bathos.authoring.models import (
    AssumptionPayload,
    ClaimPayload,
    ConfoundPayload,
    DiscriminabilityPayload,
    HypothesisPayload,
    UnionGateClausePayload,
    unknown_keys,
)
from bathos.authoring.render import render_claim

__all__ = [
    "AssumptionPayload",
    "ClaimPayload",
    "ConfoundPayload",
    "DiscriminabilityPayload",
    "HypothesisPayload",
    "UnionGateClausePayload",
    "render_claim",
    "unknown_keys",
]
