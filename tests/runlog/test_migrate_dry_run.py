"""`bth migrate --to-log --dry-run` (sprint 260927 item 1): a genuine no-write
preview of Migration steps 1-3's residual report.

Built on the same fixtures as `test_migrate_to_log.py` (`_simple_backend`,
`_converge`), with the suite-wide `isolated_home` autouse fixture
(`tests/runlog/conftest.py`) keeping HOME under `tmp_path` for every test in
this module -- never the real `~/.bth/`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bathos.catalog import write_run
from bathos.compact import compact
from bathos.schema import Run

from .test_migrate_to_log import BASE_TS, _converge, _simple_backend


@pytest.fixture(autouse=True)
def _squeue_available_and_empty(monkeypatch):
    """Same rationale as `test_migrate_to_log.py`'s own fixture of the same
    name: this sandbox has no real `squeue`, and `my_squeue_job_ids()` fails
    CLOSED on any query failure."""
    monkeypatch.setattr("bathos.runlog.migrate.my_squeue_job_ids", lambda: [])


# --------------------------------------------------------------------------
# Fingerprint helper (negative control)
# --------------------------------------------------------------------------


def _fingerprint(root: Path) -> dict[str, tuple[int, int]]:
    """`{relative_path: (size, mtime_ns)}` for every regular file under
    `root`, recursively -- a new/removed/resized/retouched file anywhere in
    the tree changes this dict's contents even at coarse mtime resolution
    (a changed path always shows up regardless of clock granularity)."""
    if not root.exists():
        return {}
    out: dict[str, tuple[int, int]] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


def _force_writers_lock_file(catalog_dir: Path) -> None:
    """Pre-create `catalog_dir/writers.lock` (an empty placeholder,
    unconditionally opened -- never written to -- by every `writers_lock()`
    acquisition, dry run included) so a fingerprint taken before a dry run
    isn't tripped by this lock file's own first-time creation, which is not
    a data write (see the task brief: "acceptable only if it creates/
    modifies no data files")."""
    catalog_dir.mkdir(parents=True, exist_ok=True)
    (catalog_dir / "writers.lock").touch()


# --------------------------------------------------------------------------
# (a) Positive: fixture catalog with legacy runs -> status dry_run with
#     residual lines/sha, generated from a genuine, already-on-disk
#     legacy/staged divergence -- an ORDINARY reap done outside migration
#     entirely (apply=True, warm-tier reconciliation disabled), matching
#     `_classify`'s own documented scope for `fragment_not_yet_compacted`
#     ("a run reaped by an ordinary (non-migration) reap_runs() call ... is
#     exactly as not yet compacted as one reaped by THIS attempt's step
#     1"). This is deliberately NOT a stale `running` row left for
#     migration's own step 1 to reap: `--dry-run`'s `reap_runs(apply=
#     False)` never actually rewrites the cool fragment, so a residual that
#     depends on step 1's OWN write (`step1_pulled_or_reaped`) would not
#     appear the way it does in a real run -- exactly the documented dry-
#     run/real divergence, not a bug in this fixture's choice of scenario.
# --------------------------------------------------------------------------


def test_dry_run_positive_reports_residual_lines_and_sha(tmp_path: Path):
    from bathos.reap import reap_runs
    from bathos.runlog.migrate import migrate_to_log

    backend = _simple_backend(tmp_path)
    stale = Run(
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
    write_run(stale, backend.catalog_dir)
    compact(backend.catalog_dir)

    # Reap it for real, right now, outside any migrate call -- the cool
    # fragment is rewritten to `abandoned`; `bathos.db` is left exactly as
    # it was (`reconcile_warm=False`), so the two sides are already, and
    # statically, out of sync before `migrate_to_log` ever runs.
    reap_runs(backend.catalog_dir, apply=True, reconcile_warm=False)

    result = migrate_to_log(backend.catalog_dir, dry_run=True)

    assert result.status == "dry_run"
    assert result.report_sha256
    assert result.residual_lines, "expected at least one residual line"
    assert result.unclassified == []
    status_lines = [
        line
        for line in result.residual_lines
        if line["table"] == "runs" and line["column"] == "status" and line["key"] == ["run-stale"]
    ]
    assert status_lines, "expected a status residual for run-stale"
    for line in status_lines:
        # NOT step1_pulled_or_reaped: dry run's OWN reap (apply=False) never
        # saw this run as a "running" candidate -- it was already abandoned
        # by the out-of-band reap above, before dry run's pass even started.
        assert line["class"] == "fragment_not_yet_compacted"
        assert line["legacy_value"] == ["running"]
        assert line["staged_value"] == ["abandoned"]

    # The returned sha256 matches the residual lines it was computed from
    # (same canonical-report hashing `_report_bytes`/`_report_sha256` use).
    import hashlib

    body = "\n".join(
        sorted(
            json.dumps(line, sort_keys=True, separators=(",", ":"))
            for line in result.residual_lines
        )
    )
    expected_sha = hashlib.sha256((body + "\n").encode("utf-8")).hexdigest()
    assert result.report_sha256 == expected_sha


# --------------------------------------------------------------------------
# (b) NEGATIVE control: dry run changes nothing, anywhere; a real run does.
# --------------------------------------------------------------------------


def test_dry_run_writes_nothing_but_a_real_run_does(tmp_path: Path, isolated_home: Path):
    from bathos.runlog.migrate import migrate_to_log

    backend = _simple_backend(tmp_path)
    _force_writers_lock_file(backend.catalog_dir)

    home_root = isolated_home
    catalog_before = _fingerprint(backend.catalog_dir)
    home_before = _fingerprint(home_root)
    workspace_before = _fingerprint(backend.workspace)

    result = migrate_to_log(backend.catalog_dir, dry_run=True)
    assert result.status == "dry_run"

    catalog_after = _fingerprint(backend.catalog_dir)
    home_after = _fingerprint(home_root)
    workspace_after = _fingerprint(backend.workspace)

    assert catalog_after == catalog_before, "dry run must not touch the catalog dir"
    assert home_after == home_before, "dry run must not touch ~/.bth"
    assert workspace_after == workspace_before, "dry run must not touch the registered root"

    # Prove the check can actually fire: a REAL (non-dry) run of the exact
    # same fixture DOES change the catalog dir (residual_pending writes a
    # report; converging to `switched` writes index.db/cutover.json/etc).
    final = _converge(backend.catalog_dir)
    assert final.status == "switched"
    catalog_after_real = _fingerprint(backend.catalog_dir)
    assert catalog_after_real != catalog_before, (
        "a real (non-dry) run must change the catalog dir -- if this fails, the "
        "fingerprint helper itself cannot detect a real write, and the equality "
        "assertions above are not meaningful"
    )


# --------------------------------------------------------------------------
# (c) Remote seams are never called in dry run.
# --------------------------------------------------------------------------


def test_dry_run_never_calls_remote_seams(tmp_path: Path, monkeypatch):
    from bathos.runlog import migrate as migrate_mod

    def _boom(*_args, **_kwargs):
        raise AssertionError("a remote seam was called during --dry-run")

    monkeypatch.setattr(migrate_mod, "rsync_full_mirror", _boom)
    monkeypatch.setattr("bathos.sync.sync_catalog", _boom)
    monkeypatch.setattr("bathos.cluster.pull_path", _boom)

    backend = _simple_backend(tmp_path)
    from bathos.runlog.migrate import migrate_to_log

    result = migrate_to_log(backend.catalog_dir, dry_run=True)
    assert result.status == "dry_run"


# --------------------------------------------------------------------------
# (d) reap called with apply=False.
# --------------------------------------------------------------------------


def test_dry_run_reap_called_with_apply_false(tmp_path: Path, monkeypatch):
    import bathos.reap as reap_mod

    real_reap_runs = reap_mod.reap_runs
    calls: list[dict] = []

    def _spy(catalog_dir, *args, **kwargs):
        calls.append(kwargs)
        return real_reap_runs(catalog_dir, *args, **kwargs)

    monkeypatch.setattr(reap_mod, "reap_runs", _spy)

    backend = _simple_backend(tmp_path)
    from bathos.runlog.migrate import migrate_to_log

    result = migrate_to_log(backend.catalog_dir, dry_run=True)

    assert result.status == "dry_run"
    assert calls, "reap_runs was never called during dry run"
    assert calls[0].get("apply") is False
    assert calls[0].get("reconcile_warm") is False


# --------------------------------------------------------------------------
# (e) --accept-residual + --dry-run rejected.
# --------------------------------------------------------------------------


def test_dry_run_with_accept_residual_rejected(tmp_path: Path):
    from bathos.runlog.migrate import migrate_to_log

    backend = _simple_backend(tmp_path)
    with pytest.raises(ValueError):
        migrate_to_log(backend.catalog_dir, dry_run=True, accept_residual="deadbeef" * 8)


# --------------------------------------------------------------------------
# (f) CLI wiring.
# --------------------------------------------------------------------------


def test_cli_migrate_to_log_dry_run_prints_status_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from bathos.cli_cyclopts import app
    from tests._cyclopts_runner import CyclopticRunner

    backend = _simple_backend(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(backend.catalog_dir))

    runner = CyclopticRunner()
    result = runner.invoke(app, ["migrate", "--to-log", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "status: dry_run" in result.output
    assert "residual lines:" in result.output


def test_cli_migrate_to_log_dry_run_with_accept_residual_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from bathos.cli_cyclopts import app
    from tests._cyclopts_runner import CyclopticRunner

    backend = _simple_backend(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(backend.catalog_dir))

    runner = CyclopticRunner()
    result = runner.invoke(
        app,
        ["migrate", "--to-log", "--dry-run", "--accept-residual", "deadbeef" * 8],
    )

    assert result.exit_code == 1, result.output


# --------------------------------------------------------------------------
# Bonus: marker-present dry run reads the persisted report, writes nothing.
# --------------------------------------------------------------------------


def test_dry_run_with_marker_present_reads_persisted_report_only(tmp_path: Path):
    """Once a prior attempt has reached the cut-over commit point (step
    4(b), marker written) but step 4(c)-(e) have not finished (staging still
    present -- an interrupted switch), `--dry-run` never recomputes or
    rewrites anything: it reads back that attempt's own already-written
    `residual_report.jsonl` from staging."""
    import hashlib

    from bathos.runlog.migrate import ResidualLine, _attempt_staging_root, _write_residual_report
    from bathos.runlog.migrate import migrate_to_log as _migrate_to_log
    from bathos.runlog.mode import cutover_marker_path

    backend = _simple_backend(tmp_path)

    attempt = "manual-interrupted-attempt"
    lines = [
        ResidualLine(
            table="runs",
            key=["run-x"],
            column="status",
            cls="warm_only_row",
            legacy_value=["completed"],
            staged_value=None,
        )
    ]
    report_path = _write_residual_report(attempt, lines)
    staging_root = _attempt_staging_root(attempt)

    marker_path = cutover_marker_path(backend.catalog_dir)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(
            {"at": "2020-01-01T00:00:00Z", "bathos": "test", "attempt": attempt, "segments": []}
        )
    )

    catalog_before = _fingerprint(backend.catalog_dir)
    staging_before = _fingerprint(staging_root)

    dry = _migrate_to_log(backend.catalog_dir, dry_run=True)

    assert dry.status == "dry_run"
    assert dry.attempt == attempt
    assert dry.report_path == str(report_path)
    assert dry.report_sha256 == hashlib.sha256(report_path.read_text().encode("utf-8")).hexdigest()
    assert dry.residual_lines == [ln.to_dict() for ln in lines]
    assert _fingerprint(backend.catalog_dir) == catalog_before
    assert _fingerprint(staging_root) == staging_before
