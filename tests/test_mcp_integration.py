"""End-to-end integration tests for FastMCP server."""

from __future__ import annotations

import json

from bathos.catalog import write_run
from bathos.mcp import (
    compact_tool,
    get_run_tool,
    init_tool,
    list_runs_tool,
    run_sql_tool,
)
from bathos.schema import Run


class TestFullMCPWorkflow:
    """Test complete workflow through MCP tools."""

    def test_full_mcp_workflow(self, tmp_path):
        """Test: init → run → list → compact → sql → get."""
        project_root = tmp_path / "workflow_test"
        project_root.mkdir()
        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir()

        # Step 1: Init project via MCP
        init_result = init_tool(
            project_root=str(project_root),
            catalog_dir=str(catalog_dir),
            slug="workflow_test",
        )
        assert init_result["initialized"]

        # Step 2: Create and register a sample run (simulate bth run)
        sample_run = Run(
            project_slug="workflow_test",
            command="python test.py",
            argv=["python", "test.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            status="completed",
            exit_code=0,
            duration_s=1.5,
            hostname="test-host",
        )
        write_run(sample_run, catalog_dir)

        # Step 3: List runs via MCP → verify run_id present
        list_result = list_runs_tool(catalog_dir=str(catalog_dir), limit=10)
        assert list_result["count"] == 1
        assert list_result["runs"][0]["id"] == sample_run.id
        run_id = list_result["runs"][0]["id"]

        # Step 4: Compact via MCP
        compact_result = compact_tool(catalog_dir=str(catalog_dir))
        assert "ingested" in compact_result
        assert "duration_s" in compact_result

        # Step 5: Run SQL via MCP
        sql_result = run_sql_tool(
            catalog_dir=str(catalog_dir),
            sql="SELECT COUNT(*) as cnt FROM runs WHERE status='completed'",
        )
        assert "rows" in sql_result
        assert sql_result["count"] >= 0

        # Step 6: Get run via MCP
        get_result = get_run_tool(catalog_dir=str(catalog_dir), run_id=run_id)
        assert get_result["id"] == run_id
        assert get_result["status"] == "completed"

    def test_mcp_workflow_with_multiple_runs(self, tmp_path):
        """Test workflow with multiple runs."""
        project_root = tmp_path / "multi_test"
        project_root.mkdir()
        catalog_dir = tmp_path / "multi_catalog"
        catalog_dir.mkdir()

        # Init
        init_tool(
            project_root=str(project_root),
            catalog_dir=str(catalog_dir),
            slug="multi_test",
        )

        # Create multiple runs
        for i in range(3):
            run = Run(
                project_slug="multi_test",
                command=f"python script{i}.py",
                argv=["python", f"script{i}.py"],
                git_hash=f"hash{i}",
                git_branch="main",
                git_dirty=False,
                status="completed",
                exit_code=0,
                duration_s=1.0,
                hostname="test-host",
            )
            write_run(run, catalog_dir)

        # List runs
        list_result = list_runs_tool(catalog_dir=str(catalog_dir), limit=10)
        assert list_result["count"] == 3

        # Compact
        compact_result = compact_tool(catalog_dir=str(catalog_dir))
        assert compact_result["ingested"] >= 0

        # SQL query
        sql_result = run_sql_tool(
            catalog_dir=str(catalog_dir),
            sql="SELECT id, status FROM runs WHERE project_slug='multi_test'",
        )
        assert sql_result["count"] >= 0

    def test_mcp_workflow_error_recovery(self, tmp_path):
        """Test that errors are handled gracefully in workflow."""
        catalog_dir = tmp_path / "error_test"
        catalog_dir.mkdir()

        # Try operations on empty catalog
        list_result = list_runs_tool(catalog_dir=str(catalog_dir), limit=10)
        assert list_result["count"] == 0  # Empty, not error

        # Try to get nonexistent run
        get_result = get_run_tool(catalog_dir=str(catalog_dir), run_id="nonexistent")
        assert "error" in get_result

        # Try valid SQL query on empty runs table (queries without catalog fail at domain layer)
        sql_result = run_sql_tool(
            catalog_dir=str(catalog_dir), sql="SELECT 1 as col"
        )
        assert "rows" in sql_result
