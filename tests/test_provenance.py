"""Tests for W3C PROV-JSON format for lineage."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from bathos.provenance import format_prov_json
from bathos.schema import Run


@pytest.fixture
def mock_run_single():
    """Create a single Run with no parent."""
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
        parent_run_id="",  # No parent
        agent_mode="human",
    )


@pytest.fixture
def mock_run_with_parent():
    """Create a Run that has a parent."""
    parent_id = str(uuid4())
    child_id = str(uuid4())
    return {
        "parent": Run(
            id=parent_id,
            project_slug="test_project",
            command="uv run python parent.py",
            argv=["uv", "run", "python", "parent.py"],
            git_hash="parent_hash",
            git_branch="main",
            git_dirty=False,
            timestamp=datetime(2026, 5, 28, 11, 0, 0, tzinfo=UTC),
            outcome="pass",
            manifest_sha256="parent_manifest",
            parent_run_id="",
            agent_mode="human",
        ),
        "child": Run(
            id=child_id,
            project_slug="test_project",
            command="uv run python child.py",
            argv=["uv", "run", "python", "child.py"],
            git_hash="child_hash",
            git_branch="main",
            git_dirty=False,
            timestamp=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
            outcome="pass",
            manifest_sha256="child_manifest",
            parent_run_id=parent_id,
            agent_mode="human",
        ),
    }


def test_prov_json_has_entity_key(mock_run_single):
    """Test that PROV-JSON has entity key."""
    result = format_prov_json([mock_run_single])
    assert "entity" in result
    assert "activity" in result
    assert "agent" in result
    assert "wasDerivedFrom" in result


def test_prov_json_single_run_no_derivation(mock_run_single):
    """Test that single run with no parent has empty wasDerivedFrom."""
    result = format_prov_json([mock_run_single])
    assert len(result["wasDerivedFrom"]) == 0
    assert len(result["entity"]) == 1


def test_prov_json_run_with_parent_has_derivation(mock_run_with_parent):
    """Test that run with parent has wasDerivedFrom entry."""
    runs = [mock_run_with_parent["parent"], mock_run_with_parent["child"]]
    result = format_prov_json(runs)
    # Child has parent, so should have at least one derivation
    assert len(result["wasDerivedFrom"]) >= 1


def test_prov_json_entity_has_manifest_sha256(mock_run_single):
    """Test that entity includes manifest_sha256."""
    result = format_prov_json([mock_run_single])
    entities = result["entity"]
    entity_key = list(entities.keys())[0]
    assert "bth:manifest_sha256" in entities[entity_key]


def test_prov_json_entity_has_run_id(mock_run_single):
    """Test that entity includes run_id."""
    result = format_prov_json([mock_run_single])
    entities = result["entity"]
    entity_key = list(entities.keys())[0]
    assert "bth:run_id" in entities[entity_key]
    assert entities[entity_key]["bth:run_id"] == mock_run_single.id


def test_prov_json_entity_has_outcome(mock_run_single):
    """Test that entity includes outcome."""
    result = format_prov_json([mock_run_single])
    entities = result["entity"]
    entity_key = list(entities.keys())[0]
    assert "bth:outcome" in entities[entity_key]
    assert entities[entity_key]["bth:outcome"] == "pass"


def test_prov_json_entity_has_git_sha(mock_run_single):
    """Test that entity includes git SHA."""
    result = format_prov_json([mock_run_single])
    entities = result["entity"]
    entity_key = list(entities.keys())[0]
    assert "bth:git_sha" in entities[entity_key]
    assert entities[entity_key]["bth:git_sha"] == "abc123def456"


def test_prov_json_entity_has_timestamp(mock_run_single):
    """Test that entity includes timestamp."""
    result = format_prov_json([mock_run_single])
    entities = result["entity"]
    entity_key = list(entities.keys())[0]
    assert "bth:timestamp" in entities[entity_key]


def test_prov_json_activity_has_run_id(mock_run_single):
    """Test that activity includes run_id."""
    result = format_prov_json([mock_run_single])
    activities = result["activity"]
    activity_key = list(activities.keys())[0]
    assert "bth:run_id" in activities[activity_key]


def test_prov_json_agent_has_id(mock_run_single):
    """Test that agent includes id."""
    result = format_prov_json([mock_run_single])
    agents = result["agent"]
    assert len(agents) > 0
    agent_key = list(agents.keys())[0]
    assert "bth:id" in agents[agent_key]


def test_prov_json_derivation_links_parent_child(mock_run_with_parent):
    """Test that wasDerivedFrom correctly links parent to child."""
    runs = [mock_run_with_parent["parent"], mock_run_with_parent["child"]]
    result = format_prov_json(runs)
    derivations = result["wasDerivedFrom"]
    assert len(derivations) >= 1


def test_prov_json_multiple_runs_creates_multiple_entities(mock_run_with_parent):
    """Test that multiple runs create multiple entities."""
    runs = [mock_run_with_parent["parent"], mock_run_with_parent["child"]]
    result = format_prov_json(runs)
    assert len(result["entity"]) == 2
    assert len(result["activity"]) == 2


class TestMultiParentWasDerivedFrom:
    """B2-03: run_parent_edges enables multiple wasDerivedFrom links per run."""

    def _make_run(self, run_id: str, parent_run_id: str = ""):
        return Run(
            id=run_id,
            project_slug="test_project",
            command=f"uv run python {run_id}.py",
            argv=["uv", "run", "python", f"{run_id}.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            timestamp=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
            outcome="pass",
            manifest_sha256="sha256_x",
            parent_run_id=parent_run_id,
            agent_mode="human",
        )

    def test_run_with_two_parents_emits_two_links(self):
        parent_a = self._make_run("aaaaaaaa-0000-0000-0000-000000000000")
        parent_b = self._make_run("bbbbbbbb-0000-0000-0000-000000000000")
        child = self._make_run("cccccccc-0000-0000-0000-000000000000")
        runs = [parent_a, parent_b, child]

        result = format_prov_json(
            runs,
            run_parent_edges={child.id: [parent_a.id, parent_b.id]},
        )
        assert len(result["wasDerivedFrom"]) == 2
        used_entities = {v["prov:usedEntity"] for v in result["wasDerivedFrom"].values()}
        assert used_entities == {f"bth:run_{parent_a.id[:8]}", f"bth:run_{parent_b.id[:8]}"}

    def test_run_absent_from_edges_falls_back_to_parent_run_id(self):
        # run_parent_edges is supplied but does NOT mention this run -- must fall back to
        # the existing single parent_run_id field, not silently drop the link.
        parent = self._make_run("aaaaaaaa-0000-0000-0000-000000000000")
        child = self._make_run("cccccccc-0000-0000-0000-000000000000", parent_run_id=parent.id)
        runs = [parent, child]

        result = format_prov_json(runs, run_parent_edges={})
        assert len(result["wasDerivedFrom"]) == 1

    def test_omitting_run_parent_edges_is_byte_identical_to_pre_b2_03(self, mock_run_with_parent):
        runs = [mock_run_with_parent["parent"], mock_run_with_parent["child"]]
        with_default = format_prov_json(runs)
        with_explicit_none = format_prov_json(runs, run_parent_edges=None)
        assert with_default == with_explicit_none
