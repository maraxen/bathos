"""Generation-swap ingest (spec "Index ingest (generation swap)"): the
disposable-index side of delivery step 3, wave a.

Covers: idempotent re-ingest, crash-safety (old generation stays readable),
torn-tail handling (mid-file vs. unterminated), eid dedup across the project
log and the mirror, and AC-19's wal-remains refusal.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from bathos.runlog.envelope import build_envelope
from bathos.runlog.index import connect_read
from bathos.runlog.ingest import (
    IngestWalRemainsError,
    _check_no_wal,
    discover_roots,
    run_ingest,
)
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import assign_project_id, read_project_id, register_main_root
from bathos.runlog.writer import mirror_dir_for

from .conftest import make_git_repo, write_bth_toml


def enable_log_mode(catalog_dir: Path) -> None:
    catalog_dir.mkdir(parents=True, exist_ok=True)
    cutover_marker_path(catalog_dir).write_text(
        json.dumps({"at": "2026-01-01T00:00:00Z", "bathos": "test", "attempt": "1", "segments": []})
    )


def _write_segment(log_dir: Path, name: str, raw_bytes: bytes) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / name
    path.write_bytes(raw_bytes)
    return path


def _envelope_line(**kwargs) -> str:
    env = build_envelope(**kwargs)
    return json.dumps(env, sort_keys=True) + "\n"


def _setup_project(tmp_path: Path, name: str = "repo") -> tuple[Path, str | None]:
    repo = make_git_repo(tmp_path / name)
    write_bth_toml(repo, slug=name)
    assign_project_id(repo)
    pid = read_project_id(repo / ".bth.toml")
    register_main_root(repo)
    return repo, pid


def test_discover_roots_finds_registered_project(tmp_path: Path):
    repo, _pid = _setup_project(tmp_path)
    roots = discover_roots()
    kinds = {(k, r) for k, r, _ in roots}
    assert ("project", str(repo.resolve())) in kinds


def test_ingest_basic_and_connect_read_end_to_end(tmp_path: Path):
    repo, pid = _setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    log_dir = repo / ".bth" / "log"
    line1 = _envelope_line(
        kind="run.started",
        entity=["run-1"],
        data={
            "project_slug": "repo",
            "command": "python x.py",
            "argv": ["python", "x.py"],
            "git_hash": "aaa",
            "git_branch": "main",
            "git_dirty": False,
            "campaign_id": None,
            "agent_mode": "manual",
        },
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="w1",
        seq=1,
        ts="2026-01-01T00:00:00.000000Z",
        eid="e-started",
    )
    line2 = _envelope_line(
        kind="run.finished",
        entity=["run-1"],
        data={
            "id": "run-1",
            "status": "completed",
            "exit_code": 0,
            "duration_s": 1.0,
            "output_paths": [],
            "outcome": "pass",
            "outcome_error_reason": "",
            "outcome_is_residual": False,
            "adversarial_check_status": "",
            "timestamp": "2026-01-01T00:00:00.000000Z",
        },
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="w1",
        seq=2,
        ts="2026-01-01T00:01:00.000000Z",
        eid="e-finished",
    )
    _write_segment(log_dir, "seg1.jsonl", (line1 + line2).encode())

    report = run_ingest(catalog_dir)
    assert report.ran
    assert report.new_events == 2
    assert report.quarantined == 0
    assert report.entities_refolded == 1

    con = connect_read(catalog_dir)
    row = con.execute(
        "SELECT status, outcome, project_slug FROM runs WHERE id = ?", ["run-1"]
    ).fetchone()
    assert row == ("completed", "pass", "repo")


def test_ingest_idempotent_on_unchanged_sources(tmp_path: Path):
    repo, pid = _setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    log_dir = repo / ".bth" / "log"
    line = _envelope_line(
        kind="run.started",
        entity=["run-1"],
        data={"project_slug": "repo"},
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="w1",
        seq=1,
        ts="2026-01-01T00:00:00.000000Z",
        eid="e-1",
    )
    _write_segment(log_dir, "seg1.jsonl", line.encode())

    first = run_ingest(catalog_dir)
    assert first.new_events == 1

    # No new bytes past the recorded watermark -- the segment is not even
    # re-read, so nothing is newly "skipped" either; the true no-op case.
    second = run_ingest(catalog_dir)
    assert second.new_events == 0
    assert second.skipped_existing == 0

    con = connect_read(catalog_dir)
    count = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 1


def test_ingest_crash_mid_swap_leaves_old_generation_readable(tmp_path: Path):
    repo, pid = _setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    log_dir = repo / ".bth" / "log"
    line = _envelope_line(
        kind="run.started",
        entity=["run-1"],
        data={"project_slug": "repo"},
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="w1",
        seq=1,
        ts="2026-01-01T00:00:00.000000Z",
        eid="e-1",
    )
    _write_segment(log_dir, "seg1.jsonl", line.encode())

    first = run_ingest(catalog_dir)
    assert first.new_events == 1

    # A second event, ingested via a run that "crashes" right before the
    # final os.replace -- the real index.db must be untouched by it.
    line2 = _envelope_line(
        kind="run.started",
        entity=["run-2"],
        data={"project_slug": "repo"},
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="w1",
        seq=2,
        ts="2026-01-01T00:02:00.000000Z",
        eid="e-2",
    )
    _write_segment(log_dir, "seg2.jsonl", line2.encode())

    import bathos.runlog.ingest as ingest_mod

    def _boom(*_a, **_kw):
        raise RuntimeError("simulated crash before swap")

    # Scoped to a private MonkeyPatch context, NOT the test's own `monkeypatch`
    # fixture instance -- that instance also backs the autouse `isolated_home`
    # fixture's HOME redirect, and a bare `monkeypatch.undo()` would revert
    # THAT too, silently pointing subsequent calls at the real ~/.bth (a
    # correctness bug that would also be a real filesystem safety issue).
    with pytest.MonkeyPatch.context() as crash_mp:
        crash_mp.setattr(ingest_mod.os, "replace", _boom)
        with pytest.raises(RuntimeError, match="simulated crash"):
            run_ingest(catalog_dir)

    # No .tmp files should survive a crash check either way; but the key
    # property is the REAL index.db: still exactly the pre-crash generation.
    con = connect_read(catalog_dir)
    count = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 1  # only e-1 -- e-2 never made it in

    # Re-run with the patch reverted: the crash must not have corrupted
    # anything -- a clean ingest afterward picks up the previously
    # uncommitted event.
    second = run_ingest(catalog_dir)
    assert second.new_events == 1
    con = connect_read(catalog_dir)
    count = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 2


def test_torn_line_in_middle_is_quarantined_good_lines_still_ingest(tmp_path: Path):
    repo, pid = _setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    log_dir = repo / ".bth" / "log"

    good1 = _envelope_line(
        kind="run.started",
        entity=["run-1"],
        data={"project_slug": "repo"},
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="w1",
        seq=1,
        ts="2026-01-01T00:00:00.000000Z",
        eid="e-1",
    )
    garbage = "{not valid json at all\n"
    good2 = _envelope_line(
        kind="run.started",
        entity=["run-2"],
        data={"project_slug": "repo"},
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="w1",
        seq=2,
        ts="2026-01-01T00:02:00.000000Z",
        eid="e-2",
    )
    _write_segment(log_dir, "seg1.jsonl", (good1 + garbage + good2).encode())

    report = run_ingest(catalog_dir)
    assert report.new_events == 2
    assert report.quarantined == 1

    con = connect_read(catalog_dir)
    ids = {r[0] for r in con.execute("SELECT id FROM runs").fetchall()}
    assert ids == {"run-1", "run-2"}
    q = con.execute("SELECT reason, raw_line FROM quarantine").fetchone()
    assert q[0] == "invalid_json"
    assert q[1].startswith("{not valid json")


def test_unterminated_tail_is_never_quarantined_and_becomes_readable_once_completed(
    tmp_path: Path,
):
    repo, pid = _setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    log_dir = repo / ".bth" / "log"

    good1 = _envelope_line(
        kind="run.started",
        entity=["run-1"],
        data={"project_slug": "repo"},
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="w1",
        seq=1,
        ts="2026-01-01T00:00:00.000000Z",
        eid="e-1",
    )
    # A second line with no trailing "\n" -- an in-flight write.
    tail_env = build_envelope(
        kind="run.started",
        entity=["run-2"],
        data={"project_slug": "repo"},
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="w1",
        seq=2,
        ts="2026-01-01T00:02:00.000000Z",
        eid="e-2",
    )
    tail_raw = json.dumps(tail_env, sort_keys=True)
    seg = _write_segment(log_dir, "seg1.jsonl", (good1 + tail_raw).encode())

    report = run_ingest(catalog_dir)
    assert report.new_events == 1
    assert report.quarantined == 0  # unterminated tail is never quarantined by ingest

    con = connect_read(catalog_dir)
    ids = {r[0] for r in con.execute("SELECT id FROM runs").fetchall()}
    assert ids == {"run-1"}

    # Complete the tail (append the trailing newline the "writer" hadn't
    # flushed yet) and re-ingest: the watermark parked before it, so it now
    # ingests cleanly, with no quarantine entry ever created for it.
    with open(seg, "ab") as f:
        f.write(b"\n")
    report2 = run_ingest(catalog_dir)
    assert report2.new_events == 1
    assert report2.quarantined == 0
    con = connect_read(catalog_dir)
    ids = {r[0] for r in con.execute("SELECT id FROM runs").fetchall()}
    assert ids == {"run-1", "run-2"}


def test_eid_dedup_across_project_log_and_mirror(tmp_path: Path):
    repo, pid = _setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    log_dir = repo / ".bth" / "log"
    mirror_dir = mirror_dir_for(pid, "repo")

    line = _envelope_line(
        kind="run.started",
        entity=["run-1"],
        data={"project_slug": "repo"},
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="w1",
        seq=1,
        ts="2026-01-01T00:00:00.000000Z",
        eid="e-1",
    )
    # Same bytes (same eid, kind, entity, data) written to both copies, as
    # D7's dual-write does in production.
    _write_segment(log_dir, "seg1.jsonl", line.encode())
    _write_segment(mirror_dir, "seg1.jsonl", line.encode())

    report = run_ingest(catalog_dir)
    assert report.new_events == 1
    assert report.skipped_existing == 1  # the mirror's copy of the same eid

    con = connect_read(catalog_dir)
    count = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 1


def test_eid_conflict_with_differing_data_is_quarantined(tmp_path: Path):
    repo, pid = _setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    log_dir = repo / ".bth" / "log"
    mirror_dir = mirror_dir_for(pid, "repo")

    line_a = _envelope_line(
        kind="run.started",
        entity=["run-1"],
        data={"project_slug": "repo"},
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="w1",
        seq=1,
        ts="2026-01-01T00:00:00.000000Z",
        eid="e-1",
    )
    # Same eid, but DIFFERENT data -- a genuine conflict, not a duplicate.
    line_b = _envelope_line(
        kind="run.started",
        entity=["run-1"],
        data={"project_slug": "DIFFERENT"},
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="w1",
        seq=1,
        ts="2026-01-01T00:00:00.000000Z",
        eid="e-1",
    )
    _write_segment(log_dir, "seg1.jsonl", line_a.encode())
    _write_segment(mirror_dir, "seg1.jsonl", line_b.encode())

    report = run_ingest(catalog_dir)
    assert report.new_events == 1
    assert report.quarantined == 1

    con = connect_read(catalog_dir)
    reason = con.execute("SELECT reason FROM quarantine").fetchone()[0]
    assert reason == "eid_conflict"


def test_ac19_check_no_wal_refuses_swap(tmp_path: Path):
    tmp_db = tmp_path / "index.db.abc.tmp"
    tmp_db.write_bytes(b"fake")
    wal = tmp_db.with_name(tmp_db.name + ".wal")
    wal.write_bytes(b"fake-wal")

    with pytest.raises(IngestWalRemainsError):
        _check_no_wal(tmp_db)
    # Refusing the swap also cleans up the staged files.
    assert not tmp_db.exists()
    assert not wal.exists()


def test_ac19_check_no_wal_passes_when_absent(tmp_path: Path):
    tmp_db = tmp_path / "index.db.abc.tmp"
    tmp_db.write_bytes(b"fake")
    _check_no_wal(tmp_db)  # must not raise
    assert tmp_db.exists()


def test_ingest_skipped_under_slurm(tmp_path: Path, monkeypatch):
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    monkeypatch.setenv("SLURM_JOB_ID", "12345")
    report = run_ingest(catalog_dir)
    assert not report.ran
    assert not (catalog_dir / "index.db").exists()


def test_ingest_skipped_when_flag_off(tmp_path: Path):
    catalog_dir = tmp_path / "catalog"  # no cutover marker written
    report = run_ingest(catalog_dir)
    assert not report.ran
    assert not (catalog_dir / "index.db").exists()


def test_connect_read_flag_off_is_legacy_pass_through(tmp_path: Path):
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir(parents=True)
    con = connect_read(catalog_dir)
    assert isinstance(con, duckdb.DuckDBPyConnection)
    # No index.db views exist in the pass-through -- it never even runs the
    # index schema.
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    assert "runs" not in tables
