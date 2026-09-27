"""AC-18 delivery step 3, wave d: flag-on smoke tests for the main read
commands.

Task brief: "for the main read commands (ls/list_runs, show/get_run,
find_runs, run_sql SELECT, campaign list/show), with the flag on and a
folded index, they return the folded rows. With the flag off, the existing
tests stay green" (the flag-off half is covered by the pre-existing test
suites this wave's migration ran unmodified: tests/test_query.py,
tests/test_campaigns.py, etc.).

These exercise `bathos.query`'s public functions and
`bathos.campaigns.list_campaigns`/`get_campaign` end to end through
`run_ingest` -- the same path `bth compact` (or the non-blocking
post-command ingest) takes -- rather than reaching into `bathos.index`
directly, so a wiring mistake in the dispatch (`_resolve_backend`'s new
"index" branch, `connect_catalog_db`'s flag-aware read branch) fails here
the way it would for a real `bth ls`/`bth campaign show`.
"""

from __future__ import annotations

import json
from pathlib import Path

from bathos.query import find_runs, get_run, list_runs, run_sql
from bathos.runlog.envelope import build_envelope
from bathos.runlog.ingest import run_ingest
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import assign_project_id, read_project_id, register_main_root

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


def _sidecar_decl() -> dict:
    return {
        "kind": "experiment",
        "result_schema": {},
        "outcomes": {"pass": {"condition": "true", "is_residual": False}},
        "popper_null_pass_rate": 0.1,
        "popper_alt_pass_rate": 0.9,
        "popper_stopping_threshold": 0.05,
        "popper_weights": {},
    }


def _build_catalog_with_one_run(tmp_path: Path) -> tuple[Path, str]:
    """Ingest a single campaign + run.started/run.finished sequence into a
    freshly cut-over catalog. Returns (catalog_dir, run_id)."""
    repo, pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    common = dict(main_root=repo, worktree_root=repo, project="repo", project_id=pid, writer="w1")
    run_id = "11111111-1111-1111-1111-111111111111"
    campaign_id = "22222222-2222-2222-2222-222222222222"

    lines = [
        _envelope_line(
            kind="campaign.created",
            entity=[campaign_id],
            data={
                "id": campaign_id,
                "project_slug": "repo",
                "name": "smoke-campaign",
                "mode": "sequential",
                "question": None,
                "hypothesis": None,
                "status": "open",
                "started_at": "2026-01-01T00:00:00Z",
                "concluded_at": None,
                "conclusion": None,
                "outcome_label": None,
                "parent_campaign_id": None,
                "stopping_threshold": None,
                "negative_check": None,
                "claim_path": None,
                "claim_sha256": None,
                "claim_mode": None,
            },
            seq=1,
            ts="2026-01-01T00:00:00.000000Z",
            eid="e-created",
            **common,
        ),
        _envelope_line(
            kind="run.started",
            entity=[run_id],
            data={
                "id": run_id,
                "project_slug": "repo",
                "command": "python x.py",
                "argv": ["python", "x.py"],
                "git_hash": "aaa",
                "git_branch": "main",
                "git_dirty": False,
                "timestamp": "2026-01-01T00:01:00.000000Z",
                "campaign_id": campaign_id,
                "agent_mode": "manual",
                "sidecar": _sidecar_decl(),
            },
            seq=2,
            ts="2026-01-01T00:01:00.000000Z",
            eid="e-started",
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
                "output_paths": ["/tmp/out.txt"],
                "outcome": "pass",
                "outcome_error_reason": "",
                "outcome_is_residual": False,
                "adversarial_check_status": "",
                "timestamp": "2026-01-01T00:02:00.000000Z",
            },
            seq=3,
            ts="2026-01-01T00:02:00.000000Z",
            eid="e-finished",
            **common,
        ),
    ]
    _write_lines(repo, *lines)

    report = run_ingest(catalog_dir)
    assert report.new_events == 3

    return catalog_dir, run_id


def test_list_runs_flag_on_returns_folded_row(tmp_path: Path):
    catalog_dir, run_id = _build_catalog_with_one_run(tmp_path)

    runs = list_runs(catalog_dir, project="repo")
    assert [r.id for r in runs] == [run_id]
    assert runs[0].status == "completed"
    assert runs[0].outcome == "pass"
    assert runs[0].argv == ["python", "x.py"]


def test_get_run_flag_on_returns_folded_row(tmp_path: Path):
    catalog_dir, run_id = _build_catalog_with_one_run(tmp_path)

    run = get_run(run_id, catalog_dir)
    assert run is not None
    assert run.id == run_id
    assert run.project_slug == "repo"
    assert run.command == "python x.py"
    assert run.output_paths == ["/tmp/out.txt"]


def test_get_run_flag_on_missing_id_returns_none(tmp_path: Path):
    catalog_dir, _run_id = _build_catalog_with_one_run(tmp_path)

    assert get_run("does-not-exist", catalog_dir) is None


def test_find_runs_flag_on_returns_folded_row(tmp_path: Path):
    catalog_dir, run_id = _build_catalog_with_one_run(tmp_path)

    runs = find_runs(catalog_dir, project="repo", status="completed")
    assert [r.id for r in runs] == [run_id]

    # A status filter that matches nothing folds to an empty list, not an error.
    assert find_runs(catalog_dir, status="running") == []


def test_run_sql_select_flag_on_reads_folded_index(tmp_path: Path):
    catalog_dir, run_id = _build_catalog_with_one_run(tmp_path)

    rows = run_sql("SELECT id, status, outcome FROM runs", catalog_dir)
    assert rows == [(run_id, "completed", "pass")]


def test_campaign_list_and_show_flag_on_return_folded_rows(tmp_path: Path):
    from bathos.campaigns import connect_catalog_db, get_campaign, list_campaigns

    catalog_dir, run_id = _build_catalog_with_one_run(tmp_path)
    campaign_id = "22222222-2222-2222-2222-222222222222"

    db = connect_catalog_db(catalog_dir, read_only=True)
    assert db is not None  # flag on: always a (possibly empty) index to query
    try:
        campaigns = list_campaigns(db, project_slug="repo", catalog_dir=catalog_dir)
        assert [c.id for c in campaigns] == [campaign_id]
        assert campaigns[0].name == "smoke-campaign"
        assert campaigns[0].mode == "sequential"

        shown = get_campaign(db, campaign_id, catalog_dir=catalog_dir)
        assert shown is not None
        assert shown.id == campaign_id
        assert shown.status == "open"

        # Short-prefix resolution still works against the folded index.
        shown_by_prefix = get_campaign(db, campaign_id[:8], catalog_dir=catalog_dir)
        assert shown_by_prefix is not None
        assert shown_by_prefix.id == campaign_id
    finally:
        db.close()


def test_connect_catalog_db_flag_on_with_no_index_yet_returns_empty_connection(tmp_path: Path):
    """Before the first ingest, flag on: no bathos.db, no index.db yet --
    `connect_catalog_db(read_only=True)` must not fall back to the old
    "no bathos.db -> None" contract (that was the pre-cut-over meaning of
    "nothing to query yet"); it returns a live connection over the empty
    schema instead, so callers see zero rows rather than erroring.
    """
    from bathos.campaigns import connect_catalog_db, list_campaigns

    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    db = connect_catalog_db(catalog_dir, read_only=True)
    assert db is not None
    try:
        assert list_campaigns(db, catalog_dir=catalog_dir) == []
    finally:
        db.close()
