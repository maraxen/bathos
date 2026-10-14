"""Regression tests for MCP/CLI parameter handling (debt batch 261014 W3)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import bathos.mcp as mcp_mod
from bathos.mcp import run_tool


@pytest.fixture
def captured_run_script(tmp_path, monkeypatch):
    """Patch run_script in mcp and capture its kwargs; isolate catalog."""
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    calls: list[dict] = []

    def fake_run_script(**kwargs):
        calls.append(kwargs)
        return 0

    with patch.object(mcp_mod, "run_script", side_effect=fake_run_script):
        yield calls


class TestRunToolTagAndOutputNormalisation:
    """#2203 (comma tags) + #2217 (JSON-list output_paths)."""

    def test_comma_separated_tags_are_split(self, captured_run_script):
        run_tool(script_path="x.py", tags=["a,b,c"])
        assert captured_run_script[0]["tags"] == ["a", "b", "c"]

    def test_tags_strip_and_drop_empties(self, captured_run_script):
        run_tool(script_path="x.py", tags=["a, b", ",", "c,,", "d"])
        assert captured_run_script[0]["tags"] == ["a", "b", "c", "d"]

    def test_json_list_output_paths_expanded(self, captured_run_script):
        run_tool(script_path="x.py", output_paths=['["a","b"]', "c"])
        assert captured_run_script[0]["output_paths"] == ["a", "b", "c"]

    def test_non_list_bracket_string_kept_literal(self, captured_run_script):
        run_tool(script_path="x.py", output_paths=["[not json", '["a", 1]', "plain"])
        assert captured_run_script[0]["output_paths"] == ["[not json", '["a", 1]', "plain"]


@pytest.fixture
def campaign_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    return tmp_path / "catalog"


class TestCampaignCreateTool:
    def test_sequential_mode_accepted(self, campaign_env):
        """#2080: library accepts 'sequential'; the MCP wrapper must too."""
        from bathos.mcp import campaign_create_tool

        result = campaign_create_tool(name="seq", mode="sequential", catalog_dir=str(campaign_env))
        assert "error" not in result, result
        assert result["mode"] == "sequential"

    def test_invalid_mode_still_rejected(self, campaign_env):
        from bathos.mcp import campaign_create_tool

        result = campaign_create_tool(name="bad", mode="bogus", catalog_dir=str(campaign_env))
        assert "error" in result
        assert "sequential" in result["error"]

    def test_parent_campaign_id_passed_through(self, campaign_env):
        """#2078: create_campaign supports parent_campaign_id; the tool must expose it."""
        from bathos.mcp import campaign_create_tool

        parent = campaign_create_tool(name="parent", catalog_dir=str(campaign_env))
        child = campaign_create_tool(
            name="child",
            catalog_dir=str(campaign_env),
            parent_campaign_id=parent["campaign_id"],
        )
        assert "error" not in child, child

        from bathos.campaigns import connect_catalog_db

        db = connect_catalog_db(campaign_env, read_only=True)
        try:
            row = db.execute(
                "SELECT parent_campaign_id FROM campaigns WHERE id=?", [child["campaign_id"]]
            ).fetchone()
        finally:
            db.close()
        assert row[0] == parent["campaign_id"]


class TestCampaignConcludeWorkspaceRoot:
    """#2193: claim resolution must honour an explicit workspace_root."""

    def _make(self, campaign_env):
        from bathos.mcp import campaign_create_tool

        return campaign_create_tool(name="c", catalog_dir=str(campaign_env))["campaign_id"]

    def test_workspace_root_threaded_to_conclude(self, campaign_env, tmp_path):
        from bathos.mcp import campaign_conclude_tool

        cid = self._make(campaign_env)
        ws = tmp_path / "elsewhere"
        ws.mkdir()
        with patch.object(mcp_mod, "conclude_campaign") as fake:
            result = campaign_conclude_tool(
                campaign_id=cid,
                outcome_label="success",
                catalog_dir=str(campaign_env),
                workspace_root=str(ws),
            )
        assert result.get("status") == "concluded", result
        assert fake.call_args.kwargs["workspace_root"] == ws.resolve()

    def test_workspace_root_defaults_to_none(self, campaign_env):
        from bathos.mcp import campaign_conclude_tool

        cid = self._make(campaign_env)
        with patch.object(mcp_mod, "conclude_campaign") as fake:
            campaign_conclude_tool(
                campaign_id=cid, outcome_label="success", catalog_dir=str(campaign_env)
            )
        assert fake.call_args.kwargs["workspace_root"] is None


def _write_runs(catalog, n: int, slug: str = "test") -> None:
    from bathos.catalog import write_run
    from bathos.schema import Run

    for i in range(n):
        write_run(
            Run(
                project_slug=slug,
                command=f"cmd {i}",
                argv=["python", f"s{i}.py"],
                git_hash=f"h{i}",
                git_branch="main",
                git_dirty=False,
                status="completed",
                exit_code=0,
                duration_s=1.0,
                hostname="test",
            ),
            catalog,
        )


class TestListRunsTruncationSignal:
    """#2206: list_runs must say when more rows exist than were returned."""

    def test_truncated_true_when_more_rows_exist(self, tmp_catalog):
        from bathos.mcp import list_runs_tool

        _write_runs(tmp_catalog, 5)
        out = list_runs_tool(catalog_dir=str(tmp_catalog), limit=3)
        assert out["count"] == 3
        assert out["limit"] == 3
        assert out["truncated"] is True

    def test_truncated_false_when_all_rows_fit(self, tmp_catalog):
        from bathos.mcp import list_runs_tool

        _write_runs(tmp_catalog, 3)
        for limit in (3, 10):
            out = list_runs_tool(catalog_dir=str(tmp_catalog), limit=limit)
            assert out["count"] == 3
            assert out["limit"] == limit
            assert out["truncated"] is False

    def test_truncated_with_filters(self, tmp_catalog):
        from bathos.mcp import list_runs_tool

        _write_runs(tmp_catalog, 5, slug="proj")
        out = list_runs_tool(catalog_dir=str(tmp_catalog), limit=2, project="proj")
        assert out["count"] == 2
        assert out["truncated"] is True
