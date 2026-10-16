"""Ingest triggers in log mode (runlog spec "When ingest runs"; cutover spec B4).

After cutover, nothing folded new log events into `index.db`, so a run that
finished after the last manual ingest was invisible to every reader. Two
triggers close that: the end of `bth run`, and `bth compact`.

Each visibility test has a negative control (`BTH_NO_INGEST=1`): without the
trigger the same run must stay invisible, otherwise the test would pass for a
reason other than the trigger.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bathos.compact import compact
from bathos.query import run_sql
from bathos.runlog.ingest import (
    IngestLockTimeout,
    ingest_after_write,
    ingest_lock,
)
from bathos.runner import run_script

from .test_ac18_flag_on_reads import enable_log_mode, setup_project


@pytest.fixture
def log_mode_project(tmp_path: Path) -> tuple[Path, Path]:
    repo, _pid = setup_project(tmp_path)
    catalog = tmp_path / "catalog"
    enable_log_mode(catalog)
    script = repo / "scripts" / "analysis" / "hello.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('hi')\n")
    return repo, catalog


def _run(repo: Path, catalog: Path) -> int:
    return run_script(
        argv=[sys.executable, str(repo / "scripts" / "analysis" / "hello.py")],
        project_slug="repo",
        catalog_dir=catalog,
        output_paths=[],
        tags=[],
        cwd=repo,
    )


def _run_ids(catalog: Path) -> list[str]:
    try:
        rows = run_sql("SELECT id FROM runs", catalog_dir=catalog)
    except Exception:
        return []
    return [r["id"] if isinstance(r, dict) else r[0] for r in rows]


def test_run_end_ingests_so_run_is_visible_without_compact(log_mode_project):
    repo, catalog = log_mode_project
    assert _run(repo, catalog) == 0
    assert len(_run_ids(catalog)) == 1
    assert not (catalog / "bathos.db").exists()


def test_negative_control_no_ingest_leaves_run_invisible(log_mode_project, monkeypatch):
    repo, catalog = log_mode_project
    monkeypatch.setenv("BTH_NO_INGEST", "1")
    assert _run(repo, catalog) == 0
    assert _run_ids(catalog) == []


def test_second_run_is_visible_too(log_mode_project):
    """The index must keep up run after run, not only on first build."""
    repo, catalog = log_mode_project
    assert _run(repo, catalog) == 0
    assert _run(repo, catalog) == 0
    assert len(set(_run_ids(catalog))) == 2


def test_compact_in_log_mode_ingests_and_never_creates_bathos_db(log_mode_project, monkeypatch):
    repo, catalog = log_mode_project
    monkeypatch.setenv("BTH_NO_INGEST", "1")
    assert _run(repo, catalog) == 0
    assert _run_ids(catalog) == []
    monkeypatch.delenv("BTH_NO_INGEST")

    result = compact(catalog)

    assert result.log_mode is True
    assert result.ingest_ran is True
    assert result.ingested > 0
    assert len(_run_ids(catalog)) == 1
    assert not (catalog / "bathos.db").exists()


def test_compact_in_log_mode_reports_skipped_ingest(log_mode_project, monkeypatch):
    _repo, catalog = log_mode_project
    monkeypatch.setenv("BTH_NO_INGEST", "1")
    result = compact(catalog)
    assert result.log_mode is True
    assert result.ingest_ran is False
    assert not (catalog / "bathos.db").exists()


def test_compact_force_rebuild_refused_in_log_mode(log_mode_project):
    _repo, catalog = log_mode_project
    with pytest.raises(ValueError, match="log mode"):
        compact(catalog, force_rebuild=True)
    assert not (catalog / "bathos.db").exists()


def test_legacy_mode_run_does_not_create_index_db(tmp_path: Path):
    """Flag off: the end-of-run hook must not run (spec: index.db is never
    created before the switch)."""
    repo, _pid = setup_project(tmp_path)
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    script = repo / "scripts" / "analysis" / "hello.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('hi')\n")
    assert _run(repo, catalog) == 0
    assert not (catalog / "index.db").exists()


def test_ingest_lock_timeout_raises(tmp_path: Path):
    with ingest_lock(tmp_path):
        with pytest.raises(IngestLockTimeout):
            with ingest_lock(tmp_path, timeout_s=0.2):
                pass


def test_ingest_after_write_never_raises_when_lock_is_held(log_mode_project):
    _repo, catalog = log_mode_project
    with ingest_lock(catalog):
        assert ingest_after_write(catalog, lock_timeout_s=0.2) is None
