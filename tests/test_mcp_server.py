"""Tests for FastMCP server tools."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from bathos.mcp import (
    _WIRED,
    app,
    archive_tool,
    capability_probe_tool,
    check_tool,
    compact_tool,
    find_runs_tool,
    get_run_tool,
    init_tool,
    list_runs_tool,
    run_sql_tool,
    run_tool,
    sync_tool,
)
from bathos.schema import Run
from bathos.telemetry import run_uuid_var


class TestListRunsTool:
    """Test list_runs MCP tool."""

    def test_list_runs_tool_returns_json_table(self, tmp_catalog, sample_run):
        """Verify list_runs returns JSON with runs array."""
        # Write sample run to catalog
        from bathos.catalog import write_run

        write_run(sample_run, tmp_catalog)

        result = list_runs_tool(catalog_dir=str(tmp_catalog), limit=10)

        data = result
        assert "runs" in data
        assert "count" in data
        assert data["count"] == 1
        assert data["runs"][0]["id"] == sample_run.id
        assert data["runs"][0]["status"] == "completed"

    def test_list_runs_tool_respects_limit(self, tmp_catalog):
        """Verify list_runs respects limit parameter."""
        from bathos.catalog import write_run

        for i in range(5):
            run = Run(
                project_slug="test",
                command=f"cmd {i}",
                argv=["python", f"script{i}.py"],
                git_hash=f"hash{i}",
                git_branch="main",
                git_dirty=False,
                status="completed",
                exit_code=0,
                duration_s=1.0,
                hostname="test",
            )
            write_run(run, tmp_catalog)

        result = list_runs_tool(catalog_dir=str(tmp_catalog), limit=2)
        data = result
        assert data["count"] == 2

    def test_list_runs_tool_error_handling(self):
        """Verify list_runs handles errors gracefully."""
        result = list_runs_tool(catalog_dir="/nonexistent/path", limit=10)
        data = result
        # Should return either empty list or error, but must be valid JSON
        assert isinstance(data, dict)


class TestFindRunsTool:
    """Test find_runs MCP tool."""

    def test_find_runs_tool_filters_by_pattern(self, tmp_catalog):
        """Verify find_runs filters runs."""
        from bathos.catalog import write_run

        run1 = Run(
            project_slug="proj_a",
            command="cmd1",
            argv=["python", "s1.py"],
            git_hash="h1",
            git_branch="main",
            git_dirty=False,
            status="completed",
            exit_code=0,
            duration_s=1.0,
            hostname="test",
        )
        run2 = Run(
            project_slug="proj_b",
            command="cmd2",
            argv=["python", "s2.py"],
            git_hash="h2",
            git_branch="main",
            git_dirty=False,
            status="completed",
            exit_code=0,
            duration_s=1.0,
            hostname="test",
        )
        write_run(run1, tmp_catalog)
        write_run(run2, tmp_catalog)

        result = find_runs_tool(catalog_dir=str(tmp_catalog), pattern="proj_a")
        data = result
        assert data["count"] == 1
        assert data["runs"][0]["project_slug"] == "proj_a"

    def test_find_runs_tool_error_handling(self):
        """Verify find_runs handles errors gracefully."""
        result = find_runs_tool(catalog_dir="/nonexistent/path")
        data = result
        # Should return either empty list or error, but must be valid JSON
        assert isinstance(data, dict)

    def test_find_runs_tool_filters_by_tags(self, tmp_catalog):
        """Verify find_runs filters by tags, independent of pattern/project."""
        from bathos.catalog import write_run

        tagged = Run(
            project_slug="same_proj",
            command="cmd1",
            argv=["python", "s1.py"],
            git_hash="h1",
            git_branch="main",
            git_dirty=False,
            status="completed",
            exit_code=0,
            duration_s=1.0,
            hostname="test",
            tags=["figure-eda:node-p2"],
        )
        untagged = Run(
            project_slug="same_proj",
            command="cmd2",
            argv=["python", "s2.py"],
            git_hash="h2",
            git_branch="main",
            git_dirty=False,
            status="completed",
            exit_code=0,
            duration_s=1.0,
            hostname="test",
            tags=["unrelated"],
        )
        write_run(tagged, tmp_catalog)
        write_run(untagged, tmp_catalog)

        result = find_runs_tool(catalog_dir=str(tmp_catalog), tags=["figure-eda:node-p2"])
        data = result
        assert data["count"] == 1
        assert data["runs"][0]["id"] == tagged.id
        assert data["runs"][0]["tags"] == ["figure-eda:node-p2"]


class TestGetRunTool:
    """Test get_run MCP tool."""

    def test_get_run_tool_returns_run_json(self, tmp_catalog, sample_run):
        """Verify get_run returns complete run details."""
        from bathos.catalog import write_run

        write_run(sample_run, tmp_catalog)

        result = get_run_tool(catalog_dir=str(tmp_catalog), run_id=sample_run.id)

        data = result
        assert data["id"] == sample_run.id
        assert data["project_slug"] == "testproj"
        assert data["status"] == "completed"
        assert data["exit_code"] == 0
        assert isinstance(data["argv"], list)

    def test_get_run_tool_missing_run_id(self):
        """Verify get_run requires run_id."""
        result = get_run_tool(catalog_dir="", run_id="")
        data = result
        assert "error" in data

    def test_get_run_tool_not_found(self, tmp_catalog):
        """Verify get_run returns error for missing run."""
        result = get_run_tool(catalog_dir=str(tmp_catalog), run_id="nonexistent")
        data = result
        assert "error" in data


class TestRunSqlTool:
    """Test run_sql MCP tool."""

    def test_run_sql_tool_executes_query(self, tmp_catalog, sample_run):
        """Verify run_sql executes and returns results."""
        from bathos.catalog import write_run
        from bathos.compact import compact

        write_run(sample_run, tmp_catalog)
        # Compact to warm tier so SQL works
        compact(tmp_catalog)

        result = run_sql_tool(
            catalog_dir=str(tmp_catalog), sql="SELECT id, status FROM runs LIMIT 1"
        )

        data = result
        assert "rows" in data
        assert "count" in data

    def test_run_sql_tool_missing_sql(self):
        """Verify run_sql requires sql parameter."""
        result = run_sql_tool(catalog_dir="", sql="")
        data = result
        assert "error" in data

    def test_run_sql_tool_error_handling(self):
        """Verify run_sql raises on invalid SQL."""
        import pytest
        with pytest.raises(Exception):
            run_sql_tool(catalog_dir="/nonexistent/path", sql="SELECT * FROM nonexistent")


class TestCompactTool:
    """Test compact MCP tool."""

    def test_compact_tool_calls_compact_module(self, tmp_catalog, sample_run):
        """Verify compact tool returns expected result."""
        from bathos.catalog import write_run

        write_run(sample_run, tmp_catalog)

        result = compact_tool(catalog_dir=str(tmp_catalog))

        data = result
        assert "ingested" in data
        assert "skipped" in data
        assert "duration_s" in data
        assert data["ingested"] >= 0

    def test_compact_tool_error_handling(self):
        """Verify compact raises on nonexistent catalog."""
        import pytest
        with pytest.raises(Exception):
            compact_tool(catalog_dir="/nonexistent/path")


class TestArchiveTool:
    """Test archive MCP tool."""

    def test_archive_tool_requires_project(self):
        """Verify archive requires project parameter."""
        result = archive_tool(catalog_dir="", project="")
        data = result
        assert "error" in data

    def test_archive_tool_calls_archive_module(self, tmp_catalog, sample_run):
        """Verify archive tool returns expected result."""
        from bathos.catalog import write_run
        from bathos.compact import compact

        write_run(sample_run, tmp_catalog)
        compact(tmp_catalog)

        result = archive_tool(
            catalog_dir=str(tmp_catalog),
            project=sample_run.project_slug,
        )

        data = result
        # Archive should return valid JSON with runs_archived field
        assert isinstance(data, dict)
        assert "runs_archived" in data or "error" in data
        if "runs_archived" in data:
            assert "partitions_created" in data
            assert "archive_size_bytes" in data
            assert "duration_s" in data

    def test_archive_tool_error_handling(self):
        """Verify archive raises on nonexistent catalog."""
        import pytest
        with pytest.raises(Exception):
            archive_tool(catalog_dir="/nonexistent/path", project="test")


class TestCheckTool:
    """Test check MCP tool."""

    def test_check_tool_returns_check_results(self, tmp_catalog, sample_run, tmp_path):
        """Verify check tool returns results."""
        from bathos.catalog import write_run

        write_run(sample_run, tmp_catalog)

        result = check_tool(catalog_dir=str(tmp_catalog), project_root=str(tmp_path))

        data = result
        assert "results" in data or "error" in data
        # Check results may have errors if not a git repo, but should return valid JSON
        assert isinstance(data, dict)

    def test_check_tool_error_handling(self, tmp_path):
        """Verify check handles errors gracefully."""
        # Non-existent catalog should return empty results
        result = check_tool(catalog_dir="/nonexistent/path", project_root=str(tmp_path))
        data = result
        assert isinstance(data, dict)
        # Can either have error or empty results
        if "error" not in data:
            assert "results" in data


class TestCapabilityProbeTool:
    """Test capability_probe MCP tool (B2-06, AC-20)."""

    def test_capability_probe_on_compacted_catalog(self, tmp_catalog, sample_run):
        import pytest

        pytest.importorskip("scipy")
        from bathos.catalog import write_run
        from bathos.compact import compact

        write_run(sample_run, tmp_catalog)
        compact(tmp_catalog)

        result = capability_probe_tool(catalog_dir=str(tmp_catalog))
        assert result["seed_live"] is True
        assert result["missing_seed_columns"] == []
        assert result["stats_battery_live"] is True
        assert result["stats_unavailable_reason"] == ""

    def test_capability_probe_on_empty_catalog(self, tmp_catalog):
        result = capability_probe_tool(catalog_dir=str(tmp_catalog))
        assert result["seed_live"] is False
        assert set(result["missing_seed_columns"]) == {
            "seed",
            "baseline_hpo_trials",
            "baseline_hpo_compute_budget",
        }


class TestSyncTool:
    """Test sync MCP tool."""

    def test_sync_tool_requires_remote_name(self):
        """Verify sync requires remote_name parameter."""
        result = sync_tool(catalog_dir="", remote_name="")
        data = result
        assert "error" in data

    @patch("bathos.mcp.sync_catalog")
    @patch("bathos.mcp.load_project_config")
    @patch("bathos.mcp.find_project_config")
    def test_sync_tool_calls_sync_module(self, mock_find_config, mock_load_config, mock_sync):
        """Verify sync tool calls sync module."""
        from bathos.config import ProjectConfig
        from bathos.sync import SyncResult

        mock_config_path = Path("/tmp/.bth.toml")
        mock_config = ProjectConfig(slug="test", root=Path("/tmp"), remotes={"origin": {}})
        mock_find_config.return_value = mock_config_path
        mock_load_config.return_value = mock_config

        mock_result = SyncResult(transferred=10, duration_s=1.5, remote="origin")
        mock_sync.return_value = mock_result

        result = sync_tool(catalog_dir="/tmp", remote_name="origin", pull=False)

        data = result
        assert "transferred" in data
        assert "duration_s" in data
        assert "remote" in data

    def test_sync_tool_error_handling(self):
        """Verify sync raises on config error."""
        import pytest
        with pytest.raises(Exception):
            sync_tool(catalog_dir="/nonexistent/path", remote_name="origin")


class TestInitTool:
    """Test init MCP tool."""

    def test_init_tool_requires_slug(self):
        """Verify init requires slug parameter."""
        result = init_tool(project_root="", slug="")
        data = result
        assert "error" in data

    def test_init_tool_initializes_project(self, tmp_path):
        """Verify init tool initializes project."""
        project_root = tmp_path / "test_project"
        project_root.mkdir()
        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir()

        result = init_tool(
            project_root=str(project_root),
            catalog_dir=str(catalog_dir),
            slug="test_proj",
        )

        data = result
        assert data["initialized"] is True
        assert data["slug"] == "test_proj"
        # Verify .bth.toml was created
        assert (project_root / ".bth.toml").exists()

    def test_init_tool_error_handling(self):
        """Verify init raises on nonexistent project root."""
        import pytest
        with pytest.raises(Exception):
            init_tool(project_root="/nonexistent/path", slug="test")


class TestRunTool:
    """Test run MCP tool."""

    def test_run_tool_requires_script_path(self):
        """Verify run requires script_path parameter."""
        result = run_tool(script_path="", args=[])
        data = result
        assert "error" in data

    @patch("bathos.mcp.run_script")
    def test_run_tool_executes_script(self, mock_run):
        """Verify run tool executes script."""
        def fake_run_script(**kwargs):
            run_uuid_var.set("11111111-1111-1111-1111-111111111111")
            return 0

        mock_run.side_effect = fake_run_script

        result = run_tool(script_path="/tmp/script.py", args=["--n", "10"])

        data = result
        assert "exit_code" in data
        assert data["exit_code"] == 0
        assert data["success"] is True
        assert data["run_id"] == "11111111-1111-1111-1111-111111111111"

    @patch("bathos.mcp.run_script")
    def test_run_tool_does_not_leak_a_stale_run_id(self, mock_run):
        """A call whose run_script never sets run_uuid_var (e.g. an early-return
        gate/sidecar failure, which bails before any Run is constructed) must not
        report a previous call's run_id in the same context."""
        # First call: succeeds, sets run_uuid_var to a real id.
        def succeeding_run(**kwargs):
            run_uuid_var.set("22222222-2222-2222-2222-222222222222")
            return 0

        mock_run.side_effect = succeeding_run
        first = run_tool(script_path="/tmp/script.py")
        assert first["run_id"] == "22222222-2222-2222-2222-222222222222"

        # Second call: simulates an early-return failure that never sets
        # run_uuid_var at all (mirrors runner.py's invalid-sidecar/gate-check
        # paths, which `return 1` before touching the var).
        def failing_run_no_run_created(**kwargs):
            return 1

        mock_run.side_effect = failing_run_no_run_created
        second = run_tool(script_path="/tmp/script.py")
        assert second["exit_code"] == 1
        assert second["run_id"] == "", "must not leak the prior call's run_id"

    def test_run_tool_error_handling(self):
        """Verify run returns nonzero exit code for nonexistent script."""
        result = run_tool(script_path="/nonexistent/script.py")
        # Should return exit code indicating failure
        assert "exit_code" in result
        assert result["exit_code"] != 0
        assert result["success"] is False


