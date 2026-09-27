"""End-to-end ingest tests for delivery step 3, wave b: the campaign fold,
edges, anchors, ledgers, and submit provenance -- exercised through
`run_ingest` + `connect_read`, the same path a real `bth compact` (or the
non-blocking post-command ingest) takes.

Several tests drive the REAL write-site function (flag ON) rather than a
hand-written envelope dict, so a field-name mismatch between an emitter's
`data` payload and the corresponding fold function fails here exactly the
way it would in production -- this is what caught wave a's own defect class.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from bathos.runlog.envelope import build_envelope
from bathos.runlog.index import connect_read
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


def _campaign_created_data(campaign_id: str, mode: str = "sequential", **extra) -> dict:
    data = {
        "id": campaign_id,
        "project_slug": "repo",
        "name": "c",
        "mode": mode,
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
    }
    data.update(extra)
    return data


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
        "sidecar": _sidecar_decl(),
    }
    data.update(extra)
    return data


def _run_finished_data(run_id: str, ts: str, outcome: str = "pass", **extra) -> dict:
    data = {
        "id": run_id,
        "status": "completed",
        "exit_code": 0,
        "duration_s": 1.0,
        "output_paths": [],
        "outcome": outcome,
        "outcome_error_reason": "",
        "outcome_is_residual": False,
        "adversarial_check_status": "",
        "timestamp": ts,
    }
    data.update(extra)
    return data


# --- campaign fold, hand-built envelopes ------------------------------------


def test_campaign_fold_end_to_end_and_run_columns_filled(tmp_path: Path):
    repo, pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    common = dict(main_root=repo, worktree_root=repo, project="repo", project_id=pid, writer="w1")
    lines = [
        _envelope_line(
            kind="campaign.created",
            entity=["camp-1"],
            data={
                "id": "camp-1",
                "project_slug": "repo",
                "name": "c",
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
            entity=["run-1"],
            data={
                "project_slug": "repo",
                "command": "python x.py",
                "argv": ["python", "x.py"],
                "git_hash": "aaa",
                "git_branch": "main",
                "git_dirty": False,
                "timestamp": "2026-01-01T00:01:00.000000Z",
                "campaign_id": "camp-1",
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
            seq=2,
            ts="2026-01-01T00:01:00.000000Z",
            eid="e-started",
            **common,
        ),
        _envelope_line(
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
                "timestamp": "2026-01-01T00:01:00.000000Z",
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

    con = connect_read(catalog_dir)
    crow = con.execute(
        "SELECT mode, stopping_threshold, status FROM campaigns WHERE id = ?", ["camp-1"]
    ).fetchone()
    assert crow == ("sequential", 0.05, "open")

    cr = con.execute(
        "SELECT evalue, seq_position FROM campaign_runs WHERE campaign_id = ? AND run_id = ?",
        ["camp-1", "run-1"],
    ).fetchone()
    assert cr[0] == 9.0
    assert cr[1] == 1

    # runs.seq_position/evalue (wave a's stub) now filled from the campaign fold.
    rrow = con.execute("SELECT seq_position, evalue FROM runs WHERE id = ?", ["run-1"]).fetchone()
    assert rrow == (1, 9.0)


def test_campaign_fold_ac20_arrival_order_independence(tmp_path: Path):
    """Deliver the SAME events via two different segment splits/orders (two
    separate catalogs) and assert the resulting campaigns/campaign_runs rows
    are identical."""
    repo_a, pid_a = setup_project(tmp_path, "repo_a")
    repo_b, pid_b = setup_project(tmp_path, "repo_b")
    catalog_a = tmp_path / "catalog_a"
    catalog_b = tmp_path / "catalog_b"
    enable_log_mode(catalog_a)
    enable_log_mode(catalog_b)

    def make_lines(repo, pid):
        common = dict(
            main_root=repo, worktree_root=repo, project=repo.name, project_id=pid, writer="w1"
        )
        created = _envelope_line(
            kind="campaign.created",
            entity=["camp-x"],
            data={
                "id": "camp-x",
                "project_slug": repo.name,
                "name": "c",
                "mode": "sequential",
                "question": None,
                "hypothesis": None,
                "status": "open",
                "started_at": "T0",
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
        )
        started = _envelope_line(
            kind="run.started",
            entity=["run-x"],
            data={
                "project_slug": repo.name,
                "command": "python x.py",
                "argv": [],
                "git_hash": "a",
                "git_branch": "main",
                "git_dirty": False,
                "timestamp": "2026-01-01T00:01:00.000000Z",
                "campaign_id": "camp-x",
                "agent_mode": "manual",
                "sidecar": {
                    "kind": "experiment",
                    "result_schema": {},
                    "outcomes": {"pass": {"condition": "true", "is_residual": False}},
                    "popper_null_pass_rate": 0.2,
                    "popper_alt_pass_rate": 0.8,
                    "popper_stopping_threshold": 0.05,
                    "popper_weights": {},
                },
            },
            seq=2,
            ts="2026-01-01T00:01:00.000000Z",
            eid="e-started",
            **common,
        )
        finished = _envelope_line(
            kind="run.finished",
            entity=["run-x"],
            data={
                "id": "run-x",
                "status": "completed",
                "exit_code": 0,
                "duration_s": 1.0,
                "output_paths": [],
                "outcome": "pass",
                "outcome_error_reason": "",
                "outcome_is_residual": False,
                "adversarial_check_status": "",
                "timestamp": "2026-01-01T00:01:00.000000Z",
            },
            seq=3,
            ts="2026-01-01T00:02:00.000000Z",
            eid="e-finished",
            **common,
        )
        return created, started, finished

    created_a, started_a, finished_a = make_lines(repo_a, pid_a)
    # repo_a: written in forward order, all in one segment.
    _write_lines(repo_a, created_a, started_a, finished_a)
    # repo_b: written in REVERSE order, one event per segment (forces the
    # ingest loop to encounter run.finished before run.started before
    # campaign.created).
    _write_segment(repo_b / ".bth" / "log", "seg_1.jsonl", finished_a.encode())
    _write_segment(repo_b / ".bth" / "log", "seg_2.jsonl", started_a.encode())
    _write_segment(repo_b / ".bth" / "log", "seg_3.jsonl", created_a.encode())

    run_ingest(catalog_a)
    run_ingest(catalog_b)

    con_a = connect_read(catalog_a)
    con_b = connect_read(catalog_b)
    row_a = con_a.execute(
        "SELECT mode, stopping_threshold FROM campaigns WHERE id = 'camp-x'"
    ).fetchone()
    row_b = con_b.execute(
        "SELECT mode, stopping_threshold FROM campaigns WHERE id = 'camp-x'"
    ).fetchone()
    assert row_a == row_b == ("sequential", 0.05)

    cr_a = con_a.execute(
        "SELECT evalue, seq_position FROM campaign_runs WHERE campaign_id='camp-x'"
    ).fetchone()
    cr_b = con_b.execute(
        "SELECT evalue, seq_position FROM campaign_runs WHERE campaign_id='camp-x'"
    ).fetchone()
    assert cr_a == cr_b == (4.0, 1)


# --- review findings: runs.campaign_id must follow campaign.run_added ------
# (HIGH: spec BC-6, lines 374-379 -- "add_run_to_campaign overwrites the
# fragment's single campaign_id ... so the canonical state keeps only B".)


def test_run_campaign_id_filled_from_run_added_only(tmp_path: Path):
    """A run that never carries a campaign_id in its OWN run.started event
    (started outside any campaign) is later added to a campaign purely via
    `campaign.run_added` -- `runs.campaign_id`/`seq_position`/`evalue` must
    still be filled from that campaign, not left NULL because `fold_run`
    itself never sees `campaign.run_added` (a different entity key)."""
    repo, pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    common = dict(main_root=repo, worktree_root=repo, project="repo", project_id=pid, writer="w1")

    lines = [
        _envelope_line(
            kind="campaign.created",
            entity=["camp-1"],
            data=_campaign_created_data("camp-1"),
            seq=1,
            ts="2026-01-01T00:00:00.000000Z",
            eid="e-created",
            **common,
        ),
        _envelope_line(
            kind="run.started",
            entity=["run-1"],
            data=_run_started_data(None, "2026-01-01T00:01:00.000000Z"),
            seq=2,
            ts="2026-01-01T00:01:00.000000Z",
            eid="e-started",
            **common,
        ),
        _envelope_line(
            kind="run.finished",
            entity=["run-1"],
            data=_run_finished_data("run-1", "2026-01-01T00:01:00.000000Z"),
            seq=3,
            ts="2026-01-01T00:02:00.000000Z",
            eid="e-finished",
            **common,
        ),
        _envelope_line(
            kind="campaign.run_added",
            entity=["camp-1", "run-1"],
            data={"campaign_id": "camp-1", "run_id": "run-1", "evalue": 1.0, "seq_position": 1},
            seq=4,
            ts="2026-01-01T00:03:00.000000Z",
            eid="e-added",
            **common,
        ),
    ]
    _write_lines(repo, *lines)

    report = run_ingest(catalog_dir)
    assert report.new_events == 4

    con = connect_read(catalog_dir)
    rrow = con.execute(
        "SELECT campaign_id, seq_position, evalue FROM runs WHERE id = 'run-1'"
    ).fetchone()
    assert rrow[0] == "camp-1"
    assert rrow[1] == 1
    assert rrow[2] == 9.0  # alt/null = 0.9/0.1, from _sidecar_decl()'s defaults


def test_run_reassigned_via_run_added_uses_new_campaign(tmp_path: Path):
    """A run started (and folded) into campaign A, then later moved into
    campaign B purely via `campaign.run_added` -- runs.campaign_id must
    follow the LATEST assignment (B), with seq_position/evalue read from
    B's fold, while `campaign_runs` keeps BOTH memberships (BC-6 union is
    unaffected -- only the run's single denormalized campaign_id changes)."""
    repo, pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    common = dict(main_root=repo, worktree_root=repo, project="repo", project_id=pid, writer="w1")

    lines = [
        _envelope_line(
            kind="campaign.created",
            entity=["camp-a"],
            data=_campaign_created_data("camp-a"),
            seq=1,
            ts="2026-01-01T00:00:00.000000Z",
            eid="e-created-a",
            **common,
        ),
        _envelope_line(
            kind="campaign.created",
            entity=["camp-b"],
            data=_campaign_created_data("camp-b"),
            seq=2,
            ts="2026-01-01T00:00:01.000000Z",
            eid="e-created-b",
            **common,
        ),
        _envelope_line(
            kind="run.started",
            entity=["run-1"],
            data=_run_started_data("camp-a", "2026-01-01T00:01:00.000000Z"),
            seq=3,
            ts="2026-01-01T00:01:00.000000Z",
            eid="e-started",
            **common,
        ),
        _envelope_line(
            kind="run.finished",
            entity=["run-1"],
            data=_run_finished_data("run-1", "2026-01-01T00:01:00.000000Z"),
            seq=4,
            ts="2026-01-01T00:02:00.000000Z",
            eid="e-finished",
            **common,
        ),
        # A LATER re-assignment into camp-b.
        _envelope_line(
            kind="campaign.run_added",
            entity=["camp-b", "run-1"],
            data={"campaign_id": "camp-b", "run_id": "run-1", "evalue": 1.0, "seq_position": 1},
            seq=5,
            ts="2026-01-01T00:03:00.000000Z",
            eid="e-added-b",
            **common,
        ),
    ]
    _write_lines(repo, *lines)

    report = run_ingest(catalog_dir)
    assert report.new_events == 5

    con = connect_read(catalog_dir)
    rrow = con.execute(
        "SELECT campaign_id, seq_position, evalue FROM runs WHERE id = 'run-1'"
    ).fetchone()
    assert rrow[0] == "camp-b"
    assert rrow[0] != "camp-a"  # negative control: the ORIGINAL run.started assignment
    assert rrow[1] == 1
    assert rrow[2] == 9.0

    # BC-6: campaign_runs keeps BOTH memberships despite the single-run
    # campaign_id column following only the latest one.
    members = {
        r[0]
        for r in con.execute(
            "SELECT campaign_id FROM campaign_runs WHERE run_id = 'run-1'"
        ).fetchall()
    }
    assert members == {"camp-a", "camp-b"}


