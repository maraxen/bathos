"""Regression tests for `bathos.index.connect_legacy` (AC-18 review finding 1,
post-4727b872): it must tell apart a *locked* legacy database from a
*corrupt* one -- DuckDB raises the SAME exception type (`duckdb.IOException`)
for both, with different messages, and AC-24 needs the two as distinct
findings (`legacy_db_locked` vs `corrupt_legacy_source`).

At 4727b872, `connect_legacy` mapped every `duckdb.IOException` to
`legacy_db_locked` -- a garbage/corrupt file was misreported as "locked by
another process" instead of "not a valid database". `test_corrupt_file_...`
below fails on 4727b872 (it asserts `status == "corrupt_legacy_source"`,
which that revision never produces) and passes after the fix.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from bathos.index import connect_legacy


def test_missing_file_raises_file_not_found_error(tmp_path: Path):
    missing = tmp_path / "does_not_exist.db"
    with pytest.raises(FileNotFoundError):
        connect_legacy(missing)


def test_locked_file_gives_legacy_db_locked_result(tmp_path: Path):
    """A real cross-process lock (not an in-process one -- DuckDB's
    in-process reuse behavior differs) must be reported as
    `legacy_db_locked`, not raised and not misclassified as corrupt."""
    db_path = tmp_path / "locked.db"

    holder_code = f"""
import duckdb, time
con = duckdb.connect({str(db_path)!r})
con.execute("CREATE TABLE t (x INT)")
time.sleep(10)
"""
    proc = subprocess.Popen([sys.executable, "-c", holder_code])
    try:
        # Give the holder time to actually open and lock the file.
        deadline = time.monotonic() + 5
        result = None
        last_exc = None
        while time.monotonic() < deadline:
            if db_path.exists():
                try:
                    result = connect_legacy(db_path)
                    break
                except Exception as e:  # noqa: BLE001 -- retry until the holder has the lock
                    last_exc = e
            time.sleep(0.1)
        assert result is not None, f"never observed a lock; last error: {last_exc}"
        assert isinstance(result, dict), f"expected a structured result, got {result!r}"
        assert result["status"] == "legacy_db_locked"
        assert result["path"] == str(db_path)
        assert "reason" in result
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_corrupt_file_gives_corrupt_legacy_source_result(tmp_path: Path):
    """A file that exists but is not a valid DuckDB database at all must be
    reported as `corrupt_legacy_source`, distinct from `legacy_db_locked`
    (AC-24). Fails on 4727b872, which reported this as `legacy_db_locked`."""
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a duckdb database file" * 20)

    result = connect_legacy(garbage)

    assert isinstance(result, dict), f"expected a structured result, got {result!r}"
    assert result["status"] == "corrupt_legacy_source"
    assert result["path"] == str(garbage)
    assert "reason" in result


def test_corrupt_and_locked_are_distinguishable(tmp_path: Path):
    """The two failure modes never collide: same exception TYPE, different
    `status` values. Guards against a future change re-merging them."""
    garbage = tmp_path / "garbage2.db"
    garbage.write_bytes(b"\x00" * 64)

    result = connect_legacy(garbage)
    assert result["status"] != "legacy_db_locked"
    assert result["status"] == "corrupt_legacy_source"
