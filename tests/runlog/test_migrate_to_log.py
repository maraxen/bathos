"""`bth migrate --to-log` (delivery step 4 wave c: Migration steps 0-4).

Built and exercised ONLY against fixture catalogs under `tmp_path`, with
`HOME`/`BTH_CATALOG_DIR` isolated by the suite-wide `bathos_test_home`
fixture (via `tests/runlog/conftest.py`'s `isolated_home`). Never touches
the real `~/.bth/`.

Reuses the AC-17 harness's `make_backend`/`execute_ops` (real legacy write
APIs: `write_run`, `campaigns.py`, `reap_runs`, postmortem validate, ...)
to build realistic legacy fixtures, and `tests/runlog/test_importer.py`'s
hand-written `_ops()` sequence for the fuller multi-entity fixture.
"""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from bathos.catalog import write_run
from bathos.compact import compact
from bathos.index import connect_read
from bathos.runlog.mode import cutover_marker_path, writers_lock
from bathos.runlog.project_id import (
    assign_project_id,
    register_main_root,
)
from bathos.schema import Run

from .ac17_harness import execute_ops, make_backend
from .conftest import make_git_repo, write_bth_toml
from .test_importer import _add_extra_entities, _ops

BASE_TS = datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _squeue_available_and_empty(monkeypatch):
    """This sandbox has no real `squeue` binary, and `my_squeue_job_ids()`
    now fails CLOSED (raises `SqueueUnavailableError`) rather than
    returning `[]` on any query failure (review finding, HIGH, 260927) --
    every test in this module that is not specifically exercising that
    failure mode needs squeue mocked as available-and-empty, matching "no
    real SSH"."""
    monkeypatch.setattr("bathos.runlog.migrate.my_squeue_job_ids", lambda: [])


def _commit_all(root: Path, message: str = "commit") -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=root, check=True, capture_output=True
    )


def _simple_backend(tmp_path: Path):
    """A single-project legacy fixture with one finished run -- committed
    project id, no reap/campaign timing quirks, so its Migration step 3
    diff converges to an EMPTY residual set in one pass (deterministic)."""
    backend = make_backend(tmp_path, "legacy", is_new=False)
    _commit_all(backend.workspace, "assign project id")
    run = Run(
        id="run-a",
        project_slug="proj",
        command="scripts/experiments/run-a.py",
        argv=["python", "run-a.py"],
        git_hash="a" * 40,
        git_branch="main",
        git_dirty=False,
        timestamp=BASE_TS,
        duration_s=1.0,
        exit_code=0,
        status="completed",
    )
    write_run(run, backend.catalog_dir)
    compact(backend.catalog_dir)
    return backend


def _converge(catalog_dir: Path, *, max_attempts: int = 5, force: bool = False):
    """Drive `migrate_to_log` to a terminal outcome, following
    `residual_pending`'s own freshly-recomputed sha256 each time (spec: "a
    fresh run of steps 1-3 reproduces a report with exactly that hash" --
    intervening state, e.g. a newly stale `running` run step 1's own reap
    picks up, can legitimately change the report between attempts, per
    AC-29). Returns the final `MigrateToLogResult`.
    """
    from bathos.runlog.migrate import migrate_to_log

    accept = None
    result = None
    for _ in range(max_attempts):
        result = migrate_to_log(catalog_dir, force=force, accept_residual=accept)
        if result.status != "residual_pending":
            return result
        accept = result.report_sha256
    return result


# --------------------------------------------------------------------------
# Step 0: ids
# --------------------------------------------------------------------------


def test_missing_committed_project_id_refuses(tmp_path: Path):
    from bathos.runlog.migrate import migrate_to_log

    root = tmp_path / "ws"
    make_git_repo(root)
    write_bth_toml(root, slug="proj")  # no project_id
    register_main_root(root)

    result = migrate_to_log(tmp_path / "cat")
    assert result.status == "missing_project_ids"
    assert str(root) in result.missing_project_ids
    assert not cutover_marker_path(tmp_path / "cat").exists()
    assert not (tmp_path / "cat").exists() or list((tmp_path / "cat").iterdir()) == []


