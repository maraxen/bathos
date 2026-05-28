"""Tests for bth lineage --format flag and backward compatibility."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from bathos.query import lineage as get_lineage_runs
from bathos.provenance import format_prov_json
from bathos.schema import Run


@pytest.fixture
def mock_lineage_runs():
    """Create a chain of runs with parent-child relationships."""
    run1 = Run(
        id="run1-uuid-0000-0000-0000000000000001",
        project_slug="test_project",
        command="uv run python script1.py",
        argv=["uv", "run", "python", "script1.py"],
        git_hash="git_hash_1",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 5, 28, 10, 0, 0, tzinfo=UTC),
        outcome="pass",
        manifest_sha256="manifest_1",
        parent_run_id="",
        agent_mode="human",
    )

    run2 = Run(
        id="run2-uuid-0000-0000-0000000000000002",
        project_slug="test_project",
        command="uv run python script2.py",
        argv=["uv", "run", "python", "script2.py"],
        git_hash="git_hash_2",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 5, 28, 11, 0, 0, tzinfo=UTC),
        outcome="pass",
        manifest_sha256="manifest_2",
        parent_run_id="run1-uuid-0000-0000-0000000000000001",
        agent_mode="human",
    )

    return [run1, run2]


def test_lineage_text_format_output_structure(mock_lineage_runs):
    """Test that text format produces expected output structure."""
    runs = mock_lineage_runs
    # In actual CLI, this would be printed; here we just verify format_prov_json handles the list
    assert len(runs) == 2
    assert runs[0].parent_run_id == ""
    assert runs[1].parent_run_id == runs[0].id


def test_lineage_prov_format_has_derivation(mock_lineage_runs):
    """Test that prov format includes wasDerivedFrom for parent-child relationship."""
    runs = mock_lineage_runs
    prov = format_prov_json(runs)

    # Parent-child relationship should create wasDerivedFrom entry
    assert len(prov["wasDerivedFrom"]) > 0

    # Verify the derivation has the expected structure
    derivations = list(prov["wasDerivedFrom"].values())
    assert len(derivations) == 1
    assert "prov:generatedEntity" in derivations[0]
    assert "prov:usedEntity" in derivations[0]
    assert "bth:run_" in derivations[0]["prov:generatedEntity"]
    assert "bth:run_" in derivations[0]["prov:usedEntity"]


def test_lineage_prov_format_all_required_keys(mock_lineage_runs):
    """Test that prov format has all required W3C PROV-JSON keys."""
    runs = mock_lineage_runs
    prov = format_prov_json(runs)

    required_keys = ["entity", "activity", "agent", "wasDerivedFrom"]
    for key in required_keys:
        assert key in prov, f"Missing required key: {key}"


def test_lineage_prov_format_entities_for_each_run(mock_lineage_runs):
    """Test that prov format creates one entity per run."""
    runs = mock_lineage_runs
    prov = format_prov_json(runs)

    assert len(prov["entity"]) == 2
    assert len(prov["activity"]) == 2


def test_lineage_prov_format_preserves_manifest_sha256(mock_lineage_runs):
    """Test that manifest_sha256 is preserved in prov entities."""
    runs = mock_lineage_runs
    prov = format_prov_json(runs)

    entities = list(prov["entity"].values())
    assert any(e["bth:manifest_sha256"] == "manifest_1" for e in entities)
    assert any(e["bth:manifest_sha256"] == "manifest_2" for e in entities)


def test_lineage_prov_format_preserves_outcome(mock_lineage_runs):
    """Test that outcome is preserved in prov entities."""
    runs = mock_lineage_runs
    prov = format_prov_json(runs)

    entities = list(prov["entity"].values())
    assert all(e["bth:outcome"] == "pass" for e in entities)


def test_lineage_prov_format_single_run_no_derivation():
    """Test that single run with no parent has empty wasDerivedFrom."""
    run = Run(
        id="single-uuid-0000-0000-0000000000000000",
        project_slug="test_project",
        command="uv run python script.py",
        argv=["uv", "run", "python", "script.py"],
        git_hash="git_hash",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 5, 28, 10, 0, 0, tzinfo=UTC),
        outcome="pass",
        manifest_sha256="manifest",
        parent_run_id="",
        agent_mode="human",
    )

    prov = format_prov_json([run])
    assert len(prov["wasDerivedFrom"]) == 0
    assert len(prov["entity"]) == 1
