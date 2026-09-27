"""Delivery step 4, wave d: the cluster root kinds (`remote-log`,
`remote-fallback`, `remote-mirror`) `bth sync --pull` populates under
`<main root>/.bth/log/remote/<remote>/{log,fallback,mirror}/`
(`bathos.sync.pull_cluster_log`), and the ingest side that discovers and
folds them (`bathos.runlog.ingest.discover_roots`).

Covers AC-6 (a SLURM array's runs recorded exactly once after two pulls,
including a segment copied mid-line by the first pull), the cluster variants
of AC-8 (a `run.started` that landed in the remote fallback is in the local
index after one pull + ingest) and AC-9 (deleting the local copy of a
cluster segment loses nothing because of the pulled mirror), idempotent
re-pull, and a job with no `BTH_PROJECT_ID` (`project_id: null`) still
folding to a queryable `runs.project_slug` (spec D7: "ingest maps it by
slug").

No real SSH/rsync/myxcel: only the local filesystem layout `pull_cluster_log`
would have produced is simulated directly, exactly as `bathos.sync.
pull_cluster_log`'s own unit tests (`tests/test_sync.py`) confirm it
constructs.
"""

from __future__ import annotations

import json
from pathlib import Path

from bathos.runlog.envelope import build_envelope
from bathos.runlog.index import connect_read
from bathos.runlog.ingest import discover_roots, remote_root_id, run_ingest
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import assign_project_id, read_project_id, register_main_root

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


def _run_started_data(**overrides) -> dict:
    data = {
        "project_slug": "repo",
        "command": "python x.py",
        "argv": ["python", "x.py"],
        "git_hash": "aaa",
        "git_branch": "main",
        "git_dirty": False,
        "campaign_id": None,
        "agent_mode": "manual",
    }
    data.update(overrides)
    return data


def _run_finished_data(run_id: str, **overrides) -> dict:
    data = {
        "id": run_id,
        "status": "completed",
        "exit_code": 0,
        "duration_s": 1.0,
        "output_paths": [],
        "outcome": "pass",
        "outcome_error_reason": "",
        "outcome_is_residual": False,
        "adversarial_check_status": "",
        "timestamp": "2026-01-01T00:00:00.000000Z",
    }
    data.update(overrides)
    return data


def _remote_dirs(repo: Path, remote: str) -> tuple[Path, Path, Path]:
    """The exact layout `bathos.sync.cluster_log_remote_dirs` produces."""
    base = repo / ".bth" / "log" / "remote" / remote
    return base / "log", base / "fallback", base / "mirror"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_roots_finds_remote_log_fallback_mirror(tmp_path: Path):
    repo, _pid = _setup_project(tmp_path)
    log_dir, fallback_dir, mirror_dir = _remote_dirs(repo, "engaging")
    for d in (log_dir, fallback_dir, mirror_dir):
        d.mkdir(parents=True)

    roots = discover_roots()
    kinds = {(kind, rid) for kind, rid, _path in roots}

    rid = remote_root_id(repo.resolve(), "engaging")
    assert ("remote-log", rid) in kinds
    assert ("remote-fallback", rid) in kinds
    assert ("remote-mirror", rid) in kinds


def test_discover_roots_skips_remote_without_populated_subdirs(tmp_path: Path):
    """A `.bth/log/remote/<remote>/` with no `log`/`fallback`/`mirror`
    subdirectory yet (e.g. right after `mkdir` but before any sub-pull
    succeeded) contributes no root kind for the missing subdirectories."""
    repo, _pid = _setup_project(tmp_path)
    (repo / ".bth" / "log" / "remote" / "engaging").mkdir(parents=True)

    roots = discover_roots()
    kinds = {kind for kind, _rid, _path in roots if kind.startswith("remote-")}
    assert kinds == set()


# ---------------------------------------------------------------------------
# AC-6: a SLURM array's runs recorded exactly once after two pulls,
# including a segment copied mid-line by the first pull.
# ---------------------------------------------------------------------------


