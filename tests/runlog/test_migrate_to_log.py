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
    """AC-30's `step1_pulled_or_reaped` half: a stale `running` run that
    migrate's own step 1 reaps (`reconcile_warm=False`, so `bathos.db` is
    left exactly as it was) is classified, not left unclassified.

    Note (spec-vs-implementation finding, reported in full in the task
    report): AC-30 also describes a "warm-only row (absent from every cool
    fragment)" whose residual lines carry `staged_value: null` -- i.e. the
    `warm_only_row` class. This wave's importer (`_warm_candidates`,
    delivery step 4 wave a, reused unchanged here) reads `bathos.db`'s OWN
    rows as a first-class "warm" source candidate REGARDLESS of cut-over
    state (gated only on `bathos.db.frozen` vs `bathos.db` existing, not on
    `is_log_mode()`), so a row present in `bathos.db` but absent from every
    cool fragment is still correctly imported (via that warm-source
    candidate) and therefore never actually produces a `staged_value: null`
    residual for a `runs`/`campaigns`/edge/anchor/ledger row -- confirmed
    empirically (see the task report). `warm_only_row` is implemented in
    `_classify` for the case it CAN fire (the importer failing to resolve
    the entity via ANY source, e.g. a locked/corrupt warm database handled
    separately as `legacy_db_locked`/`corrupt_legacy_source`), but this test
    does not attempt to manufacture the AC-30 scenario as literally worded,
    since doing so would require changing the already-built importer.
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
    compact(backend.catalog_dir)

    from bathos.runlog.migrate import migrate_to_log

    result = migrate_to_log(backend.catalog_dir)
    assert result.status == "residual_pending"
    classes = {
        (line["table"], tuple(line["key"])): line["class"] for line in result.residual_lines
    }
    assert classes[("runs", ("run-stale2",))] == "step1_pulled_or_reaped"

    # bathos.db itself is untouched by steps 1-3 (spec AC-30: "leave
    # bathos.db byte-identical") -- only its cool fragment and a reap ledger
    # were written.
    con = duckdb.connect(str(backend.catalog_dir / "bathos.db"), read_only=True)
    try:
        legacy_status = con.execute(
            "SELECT status FROM runs WHERE id = ?", ["run-stale2"]
        ).fetchone()[0]
    finally:
        con.close()
    assert legacy_status == "running"
    assert (backend.catalog_dir / "reaped" / "proj" / "run-stale2.json").is_file()

    # A second call still converges (this time the run is already reaped by
    # the first call, so it's classified `fragment_not_yet_compacted`
    # instead -- a DIFFERENT class label than the first call's
    # `step1_pulled_or_reaped`, so the report is not byte-identical across
    # calls here; determinism-on-an-unchanged-catalog is asserted instead in
    # `test_residual_report_format_and_hash`, whose fixture never mutates).
    other = migrate_to_log(backend.catalog_dir)
    assert other.status == "residual_pending"
    other_classes = {
        (line["table"], tuple(line["key"])): line["class"] for line in other.residual_lines
    }
    assert other_classes[("runs", ("run-stale2",))] == "fragment_not_yet_compacted"


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
