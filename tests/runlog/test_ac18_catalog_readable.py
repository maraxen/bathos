"""Regression tests for `bathos.index.catalog_readable` (AC-18 review
finding 2, post-4727b872).

At 4727b872, roughly a dozen migrated readers gated their `connect_read`
call on a bare `(catalog_dir / "bathos.db").exists()` check carried over
unchanged from the pre-migration code. That guard is correct before
cut-over, but Migration step 4(d) renames `bathos.db` to `bathos.db.frozen`,
so the guard goes false forever afterwards -- with the flag on and no
`bathos.db` present (the real post-cutover state), those readers silently
returned empty/`None` instead of falling through to the folded index.

`catalog_readable(catalog_dir)` = `is_log_mode(catalog_dir) or
(catalog_dir / "bathos.db").exists()` fixes this; every such guard across
`linter.py`, `prereg.py`, `sprint_audit.py`, `capability.py`, `corpus.py`,
`repair.py`, `archive.py`, `artifact_archive.py`, `blast_radius.py`,
`cli_cyclopts.py`, `readback.py`, and `postmortem.py` was replaced with it.

This file: (a) unit tests of `catalog_readable` itself, and (b) end-to-end
flag-on regression tests for three of the migrated readers (`capability`,
`corpus`, `postmortem`) -- built with a real `run_ingest()`-folded index and
NO `bathos.db` on disk, which is exactly the state that made the pre-fix
`.exists()` guard misfire. Each of the three fails on 4727b872 (returns
`None`/`{}`/empty instead of the folded data).
"""

from __future__ import annotations

import json
from pathlib import Path

from bathos.index import catalog_readable
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import assign_project_id, read_project_id, register_main_root

from .conftest import make_git_repo, write_bth_toml
from .test_ac18_flag_on_reads import _build_catalog_with_one_run, _envelope_line, _write_lines


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


# --- catalog_readable() unit tests -------------------------------------


def test_catalog_readable_flag_off_no_bathos_db(tmp_path: Path):
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir(parents=True)
    assert catalog_readable(catalog_dir) is False


def test_catalog_readable_flag_off_bathos_db_exists(tmp_path: Path):
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "bathos.db").write_bytes(b"")
    assert catalog_readable(catalog_dir) is True


def test_catalog_readable_flag_on_no_bathos_db(tmp_path: Path):
    """The exact post-cutover state: marker present, bathos.db gone
    (renamed to bathos.db.frozen by Migration step 4(d))."""
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    assert not (catalog_dir / "bathos.db").exists()
    assert catalog_readable(catalog_dir) is True


def test_catalog_readable_flag_on_with_frozen_bathos_db(tmp_path: Path):
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    (catalog_dir / "bathos.db.frozen").write_bytes(b"")
    assert not (catalog_dir / "bathos.db").exists()
    assert catalog_readable(catalog_dir) is True


# --- end-to-end flag-on regressions for 3 migrated readers -------------


def test_capability_warm_runs_columns_flag_on_no_bathos_db(tmp_path: Path):
    from bathos.capability import SEED_COLUMNS, _warm_runs_columns

    catalog_dir, _run_id = _build_catalog_with_one_run(tmp_path)
    assert not (catalog_dir / "bathos.db").exists()

    columns = _warm_runs_columns(catalog_dir)

    assert columns is not None, (
        "pre-fix behaviour: the stale .exists() guard returns None here even "
        "though the folded index has the runs table"
    )
    for col in SEED_COLUMNS:
        assert col in columns


def test_corpus_catalog_counts_flag_on_no_bathos_db(tmp_path: Path):
    from bathos.corpus import _catalog_counts

    repo, pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    common = dict(main_root=repo, worktree_root=repo, project="repo", project_id=pid, writer="w1")
    run_id = "33333333-3333-3333-3333-333333333333"
    lines = [
        _envelope_line(
            kind="run.started",
            entity=[run_id],
            data={
                "id": run_id,
                "project_slug": "repo",
                "command": "python fit_model.py --seed 1",
                "argv": ["python", "fit_model.py", "--seed", "1"],
                "git_hash": "aaa",
                "git_branch": "main",
                "git_dirty": False,
                "timestamp": "2026-01-01T00:01:00.000000Z",
                "campaign_id": None,
                "agent_mode": "manual",
                "sidecar": {
                    "kind": "experiment",
                    "result_schema": {},
                    "outcomes": {"pass": {"condition": "true", "is_residual": False}},
                    "popper_null_pass_rate": 0.1,
                    "popper_alt_pass_rate": 0.9,
                    "popper_stopping_threshold": 0.05,
                    "popper_weights": {},
                },
            },
            seq=1,
            ts="2026-01-01T00:01:00.000000Z",
            eid="e-started-corpus",
            **common,
        ),
        _envelope_line(
            kind="run.finished",
            entity=[run_id],
            data={
                "id": run_id,
                "status": "completed",
                "exit_code": 0,
                "duration_s": 1.0,
                "output_paths": [],
                "outcome": "pass",
                "outcome_error_reason": "",
                "outcome_is_residual": False,
                "adversarial_check_status": "",
                "timestamp": "2026-01-01T00:02:00.000000Z",
            },
            seq=2,
            ts="2026-01-01T00:02:00.000000Z",
            eid="e-finished-corpus",
            **common,
        ),
    ]
    _write_lines(repo, *lines)

    from bathos.runlog.ingest import run_ingest

    report = run_ingest(catalog_dir)
    assert report.new_events == 2
    assert not (catalog_dir / "bathos.db").exists()

    counts = _catalog_counts(Path("fit_model.py"), catalog_dir)

    assert counts == {"run_count": 1, "campaign_run_count": 0}, (
        "pre-fix behaviour: the stale .exists() guard returns {} here even "
        "though the folded index has a matching run"
    )


def test_postmortem_find_run_for_scaffold_flag_on_no_bathos_db(tmp_path: Path):
    from bathos.postmortem import find_run_for_scaffold

    catalog_dir, run_id = _build_catalog_with_one_run(tmp_path)
    assert not (catalog_dir / "bathos.db").exists()

    found = find_run_for_scaffold(run_id, catalog_dir)

    assert found is not None, (
        "pre-fix behaviour: the stale .exists() guard falls through to the "
        "cool-tier read_runs() fallback, which is also empty in a purely "
        "event-sourced catalog, so this returned None"
    )
    command, project_slug = found
    assert command == "python x.py"
    assert project_slug == "repo"
