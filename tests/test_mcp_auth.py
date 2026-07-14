"""Tests for the MCP write-seam token auth gate (debt #619).

The MCP anchor write seam (anchor_insert, attestation_register,
figure_entry_register, etc.) previously accepted writes from ANY MCP caller
with no auth check at all — flagged by the cross-boundary gate spike, which
wrote a real row into the shared ~/.bth/catalog/bathos.db from an external
session.

This hardens every mutating (@app.tool + @traced_tool + @require_write_token)
tool behind a shared-secret token check (bathos.mcp_auth), while leaving
read-only tools (figure_lookup, anchor_get, anchor_find, list_runs, ...)
untouched — see the require_write_token and bathos.mcp_auth module
docstrings for why a token check (rather than full RBAC) is the
proportionate fix given bathos's stdio-only MCP transport.
"""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest

from bathos.errors import BathosErrorCode
from bathos.mcp import (
    claim_attest_parity,
    claim_register,
    claim_scaffold,
    mcp_anchor_find_tool,
    mcp_anchor_get_tool,
    mcp_anchor_insert_tool,
    mcp_archive_tool,
    mcp_attestation_register_tool,
    mcp_attestation_scaffold_tool,
    mcp_campaign_add_tool,
    mcp_campaign_conclude_tool,
    mcp_campaign_create_tool,
    mcp_compact_tool,
    mcp_figure_entry_register_tool,
    mcp_figure_lookup_tool,
    mcp_init_tool,
    mcp_list_runs_tool,
    mcp_repair_tool,
    mcp_run_tool,
    mcp_sync_tool,
)
from bathos.mcp_auth import check_token, get_or_create_token, token_path

# All 17 write-verb MCP tools gated with @require_write_token (debt #619).
# Kept as an explicit enumeration (rather than introspecting mcp.py) so this
# test file itself is the audit trail for "every write tool is covered" —
# see mcp.py's `rg -F "@require_write_token"` for the source-of-truth count.
WRITE_TOOLS = [
    mcp_anchor_insert_tool,
    mcp_figure_entry_register_tool,
    mcp_attestation_scaffold_tool,
    mcp_attestation_register_tool,
    mcp_compact_tool,
    mcp_archive_tool,
    mcp_sync_tool,
    mcp_init_tool,
    mcp_run_tool,
    mcp_campaign_create_tool,
    mcp_campaign_conclude_tool,
    claim_register,
    claim_scaffold,
    claim_attest_parity,
    mcp_campaign_add_tool,
    mcp_repair_tool,
]

# postmortem_scaffold is imported separately below since its name collides
# with nothing else, but we import it here for clarity in the parametrize ids.
from bathos.mcp import postmortem_scaffold  # noqa: E402

WRITE_TOOLS.append(postmortem_scaffold)

READ_ONLY_TOOLS = [
    mcp_list_runs_tool,
    mcp_anchor_get_tool,
    mcp_anchor_find_tool,
    mcp_figure_lookup_tool,
]


@pytest.fixture(autouse=True)
def isolated_token_path(tmp_path: Path, monkeypatch):
    """Point BTH_MCP_TOKEN_PATH at a scratch file so tests never touch the
    real ~/.bth/mcp_token, and each test starts with no token file."""
    token_file = tmp_path / "mcp_token"
    monkeypatch.setenv("BTH_MCP_TOKEN_PATH", str(token_file))
    return token_file


class TestMcpAuthModule:
    def test_get_or_create_token_creates_0600_file(self, isolated_token_path):
        assert not isolated_token_path.exists()
        token = get_or_create_token()
        assert isolated_token_path.exists()
        assert len(token) > 0
        mode = stat.S_IMODE(isolated_token_path.stat().st_mode)
        assert mode == (stat.S_IRUSR | stat.S_IWUSR), f"expected 0600, got {oct(mode)}"

    def test_get_or_create_token_is_idempotent(self):
        first = get_or_create_token()
        second = get_or_create_token()
        assert first == second

    def test_check_token_rejects_missing_and_wrong(self):
        real = get_or_create_token()
        assert check_token(None) is False
        assert check_token("") is False
        assert check_token("not-the-real-token") is False
        assert check_token(real) is True

    def test_token_path_honors_env_override(self, isolated_token_path):
        assert token_path() == isolated_token_path