class TestMCPServerStartup:
    """Test MCP server initialization."""

    def test_mcp_server_starts(self):
        """Verify FastMCP app is initialized."""
        assert app is not None
        assert hasattr(app, "run")

    def test_mcp_server_has_all_tools(self):
        """Verify all 10 tools are registered."""
        # FastMCP tools are registered via @app.tool decorator
        # We can verify by checking that the decorated functions exist
        assert callable(list_runs_tool)
        assert callable(find_runs_tool)
        assert callable(get_run_tool)
        assert callable(run_sql_tool)
        assert callable(compact_tool)
        assert callable(archive_tool)
        assert callable(check_tool)
        assert callable(sync_tool)
        assert callable(init_tool)
        assert callable(run_tool)

    def test_wired_mcp_tool_names_match_expected(self):
        """Every tool actually registered on the FastMCP server (via
        cisternal.wire()) must be exposed under its intended short name,
        not the Python wrapper function's own name (e.g. list_runs, not
        mcp_list_runs_tool). Regression test: cisternal.wire() previously
        dropped the @cisternal.tool(name=...) override when registering on
        FastMCP, silently exposing 40 of the 50 tools under their raw
        mcp_x_tool wrapper name instead (maraxen/cisternal#6)."""
        tools = asyncio.run(app.list_tools())
        wired_names = sorted(t.name for t in tools)

        assert wired_names == sorted(_WIRED.mcp_tools), (
            "FastMCP-registered tool names diverge from cisternal.wire()'s "
            f"own registry snapshot. Wired on server: {wired_names}"
        )
        # No mcp_x_tool wrapper names should ever leak onto the server.
        leaked = [n for n in wired_names if n.startswith("mcp_") and n.endswith("_tool")]
        assert not leaked, f"Raw wrapper function names leaked as tool names: {leaked}"
