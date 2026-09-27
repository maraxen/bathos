"""AC-24: `bth verify`'s project-local-run-log checks (`bathos.verify.
verify_runlog`, delivery step 4).

One fixture per finding AC-24 lists, plus a clean fixture asserting zero
findings. Each fixture is built from hand-written envelope lines (the same
idiom `test_ingest.py`/`test_ingest_wave_b.py` use) or, for the legacy-source
checks, real legacy artifacts (`bathos.catalog.write_run`, a garbage
`.parquet` file, a cross-process `bathos.db` lock) written directly under
`catalog_dir` -- never through `verify_cool`/`verify_warm`/`verify_archive`,
which cover a different, non-overlapping set of checks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from bathos.catalog import write_run
from bathos.runlog.envelope import build_envelope
from bathos.runlog.ingest import run_ingest
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import assign_project_id, read_project_id, register_main_root
from bathos.runlog.writer import mirror_dir_for
from bathos.schema import Run
from bathos.verify import verify_runlog

from .conftest import make_git_repo, write_bth_toml


def enable_log_mode(catalog_dir: Path) -> None:
    catalog_dir.mkdir(parents=True, exist_ok=True)
    cutover_marker_path(catalog_dir).write_text(
        json.dumps({"at": "2026-01-01T00:00:00Z", "bathos": "test", "attempt": "1", "segments": []})
    )


def setup_project(tmp_path: Path, name: str = "repo") -> tuple[Path, str | None]:
    repo = make_git_repo(tmp_path / name)
    write_bth_toml(repo, slug=name)
    assign_project_id(repo)
    pid = read_project_id(repo / ".bth.toml")
    register_main_root(repo)
    return repo, pid


def _write_segment(log_dir: Path, name: str, raw_bytes: bytes) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / name
    path.write_bytes(raw_bytes)
    return path


def _envelope_line(**kwargs) -> str:
    env = build_envelope(**kwargs)
    return json.dumps(env, sort_keys=True) + "\n"


def _write_lines(repo: Path, *lines: str, seg: str = "seg1.jsonl") -> None:
    log_dir = repo / ".bth" / "log"
    _write_segment(log_dir, seg, "".join(lines).encode())


def _sidecar_decl(null_rate=0.1, alt_rate=0.9, threshold=0.05) -> dict:
    return {
        "kind": "experiment",
        "result_schema": {},
        "outcomes": {"pass": {"condition": "true", "is_residual": False}},
        "popper_null_pass_rate": null_rate,
        "popper_alt_pass_rate": alt_rate,
        "popper_stopping_threshold": threshold,
        "popper_weights": {},
    }


def _campaign_created_data(campaign_id: str, mode: str = "sequential", **extra) -> dict:
    data = {
        "id": campaign_id,
        "project_slug": "repo",
        "name": "c",
        "mode": mode,
        "status": "open",
        "started_at": "2026-01-01T00:00:00Z",
    }
    data.update(extra)
    return data


def _run_started_data(campaign_id: str | None, ts: str, **extra) -> dict:
    data = {
        "project_slug": "repo",
        "command": "python x.py",
        "argv": [],
        "git_hash": "a",
        "git_branch": "main",
        "git_dirty": False,
        "timestamp": ts,
        "campaign_id": campaign_id,
        "agent_mode": "manual",
    }
    data.update(extra)
    return data


def _run_finished_data(
    run_id: str, ts: str, duration_s: float = 1.0, outcome: str = "pass", **extra
) -> dict:
    data = {
        "id": run_id,
        "status": "completed",
        "exit_code": 0,
        "duration_s": duration_s,
        "output_paths": [],
        "outcome": outcome,
        "outcome_error_reason": "",
        "outcome_is_residual": False,
        "adversarial_check_status": "",
        "timestamp": ts,
    }
    data.update(extra)
    return data


# --------------------------------------------------------------------------
# threshold_mismatch (BC-12)
# --------------------------------------------------------------------------


def test_threshold_mismatch_finding(tmp_path: Path):
    repo, pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    common = dict(main_root=repo, worktree_root=repo, project="repo", project_id=pid, writer="w1")

    lines = [
        _envelope_line(
            kind="campaign.created",
            entity=["camp-mismatch"],
            data=_campaign_created_data("camp-mismatch", mode="sequential"),
            seq=1,
            ts="2026-01-01T00:00:00.000000Z",
            eid="e-created",
            **common,
        ),
        _envelope_line(
            kind="run.started",
            entity=["run-a"],
            data=_run_started_data(
                "camp-mismatch",
                "2026-01-01T00:01:00.000000Z",
                sidecar=_sidecar_decl(threshold=0.05),
            ),
            seq=2,
            ts="2026-01-01T00:01:00.000000Z",
            eid="e-a-start",
            **common,
        ),
        _envelope_line(
            kind="run.finished",
            entity=["run-a"],
            data=_run_finished_data("run-a", "2026-01-01T00:01:00.000000Z"),
            seq=3,
            ts="2026-01-01T00:02:00.000000Z",
            eid="e-a-finish",
            **common,
        ),
        _envelope_line(
            kind="run.started",
            entity=["run-b"],
            data=_run_started_data(
                "camp-mismatch",
                "2026-01-01T00:03:00.000000Z",
                sidecar=_sidecar_decl(threshold=0.10),
            ),
            seq=4,
            ts="2026-01-01T00:03:00.000000Z",
            eid="e-b-start",
            **common,
        ),
        _envelope_line(
            kind="run.finished",
            entity=["run-b"],
            data=_run_finished_data("run-b", "2026-01-01T00:03:00.000000Z"),
            seq=5,
            ts="2026-01-01T00:04:00.000000Z",
            eid="e-b-finish",
            **common,
        ),
    ]
    _write_lines(repo, *lines)
    run_ingest(catalog_dir)

    result = verify_runlog(catalog_dir)
    assert result.ok is False
    findings = [f for f in result.stats["findings"] if f["type"] == "threshold_mismatch"]
    assert len(findings) == 1
    assert findings[0]["campaign_id"] == "camp-mismatch"


# --------------------------------------------------------------------------
# evalue_changed_after_conclusion
# --------------------------------------------------------------------------


def test_evalue_changed_after_conclusion_finding(tmp_path: Path):
    repo, pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    common = dict(main_root=repo, worktree_root=repo, project="repo", project_id=pid, writer="w1")

    start_ts = "2026-01-01T00:01:00.000000Z"
    lines = [
        _envelope_line(
            kind="campaign.created",
            entity=["camp-concl"],
            data=_campaign_created_data("camp-concl", mode="exploration"),
            seq=1,
            ts="2026-01-01T00:00:00.000000Z",
            eid="e-created",
            **common,
        ),
        _envelope_line(
            kind="run.started",
            entity=["run-late"],
            data=_run_started_data("camp-concl", start_ts),
            seq=2,
            ts=start_ts,
            eid="e-start",
            **common,
        ),
        _envelope_line(
            kind="campaign.concluded",
            entity=["camp-concl"],
            data={
                "concluded_at": "2026-01-01T00:02:00.000000Z",
                "outcome_label": "pass",
                "conclusion": "done",
            },
            seq=3,
            ts="2026-01-01T00:02:00.000000Z",
            eid="e-concluded",
            **common,
        ),
        _envelope_line(
            kind="run.finished",
            # Ends ~1 year after start -- and so long after the conclusion
            # above -- via duration_s, not a later envelope ts.
            entity=["run-late"],
            data=_run_finished_data("run-late", start_ts, duration_s=31_536_000.0),
            seq=4,
            ts="2026-01-01T00:03:00.000000Z",
            eid="e-finish",
            **common,
        ),
    ]
    _write_lines(repo, *lines)
    run_ingest(catalog_dir)

    result = verify_runlog(catalog_dir)
    assert result.ok is False
    findings = [
        f for f in result.stats["findings"] if f["type"] == "evalue_changed_after_conclusion"
    ]
    assert len(findings) == 1
    assert findings[0]["campaign_id"] == "camp-concl"
    assert findings[0]["run_id"] == "run-late"


# --------------------------------------------------------------------------
# duplicate_project_id (D7)
# --------------------------------------------------------------------------


def test_duplicate_project_id_finding(tmp_path: Path):
    shared_id = "11111111-1111-1111-1111-111111111111"
    root_a = make_git_repo(tmp_path / "root_a")
    root_b = make_git_repo(tmp_path / "root_b")
    write_bth_toml(root_a, slug="a", project_id=shared_id)
    write_bth_toml(root_b, slug="b", project_id=shared_id)
    register_main_root(root_a)
    register_main_root(root_b)

    catalog_dir = tmp_path / "catalog"
    result = verify_runlog(catalog_dir)

    findings = [f for f in result.stats["findings"] if f["type"] == "duplicate_project_id"]
    assert len(findings) == 1
    assert findings[0]["project_id"] == shared_id
    assert set(findings[0]["roots"]) == {str(root_a.resolve()), str(root_b.resolve())}


# --------------------------------------------------------------------------
# log_watermark_shrunk / line_missing_from_both_copies
# --------------------------------------------------------------------------


def _ingested_project_and_mirror_segment(tmp_path: Path) -> tuple[Path, Path, Path]:
    """One ingested `run.started` line, written identically to the project
    log and the mirror (D7's normal dual write); returns
    (catalog_dir, project_seg_path, mirror_seg_path)."""
    repo, pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    line = _envelope_line(
        kind="run.started",
        entity=["run-wm"],
        data=_run_started_data(None, "2026-01-01T00:01:00.000000Z"),
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="w1",
        seq=1,
        ts="2026-01-01T00:01:00.000000Z",
        eid="e-wm",
    )
    log_dir = repo / ".bth" / "log"
    mirror_dir = mirror_dir_for(pid, "repo")
    project_seg = _write_segment(log_dir, "seg1.jsonl", line.encode())
    mirror_seg = _write_segment(mirror_dir, "seg1.jsonl", line.encode())

    report = run_ingest(catalog_dir)
    assert report.new_events == 1
    return catalog_dir, project_seg, mirror_seg


def test_log_watermark_shrunk_finding(tmp_path: Path):
    catalog_dir, project_seg, _mirror_seg = _ingested_project_and_mirror_segment(tmp_path)

    # Simulate data loss on the project copy only (e.g. `git clean -fdX`,
    # AC-14) -- the mirror still has the full bytes, so this is recoverable
    # and must NOT also be reported as lost from both copies.
    project_seg.write_bytes(b"")

    result = verify_runlog(catalog_dir)
    shrunk = [f for f in result.stats["findings"] if f["type"] == "log_watermark_shrunk"]
    assert any(f["root_kind"] == "project" for f in shrunk)
    assert not any(f["type"] == "line_missing_from_both_copies" for f in result.stats["findings"])


def test_line_missing_from_both_copies_finding(tmp_path: Path):
    catalog_dir, project_seg, mirror_seg = _ingested_project_and_mirror_segment(tmp_path)

    # Both copies now fall short of the recorded watermark -- genuinely lost.
    project_seg.write_bytes(b"")
    mirror_seg.write_bytes(b"")

    result = verify_runlog(catalog_dir)
    findings = [f for f in result.stats["findings"] if f["type"] == "line_missing_from_both_copies"]
    assert len(findings) == 1
    assert findings[0]["path"] == "seg1.jsonl"


# --------------------------------------------------------------------------
# eid_conflict / quarantined_line
# --------------------------------------------------------------------------


def test_eid_conflict_finding(tmp_path: Path):
    repo, pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    log_dir = repo / ".bth" / "log"
    mirror_dir = mirror_dir_for(pid, "repo")

    common = dict(
        entity=["run-1"],
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="w1",
        seq=1,
        ts="2026-01-01T00:00:00.000000Z",
        eid="e-1",
    )
    line_a = _envelope_line(kind="run.started", data={"project_slug": "repo"}, **common)
    line_b = _envelope_line(kind="run.started", data={"project_slug": "DIFFERENT"}, **common)
    _write_segment(log_dir, "seg1.jsonl", line_a.encode())
    _write_segment(mirror_dir, "seg1.jsonl", line_b.encode())

    run_ingest(catalog_dir)

    result = verify_runlog(catalog_dir)
    findings = [f for f in result.stats["findings"] if f["type"] == "eid_conflict"]
    assert len(findings) == 1
    assert findings[0]["eid"] == "e-1"


def test_quarantined_line_finding(tmp_path: Path):
    repo, pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    log_dir = repo / ".bth" / "log"

    good = _envelope_line(
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
    _write_segment(log_dir, "seg1.jsonl", (good + garbage).encode())

    run_ingest(catalog_dir)

    result = verify_runlog(catalog_dir)
    findings = [f for f in result.stats["findings"] if f["type"] == "quarantined_line"]
    assert len(findings) == 1
    assert findings[0]["reason"] == "invalid_json"


# --------------------------------------------------------------------------
# legacy_write_after_cutover / corrupt_legacy_source / legacy_db_locked
# --------------------------------------------------------------------------


def test_legacy_write_after_cutover_finding(tmp_path: Path):
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    run = Run(
        id="run-stale-write",
        project_slug="repo",
        command="test",
        argv=["test"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        status="completed",
        hostname="stale-host",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    # An older bathos install, still writing cool fragments after this
    # catalog's cut-over -- never imported, so its chain is unknown.
    write_run(run, catalog_dir)

    result = verify_runlog(catalog_dir)
    findings = [f for f in result.stats["findings"] if f["type"] == "legacy_write_after_cutover"]
    assert len(findings) == 1
    assert findings[0]["writing_host"] == "stale-host"


def test_corrupt_legacy_source_finding(tmp_path: Path):
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    runs_dir = catalog_dir / "runs" / "proj"
    runs_dir.mkdir(parents=True)
    (runs_dir / "run_corrupt.parquet").write_bytes(b"not a parquet file")

    result = verify_runlog(catalog_dir)
    findings = [f for f in result.stats["findings"] if f["type"] == "corrupt_legacy_source"]
    assert len(findings) == 1
    assert findings[0]["source_locator"] == "runs/proj/run_corrupt.parquet"


def test_legacy_db_locked_finding(tmp_path: Path):
    """A real cross-process lock (DuckDB's in-process connection reuse
    behaves differently -- see `test_index_connect_legacy.py`)."""
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    db_path = catalog_dir / "bathos.db"

    holder_code = f"""
import duckdb, time
con = duckdb.connect({str(db_path)!r})
con.execute("CREATE TABLE runs (id VARCHAR)")
time.sleep(10)
"""
    proc = subprocess.Popen([sys.executable, "-c", holder_code])
    try:
        deadline = time.monotonic() + 5
        findings: list[dict] = []
        while time.monotonic() < deadline:
            if db_path.exists():
                result = verify_runlog(catalog_dir)
                findings = [f for f in result.stats["findings"] if f["type"] == "legacy_db_locked"]
                if findings:
                    break
            time.sleep(0.1)
        assert findings, "never observed the lock"
        assert findings[0]["path"] == str(db_path)
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# --------------------------------------------------------------------------
# unterminated_tail (AC-7 verify half)
# --------------------------------------------------------------------------


def test_unterminated_tail_finding_via_stale_mtime(tmp_path: Path):
    """AC-7: "An unterminated tail is never quarantined by ingest, and
    `bth verify` reports it once ... the file is unchanged for 7 days."""
    catalog_dir, project_seg, _mirror_seg = _ingested_project_and_mirror_segment(tmp_path)

    # Append an incomplete trailing line (no closing \n) past the watermark
    # ingest already recorded for the one complete line above.
    with open(project_seg, "ab") as f:
        f.write(b'{"eid": "not-yet-terminated"')

    old = time.time() - 8 * 86400
    os.utime(project_seg, (old, old))

    result = verify_runlog(catalog_dir)
    findings = [f for f in result.stats["findings"] if f["type"] == "unterminated_tail"]
    assert len(findings) == 1
    assert findings[0]["path"] == "seg1.jsonl"
    assert findings[0]["root_kind"] == "project"


def test_unterminated_tail_not_reported_while_fresh_and_no_later_segment(tmp_path: Path):
    """The negative case: a fresh, still-being-written tail (neither a
    later segment from the same writer nor 7 days stale) must NOT be
    reported -- it may simply be mid-write."""
    catalog_dir, project_seg, _mirror_seg = _ingested_project_and_mirror_segment(tmp_path)

    with open(project_seg, "ab") as f:
        f.write(b'{"eid": "still-being-written"')
    # mtime left at "now" -- neither trigger condition holds.

    result = verify_runlog(catalog_dir)
    findings = [f for f in result.stats["findings"] if f["type"] == "unterminated_tail"]
    assert findings == []


def test_unterminated_tail_finding_via_later_segment_from_same_writer(tmp_path: Path):
    """The other trigger: a LATER segment from the same writer (same host
    and pid, a higher `start_ns`) proves the earlier one will never be
    completed, regardless of its mtime."""
    from bathos.runlog.envelope import segment_stem

    repo, pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    log_dir = repo / ".bth" / "log"
    mirror_dir = mirror_dir_for(pid, "repo")

    old_stem = segment_stem("myhost", 4242, 1_700_000_000_000_000_000)
    new_stem = segment_stem("myhost", 4242, 1_700_000_100_000_000_000)

    line = _envelope_line(
        kind="run.started",
        entity=["run-later-seg"],
        data=_run_started_data(None, "2026-01-01T00:01:00.000000Z"),
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer=old_stem,
        seq=1,
        ts="2026-01-01T00:01:00.000000Z",
        eid="e-later-seg",
    )
    # The old segment: one complete line, then an unterminated tail.
    _write_segment(log_dir, f"{old_stem}.jsonl", line.encode() + b'{"eid": "torn-by-rotation"')
    _write_segment(mirror_dir, f"{old_stem}.jsonl", line.encode())
    # The new segment this writer rotated to -- proves the old one is done.
    _write_segment(log_dir, f"{new_stem}.jsonl", b"")
    _write_segment(mirror_dir, f"{new_stem}.jsonl", b"")

    report = run_ingest(catalog_dir)
    assert report.new_events == 1  # only the one complete line

    result = verify_runlog(catalog_dir)
    findings = [f for f in result.stats["findings"] if f["type"] == "unterminated_tail"]
    assert len(findings) == 1
    assert findings[0]["path"] == f"{old_stem}.jsonl"


# --------------------------------------------------------------------------
# Clean fixture: zero findings
# --------------------------------------------------------------------------


def test_clean_fixture_yields_zero_findings(tmp_path: Path):
    repo, pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    line = _envelope_line(
        kind="run.started",
        entity=["run-clean"],
        data=_run_started_data(None, "2026-01-01T00:01:00.000000Z"),
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="w1",
        seq=1,
        ts="2026-01-01T00:01:00.000000Z",
        eid="e-clean",
    )
    log_dir = repo / ".bth" / "log"
    mirror_dir = mirror_dir_for(pid, "repo")
    _write_segment(log_dir, "seg1.jsonl", line.encode())
    _write_segment(mirror_dir, "seg1.jsonl", line.encode())

    run_ingest(catalog_dir)

    result = verify_runlog(catalog_dir)
    assert result.ok is True
    assert result.errors == []
    assert result.stats["findings"] == []