def test_uncommitted_project_id_still_refuses(tmp_path: Path):
    """An id assigned but never committed does not satisfy step 0 (spec:
    "present in its committed HEAD")."""
    from bathos.runlog.migrate import migrate_to_log

    root = tmp_path / "ws"
    make_git_repo(root)
    write_bth_toml(root, slug="proj")
    assign_project_id(root)  # writes .bth.toml but does not commit it
    register_main_root(root)

    result = migrate_to_log(tmp_path / "cat")
    assert result.status == "missing_project_ids"
    assert str(root) in result.missing_project_ids


def test_committed_project_id_satisfies_step0(tmp_path: Path):
    root = tmp_path / "ws"
    make_git_repo(root)
    write_bth_toml(root, slug="proj")
    assign_project_id(root)
    _commit_all(root, "assign id")
    register_main_root(root)

    from bathos.runlog.migrate import roots_missing_project_id

    assert roots_missing_project_id() == []


def test_no_git_repo_reads_id_from_working_file(tmp_path: Path):
    """Spec: "or, for a root with no git repository, present in the file"."""
    root = tmp_path / "ws"
    root.mkdir()
    write_bth_toml(root, slug="proj", project_id="11111111-1111-1111-1111-111111111111")
    register_main_root(root)

    from bathos.runlog.migrate import roots_missing_project_id

    assert roots_missing_project_id() == []


# --------------------------------------------------------------------------
# Full happy path
# --------------------------------------------------------------------------


def test_full_happy_path_marker_frozen_and_folded_reads_match(tmp_path: Path):
    backend = _simple_backend(tmp_path)

    result = _converge(backend.catalog_dir)
    assert result.status == "switched"
    assert cutover_marker_path(backend.catalog_dir).exists()
    assert (backend.catalog_dir / "bathos.db.frozen").exists()
    assert not (backend.catalog_dir / "bathos.db").exists()

    marker = json.loads(cutover_marker_path(backend.catalog_dir).read_text())
    assert marker["attempt"] == result.attempt
    assert "at" in marker and "bathos" in marker and "segments" in marker

    con = connect_read(backend.catalog_dir)
    try:
        rows = {
            r[0]: r
            for r in con.execute(
                "SELECT id, status, project_slug, command FROM runs"
            ).fetchall()
        }
    finally:
        con.close()
    assert rows["run-a"] == ("run-a", "completed", "proj", "scripts/experiments/run-a.py")


def test_full_happy_path_richer_fixture(tmp_path: Path):
    """The `_ops()` fixture: campaigns, reap+revert, a plain reap, a
    postmortem, a run edge, an anchor, plus blast_radius/trust_ledger/
    archived_items/submits -- every simple entity kind at least once."""
    backend = make_backend(tmp_path, "legacy", is_new=False)
    _commit_all(backend.workspace, "assign project id")
    execute_ops(backend, _ops())
    _add_extra_entities(backend)
    compact(backend.catalog_dir)

    result = _converge(backend.catalog_dir, max_attempts=6)
    assert result.status == "switched", result.unclassified

    con = connect_read(backend.catalog_dir)
    try:
        statuses = dict(con.execute("SELECT id, status FROM runs").fetchall())
    finally:
        con.close()
    assert statuses["run-a"] == "completed"
    assert statuses["run-b"] == "abandoned"  # reaped by migrate's own step 1 (stale, 2020)
    assert statuses["run-c"] == "abandoned"
    assert statuses["run-d"] == "completed"


# --------------------------------------------------------------------------
# Residual report format
# --------------------------------------------------------------------------


def test_residual_report_format_and_hash(tmp_path: Path):
    from bathos.runlog.migrate import migrate_to_log

    backend = _simple_backend(tmp_path)
    result = migrate_to_log(backend.catalog_dir)
    assert result.status == "residual_pending"
    assert result.residual_lines == []  # this fixture has no residual differences

    report_path = Path(result.report_path)
    assert report_path.is_file()
    text = report_path.read_text()
    assert text == ""  # zero residual lines -> empty file
    import hashlib

    assert hashlib.sha256(text.encode()).hexdigest() == result.report_sha256


