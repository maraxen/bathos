"""Mode section: the flag, cut-over marker, and BTH_LOG_MODE honouring rules."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from bathos.runlog.mode import (
    LogModeRefusedError,
    _format_duration,
    cutover_marker_path,
    is_log_mode,
    lock_holders,
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


# --- Lock-wait reporting (debt #2012): a blocked command must say so ---

_LOCK_HOLDER = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX if sys.argv[2] == "ex" else fcntl.LOCK_SH)
open(sys.argv[3], "w").write("held")
sys.stdin.read()          # exits on EOF, so it cannot outlive the test
"""


@contextmanager
def _lock_held_by_subprocess(catalog_dir: Path, mode: str, tmp_path: Path):
    lock_path = writers_lock_path(catalog_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    ready = tmp_path / f"ready.{mode}"
    proc = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(lock_path), mode, str(ready)],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            if proc.poll() is not None:
                raise AssertionError(f"holder died: {proc.stderr.read()}")
            if time.monotonic() > deadline:
                raise AssertionError("holder never took the lock")
            time.sleep(0.02)
        yield proc
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def test_uncontended_lock_reports_nothing(tmp_path: Path, capsys):
    """Negative control: the common path must stay silent."""
    catalog_dir = tmp_path / "catalog"
    with writers_lock(catalog_dir, exclusive=True):
        pass
    assert capsys.readouterr().err == ""


def test_shared_lock_behind_shared_reports_nothing(tmp_path: Path, capsys):
    """Shared holders do not exclude each other, so there is nothing to report."""
    catalog_dir = tmp_path / "catalog"
    with _lock_held_by_subprocess(catalog_dir, "sh", tmp_path):
        capsys.readouterr()
        with writers_lock(catalog_dir, exclusive=False):
            pass
        assert capsys.readouterr().err == ""


def test_blocked_exclusive_lock_reports_the_wait(tmp_path: Path, capsys):
    """An exclusive waiter behind a shared holder must announce the wait.

    This is the `bth migrate --to-log` behind a long `bth run` case: before
    this, the command blocked in `fcntl.flock` with no output at all and was
    indistinguishable from a hang.
    """
    catalog_dir = tmp_path / "catalog"
    with _lock_held_by_subprocess(catalog_dir, "sh", tmp_path) as holder:
        capsys.readouterr()

        done = threading.Event()
        errors: list[BaseException] = []

        def take_lock():
            try:
                with writers_lock(catalog_dir, exclusive=True):
                    done.set()
            except BaseException as e:  # pragma: no cover - surfaced below
                errors.append(e)
                done.set()

        t = threading.Thread(target=take_lock, daemon=True)
        t.start()
        # Give the waiter time to report and block, then confirm it is still
        # waiting -- it must not be granted while a shared holder is active.
        time.sleep(1.5)
        assert not done.is_set(), "exclusive lock was granted while a shared holder was active"

    t.join(timeout=30)
    assert not errors, errors
    assert done.is_set(), "exclusive lock never acquired after the holder exited"

    err = capsys.readouterr().err
    assert "waiting for the catalog writers lock (exclusive)" in err
    assert str(writers_lock_path(catalog_dir)) in err
    assert "acquired the writers lock after" in err
    if Path("/proc/locks").exists():
        assert f"held by pid {holder.pid}" in err, err
    else:  # pragma: no cover - non-Linux
        assert "held by another bth process" in err


def test_lock_holders_is_never_fatal(tmp_path: Path):
    """A path that cannot be inspected yields no holders rather than raising."""
    assert lock_holders(tmp_path / "does-not-exist.lock") == []


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0.5, "0.5s"), (59.4, "59.4s"), (75, "1m15s"), (3725, "1h02m")],
)
def test_format_duration(seconds: float, expected: str):
    assert _format_duration(seconds) == expected
