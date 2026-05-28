"""Tests for bth cite command and citation formatting."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from bathos.cite import format_citation
from bathos.schema import Run


@pytest.fixture
def mock_run_with_manifest():
    """Create a Run with manifest_sha256 and manifest_path set."""
    return Run(
        id=str(uuid4()),
        project_slug="test_project",
        command="uv run python test.py",
        argv=["uv", "run", "python", "test.py"],
        git_hash="abc123def456",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
        outcome="pass",
        manifest_sha256="sha256_1234567890abcdef",
        manifest_path="path/to/manifest.json",
        sidecar_sha256="sidecar_sha256_value",
    )


@pytest.fixture
def mock_run_no_manifest():
    """Create a Run with empty manifest_sha256 and manifest_path (pre-v0.6)."""
    return Run(
        id=str(uuid4()),
        project_slug="test_project",
        command="uv run python test.py",
        argv=["uv", "run", "python", "test.py"],
        git_hash="abc123def456",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
        outcome="pass",
        manifest_sha256="",
        manifest_path="",
        sidecar_sha256="",
    )


def test_cite_markdown_contains_manifest_hash(mock_run_with_manifest):
    """Test that markdown citation includes manifest hash."""
    output = format_citation(mock_run_with_manifest, fmt="markdown")
    assert "manifest" in output.lower()
    assert "sha256_12345678" in output


def test_cite_markdown_contains_run_id(mock_run_with_manifest):
    """Test that markdown citation includes run ID."""
    output = format_citation(mock_run_with_manifest, fmt="markdown")
    assert mock_run_with_manifest.id[:8] in output


def test_cite_pre_v6_run_shows_not_recorded(mock_run_no_manifest):
    """Test that pre-v0.6 runs show 'not recorded' for missing fields."""
    output = format_citation(mock_run_no_manifest, fmt="markdown")
    assert "not recorded" in output


def test_cite_json_format_has_required_keys(mock_run_with_manifest):
    """Test that JSON citation has required keys."""
    import json

    output = format_citation(mock_run_with_manifest, fmt="json")
    data = json.loads(output)
    assert "manifest_sha256" in data
    assert "run_id" in data
    assert "outcome" in data
    assert "timestamp" in data


def test_cite_json_format_values(mock_run_with_manifest):
    """Test that JSON citation has correct values."""
    import json

    output = format_citation(mock_run_with_manifest, fmt="json")
    data = json.loads(output)
    assert data["manifest_sha256"] == "sha256_1234567890abcdef"
    assert data["outcome"] == "pass"
    assert data["run_id"] == mock_run_with_manifest.id


def test_cite_markdown_default_format(mock_run_with_manifest):
    """Test that default format is markdown."""
    output = format_citation(mock_run_with_manifest)  # No fmt specified
    assert "Run" in output
    assert "manifest" in output.lower()


def test_cite_includes_git_sha(mock_run_with_manifest):
    """Test that citation includes git SHA."""
    output = format_citation(mock_run_with_manifest, fmt="markdown")
    assert "abc12345" in output or "abc123de" in output  # First 8 chars


def test_cite_includes_sidecar_sha256(mock_run_with_manifest):
    """Test that citation includes sidecar SHA256."""
    output = format_citation(mock_run_with_manifest, fmt="markdown")
    assert "sidecar" in output.lower()
