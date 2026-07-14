"""Regression tests for debt #638 (escalates #629): bathos's promotion ratchet was
defeatable by an evidence-free attestation.

task_id: 260713_figure-eda-build-dag.

The bug (found by adversarial audit 260714): ``register_attestation``
(``bathos.attestation``) never called its own ``validate_attestation`` before
anchoring, and ``readback.query_attestation`` only checked ``verdict == "PASS"`` --
never ``validate_attestation`` either. So an ``oracle_match`` attestation missing
ALL required evidence fields (``oracle_sha256``, ``harness_run_ref``,
``max_discrepancy``, ``tolerance_policy`` -- exactly what ``validate_attestation``
itself flags with 4 errors when called directly) was nonetheless ACCEPTED by
``query_attestation`` and consumed by ``graduate_product`` to promote a fabricated
``content_hash`` to ``trust_state="promoted"``. This defeated spec §5.1's
evaluator-first ratchet ("nothing reaches promoted without an evaluator PASS
attestation").

The fix: ``register_attestation`` now calls ``validate_attestation`` internally and
raises ``AttestationValidationFailed`` -- before any anchor/write happens -- when
the attestation fails validation. This test module is the red->green pair:

- ``TestEvidenceFreeAttestationIsRejected`` is the repro. It must FAIL against the
  pre-fix code (register_attestation silently anchors, graduate_product promotes)
  and PASS against the fix (register_attestation raises, nothing is anchored,
  graduate_product still refuses).
- ``TestGenuineAttestationStillPromotes`` is the legitimate-path guard: a fully
  evidenced attestation (matching the real fields the merged S6 harness_run path
  produces -- oracle_sha256/harness_run_ref/max_discrepancy/tolerance_policy, per
  ``bathos.harness_run``'s verdict-sidecar docstring) must still register and
  promote successfully after the fix.
"""

from __future__ import annotations

import pytest

from bathos.attestation import (
    AttestationValidationFailed,
    parse_attestation,
    register_attestation,
    validate_attestation,
)
from bathos.readback import query_attestation
from bathos.trust_ledger import GraduationRefused, graduate_product

# A genuine oracle_match attestation shaped exactly like the schema documented in
# bathos.attestation's module docstring and the fields bathos.harness_run's verdict
# sidecar (max_discrepancy, tolerance_policy, stdout_hash-as-oracle_sha256,
# harness run_id-as-harness_run_ref) actually produces.
GENUINE_ORACLE_MATCH_TOML = """
[attestation]
kind = "oracle_match"
verdict = "PASS"
attested = {{ run_id = "run-real-001", output_path = "out/real_result.zarr", content_hash = "{content_hash}" }}
oracle_sha256 = "{oracle_sha}"
harness_run_ref = "harness-run-real-001"
max_discrepancy = 0.0005
tolerance_policy = "abs<=1e-3"
created_by = "s6-harness-wrapper"
created_at = "2026-07-14T00:00:00Z"
"""


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    cat.mkdir(parents=True)
    return cat


class TestEvidenceFreeAttestationIsRejected:
    """debt #638 repro: an oracle_match attestation missing all required evidence
    fields must never reach the anchor store, let alone back a promotion."""

    def _write_evidence_free_attestation(self, tmp_path, content_hash: str):
        toml_text = f"""
[attestation]
kind = "oracle_match"
verdict = "PASS"
attested = {{ run_id = "fabricated-run", output_path = "out/fake.zarr", content_hash = "{content_hash}" }}
created_by = "attacker"
created_at = "2026-07-14T00:00:00Z"
"""
        path = tmp_path / "evidence_free.attestation.bth.toml"
        path.write_text(toml_text)
        return path

    def test_validate_attestation_flags_all_four_missing_evidence_fields(self, tmp_path):
        """Sanity check on the measurement pipeline itself (BATHOS.md discipline):
        confirm validate_attestation actually catches this shape before trusting
        any conclusion built on top of it."""
        content_hash = "a" * 64
        path = self._write_evidence_free_attestation(tmp_path, content_hash)

        result = validate_attestation(parse_attestation(path))

        assert not result.ok
        messages = " ".join(e.message for e in result.errors)
        assert "oracle_sha256" in messages
        assert "harness_run_ref" in messages
        assert "max_discrepancy" in messages
        assert "tolerance_policy" in messages

    def test_register_attestation_rejects_evidence_free_attestation(self, tmp_path, catalog_dir):
        """THE repro for debt #638. Pre-fix: this raises nothing -- register_attestation
        returns a valid AnchorRecord for an attestation with zero required evidence.
        Post-fix: register_attestation must raise AttestationValidationFailed and
        anchor nothing."""
        content_hash = "b" * 64
        path = self._write_evidence_free_attestation(tmp_path, content_hash)

        with pytest.raises(AttestationValidationFailed):
            register_attestation(path, catalog_dir)

        # Nothing was anchored -- query_attestation must find nothing for this hash.
        assert query_attestation(catalog_dir, content_hash) is None

    def test_graduate_product_cannot_promote_via_evidence_free_attestation(
        self, tmp_path, catalog_dir
    ):
        """THE full-chain repro for debt #638: even attempting to register the
        evidence-free attestation first (as an attacker/careless caller would),
        graduate_product must still refuse to promote the fabricated content_hash."""
        content_hash = "c" * 64
        path = self._write_evidence_free_attestation(tmp_path, content_hash)

        with pytest.raises(AttestationValidationFailed):
            register_attestation(path, catalog_dir)

        with pytest.raises(GraduationRefused):
            graduate_product(catalog_dir, content_hash, "fabricated-attn-ref")


class TestGenuineAttestationStillPromotes:
    """Guard against over-correction: a fully-evidenced attestation matching the
    real S6 harness_run output shape must still register and promote cleanly."""

    def test_genuine_oracle_match_registers_and_promotes(self, tmp_path, catalog_dir):
        content_hash = "d" * 64
        toml_text = GENUINE_ORACLE_MATCH_TOML.format(
            content_hash=content_hash, oracle_sha="e" * 64
        )
        path = tmp_path / "genuine.attestation.bth.toml"
        path.write_text(toml_text)

        # Registration succeeds -- no exception.
        record = register_attestation(path, catalog_dir)
        assert record.kind == "oracle_match"
        assert record.content_hash == content_hash

        # query_attestation finds it.
        result = query_attestation(catalog_dir, content_hash)
        assert result is not None
        assert result["verdict"] == "PASS"

        # graduate_product promotes it -- the legitimate ratchet path still works.
        ledger_record = graduate_product(catalog_dir, content_hash, record.sha256)
        assert ledger_record.to_state == "promoted"
        assert ledger_record.content_hash == content_hash
