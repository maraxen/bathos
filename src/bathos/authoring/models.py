"""Structured payloads for authored bathos documents.

These models are the *authoring* surface: what an agent supplies over MCP or the CLI
instead of hand-writing TOML. They are deliberately NOT a second copy of the reader's
dataclasses -- ``bathos.claim.ClaimFile`` and ``bathos.sidecar.Sidecar`` remain the
read path. A payload is rendered to canonical TOML, re-parsed by those same readers,
and validated by the existing validators before anything is written.

Two configuration choices are load-bearing and pinned by ``tests/test_authoring_probe.py``:

* ``extra="allow"`` rather than ``extra="forbid"``. Under FastMCP, ``forbid`` raises
  inside the transport's own coercion -- before the wrapped function -- so bathos's
  ``@traced_tool`` never sees it and the ``ok``/``error_code``/``error``/
  ``resolution_hint`` envelope is bypassed. With ``allow``, unknown keys survive into
  ``model_extra`` and :func:`unknown_keys` reports them, so a typo is refused through
  the ordinary envelope with the offending key named. (``TypedDict`` was rejected
  outright: it drops unknown keys silently, which is the exact failure this layer
  exists to eliminate.)

* Sub-blocks are FLATTENED with prefixed keys -- ``reference_parity_paper`` rather than
  a nested ``reference_parity`` object. Deeply nested tool schemas are a known failure
  mode for tool-calling models; flattening holds the published schema at depth 2. The
  renderer restores the real TOML nesting, so the agent never sees it.

This module must not import from ``bathos`` -- it is written to be liftable into
cisternal later, following the ``bathos.git_pin`` precedent. Enforced by
``tests/test_authoring_isolation.py``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# Stage names bathos recognises. A value outside this set is currently coerced to
# "exploration" with only a log warning on the read path; on the write path it is a
# hard error, which is the whole point of authoring through a model.
CANONICAL_STAGES = ("exploration", "calibration", "validation", "ablation", "production")

StageName = Literal["exploration", "calibration", "validation", "ablation", "production"]

# Closed vocabularies enforced by literal string comparison downstream: obligation
# triggering keys on the exact token "supports", so a plausible synonym is a real error.
LiteratureDisposition = Literal["supports", "contradicts", "scope-differs"]
ImplementationDisposition = Literal["matches", "diverges", "not-applicable"]


class _Payload(BaseModel):
    """Base: permissive coercion, with unknown keys retained for explicit refusal."""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


def unknown_keys(payload: BaseModel, _path: str = "") -> list[str]:
    """Every unknown key in *payload*, recursively, as dotted paths.

    ``extra="allow"`` keeps typos rather than dropping them; this is what turns that
    retention into a refusal the caller can act on. Returns dotted paths such as
    ``hypotheses[0].labl`` so the error names exactly what to fix.
    """
    found: list[str] = []
    prefix = f"{_path}." if _path else ""

    for key in payload.model_extra or {}:
        found.append(f"{prefix}{key}")

    for name, _field in type(payload).model_fields.items():
        value = getattr(payload, name, None)
        if isinstance(value, BaseModel):
            found.extend(unknown_keys(value, f"{prefix}{name}"))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, BaseModel):
                    found.extend(unknown_keys(item, f"{prefix}{name}[{i}]"))

    return found


# ---------------------------------------------------------------------------
# Claim payloads
# ---------------------------------------------------------------------------


class HypothesisPayload(_Payload):
    """One entry of the top-level ``[[hypotheses]]`` array."""

    id: Annotated[str, Field(min_length=1, description="Stable id, e.g. H_information_symmetry")]
    label: Annotated[str, Field(min_length=1, description="What this hypothesis asserts")]
    predicted_signature: str | None = Field(
        default=None, description="The observable pattern this hypothesis predicts"
    )


class AssumptionPayload(_Payload):
    """One entry of the top-level ``[[assumptions]]`` array."""

    id: Annotated[str, Field(min_length=1, description="Stable id, e.g. A_stationarity")]
    label: Annotated[str, Field(min_length=1, description="What is being assumed")]


class ConfoundPayload(_Payload):
    """One entry of the top-level ``[[confounds]]`` array.

    ``[confounds.reference_parity]`` and ``[confounds.synthetic_recovery]`` are flattened
    into prefixed fields here and re-nested by the renderer.

    ``parity_run_id`` is deliberately absent: it is not author-supplied, it is bound
    later by ``bth claim attest-parity`` once a literature-parity run exists. Keeping it
    off the authoring surface removes a whole class of "already set or TOML format
    mismatch" errors at source.
    """

    id: Annotated[str, Field(min_length=1, description="Stable id, e.g. C_topology_coupling")]
    label: Annotated[str, Field(min_length=1, description="What this confound would explain")]

    reference_parity_paper: str | None = Field(
        default=None, description="Published work this baseline reproduces"
    )
    reference_parity_metric: str | None = Field(
        default=None, description="Metric key compared against the published value"
    )
    reference_parity_value: float | None = Field(
        default=None, description="The published value being matched"
    )
    reference_parity_equivalence_bound: float | None = Field(
        default=None, description="Tolerance within which the reimplementation counts as parity"
    )

    synthetic_recovery_gate_name: str | None = Field(
        default=None, description="Name of the known-answer invariant test proving soundness"
    )
    synthetic_recovery_guards: list[str] = Field(
        default_factory=list,
        description="Source paths whose change invalidates a recorded green stamp",
    )

    @property
    def has_reference_parity(self) -> bool:
        return any(
            v is not None
            for v in (
                self.reference_parity_paper,
                self.reference_parity_metric,
                self.reference_parity_value,
                self.reference_parity_equivalence_bound,
            )
        )

    @property
    def has_synthetic_recovery(self) -> bool:
        return bool(self.synthetic_recovery_gate_name or self.synthetic_recovery_guards)


class DiscriminabilityPayload(_Payload):
    """One row of ``[[claim.discriminability]]``: a hypothesis pair and its planned run."""

    hypothesis_a: Annotated[str, Field(min_length=1)]
    hypothesis_b: Annotated[str, Field(min_length=1)]
    planned_run_label: Annotated[str, Field(min_length=1)]
    predicted_outcome: Annotated[
        str,
        Field(min_length=1, description='Outcome label this pair predicts, or "??" if unassigned'),
    ]


class UnionGateClausePayload(_Payload):
    """One clause of ``[[claim.union_gate.clauses]]``."""

    id: Annotated[str, Field(min_length=1, description="Clause id, e.g. C_main")]
    description: Annotated[str, Field(min_length=1, description="What this clause discriminates")]
    hypothesis_ids: list[str] = Field(
        default_factory=list, description="Hypothesis ids this clause adjudicates"
    )
    positive_control: bool | None = Field(
        default=None,
        description=(
            "True when this clause is proven by a [differential] pre-flight run. Required "
            "somewhere in the gate when kill_condition_satisfiable_by_null is true (AC-23)."
        ),
    )


class ClaimPayload(_Payload):
    """A complete claim document, as an agent supplies it.

    Mirrors ``bathos.claim.ClaimFile``. Note the nesting asymmetry it must reproduce:
    ``hypotheses``/``assumptions``/``confounds`` are TOP-level arrays, while
    ``discriminability`` and ``union_gate.clauses`` live under ``[claim]``.
    """

    headline: Annotated[str, Field(min_length=1, description="The claim in one sentence")]
    kill_condition: Annotated[
        str, Field(min_length=1, description="The observation that would falsify the claim")
    ]
    kill_condition_satisfiable_by_null: bool = Field(
        description=(
            "Whether a null/misspecified model could satisfy the kill condition. Required "
            "(AC-23): if true, the union gate must carry a positive_control clause."
        )
    )
    regime: str | None = Field(default=None, description="Regime this claim is scoped to")

    hypotheses: list[HypothesisPayload] = Field(default_factory=list)
    assumptions: list[AssumptionPayload] = Field(default_factory=list)
    confounds: list[ConfoundPayload] = Field(default_factory=list)
    discriminability: list[DiscriminabilityPayload] = Field(default_factory=list)
    union_gate_clauses: list[UnionGateClausePayload] = Field(default_factory=list)