# --- review finding: campaign fold caching (MEDIUM) -------------------------


def test_compute_campaign_fold_called_once_per_batch_for_multiple_members(tmp_path: Path):
    """A single ingest batch that refolds 3 member runs of ONE sequential
    campaign (each via its own run.finished event) must compute that
    campaign's fold exactly once -- not once per member refold plus once
    more for the campaign's own persist step."""
    import bathos.runlog.ingest as ingest_mod

    repo, pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    common = dict(main_root=repo, worktree_root=repo, project="repo", project_id=pid, writer="w1")

    lines = [
        _envelope_line(
            kind="campaign.created",
            entity=["camp-1"],
            data=_campaign_created_data("camp-1"),
            seq=1,
            ts="2026-01-01T00:00:00.000000Z",
            eid="e-created",
            **common,
        ),
    ]
    for i in range(1, 4):
        run_id = f"run-{i}"
        ts_start = f"2026-01-01T00:0{i}:00.000000Z"
        ts_finish = f"2026-01-01T00:0{i}:30.000000Z"
        lines.append(
            _envelope_line(
                kind="run.started",
                entity=[run_id],
                data=_run_started_data("camp-1", ts_start),
                seq=10 + i,
                ts=ts_start,
                eid=f"e-started-{i}",
                **common,
            )
        )
        lines.append(
            _envelope_line(
                kind="run.finished",
                entity=[run_id],
                data=_run_finished_data(run_id, ts_start),
                seq=20 + i,
                ts=ts_finish,
                eid=f"e-finished-{i}",
                **common,
            )
        )
    _write_lines(repo, *lines)

    call_count = 0
    original = ingest_mod._compute_campaign_fold

    def _counting_compute(con, campaign_id):
        nonlocal call_count
        call_count += 1
        return original(con, campaign_id)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ingest_mod, "_compute_campaign_fold", _counting_compute)
        report = run_ingest(catalog_dir)

    assert report.new_events == 7  # 1 created + 3x(started + finished)
    assert call_count == 1

    con = connect_read(catalog_dir)
    positions = {
        r[0]: r[1]
        for r in con.execute("SELECT id, seq_position FROM runs ORDER BY id").fetchall()
    }
    assert positions == {"run-1": 1, "run-2": 2, "run-3": 3}


