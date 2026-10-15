"""Equivalence and atomicity checks for the batched index fold (`fold_roots_into`).

The batched path (bulk event insert, one transaction, per-batch `_FoldLookup`
cache) must produce exactly the rows the uncached, per-entity path produces,
and a failed fold must leave no partially committed events behind.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from bathos.runlog import ingest as ingest_mod
from bathos.runlog.envelope import build_envelope
from bathos.runlog.ingest import (
    _CAMPAIGN_MEMBER_KINDS,
    _RUN_STARTED_KINDS,
    _bulk_insert_events,
    _event_row,
    _fetch_events_by_entity_key,
    _fetch_events_by_kinds,
    _FoldLookup,
    _insert_event,
    _refold_campaign,
    _refold_edge,
    _refold_run,
    discover_roots,
    fold_roots_into,
)
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import assign_project_id, read_project_id, register_main_root

from .conftest import make_git_repo, write_bth_toml

FOLDED_TABLES = [
    "events",
    "runs",
    "campaigns",
    "campaign_runs",
    "campaign_edges",
    "run_edges",
    "ingest_watermarks",
]


# --- fixture helpers (same envelope shapes as test_ingest_wave_b.py) -------


def _enable_log_mode(catalog_dir: Path) -> None:
    catalog_dir.mkdir(parents=True, exist_ok=True)
    cutover_marker_path(catalog_dir).write_text(
        json.dumps({"at": "2026-01-01T00:00:00Z", "bathos": "test", "attempt": "1", "segments": []})
    )


def _setup_project(tmp_path: Path, name: str = "repo") -> tuple[Path, str | None]:
    repo = make_git_repo(tmp_path / name)
    write_bth_toml(repo, slug=name)
    assign_project_id(repo)
    pid = read_project_id(repo / ".bth.toml")
    register_main_root(repo)
    return repo, pid


def _write_segment(repo: Path, name: str, lines: list[str]) -> None:
    log_dir = repo / ".bth" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / name).write_bytes("".join(lines).encode())


def _line(common: dict, **kwargs) -> str:
    return json.dumps(build_envelope(**common, **kwargs), sort_keys=True) + "\n"


def _campaign_created(common: dict, campaign_id: str, seq: int, ts: str) -> str:
    data = {
        "id": campaign_id,
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
    }
    return _line(
        common,
        kind="campaign.created",
        entity=[campaign_id],
        data=data,
        seq=seq,
        ts=ts,
        eid=f"e-created-{campaign_id}",
    )


def _run_started(common: dict, run_id: str, campaign_id: str | None, seq: int, ts: str) -> str:
    sidecar = {
        "kind": "experiment",
        "result_schema": {},
        "outcomes": {"pass": {"condition": "true", "is_residual": False}},
        "popper_null_pass_rate": 0.1,
        "popper_alt_pass_rate": 0.9,
        "popper_stopping_threshold": 0.05,
        "popper_weights": {},
    }
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
        "sidecar": sidecar,
    }
    return _line(
        common,
        kind="run.started",
        entity=[run_id],
        data=data,
        seq=seq,
        ts=ts,
        eid=f"e-started-{run_id}-{seq}",
    )


def _run_finished(common: dict, run_id: str, seq: int, ts: str, outcome: str = "pass") -> str:
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
    return _line(
        common,
        kind="run.finished",
        entity=[run_id],
        data=data,
        seq=seq,
        ts=ts,
        eid=f"e-finished-{run_id}-{seq}",
    )


def _run_added(common: dict, campaign_id: str, run_id: str, seq: int, ts: str) -> str:
    data = {"campaign_id": campaign_id, "run_id": run_id, "evalue": 1.0, "seq_position": 1}
    return _line(
        common,
        kind="campaign.run_added",
        entity=[campaign_id, run_id],
        data=data,
        seq=seq,
        ts=ts,
        eid=f"e-added-{campaign_id}-{run_id}-{seq}",
    )


def _part_one(common: dict) -> list[str]:
    """Campaigns, runs in a campaign via run.started, a run moved between
    campaigns via campaign.run_added (reassignment), an imported campaign
    member, and a campaign edge plus a run edge."""
    t = "2026-01-01T00:0{}:00.000000Z"
    return [
        _campaign_created(common, "camp-a", 1, t.format(0)),
        _campaign_created(common, "camp-b", 2, t.format(0)),
        _run_started(common, "run-1", "camp-a", 3, t.format(1)),
        _run_finished(common, "run-1", 4, t.format(2)),
        _run_started(common, "run-2", None, 5, t.format(3)),
        _run_added(common, "camp-a", "run-2", 6, t.format(4)),
        _run_finished(common, "run-2", 7, t.format(5)),
        _run_started(common, "run-3", "camp-b", 8, t.format(6)),
        _run_finished(common, "run-3", 9, t.format(7)),
        _run_added(common, "camp-b", "run-2", 10, t.format(8)),
        _line(
            common,
            kind="campaign_run.imported",
            entity=["camp-a", "run-4"],
            data={"campaign_id": "camp-a", "run_id": "run-4", "evalue": 2.0},
            seq=11,
            ts=t.format(9),
            eid="e-imported-run-4",
            origin="migration",
        ),
        _line(
            common,
            kind="edge.added",
            entity=["camp-b", "camp-a", "campaign"],
            data={"src": "camp-b", "dst": "camp-a", "type": "campaign"},
            seq=12,
            ts=t.format(10),
            eid="e-edge-campaign",
        ),
        _line(
            common,
            kind="edge.added",
            entity=["run-3", "run-1", "derived"],
            data={"src": "run-3", "dst": "run-1", "type": "derived"},
            seq=13,
            ts=t.format(11),
            eid="e-edge-run",
        ),
    ]


def _part_two(common: dict) -> list[str]:
    """Follow-up events in a second segment: a new outcome for run-3 and a
    new membership of run-3 in camp-a."""
    t = "2026-01-01T00:1{}:00.000000Z"
    return [
        _run_finished(common, "run-3", 14, t.format(2), outcome="fail"),
        _run_added(common, "camp-a", "run-3", 15, t.format(3)),
    ]


def _common(repo: Path, pid: str | None) -> dict:
    return dict(
        main_root=repo,
        worktree_root=repo,
        project="repo",
        project_id=pid,
        writer="w1",
    )


# --- independent (naive) oracles: the original scan semantics --------------


def _naive_campaign_ids_for_run(con: duckdb.DuckDBPyConnection, run_id: str) -> set[str]:
    ids: set[str] = set()
    for e in _fetch_events_by_kinds(con, _CAMPAIGN_MEMBER_KINDS):
        entity = e.get("entity") or []
        if len(entity) >= 2 and entity[1] == run_id:
            ids.add(entity[0])
    for e in _fetch_events_by_kinds(con, _RUN_STARTED_KINDS):
        entity = e.get("entity") or []
        if entity and entity[0] == run_id:
            cid = (e.get("data") or {}).get("campaign_id")
            if cid:
                ids.add(cid)
    return ids


def _naive_member_run_ids(con: duckdb.DuckDBPyConnection, campaign_id: str) -> set[str]:
    ids: set[str] = set()
    for e in _fetch_events_by_kinds(con, _CAMPAIGN_MEMBER_KINDS):
        entity = e.get("entity") or []
        if len(entity) >= 2 and entity[0] == campaign_id:
            ids.add(entity[1])
    for e in _fetch_events_by_kinds(con, _RUN_STARTED_KINDS):
        entity = e.get("entity") or []
        if entity and (e.get("data") or {}).get("campaign_id") == campaign_id:
            ids.add(entity[0])
    return ids


def _naive_run_added_for_run(con: duckdb.DuckDBPyConnection, run_id: str) -> list[dict]:
    return [
        e
        for e in _fetch_events_by_kinds(con, frozenset({"campaign.run_added"}))
        if len(e.get("entity") or []) >= 2 and e["entity"][1] == run_id
    ]


def _naive_run_added_for_campaign(con: duckdb.DuckDBPyConnection, cid: str) -> list[dict]:
    return [
        e
        for e in _fetch_events_by_kinds(con, frozenset({"campaign.run_added"}))
        if (e["entity"] or [None])[0] == cid
    ]


def _naive_imported_for_campaign(con: duckdb.DuckDBPyConnection, cid: str) -> list[dict]:
    return [
        e
        for e in _fetch_events_by_kinds(con, frozenset({"campaign_run.imported"}))
        if (e["entity"] or [None])[0] == cid
    ]


def _naive_linked_for_campaign(con: duckdb.DuckDBPyConnection, cid: str) -> set[str]:
    return {
        e["entity"][0]
        for e in _fetch_events_by_kinds(con, _RUN_STARTED_KINDS)
        if e.get("entity") and (e.get("data") or {}).get("campaign_id") == cid
    }


def _all_entity_strings(con: duckdb.DuckDBPyConnection) -> set[str]:
    out: set[str] = set()
    for (entity,) in con.execute("SELECT entity FROM events").fetchall():
        out.update(s for s in json.loads(entity) if isinstance(s, str))
    return out


def _dump_tables(db_path: Path) -> dict[str, list[tuple]]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return {
            t: con.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()  # noqa: S608
            for t in FOLDED_TABLES
        }
    finally:
        con.close()


# --- tests -----------------------------------------------------------------


def test_batched_fold_matches_unbatched_per_entity_refold(tmp_path: Path):
    repo, pid = _setup_project(tmp_path)
    catalog = tmp_path / "catalog"
    _enable_log_mode(catalog)
    _write_segment(repo, "seg1.jsonl", _part_one(_common(repo, pid)))
    roots = discover_roots(catalog)

    batched = tmp_path / "batched.db"
    fold_roots_into(batched, roots)

    # Second copy: same events, then every entity re-derived one at a time
    # with no shared lookup (lookup=None and no ambient batch scope).
    unbatched = tmp_path / "unbatched.db"
    fold_roots_into(unbatched, roots)
    con = duckdb.connect(str(unbatched))
    try:
        for (entity,) in con.execute("SELECT DISTINCT entity FROM events").fetchall():
            key = json.loads(entity)
            if len(key) == 3:
                _refold_edge(con, key)
                continue
            kinds = {
                k
                for (k,) in con.execute(
                    "SELECT DISTINCT kind FROM events WHERE entity_key = ?", [entity]
                ).fetchall()
            }
            if kinds & {"run.started", "run.imported", "run.finished"}:
                _refold_run(con, key[0])
            if kinds & {"campaign.created", "campaign.imported"}:
                _refold_campaign(con, key[0])
        con.execute("CHECKPOINT")
    finally:
        con.close()

    got_batched = _dump_tables(batched)
    got_unbatched = _dump_tables(unbatched)
    for table in FOLDED_TABLES:
        assert got_batched[table] == got_unbatched[table], table

    # The fixture must actually exercise membership, not fold to nothing.
    assert [r[0] for r in got_batched["runs"]] == ["run-1", "run-2", "run-3"]
    assert [r[0] for r in got_batched["campaigns"]] == ["camp-a", "camp-b"]
    members = {(r[0], r[1]) for r in got_batched["campaign_runs"]}
    assert ("camp-a", "run-1") in members
    assert ("camp-a", "run-2") in members and ("camp-b", "run-2") in members
    assert ("camp-a", "run-4") in members  # imported member, no run events of its own
    assert len(got_batched["campaign_edges"]) == 1
    assert len(got_batched["run_edges"]) == 1
    con = duckdb.connect(str(batched), read_only=True)
    try:
        # Latest run_added (camp-b) wins the denormalized runs.campaign_id.
        cid = con.execute("SELECT campaign_id FROM runs WHERE id = 'run-2'").fetchone()[0]
    finally:
        con.close()
    assert cid == "camp-b"


def test_lookup_matches_naive_scans_and_prefetch_keeps_row_order(tmp_path: Path):
    repo, pid = _setup_project(tmp_path)
    catalog = tmp_path / "catalog"
    _enable_log_mode(catalog)
    _write_segment(repo, "seg1.jsonl", _part_one(_common(repo, pid)))
    db = tmp_path / "index.db"
    fold_roots_into(db, discover_roots(catalog))

    con = duckdb.connect(str(db), read_only=True)
    try:
        universe = _all_entity_strings(con) | {"no-such-id"}
        lk = _FoldLookup(con)
        lk.prefetch_entity_keys([[s] for s in universe] + [["run-1", "run-2"]])
        for s in universe:
            assert lk.events_for_entity_key([s]) == _fetch_events_by_entity_key(con, [s]), s
            assert lk.campaign_ids_for_run(s) == _naive_campaign_ids_for_run(con, s), s
            assert lk.campaign_member_run_ids(s) == _naive_member_run_ids(con, s), s
            assert lk.run_added_events_for_run(s) == _naive_run_added_for_run(con, s), s
            assert lk.run_added_events_for_campaign(s) == _naive_run_added_for_campaign(con, s), s
            assert lk.imported_member_events_for_campaign(s) == _naive_imported_for_campaign(
                con, s
            ), s
            assert lk.linked_run_ids_for_campaign(s) == _naive_linked_for_campaign(con, s), s
        # A key with no rows memoizes as an empty list, not an error.
        assert lk.events_for_entity_key(["no-such-id"]) == []
        # The unbatched helpers agree with the same naive scans.
        assert ingest_mod._campaign_ids_for_run(con, "run-2") == _naive_campaign_ids_for_run(
            con, "run-2"
        )
    finally:
        con.close()


def test_failed_fold_commits_no_events(tmp_path: Path, monkeypatch):
    repo, pid = _setup_project(tmp_path)
    catalog = tmp_path / "catalog"
    _enable_log_mode(catalog)
    common = _common(repo, pid)
    _write_segment(repo, "seg1.jsonl", _part_one(common))
    db = tmp_path / "index.db"
    fold_roots_into(db, discover_roots(catalog))

    before = _dump_tables(db)
    assert len(before["events"]) == 13

    _write_segment(repo, "seg2.jsonl", _part_two(common))
    roots = discover_roots(catalog)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("injected refold failure")

    # The bulk insert has already run when this fires, so the rollback is
    # what keeps the new events out of the file.
    monkeypatch.setattr(ingest_mod, "_refold_campaign", _boom)
    with pytest.raises(RuntimeError, match="injected refold failure"):
        fold_roots_into(db, roots)

    fresh = duckdb.connect(str(db), read_only=True)
    try:
        assert fresh.execute("SELECT count(*) FROM events").fetchone()[0] == 13
        assert fresh.execute("SELECT count(*) FROM ingest_watermarks").fetchone()[0] == len(
            before["ingest_watermarks"]
        )
    finally:
        fresh.close()
    assert _dump_tables(db) == before

    # A later, unmonkeypatched fold picks the same events up and commits them.
    monkeypatch.undo()
    fold_roots_into(db, roots)
    assert len(_dump_tables(db)["events"]) == 15


def test_failed_fold_into_fresh_file_leaves_zero_events(tmp_path: Path, monkeypatch):
    repo, pid = _setup_project(tmp_path)
    catalog = tmp_path / "catalog"
    _enable_log_mode(catalog)
    _write_segment(repo, "seg1.jsonl", _part_one(_common(repo, pid)))
    roots = discover_roots(catalog)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("injected run failure")

    monkeypatch.setattr(ingest_mod, "_refold_run", _boom)
    db = tmp_path / "fresh.db"
    with pytest.raises(RuntimeError, match="injected run failure"):
        fold_roots_into(db, roots)

    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    finally:
        con.close()


def test_bulk_insert_matches_per_row_insert_including_fallback(tmp_path: Path):
    """Arrow cannot hold an integer `origin` (VARCHAR) or a float `seq`
    exactly as the per-row path does, so this exercises the fallback too."""
    from bathos.runlog.index import init_index_schema

    base = {
        "eid": "e1",
        "v": 1,
        "kind": "run.started",
        "entity": ["r1"],
        "ts": "t",
        "project": "p",
        "project_id": "pid",
        "writer": "w",
        "seq": 1,
        "main_root": "m",
        "worktree_root": "w",
        "origin": "live",
        "data": {"a": 1},
    }
    odd = dict(base, eid="e2", origin=5, seq=2.0)
    bulk = duckdb.connect(str(tmp_path / "bulk.db"))
    per_row = duckdb.connect(str(tmp_path / "per_row.db"))
    try:
        init_index_schema(bulk)
        init_index_schema(per_row)
        _bulk_insert_events(bulk, [_event_row(base), _event_row(odd)])
        _insert_event(per_row, base)
        _insert_event(per_row, odd)
        q = "SELECT * FROM events ORDER BY eid"
        assert bulk.execute(q).fetchall() == per_row.execute(q).fetchall()
        assert len(bulk.execute(q).fetchall()) == 2
    finally:
        bulk.close()
        per_row.close()
