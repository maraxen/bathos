"""Verdict-aware query_attestation tests (S4, backlog item 3492).

task_id: 260713_figure-eda-build-dag. Covers the read side of the attestation sidecar:
query_attestation(content_hash, min_strength) must return an attestation ONLY when a
matching-strength PASS attestation is registered — WARN/FAIL attestations are present
in the catalog (registerable, anchored) but never satisfy this query (spec §5.1 step 4).
"""

from __future__ import annotations

import pytest

from bathos.attestation import register_attestation
from bathos.readback import query_attestation

ORACLE_MATCH_TOML = """
[attestation]
kind = "oracle_match"
verdict = "{verdict}"
attested = {{ run_id = "run-001", output_path = "out/result.zarr", content_hash = "{content_hash}" }}
oracle_sha256 = "{oracle_sha}"
harness_run_ref = "run-harness-001"
max_discrepancy = 0.001
tolerance_policy = "abs<=1e-3"
created_by = "test-suite"
created_at = "2026-07-14T00:00:00Z"
"""

REPRO_FLOOR_TOML = """
[attestation]
kind = "repro_floor"
verdict = "{verdict}"
attested = {{ run_id = "run-002", output_path = "out/result2.zarr", content_hash = "{content_hash}" }}
seed_pin = 42
rerun_count = 3
rerun_digests = ["{content_hash}", "{content_hash}", "{content_hash}"]
created_by = "test-suite"
created_at = "2026-07-14T00:00:00Z"
"""


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    cat.mkdir(parents=True)
    return cat


def _register(tmp_path, catalog_dir, template, content_hash, verdict, name, **fmt):
    path = tmp_path / name
    path.write_text(template.format(content_hash=content_hash, verdict=verdict, **fmt))
    return register_attestation(path, catalog_dir)


class TestQueryAttestationReturnsNoneWhenNothingRegistered:
    def test_returns_none_for_unknown_content_hash(self, catalog_dir):
        assert query_attestation(catalog_dir, "a" * 64) is None

    def test_returns_none_with_min_strength(self, catalog_dir):
        assert query_attestation(catalog_dir, "a" * 64, min_strength="oracle_match") is None
        assert query_attestation(catalog_dir, "a" * 64, min_strength="repro_floor") is None


class TestQueryAttestationVerdictGate:
    def test_pass_oracle_match_is_returned(self, tmp_path, catalog_dir):
        content_hash = "a" * 64
        _register(
            tmp_path, catalog_dir, ORACLE_MATCH_TOML, content_hash, "PASS",
            "a1.attestation.bth.toml", oracle_sha="b" * 64,
        )

        result = query_attestation(catalog_dir, content_hash)

        assert result is not None
        assert result["kind"] == "oracle_match"
        assert result["verdict"] == "PASS"
        assert result["content_hash"] == content_hash
        assert result["attested"]["run_id"] == "run-001"
        assert result["oracle_sha256"] == "b" * 64

    def test_warn_attestation_is_not_returned(self, tmp_path, catalog_dir):
        content_hash = "b" * 64
        _register(
            tmp_path, catalog_dir, ORACLE_MATCH_TOML, content_hash, "WARN",
            "b1.attestation.bth.toml", oracle_sha="c" * 64,
        )

        assert query_attestation(catalog_dir, content_hash) is None

    def test_fail_attestation_is_not_returned(self, tmp_path, catalog_dir):
        content_hash = "c" * 64
        _register(
            tmp_path, catalog_dir, ORACLE_MATCH_TOML, content_hash, "FAIL",
            "c1.attestation.bth.toml", oracle_sha="d" * 64,
        )

        assert query_attestation(catalog_dir, content_hash) is None

    def test_warn_attestation_is_still_present_via_anchor_find(self, tmp_path, catalog_dir):
        """WARN/FAIL attestations exist as anchored records — they are just not
        surfaced by the verdict-gated query_attestation."""
        from bathos.anchor import find_anchors

        content_hash = "d" * 64
        _register(
            tmp_path, catalog_dir, ORACLE_MATCH_TOML, content_hash, "WARN",
            "d1.attestation.bth.toml", oracle_sha="e" * 64,
        )

        assert query_attestation(catalog_dir, content_hash) is None
        anchors = find_anchors(catalog_dir, content_hash=content_hash)
        assert len(anchors) == 1
        assert anchors[0].label == "WARN"

    def test_pass_repro_floor_is_returned(self, tmp_path, catalog_dir):
        content_hash = "e" * 64
        _register(
            tmp_path, catalog_dir, REPRO_FLOOR_TOML, content_hash, "PASS",
            "e1.attestation.bth.toml",
        )

        result = query_attestation(catalog_dir, content_hash)

        assert result is not None
        assert result["kind"] == "repro_floor"
        assert result["seed_pin"] == 42