def test_ac6_array_job_recorded_once_after_two_pulls_mid_line_copy(tmp_path: Path):
    repo, pid = _setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    log_dir, _fallback_dir, _mirror_dir = _remote_dirs(repo, "engaging")

    line1 = _envelope_line(
        kind="run.started",
        entity=["run-array-0"],
        data=_run_started_data(),
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="node01.111.1000",
        seq=1,
        ts="2026-01-01T00:00:00.000000Z",
        eid="e-array-0-started",
    )
    line2 = _envelope_line(
        kind="run.finished",
        entity=["run-array-0"],
        data=_run_finished_data("run-array-0"),
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="node01.111.1000",
        seq=2,
        ts="2026-01-01T00:01:00.000000Z",
        eid="e-array-0-finished",
    )
    full_bytes = (line1 + line2).encode()

    # First pull: only the first line, plus a torn (mid-line) second line --
    # simulating a pull that copied the segment while the writer was
    # mid-`write()` on the second line.
    torn_bytes = full_bytes[: len(line1.encode()) + len(line2.encode()) // 2]
    _write_segment(log_dir, "node01.111.1000.jsonl", torn_bytes)

    report1 = run_ingest(catalog_dir)
    assert report1.ran
    assert report1.new_events == 1  # only run.started ingested
    assert report1.quarantined == 0  # torn TAIL is never quarantined by ingest

    con = connect_read(catalog_dir)
    row = con.execute("SELECT status FROM runs WHERE id = ?", ["run-array-0"]).fetchone()
    assert row == ("running",)
    con.close()

    # Second pull: the full segment (myxcel/rsync completed the transfer).
    _write_segment(log_dir, "node01.111.1000.jsonl", full_bytes)
    report2 = run_ingest(catalog_dir)
    assert report2.new_events == 1  # only the finished line is new
    assert report2.quarantined == 0

    con = connect_read(catalog_dir)
    row = con.execute("SELECT status, outcome FROM runs WHERE id = ?", ["run-array-0"]).fetchone()
    assert row == ("completed", "pass")
    con.close()


def test_ac6_two_bth_runs_in_one_array_task_both_recorded(tmp_path: Path):
    """"a task running two `bth run`s" -- two distinct run entities in the
    SAME pulled segment file both end up recorded exactly once."""
    repo, pid = _setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    log_dir, _fallback_dir, _mirror_dir = _remote_dirs(repo, "engaging")

    lines = "".join(
        _envelope_line(
            kind=kind,
            entity=[run_id],
            data=data,
            main_root=repo,
            worktree_root=repo,
            project="repo",
            project_id=pid,
            writer="node02.222.2000",
            seq=seq,
            ts=ts,
            eid=eid,
        )
        for run_id, kind, data, seq, ts, eid in [
            (
                "run-a",
                "run.started",
                _run_started_data(),
                1,
                "2026-01-01T00:00:00.000000Z",
                "e-a-started",
            ),
            (
                "run-a",
                "run.finished",
                _run_finished_data("run-a"),
                2,
                "2026-01-01T00:01:00.000000Z",
                "e-a-finished",
            ),
            (
                "run-b",
                "run.started",
                _run_started_data(),
                3,
                "2026-01-01T00:02:00.000000Z",
                "e-b-started",
            ),
            (
                "run-b",
                "run.finished",
                _run_finished_data("run-b"),
                4,
                "2026-01-01T00:03:00.000000Z",
                "e-b-finished",
            ),
        ]
    )
    _write_segment(log_dir, "node02.222.2000.jsonl", lines.encode())

    report = run_ingest(catalog_dir)
    assert report.new_events == 4
    assert report.quarantined == 0

    con = connect_read(catalog_dir)
    rows = con.execute("SELECT id, status FROM runs ORDER BY id").fetchall()
    con.close()
    assert rows == [("run-a", "completed"), ("run-b", "completed")]


# ---------------------------------------------------------------------------
# AC-8 cluster variant: a run.started landing in the remote FALLBACK is in
# the local index after one pull + ingest.
# ---------------------------------------------------------------------------


def test_ac8_cluster_variant_fallback_run_started_reaches_index_after_pull(tmp_path: Path):
    repo, pid = _setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    _log_dir, fallback_dir, _mirror_dir = _remote_dirs(repo, "engaging")

    # The remote project log was unwritable when this job started (D6): its
    # writer fell back to the remote's OWN `~/.bth/log/fallback/<slug>/`,
    # which `pull_cluster_log` pulls into `.../remote/<remote>/fallback/`.
    line = _envelope_line(
        kind="run.started",
        entity=["run-fallback-1"],
        data=_run_started_data(),
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="node03.333.3000",
        seq=1,
        ts="2026-01-01T00:00:00.000000Z",
        eid="e-fallback-started",
    )
    _write_segment(fallback_dir, "node03.333.3000.jsonl", line.encode())

    report = run_ingest(catalog_dir)
    assert report.new_events == 1
    assert report.quarantined == 0

    con = connect_read(catalog_dir)
    row = con.execute("SELECT id, status FROM runs WHERE id = ?", ["run-fallback-1"]).fetchone()
    con.close()
    assert row == ("run-fallback-1", "running")


# ---------------------------------------------------------------------------
# AC-9 cluster variant: deleting an active segment mid-run loses no event,
# via the pulled remote mirror.
# ---------------------------------------------------------------------------


def test_ac9_cluster_variant_deleted_remote_segment_recovered_from_pulled_mirror(
    tmp_path: Path,
):
    repo, pid = _setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    log_dir, _fallback_dir, mirror_dir = _remote_dirs(repo, "engaging")

    line1 = _envelope_line(
        kind="run.started",
        entity=["run-mirror-1"],
        data=_run_started_data(),
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="node04.444.4000",
        seq=1,
        ts="2026-01-01T00:00:00.000000Z",
        eid="e-mirror-started",
    )
    line2 = _envelope_line(
        kind="run.finished",
        entity=["run-mirror-1"],
        data=_run_finished_data("run-mirror-1"),
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="node04.444.4000",
        seq=2,
        ts="2026-01-01T00:01:00.000000Z",
        eid="e-mirror-finished",
    )
    full_bytes = (line1 + line2).encode()

    # The remote-log copy never even arrived locally (the segment was
    # deleted on the compute node before the pull could copy it) -- only
    # the pulled mirror copy exists.
    _write_segment(mirror_dir, "node04.444.4000.jsonl", full_bytes)
    assert not (log_dir / "node04.444.4000.jsonl").exists()

    report = run_ingest(catalog_dir)
    assert report.new_events == 2
    assert report.quarantined == 0

    con = connect_read(catalog_dir)
    row = con.execute(
        "SELECT status, outcome FROM runs WHERE id = ?", ["run-mirror-1"]
    ).fetchone()
    con.close()
    assert row == ("completed", "pass")


# ---------------------------------------------------------------------------
# Idempotent re-pull: ingesting the same pulled segments twice (a second
# `bth sync --pull` that re-copies unchanged files) appends nothing new.
# ---------------------------------------------------------------------------


def test_idempotent_repull_appends_nothing_new(tmp_path: Path):
    repo, pid = _setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    log_dir, _fallback_dir, mirror_dir = _remote_dirs(repo, "engaging")

    line = _envelope_line(
        kind="run.started",
        entity=["run-idempotent-1"],
        data=_run_started_data(),
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="node05.555.5000",
        seq=1,
        ts="2026-01-01T00:00:00.000000Z",
        eid="e-idempotent-started",
    )
    raw = line.encode()
    _write_segment(log_dir, "node05.555.5000.jsonl", raw)
    _write_segment(mirror_dir, "node05.555.5000.jsonl", raw)  # eid dedup across both copies

    report1 = run_ingest(catalog_dir)
    assert report1.new_events == 1
    # The same eid appears in BOTH the remote-log and remote-mirror copies
    # already on this first ingest -- whichever root is folded second sees
    # it as already-known (D7: "neither is authoritative on divergence").
    assert report1.skipped_existing == 1

    # A second `bth sync --pull` re-copies the identical bytes into both
    # destinations (myxcel/rsync re-transfers unchanged files harmlessly).
    # Both files are already fully read past their watermark, so ingest
    # reads no new bytes at all from either -- the truly idempotent case.
    report2 = run_ingest(catalog_dir)
    assert report2.new_events == 0
    assert report2.skipped_existing == 0
    assert report2.quarantined == 0

    con = connect_read(catalog_dir)
    count = con.execute(
        "SELECT COUNT(*) FROM runs WHERE id = ?", ["run-idempotent-1"]
    ).fetchone()[0]
    con.close()
    assert count == 1


# ---------------------------------------------------------------------------
# A job with no BTH_PROJECT_ID writes project_id null; ingest still folds a
# queryable `runs.project_slug` (spec D7: "ingest maps it by slug") -- the
# `project_slug` comes from the run's own `data.project_slug`, which a
# compute node's `_bth_env.sh` (existing, pre-runlog mechanism) already
# populates independent of BTH_PROJECT_ID.
# ---------------------------------------------------------------------------


def test_null_project_id_run_still_folds_queryable_project_slug(tmp_path: Path):
    repo, _pid = _setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    log_dir, _fallback_dir, _mirror_dir = _remote_dirs(repo, "engaging")

    line1 = _envelope_line(
        kind="run.started",
        entity=["run-null-pid-1"],
        data=_run_started_data(project_slug="repo"),
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=None,  # no BTH_PROJECT_ID in the job environment
        writer="node06.666.6000",
        seq=1,
        ts="2026-01-01T00:00:00.000000Z",
        eid="e-null-pid-started",
    )
    line2 = _envelope_line(
        kind="run.finished",
        entity=["run-null-pid-1"],
        data=_run_finished_data("run-null-pid-1"),
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=None,
        writer="node06.666.6000",
        seq=2,
        ts="2026-01-01T00:01:00.000000Z",
        eid="e-null-pid-finished",
    )
    _write_segment(log_dir, "node06.666.6000.jsonl", (line1 + line2).encode())

    report = run_ingest(catalog_dir)
    assert report.new_events == 2
    assert report.quarantined == 0

    con = connect_read(catalog_dir)
    row = con.execute(
        "SELECT project_slug, status FROM runs WHERE id = ?", ["run-null-pid-1"]
    ).fetchone()
    events_row = con.execute(
        "SELECT project_id FROM events WHERE eid = ?", ["e-null-pid-started"]
    ).fetchone()
    con.close()
    assert row == ("repo", "completed")
    assert events_row == (None,)
