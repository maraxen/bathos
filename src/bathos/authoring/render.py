"""Render authored payloads to canonical TOML.

Every value goes through ``tomlkit`` rather than an f-string, so quoting and escaping
are the library's problem rather than the template author's. The section *order* and
the guidance comments are assembled here deliberately: a scaffold is a document a human
edits, and tomlkit's container model cannot interleave a table's sub-tables with
unrelated top-level arrays the way the readable layout requires.

The specific bug this design forecloses: the previous claim template was a hand-written
f-string that opened ``[claim.discriminability]`` and then ``[[claim.discriminability]]``.
TOML forbids redefining a table as an array of tables, so every scaffolded claim was
unparseable. Structure is now emitted by code with a round-trip test behind it.

This module must not import from ``bathos`` -- see :mod:`bathos.authoring.models`.
"""

from __future__ import annotations

import tomlkit

from bathos.authoring.models import ClaimPayload, ConfoundPayload

# The scaffold's guidance prose. Kept as named constants rather than inline strings so
# the "scaffold still explains itself" test can assert on them by name, and so editing
# the wording cannot accidentally change the document's structure.
KILL_CONDITION_NULL_GUIDANCE = (
    "REQUIRED (debt #1071): is a null result a live possibility this kill_condition needs to\n"
    "rule out? If true, at least one [[claim.union_gate.clauses]] entry below must set\n"
    "positive_control = true, proving (via a [differential] pre-flight) that the instrument\n"
    "can actually detect a known-real effect -- otherwise a null result is unfalsifiable-by-\n"
    "instrument-failure."
)
SYNTHETIC_RECOVERY_GUIDANCE = (
    "Prove the invariant test passes yourself, then: bth gate stamp <gate_name> --result pass"
)
DISCRIMINABILITY_GUIDANCE = (
    "Matrix indexed by hypothesis-pair \u00d7 outcome-label\n"
    'predicted_outcome: any outcome label from the runs, or "??" for unspecified'
)
POSITIVE_CONTROL_GUIDANCE = (
    "positive_control = true  # set true (+ kill_condition_satisfiable_by_null=true above) if\n"
    "this clause is proven by a [differential] pre-flight run"
)

# An unbound parity confound always starts empty; `bth claim attest-parity` binds it
# later by locating this exact literal. It is not author-supplied, which is why
# ConfoundPayload has no parity_run_id field.
EMPTY_PARITY_RUN_ID = 'parity_run_id = ""'


def _fmt(value: object) -> str:
    """TOML representation of *value*, with quoting and escaping handled by tomlkit."""
    return tomlkit.item(value).as_string()


def _kv(key: str, value: object) -> str:
    return f"{key} = {_fmt(value)}"


def _comment(text: str) -> list[str]:
    """Render possibly-multi-line guidance as TOML comment lines."""
    return [f"# {line}" if line else "#" for line in text.split("\n")]


def _confound_lines(confound: ConfoundPayload, *, guidance: bool) -> list[str]:
    out = ["[[confounds]]", _kv("id", confound.id), _kv("label", confound.label)]

    if confound.has_reference_parity:
        out.append("[confounds.reference_parity]")
        for field, key in (
            ("reference_parity_paper", "reference_paper"),
            ("reference_parity_metric", "reference_metric"),
            ("reference_parity_value", "reference_value"),
            ("reference_parity_equivalence_bound", "equivalence_bound"),
        ):
            value = getattr(confound, field)
            if value is not None:
                out.append(_kv(key, value))
        out.append(EMPTY_PARITY_RUN_ID)

    if confound.has_synthetic_recovery:
        out.append("[confounds.synthetic_recovery]")
        if confound.synthetic_recovery_gate_name is not None:
            out.append(_kv("gate_name", confound.synthetic_recovery_gate_name))
        out.append(_kv("guards", list(confound.synthetic_recovery_guards)))
        if guidance:
            out.extend(_comment(SYNTHETIC_RECOVERY_GUIDANCE))

    return out


def render_claim(
    payload: ClaimPayload, *, header: str | None = None, guidance: bool = False
) -> str:
    """Render *payload* as a claim TOML document.

    Args:
        payload:  the claim to render.
        header:   optional comment block for the top of the file.
        guidance: when True, interleave the explanatory comments a freshly scaffolded
                  claim carries. Authored documents built from a filled payload set
                  this False -- the prose is scaffolding, not content.

    Section order reproduces the layout the scaffold has always used, which is the order
    a human fills the document in: claim scalars, hypotheses, assumptions, confounds,
    the discriminability matrix, then the union gate.
    """
    lines: list[str] = []

    if header:
        lines.extend(_comment(header))
        lines.append("")

    lines.append("[claim]")
    lines.append(_kv("headline", payload.headline))
    lines.append(_kv("kill_condition", payload.kill_condition))
    if guidance:
        lines.extend(_comment(KILL_CONDITION_NULL_GUIDANCE))
    lines.append(
        _kv("kill_condition_satisfiable_by_null", payload.kill_condition_satisfiable_by_null)
    )
    if payload.regime is not None:
        lines.append(_kv("regime", payload.regime))

    for hypothesis in payload.hypotheses:
        lines.append("")
        lines.append("[[hypotheses]]")
        lines.append(_kv("id", hypothesis.id))
        lines.append(_kv("label", hypothesis.label))
        if hypothesis.predicted_signature is not None:
            lines.append(_kv("predicted_signature", hypothesis.predicted_signature))

    for assumption in payload.assumptions:
        lines.append("")
        lines.append("[[assumptions]]")
        lines.append(_kv("id", assumption.id))
        lines.append(_kv("label", assumption.label))

    for confound in payload.confounds:
        lines.append("")
        lines.extend(_confound_lines(confound, guidance=guidance))

    if payload.discriminability:
        lines.append("")
        if guidance:
            lines.extend(_comment(DISCRIMINABILITY_GUIDANCE))
        for row in payload.discriminability:
            lines.append("[[claim.discriminability]]")
            lines.append(_kv("hypothesis_a", row.hypothesis_a))
            lines.append(_kv("hypothesis_b", row.hypothesis_b))
            lines.append(_kv("planned_run_label", row.planned_run_label))
            lines.append(_kv("predicted_outcome", row.predicted_outcome))

    if payload.union_gate_clauses:
        lines.append("")
        lines.append("[claim.union_gate]")
        for clause in payload.union_gate_clauses:
            lines.append("[[claim.union_gate.clauses]]")
            lines.append(_kv("id", clause.id))
            lines.append(_kv("description", clause.description))
            lines.append(_kv("hypothesis_ids", list(clause.hypothesis_ids)))
            if clause.positive_control is not None:
                lines.append(_kv("positive_control", clause.positive_control))
            elif guidance:
                lines.extend(_comment(POSITIVE_CONTROL_GUIDANCE))

    return "\n".join(lines) + "\n"