class TestQueryAttestationMinStrength:
    def test_repro_floor_does_not_satisfy_oracle_match_minimum(self, tmp_path, catalog_dir):
        content_hash = "f" * 64
        _register(
            tmp_path, catalog_dir, REPRO_FLOOR_TOML, content_hash, "PASS",
            "f1.attestation.bth.toml",
        )

        assert query_attestation(catalog_dir, content_hash, min_strength="oracle_match") is None
        # But it does satisfy the weaker (or no) minimum.
        assert query_attestation(catalog_dir, content_hash, min_strength="repro_floor") is not None
        assert query_attestation(catalog_dir, content_hash) is not None

    def test_oracle_match_satisfies_repro_floor_minimum(self, tmp_path, catalog_dir):
        """oracle_match is the stronger strength — it must satisfy a weaker
        (repro_floor) minimum requirement too."""
        content_hash = "1" * 64
        _register(
            tmp_path, catalog_dir, ORACLE_MATCH_TOML, content_hash, "PASS",
            "g1.attestation.bth.toml", oracle_sha="2" * 64,
        )

        result = query_attestation(catalog_dir, content_hash, min_strength="repro_floor")
        assert result is not None
        assert result["kind"] == "oracle_match"

    def test_oracle_match_satisfies_oracle_match_minimum(self, tmp_path, catalog_dir):
        content_hash = "3" * 64
        _register(
            tmp_path, catalog_dir, ORACLE_MATCH_TOML, content_hash, "PASS",
            "h1.attestation.bth.toml", oracle_sha="4" * 64,
        )

        result = query_attestation(catalog_dir, content_hash, min_strength="oracle_match")
        assert result is not None


class TestQueryAttestationPrefersStrongest:
    def test_prefers_oracle_match_over_repro_floor_for_same_content_hash(self, tmp_path, catalog_dir):
        content_hash = "5" * 64
        _register(
            tmp_path, catalog_dir, REPRO_FLOOR_TOML, content_hash, "PASS",
            "i1.attestation.bth.toml",
        )
        _register(
            tmp_path, catalog_dir, ORACLE_MATCH_TOML, content_hash, "PASS",
            "i2.attestation.bth.toml", oracle_sha="6" * 64,
        )

        result = query_attestation(catalog_dir, content_hash)

        assert result is not None
        assert result["kind"] == "oracle_match"


class TestInertEvidenceUntilTrustLedger:
    """Spec item 9 acceptance note: a PASS attestation recorded before the S3 trust
    ledger (#3491) exists is inert — nothing promotes on it alone. get_trust_state
    must stay UNKNOWN regardless of a PASS attestation for the same content_hash."""

    def test_pass_attestation_does_not_change_get_trust_state(self, tmp_path, catalog_dir):
        from bathos.readback import ProductTrustState, get_trust_state

        content_hash = "7" * 64
        _register(
            tmp_path, catalog_dir, ORACLE_MATCH_TOML, content_hash, "PASS",
            "j1.attestation.bth.toml", oracle_sha="8" * 64,
        )

        assert query_attestation(catalog_dir, content_hash) is not None
        assert get_trust_state(catalog_dir, content_hash) == ProductTrustState.UNKNOWN
