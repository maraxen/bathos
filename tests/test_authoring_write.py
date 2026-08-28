"""The refusal gate: an invalid document is never written, not written-then-flagged.

Today an agent can scaffold a claim, hand-edit it, and simply never call validate --
validation is a separate, optional, skippable round trip. These tests pin the inversion:
the validator runs before the bytes exist, so a document that would fail validation
leaves no trace on disk at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bathos.authoring.models import (
    AssumptionPayload,
    ClaimPayload,
    ConfoundPayload,
    DiscriminabilityPayload,
    HypothesisPayload,
    UnionGateClausePayload,
)
from bathos.authoring.write import author_claim
from bathos.errors import BathosErrorCode


def valid_payload(**overrides) -> ClaimPayload:
    """A claim that passes validate_claim with no catalog attached."""
    base = {
        "headline": "Sparse attention matches dense attention below 4k context",
        "kill_condition": "Sparse trails dense by more than 2 points at any context length",
        "kill_condition_satisfiable_by_null": False,
        "hypotheses": [
            HypothesisPayload(id="H_sparse_parity", label="Sparse attention reaches parity"),
            HypothesisPayload(id="H_null_misspec", label="Any parity is measurement artefact"),
        ],
        "assumptions": [AssumptionPayload(id="A_tokenizer", label="Both arms share a tokenizer")],
        "confounds": [
            ConfoundPayload(id="C_seq_len", label="Sequence length distribution differs")
        ],
        "discriminability": [
            DiscriminabilityPayload(
                hypothesis_a="H_sparse_parity",
                hypothesis_b="H_null_misspec",
                planned_run_label="ctx_2k",
                predicted_outcome="pass",
            ),
            DiscriminabilityPayload(
                hypothesis_a="H_sparse_parity",
                hypothesis_b="H_null_misspec",
                planned_run_label="ctx_8k",
                predicted_outcome="fail",
            ),
        ],
        "union_gate_clauses": [
            UnionGateClausePayload(
                id="C_main",
                description="Does sparse reach parity below 4k?",
                hypothesis_ids=["H_sparse_parity", "H_null_misspec"],
            )
        ],
    }
    base.update(overrides)
    return ClaimPayload(**base)


def test_a_valid_payload_is_written_and_reparses(tmp_path):
    from bathos.claim import parse_claim

    target = tmp_path / "claims" / "demo.claim.toml"
    result = author_claim(valid_payload(), target)

    assert result.ok, result.errors
    assert target.exists()
    assert result.sha256 and len(result.sha256) == 64

    claim = parse_claim(target)
    assert claim.headline.startswith("Sparse attention")
    assert len(claim.hypotheses) == 2
    assert claim.sha256 == result.sha256


# ---------------------------------------------------------------------------
# Refusals -- in every case, nothing is written
# ---------------------------------------------------------------------------


def test_unknown_key_is_refused_and_named(tmp_path):
    """The typo is reported by name rather than silently dropped."""
    target = tmp_path / "demo.claim.toml"
    payload = valid_payload().model_dump()
    payload["headlien"] = "typo"

    result = author_claim(payload, target)

    assert not result.ok
    assert result.error_code == BathosErrorCode.DOCUMENT_INVALID.value
    assert result.unknown_keys == ["headlien"]
    assert "headlien" in result.errors[0]
    assert not target.exists(), "a refused document must not be written"


def test_nested_unknown_key_is_refused_with_its_path(tmp_path):
    target = tmp_path / "demo.claim.toml"
    payload = valid_payload().model_dump()
    payload["hypotheses"][0]["labl"] = "nested typo"

    result = author_claim(payload, target)

    assert not result.ok
    assert result.unknown_keys == ["hypotheses[0].labl"]
    assert not target.exists()


def test_validation_failure_writes_nothing(tmp_path):
    """A claim with one hypothesis fails validate_claim, so it never reaches disk."""
    target = tmp_path / "demo.claim.toml"
    payload = valid_payload(
        hypotheses=[HypothesisPayload(id="H_only", label="the sole hypothesis")]
    )

    result = author_claim(payload, target)

    assert not result.ok
    assert result.error_code == BathosErrorCode.DOCUMENT_INVALID.value
    assert result.errors
    assert not target.exists(), "validation must gate the write, not annotate it"


def test_missing_required_field_is_refused(tmp_path):
    target = tmp_path / "demo.claim.toml"
    payload = valid_payload().model_dump()
    del payload["kill_condition_satisfiable_by_null"]

    result = author_claim(payload, target)

    assert not result.ok
    assert result.error_code == BathosErrorCode.DOCUMENT_INVALID.value
    assert not target.exists()


def test_existing_document_is_not_clobbered(tmp_path):
    target = tmp_path / "demo.claim.toml"
    target.write_text("# hand-written, precious\n")

    result = author_claim(valid_payload(), target)

    assert not result.ok
    assert result.error_code == BathosErrorCode.DOCUMENT_CONFLICT.value
    assert target.read_text() == "# hand-written, precious\n"


def test_force_overwrites_and_records_the_prior_sha(tmp_path):
    target = tmp_path / "demo.claim.toml"
    first = author_claim(valid_payload(), target)
    assert first.ok

    second = author_claim(
        valid_payload(headline="Revised: sparse matches dense below 8k context"),
        target,
        force=True,
    )

    assert second.ok
    assert second.sha256 != first.sha256
    assert "8k" in target.read_text()


def test_refusal_carries_a_resolution_hint(tmp_path):
    """Every refusal must be actionable through the standard envelope."""
    result = author_claim({"headline": "x"}, tmp_path / "demo.claim.toml")

    assert not result.ok
    envelope = result.as_envelope()
    assert envelope["ok"] is False
    assert envelope["error_code"] == BathosErrorCode.DOCUMENT_INVALID.value
    assert envelope["error"]
    assert envelope["resolution_hint"], "a refusal with no hint leaves the agent stuck"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.update({"headlien": "typo"}), id="unknown_key"),
        pytest.param(
            lambda p: p.update({"hypotheses": [{"id": "H_only", "label": "sole"}]}),
            id="validation_failure",
        ),
        pytest.param(lambda p: p.pop("kill_condition_satisfiable_by_null"), id="missing_required"),
    ],
)
def test_no_temp_files_survive_a_refusal(tmp_path, mutate):
    """A refusal leaves the target directory exactly as it found it."""
    target = tmp_path / "claims" / "demo.claim.toml"
    target.parent.mkdir(parents=True)

    payload = valid_payload().model_dump()
    mutate(payload)
    result = author_claim(payload, target)

    assert not result.ok
    assert list(target.parent.iterdir()) == [], "refusal left files behind: " + str(
        list(target.parent.iterdir())
    )


# ---------------------------------------------------------------------------
# The rendered artifact, not the payload, is what gets validated
# ---------------------------------------------------------------------------


def test_a_broken_renderer_cannot_reach_disk(tmp_path, monkeypatch):
    """Step 3 re-parses the rendered bytes, so a renderer bug is caught before the write.

    This is the structural guard against the class of defect that made every scaffolded
    claim unparseable for two and a half months.
    """
    import bathos.authoring.write as write_mod

    monkeypatch.setattr(
        write_mod, "render_claim", lambda *_a, **_k: "[claim]\nthis = is not ] valid toml\n"
    )

    target = tmp_path / "demo.claim.toml"
    result = author_claim(valid_payload(), target)

    assert not result.ok
    assert result.error_code == BathosErrorCode.DOCUMENT_INVALID.value
    assert "did not parse" in result.errors[0]
    assert not target.exists()


def test_write_is_atomic_leaving_no_tmp_on_success(tmp_path):
    target = tmp_path / "claims" / "demo.claim.toml"
    assert author_claim(valid_payload(), target).ok

    leftovers = [p.name for p in target.parent.iterdir() if p.name != target.name]
    assert leftovers == [], f"atomic write left temp files: {leftovers}"


def test_authoring_emits_telemetry(tmp_path, monkeypatch):
    """Mutations are observable, which is half of the ledger contract."""
    seen: list[tuple[str, dict]] = []

    import bathos.telemetry as telemetry_mod

    monkeypatch.setattr(telemetry_mod, "event", lambda name, **kw: seen.append((name, kw)))

    target = tmp_path / "demo.claim.toml"
    assert author_claim(valid_payload(), target, actor="mcp", reason="initial draft").ok

    names = [n for n, _ in seen]
    assert "authoring.create" in names, f"expected authoring.create, saw {names}"

    payload = dict(seen[names.index("authoring.create")][1])
    assert payload["doc_kind"] == "claim"
    assert payload["actor"] == "mcp"
    assert payload["reason"] == "initial draft"
    assert payload["before_sha256"] is None
    assert len(payload["after_sha256"]) == 64


def test_amend_telemetry_carries_both_shas(tmp_path, monkeypatch):
    target = tmp_path / "demo.claim.toml"
    first = author_claim(valid_payload(), target)
    assert first.ok

    seen: list[tuple[str, dict]] = []
    import bathos.telemetry as telemetry_mod

    monkeypatch.setattr(telemetry_mod, "event", lambda name, **kw: seen.append((name, kw)))

    second = author_claim(
        valid_payload(headline="Revised headline for the claim"), target, force=True
    )
    assert second.ok

    names = [n for n, _ in seen]
    assert "authoring.amend" in names
    payload = dict(seen[names.index("authoring.amend")][1])
    assert payload["before_sha256"] == first.sha256
    assert payload["after_sha256"] == second.sha256


def test_target_is_untouched_when_the_payload_is_not_even_a_claim(tmp_path):
    target = tmp_path / "demo.claim.toml"
    result = author_claim({"not": "a claim"}, target)

    assert not result.ok
    assert not target.exists()
    assert Path(target).parent.exists() is True  # tmp_path itself, unchanged