# --- real emitters -----------------------------------------------------------


def test_real_emitter_campaign_created(tmp_path: Path):
    """Drives `bathos.campaigns.create_campaign` itself (flag ON) rather than
    a hand-built dict -- catches a field-name mismatch between `asdict
    (Campaign)` and the fold's expectations."""
    from bathos.campaigns import create_campaign

    repo, _pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    campaign = create_campaign(
        None, "my campaign", "repo", "exploration", catalog_dir=catalog_dir, cwd=repo
    )

    report = run_ingest(catalog_dir)
    assert report.new_events == 1

    con = connect_read(catalog_dir)
    row = con.execute(
        "SELECT name, mode, project_slug, status FROM campaigns WHERE id = ?", [campaign.id]
    ).fetchone()
    assert row == ("my campaign", "exploration", "repo", "open")


def test_real_emitter_campaign_edge(tmp_path: Path):
    from bathos.campaign_edges import add_campaign_edge

    repo, _pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    db = duckdb.connect(":memory:")
    db.execute(
        "CREATE TABLE campaign_edges (child_campaign_id VARCHAR, parent_campaign_id VARCHAR, "
        "PRIMARY KEY (child_campaign_id, parent_campaign_id))"
    )
    add_campaign_edge(db, "child-camp", "parent-camp", catalog_dir=catalog_dir, cwd=repo)

    report = run_ingest(catalog_dir)
    assert report.new_events == 1

    con = connect_read(catalog_dir)
    row = con.execute("SELECT child_campaign_id, parent_campaign_id FROM campaign_edges").fetchone()
    assert row == ("child-camp", "parent-camp")