class TestWriteToolRejectsMissingToken:
    """Acceptance #1: a write-verb tool call WITHOUT the token is now REJECTED."""

    def test_anchor_insert_without_token_is_rejected(self, tmp_catalog):
        result = asyncio.run(
            mcp_anchor_insert_tool(
                path="fig.png",
                sha256="a" * 64,
                kind="figure",
                catalog_dir=str(tmp_catalog),
                # token omitted entirely
            )
        )
        assert result["ok"] is False
        assert result["error_code"] == BathosErrorCode.AUTH_ERROR.value

    def test_anchor_insert_with_wrong_token_is_rejected(self, tmp_catalog):
        get_or_create_token()  # ensure a real token exists to be wrong about
        result = asyncio.run(
            mcp_anchor_insert_tool(
                path="fig.png",
                sha256="a" * 64,
                kind="figure",
                catalog_dir=str(tmp_catalog),
                token="definitely-wrong",
            )
        )
        assert result["ok"] is False
        assert result["error_code"] == BathosErrorCode.AUTH_ERROR.value

    @pytest.mark.parametrize("tool", WRITE_TOOLS, ids=lambda t: t.__name__)
    def test_every_write_tool_rejects_missing_token(self, tool):
        """Acceptance #3: ALL write-verb tools are covered by the auth gate.

        Auth is checked before any business-logic argument validation inside
        require_write_token's wrapper, so calling with no args at all still
        must fail with auth_error (never a business-logic error) when the
        token is missing.
        """
        result = asyncio.run(tool())
        assert result["ok"] is False, f"{tool.__name__} did not fail with no token"
        assert result["error_code"] == BathosErrorCode.AUTH_ERROR.value, (
            f"{tool.__name__} failed for a reason other than auth: {result}"
        )


class TestWriteToolAcceptsValidToken:
    """Acceptance #2: the SAME tool call WITH the correct token still succeeds."""

    def test_anchor_insert_with_correct_token_succeeds(self, tmp_catalog):
        token = get_or_create_token()
        result = asyncio.run(
            mcp_anchor_insert_tool(
                path="fig.png",
                sha256="a" * 64,
                kind="figure",
                catalog_dir=str(tmp_catalog),
                token=token,
            )
        )
        assert result["ok"] is True, f"expected success with valid token, got: {result}"
        assert result["anchor"]["path"] == "fig.png"

    def test_compact_with_correct_token_is_not_auth_blocked(self, tmp_catalog):
        token = get_or_create_token()
        result = asyncio.run(mcp_compact_tool(catalog_dir=str(tmp_catalog), token=token))
        # compact() on an empty catalog should succeed outright (nothing to ingest).
        assert result["ok"] is True, f"expected success with valid token, got: {result}"
        assert result["error_code"] != BathosErrorCode.AUTH_ERROR.value

    @pytest.mark.parametrize("tool", WRITE_TOOLS, ids=lambda t: t.__name__)
    def test_every_write_tool_is_not_auth_blocked_with_valid_token(self, tool):
        """With a correct token, whatever a tool returns must NOT be an
        auth_error — any remaining failure must come from missing/invalid
        business parameters, not from the auth gate."""
        token = get_or_create_token()
        result = asyncio.run(tool(token=token))
        assert result.get("error_code") != BathosErrorCode.AUTH_ERROR.value, (
            f"{tool.__name__} was still auth-blocked with a valid token: {result}"
        )


class TestReadOnlyToolsRemainUngated:
    """Read-only tools (figure_lookup, anchor_get, anchor_find, list_runs, ...)
    must NOT require a token — no regression to legitimate local read usage."""

    def test_list_runs_without_token(self, tmp_catalog):
        result = asyncio.run(mcp_list_runs_tool(catalog_dir=str(tmp_catalog)))
        assert result["ok"] is True
        assert result.get("error_code") != BathosErrorCode.AUTH_ERROR.value

    def test_anchor_get_without_token(self, tmp_catalog):
        result = asyncio.run(
            mcp_anchor_get_tool(path="nope.png", sha256="a" * 64, catalog_dir=str(tmp_catalog))
        )
        assert result["ok"] is True

    def test_anchor_find_without_token(self, tmp_catalog):
        result = asyncio.run(mcp_anchor_find_tool(catalog_dir=str(tmp_catalog)))
        assert result["ok"] is True

    def test_figure_lookup_without_token(self, tmp_catalog):
        result = asyncio.run(mcp_figure_lookup_tool(catalog_dir=str(tmp_catalog)))
        assert result["ok"] is True

    @pytest.mark.parametrize("tool", READ_ONLY_TOOLS, ids=lambda t: t.__name__)
    def test_every_read_only_tool_ignores_missing_token(self, tool, tmp_catalog):
        result = asyncio.run(tool(catalog_dir=str(tmp_catalog)))
        assert result.get("error_code") != BathosErrorCode.AUTH_ERROR.value, (
            f"{tool.__name__} unexpectedly required a token"
        )
