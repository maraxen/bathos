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


# ---------------------------------------------------------------------------
# AC-25: edge.added write site behind the runlog flag (delivery step 2b, wave ii).
# ---------------------------------------------------------------------------


def _read_jsonl_dir(log_dir: Path) -> list[dict]:
    import json

    lines = []
    if not log_dir.exists():
        return lines
    for f in sorted(log_dir.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            if line.strip():
                lines.append(json.loads(line))
    return lines


def test_add_campaign_edge_flag_off_is_legacy_only(warm_db, tmp_catalog: Path):
    from bathos.runlog.writer import reset_writers_for_test

    add_campaign_edge(warm_db, "child-off", "parent-off", tmp_catalog)
    assert get_campaign_parents(warm_db, "child-off") == ["parent-off"]
    assert not (Path.home() / ".bth" / "log" / "unaffiliated").exists()
    reset_writers_for_test()


def test_add_campaign_edge_flag_on_emits_event_only(warm_db, tmp_catalog: Path, monkeypatch):
    from bathos.runlog.writer import reset_writers_for_test

    monkeypatch.setenv("BTH_LOG_MODE", "1")
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_catalog))

    add_campaign_edge(warm_db, "child-on", "parent-on", tmp_catalog, cwd=tmp_catalog.parent)

    assert get_campaign_parents(warm_db, "child-on") == []  # no legacy INSERT

    lines = _read_jsonl_dir(Path.home() / ".bth" / "log" / "unaffiliated")
    edge_events = [line for line in lines if line["kind"] == "edge.added"]
    assert len(edge_events) == 1
    assert edge_events[0]["entity"] == ["child-on", "parent-on", "campaign"]
    assert edge_events[0]["data"] == {
        "src": "child-on",
        "dst": "parent-on",
        "type": "campaign",
    }
    reset_writers_for_test()


def test_add_run_edge_flag_off_is_legacy_only(warm_db, tmp_catalog: Path):
    from bathos.runlog.writer import reset_writers_for_test

    add_run_edge(warm_db, "run-child-off", "run-parent-off", tmp_catalog)
    assert get_run_parents(warm_db, "run-child-off") == ["run-parent-off"]
    assert not (Path.home() / ".bth" / "log" / "unaffiliated").exists()
    reset_writers_for_test()


def test_add_run_edge_flag_on_emits_event_only(warm_db, tmp_catalog: Path, monkeypatch):
    from bathos.runlog.writer import reset_writers_for_test

    monkeypatch.setenv("BTH_LOG_MODE", "1")
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_catalog))

    add_run_edge(warm_db, "run-child-on", "run-parent-on", tmp_catalog, cwd=tmp_catalog.parent)

    assert get_run_parents(warm_db, "run-child-on") == []  # no legacy INSERT

    lines = _read_jsonl_dir(Path.home() / ".bth" / "log" / "unaffiliated")
    edge_events = [line for line in lines if line["kind"] == "edge.added"]
    assert len(edge_events) == 1
    assert edge_events[0]["entity"] == ["run-child-on", "run-parent-on", "run"]
    assert edge_events[0]["data"] == {
        "src": "run-child-on",
        "dst": "run-parent-on",
        "type": "run",
    }
    reset_writers_for_test()


# ---------------------------------------------------------------------------
# #2079: MCP tools for campaign_edges and run_edges
# ---------------------------------------------------------------------------


