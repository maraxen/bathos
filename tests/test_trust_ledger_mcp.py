"""Tests for the S3 trust-ledger MCP tool surface (graduate_product, backlog #3491).

Mirrors tests/test_attestation_mcp.py convention: the *_tool functions are plain,
synchronously-callable functions (the @app.tool-decorated async wrapper just
forwards to them). graduate_product_tool wraps the already-tested
bathos.trust_ledger.graduate_product() (see tests/test_trust_ledger.py) — these
tests cover the MCP-surface plumbing (arg validation, error_code mapping), not
the ratchet invariant itself.
"""

from __future__ import annotations

from bathos.mcp import attestation_register_tool, graduate_product_tool

ORACLE_MATCH_TOML = """
[attestation]
kind = "oracle_match"
verdict = "{verdict}"
attested = {{ run_id = "run-001", output_path = "out/result.zarr", content_hash = "{content_hash}" }}
oracle_sha256 = "{oracle_sha}"
harness_run_ref = "run-harness-001"
max_discrepancy = 0.0
tolerance_policy = "exact_hash_match"
created_by = "test-suite"
created_at = "2026-07-14T00:00:00Z"
"""


class TestGraduateProductTool:
    def test_requires_content_hash_and_attestation_ref(self, tmp_catalog):
        result = graduate_product_tool(content_hash="", attestation_ref="", catalog_dir=str(tmp_catalog))
        assert result["ok"] is False

        result = graduate_product_tool(
            content_hash="a" * 64, attestation_ref="", catalog_dir=str(tmp_catalog)
        )
        assert result["ok"] is False

    def test_refuses_without_a_pass_attestation(self, tmp_catalog):
        result = graduate_product_tool(
            content_hash="b" * 64, attestation_ref="nonexistent-ref", catalog_dir=str(tmp_catalog)
        )
        assert result["ok"] is False
        assert result["error_code"] == "graduation_refused"

    def test_succeeds_with_a_registered_pass_attestation(self, tmp_path, tmp_catalog):
        content_hash = "c" * 64
        src = tmp_path / "attest.toml"
        src.write_text(
            ORACLE_MATCH_TOML.format(verdict="PASS", content_hash=content_hash, oracle_sha="d" * 64)
        )
        registered = attestation_register_tool(path=str(src), catalog_dir=str(tmp_catalog))
        assert registered["ok"] is True
        attestation_sha256 = registered["anchor"]["sha256"]

        result = graduate_product_tool(
            content_hash=content_hash,
            attestation_ref=attestation_sha256,
            catalog_dir=str(tmp_catalog),
            min_strength="oracle_match",
            run_id="run-001",
            output_path="out/result.zarr",
            reason="test graduation",
        )
        assert result["ok"] is True
        assert result["record"]["to_state"] == "promoted"
        assert result["record"]["from_state"] == "candidate"

    def test_idempotent_on_a_repeated_call(self, tmp_path, tmp_catalog):
        content_hash = "e" * 64
        src = tmp_path / "attest.toml"
        src.write_text(
            ORACLE_MATCH_TOML.format(verdict="PASS", content_hash=content_hash, oracle_sha="f" * 64)
        )
        registered = attestation_register_tool(path=str(src), catalog_dir=str(tmp_catalog))
        attestation_sha256 = registered["anchor"]["sha256"]

        first = graduate_product_tool(
            content_hash=content_hash, attestation_ref=attestation_sha256, catalog_dir=str(tmp_catalog)
        )
        second = graduate_product_tool(
            content_hash=content_hash, attestation_ref=attestation_sha256, catalog_dir=str(tmp_catalog)
        )
        assert first["ok"] is True
        assert second["ok"] is True
        assert first["record"]["run_id"] == second["record"]["run_id"]

    def test_a_warn_attestation_does_not_satisfy_the_ratchet(self, tmp_path, tmp_catalog):
        content_hash = "1" * 64
        src = tmp_path / "attest.toml"
        src.write_text(
            ORACLE_MATCH_TOML.format(verdict="WARN", content_hash=content_hash, oracle_sha="2" * 64)
        )
        registered = attestation_register_tool(path=str(src), catalog_dir=str(tmp_catalog))
        attestation_sha256 = registered["anchor"]["sha256"]

        result = graduate_product_tool(
            content_hash=content_hash, attestation_ref=attestation_sha256, catalog_dir=str(tmp_catalog)
        )
        assert result["ok"] is False
        assert result["error_code"] == "graduation_refused"
