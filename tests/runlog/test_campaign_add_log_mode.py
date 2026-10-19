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

from bathos.campaigns import (
    Campaign,
    CampaignError,
    add_run_to_campaign,
    create_campaign,
    write_campaign_cool,
)
from bathos.catalog import write_run
from bathos.mcp import campaign_add_tool
from bathos.runlog.emit import emit_event, run_event_data, sidecar_declaration_for_event
from bathos.runlog.index import connect_read
from bathos.runlog.ingest import run_ingest
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import assign_project_id, register_main_root
from bathos.schema import Run
from bathos.sidecar import parse_sidecar

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


def test_tmp_fragment_or_id_prefix_gets_no_import_hint(log_mode_project) -> None:
    """Only an exact `run_<id>.parquet` fragment means "not imported yet"."""
    workspace, catalog_dir = log_mode_project
    campaign_id = _create(workspace, catalog_dir, "exploration")
    write_run(_make_run("full-run-id", datetime.now(UTC)), catalog_dir)
    tmp_run = _make_run("tmp-run", datetime.now(UTC))
    write_run(tmp_run, catalog_dir)
    frag = catalog_dir / "runs" / "proj" / "run_tmp-run.parquet"
    frag.rename(frag.with_name("run_tmp-run.tmp.parquet"))

    for run_id in ("full-run", "tmp-run"):
        result = campaign_add_tool(
            run_id=run_id, campaign_id=campaign_id, catalog_dir=str(catalog_dir)
        )
        assert result.get("ok") is False
        assert result["error"] == f"Run not found: {run_id}"


def test_cool_only_campaign_names_import_legacy(log_mode_project) -> None:
    workspace, catalog_dir = log_mode_project
    _emit_run(workspace, "run-1", datetime.now(UTC))
    cool = Campaign(
        id="99999999-9999-9999-9999-999999999999",
        project_slug="proj",
        name="synced",
        mode="exploration",
        started_at=datetime.now(UTC).isoformat(),
    )
    write_campaign_cool(cool, catalog_dir)

    result = campaign_add_tool(run_id="run-1", campaign_id=cool.id, catalog_dir=str(catalog_dir))

    assert result.get("ok") is False
    assert "bth migrate --import-legacy" in result["error"]


def test_flag_off_without_bathos_db_is_a_clean_error(tmp_path: Path) -> None:
    catalog_dir = tmp_path / "legacy_catalog"
    catalog_dir.mkdir()

    result = campaign_add_tool(run_id="r", campaign_id="c", catalog_dir=str(catalog_dir))

    assert result.get("ok") is False
    assert "error" in result


# Debt #2843: the rest of the campaign write path after cut-over.


def _write_popper_sidecar(directory: Path, threshold: float) -> Path:
    p = directory / f"seq_{str(threshold).replace('.', '_')}.bth.toml"
    p.write_text(
        "[experiment]\n"
        'hypothesis = "h"\n'
        "[outcomes.pass]\n"
        'condition = "x > 0"\n'
        'decision = "proceed"\n'
        "[outcomes.fail]\n"
        'condition = "x <= 0"\n'
        'decision = "debug"\n'
        "[result_schema]\n"
        'x = "float"\n'
        "[popper]\n"
        "null_pass_rate = 0.1\n"
        "alt_pass_rate = 0.9\n"
        f"stopping_threshold = {threshold}\n"
    )
    return p


def _emit_finished_run(workspace: Path, run_id: str, sidecar: Path) -> None:
    run = _make_run(run_id, datetime.now(UTC))
    run.exit_code = 0
    run.outcome = "pass"
    run.sidecar_path = str(sidecar)
    data = run_event_data(run)
    # As `bth run` does: the declaration is frozen into run.started (BC-3).
    started = {**data, "sidecar": sidecar_declaration_for_event(parse_sidecar(sidecar))}
    emit_event(kind="run.started", entity=[run_id], data=started, cwd=workspace, hard_fail=True)
    emit_event(kind="run.finished", entity=[run_id], data=data, cwd=workspace)


def test_create_and_conclude_work_after_bathos_db_is_renamed(log_mode_project) -> None:
    """Cut-over renames `bathos.db`; create/conclude must not need it (#2843 item 7)."""
    from bathos.mcp import campaign_conclude_tool, campaign_create_tool

    _workspace, catalog_dir = log_mode_project
    (catalog_dir / "bathos.db").rename(catalog_dir / "bathos.db.frozen")

    created = campaign_create_tool(
        name="after-rename", mode="exploration", project_slug="proj", catalog_dir=str(catalog_dir)
    )
    assert "error" not in created, created
    concluded = campaign_conclude_tool(
        campaign_id=created["campaign_id"], outcome_label="pass", catalog_dir=str(catalog_dir)
    )

    assert concluded.get("status") == "concluded", concluded
    assert not (catalog_dir / "bathos.db").exists()


def test_library_add_folds_pending_events_first(log_mode_project) -> None:
    """A run whose events are not yet in the index is still found (#2843 item 8)."""
    workspace, catalog_dir = log_mode_project
    campaign_id = _create(workspace, catalog_dir, "exploration")
    _emit_run(workspace, "run-1", datetime.now(UTC))  # no ingest after this

    add_run_to_campaign(None, campaign_id, "run-1", catalog_dir=catalog_dir, cwd=workspace)

    assert _members(catalog_dir, campaign_id) == ["run-1"]


def test_sequential_threshold_lock_holds_when_the_post_write_ingest_fails(
    log_mode_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Back-to-back adds must see each other's events (#2843 item 2).

    With the post-write ingest failing, the second add used to read an index
    with no threshold yet and emit a conflicting `campaign.threshold_set`.
    """
    workspace, catalog_dir = log_mode_project
    campaign_id = _create(workspace, catalog_dir, "sequential")
    _emit_finished_run(workspace, "run-a", _write_popper_sidecar(workspace, 0.05))
    _emit_finished_run(workspace, "run-b", _write_popper_sidecar(workspace, 0.1))
    run_ingest(catalog_dir)
    monkeypatch.setattr("bathos.runlog.emit._refresh_index_after_unit", lambda _cd: None)

    add_run_to_campaign(None, campaign_id, "run-a", catalog_dir=catalog_dir, cwd=workspace)
    with pytest.raises(CampaignError, match="Cannot change stopping_threshold"):
        add_run_to_campaign(None, campaign_id, "run-b", catalog_dir=catalog_dir, cwd=workspace)


def test_campaign_add_lock_is_exclusive(tmp_path: Path) -> None:
    import fcntl
    import os

    from bathos.campaigns import campaign_add_lock, campaign_add_lock_path

    with campaign_add_lock(tmp_path):
        fd = os.open(campaign_add_lock_path(tmp_path), os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


def test_prepare_for_conclude_uses_the_unit_of_work_mode(
    log_mode_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag is read once per call tree, not again by prepare (#2843 item 9)."""
    from bathos.campaigns import prepare_catalog_for_conclude
    from bathos.runlog.emit import unit_of_work

    _workspace, catalog_dir = log_mode_project

    def _legacy_ingest(*_a, **_k):
        raise AssertionError("prepare took the legacy path inside a log-mode unit of work")

    monkeypatch.setattr("bathos.campaigns.ingest_cool_campaigns", _legacy_ingest)
    with unit_of_work(catalog_dir) as mode:
        assert mode is True
        # If prepare re-read the flag it would now see legacy mode.
        cutover_marker_path(catalog_dir).unlink()
        prepare_catalog_for_conclude(catalog_dir)
