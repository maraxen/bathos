"""The legacy importer (delivery step 4, wave a): spec "Migration" step 2,
the "Import kinds" table, and "Fold rules" / "Imported history" +
"Snapshots".

Builds a real legacy catalog (flag off: cool fragments + warm `bathos.db`,
via the exact same write APIs and harness `ac17_harness`/`test_ac17_
differential.py` use), imports it with `bathos.runlog.importer.
import_legacy_catalog`, ingests + folds the staged events, and compares
against the canonical legacy reference state the AC-17 harness already
computes (`canonical_legacy_state`) -- reusing its comparator
(`_normalize_row`/`_diff_rows`/`_RUNS_EXCLUDE_COLS`) for the tables that
overlap with AC-17's own scope.
"""

from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from bathos.archived_items import ArchivedItemRecord, append_archived_item_record
from bathos.blast_radius import BlastRadiusRecord
from bathos.blast_radius import append_ledger_record as append_blast_radius_record
from bathos.catalog import write_submit_provenance
from bathos.compact import compact
from bathos.index import connect_legacy
from bathos.reap import reconcile_warm_tier
from bathos.runlog.envelope import build_envelope
from bathos.runlog.importer import ImportReport, import_legacy_catalog
from bathos.runlog.index import connect_read
from bathos.runlog.ingest import run_ingest
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import read_project_id
from bathos.trust_ledger import TrustLedgerRecord
from bathos.trust_ledger import append_ledger_record as append_trust_ledger_record

from .ac17_harness import execute_ops, make_backend, sidecar_toml
from .test_ac17_differential import _diff_rows, _normalize_row

BASE_TS = datetime(2020, 1, 1, tzinfo=UTC)


def _dump_table(con, table: str) -> list[dict]:
    cols = [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]  # noqa: S608
    return [
        dict(zip(cols, r, strict=True)) for r in con.execute(f"SELECT * FROM {table}").fetchall()
    ]  # noqa: S608


def _dump_table_by(con, table: str, key: str) -> dict:
    return {row[key]: row for row in _dump_table(con, table)}


def _dump_campaign_runs(con) -> dict:
    return {(row["campaign_id"], row["run_id"]): row for row in _dump_table(con, "campaign_runs")}


def _row(con, table: str, run_id: str) -> dict:
    return _dump_table_by(con, table, "id")[run_id]


def _enable_log_mode(catalog_dir: Path) -> None:
    cutover_marker_path(catalog_dir).write_text(
        json.dumps({"at": "2026-01-01T00:00:00Z", "bathos": "test", "attempt": "1", "segments": []})
    )


def _ops():
    """A small, hand-written op sequence (not `ac17_gen`'s randomized one --
    this suite wants a fixed, easy-to-reason-about shape covering every
    import kind, not fold-vs-legacy parity breadth) covering: a sequential
    campaign with two members (evalue/seq_position), a reap+revert (back to
    running), a plain reap (stays abandoned), a postmortem override, a run
    edge, and an anchor.
    """
    thr = 0.2
    return [
        {"kind": "create_campaign", "handle": "c1", "name": "campaign one", "mode": "sequential"},
        {
            "kind": "start_run",
            "run_id": "run-a",
            "ts": BASE_TS + timedelta(seconds=1),
            "campaign_handle": "c1",
            "sidecar": sidecar_toml(0.1, 0.9, thr),
            "popper": (0.1, 0.9, thr),
        },
        {
            "kind": "finish_run",
            "run_id": "run-a",
            "ts": BASE_TS + timedelta(seconds=2),
            "outcome": "pass",
            "status": "completed",
            "exit_code": 0,
        },
        {
            "kind": "start_run",
            "run_id": "run-d",
            "ts": BASE_TS + timedelta(seconds=3),
            "campaign_handle": "c1",
            "sidecar": sidecar_toml(0.1, 0.9, thr),
            "popper": (0.1, 0.9, thr),
        },
        {
            "kind": "finish_run",
            "run_id": "run-d",
            "ts": BASE_TS + timedelta(seconds=4),
            "outcome": "pass",
            "status": "completed",
            "exit_code": 0,
        },
        {
            "kind": "postmortem",
            "run_id": "run-d",
            "hypothesis_status": "refuted",
            "verdict_override": "fail",
            "author": "importer-test",
        },
        {
            "kind": "start_run",
            "run_id": "run-b",
            "ts": BASE_TS + timedelta(seconds=5),
            "campaign_handle": None,
            "sidecar": None,
            "popper": None,
        },
        {"kind": "reap", "run_id": "run-b", "ts": BASE_TS + timedelta(seconds=6)},
        {"kind": "revert_reap", "run_id": "run-b", "ts": BASE_TS + timedelta(seconds=7)},
        {
            "kind": "start_run",
            "run_id": "run-c",
            "ts": BASE_TS + timedelta(seconds=8),
            "campaign_handle": None,
            "sidecar": None,
            "popper": None,
        },
        {"kind": "reap", "run_id": "run-c", "ts": BASE_TS + timedelta(seconds=9)},
        {"kind": "add_run_edge", "child": "run-d", "parent": "run-a"},
        {
            "kind": "anchor_insert",
            "path": "figs/0.svg",
            "sha256": "a" * 64,
            "anchor_kind": "figure",
            "label": "fig0",
            "campaign_handle": "c1",
            "ts": BASE_TS + timedelta(seconds=10),
        },
    ]


