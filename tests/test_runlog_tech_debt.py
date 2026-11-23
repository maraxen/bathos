"""Regression and negative tests for tech_debt #3051, #3118, and #2819 in runlog."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import duckdb
import pytest

from bathos.repair import repair
from bathos.runlog.envelope import build_envelope
from bathos.runlog.index import connect_read, index_db_path
from bathos.runlog.ingest import (
    IngestLockTimeout,
    ingest_after_write,
    ingest_lock,
    reconcile_index_campaign_runs,
    run_ingest,
)
from bathos.runlog.migrate import _do_switch
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import assign_project_id, read_project_id, register_main_root
from bathos.verify import verify_runlog
from tests.runlog.conftest import make_git_repo, write_bth_toml


def _setup_log_repo(tmp_path: Path) -> tuple[Path, Path, str | None]:
    repo = make_git_repo(tmp_path / "repo")
    write_bth_toml(repo, slug="repo")
    assign_project_id(repo)
    pid = read_project_id(repo / ".bth.toml")
    register_main_root(repo)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    cutover_marker_path(catalog_dir).write_text(
        json.dumps({"at": "2026-01-01T00:00:00Z", "bathos": "test", "attempt": "1", "segments": []})
    )
    return repo, catalog_dir, pid


def _env_line(**kwargs) -> str:
    return json.dumps(build_envelope(**kwargs), sort_keys=True) + "\n"


def _write_seg(repo: Path, name: str, *lines: str) -> Path:
    seg = repo / ".bth" / "log" / name
    seg.parent.mkdir(parents=True, exist_ok=True)
    seg.write_text("".join(lines))
    return seg


def _common(repo: Path, pid: str | None) -> dict:
    return dict(main_root=repo, worktree_root=repo, project="repo", project_id=pid, writer="w1")


# ---------------------------------------------------------------------------
# tech_debt #3051: orphaned campaign_runs / 8-char prefix mismatches
# ---------------------------------------------------------------------------


def test_3051_ingest_resolves_8char_prefix_and_blocks_orphan_writes(tmp_path: Path):
    repo, catalog_dir, pid = _setup_log_repo(tmp_path)
    full_cid = "abcd1234-1111-2222-3333-444444444444"
    common = _common(repo, pid)

    lines = [
        _env_line(
            kind="campaign.created",
            entity=[full_cid],
            data={
                "id": full_cid,
                "project_slug": "repo",
                "name": "c",
                "mode": "exploration",
                "status": "open",
                "started_at": "2026-01-01T00:00:00Z",
            },
            seq=1,
            ts="2026-01-01T00:00:00Z",
            eid="e-camp",
            **common,
        ),
        _env_line(
            kind="run.started",
            entity=["run-prefix"],
            data={"project_slug": "repo", "campaign_id": "abcd1234", "timestamp": "2026-01-01T00:01:00Z"},
            seq=2,
            ts="2026-01-01T00:01:00Z",
            eid="e-r1",
            **common,
        ),
        _env_line(
            kind="run.started",
            entity=["run-orphan"],
            data={
                "project_slug": "repo",
                "campaign_id": "deadbeef-0000-0000-0000-000000000000",
                "timestamp": "2026-01-01T00:02:00Z",
            },
            seq=3,
            ts="2026-01-01T00:02:00Z",
            eid="e-r2",
            **common,
        ),
    ]
    _write_seg(repo, "seg1.jsonl", *lines)
    run_ingest(catalog_dir)

    con = connect_read(catalog_dir)
    try:
        cr_rows = con.execute("SELECT campaign_id, run_id FROM campaign_runs").fetchall()
        camp_rows = con.execute("SELECT id, mode FROM campaigns").fetchall()
        r_prefix = con.execute("SELECT campaign_id FROM runs WHERE id = 'run-prefix'").fetchone()
    finally:
        con.close()

    assert cr_rows == [(full_cid, "run-prefix")]
    assert camp_rows == [(full_cid, "exploration")]
    assert r_prefix == (full_cid,)
    assert verify_runlog(catalog_dir).ok is True


def test_3051_verify_and_repair_reconcile_legacy_corrupt_index_db(tmp_path: Path):
    repo, catalog_dir, pid = _setup_log_repo(tmp_path)
    full_cid = "abcd1234-1111-2222-3333-444444444444"
    common = _common(repo, pid)

    _write_seg(
        repo,
        "seg1.jsonl",
        _env_line(
            kind="campaign.created",
            entity=[full_cid],
            data={
                "id": full_cid,
                "project_slug": "repo",
                "name": "c",
                "mode": "exploration",
                "status": "open",
                "started_at": "2026-01-01T00:00:00Z",
            },
            seq=1,
            ts="2026-01-01T00:00:00Z",
            eid="e-camp",
            **common,
        ),
        _env_line(
            kind="run.started",
            entity=["run-1"],
            data={"project_slug": "repo", "campaign_id": "abcd1234", "timestamp": "2026-01-01T00:01:00Z"},
            seq=2,
            ts="2026-01-01T00:01:00Z",
            eid="e-r1",
            **common,
        ),
    )
    run_ingest(catalog_dir)

    # Plant historical pre-fix corruption directly into index.db:
    # 1) 8-char prefix campaign_runs row + phantom NULL-mode campaign row
    # 2) true orphan campaign_runs row referencing nonexistent campaign
    idx = index_db_path(catalog_dir)
    con = duckdb.connect(str(idx))
    con.execute("DELETE FROM campaign_runs")
    con.execute("INSERT INTO campaigns (id, mode) VALUES ('abcd1234', NULL)")
    con.execute("INSERT INTO campaign_runs VALUES ('abcd1234', 'run-1', NULL, NULL)")
    con.execute("INSERT INTO campaign_runs VALUES ('missing-9999', 'run-orphan', NULL, NULL)")
    con.execute("CHECKPOINT")
    con.close()

    res = verify_runlog(catalog_dir)
    assert res.ok is False
    types = {f["type"] for f in res.stats["findings"]}
    assert "campaign_run_prefix_mismatch" in types
    assert "orphaned_campaign_run" in types

    dry = repair(catalog_dir, tier="warm", dry_run=True)
    assert any(a.action == "reconcile_campaign_runs" for a in dry.actions)
    assert verify_runlog(catalog_dir).ok is False

    applied = repair(catalog_dir, tier="warm", dry_run=False)
    assert any(a.action == "reconcile_campaign_runs" and not a.dry_run for a in applied.actions)
    assert verify_runlog(catalog_dir).ok is True

    con = connect_read(catalog_dir)
    try:
        assert con.execute("SELECT campaign_id, run_id FROM campaign_runs").fetchall() == [
            (full_cid, "run-1")
        ]
        assert con.execute("SELECT COUNT(*) FROM campaigns WHERE mode IS NULL").fetchone()[0] == 0
    finally:
        con.close()


def test_3051_reconcile_index_campaign_runs_noop_when_missing(tmp_path: Path):
    counts = reconcile_index_campaign_runs(tmp_path / "no_catalog")
    assert counts == {"reassigned": 0, "deleted_orphans": 0, "deleted_phantom_campaigns": 0}


# ---------------------------------------------------------------------------
# tech_debt #3118: frozen sidecar in campaign.run_added + fold fallback
# ---------------------------------------------------------------------------


def test_3118_add_run_to_campaign_freezes_sidecar_in_log_mode_after_wt_deleted(tmp_path: Path):
    import shutil

    from bathos.campaigns import add_run_to_campaign, create_campaign
    from bathos.runlog.emit import emit_event, run_event_data
    from bathos.schema import Run

    repo, catalog_dir, _pid = _setup_log_repo(tmp_path)

    wt = tmp_path / "tmp_wt"
    wt.mkdir()
    sc_path = wt / "exp.bth.toml"
    sc_path.write_text(
        '[experiment]\nhypothesis = "h"\nstage_name = "exploration"\n'
        '[outcomes.pass]\ncondition = "x > 0"\ndecision = "go"\n'
        '[outcomes.fail]\ncondition = "x <= 0"\ndecision = "stop"\nis_residual = true\n'
        '[result_schema]\nx = "float"\n'
        "[popper]\nnull_pass_rate = 0.1\nalt_pass_rate = 0.9\nstopping_threshold = 0.05\n"
    )

    camp = create_campaign(None, "seq", "repo", "sequential", catalog_dir=catalog_dir, cwd=repo)
    run = Run(
        id="run-wt-1",
        project_slug="repo",
        command=f"python {wt}/exp.py",
        argv=["python", str(wt / "exp.py")],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        outcome="pass",
        sidecar_path=str(sc_path),
    )
    # Deliberately omit sidecar declaration from run.started (simulating pre-snapshot or imported run)
    # and snapshot via add_run_to_campaign before deleting the /tmp worktree, OR verify run_added carries it.
    emit_event(
        kind="run.started",
        entity=[run.id],
        data={**run_event_data(run), "campaign_id": None},
        cwd=repo,
        hard_fail=True,
    )
    emit_event(kind="run.finished", entity=[run.id], data=run_event_data(run), cwd=repo)
    run_ingest(catalog_dir)

    add_run_to_campaign(None, camp.id, run.id, catalog_dir=catalog_dir, cwd=repo)
    shutil.rmtree(wt)
    assert not sc_path.exists()

    # Rebuild index from logs from scratch to prove campaign.run_added frozen payload survives
    index_db_path(catalog_dir).unlink()
    run_ingest(catalog_dir)
    con = connect_read(catalog_dir)
    try:
        cr = con.execute(
            "SELECT evalue, seq_position FROM campaign_runs WHERE campaign_id = ? AND run_id = ?",
            [camp.id, run.id],
        ).fetchone()
    finally:
        con.close()
    assert cr == (9.0, 1)


# ---------------------------------------------------------------------------
# tech_debt #2819: ingest_lock timeout, non-silent skip warnings, migrate lock
# ---------------------------------------------------------------------------


def test_2819_ingest_lock_zero_timeout_and_after_write_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    _repo, catalog_dir, _pid = _setup_log_repo(tmp_path)
    with ingest_lock(catalog_dir):
        with (
            pytest.raises(IngestLockTimeout, match="still held after 0s"),
            ingest_lock(catalog_dir, timeout_s=0.0),
        ):
            pass
        with caplog.at_level(logging.WARNING, logger="bathos.runlog.ingest"):
            result = ingest_after_write(catalog_dir, lock_timeout_s=0.05)
    assert result is None
    assert any("post-write index refresh skipped" in rec.message for rec in caplog.records)


def test_2819_fold_roots_into_warns_on_shrunk_segment(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    repo, catalog_dir, pid = _setup_log_repo(tmp_path)
    common = _common(repo, pid)
    seg = _write_seg(
        repo,
        "seg1.jsonl",
        _env_line(
            kind="run.started",
            entity=["run-1"],
            data={"project_slug": "repo"},
            seq=1,
            ts="2026-01-01T00:00:00Z",
            eid="e-1",
            **common,
        ),
    )
    run_ingest(catalog_dir)
    seg.write_bytes(b"")  # shrink below watermark

    with caplog.at_level(logging.WARNING, logger="bathos.runlog.ingest"):
        run_ingest(catalog_dir)
    assert any("skipping shrunk segment" in rec.message for rec in caplog.records)


def test_2819_migrate_do_switch_holds_ingest_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import fcntl
    import os

    import bathos.runlog.migrate as migrate_mod
    from bathos.runlog.ingest import ingest_lock_path

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    observed_locked: list[bool] = []

    def _probe_step4a(cd: Path, _attempt: str) -> None:
        fd = os.open(ingest_lock_path(cd), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            observed_locked.append(False)
        except BlockingIOError:
            observed_locked.append(True)
        finally:
            os.close(fd)

    monkeypatch.setattr(migrate_mod, "_step4a_build_index", _probe_step4a)
    monkeypatch.setattr(migrate_mod, "_step4b_write_marker", lambda *_a: None)
    monkeypatch.setattr(migrate_mod, "_step4c_move_staged_segments", lambda *_a: None)
    monkeypatch.setattr(migrate_mod, "_step4d_rename_warm", lambda *_a: None)
    monkeypatch.setattr(migrate_mod, "_step4e_delete_staging", lambda *_a: None)

    _do_switch(catalog_dir, "attempt-1")
    assert observed_locked == [True]