def test_residual_report_line_shape(tmp_path: Path):
    """A residual line is exactly `{table, key, column, class, legacy_value,
    staged_value}`, `key` a JSON array, and each value `[v]` or `null`."""
    backend = make_backend(tmp_path, "legacy", is_new=False)
    _commit_all(backend.workspace, "assign project id")
    # A run reaped OUTSIDE migration (a plain reap_runs call, warm never
    # reconciled again) -- this fixture's own metadata/status residual is
    # exactly the `fragment_not_yet_compacted`/`step1_pulled_or_reaped`
    # shape this test wants to see formatted.
    run = Run(
        id="run-stale",
        project_slug="proj",
        command="scripts/experiments/stale.py",
        argv=["python", "stale.py"],
        git_hash="b" * 40,
        git_branch="main",
        git_dirty=False,
        timestamp=BASE_TS,
        duration_s=0.0,
        exit_code=-1,
        status="running",
    )
    write_run(run, backend.catalog_dir)
    compact(backend.catalog_dir)

    from bathos.runlog.migrate import migrate_to_log

    result = migrate_to_log(backend.catalog_dir)
    assert result.status == "residual_pending"
    assert result.residual_lines, "expected at least one residual line"
    for line in result.residual_lines:
        assert set(line) == {"table", "key", "column", "class", "legacy_value", "staged_value"}
        assert isinstance(line["key"], list)
        assert line["legacy_value"] is None or (
            isinstance(line["legacy_value"], list) and len(line["legacy_value"]) == 1
        )
        assert line["staged_value"] is None or (
            isinstance(line["staged_value"], list) and len(line["staged_value"]) == 1
        )
        assert line["class"] == "step1_pulled_or_reaped"

    report_path = Path(result.report_path)
    lines = [json.loads(x) for x in report_path.read_text().splitlines() if x]
    assert lines == sorted(lines, key=lambda x: json.dumps(x, sort_keys=True))


# --------------------------------------------------------------------------
# AC-30-style scenarios: warm_only_row + step1_pulled_or_reaped
# --------------------------------------------------------------------------


def test_step1_pulled_or_reaped_class(tmp_path: Path):
    """AC-30 (spec v35): a stale `running` run that migrate's own step 1
    reaps (`reconcile_warm=False`, so `bathos.db` is left exactly as it
    was) is classified `step1_pulled_or_reaped`, not left unclassified; and
    a warm-only row (present in `bathos.db`, absent from every cool
    fragment) is imported from the `warm` source, so its staged row equals
    its warm row and it yields NO residual line -- it survives the
    migration, which is the property AC-30 guards (v35 corrected v34's
    "`staged_value: null`" wording, which cannot occur: step 3 diffs
    against `bathos.db` and the importer reads it as a first-class source
    regardless of cut-over state -- confirmed empirically before v35
    landed, see the task report on delivery step 4 wave c).
    """
    backend = make_backend(tmp_path, "legacy", is_new=False)
    _commit_all(backend.workspace, "assign project id")

    stale = Run(
        id="run-stale2",
        project_slug="proj",
        command="scripts/experiments/stale2.py",
        argv=["python", "stale2.py"],
        git_hash="d" * 40,
        git_branch="main",
        git_dirty=False,
        timestamp=BASE_TS,
        duration_s=0.0,
        exit_code=-1,
        status="running",
    )
    write_run(stale, backend.catalog_dir)

    warm_only = Run(
        id="run-warmonly",
        project_slug="proj",
        command="scripts/experiments/warmonly.py",
        argv=["python", "warmonly.py"],
        git_hash="c" * 40,
        git_branch="main",
        git_dirty=False,
        timestamp=BASE_TS,
        duration_s=1.0,
        exit_code=0,
        status="completed",
    )
    write_run(warm_only, backend.catalog_dir)
    compact(backend.catalog_dir)

    # Absent from every cool fragment from this point on -- a genuine
    # "warm-only row", present only in bathos.db.
    import pyarrow.parquet as pq

    for f in (backend.catalog_dir / "runs" / "proj").glob("*.parquet"):
        if pq.read_table(f, columns=["id"]).column("id")[0].as_py() == "run-warmonly":
            f.unlink()

    from bathos.runlog.migrate import migrate_to_log

    result = migrate_to_log(backend.catalog_dir)
    assert result.status == "residual_pending"
    classes = {
        (line["table"], tuple(line["key"])): line["class"] for line in result.residual_lines
    }
    assert classes[("runs", ("run-stale2",))] == "step1_pulled_or_reaped"
    # AC-30 (v35): the warm-only row yields NO residual line at all.
    assert ("runs", ("run-warmonly",)) not in classes

    # bathos.db itself is untouched by steps 1-3 (spec AC-30: "leave
    # bathos.db byte-identical") -- only its cool fragment and a reap ledger
    # were written.
    con = duckdb.connect(str(backend.catalog_dir / "bathos.db"), read_only=True)
    try:
        legacy_status = con.execute(
            "SELECT status FROM runs WHERE id = ?", ["run-stale2"]
        ).fetchone()[0]
        legacy_warmonly_row = con.execute(
            "SELECT id, status, command, exit_code FROM runs WHERE id = ?", ["run-warmonly"]
        ).fetchone()
    finally:
        con.close()
    assert legacy_status == "running"
    assert (backend.catalog_dir / "reaped" / "proj" / "run-stale2.json").is_file()
    assert legacy_warmonly_row is not None

    # Converge to a switch, then assert the warm-only row SURVIVED into the
    # staged/folded index, equal to its warm row (AC-30 v35's actual guard).
    final = _converge(backend.catalog_dir)
    assert final.status == "switched"
    con = connect_read(backend.catalog_dir)
    try:
        staged_warmonly_row = con.execute(
            "SELECT id, status, command, exit_code FROM runs WHERE id = ?", ["run-warmonly"]
        ).fetchone()
    finally:
        con.close()
    assert staged_warmonly_row == legacy_warmonly_row


