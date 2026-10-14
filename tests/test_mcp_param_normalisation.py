"""Regression tests for MCP/CLI parameter handling (debt batch 261014 W3)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import bathos.mcp as mcp_mod
from bathos.mcp import run_tool


@pytest.fixture
def captured_run_script(tmp_path, monkeypatch):
    """Patch run_script in mcp and capture its kwargs; isolate catalog."""
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    calls: list[dict] = []

    def fake_run_script(**kwargs):
        calls.append(kwargs)
        return 0

    with patch.object(mcp_mod, "run_script", side_effect=fake_run_script):
        yield calls


class TestRunToolTagAndOutputNormalisation:
    """#2203 (comma tags) + #2217 (JSON-list output_paths)."""

    def test_comma_separated_tags_are_split(self, captured_run_script):
        run_tool(script_path="x.py", tags=["a,b,c"])
        assert captured_run_script[0]["tags"] == ["a", "b", "c"]

    def test_tags_strip_and_drop_empties(self, captured_run_script):
        run_tool(script_path="x.py", tags=["a, b", ",", "c,,", "d"])
        assert captured_run_script[0]["tags"] == ["a", "b", "c", "d"]

    def test_json_list_output_paths_expanded(self, captured_run_script):
        run_tool(script_path="x.py", output_paths=['["a","b"]', "c"])
        assert captured_run_script[0]["output_paths"] == ["a", "b", "c"]

    def test_non_list_bracket_string_kept_literal(self, captured_run_script):
        run_tool(script_path="x.py", output_paths=["[not json", '["a", 1]', "plain"])
        assert captured_run_script[0]["output_paths"] == ["[not json", '["a", 1]', "plain"]


@pytest.fixture
def campaign_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    return tmp_path / "catalog"


class TestCampaignCreateTool:
    def test_sequential_mode_accepted(self, campaign_env):
        """#2080: library accepts 'sequential'; the MCP wrapper must too."""
        from bathos.mcp import campaign_create_tool

        result = campaign_create_tool(name="seq", mode="sequential", catalog_dir=str(campaign_env))
        assert "error" not in result, result
        assert result["mode"] == "sequential"

    def test_invalid_mode_still_rejected(self, campaign_env):
        from bathos.mcp import campaign_create_tool

        result = campaign_create_tool(name="bad", mode="bogus", catalog_dir=str(campaign_env))
        assert "error" in result
        assert "sequential" in result["error"]
