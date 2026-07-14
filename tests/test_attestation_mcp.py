"""Tests for the S4 attestation sidecar MCP tool surface (backlog item 3492).

Mirrors tests/test_anchor_mcp.py convention: the *_tool functions are plain,
synchronously-callable functions (the @app.tool-decorated async wrappers just
forward to them) — this follows the anchor/query tool convention rather than
claim.py's async-only style, per the dispatch brief ("Wire register + query on MCP
and CLI following the anchor/query patterns").
"""

from __future__ import annotations

from bathos.mcp import (
    attestation_register_tool,
    attestation_scaffold_tool,
    attestation_validate_tool,
    query_attestation_tool,
)

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


class TestAttestationScaffoldTool:
    def test_scaffold_oracle_match(self, tmp_path):
        result = attestation_scaffold_tool(kind="oracle_match", workspace_root=str(tmp_path))
        assert result["ok"] is True
        assert result["path"]

    def test_scaffold_rejects_bad_kind(self, tmp_path):
        result = attestation_scaffold_tool(kind="bogus", workspace_root=str(tmp_path))
        assert result["ok"] is False


class TestAttestationRegisterAndQueryTool:
    def test_register_then_query(self, tmp_path, tmp_catalog):
        content_hash = "a" * 64
        src = tmp_path / "attest.toml"
        src.write_text(ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="b" * 64))

        result = attestation_register_tool(path=str(src), catalog_dir=str(tmp_catalog))
        assert result["ok"] is True
        assert result["anchor"]["kind"] == "oracle_match"
        assert result["anchor"]["label"] == "PASS"

        queried = query_attestation_tool(content_hash=content_hash, catalog_dir=str(tmp_catalog))
        assert queried["attestation"] is not None
        assert queried["attestation"]["verdict"] == "PASS"

    def test_register_missing_file_returns_error(self, tmp_path, tmp_catalog):
        result = attestation_register_tool(
            path=str(tmp_path / "nope.toml"), catalog_dir=str(tmp_catalog)
        )
        assert result["ok"] is False

    def test_register_requires_path(self, tmp_catalog):
        result = attestation_register_tool(path="", catalog_dir=str(tmp_catalog))
        assert result["ok"] is False

    def test_query_returns_none_for_warn(self, tmp_path, tmp_catalog):
        content_hash = "b" * 64
        src = tmp_path / "attest.toml"
        src.write_text(
            ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="c" * 64).replace(
                'verdict = "PASS"', 'verdict = "WARN"'
            )
        )

        attestation_register_tool(path=str(src), catalog_dir=str(tmp_catalog))

        queried = query_attestation_tool(content_hash=content_hash, catalog_dir=str(tmp_catalog))
        assert queried["attestation"] is None


class TestAttestationValidateTool:
    def test_validate_valid_file(self, tmp_path):
        content_hash = "c" * 64
        src = tmp_path / "attest.toml"
        src.write_text(ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="d" * 64))

        result = attestation_validate_tool(path=str(src))
        assert result["ok"] is True

    def test_validate_missing_fields_fails(self, tmp_path):
        src = tmp_path / "bad.toml"
        src.write_text(
            """
[attestation]
kind = "oracle_match"
verdict = "PASS"
[attestation.attested]
run_id = "r1"
output_path = "o"
content_hash = "x"
"""
        )

        result = attestation_validate_tool(path=str(src))
        assert result["ok"] is False
        assert result["errors"]

    def test_validate_missing_file_errors(self, tmp_path):
        result = attestation_validate_tool(path=str(tmp_path / "nope.toml"))
        assert result["ok"] is False
