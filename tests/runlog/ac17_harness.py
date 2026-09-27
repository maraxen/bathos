"""AC-17 differential-fold test harness.

Drives the SAME randomized operation sequence through the real bathos write
APIs twice -- once with the runlog flag off (legacy: cool fragments + warm
`bathos.db`), once with it on (events + the new index fold) -- against two
isolated (workspace, catalog_dir) fixtures, then compares the resulting rows.

Design notes (see the module docstring in `test_ac17_differential.py` for the
full write-up):

- **Determinism strategy: hybrid.** Run ids, timestamps, sidecar file
  content/paths and anchor `anchored_at` are INJECTED (literal, identical
  values passed to both backends) wherever the API accepts them. The one
  entity whose id is minted internally with no injection point --
  `create_campaign`'s `uuid4()` -- is handled by a per-backend **handle ->
  concrete id mapping** built while executing (`camp_id` dict below); rows
  are translated back to the symbolic handle before cross-backend
  comparison. `docs.md`-style: this is intentionally a hybrid of the two
  choices the task offered ("inject ids" for everything controllable,
  "build a mapping" for the one thing that is not).
- **Wall-clock fields that cannot be injected** (`campaigns.started_at`/
  `concluded_at`, minted by `datetime.now(UTC).isoformat()` inside
  `create_campaign`/`conclude_campaign`; `metadata.reaped.reaped_at`/
  `reverted_at`, minted inside `reap.py`) are scrubbed to a placeholder
  before comparison -- this is a harness limitation (these two functions do
  not accept an injectable clock), not a fold behaviour difference, and is
  reported as such rather than silently a passing "equal" assertion.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import struct
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from bathos.anchor import AnchorRecord, CatalogAnchorStore
from bathos.campaign_edges import add_campaign_edge, add_run_edge
from bathos.campaigns import add_run_to_campaign, conclude_campaign, create_campaign
from bathos.catalog import write_run
from bathos.compact import compact
from bathos.mcp import postmortem_validate_tool
from bathos.reap import _write_reap_ledger_entry
from bathos.runlog.emit import emit_event, run_event_data, sidecar_declaration_for_event
from bathos.runlog.index import connect_read
from bathos.runlog.ingest import run_ingest
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import assign_project_id, register_main_root
from bathos.schema import Run
from bathos.sidecar import parse_sidecar

from .conftest import make_git_repo, write_bth_toml

# Anchor epoch chosen well in the past relative to any real invocation of this
# suite, so a run "started" here is unconditionally reap-eligible (reap_runs'
# cutoff compares run.timestamp against REAL wall-clock `datetime.now(UTC)`,
# not this fixture clock) without needing a second, separate injected time
# axis just for reap eligibility.
BASE_TS = datetime(2020, 1, 1, tzinfo=UTC)

_WALLCLOCK_KEYS = {"reaped_at", "reverted_at"}


# --------------------------------------------------------------------------
# Backend setup
# --------------------------------------------------------------------------


@dataclass
class Backend:
    name: str
    is_new: bool
    workspace: Path
    catalog_dir: Path


def _refuse_real_home() -> None:
    """The harness registers roots and writes mirrors under HOME. Run outside pytest's
    `bathos_test_home` (e.g. from a debugging script) it wrote into the REAL
    ~/.bth/projects.toml and ~/.bth/log-mirror/ (seen 2026-09-26). Refuse instead."""
    import pwd

    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if Path.home().resolve() == real_home:
        raise RuntimeError(
            "ac17_harness refuses to run with HOME at the real home directory; "
            "run it under pytest (bathos_test_home) or point HOME at a temp dir."
        )


def make_backend(tmp_path: Path, name: str, *, is_new: bool) -> Backend:
    _refuse_real_home()
    workspace = tmp_path / f"ws_{name}"
    catalog_dir = tmp_path / f"cat_{name}"
    make_git_repo(workspace)
    write_bth_toml(workspace, slug="proj")
    assign_project_id(workspace)
    register_main_root(workspace)
    gitignore = workspace / ".gitignore"
    gitignore.write_text("/.bth/log/\n")
    catalog_dir.mkdir(parents=True, exist_ok=True)
    if is_new:
        cutover_marker_path(catalog_dir).write_text(
            json.dumps(
                {"at": "2020-01-01T00:00:00Z", "bathos": "test", "attempt": "ac17", "segments": []}
            )
        )
    else:
        # Seed the warm-tier schema (runs/campaigns/campaign_edges/run_edges/
        # sidecar_anchors/...) so operations that read `db` mid-sequence
        # (add_run_to_campaign, conclude_campaign, add_*_edge's cycle check)
        # find their tables already present, same as a real project that has
        # run `bth compact` at least once.
        compact(catalog_dir)
    return Backend(name=name, is_new=is_new, workspace=workspace, catalog_dir=catalog_dir)


def _refresh_and_get_db(backend: Backend):
    """A `db` connection reflecting every op executed on `backend` so far.

    New: ingest then a fresh read-only view (`connect_read`). Legacy: an
    incremental `compact()` (never force_rebuild -- that only happens once,
    at the end, to compute the canonical reference state) then a live
    read-write connection, matching how these write sites are used in
    production (a `bth compact` between actions).
    """
    if backend.is_new:
        run_ingest(backend.catalog_dir)
        return connect_read(backend.catalog_dir)
    compact(backend.catalog_dir)
    return duckdb.connect(str(backend.catalog_dir / "bathos.db"))


def _safe_close(db) -> None:
    with contextlib.suppress(Exception):  # pragma: no cover - defensive only
        db.close()


# --------------------------------------------------------------------------
# Sidecar fixture content
# --------------------------------------------------------------------------


def _f32_safe(x: float) -> float:
    """Round `x` to its nearest exact float32 representation.

    (b) legacy quirk, discovered by this harness, worked around here rather
    than in `campaigns.py`/`compact.py` (out of scope -- see the delivery
    report): `campaigns.stopping_threshold` is a `REAL` (32-bit float)
    column (`compact.py`'s `_CAMPAIGNS_TABLE_SCHEMA` ALTER). Once a
    sequential campaign's threshold is locked, `link_cool_runs_to_campaigns`
    reads it back ALREADY ROUNDED via `get_campaign()` and round-trips that
    rounded value into the cool JSON via `write_campaign_cool` -- so a
    SECOND `compact(force_rebuild=True)` on the identical, unmodified
    fixture (exactly what the spec's canonical-state recipe does:
    `compact(force_rebuild=True)` followed by `reconcile_warm_tier`, which
    force-rebuilds AGAIN internally) re-parses the sidecar's exact float64
    value and compares it against the now-rounded stored one
    (`campaigns.py`, the `sidecar_stopping_threshold != pending_threshold`
    check inside `link_cool_runs_to_campaigns`), producing a FALSE
    threshold-mismatch and nulling out `evalue`/`seq_position` for every
    member on the second rebuild -- reproduced with a two-line repro
    independent of this whole harness (see
    `tests/test_ac17_finding_stopping_threshold_real32_drift.py`). Rounding
    every threshold this harness writes to its exact float32 value up front
    keeps AC-17 focused on fold-vs-legacy parity (what it is chartered to
    test) rather than this orthogonal, pre-existing legacy idempotency bug.
    """
    return struct.unpack("f", struct.pack("f", x))[0]


def sidecar_toml(null_rate: float, alt_rate: float, threshold: float) -> str:
    threshold = _f32_safe(threshold)
    return (
        "[experiment]\n"
        'hypothesis = "h"\n'
        "\n"
        "[popper]\n"
        f"null_pass_rate = {null_rate}\n"
        f"alt_pass_rate = {alt_rate}\n"
        f"stopping_threshold = {threshold}\n"
        "\n"
        "[outcomes.pass]\n"
        'condition = "true"\n'
        'decision = "proceed"\n'
        "\n"
        "[outcomes.fail]\n"
        'condition = "true"\n'
        'decision = "stop"\n'
        "\n"
        "[outcomes.marginal]\n"
        'condition = "true"\n'
        'decision = "review"\n'
        "is_residual = true\n"
        "\n"
        "[result_schema]\n"
    )


def postmortem_toml(
    run_id: str, hypothesis_status: str, verdict_override: str, author: str
) -> str:
    return (
        f'run_id = "{run_id}"\n'
        "\n"
        "[postmortem]\n"
        f'hypothesis_status = "{hypothesis_status}"\n'
        f'verdict_override = "{verdict_override}"\n'
        f'author = "{author}"\n'
        'status = "final"\n'
        'summary = "test postmortem"\n'
    )


# --------------------------------------------------------------------------
# Op execution
# --------------------------------------------------------------------------


@dataclass
class ExecState:
    camp_id: dict[str, str]
    run_obj: dict[str, Run]
    reassigned_out: dict[str, set[str]]  # campaign handle -> run_ids reassigned AWAY from it
    reap_ledger: dict[str, dict]  # run_id -> the ledger record its (live) reap wrote


def execute_ops(backend: Backend, ops: list[dict[str, Any]]) -> ExecState:
    state = ExecState(camp_id={}, run_obj={}, reassigned_out={}, reap_ledger={})

    for op in ops:
        kind = op["kind"]

        if kind == "create_campaign":
            db = _refresh_and_get_db(backend)
            try:
                campaign = create_campaign(
                    db,
                    op["name"],
                    "proj",
                    op["mode"],
                    catalog_dir=backend.catalog_dir,
                    cwd=backend.workspace,
                )
            finally:
                _safe_close(db)
            state.camp_id[op["handle"]] = campaign.id
            state.reassigned_out.setdefault(op["handle"], set())

        elif kind == "start_run":
            _start_run(backend, op, state)

        elif kind == "finish_run":
            _finish_run(backend, op, state)

        elif kind == "reap":
            _reap_run(backend, op, state)

        elif kind == "revert_reap":
            _revert_reap_run(backend, op, state)

        elif kind == "postmortem":
            _register_postmortem(backend, op)

        elif kind == "add_run_to_campaign":
            db = _refresh_and_get_db(backend)
            try:
                add_run_to_campaign(
                    db,
                    state.camp_id[op["campaign_handle"]],
                    op["run_id"],
                    catalog_dir=backend.catalog_dir,
                    cwd=backend.workspace,
                )
            finally:
                _safe_close(db)
            prev = op.get("prev_campaign_handle")
            if prev:
                state.reassigned_out.setdefault(prev, set()).add(op["run_id"])

        elif kind == "conclude":
            db = _refresh_and_get_db(backend)
            try:
                conclude_campaign(
                    db,
                    state.camp_id[op["handle"]],
                    op["outcome_label"],
                    op["conclusion"],
                    workspace_root=backend.workspace,
                    catalog_dir=backend.catalog_dir,
                )
            finally:
                _safe_close(db)

        elif kind == "add_campaign_edge":
            db = _refresh_and_get_db(backend)
            try:
                add_campaign_edge(
                    db,
                    state.camp_id[op["child"]],
                    state.camp_id[op["parent"]],
                    catalog_dir=backend.catalog_dir,
                    cwd=backend.workspace,
                )
            finally:
                _safe_close(db)

        elif kind == "add_run_edge":
            db = _refresh_and_get_db(backend)
            try:
                add_run_edge(
                    db,
                    op["child"],
                    op["parent"],
                    catalog_dir=backend.catalog_dir,
                    cwd=backend.workspace,
                )
            finally:
                _safe_close(db)

        elif kind == "anchor_insert":
            store = CatalogAnchorStore(backend.catalog_dir)
            campaign_id = (
                state.camp_id.get(op["campaign_handle"]) if op.get("campaign_handle") else None
            )
            record = AnchorRecord(
                path=op["path"],
                sha256=op["sha256"],
                kind=op["anchor_kind"],
                label=op["label"],
                campaign_id=campaign_id,
                anchored_at=op["ts"].isoformat(),
            )
            store.insert(record, cwd=backend.workspace)

        else:  # pragma: no cover - defensive
            raise AssertionError(f"unknown op kind {kind!r}")

    return state


def _script_and_sidecar_paths(backend: Backend, run_id: str) -> tuple[Path, Path]:
    d = backend.workspace / "scripts" / "experiments"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{run_id}.py", d / f"{run_id}.bth.toml"


def _start_run(backend: Backend, op: dict, state: ExecState) -> None:
    run_id = op["run_id"]
    ts: datetime = op["ts"]
    sidecar_obj = None
    sidecar_sha256 = ""
    sidecar_path_str = ""

    if op.get("sidecar") is not None:
        script_path, toml_path = _script_and_sidecar_paths(backend, run_id)
        if not script_path.exists():
            script_path.write_text(f"# {run_id}\n")
        toml_path.write_text(op["sidecar"])
        sidecar_obj = parse_sidecar(toml_path)
        sidecar_sha256 = hashlib.sha256(toml_path.read_bytes()).hexdigest()
        sidecar_path_str = str(toml_path)

    campaign_id = state.camp_id.get(op["campaign_handle"]) if op.get("campaign_handle") else None

    run = Run(
        id=run_id,
        project_slug="proj",
        command=f"python {run_id}.py",
        argv=["python", f"{run_id}.py"],
        git_hash="deadbeef",
        git_branch="main",
        git_dirty=False,
        timestamp=ts,
        status="running",
        campaign_id=campaign_id or "",
        agent_mode="manual",
        sidecar_sha256=sidecar_sha256,
        sidecar_path=sidecar_path_str,
        sidecar_mode="declared" if sidecar_obj is not None else "",
    )
    state.run_obj[run_id] = run

    if backend.is_new:
        started_data = {
            **run_event_data(run),
            "sidecar": sidecar_declaration_for_event(sidecar_obj),
            "sidecar_sha256": sidecar_sha256,
            "argv": run.argv,
            "campaign_id": campaign_id or None,
            "agent_mode": "manual",
        }
        emit_event(
            kind="run.started",
            entity=[run.id],
            data=started_data,
            cwd=backend.workspace,
            hard_fail=True,
        )
    else:
        write_run(run, backend.catalog_dir)


def _finish_run(backend: Backend, op: dict, state: ExecState) -> None:
    run = state.run_obj[op["run_id"]]
    ts: datetime = op["ts"]
    duration_s = (ts - run.timestamp).total_seconds()
    finished = replace(
        run,
        status=op["status"],
        exit_code=op["exit_code"],
        duration_s=duration_s,
        outcome=op["outcome"],
    )
    state.run_obj[op["run_id"]] = finished

    if backend.is_new:
        emit_event(
            kind="run.finished",
            entity=[finished.id],
            data=run_event_data(finished),
            cwd=backend.workspace,
        )
    else:
        write_run(finished, backend.catalog_dir)


def _reap_run(backend: Backend, op: dict, state: ExecState) -> None:
    """Reap one run.

    New backend (delivery step 4, "the reaper on folded status"): calls the
    REAL `bathos.reap.reap_runs()`, which now discovers its candidates from
    the folded index (`bathos.index.connect_read`) rather than cool
    fragments -- so this exercises real candidate discovery, not just the
    write action (the earlier version of this function bypassed discovery
    entirely; see git history / the task brief for why that was needed
    before this step). `run_ingest` first, so the run's `run.started` event
    is folded and visible to the scan.

    `reap_runs` sweeps EVERY run whose folded status is `running`, not just
    the one named by this op -- `ac17_gen.py`'s generator guarantees at most
    one run is ever `running` when a `reap` op fires (its `pending_finish`
    mechanism flushes any earlier reap+revert-without-finish first), so this
    real, catalog-wide sweep reaps exactly `op["run_id"]` here.

    Legacy backend: unchanged from before this step -- writes the cool
    fragment + ledger JSON directly, `reap_runs`'s own legacy write action.
    Driving the legacy side through a real `reap_runs()` call too is out of
    this task's scope (only the new backend's bypass is removed).
    """
    run = state.run_obj[op["run_id"]]
    ledger_record = {
        "run_id": run.id,
        "project_slug": run.project_slug,
        "reaped_at": op["ts"].isoformat(),
        "reason": "orphan_window_exceeded",
        "window_h": 24,
        "prior_status": "running",
    }
    state.reap_ledger[op["run_id"]] = ledger_record
    reaped = replace(run, status="abandoned")
    state.run_obj[op["run_id"]] = reaped

    if backend.is_new:
        from bathos.reap import reap_runs

        run_ingest(backend.catalog_dir)
        reap_runs(
            backend.catalog_dir,
            older_than_h=24,
            apply=True,
            cwd=backend.workspace,
        )
    else:
        write_run(reaped, backend.catalog_dir)
        _write_reap_ledger_entry(backend.catalog_dir, reaped.id, reaped.project_slug, ledger_record)


def _revert_reap_run(backend: Backend, op: dict, state: ExecState) -> None:
    """The revert half of `_reap_run`.

    New backend: calls the REAL `reap_runs(revert=True, revert_ids=...)`,
    which (delivery step 4) reads the ledger record to revert from the
    folded run's own `metadata.reaped` (set by the `run.reaped` event
    `_reap_run` above just emitted), not a `reaped/<slug>/<run_id>.json`
    file -- a flag-on reap writes no such file. `run_ingest` first, so that
    `run.reaped` event is folded and visible.

    Legacy backend: unchanged -- writes the cool fragment + moves the
    ledger JSON to `reverted/` directly.
    """
    run = state.run_obj[op["run_id"]]
    ledger_record = state.reap_ledger[op["run_id"]]
    reverted = replace(run, status=ledger_record["prior_status"])
    state.run_obj[op["run_id"]] = reverted

    if backend.is_new:
        from bathos.reap import reap_runs

        run_ingest(backend.catalog_dir)
        reap_runs(
            backend.catalog_dir,
            older_than_h=24,
            apply=True,
            revert=True,
            revert_ids=[reverted.id],
            cwd=backend.workspace,
        )
    else:
        write_run(reverted, backend.catalog_dir)
        ledger_dir = backend.catalog_dir / "reaped" / reverted.project_slug
        ledger_path = ledger_dir / f"{reverted.id}.json"
        reverted_dir = ledger_dir / "reverted"
        reverted_dir.mkdir(parents=True, exist_ok=True)
        ts_str = op["ts"].strftime("%Y%m%d%H%M%S")
        reverted_path = reverted_dir / f"{reverted.id}.{ts_str}.json"
        os.replace(ledger_path, reverted_path)


def _register_postmortem(backend: Backend, op: dict) -> None:
    run_id = op["run_id"]
    d = backend.workspace / "postmortems"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{run_id}.bth.postmortem.toml"
    path.write_text(
        postmortem_toml(run_id, op["hypothesis_status"], op["verdict_override"], op["author"])
    )
    postmortem_validate_tool(
        path=str(path),
        workspace_root=str(backend.workspace),
        catalog_dir=str(backend.catalog_dir),
    )


# --------------------------------------------------------------------------
# Canonical state extraction
# --------------------------------------------------------------------------


def canonical_legacy_state(backend: Backend, monkeypatch) -> dict[str, list[dict]]:
    """`compact(force_rebuild=True)` followed by the reap-ledger merge of
    `reconcile_warm_tier`, both run unconditionally, with `BTH_WORKSPACE_ROOT`
    pinned -- exactly the spec's "Fold rules" reference state."""
    from bathos.reap import reconcile_warm_tier

    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(backend.workspace))
    compact(backend.catalog_dir, force_rebuild=True)
    # reconcile_warm_tier's own guard ("if not db_path.exists(): return") is
    # now satisfied by the compact() call above, so its ledger merge runs
    # unconditionally as the spec requires -- it also redundantly
    # force-rebuilds again internally, which is idempotent here.
    reconcile_warm_tier(backend.catalog_dir)

    db = duckdb.connect(str(backend.catalog_dir / "bathos.db"), read_only=True)
    try:
        return _dump_tables(db)
    finally:
        db.close()


def new_fold_state(backend: Backend) -> dict[str, list[dict]]:
    run_ingest(backend.catalog_dir)
    con = connect_read(backend.catalog_dir)
    try:
        return _dump_tables(con)
    finally:
        con.close()


_TABLES = ("runs", "campaigns", "campaign_runs", "campaign_edges", "run_edges", "sidecar_anchors")


def _dump_tables(con) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for table in _TABLES:
        try:
            cols = [
                d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description  # noqa: S608
            ]
            rows = con.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
        except duckdb.CatalogException:
            # The legacy warm tier only creates a table lazily on first use
            # (e.g. `sidecar_anchors`/`campaign_edges`/`run_edges` are never
            # created by `compact()` unless a fragment of that kind exists,
            # `_ingest_anchor_fragments` et al.) -- a table that was never
            # created has, by construction, zero rows.
            out[table] = []
            continue
        out[table] = [dict(zip(cols, r, strict=True)) for r in rows]
    return out
