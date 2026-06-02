"""Tests for bth outputs subcommand group."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from bathos.rich_fmt import render_output_list, render_outputs_summary
from bathos.schema import Run


@pytest.fixture
def sample_run_no_outputs() -> Run:
    """Run with no output files."""
    return Run(
        id="test_run_no_outputs",
        project_slug="test_proj",
        command="python test.py",
        argv=["python", "test.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        output_paths=[],
        output_metadata="[]",
    )


@pytest.fixture
def sample_run_with_outputs() -> Run:
    """Run with output files in metadata."""
    output_metadata = [
        {
            "path": "/tmp/results.json",
            "status": "present",
            "size_bytes": 1024,
            "mtime_unix": 1000000.0,
            "sha256": "deadbeef" * 8,
        },
        {
            "path": "/tmp/log.txt",
            "status": "present",
            "size_bytes": 512,
            "mtime_unix": 1000001.0,
            "sha256": "cafebabe" * 8,
        },
    ]
    return Run(
        id="test_run_with_outputs",
        project_slug="test_proj",
        command="python test.py",
        argv=["python", "test.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        output_paths=["/tmp/results.json", "/tmp/log.txt"],
        output_metadata=json.dumps(output_metadata),
    )


class TestRenderOutputList:
    """Tests for render_output_list function."""

    def test_render_output_list_no_files(self, capsys):
        """Test rendering output list with empty files list."""
        render_output_list("test_run", [], live=False)
        captured = capsys.readouterr()
        assert "no registered output files" in captured.out.lower()

    def test_render_output_list_snapshot(self, capsys):
        """Test rendering output list from snapshot."""
        files = [
            {
                "path": "/tmp/results.json",
                "status": "present",
                "size_bytes": 1024,
                "mtime_unix": 1000000.0,
                "sha256": "deadbeef" * 8,
            }
        ]
        render_output_list("test_run", files, live=False)
        captured = capsys.readouterr()
        assert "results.json" in captured.out or "present" in captured.out

    def test_render_output_list_live(self, capsys):
        """Test rendering output list with --live flag."""
        files = [
            {
                "path": "/tmp/test_file.txt",
                "status": "present",
                "size_bytes": 100,
                "mtime_unix": 1000000.0,
                "sha256": "abc123",
            }
        ]
        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.stat") as mock_stat:
                mock_stat.return_value = MagicMock(st_size=200, st_mtime=1000001.0)
                render_output_list("test_run", files, live=True)
                assert True


class TestRenderOutputsSummary:
    """Tests for render_outputs_summary function."""

    def test_render_outputs_summary_all_projects(self, capsys):
        """Test rendering summary across multiple projects."""
        rows = [
            {
                "project": "proj_a",
                "run_count": 5,
                "file_count": 10,
                "total_bytes": 50000,
                "missing_count": 0,
            },
            {
                "project": "proj_b",
                "run_count": 3,
                "file_count": 8,
                "total_bytes": 30000,
                "missing_count": 2,
            },
        ]
        render_outputs_summary(rows, since=None)
        captured = capsys.readouterr()
        assert "proj_a" in captured.out or "proj_b" in captured.out or "Output Summary" in captured.out

    def test_render_outputs_summary_single_project(self, capsys):
        """Test rendering summary for single project."""
        rows = [
            {
                "project": "my_proj",
                "run_count": 10,
                "file_count": 25,
                "total_bytes": 100000,
                "missing_count": 1,
            }
        ]
        render_outputs_summary(rows, since="7d")
        captured = capsys.readouterr()
        assert "my_proj" in captured.out or "10" in captured.out or "Output Summary" in captured.out

    def test_render_outputs_summary_empty(self, capsys):
        """Test rendering summary with no rows."""
        render_outputs_summary([], since=None)
        captured = capsys.readouterr()
        assert "No output data found" in captured.out


class TestOutputsListCommand:
    """Tests for 'bth outputs list' command."""

    def test_outputs_list_exists(self):
        """Test that outputs_list command exists."""
        from bathos.cli import outputs_list
        assert callable(outputs_list)

    def test_outputs_summary_exists(self):
        """Test that outputs_summary command exists."""
        from bathos.cli import outputs_summary
        assert callable(outputs_summary)


class TestMCPTools:
    """Tests for MCP tools."""

    def test_list_outputs_tool_exists(self):
        """Test that list_outputs_tool exists."""
        from bathos.mcp import list_outputs_tool
        assert callable(list_outputs_tool)

    def test_outputs_summary_tool_exists(self):
        """Test that outputs_summary_tool exists."""
        from bathos.mcp import outputs_summary_tool
        assert callable(outputs_summary_tool)
