"""Tests for FastAPI visualization server."""
from __future__ import annotations

import pytest
from datetime import UTC, datetime

# Skip if fastapi not available
pytest.importorskip("fastapi")

from bathos.schema import Run
from bathos.campaigns import Campaign
from bathos.viz.server import create_app


def _make_run(
    project_slug: str = "test-project",
    command: str = "python test.py",
    argv: list[str] | None = None,
    git_hash: str = "abc123def456",
    git_branch: str = "main",
    git_dirty: bool = False,
    **kwargs,
) -> Run:
    """Helper to create a Run with minimal required fields."""
    if argv is None:
        argv = ["test.py"]
    return Run(
        project_slug=project_slug,
        command=command,
        argv=argv,
        git_hash=git_hash,
        git_branch=git_branch,
        git_dirty=git_dirty,
        **kwargs,
    )


def test_fastapi_app_get_root():
    """Test that GET / returns 200 with HTML containing run info."""
    from fastapi.testclient import TestClient

    run = _make_run(project_slug="my-test", id="run-12345")
    app = create_app([run])
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    # Verify that the run is represented in the HTML
    # Either the short ID or project slug should appear
    assert (
        "run-12345" in response.text or "my-test" in response.text
    ), "Run info should appear in HTML"


def test_fastapi_app_empty():
    """Test that GET / returns 200 with empty runs list."""
    from fastapi.testclient import TestClient

    app = create_app([])
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    # Should still have valid HTML structure
    assert "<html" in response.text.lower()
