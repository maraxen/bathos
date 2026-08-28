"""The claim renderer: round trip, idempotence, and preservation of scaffold guidance."""

from __future__ import annotations

import tomllib

import pytest

from bathos.authoring.models import (
    ClaimPayload,
    ConfoundPayload,
    DiscriminabilityPayload,
    HypothesisPayload,
    UnionGateClausePayload,
    unknown_keys,
)
from bathos.authoring.render import render_claim
from bathos.authoring.scaffolds import scaffold_claim_payload


def _render_scaffold() -> str:
    return render_claim(
        scaffold_claim_payload(),
        header="Claim for campaign: demo\nGenerated via bth claim scaffold",
        guidance=True,
    )


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_rendered_scaffold_is_parseable_toml():
    tomllib.loads(_render_scaffold())


def test_rendered_scaffold_round_trips_through_parse_claim(tmp_path):
    from bathos.claim import parse_claim

    path = tmp_path / "demo.claim.toml"
    path.write_text(_render_scaffold())
    claim = parse_claim(path)

    # The nesting asymmetry the renderer must reproduce: hypotheses/assumptions/confounds
    # are TOP-level arrays, discriminability and union_gate.clauses live under [claim].
    assert len(claim.hypotheses) == 2
    assert len(claim.assumptions) == 1
    assert len(claim.confounds) == 2
    assert len(claim.discriminability) == 1
    assert len(claim.union_gate_clauses) == 1
    assert claim.kill_condition_satisfiable_by_null is False


def test_discriminability_parses_as_an_array_not_a_table(tmp_path):
    """The specific regression: a table header collided with the array-of-tables.

    ``parse_claim`` reads ``discriminability`` by key, so a malformed shape yields a dict
    (or an empty list) rather than raising -- which is why this asserts on the type.
    """
    from bathos.claim import parse_claim

    path = tmp_path / "demo.claim.toml"
    path.write_text(_render_scaffold())
    claim = parse_claim(path)

    assert isinstance(claim.discriminability, list)
    assert all(isinstance(row, dict) for row in claim.discriminability)
    assert claim.discriminability[0]["hypothesis_a"] == "H_information_symmetry"


def test_scaffold_validates_modulo_placeholders(tmp_path):
    from bathos.claim import parse_claim, validate_claim

    path = tmp_path / "demo.claim.toml"
    path.write_text(_render_scaffold())
    result = validate_claim(parse_claim(path))

    unexpected = [
        e.message
        for e in result.errors
        if not any(m in e.message for m in ("REQUIRED:", "TODO:", "??", "EDIT:"))
    ]
    assert not unexpected, "unexpected errors on a fresh scaffold:\n  " + "\n  ".join(unexpected)


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


def test_rendering_is_deterministic():
    assert _render_scaffold() == _render_scaffold()


def test_reparsing_and_rerendering_is_byte_identical(tmp_path):
    """Data survives a full render -> parse -> payload -> render cycle unchanged.

    Rendered without guidance, since the prose is scaffolding rather than content and
    is not recoverable from a parsed document.
    """
    from bathos.claim import parse_claim

    payload = scaffold_claim_payload()
    first = render_claim(payload, guidance=False)

    path = tmp_path / "demo.claim.toml"
    path.write_text(first)
    claim = parse_claim(path)

    rebuilt = ClaimPayload(
        headline=claim.headline,
        kill_condition=claim.kill_condition,
        kill_condition_satisfiable_by_null=bool(claim.kill_condition_satisfiable_by_null),
        regime=claim.regime,
        hypotheses=[HypothesisPayload(**h) for h in claim.hypotheses],
        assumptions=[{"id": a["id"], "label": a["label"]} for a in claim.assumptions],
        confounds=[_confound_from_parsed(c) for c in claim.confounds],
        discriminability=[DiscriminabilityPayload(**d) for d in claim.discriminability],
        union_gate_clauses=[UnionGateClausePayload(**c) for c in claim.union_gate_clauses],
    )
    assert render_claim(rebuilt, guidance=False) == first