def _add_extra_entities(backend) -> dict:
    """The four entity kinds AC-17 doesn't cover: blast_radius, trust_ledger,
    archived_items, submit provenance -- all written through their real
    legacy-mode (flag off) public write APIs."""
    blast = BlastRadiusRecord(
        entity_type="run",
        entity_id="run-a",
        to_state="affected",
        anchor_kind="commit",
        anchor_value="deadbeef",
        amended_at=(BASE_TS + timedelta(seconds=11)).isoformat(),
    )
    append_blast_radius_record(blast, backend.catalog_dir)

    trust = TrustLedgerRecord(
        content_hash="c" * 64,
        from_state="candidate",
        to_state="promoted",
        run_id="run-a",
        output_path="out/result.json",
        attestation_ref="att-1",
        amended_at=(BASE_TS + timedelta(seconds=12)).isoformat(),
    )
    append_trust_ledger_record(trust, backend.catalog_dir)

    archived = ArchivedItemRecord(
        id="item-1",
        project_slug="proj",
        event="archived",
        kind="script",
        paths=["scripts/experiments/run-a.py"],
        bundle_sha256="d" * 64,
        recorded_at=(BASE_TS + timedelta(seconds=13)).isoformat(),
    )
    append_archived_item_record(archived, backend.catalog_dir)

    write_submit_provenance(
        project_slug="proj",
        command="scripts/experiments/run-a.py",
        sidecar_sha256="e" * 64,
        myxcel_job_id="job-123",
        stage_name="exploration",
        catalog_dir=backend.catalog_dir,
        submit_id="submit-1",
    )

    return {"blast": blast, "trust": trust, "archived": archived}


def _build_legacy_fixture(tmp_path: Path):
    backend = make_backend(tmp_path, "legacy", is_new=False)
    execute_ops(backend, _ops())
    extras = _add_extra_entities(backend)
    return backend, extras


def _import_and_fold(backend, staging_dir: Path) -> ImportReport:
    # A real `bth compact` between the fixture's writes and the import, with
    # the workspace root resolvable (BTH_WORKSPACE_ROOT), so any registered
    # postmortem the fixture applied is actually baked into the warm row
    # before the importer reads it -- exactly what production has (compact
    # runs from inside the project, so its cwd-based postmortem walk always
    # resolves); `execute_ops`'s own incremental `compact()` calls run with
    # neither set, so they never see the postmortem files under
    # `backend.workspace/postmortems/` at all. Test-fixture concern only,
    # not a fold/importer behaviour.
    prior = os.environ.get("BTH_WORKSPACE_ROOT")
    os.environ["BTH_WORKSPACE_ROOT"] = str(backend.workspace)
    try:
        compact(backend.catalog_dir)
    finally:
        if prior is None:
            os.environ.pop("BTH_WORKSPACE_ROOT", None)
        else:
            os.environ["BTH_WORKSPACE_ROOT"] = prior

    pid = read_project_id(backend.workspace / ".bth.toml")
    report = import_legacy_catalog(
        backend.catalog_dir,
        staging_dir,
        project="proj",
        project_id=pid,
        main_root=backend.workspace,
        worktree_root=backend.workspace,
    )
    _enable_log_mode(backend.catalog_dir)
    run_ingest(backend.catalog_dir)
    return report


