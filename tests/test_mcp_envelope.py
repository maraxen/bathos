"""Tests for MCP tool envelope shape compliance (AC-5, AC-6)."""

import pytest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bathos.mcp import traced_tool
from bathos.errors import BathosErrorCode
from bathos.query import CatalogError


def test_success_envelope_has_all_four_keys():
    """Verify that a successful tool return includes all four mandatory keys (AC-5).
    
    Given a tool that returns successfully with custom data,
    When the tool is wrapped with traced_tool and returns,
    Then the envelope includes ok=True, error_code=None, error=None, resolution_hint=None
         plus the custom data keys.
    """
    async def run_test():
        @traced_tool
        async def test_tool():
            return {"custom_data": "test_value", "count": 42}
        
        result = await test_tool()
        
        # All four mandatory keys must be present
        assert "ok" in result
        assert "error_code" in result
        assert "error" in result
        assert "resolution_hint" in result
        
        # Values on success path
        assert result["ok"] is True
        assert result["error_code"] is None
        assert result["error"] is None
        assert result["resolution_hint"] is None
        
        # Custom data preserved
        assert result["custom_data"] == "test_value"
        assert result["count"] == 42
    
    asyncio.run(run_test())


def test_error_envelope_has_all_four_keys():
    """Verify that an error envelope includes all four mandatory keys (AC-6).
    
    Given a tool that raises a CatalogError,
    When the tool is wrapped with traced_tool,
    Then the envelope includes ok=False, error_code (non-null), error (non-null),
         and resolution_hint (non-null).
    """
    async def run_test():
        @traced_tool
        async def test_tool():
            raise CatalogError("Test catalog error")
        
        result = await test_tool()
        
        # All four mandatory keys must be present
        assert "ok" in result
        assert "error_code" in result
        assert "error" in result
        assert "resolution_hint" in result
        
        # Values on error path
        assert result["ok"] is False
        assert result["error_code"] == "catalog_error"
        assert result["error"] == "Test catalog error"
        assert result["resolution_hint"] != ""
        assert isinstance(result["resolution_hint"], str)
    
    asyncio.run(run_test())


def test_success_envelope_return_type():
    """Verify that success envelope is a plain dict, not json.dumps() string."""
    async def run_test():
        @traced_tool
        async def test_tool():
            return {"data": "value"}
        
        result = await test_tool()
        assert isinstance(result, dict)
        # Should NOT be a JSON string
        assert not isinstance(result, str)
    
    asyncio.run(run_test())


def test_error_envelope_return_type():
    """Verify that error envelope is a plain dict, not json.dumps() string."""
    async def run_test():
        @traced_tool
        async def test_tool():
            raise CatalogError("error")
        
        result = await test_tool()
        assert isinstance(result, dict)
        # Should NOT be a JSON string
        assert not isinstance(result, str)
    
    asyncio.run(run_test())


def test_mandatory_keys_before_custom_data():
    """Verify that mandatory keys cannot be clobbered by tool-specific data (M-2).
    
    Given a tool that tries to return a custom 'ok' key,
    When wrapped with traced_tool,
    Then the envelope's ok key (from the wrapper) takes precedence.
    """
    async def run_test():
        @traced_tool
        async def test_tool():
            # Tool tries to return its own 'ok' key
            return {"ok": False, "custom": "data"}
        
        result = await test_tool()
        
        # The wrapper's ok=True must win (success path)
        assert result["ok"] is True
        # Custom 'ok' is effectively overridden
        assert result["custom"] == "data"
    
    asyncio.run(run_test())
