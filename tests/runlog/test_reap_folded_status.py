"""Reaper candidate discovery on folded status (delivery step 4).

Flag ON: `bathos.reap.reap_runs` discovers its candidates -- and, for a
revert, the ledger record to restore -- from the folded `runs` table via
`bathos.index.connect_read`, never from cool fragments: a flag-on `bth run`
writes no cool fragment at all, so `bathos.catalog.read_runs` would find
nothing regardless of what `run.reaped`/`run.reap_reverted` events already
say. Flag OFF is unchanged (cool fragments via `read_runs`, the JSON ledger
via `read_reap_ledger`).

`test_fold_runs.py` already covers AC-26's fold semantics in isolation
(hand-built event lists); the tests here drive the same scenarios through
the REAL `reap_runs()` end to end (candidate discovery + the write action),
which is what changed in this delivery step.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bathos.catalog import write_run
from bathos.index import connect_read
from bathos.reap import reap_runs
from bathos.runlog.emit import emit_event, run_event_data
from bathos.runlog.ingest import run_ingest
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import assign_project_id, register_main_root
from bathos.schema import Run

from .conftest import make_git_repo, write_bth_toml


def enable_log_mode(catalog_dir: Path) -> None:
    catalog_dir.mkdir(parents=True, exist_ok=True)
    cutover_marker_path(catalog_dir).write_text(
        json.dumps({"at": "2026-01-01T00:00:00Z", "bathos": "test", "attempt": "1", "segments": []})
    )


def setup_project(tmp_path: Path, name: str = "repo") -> Path:
    repo = make_git_repo(tmp_path / name)
    write_bth_toml(repo, slug=name)
    assign_project_id(repo)
    register_main_root(repo)
    return repo


def _make_run(run_id: str, age_hours: float, status: str = "running") -> Run:
    return Run(
        id=run_id,
        project_slug="repo",
        command="test cmd",
        argv=["test"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        status=status,
        timestamp=datetime.now(UTC) - timedelta(hours=age_hours),
    )


def _start(repo: Path, run: Run) -> None:
    emit_event(
        kind="run.started",
        entity=[run.id],
        data=run_event_data(run),
        cwd=repo,
        hard_fail=True,
    )


def _folded_run(catalog_dir: Path, run_id: str) -> dict:
    con = connect_read(catalog_dir)
    try:
        cols = [d[0] for d in con.execute("SELECT * FROM runs LIMIT 0").description]
        row = con.execute("SELECT * FROM runs WHERE id = ?", [run_id]).fetchone()
    finally:
        con.close()
    assert row is not None, f"{run_id} not found in the folded runs table"
    return dict(zip(cols, row, strict=True))


def test_candidate_discovery_reads_folded_status_not_cool_fragments(tmp_path):
    repo = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    run = _make_run("run-old", age_hours=25)
    _start(repo, run)
    run_ingest(catalog_dir)

    runs_dir = catalog_dir / "runs"
    assert not runs_dir.exists() or not list(runs_dir.rglob("run_*.parquet"))

    candidates, _skipped = reap_runs(catalog_dir, older_than_h=24, apply=True, cwd=repo)
    assert [c.id for c in candidates] == ["run-old"]

    run_ingest(catalog_dir)
    folded = _folded_run(catalog_dir, "run-old")
    assert folded["status"] == "abandoned"
    metadata = json.loads(folded["metadata"])
    assert metadata["reaped"]["reason"] == "orphan_window_exceeded"


def test_too_recent_run_not_reaped_via_folded_status(tmp_path):
    repo = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    run = _make_run("run-fresh", age_hours=1)
    _start(repo, run)
    run_ingest(catalog_dir)

    candidates, skipped = reap_runs(catalog_dir, older_than_h=24, apply=True, cwd=repo)
    assert candidates == []
    assert any(r.id == "run-fresh" and reason == "too_recent" for r, reason in skipped)


def test_already_terminal_run_not_rediscovered(tmp_path):
    repo = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    run = _make_run("run-done", age_hours=48)
    _start(repo, run)
    finished = replace(run, status="completed", exit_code=0, duration_s=1.0, outcome="pass")
    emit_event(kind="run.finished", entity=[run.id], data=run_event_data(finished), cwd=repo)
    run_ingest(catalog_dir)

    candidates, skipped = reap_runs(catalog_dir, older_than_h=24, apply=True, cwd=repo)
    assert candidates == []
    assert any(r.id == "run-done" and reason == "not_running" for r, reason in skipped)


def test_revert_reads_ledger_from_folded_metadata_not_json_file(tmp_path):
    repo = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    run = _make_run("run-revert", age_hours=25)
    _start(repo, run)
    run_ingest(catalog_dir)

    reap_runs(catalog_dir, older_than_h=24, apply=True, cwd=repo)
    run_ingest(catalog_dir)

    # No ledger JSON file is ever written under the flag (spec: the event
    # "replaces ... the ledger-JSON write").
    assert not (catalog_dir / "reaped").exists()

    candidates, _skipped = reap_runs(
        catalog_dir,
        older_than_h=24,
        apply=True,
        revert=True,
        revert_ids=["run-revert"],
        cwd=repo,
    )
    assert [c.id for c in candidates] == ["run-revert"]

    run_ingest(catalog_dir)
    folded = _folded_run(catalog_dir, "run-revert")
    assert folded["status"] == "running"
    metadata = json.loads(folded["metadata"])
    assert "reaped" not in metadata


def test_revert_with_no_ledger_entry_is_skipped(tmp_path):
    """A run never reaped has no folded `metadata.reaped` -- revert must
    skip it (`no_ledger_entry`), not crash or invent a prior status."""
    repo = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    run = _make_run("run-never-reaped", age_hours=25)
    _start(repo, run)
    run_ingest(catalog_dir)

    candidates, skipped = reap_runs(
        catalog_dir,
        older_than_h=24,
        apply=True,
        revert=True,
        revert_ids=["run-never-reaped"],
        cwd=repo,
    )
    assert candidates == []
    assert any(r.id == "run-never-reaped" and reason == "no_ledger_entry" for r, reason in skipped)


def test_ac26_reap_then_revert_no_finish_folds_running_through_real_reap_runs(tmp_path):
    """AC-26 (integration level): a reap then revert with no finish folds to
    `running`, driven through the real reap_runs candidate discovery + write
    path end to end (the pure fold is already covered by test_fold_runs.py).
    """
    repo = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    run = _make_run("run-rr", age_hours=30)
    _start(repo, run)
    run_ingest(catalog_dir)

    reap_runs(catalog_dir, older_than_h=24, apply=True, cwd=repo)
    run_ingest(catalog_dir)
    reap_runs(
        catalog_dir, older_than_h=24, apply=True, revert=True, revert_ids=["run-rr"], cwd=repo
    )
    run_ingest(catalog_dir)

    folded = _folded_run(catalog_dir, "run-rr")
    assert folded["status"] == "running"
    metadata = json.loads(folded["metadata"])
    assert "reaped" not in metadata


def test_ac26_reaped_run_that_later_finishes_keeps_metadata_reaped(tmp_path):
    """AC-26: "a reaped run that later finishes keeps metadata.reaped"."""
    repo = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    run = _make_run("run-late-finish", age_hours=25)
    _start(repo, run)
    run_ingest(catalog_dir)

    reap_runs(catalog_dir, older_than_h=24, apply=True, cwd=repo)
    run_ingest(catalog_dir)

    finished = replace(run, status="completed", exit_code=0, duration_s=1.0, outcome="pass")
    emit_event(kind="run.finished", entity=[run.id], data=run_event_data(finished), cwd=repo)
    run_ingest(catalog_dir)

    folded = _folded_run(catalog_dir, "run-late-finish")
    assert folded["status"] == "completed"
    metadata = json.loads(folded["metadata"])
    assert metadata["reaped"]["reason"] == "orphan_window_exceeded"


def test_reconcile_warm_false_skips_legacy_warm_rebuild(tmp_path):
    """Migration step 1's new `reconcile_warm=False` keyword: legacy
    (flag-off) reap still writes the cool fragment rewrite + ledger JSON,
    but never force-rebuilds `bathos.db` nor merges the ledger record into
    its `metadata` column (spec: "the reap writes only its cool fragment
    rewrite and ledger JSON"). Seeds `bathos.db` via `compact()` first (a
    real project already has one) so "untouched" is a meaningful claim, not
    just "never created" (`reconcile_warm_tier` itself already no-ops on a
    catalog with none, `reap.py`'s own `if not db_path.exists(): return`)."""
    import duckdb

    from bathos.compact import compact

    catalog_dir = tmp_path / "catalog"
    (catalog_dir / "runs").mkdir(parents=True)
    run = _make_run("run-legacy-noreconcile", age_hours=25)
    write_run(run, catalog_dir)
    compact(catalog_dir)

    reap_runs(catalog_dir, older_than_h=24, apply=True, reconcile_warm=False)

    ledger_path = catalog_dir / "reaped" / "repo" / "run-legacy-noreconcile.json"
    assert ledger_path.exists()

    con = duckdb.connect(str(catalog_dir / "bathos.db"), read_only=True)
    try:
        metadata = con.execute(
            "SELECT metadata FROM runs WHERE id = ?", ["run-legacy-noreconcile"]
        ).fetchone()[0]
    finally:
        con.close()
    assert "reaped" not in (metadata or "")


def test_reconcile_warm_default_true_rebuilds_warm(tmp_path):
    """Default behaviour (every caller except migrate step 1) is unchanged:
    `reconcile_warm` defaults to True, so a legacy reap still merges the
    reap ledger record into the warm tier's `metadata` column exactly as
    before this keyword existed."""
    import duckdb

    from bathos.compact import compact

    catalog_dir = tmp_path / "catalog"
    (catalog_dir / "runs").mkdir(parents=True)
    run = _make_run("run-legacy-reconcile", age_hours=25)
    write_run(run, catalog_dir)
    compact(catalog_dir)

    reap_runs(catalog_dir, older_than_h=24, apply=True)

    con = duckdb.connect(str(catalog_dir / "bathos.db"), read_only=True)
    try:
        metadata = con.execute(
            "SELECT metadata FROM runs WHERE id = ?", ["run-legacy-reconcile"]
        ).fetchone()[0]
    finally:
        con.close()
    assert "reaped" in metadata


def test_reconcile_warm_false_has_no_effect_when_flag_on(tmp_path):
    """`reconcile_warm` is a legacy-only knob: the flag-on path never
    touches the warm tier regardless of its value."""
    repo = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    run = _make_run("run-flagon-reconcile", age_hours=25)
    _start(repo, run)
    run_ingest(catalog_dir)

    reap_runs(catalog_dir, older_than_h=24, apply=True, cwd=repo, reconcile_warm=False)

    assert not (catalog_dir / "bathos.db").exists()
