"""Tests for sprint_audit module."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bathos.catalog import init_catalog, write_run
from bathos.compact import compact
from bathos.schema import Run


@pytest.fixture
def monkeypatch_registry(monkeypatch, tmp_path) -> None:
    """Monkey-patch the registry to a temporary location."""
    registry_path = tmp_path / "projects.toml"
    monkeypatch.setattr(
        "bathos.config.PROJECTS_REGISTRY",
        registry_path,
    )


def test_sprint_audit_empty_registry(monkeypatch_registry):
    """Test that audit returns empty results with no registered projects."""
    from bathos.sprint_audit import sprint_audit

    result = sprint_audit(hours=24)
    assert result["audit_results"] == {}
    assert result["warnings"] == []


def test_sprint_audit_skips_incompatible_schema(monkeypatch_registry, tmp_path, monkeypatch):
    """Test that audit skips projects with incompatible schema versions."""
    from bathos.config import register_project
    from bathos.sprint_audit import sprint_audit

    # Create a catalog dir with a fake warm DB that has wrong schema version
    catalog_dir = tmp_path / "fake_catalog"
    catalog_dir.mkdir()

    # Register the project
    register_project(slug="test_project", catalog_dir=catalog_dir)

    # Create a fake warm DB with wrong schema version
    import duckdb

    db = duckdb.connect(str(catalog_dir / "bathos.db"))
    db.execute(
        """
        CREATE TABLE _schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """
    )
    db.execute(
        "INSERT INTO _schema_meta (key, value) VALUES ('warm_version', '999')"
    )
    db.close()

    result = sprint_audit(hours=24)
    assert "test_project" not in result["audit_results"]
    assert any("schema version mismatch" in w for w in result["warnings"])


def test_sprint_audit_reports_anomalies(monkeypatch_registry, tmp_path, monkeypatch):
    """Test that audit detects and reports anomalies."""
    from bathos.config import register_project
    from bathos.sprint_audit import sprint_audit

    catalog_dir = tmp_path / "test_catalog"
    catalog_dir.mkdir()

    # Initialize catalog and write some runs
    init_catalog(catalog_dir)

    base_time = datetime.now(UTC)
    runs = [
        Run(
            project_slug="test_project",
            command="python test.py",
            argv=["python", "test.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            timestamp=base_time,
            status="completed",
            exit_code=0,
            outcome="pass",
            sidecar_mode="normal",
            outcome_is_residual=False,
        ),
        Run(
            project_slug="test_project",
            command="python test.py",
            argv=["python", "test.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            timestamp=base_time + timedelta(seconds=1),
            status="completed",
            exit_code=0,
            outcome="",  # Unknown outcome
            sidecar_mode="normal",
            outcome_is_residual=False,
        ),
    ]

    for r in runs:
        write_run(r, catalog_dir)

    # Compact to warm DB
    compact(catalog_dir)

    # Register the project
    register_project(slug="test_project", catalog_dir=catalog_dir)

    result = sprint_audit(hours=24)
    assert "test_project" in result["audit_results"]
    audit_data = result["audit_results"]["test_project"]
    assert audit_data["runs"] == 2
    assert any("unknown outcome" in str(a) for a in audit_data["anomalies"])