# --------------------------------------------------------------------------
# Locked legacy DB
# --------------------------------------------------------------------------


def test_locked_legacy_db_aborts(tmp_path: Path):
    """A same-process `duckdb.connect()` on the identical path does not
    reproduce DuckDB's cross-process OS-level lock error (it raises a
    DIFFERENT `ConnectionException`, "different configuration than existing
    connections") -- `connect_legacy`'s own docstring notes it was verified
    "against a real cross-process lock". A genuinely separate process is
    needed here."""
    backend = _simple_backend(tmp_path)
    db_path = backend.catalog_dir / "bathos.db"

    holder_script = (
        "import duckdb, sys\n"
        f"con = duckdb.connect({str(db_path)!r})\n"
        "print('locked', flush=True)\n"
        "sys.stdin.readline()\n"
        "con.close()\n"
    )
    holder = subprocess.Popen(
        ["uv", "run", "--no-sync", "python3", "-c", holder_script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    try:
        ready = holder.stdout.readline()
        assert ready.strip() == "locked", f"holder process did not signal readiness: {ready!r}"

        from bathos.runlog.migrate import migrate_to_log

        result = migrate_to_log(backend.catalog_dir)
        assert result.status == "legacy_db_locked"
        assert not cutover_marker_path(backend.catalog_dir).exists()
        assert not (backend.catalog_dir / "bathos.db.frozen").exists()
    finally:
        holder.stdin.write("go\n")
        holder.stdin.close()
        holder.wait(timeout=10)

    # Once released, migration proceeds normally.
    result = _converge(backend.catalog_dir)
    assert result.status == "switched"


# --------------------------------------------------------------------------
# squeue conflict
# --------------------------------------------------------------------------


def test_squeue_conflict_refuses_without_force(tmp_path: Path, monkeypatch):
    backend = _simple_backend(tmp_path)
    monkeypatch.setattr(
        "bathos.runlog.migrate.my_squeue_job_ids", lambda: ["999999"]
    )
    # Make run-a's slurm_job_id (via a submit record) match, so it's a
    # recognized conflict rather than an unrelated queued job.
    write_run(
        Run(
            id="run-with-job",
            project_slug="proj",
            command="scripts/experiments/withjob.py",
            argv=["python", "withjob.py"],
            git_hash="e" * 40,
            git_branch="main",
            git_dirty=False,
            timestamp=BASE_TS,
            duration_s=0.0,
            exit_code=-1,
            status="running",
            slurm_job_id="999999",
        ),
        backend.catalog_dir,
    )
    compact(backend.catalog_dir)

    from bathos.runlog.migrate import migrate_to_log

    result = migrate_to_log(backend.catalog_dir)
    assert result.status == "squeue_conflict"
    assert result.conflicting_jobs == ["999999"]
    assert not cutover_marker_path(backend.catalog_dir).exists()
    assert not (backend.catalog_dir / "bathos.db.frozen").exists()

    forced = _converge(backend.catalog_dir, force=True)
    assert forced.status == "switched"


def test_squeue_unavailable_refuses_unconditionally_even_with_force(
    tmp_path: Path, monkeypatch
):
    """Review finding (HIGH, 260927): `my_squeue_job_ids()` used to return
    `[]` on an OSError/timeout/non-zero exit, indistinguishable from "no
    jobs" -- an unreachable cluster silently let migration proceed. It must
    fail CLOSED with its own status, in BOTH the step-1 shared-lock check
    and the post-exclusive-acquire re-check, and `--force` (which only
    overrides an actually-observed conflict) must not paper over it.
    """
    from bathos.runlog.migrate import SqueueUnavailableError, migrate_to_log

    backend = _simple_backend(tmp_path)

    def _raise():
        raise SqueueUnavailableError("squeue timed out: Command timed out after 30s")

    monkeypatch.setattr("bathos.runlog.migrate.my_squeue_job_ids", _raise)

    result = migrate_to_log(backend.catalog_dir)
    assert result.status == "squeue_unavailable"
    assert "timed out" in result.detail
    assert not cutover_marker_path(backend.catalog_dir).exists()
    assert not (backend.catalog_dir / "bathos.db.frozen").exists()

    forced = migrate_to_log(backend.catalog_dir, force=True)
    assert forced.status == "squeue_unavailable"
    assert not cutover_marker_path(backend.catalog_dir).exists()


def test_squeue_unavailable_in_exclusive_recheck_also_refuses(tmp_path: Path, monkeypatch):
    """The SAME failure mode, but surfacing only on the post-exclusive-lock
    re-check (the step-1 check succeeded moments earlier) -- both call
    sites must fail closed independently."""
    from bathos.runlog.migrate import SqueueUnavailableError, migrate_to_log

    backend = _simple_backend(tmp_path)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise SqueueUnavailableError("squeue: connection refused")
        return []

    monkeypatch.setattr("bathos.runlog.migrate.my_squeue_job_ids", flaky)

    result = migrate_to_log(backend.catalog_dir)
    assert result.status == "squeue_unavailable"
    assert calls["n"] == 2
    assert not cutover_marker_path(backend.catalog_dir).exists()


def test_step1_reap_failure_aborts_with_its_own_status(tmp_path: Path, monkeypatch):
    """Review finding (MEDIUM, 260927): a `reap_runs()` exception during
    step 1 used to be swallowed (`contextlib.suppress(Exception)`), letting
    migration continue as though nothing had failed, on a possibly
    half-reaped catalog. It must abort instead, with its own status naming
    the failure, and touch nothing (no marker, no `bathos.db.frozen`)."""
    from bathos.runlog.migrate import migrate_to_log

    backend = _simple_backend(tmp_path)

    import bathos.reap as reap_module

    real_reap_runs = reap_module.reap_runs

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated reap_runs failure")

    monkeypatch.setattr(reap_module, "reap_runs", _boom)

    result = migrate_to_log(backend.catalog_dir)
    assert result.status == "step1_reap_failed"
    assert "simulated reap_runs failure" in result.detail
    assert not cutover_marker_path(backend.catalog_dir).exists()
    assert not (backend.catalog_dir / "bathos.db.frozen").exists()

    # Once the failure clears, migration proceeds normally. Restore just
    # THIS patch (not monkeypatch.undo(), which would also revert the
    # autouse squeue mock this test relies on).
    monkeypatch.setattr(reap_module, "reap_runs", real_reap_runs)
    result2 = _converge(backend.catalog_dir)
    assert result2.status == "switched"


# --------------------------------------------------------------------------
# Re-run after successful cut-over
# --------------------------------------------------------------------------


def test_rerun_after_switch_is_noop(tmp_path: Path):
    backend = _simple_backend(tmp_path)
    first = _converge(backend.catalog_dir)
    assert first.status == "switched"

    from bathos.runlog.migrate import migrate_to_log

    second = migrate_to_log(backend.catalog_dir)
    assert second.status == "already_migrated"
    assert second.attempt == first.attempt


def test_cleanup_prior_attempt_sweeps_stray_tmp_files(tmp_path: Path):
    """Review finding (LOW, 260927): `_cleanup_prior_attempt_state()` (the
    fresh-attempt path, marker absent) used to leave stray `index.db.*.tmp`
    (an interrupted step 4(a) build) and `.import-<attempt>-*.jsonl.tmp`
    (an interrupted step 4(c) segment copy) behind forever."""
    from bathos.runlog.migrate import migrate_to_log

    backend = _simple_backend(tmp_path)

    stray_index_tmp = backend.catalog_dir / "index.db.deadbeef12ab.tmp"
    stray_index_tmp.write_bytes(b"stale partial build")
    stray_index_wal = backend.catalog_dir / "index.db.deadbeef12ab.tmp.wal"
    stray_index_wal.write_bytes(b"stale wal")

    log_dir = backend.workspace / ".bth" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    stray_segment_tmp = log_dir / ".import-oldattempt-1.jsonl.tmp"
    stray_segment_tmp.write_text('{"stale": true}\n')

    assert stray_index_tmp.exists()
    assert stray_segment_tmp.exists()

    result = migrate_to_log(backend.catalog_dir)
    assert result.status == "residual_pending"  # the fresh-attempt path ran

    assert not stray_index_tmp.exists()
    assert not stray_index_wal.exists()
    assert not stray_segment_tmp.exists()


# --------------------------------------------------------------------------
# Writers-lock semantics
# --------------------------------------------------------------------------


def test_exclusive_lock_blocks_on_concurrent_shared_holder(tmp_path: Path):
    backend = _simple_backend(tmp_path)
    catalog_dir = backend.catalog_dir

    holder_ready = threading.Event()
    release_holder = threading.Event()

    def hold_shared():
        with writers_lock(catalog_dir, exclusive=False):
            holder_ready.set()
            release_holder.wait(timeout=10)

    holder_thread = threading.Thread(target=hold_shared)
    holder_thread.start()
    assert holder_ready.wait(timeout=5)

    from bathos.runlog.migrate import migrate_to_log

    outcome: dict = {}

    def run_migrate():
        outcome["result"] = migrate_to_log(catalog_dir)

    migrate_thread = threading.Thread(target=run_migrate)
    migrate_thread.start()
    # Step 1 (shared) proceeds fine alongside another shared holder; the
    # blocking point is the EXCLUSIVE re-acquire afterward -- give it a
    # moment then confirm it has not finished while the shared lock stands.
    migrate_thread.join(timeout=1.5)
    assert migrate_thread.is_alive(), (
        "migrate_to_log should still be blocked on the exclusive lock "
        "while a shared holder is active"
    )

    release_holder.set()
    holder_thread.join(timeout=5)
    migrate_thread.join(timeout=10)
    assert not migrate_thread.is_alive()
    assert outcome["result"].status == "residual_pending"


def test_squeue_conflict_appearing_between_shared_and_exclusive_refuses(
    tmp_path: Path, monkeypatch
):
    """AC-29: "A `bth submit` that takes the shared lock between step 1's
    shared hold and its exclusive lock makes migrate refuse after the
    re-check." Simulated by making the squeue-conflict check return clean
    the first time (step 1's own check) and conflicting the second (the
    post-exclusive-acquire re-check).
    """
    backend = _simple_backend(tmp_path)
    calls = {"n": 0}

    def fake_conflict(_catalog_dir):
        calls["n"] += 1
        return ["999999"] if calls["n"] >= 2 else []

    monkeypatch.setattr("bathos.runlog.migrate.squeue_conflict", fake_conflict)

    from bathos.runlog.migrate import migrate_to_log

    result = migrate_to_log(backend.catalog_dir)
    assert result.status == "squeue_conflict"
    assert calls["n"] == 2
    assert not cutover_marker_path(backend.catalog_dir).exists()


# --------------------------------------------------------------------------
# --import-legacy (AC-23)
# --------------------------------------------------------------------------


def test_import_legacy_refused_before_cutover(tmp_path: Path):
    backend = _simple_backend(tmp_path)
    from bathos.runlog.migrate import import_legacy_post_cutover

    result = import_legacy_post_cutover(backend.catalog_dir)
    assert result.status == "refused_before_cutover"


def test_import_legacy_after_cutover_imports_stale_write_then_converges(tmp_path: Path):
    backend = _simple_backend(tmp_path)
    result = _converge(backend.catalog_dir)
    assert result.status == "switched"

    # An older bathos install writes a new cool fragment after cut-over.
    late_run = Run(
        id="run-late",
        project_slug="proj",
        command="scripts/experiments/late.py",
        argv=["python", "late.py"],
        git_hash="f" * 40,
        git_branch="main",
        git_dirty=False,
        timestamp=BASE_TS + timedelta(days=1),
        duration_s=1.0,
        exit_code=0,
        status="completed",
    )
    write_run(late_run, backend.catalog_dir)

    from bathos.runlog.ingest import run_ingest
    from bathos.runlog.migrate import import_legacy_post_cutover

    first = import_legacy_post_cutover(backend.catalog_dir)
    assert first.status == "imported"
    run_ingest(backend.catalog_dir)  # a real caller (bth ls/show/...) triggers this

    con = connect_read(backend.catalog_dir)
    try:
        row = con.execute("SELECT id, status FROM runs WHERE id = ?", ["run-late"]).fetchone()
    finally:
        con.close()
    assert row == ("run-late", "completed")

    # A third run (second --import-legacy call) appends nothing new.
    second = import_legacy_post_cutover(backend.catalog_dir)
    assert second.status == "imported"
    assert "appended=0" in second.detail


# --------------------------------------------------------------------------
# Multi-root routing (review finding (a), 260927)
# --------------------------------------------------------------------------


def _legacy_db(catalog_dir: Path):
    """A live, writable connection to a freshly-compacted `bathos.db` (the
    same idiom `ac17_harness._refresh_and_get_db` uses for its legacy
    backend) -- needed by `create_campaign`/`add_run_to_campaign`/
    `add_run_edge`, which take an already-open connection rather than a
    bare `catalog_dir`."""
    compact(catalog_dir)
    return duckdb.connect(str(catalog_dir / "bathos.db"))


def test_multi_root_routing_by_parent_entity(tmp_path: Path):
    """Review finding (a), 260927: the real catalog has 10+ projects
    sharing ONE catalog. An entity without its own `project_slug` must
    route through its PARENT entity's project, not fall to `unaffiliated/`
    just because 2+ roots are registered.

    Three registered roots share one `catalog_dir`:
    - proj1: a run, a campaign, and that run's `campaign_run` membership
      (no `project_slug` of its own -> must route via the campaign);
      also a blast_radius record and an anchor, both keyed via that
      campaign/run.
    - proj2: two runs and a run edge between them (no `project_slug` of
      its own -> must route via the child run's project).
    - proj3: a run and a trust_ledger record keyed on it (no `project_slug`
      of its own -> must route via that run's project).
    """
    from bathos.anchor import AnchorRecord, CatalogAnchorStore
    from bathos.blast_radius import BlastRadiusRecord
    from bathos.blast_radius import append_ledger_record as append_blast_radius_record
    from bathos.campaign_edges import add_run_edge
    from bathos.campaigns import add_run_to_campaign, create_campaign
    from bathos.runlog.migrate import _attempt_staging_root, _import_candidates, root_id
    from bathos.trust_ledger import TrustLedgerRecord
    from bathos.trust_ledger import append_ledger_record as append_trust_ledger_record

    shared_cat = tmp_path / "shared_cat"

    roots: dict[str, Path] = {}
    for slug in ("proj1", "proj2", "proj3"):
        root = tmp_path / slug
        make_git_repo(root)
        write_bth_toml(root, slug=slug)
        assign_project_id(root)
        _commit_all(root, f"assign id for {slug}")
        register_main_root(root)
        roots[slug] = root

    # proj1: run + campaign + membership + blast_radius + anchor.
    write_run(
        Run(
            id="run-p1",
            project_slug="proj1",
            command="a.py",
            argv=["python", "a.py"],
            git_hash="1" * 40,
            git_branch="main",
            git_dirty=False,
            timestamp=BASE_TS,
            duration_s=1.0,
            exit_code=0,
            status="completed",
        ),
        shared_cat,
    )
    db = _legacy_db(shared_cat)
    try:
        campaign = create_campaign(db, "c1", "proj1", "exploration", catalog_dir=shared_cat)
        add_run_to_campaign(db, campaign.id, "run-p1", catalog_dir=shared_cat)
    finally:
        db.close()
    append_blast_radius_record(
        BlastRadiusRecord(entity_type="run", entity_id="run-p1", to_state="affected"),
        shared_cat,
    )
    CatalogAnchorStore(shared_cat).insert(
        AnchorRecord(
            path="figs/0.svg",
            sha256="a" * 64,
            kind="figure",
            label="fig0",
            campaign_id=campaign.id,
            anchored_at=BASE_TS.isoformat(),
        )
    )

    # proj2: two runs + a run edge between them.
    write_run(
        Run(
            id="run-p2a",
            project_slug="proj2",
            command="b.py",
            argv=["python", "b.py"],
            git_hash="2" * 40,
            git_branch="main",
            git_dirty=False,
            timestamp=BASE_TS,
            duration_s=1.0,
            exit_code=0,
            status="completed",
        ),
        shared_cat,
    )
    write_run(
        Run(
            id="run-p2b",
            project_slug="proj2",
            command="c.py",
            argv=["python", "c.py"],
            git_hash="3" * 40,
            git_branch="main",
            git_dirty=False,
            timestamp=BASE_TS,
            duration_s=1.0,
            exit_code=0,
            status="completed",
        ),
        shared_cat,
    )
    db = _legacy_db(shared_cat)
    try:
        add_run_edge(db, "run-p2b", "run-p2a", catalog_dir=shared_cat)
    finally:
        db.close()

    # proj3: a run + a trust_ledger record keyed on it.
    write_run(
        Run(
            id="run-p3",
            project_slug="proj3",
            command="d.py",
            argv=["python", "d.py"],
            git_hash="4" * 40,
            git_branch="main",
            git_dirty=False,
            timestamp=BASE_TS,
            duration_s=1.0,
            exit_code=0,
            status="completed",
        ),
        shared_cat,
    )
    append_trust_ledger_record(
        TrustLedgerRecord(
            content_hash="c" * 64,
            from_state="candidate",
            to_state="promoted",
            run_id="run-p3",
        ),
        shared_cat,
    )
    compact(shared_cat)

    staging_root = _attempt_staging_root("test-multi-root")
    report, unresolved = _import_candidates(shared_cat, staging_root=staging_root)
    assert report.locked == []
    assert unresolved == 0, "every entity here has a resolvable parent"

    def _events_under(slug: str) -> list[dict]:
        d = staging_root / "project" / root_id(roots[slug])
        events: list[dict] = []
        if d.is_dir():
            for f in sorted(d.glob("*.jsonl")):
                for line in f.read_text().splitlines():
                    if line.strip():
                        events.append(json.loads(line))
        return events

    proj1_events = _events_under("proj1")
    proj2_events = _events_under("proj2")
    proj3_events = _events_under("proj3")

    def _has(events: list[dict], kind: str, entity: list[str]) -> bool:
        return any(e["kind"] == kind and e["entity"] == entity for e in events)

    # proj1: run, campaign, campaign_run (routed via the campaign), blast_radius
    # (routed via the run), and the anchor (routed via the campaign).
    assert _has(proj1_events, "run.imported", ["run-p1"])
    assert _has(proj1_events, "campaign.imported", [campaign.id])
    assert _has(proj1_events, "campaign_run.imported", [campaign.id, "run-p1"])
    assert any(e["kind"] == "blast_radius.imported" for e in proj1_events)
    assert any(e["kind"] == "anchor.imported" for e in proj1_events)

    # proj2: both runs, and the edge (routed via the child run's project).
    assert _has(proj2_events, "run.imported", ["run-p2a"])
    assert _has(proj2_events, "run.imported", ["run-p2b"])
    assert _has(proj2_events, "edge.imported", ["run-p2b", "run-p2a", "run"])

    # proj3: the run, and the trust_ledger record (routed via that run).
    assert _has(proj3_events, "run.imported", ["run-p3"])
    assert any(e["kind"] == "trust_ledger.imported" for e in proj3_events)

    # None of proj1/proj2/proj3's own entities leaked into unaffiliated/.
    unaffiliated_dir = staging_root / "unaffiliated"
    if unaffiliated_dir.is_dir():
        unaffiliated_events = []
        for f in sorted(unaffiliated_dir.glob("*.jsonl")):
            for line in f.read_text().splitlines():
                if line.strip():
                    unaffiliated_events.append(json.loads(line))
        run_ids_seen = {
            tuple(e["entity"])
            for e in unaffiliated_events
            if e["kind"] in ("run.imported", "campaign.imported", "edge.imported")
        }
        assert ("run-p1",) not in run_ids_seen
        assert ("run-p2a",) not in run_ids_seen
        assert ("run-p2b",) not in run_ids_seen
        assert ("run-p3",) not in run_ids_seen