def test_real_emitter_anchor(tmp_path: Path):
    from bathos.anchor import AnchorRecord, CatalogAnchorStore

    repo, _pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    store = CatalogAnchorStore(catalog_dir)
    record = AnchorRecord(
        path="figs/a.svg", sha256="abc123", kind="figure", label="fig1", campaign_id="camp-1"
    )
    store.insert(record, cwd=repo)

    report = run_ingest(catalog_dir)
    assert report.new_events == 1

    con = connect_read(catalog_dir)
    row = con.execute(
        "SELECT path, sha256, kind, label, campaign_id FROM sidecar_anchors"
    ).fetchone()
    assert row == ("figs/a.svg", "abc123", "figure", "fig1", "camp-1")


def test_real_emitter_blast_radius(tmp_path: Path):
    from bathos.blast_radius import BlastRadiusRecord, append_ledger_record

    repo, _pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    record = BlastRadiusRecord(
        entity_type="run",
        entity_id="r1",
        to_state="affected",
        anchor_kind="commit",
        anchor_value="deadbeef",
    )
    append_ledger_record(record, catalog_dir, cwd=repo)

    report = run_ingest(catalog_dir)
    assert report.new_events == 1

    con = connect_read(catalog_dir)
    row = con.execute(
        "SELECT entity_type, entity_id, to_state, anchor_value FROM blast_radius_ledger"
    ).fetchone()
    assert row == ("run", "r1", "affected", "deadbeef")