def _confound_from_parsed(raw: dict) -> ConfoundPayload:
    """Flatten a parsed confound's sub-blocks back into the payload's prefixed fields."""
    parity = raw.get("reference_parity", {})
    recovery = raw.get("synthetic_recovery", {})
    return ConfoundPayload(
        id=raw["id"],
        label=raw["label"],
        reference_parity_paper=parity.get("reference_paper"),
        reference_parity_metric=parity.get("reference_metric"),
        reference_parity_value=parity.get("reference_value"),
        reference_parity_equivalence_bound=parity.get("equivalence_bound"),
        synthetic_recovery_gate_name=recovery.get("gate_name"),
        synthetic_recovery_guards=list(recovery.get("guards", [])),
    )


# ---------------------------------------------------------------------------
# Guidance preservation
#
# The scaffold is a document a human edits; its explanatory prose is hard-won and must
# survive the move from a hand-written template to a rendered payload.
# ---------------------------------------------------------------------------

EXPECTED_PROSE = [
    "REQUIRED (debt #1071): is a null result a live possibility",
    "positive_control = true, proving (via a [differential] pre-flight)",
    "instrument-failure.",
    "Prove the invariant test passes yourself, then: bth gate stamp <gate_name> --result pass",
    "Matrix indexed by hypothesis-pair × outcome-label",
    'predicted_outcome: any outcome label from the runs, or "??" for unspecified',
    "this clause is proven by a [differential] pre-flight run",
]


@pytest.mark.parametrize("fragment", EXPECTED_PROSE)
def test_scaffold_still_explains_itself(fragment):
    assert fragment in _render_scaffold(), (
        f"scaffold guidance lost in the move to the renderer: {fragment!r}"
    )


def test_guidance_is_omitted_for_authored_documents():
    """A filled payload renders as data, not as a tutorial."""
    rendered = render_claim(scaffold_claim_payload(), guidance=False)
    assert "debt #1071" not in rendered
    assert "Matrix indexed by" not in rendered


# ---------------------------------------------------------------------------
# Structure emitted from the model, not typed by hand
# ---------------------------------------------------------------------------


def test_parity_confound_always_carries_exactly_one_unbound_run_id():
    """`bth claim attest-parity` locates this literal, and refuses if it is ambiguous."""
    rendered = render_claim(scaffold_claim_payload(), guidance=False)
    assert rendered.count('parity_run_id = ""') == 1


def test_confound_sub_blocks_are_omitted_when_unused():
    payload = ClaimPayload(
        headline="h",
        kill_condition="k",
        kill_condition_satisfiable_by_null=False,
        confounds=[ConfoundPayload(id="C1", label="plain confound")],
    )
    rendered = render_claim(payload)
    assert "[confounds.reference_parity]" not in rendered
    assert "[confounds.synthetic_recovery]" not in rendered
    tomllib.loads(rendered)


def test_values_needing_escapes_survive_rendering():
    """Quoting is tomlkit's problem, which is the point of not using an f-string."""
    payload = ClaimPayload(
        headline='He said "yes" \\ no',
        kill_condition="line1\nline2",
        kill_condition_satisfiable_by_null=True,
    )
    parsed = tomllib.loads(render_claim(payload))
    assert parsed["claim"]["headline"] == 'He said "yes" \\ no'
    assert parsed["claim"]["kill_condition"] == "line1\nline2"


# ---------------------------------------------------------------------------
# Unknown-key detection (the write path's refusal signal)
# ---------------------------------------------------------------------------


def test_unknown_keys_are_retained_and_reported():
    payload = ClaimPayload.model_validate(
        {
            "headline": "h",
            "kill_condition": "k",
            "kill_condition_satisfiable_by_null": False,
            "headlien": "typo",
            "hypotheses": [{"id": "H1", "label": "l", "labl": "nested typo"}],
        }
    )
    assert sorted(unknown_keys(payload)) == ["headlien", "hypotheses[0].labl"]


def test_no_unknown_keys_on_a_clean_payload():
    assert unknown_keys(scaffold_claim_payload()) == []
