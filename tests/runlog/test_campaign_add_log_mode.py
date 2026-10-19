"""`bth campaign add` after cut-over (debt #2841).

After cut-over the legacy `bathos.db` is frozen, so campaigns and runs
recorded since then exist only in the folded index. `campaign add` used to
look both up in `bathos.db` and report "Campaign not found" for every
post-cutover campaign.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from bathos.campaigns import CampaignError, add_run_to_campaign, create_campaign
from bathos.catalog import write_run
from bathos.mcp import campaign_add_tool
from bathos.runlog.emit import emit_event, run_event_data
from bathos.runlog.index import connect_read
from bathos.runlog.ingest import run_ingest
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import assign_project_id, register_main_root
from bathos.schema import Run

from .conftest import make_git_repo, write_bth_toml


@pytest.fixture
def log_mode_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    workspace = make_git_repo(tmp_path / "ws")
    write_bth_toml(workspace, slug="proj")
    assign_project_id(workspace)
    register_main_root(workspace)
    (workspace / ".gitignore").write_text("/.bth/log/\n")
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    cutover_marker_path(catalog_dir).write_text(
        json.dumps({"at": "2020-01-01T00:00:00Z", "bathos": "test", "attempt": "t", "segments": []})
    )
    # A frozen legacy db that predates every campaign below, as on a real
    # catalog after cut-over.
    con = duckdb.connect(str(catalog_dir / "bathos.db"))
    con.execute(
        "CREATE TABLE campaigns (id TEXT, mode TEXT, started_at TEXT, stopping_threshold DOUBLE)"
    )
    con.execute("CREATE TABLE runs (id TEXT, timestamp TIMESTAMP, outcome TEXT, sidecar_path TEXT)")
    con.close()
    monkeypatch.chdir(workspace)
    return workspace, catalog_dir


def _make_run(run_id: str, ts: datetime) -> Run:
    return Run(
        id=run_id,
        project_slug="proj",
        command=f"python {run_id}.py",
        argv=["python", f"{run_id}.py"],
        git_hash="deadbeef",
        git_branch="main",
        git_dirty=False,
        timestamp=ts,
        status="completed",
        agent_mode="manual",
    )


def _emit_run(workspace: Path, run_id: str, ts: datetime) -> None:
    run = _make_run(run_id, ts)
    emit_event(
        kind="run.started",
        entity=[run_id],
        data={**run_event_data(run), "argv": run.argv, "campaign_id": None},
        cwd=workspace,
        hard_fail=True,
    )


def _create(workspace: Path, catalog_dir: Path, mode: str) -> str:
    db = connect_read(catalog_dir)
    try:
        return create_campaign(
            db, f"c-{mode}", "proj", mode, catalog_dir=catalog_dir, cwd=workspace
        ).id
    finally:
        db.close()


def _members(catalog_dir: Path, campaign_id: str) -> list[str]:
    run_ingest(catalog_dir)
    db = connect_read(catalog_dir)
    try:
        rows = db.execute(
            "SELECT run_id FROM campaign_runs WHERE campaign_id = ? ORDER BY run_id", [campaign_id]
        ).fetchall()
    finally:
        db.close()
    return [r[0] for r in rows]


def test_campaign_add_tool_finds_post_cutover_campaign(log_mode_project) -> None:
    workspace, catalog_dir = log_mode_project
    campaign_id = _create(workspace, catalog_dir, "exploration")
    _emit_run(workspace, "run-1", datetime.now(UTC))

    result = campaign_add_tool(
        run_id="run-1", campaign_id=campaign_id, catalog_dir=str(catalog_dir)
    )

    assert result.get("ok") is True, result
    assert _members(catalog_dir, campaign_id) == ["run-1"]


def test_campaign_add_tool_resolves_a_prefix_against_the_index(log_mode_project) -> None:
    workspace, catalog_dir = log_mode_project
    campaign_id = _create(workspace, catalog_dir, "exploration")
    _emit_run(workspace, "run-1", datetime.now(UTC))

    result = campaign_add_tool(
        run_id="run-1", campaign_id=campaign_id[:8], catalog_dir=str(catalog_dir)
    )

    assert result.get("ok") is True, result
    assert _members(catalog_dir, campaign_id) == ["run-1"]


def test_campaign_add_tool_does_not_create_bathos_db(log_mode_project) -> None:
    """Cut-over renames `bathos.db` away; `campaign add` must not recreate it."""
    workspace, catalog_dir = log_mode_project
    (catalog_dir / "bathos.db").rename(catalog_dir / "bathos.db.frozen")
    campaign_id = _create(workspace, catalog_dir, "exploration")
    _emit_run(workspace, "run-1", datetime.now(UTC))

    result = campaign_add_tool(
        run_id="run-1", campaign_id=campaign_id, catalog_dir=str(catalog_dir)
    )

    assert result.get("ok") is True, result
    assert not (catalog_dir / "bathos.db").exists()


def test_unimported_legacy_fragment_names_import_legacy(log_mode_project) -> None:
    workspace, catalog_dir = log_mode_project
    campaign_id = _create(workspace, catalog_dir, "exploration")
    # A fragment pulled from a host that is not cut over: on disk, not in the log.
    write_run(_make_run("legacy-run", datetime.now(UTC)), catalog_dir)

    result = campaign_add_tool(
        run_id="legacy-run", campaign_id=campaign_id, catalog_dir=str(catalog_dir)
    )

    assert result.get("ok") is False
    assert "bth migrate --import-legacy" in result["error"]


def test_confirmation_ordering_holds_on_index_timestamps(log_mode_project) -> None:
    """The index stores `runs.timestamp` as VARCHAR; the check must still apply."""
    workspace, catalog_dir = log_mode_project
    _emit_run(workspace, "early-run", datetime.now(UTC) - timedelta(days=1))
    campaign_id = _create(workspace, catalog_dir, "confirmation")
    run_ingest(catalog_dir)

    with pytest.raises(CampaignError, match="predates campaign creation"):
        add_run_to_campaign(None, campaign_id, "early-run", catalog_dir=catalog_dir, cwd=workspace)
