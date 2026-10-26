"""Output tiering step 2, index side: adopted entries never become `output_metadata` (spec 261023 section 3.3)
and `runs.adopted_outputs` exists, is folded, and is tolerated when absent (section 5.3).

End to end through `run_ingest`, so a fold that is correct in isolation but unwired in the index fails here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb

from bathos.index import connect_read, index_db_path
from bathos.output_tiers.reference import read_adopted_outputs, reference_hashes
from bathos.readback import _find_run_with_output_sha256
from bathos.runlog.envelope import build_envelope
from bathos.runlog.ingest import run_ingest
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import assign_project_id, read_project_id, register_main_root

from .conftest import make_git_repo, write_bth_toml

RUN_ID = "11111111-1111-1111-1111-111111111111"
SHA_RUN_END = hashlib.sha256(b"run-end bytes").hexdigest()
SHA_ADOPTED = hashlib.sha256(b"adopted bytes").hexdigest()


def _enable_log_mode(catalog_dir: Path) -> None:
    catalog_dir.mkdir(parents=True, exist_ok=True)
    cutover_marker_path(catalog_dir).write_text(
        json.dumps({"at": "2026-01-01T00:00:00Z", "bathos": "test", "attempt": "1", "segments": []})
    )


def _line(common: dict, kind: str, data: dict, seq: int, ts: str, eid: str) -> str:
    env = build_envelope(kind=kind, entity=[RUN_ID], data=data, seq=seq, ts=ts, eid=eid, **common)
    return json.dumps(env, sort_keys=True) + "\n"


def _entry(rel: str, sha: str, phase: str) -> dict:
    return {
        "path": f"/wt/{rel}",
        "status": "present",
        "size_bytes": 5,
        "mtime_unix": 1.0,
        "sha256": sha,
        "relpath": rel,
        "phase": phase,
    }


def _build(tmp_path: Path, *, with_adopt: bool = True) -> tuple[Path, Path, dict]:
    repo = make_git_repo(tmp_path / "repo")
    write_bth_toml(repo, slug="repo")
    assign_project_id(repo)
    pid = read_project_id(repo / ".bth.toml")
    register_main_root(repo)
    catalog_dir = tmp_path / "catalog"
    _enable_log_mode(catalog_dir)
    common = dict(main_root=repo, worktree_root=repo, project="repo", project_id=pid, writer="w1")

    lines = [
        _line(
            common,
            "run.started",
            {
                "id": RUN_ID,
                "project_slug": "repo",
                "command": "python x.py",
                "argv": ["python", "x.py"],
                "git_hash": "aaa",
                "git_branch": "main",
                "git_dirty": False,
                "timestamp": "2026-01-01T00:01:00.000000Z",
                "agent_mode": "manual",
                "outputs_declared": True,
            },
            1,
            "2026-01-01T00:01:00.000000Z",
            "e-started",
        ),
        _line(
            common,
            "run.outputs_hashed",
            {"phase": "run_end", "output_metadata": [_entry("a.bin", SHA_RUN_END, "run_end")]},
            2,
            "2026-01-01T00:02:00.000000Z",
            "e-run-end",
        ),
        _line(
            common,
            "run.finished",
            {
                "id": RUN_ID,
                "status": "completed",
                "exit_code": 0,
                "duration_s": 1.0,
                "output_paths": ["/wt/a.bin"],
                "outcome": "pass",
                "outcome_error_reason": "",
                "outcome_is_residual": False,
                "adversarial_check_status": "",
                "timestamp": "2026-01-01T00:03:00.000000Z",
            },
            3,
            "2026-01-01T00:03:00.000000Z",
            "e-finished",
        ),
    ]
    if with_adopt:
        lines.append(
            _line(
                common,
                "run.outputs_hashed",
                {
                    "phase": "adopted",
                    "output_metadata": [_entry("cells/c1.npy", SHA_ADOPTED, "adopted")],
                },
                4,
                "2026-01-01T00:04:00.000000Z",
                "e-adopted",
            )
        )
    log_dir = repo / ".bth" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "seg1.jsonl").write_text("".join(lines))
    run_ingest(catalog_dir)
    return repo, catalog_dir, common


def _runs_columns(catalog_dir: Path) -> set[str]:
    con = duckdb.connect(str(index_db_path(catalog_dir)), read_only=True)
    try:
        return {
            r[0]
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'runs'"
            ).fetchall()
        }
    finally:
        con.close()


def test_adopt_after_run_end_keeps_run_end_in_output_metadata_and_folds_adopted_separately(
    tmp_path: Path,
):
    _, catalog_dir, _ = _build(tmp_path)
    con = connect_read(catalog_dir)
    try:
        om, adopted = con.execute(
            "SELECT output_metadata, adopted_outputs FROM runs WHERE id = ?", [RUN_ID]
        ).fetchone()
    finally:
        con.close()
    assert [e["relpath"] for e in json.loads(om)] == [
        "a.bin"
    ]  # latest-wins would leave cells/c1.npy
    assert [e["relpath"] for e in json.loads(adopted)] == ["cells/c1.npy"]


def test_readback_finds_the_run_end_sha_but_no_candidate_for_an_adopted_only_sha(tmp_path: Path):
    _, catalog_dir, _ = _build(tmp_path)
    found = _find_run_with_output_sha256(catalog_dir, SHA_RUN_END)
    assert found is not None and found[0] == RUN_ID
    assert _find_run_with_output_sha256(catalog_dir, SHA_ADOPTED) is None


def test_run_without_an_adopt_event_has_null_adopted_outputs(tmp_path: Path):
    _, catalog_dir, _ = _build(tmp_path, with_adopt=False)
    con = connect_read(catalog_dir)
    try:
        assert read_adopted_outputs(con, RUN_ID) == []
        (value,) = con.execute("SELECT adopted_outputs FROM runs WHERE id = ?", [RUN_ID]).fetchone()
    finally:
        con.close()
    assert value is None


def test_read_adopted_outputs_returns_the_folded_entries(tmp_path: Path):
    _, catalog_dir, _ = _build(tmp_path)
    con = connect_read(catalog_dir)
    try:
        entries = read_adopted_outputs(con, RUN_ID)
    finally:
        con.close()
    assert [(e["relpath"], e["sha256"]) for e in entries] == [("cells/c1.npy", SHA_ADOPTED)]


def test_index_built_before_the_column_existed_is_readable_and_refolds_with_it(tmp_path: Path):
    repo, catalog_dir, common = _build(tmp_path)

    # Simulate an index.db from before this step: no adopted_outputs column.
    db = duckdb.connect(str(index_db_path(catalog_dir)))
    try:
        db.execute("ALTER TABLE runs DROP COLUMN adopted_outputs")
    finally:
        db.close()
    assert "adopted_outputs" not in _runs_columns(catalog_dir)

    con = connect_read(catalog_dir)
    try:
        assert read_adopted_outputs(con, RUN_ID) == []  # absent, not a CatalogException
        assert con.execute("SELECT id FROM runs").fetchall() == [
            (RUN_ID,)
        ]  # other readers unaffected
    finally:
        con.close()

    # The next ingest that touches the run adds the column back and refolds it.
    seg = repo / ".bth" / "log" / "seg2.jsonl"
    seg.write_text(
        _line(
            common,
            "run.outputs_hashed",
            {"phase": "adopted", "output_metadata": [_entry("d.bin", SHA_ADOPTED, "adopted")]},
            5,
            "2026-01-01T00:05:00.000000Z",
            "e-adopted-2",
        )
    )
    report = run_ingest(catalog_dir)
    assert report.new_events == 1
    assert "adopted_outputs" in _runs_columns(catalog_dir)
    con = connect_read(catalog_dir)
    try:
        got = {e["relpath"] for e in read_adopted_outputs(con, RUN_ID)}
    finally:
        con.close()
    assert got == {"cells/c1.npy", "d.bin"}


def test_events_in_the_index_give_the_same_reference_as_the_raw_events(tmp_path: Path):
    """`reference_hashes` over events read back from index.db: run_end wins, adopted fills the rest."""
    _, catalog_dir, _ = _build(tmp_path)
    con = connect_read(catalog_dir)
    try:
        rows = con.execute(
            "SELECT eid, kind, entity, ts, worktree_root, data FROM events WHERE entity_key = ?",
            [json.dumps([RUN_ID])],
        ).fetchall()
    finally:
        con.close()
    events = [
        {
            "eid": eid,
            "kind": kind,
            "entity": json.loads(entity),
            "ts": ts,
            "worktree_root": wroot,
            "data": json.loads(data),
        }
        for eid, kind, entity, ts, wroot, data in rows
    ]
    ref = reference_hashes(events)
    assert {k: v.sha256 for k, v in ref.items()} == {
        "a.bin": SHA_RUN_END,
        "cells/c1.npy": SHA_ADOPTED,
    }
    assert ref["a.bin"].phase == "run_end" and ref["cells/c1.npy"].phase == "adopted"