def test_import_appends_expected_kinds_and_snapshot_zero(tmp_path: Path):
    backend, _extras = _build_legacy_fixture(tmp_path)
    staging_dir = backend.workspace / ".bth" / "log"
    report = _import_and_fold(backend, staging_dir)

    assert report.unreadable == []
    assert report.locked == []
    kinds = {ev["kind"] for ev in report.events}
    assert kinds == {
        "run.imported",
        "run_reap.imported",
        "campaign.imported",
        "campaign_run.imported",
        "edge.imported",
        "anchor.imported",
        "blast_radius.imported",
        "trust_ledger.imported",
        "archived_item.imported",
        "submit.imported",
    }
    for ev in report.events:
        assert ev["origin"] == "migration"
        assert ev["data"]["snapshot"] == 0
        assert ev["data"]["canon"] == 1
        assert ev["data"]["source_class"] in {
            "warm",
            "warm_recreated",
            "fragment",
            "fragment_remote",
            "campaign_json",
            "ledger_json",
            "ledger_reverted",
            "submit_parquet",
        }

    # run.imported fires once per (entity, source): run-a has a warm row AND
    # a cool fragment -- two events, not one pre-merged one.
    run_a_imports = [
        ev for ev in report.events if ev["kind"] == "run.imported" and ev["entity"] == ["run-a"]
    ]
    assert {ev["data"]["source_class"] for ev in run_a_imports} == {"warm", "fragment"}


def test_import_then_fold_matches_canonical_legacy_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    backend, _extras = _build_legacy_fixture(tmp_path)
    staging_dir = backend.workspace / ".bth" / "log"
    _import_and_fold(backend, staging_dir)

    folded = connect_read(backend.catalog_dir)
    try:
        folded_runs = _dump_table_by(folded, "runs", "id")
        folded_campaigns = _dump_table_by(folded, "campaigns", "id")
        folded_campaign_runs = _dump_campaign_runs(folded)
    finally:
        folded.close()

    # Canonical reference: force-rebuild + reconcile, unconditionally, per
    # the spec's "Fold rules" reference recipe -- mutates backend.catalog_dir
    # in place, so this MUST run after the importer has already read it.
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(backend.workspace))
    compact(backend.catalog_dir, force_rebuild=True)
    reconcile_warm_tier(backend.catalog_dir)
    warm = duckdb.connect(str(backend.catalog_dir / "bathos.db"), read_only=True)
    try:
        canon_runs = _dump_table_by(warm, "runs", "id")
        canon_campaigns = _dump_table_by(warm, "campaigns", "id")
        canon_campaign_runs = _dump_campaign_runs(warm)
    finally:
        warm.close()

    all_diffs: list[str] = []

    assert set(canon_runs) == set(folded_runs), (
        f"run id sets differ: only_canon={set(canon_runs) - set(folded_runs)} "
        f"only_folded={set(folded_runs) - set(canon_runs)}"
    )
    for rid in canon_runs:
        l_row = _normalize_row("runs", canon_runs[rid], backend.workspace, {})
        n_row = _normalize_row("runs", folded_runs[rid], backend.workspace, {})
        for col, lv, nv in _diff_rows(l_row, n_row):
            all_diffs.append(f"runs[{rid}].{col}: canon={lv!r} folded={nv!r}")

    assert set(canon_campaigns) == set(folded_campaigns)
    for cid in canon_campaigns:
        l_row = _normalize_row("campaigns", canon_campaigns[cid], backend.workspace, {})
        n_row = _normalize_row("campaigns", folded_campaigns[cid], backend.workspace, {})
        for col, lv, nv in _diff_rows(l_row, n_row):
            all_diffs.append(f"campaigns[{cid}].{col}: canon={lv!r} folded={nv!r}")

    assert set(canon_campaign_runs) == set(folded_campaign_runs)
    for key in canon_campaign_runs:
        lv, nv = canon_campaign_runs[key], folded_campaign_runs[key]
        for col in ("evalue", "seq_position"):
            assert (
                lv[col] == pytest.approx(nv[col])
                if isinstance(lv[col], float)
                else lv[col] == nv[col]
            ), f"campaign_runs[{key}].{col}: canon={lv[col]!r} folded={nv[col]!r}"

    assert not all_diffs, "importer/fold divergence(s) from canonical state:\n" + "\n".join(
        all_diffs
    )

    # run-d's postmortem override took effect through the IMPORTED postmortem
    # fields (BC-2), not a live `run.postmortem_applied` -- this suite never
    # emits one, so this specifically exercises the stage-1 general-field
    # merge carrying postmortem_* fields from the warm/fragment row.
    assert folded_runs["run-d"]["outcome"] == "fail"
    assert folded_runs["run-d"]["postmortem_verdict_override"] == "fail"

    # run-b: reaped then reverted -> running, with NO metadata.reaped (AC-26).
    assert folded_runs["run-b"]["status"] == "running"
    assert json.loads(folded_runs["run-b"]["metadata"]) == {}

    # run-c: reaped, never reverted -> abandoned, WITH metadata.reaped.
    assert folded_runs["run-c"]["status"] == "abandoned"
    reaped = json.loads(folded_runs["run-c"]["metadata"])["reaped"]
    assert reaped["run_id"] == "run-c"
    assert set(reaped) == {
        "run_id",
        "project_slug",
        "reaped_at",
        "reason",
        "window_h",
        "prior_status",
    }


