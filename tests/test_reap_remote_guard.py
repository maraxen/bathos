"""Tests for `bathos.reap` remote-host guard (spec 261125 §8.3, AC-10, NC-9)."""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bathos.catalog import read_runs, write_run
from bathos.index import connect_read
from bathos.reap import _same_host, find_stale_running_runs, reap_runs
from bathos.run_bundle import export_run_bundle, import_run_bundle
from bathos.runlog.envelope import build_envelope
from bathos.runlog.ingest import run_ingest
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import assign_project_id, read_project_id, register_main_root
from bathos.schema import Run
from tests.runlog.conftest import make_git_repo, write_bth_toml


def test_same_host_normalizes_fqdn_and_case():
    assert _same_host("titanix", "titanix.mit.edu") is True
    assert _same_host("TITANIX.MIT.EDU", "titanix") is True
    assert _same_host("titanix", "engaging") is False
    assert _same_host("", "titanix") is False
    assert _same_host(None, "titanix") is False


def test_nc9_remote_non_slurm_running_runs_protected_by_default(tmp_path: Path):
    cat = tmp_path / "cat"
    cat.mkdir()
    (cat / "runs").mkdir()

    local_host = socket.gethostname()
    old_ts = datetime.now(UTC) - timedelta(hours=48)

    r_remote_short = Run(
        id="run-remote-short",
        project_slug="proj",
        command="python a.py",
        argv=["python", "a.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        status="running",
        hostname="titanix",
        timestamp=old_ts,
    )
    r_remote_fqdn = Run(
        id="run-remote-fqdn",
        project_slug="proj",
        command="python b.py",
        argv=["python", "b.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        status="running",
        hostname="titanix.mit.edu",
        timestamp=old_ts,
    )
    r_local_fqdn = Run(
        id="run-local-fqdn",
        project_slug="proj",
        command="python c.py",
        argv=["python", "c.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        status="running",
        hostname=f"{local_host}.local",
        timestamp=old_ts,
    )
    for r in (r_remote_short, r_remote_fqdn, r_local_fqdn):
        write_run(r, cat)

    # Default reap_runs(apply=True) skips remote hosts and reaps local
    cands, skipped = reap_runs(cat, older_than_h=24, apply=True)
    assert [c.id for c in cands] == ["run-local-fqdn"]
    skip_map = {r.id: reason for r, reason in skipped}
    assert skip_map == {
        "run-remote-short": "remote_host_skipped",
        "run-remote-fqdn": "remote_host_skipped",
    }

    by_id = {r.id: r for r in read_runs(cat)}
    assert by_id["run-remote-short"].status == "running"
    assert by_id["run-remote-fqdn"].status == "running"
    assert by_id["run-local-fqdn"].status == "abandoned"

    # find_stale_running_runs matches default guard and include_remote=True opt-in
    assert find_stale_running_runs(cat, older_than_h=24) == []
    stale_remote = find_stale_running_runs(cat, older_than_h=24, include_remote=True)
    assert {r["run_id"] for r in stale_remote} == {"run-remote-short", "run-remote-fqdn"}

    # include_remote=True explicitly reaps remote runs
    cands_rem, _ = reap_runs(cat, older_than_h=24, apply=True, include_remote=True)
    assert {c.id for c in cands_rem} == {"run-remote-short", "run-remote-fqdn"}


def test_ac10_imported_running_run_guarded_even_if_hostname_matches_or_empty(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "home_src"))
    src_cat = tmp_path / "src_cat"
    src_cat.mkdir()
    cutover_marker_path(src_cat).write_text(
        json.dumps({"at": "2026-11-25T00:00:00Z", "bathos": "test", "attempt": "1", "segments": []})
    )
    src_repo = make_git_repo(tmp_path / "src_repo")
    write_bth_toml(src_repo, slug="proj")
    assign_project_id(src_repo)
    pid = read_project_id(src_repo / ".bth.toml")
    register_main_root(src_repo)

    old_ts = (datetime.now(UTC) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    ev = build_envelope(
        kind="run.started",
        entity=["imp-running-1"],
        data={
            "id": "imp-running-1",
            "project_slug": "proj",
            "command": "python run.py",
            "argv": ["python", "run.py"],
            "git_hash": "abc",
            "git_branch": "main",
            "git_dirty": False,
            "timestamp": old_ts,
            "hostname": "localhost",
        },
        main_root=src_repo,
        worktree_root=src_repo,
        project="proj",
        project_id=pid,
        writer="titanix.1.1",
        seq=1,
        ts=old_ts,
        eid="eid-imp-run-1",
    )
    seg = src_repo / ".bth" / "log" / "seg1.jsonl"
    seg.parent.mkdir(parents=True)
    seg.write_text(json.dumps(ev) + "\n", encoding="utf-8")
    run_ingest(src_cat)

    bundle = tmp_path / "imp.bthbundle"
    export_run_bundle("imp-running-1", bundle, catalog_dir=src_cat)

    monkeypatch.setenv("HOME", str(tmp_path / "home_dst"))
    dst_cat = tmp_path / "dst_cat"
    dst_cat.mkdir()
    cutover_marker_path(dst_cat).write_text(
        json.dumps({"at": "2026-11-25T00:00:00Z", "bathos": "test", "attempt": "1", "segments": []})
    )
    import_run_bundle(bundle, catalog_dir=dst_cat)

    cands, skipped = reap_runs(dst_cat, older_than_h=24, apply=True)
    assert cands == []
    assert any(r.id == "imp-running-1" and reason == "remote_host_skipped" for r, reason in skipped)

    con = connect_read(dst_cat, wire_validity=False)
    try:
        assert con.execute("SELECT status FROM runs WHERE id = 'imp-running-1'").fetchone() == (
            "running",
        )
    finally:
        con.close()


def test_imported_remote_slurm_run_skips_local_sacct(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home_src"))
    src_cat = tmp_path / "src_cat"
    src_cat.mkdir()
    cutover_marker_path(src_cat).write_text(
        json.dumps({"at": "2026-11-25T00:00:00Z", "bathos": "test", "attempt": "1", "segments": []})
    )
    src_repo = make_git_repo(tmp_path / "src_repo")
    write_bth_toml(src_repo, slug="proj")
    assign_project_id(src_repo)
    pid = read_project_id(src_repo / ".bth.toml")
    register_main_root(src_repo)

    old_ts = (datetime.now(UTC) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    ev = build_envelope(
        kind="run.started",
        entity=["imp-slurm-1"],
        data={
            "id": "imp-slurm-1",
            "project_slug": "proj",
            "command": "python run.py",
            "argv": ["python", "run.py"],
            "git_hash": "abc",
            "git_branch": "main",
            "git_dirty": False,
            "timestamp": old_ts,
            "hostname": "othercluster-login01",
            "slurm_job_id": "123456",
        },
        main_root=src_repo,
        worktree_root=src_repo,
        project="proj",
        project_id=pid,
        writer="othercluster.1.1",
        seq=1,
        ts=old_ts,
        eid="eid-imp-slurm-1",
    )
    seg = src_repo / ".bth" / "log" / "seg1.jsonl"
    seg.parent.mkdir(parents=True)
    seg.write_text(json.dumps(ev) + "\n", encoding="utf-8")
    run_ingest(src_cat)

    bundle = tmp_path / "imp_slurm.bthbundle"
    export_run_bundle("imp-slurm-1", bundle, catalog_dir=src_cat)

    monkeypatch.setenv("HOME", str(tmp_path / "home_dst"))
    dst_cat = tmp_path / "dst_cat"
    dst_cat.mkdir()
    cutover_marker_path(dst_cat).write_text(
        json.dumps({"at": "2026-11-25T00:00:00Z", "bathos": "test", "attempt": "1", "segments": []})
    )
    import_run_bundle(bundle, catalog_dir=dst_cat)

    def _boom_sacct(_job_id: str):
        raise AssertionError("sacct must not be queried for an imported remote SLURM run")

    monkeypatch.setattr("bathos.reap._query_slurm_job_details", _boom_sacct)
    monkeypatch.setattr("bathos.reap._query_slurm_job", _boom_sacct)

    cands, skipped = reap_runs(dst_cat, older_than_h=24, apply=True)
    assert cands == []
    assert any(r.id == "imp-slurm-1" and reason == "remote_host_skipped" for r, reason in skipped)

    con = connect_read(dst_cat, wire_validity=False)
    try:
        assert con.execute("SELECT status FROM runs WHERE id = 'imp-slurm-1'").fetchone() == (
            "running",
        )
    finally:
        con.close()
