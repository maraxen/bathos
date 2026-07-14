"""Force-rebuild durability proof for the S4 attestation sidecar (backlog item 3492).

task_id: 260713_figure-eda-build-dag. Mirrors tests/test_anchor_durability.py: proves
an attestation registered via bathos.attestation.register_attestation (which anchors
via DurableAnchorStore by default) survives
bathos.compact.compact(catalog_dir, force_rebuild=True) and remains queryable via
bathos.readback.query_attestation (S1).
"""

from __future__ import annotations

import pytest

from bathos.attestation import register_attestation
from bathos.compact import compact as compact_catalog
from bathos.readback import query_attestation

ORACLE_MATCH_TOML = """
[attestation]
kind = "oracle_match"
verdict = "PASS"
attested = {{ run_id = "run-001", output_path = "out/result.zarr", content_hash = "{content_hash}" }}
oracle_sha256 = "{oracle_sha}"
harness_run_ref = "run-harness-001"
max_discrepancy = 0.001
tolerance_policy = "abs<=1e-3"
created_by = "test-suite"
created_at = "2026-07-14T00:00:00Z"
"""


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    cat.mkdir(parents=True)
    return cat


class TestAttestationSurvivesForceRebuild:
    def test_pass_attestation_survives_force_rebuild_via_query_attestation(
        self, tmp_path, catalog_dir
    ):
        content_hash = "a" * 64
        src = tmp_path / "attest.toml"
        src.write_text(ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="b" * 64))

        register_attestation(src, catalog_dir)

        before = query_attestation(catalog_dir, content_hash)
        assert before is not None
        assert before["verdict"] == "PASS"

        result = compact_catalog(catalog_dir, force_rebuild=True)
        assert result is not None

        after = query_attestation(catalog_dir, content_hash)
        assert after is not None
        assert after["verdict"] == "PASS"
        assert after["kind"] == "oracle_match"
        assert after["content_hash"] == content_hash
        assert after["oracle_sha256"] == "b" * 64

    def test_survives_multiple_force_rebuilds(self, tmp_path, catalog_dir):
        content_hash_1 = "1" * 64
        content_hash_2 = "2" * 64

        src1 = tmp_path / "a1.toml"
        src1.write_text(ORACLE_MATCH_TOML.format(content_hash=content_hash_1, oracle_sha="3" * 64))
        register_attestation(src1, catalog_dir)
        compact_catalog(catalog_dir, force_rebuild=True)

        src2 = tmp_path / "a2.toml"
        src2.write_text(ORACLE_MATCH_TOML.format(content_hash=content_hash_2, oracle_sha="4" * 64))
        register_attestation(src2, catalog_dir)
        compact_catalog(catalog_dir, force_rebuild=True)

        assert query_attestation(catalog_dir, content_hash_1) is not None
        assert query_attestation(catalog_dir, content_hash_2) is not None

    def test_canonical_sidecar_file_survives_force_rebuild(self, tmp_path, catalog_dir):
        """The compact force_rebuild step only deletes/recreates <catalog_dir>/bathos.db
        — it must never touch <catalog_dir>/sidecars/attestations/, which is where the
        canonical durable attestation copy lives (mirrors sidecars/<campaign_id>/
        conventions, never rebuilt by compact)."""
        content_hash = "5" * 64
        src = tmp_path / "attest.toml"
        src.write_text(ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="6" * 64))

        record = register_attestation(src, catalog_dir)
        canonical = catalog_dir / "sidecars" / "attestations" / f"{record.sha256}.attestation.bth.toml"
        assert canonical.exists()

        compact_catalog(catalog_dir, force_rebuild=True)

        assert canonical.exists()

    def test_warn_attestation_anchor_survives_but_still_unqueryable_after_rebuild(
        self, tmp_path, catalog_dir
    ):
        content_hash = "7" * 64
        src = tmp_path / "warn.toml"
        src.write_text(
            ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="8" * 64).replace(
                'verdict = "PASS"', 'verdict = "WARN"'
            )
        )
        register_attestation(src, catalog_dir)

        compact_catalog(catalog_dir, force_rebuild=True)

        from bathos.anchor import find_anchors

        assert query_attestation(catalog_dir, content_hash) is None
        anchors = find_anchors(catalog_dir, content_hash=content_hash)
        assert len(anchors) == 1
        assert anchors[0].label == "WARN"