def test_real_emitter_trust_ledger(tmp_path: Path):
    from bathos.trust_ledger import TrustLedgerRecord, append_ledger_record

    repo, _pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    record = TrustLedgerRecord(
        content_hash="ch1", from_state="candidate", to_state="promoted", run_id="r1"
    )
    append_ledger_record(record, catalog_dir, cwd=repo)

    report = run_ingest(catalog_dir)
    assert report.new_events == 1

    con = connect_read(catalog_dir)
    row = con.execute(
        "SELECT content_hash, from_state, to_state, run_id FROM trust_ledger"
    ).fetchone()
    assert row == ("ch1", "candidate", "promoted", "r1")


def test_real_emitter_archived_item(tmp_path: Path):
    from bathos.archived_items import ArchivedItemRecord, append_archived_item_record

    repo, _pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    record = ArchivedItemRecord(
        id="item1", project_slug="repo", event="archived", kind="figure", paths=["a.svg"]
    )
    append_archived_item_record(record, catalog_dir, cwd=repo)

    report = run_ingest(catalog_dir)
    assert report.new_events == 1

    con = connect_read(catalog_dir)
    row = con.execute("SELECT id, project_slug, event, paths FROM archived_items").fetchone()
    assert row[0] == "item1"
    assert row[1] == "repo"
    assert row[2] == "archived"
    assert json.loads(row[3]) == ["a.svg"]


def test_real_emitter_submit_provenance(tmp_path: Path):
    from bathos.catalog import write_submit_provenance

    repo, _pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    write_submit_provenance(
        "repo",
        "scripts/experiments/foo.py",
        "sha256abc",
        "999",
        "exploration",
        catalog_dir,
        submit_id="submit-1",
        cwd=repo,
    )

    report = run_ingest(catalog_dir)
    assert report.new_events == 1

    con = connect_read(catalog_dir)
    row = con.execute(
        "SELECT project_slug, command, myxcel_job_id, slurm_job_id, stage_name "
        "FROM submits WHERE id = 'submit-1'"
    ).fetchone()
    assert row == ("repo", "scripts/experiments/foo.py", "999", "999", "exploration")
