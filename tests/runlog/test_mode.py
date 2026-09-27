"""Mode section: the flag, cut-over marker, and BTH_LOG_MODE honouring rules."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bathos.runlog.mode import (
    LogModeRefusedError,
    cutover_marker_path,
    is_log_mode,
    writers_lock,
    writers_lock_path,
)


def test_flag_off_by_default(tmp_path: Path):
    catalog_dir = tmp_path / "catalog"
    assert not is_log_mode(catalog_dir)


def test_flag_on_once_marker_written(tmp_path: Path):
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir(parents=True)
    marker = cutover_marker_path(catalog_dir)
    marker.write_text(json.dumps({"at": "2026-01-01T00:00:00Z", "bathos": "x", "attempt": "1"}))
    assert is_log_mode(catalog_dir)


def test_env_flag_refused_outside_honored_context(tmp_path: Path, monkeypatch):
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("BTH_LOG_MODE", "1")
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(LogModeRefusedError):
        is_log_mode(catalog_dir)


def test_env_flag_honored_under_slurm(tmp_path: Path, monkeypatch):
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("BTH_LOG_MODE", "1")
    monkeypatch.setenv("SLURM_JOB_ID", "12345")
    assert is_log_mode(catalog_dir)


def test_env_flag_honored_under_test_with_nondefault_catalog_dir(tmp_path: Path, monkeypatch):
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("BTH_LOG_MODE", "1")
    # PYTEST_CURRENT_TEST is already set by pytest itself; just needs a
    # non-default BTH_CATALOG_DIR alongside it.
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
    assert os.environ.get("PYTEST_CURRENT_TEST")
    assert is_log_mode(catalog_dir)


def test_writers_lock_shared_does_not_block_shared(tmp_path: Path):
    catalog_dir = tmp_path / "catalog"
    with writers_lock(catalog_dir, exclusive=False), writers_lock(catalog_dir, exclusive=False):
        pass  # two shared holders coexist -- must not deadlock


def test_writers_lock_creates_lock_file(tmp_path: Path):
    catalog_dir = tmp_path / "catalog"
    with writers_lock(catalog_dir):
        pass
    assert writers_lock_path(catalog_dir).exists()


def test_writers_lock_skipped_under_slurm(tmp_path: Path, monkeypatch):
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("SLURM_JOB_ID", "999")
    with writers_lock(catalog_dir, exclusive=True):
        pass
    # No lock file needed/created when SLURM_JOB_ID is set -- SLURM jobs skip
    # the local lock entirely (their catalog is remote).
    assert not writers_lock_path(catalog_dir).exists()
