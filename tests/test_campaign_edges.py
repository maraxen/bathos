"""Tests for B2-03 campaign_edges / run_edges (#2181, fork 7).

B2-03's own acceptance criterion: "campaign_edges round-trip + cycle-rejection contract test ->
exit 1 on any lossy round-trip or accepted cycle." TestCampaignEdgesRoundTrip / TestRunEdgesRoundTrip
and TestCampaignEdgesCycleRejection / TestRunEdgesCycleRejection are that contract test.
"""

from pathlib import Path

import duckdb
import pytest

from bathos.campaign_edges import (
    CycleRejectedError,
    add_campaign_edge,
    add_run_edge,
    get_campaign_parents,
    get_run_parents,
)
from bathos.catalog import init_catalog, write_run
from bathos.compact import compact
from bathos.schema import Run


@pytest.fixture
def warm_db(tmp_catalog: Path, sample_run: Run):
    """A compacted catalog (so campaign_edges/run_edges tables exist), open connection."""
    init_catalog(tmp_catalog)
    write_run(sample_run, tmp_catalog)
    compact(tmp_catalog)
    con = duckdb.connect(str(tmp_catalog / "bathos.db"))
    yield con
    con.close()


class TestCampaignEdgesRoundTrip:
    def test_single_parent_round_trips(self, warm_db):
        add_campaign_edge(warm_db, "child-1", "parent-1")
        assert get_campaign_parents(warm_db, "child-1") == ["parent-1"]

    def test_multi_parent_round_trips_lossless(self, warm_db):
        add_campaign_edge(warm_db, "child-1", "parent-a")
        add_campaign_edge(warm_db, "child-1", "parent-b")
        add_campaign_edge(warm_db, "child-1", "parent-c")
        assert get_campaign_parents(warm_db, "child-1") == ["parent-a", "parent-b", "parent-c"]

    def test_no_parents_round_trips_empty(self, warm_db):
        assert get_campaign_parents(warm_db, "orphan-campaign") == []

    def test_duplicate_edge_insert_is_idempotent(self, warm_db):
        add_campaign_edge(warm_db, "child-1", "parent-1")
        add_campaign_edge(
            warm_db, "child-1", "parent-1"
        )  # re-assert, should not error or duplicate
        assert get_campaign_parents(warm_db, "child-1") == ["parent-1"]

    def test_diamond_shape_round_trips(self, warm_db):
        # A diamond: D's parents are B and C; both B and C's parent is A.
        add_campaign_edge(warm_db, "B", "A")
        add_campaign_edge(warm_db, "C", "A")
        add_campaign_edge(warm_db, "D", "B")
        add_campaign_edge(warm_db, "D", "C")
        assert get_campaign_parents(warm_db, "D") == ["B", "C"]
        assert get_campaign_parents(warm_db, "B") == ["A"]
        assert get_campaign_parents(warm_db, "C") == ["A"]

    def test_edges_for_different_children_are_independent(self, warm_db):
        add_campaign_edge(warm_db, "child-1", "parent-x")
        add_campaign_edge(warm_db, "child-2", "parent-y")
        assert get_campaign_parents(warm_db, "child-1") == ["parent-x"]
        assert get_campaign_parents(warm_db, "child-2") == ["parent-y"]


class TestCampaignEdgesCycleRejection:
    def test_self_loop_rejected(self, warm_db):
        with pytest.raises(CycleRejectedError):
            add_campaign_edge(warm_db, "campaign-1", "campaign-1")

    def test_direct_two_cycle_rejected(self, warm_db):
        add_campaign_edge(warm_db, "B", "A")  # B's parent is A
        with pytest.raises(CycleRejectedError):
            add_campaign_edge(warm_db, "A", "B")  # A's parent is B -> A -> B -> A cycle

    def test_indirect_three_cycle_rejected(self, warm_db):
        add_campaign_edge(warm_db, "B", "A")  # B -> A
        add_campaign_edge(warm_db, "C", "B")  # C -> B -> A
        with pytest.raises(CycleRejectedError):
            add_campaign_edge(warm_db, "A", "C")  # A -> C -> B -> A cycle

    def test_diamond_shape_does_not_falsely_reject(self, warm_db):
        # A diamond is NOT a cycle -- D having two parents B and C that share a common
        # ancestor A must not be mistaken for a cycle.
        add_campaign_edge(warm_db, "B", "A")
        add_campaign_edge(warm_db, "C", "A")
        add_campaign_edge(warm_db, "D", "B")
        add_campaign_edge(warm_db, "D", "C")  # must NOT raise
        assert set(get_campaign_parents(warm_db, "D")) == {"B", "C"}

    def test_rejected_cycle_does_not_partially_insert(self, warm_db):
        add_campaign_edge(warm_db, "B", "A")
        with pytest.raises(CycleRejectedError):
            add_campaign_edge(warm_db, "A", "B")
        # The rejected edge must not have been inserted despite raising.
        assert get_campaign_parents(warm_db, "A") == []


class TestRunEdgesRoundTrip:
    def test_multi_parent_round_trips_lossless(self, warm_db):
        add_run_edge(warm_db, "run-child", "run-parent-a")
        add_run_edge(warm_db, "run-child", "run-parent-b")
        assert get_run_parents(warm_db, "run-child") == ["run-parent-a", "run-parent-b"]

    def test_no_parents_round_trips_empty(self, warm_db):
        assert get_run_parents(warm_db, "orphan-run") == []


class TestRunEdgesCycleRejection:
    def test_self_loop_rejected(self, warm_db):
        with pytest.raises(CycleRejectedError):
            add_run_edge(warm_db, "run-1", "run-1")

    def test_indirect_cycle_rejected(self, warm_db):
        add_run_edge(warm_db, "run-B", "run-A")
        add_run_edge(warm_db, "run-C", "run-B")
        with pytest.raises(CycleRejectedError):
            add_run_edge(warm_db, "run-A", "run-C")

    def test_campaign_edges_and_run_edges_are_independent_graphs(self, warm_db):
        # A cycle in campaign_edges must not spuriously block an identically-named edge in
        # run_edges (separate tables, separate cycle checks).
        add_campaign_edge(warm_db, "X", "Y")
        add_run_edge(warm_db, "X", "Y")  # same IDs, different table -- must not raise
        assert get_run_parents(warm_db, "X") == ["Y"]
