"""Tests for MCP envelope shape consistency (AC-5, AC-6).

Verifies that all MCP tools return envelopes with the four mandatory keys:
ok, error_code, error, resolution_hint.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from bathos.errors import BathosErrorCode, RESOLUTION_HINTS
from bathos.query import CatalogError
from bathos.mcp import traced_tool


@pytest.fixture
def event_mock():
    """Mock the telemetry event function."""
    with patch("bathos.mcp.event") as mock:
        yield mock


def test_success_envelope_has_all_four_keys(tmp_path: Path, monkeypatch, event_mock):
    """Verify success envelope contains all four mandatory keys.

    Given: A successful call to a tool wrapped by traced_tool
    When: The underlying operation completes without error
    Then: The returned dict contains ok=True, error_code=None, error=None, resolution_hint=None
    """
    catalog_dir = tmp_path / ".bth" / "catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

    # Create a simple async tool function that succeeds
    @traced_tool
    async def mock_success_tool(catalog_dir: str = ""):
        """A mock tool that returns success."""
        return {
            "data": {"runs": [], "count": 0}
        }

    # Call the tool using asyncio.run
    result = asyncio.run(mock_success_tool(catalog_dir=str(catalog_dir)))

    # Verify all four keys are present
    assert isinstance(result, dict), f"Expected dict, got {type(result)}"
    assert all(k in result for k in ["ok", "error_code", "error", "resolution_hint"]), \
        f"Missing keys in success envelope. Keys present: {result.keys()}"

    # Verify success values
    assert result["ok"] is True, "ok should be True on success"
    assert result["error_code"] is None, "error_code should be None on success"
    assert result["error"] is None, "error should be None on success"
    assert result["resolution_hint"] is None, "resolution_hint should be None on success"


def test_error_envelope_has_all_four_keys(tmp_path: Path, monkeypatch, event_mock):
    """Verify error envelope contains all four mandatory keys with proper values.

    Given: A tool that raises CatalogError
    When: traced_tool catches and shapes the exception
    Then: The returned dict contains ok=False, error_code='catalog_error',
          error as non-empty str, resolution_hint from RESOLUTION_HINTS
    """
    catalog_dir = tmp_path / ".bth" / "catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

    # Create a tool function that raises CatalogError
    @traced_tool
    async def mock_error_tool(catalog_dir: str = ""):
        """A mock tool that raises CatalogError."""
        raise CatalogError("Mock catalog error for testing")

    # Call the tool — traced_tool should catch the exception and return a shaped envelope
    result = asyncio.run(mock_error_tool(catalog_dir=str(catalog_dir)))

    # Verify all four keys are present
    assert isinstance(result, dict), f"Expected dict, got {type(result)}"
    assert all(k in result for k in ["ok", "error_code", "error", "resolution_hint"]), \
        f"Missing keys in error envelope. Keys present: {result.keys()}"

    # Verify error values
    assert result["ok"] is False, "ok should be False on error"
    assert result["error_code"] == BathosErrorCode.CATALOG_ERROR.value, \
        f"error_code should be '{BathosErrorCode.CATALOG_ERROR.value}', got {result['error_code']}"
    assert isinstance(result["error"], str) and len(result["error"]) > 0, \
        f"error should be non-empty str, got {result['error']}"
    assert isinstance(result["resolution_hint"], str) and len(result["resolution_hint"]) > 0, \
        f"resolution_hint should be non-empty str, got {result['resolution_hint']}"

    # Verify resolution_hint matches the registry
    assert result["resolution_hint"] == RESOLUTION_HINTS[BathosErrorCode.CATALOG_ERROR], \
        f"resolution_hint should match registry entry"


def test_self_reported_error_dict_is_not_clobbered_to_success(tmp_path: Path, monkeypatch, event_mock):
    """A tool that returns its own {"ok": False, "error": ...} dict (rather than
    raising) must not have that silently overwritten into a fake success.

    Regression: traced_tool's success-path merge used to do
    `{**result, "ok": True, "error": None, ...}`, which put the defaults AFTER
    **result and so always won — turning every self-reported failure dict into
    {"ok": True, "error": None}. This is the root cause behind debt #477's report
    of `campaign_add` returning {"ok": true} via MCP despite writing nothing.
    """
    catalog_dir = tmp_path / ".bth" / "catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

    @traced_tool
    async def mock_self_reported_error_tool(catalog_dir: str = ""):
        """Mimics campaign_add_tool's except-block return shape."""
        return {"ok": False, "error": "Campaign not found: deadbeef"}

    result = asyncio.run(mock_self_reported_error_tool(catalog_dir=str(catalog_dir)))

    assert result["ok"] is False, f"Expected ok=False to survive, got envelope: {result}"
    assert result["error"] == "Campaign not found: deadbeef", \
        f"Expected the real error message to survive, got envelope: {result}"


def test_self_reported_bare_error_key_infers_ok_false(tmp_path: Path, monkeypatch, event_mock):
    """A tool that returns only {"error": "..."} (no explicit "ok" key) must still
    surface as ok=False, not the default ok=True."""
    catalog_dir = tmp_path / ".bth" / "catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

    @traced_tool
    async def mock_bare_error_tool(catalog_dir: str = ""):
        """Mimics run_tool's `if not script_path: return {"error": "..."}` pattern."""
        return {"error": "script_path parameter is required"}

    result = asyncio.run(mock_bare_error_tool(catalog_dir=str(catalog_dir)))

    assert result["ok"] is False, f"Expected ok=False to be inferred, got envelope: {result}"
    assert result["error"] == "script_path parameter is required"