def test_edge_and_anchor_imported(tmp_path: Path):
    backend, _extras = _build_legacy_fixture(tmp_path)
    staging_dir = backend.workspace / ".bth" / "log"
    _import_and_fold(backend, staging_dir)

    con = connect_read(backend.catalog_dir)
    try:
        edges = con.execute("SELECT child_run_id, parent_run_id FROM run_edges").fetchall()
        assert edges == [("run-d", "run-a")]

        anchors = _dump_table(con, "sidecar_anchors")
        assert len(anchors) == 1
        assert anchors[0]["path"] == "figs/0.svg"
        assert anchors[0]["sha256"] == "a" * 64
        assert anchors[0]["label"] == "fig0"
    finally:
        con.close()


def test_extra_ledgers_imported(tmp_path: Path):
    backend, extras = _build_legacy_fixture(tmp_path)
    staging_dir = backend.workspace / ".bth" / "log"
    _import_and_fold(backend, staging_dir)

    con = connect_read(backend.catalog_dir)
    try:
        br_rows = _dump_table(con, "blast_radius_ledger")
        assert len(br_rows) == 1
        assert br_rows[0]["id"] == extras["blast"].id
        assert br_rows[0]["entity_id"] == "run-a"
        assert br_rows[0]["to_state"] == "affected"

        tl_rows = _dump_table(con, "trust_ledger")
        assert len(tl_rows) == 1
        assert tl_rows[0]["id"] == extras["trust"].id
        assert tl_rows[0]["to_state"] == "promoted"

        ai_rows = _dump_table(con, "archived_items")
        assert len(ai_rows) == 1
        assert ai_rows[0]["record_id"] == extras["archived"].record_id
        assert ai_rows[0]["event"] == "archived"

        sub_rows = _dump_table(con, "submits")
        assert len(sub_rows) == 1
        assert sub_rows[0]["project_slug"] == "proj"
        assert sub_rows[0]["command"] == "scripts/experiments/run-a.py"
        assert sub_rows[0]["myxcel_job_id"] == "job-123"
        assert sub_rows[0]["submitted_at"]
    finally:
        con.close()


def test_reimport_unchanged_source_appends_nothing(tmp_path: Path):
    backend, _extras = _build_legacy_fixture(tmp_path)
    staging_dir = backend.workspace / ".bth" / "log"
    first = _import_and_fold(backend, staging_dir)
    assert first.appended > 0

    second = import_legacy_catalog(
        backend.catalog_dir,
        staging_dir,
        project="proj",
        main_root=backend.workspace,
        worktree_root=backend.workspace,
    )
    assert second.appended == 0
    assert second.unchanged == first.appended


def test_changed_source_appends_new_snapshot(tmp_path: Path):
    backend, _extras = _build_legacy_fixture(tmp_path)
    staging_dir = backend.workspace / ".bth" / "log"
    first = _import_and_fold(backend, staging_dir)

    # Change the warm campaigns row's `name` in place (simulating a legacy
    # write between two importer runs, e.g. `campaign.updated`-equivalent
    # legacy behaviour before cut-over).
    warm = duckdb.connect(str(backend.catalog_dir / "bathos.db"))
    try:
        warm.execute("UPDATE campaigns SET name = 'renamed' WHERE name = 'campaign one'")
    finally:
        warm.close()

    second = import_legacy_catalog(
        backend.catalog_dir,
        staging_dir,
        project="proj",
        main_root=backend.workspace,
        worktree_root=backend.workspace,
    )
    assert second.appended >= 1
    changed = [
        ev
        for ev in second.events
        if ev["kind"] == "campaign.imported" and ev["data"].get("source_class") == "warm"
    ]
    assert len(changed) == 1
    assert changed[0]["data"]["snapshot"] == 1
    assert changed[0]["data"]["name"] == "renamed"

    run_ingest(backend.catalog_dir)
    con = connect_read(backend.catalog_dir)
    try:
        names = {r[0] for r in con.execute("SELECT name FROM campaigns").fetchall()}
    finally:
        con.close()
    assert "renamed" in names
    assert first.appended > 0  # sanity: the first pass actually wrote something