class TestCampaignAndRunEdgeMcpTools:
    def test_campaign_and_run_edges_mcp_roundtrip_legacy(self, warm_db, tmp_catalog: Path, monkeypatch):
        import asyncio

        from bathos import mcp_auth
        from bathos.mcp import (
            mcp_add_campaign_edge_tool,
            mcp_add_run_edge_tool,
            mcp_get_campaign_parents_tool,
            mcp_get_run_parents_tool,
        )

        # Close warm_db fixture connection before opening writable connections via MCP tools.
        warm_db.close()
        token_file = tmp_catalog.parent / "mcp_token"
        monkeypatch.setenv("BTH_MCP_TOKEN_PATH", str(token_file))
        monkeypatch.setattr(mcp_auth, "_stdio_transport", False)
        token = mcp_auth.get_or_create_token()

        res1 = asyncio.run(
            mcp_add_campaign_edge_tool(
                child_campaign_id="c-child",
                parent_campaign_id="c-parent-b",
                catalog_dir=str(tmp_catalog),
                token=token,
            )
        )
        assert res1["ok"] is True
        res2 = asyncio.run(
            mcp_add_campaign_edge_tool(
                child_campaign_id="c-child",
                parent_campaign_id="c-parent-a",
                catalog_dir=str(tmp_catalog),
                token=token,
            )
        )
        assert res2["ok"] is True

        parents = asyncio.run(
            mcp_get_campaign_parents_tool(campaign_id="c-child", catalog_dir=str(tmp_catalog))
        )
        assert parents["ok"] is True
        assert parents["parents"] == ["c-parent-a", "c-parent-b"]
        assert parents["count"] == 2

        # Cycle rejection returns structured campaign_error envelope.
        cycle = asyncio.run(
            mcp_add_campaign_edge_tool(
                child_campaign_id="c-parent-a",
                parent_campaign_id="c-child",
                catalog_dir=str(tmp_catalog),
                token=token,
            )
        )
        assert cycle["ok"] is False
        assert cycle["error_code"] == "campaign_error"

        # Run edges round-trip + cycle rejection.
        r1 = asyncio.run(
            mcp_add_run_edge_tool(
                child_run_id="r-child",
                parent_run_id="r-parent",
                catalog_dir=str(tmp_catalog),
                token=token,
            )
        )
        assert r1["ok"] is True
        r_parents = asyncio.run(
            mcp_get_run_parents_tool(run_id="r-child", catalog_dir=str(tmp_catalog))
        )
        assert r_parents["ok"] is True
        assert r_parents["parents"] == ["r-parent"]

        r_cycle = asyncio.run(
            mcp_add_run_edge_tool(
                child_run_id="r-parent",
                parent_run_id="r-child",
                catalog_dir=str(tmp_catalog),
                token=token,
            )
        )
        assert r_cycle["ok"] is False
        assert r_cycle["error_code"] == "campaign_error"

    def test_campaign_and_run_edges_mcp_auth_and_validation(self, warm_db, tmp_catalog: Path, monkeypatch):
        import asyncio

        from bathos import mcp_auth
        from bathos.mcp import mcp_add_campaign_edge_tool, mcp_get_campaign_parents_tool

        warm_db.close()
        token_file = tmp_catalog.parent / "mcp_token"
        monkeypatch.setenv("BTH_MCP_TOKEN_PATH", str(token_file))
        monkeypatch.setattr(mcp_auth, "_stdio_transport", False)
        token = mcp_auth.get_or_create_token()

        unauth = asyncio.run(
            mcp_add_campaign_edge_tool(
                child_campaign_id="c1",
                parent_campaign_id="c2",
                catalog_dir=str(tmp_catalog),
            )
        )
        assert unauth["ok"] is False
        assert unauth["error_code"] == "auth_error"

        missing_param = asyncio.run(
            mcp_add_campaign_edge_tool(
                child_campaign_id="",
                parent_campaign_id="c2",
                catalog_dir=str(tmp_catalog),
                token=token,
            )
        )
        assert missing_param["ok"] is False
        assert missing_param["error_code"] == "invalid_param"

        missing_get = asyncio.run(
            mcp_get_campaign_parents_tool(campaign_id="", catalog_dir=str(tmp_catalog))
        )
        assert missing_get["ok"] is False
        assert missing_get["error_code"] == "invalid_param"

    def test_campaign_and_run_edges_mcp_log_mode_roundtrip(
        self, tmp_catalog: Path, monkeypatch
    ):
        import asyncio

        from bathos import mcp_auth
        from bathos.mcp import (
            mcp_add_campaign_edge_tool,
            mcp_add_run_edge_tool,
            mcp_get_campaign_parents_tool,
            mcp_get_run_parents_tool,
        )
        from bathos.runlog.mode import cutover_marker_path
        from bathos.runlog.writer import reset_writers_for_test

        tmp_catalog.mkdir(parents=True, exist_ok=True)
        cutover_marker_path(tmp_catalog).write_text('{"at":"2026-01-01T00:00:00Z","segments":[]}')
        monkeypatch.setenv("BTH_LOG_MODE", "1")
        monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_catalog))
        token_file = tmp_catalog.parent / "mcp_token"
        monkeypatch.setenv("BTH_MCP_TOKEN_PATH", str(token_file))
        monkeypatch.setattr(mcp_auth, "_stdio_transport", False)
        token = mcp_auth.get_or_create_token()

        proj = tmp_catalog.parent
        assert asyncio.run(
            mcp_add_campaign_edge_tool(
                child_campaign_id="lc-child",
                parent_campaign_id="lc-parent",
                catalog_dir=str(tmp_catalog),
                project_root=str(proj),
                token=token,
            )
        )["ok"] is True
        assert asyncio.run(
            mcp_get_campaign_parents_tool(
                campaign_id="lc-child",
                catalog_dir=str(tmp_catalog),
                project_root=str(proj),
            )
        )["parents"] == ["lc-parent"]

        assert asyncio.run(
            mcp_add_run_edge_tool(
                child_run_id="lr-child",
                parent_run_id="lr-parent",
                catalog_dir=str(tmp_catalog),
                project_root=str(proj),
                token=token,
            )
        )["ok"] is True
        assert asyncio.run(
            mcp_get_run_parents_tool(
                run_id="lr-child",
                catalog_dir=str(tmp_catalog),
                project_root=str(proj),
            )
        )["parents"] == ["lr-parent"]
        reset_writers_for_test()