def test_corrupt_fragment_gives_legacy_source_unreadable(tmp_path: Path):
    backend, _extras = _build_legacy_fixture(tmp_path)
    # Corrupt run-a's cool fragment in place.
    frag = next((backend.catalog_dir / "runs").rglob("run_run-a.parquet"))
    frag.write_bytes(b"not a parquet file")

    staging_dir = backend.workspace / ".bth" / "log"
    report = import_legacy_catalog(
        backend.catalog_dir,
        staging_dir,
        project="proj",
        main_root=backend.workspace,
        worktree_root=backend.workspace,
    )
    locator = str(frag.relative_to(backend.catalog_dir))
    assert locator in report.unreadable
    unreadable_events = [ev for ev in report.events if ev["kind"] == "legacy_source.unreadable"]
    assert any(ev["entity"] == [locator] for ev in unreadable_events)

    # The warm row for run-a is untouched -- corruption of ONE source never
    # blocks another source of the SAME entity.
    warm_imports = [
        ev for ev in report.events if ev["kind"] == "run.imported" and ev["entity"] == ["run-a"]
    ]
    assert {ev["data"]["source_class"] for ev in warm_imports} == {"warm"}

    # Re-running with the same corrupt bytes appends nothing new for it.
    report2 = import_legacy_catalog(
        backend.catalog_dir,
        staging_dir,
        project="proj",
        main_root=backend.workspace,
        worktree_root=backend.workspace,
    )
    assert not any(ev["kind"] == "legacy_source.unreadable" for ev in report2.events)


def test_locked_warm_db_gives_locked_status_no_crash(tmp_path: Path):
    """A REAL cross-process lock (matching `test_index_connect_legacy.py`'s
    own precedent) -- DuckDB's in-process connection reuse behaves
    differently and does not reproduce the lock error `connect_legacy`
    actually parses for."""
    backend, _extras = _build_legacy_fixture(tmp_path)
    staging_dir = backend.workspace / ".bth" / "log"
    db_path = backend.catalog_dir / "bathos.db"

    # Drop this process's own handle on the db before the holder starts
    # (debt #1995). The fixture leaves an unreferenced DuckDB connection to
    # this same path alive until it is collected, and DuckDB caches one
    # instance per path, so while it survives: `connect_legacy` hands back
    # that cached connection instead of the lock error, AND the holder below
    # cannot take the lock at all -- it dies with `IOException: Could not set
    # lock on file ... Conflicting lock is held`. The old bare-deadline poll
    # then spun against a dead holder and failed ~1 run in 3. Collecting
    # first makes the very first `connect_legacy` call observe the lock.
    gc.collect()

    ready = tmp_path / "holder-has-the-lock"
    holder_code = f"""
import pathlib, sys, duckdb
con = duckdb.connect({str(db_path)!r})
con.execute("SELECT 1")
pathlib.Path({str(ready)!r}).write_text("ready")
sys.stdin.read()
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", holder_code],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait for the holder to SAY it holds the lock rather than guessing
        # with a bare deadline, and surface its own stderr if it died trying.
        deadline = time.monotonic() + 60
        while not ready.exists():
            if proc.poll() is not None:
                stderr = proc.stderr.read() if proc.stderr else ""
                raise AssertionError(
                    f"lock holder exited before acquiring the lock "
                    f"(returncode={proc.returncode}): {stderr}"
                )
            assert time.monotonic() < deadline, "lock holder never signalled it held the lock"
            time.sleep(0.05)

        # The lock is now definitely held by another process, so a single
        # call must observe it -- probing with `connect_legacy` itself, never
        # the importer, since a probe call would otherwise import everything
        # ELSE early and the real assertion below wants a single call that
        # observes the lock on its first and only pass.
        result = connect_legacy(db_path)
        if not isinstance(result, dict):
            result.close()
            raise AssertionError(
                "connect_legacy returned a live connection while another process held "
                "the lock -- this process still has a cached DuckDB instance for the db"
            )
        assert result.get("status") == "legacy_db_locked", result

        report = import_legacy_catalog(
            backend.catalog_dir,
            staging_dir,
            project="proj",
            main_root=backend.workspace,
            worktree_root=backend.workspace,
        )
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    assert str(db_path) in report.locked
    # Nothing crashed, and no `legacy_source.unreadable` was raised for the
    # lock -- AC-18: locked and corrupt are distinct outcomes.
    assert not any(ev["kind"] == "legacy_source.unreadable" for ev in report.events)
    # Every OTHER source (cool fragments, ledgers, extra ledgers, submit,
    # campaign JSON) is still imported -- a locked warm db never blocks the
    # rest of the catalog.
    kinds = {ev["kind"] for ev in report.events}
    assert "run.imported" in kinds  # from cool fragments, even with warm locked
    assert "trust_ledger.imported" in kinds  # from its own fragment, even with warm locked
    # But every candidate that ONLY has a warm source (campaign_run.imported,
    # edge.imported, anchor.imported -- see the Import kinds table) is
    # simply absent this run, not crashed or reported as unreadable.
    assert "campaign_run.imported" not in kinds
    assert "edge.imported" not in kinds
    assert "anchor.imported" not in kinds


def test_mixed_import_then_live_events_on_top(tmp_path: Path):
    """Stage 2 applying on top of stage 1 (spec "Fold rules"): after
    importing a run's legacy history, a LIVE `run.finished` for the SAME
    run_id (representing an old bathos still writing after cut-over, or a
    fixture directly exercising the two-stage merge) overrides the imported
    general fields, and a later live status claim wins the status bundle
    regardless of the imported claim's rank.
    """
    backend, _extras = _build_legacy_fixture(tmp_path)
    staging_dir = backend.workspace / ".bth" / "log"
    _import_and_fold(backend, staging_dir)

    con = connect_read(backend.catalog_dir)
    try:
        before = _row(con, "runs", "run-c")
    finally:
        con.close()
    assert before["status"] == "abandoned"

    # A live `run.finished` lands after the import (spec: "a real finish
    # beats a reap whatever their ts").
    log_dir = backend.workspace / ".bth" / "log"
    pid = read_project_id(backend.workspace / ".bth.toml")
    seg = log_dir / "live.jsonl"
    env = build_envelope(
        kind="run.finished",
        entity=["run-c"],
        data={
            "status": "completed",
            "exit_code": 0,
            "duration_s": 5.0,
            "output_paths": [],
            "outcome": "pass",
        },
        main_root=backend.workspace,
        worktree_root=backend.workspace,
        project="proj",
        project_id=pid,
        writer="live",
        seq=1,
        origin="live",
        ts="2027-01-01T00:00:00.000000Z",
    )
    with open(seg, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(env) + "\n")

    run_ingest(backend.catalog_dir)
    con = connect_read(backend.catalog_dir)
    try:
        after = _row(con, "runs", "run-c")
    finally:
        con.close()
    assert after["status"] == "completed"
    assert after["outcome"] == "pass"
    # metadata.reaped survives the later finish (AC-26: "a reaped run that
    # later finishes keeps metadata.reaped").
    assert json.loads(after["metadata"]).get("reaped", {}).get("run_id") == "run-c"


def test_non_object_reap_ledger_json_is_unreadable_not_a_crash(tmp_path: Path):
    """Review 4a: valid JSON that is not an object (`[]`, `null`, a string) in a live
    or reverted reap ledger must become legacy_source.unreadable -- one bad file
    must never abort the whole (one-shot) import."""
    backend, _extras = _build_legacy_fixture(tmp_path)
    reaped = backend.catalog_dir / "reaped" / "proj"
    (reaped / "reverted").mkdir(parents=True, exist_ok=True)
    (reaped / "run_bad_list.json").write_text("[]")
    (reaped / "run_bad_null.json").write_text("null")
    (reaped / "reverted" / "run_bad_str.json").write_text('"oops"')
    (reaped / "run_numeric_ts.json").write_text('{"run_id": "run-z", "reaped_at": 1700000000}')

    report = import_legacy_catalog(
        backend.catalog_dir,
        backend.workspace / ".bth" / "log",
        project="proj",
        main_root=backend.workspace,
        worktree_root=backend.workspace,
    )
    for name in ("run_bad_list.json", "run_bad_null.json", "reverted/run_bad_str.json"):
        assert f"reaped/proj/{name}" in report.unreadable
    # The rest of the catalog still imported.
    assert any(ev["kind"] == "run.imported" and ev["entity"] == ["run-a"] for ev in report.events)
    assert any(ev["kind"] == "run.imported" and ev["entity"] == ["run-z"] for ev in report.events)
