"""run_sql error mapping (debt #2812).

Only a genuinely missing `runs` table may be reported as "No warm catalog".
Lock, I/O, syntax and other SQL errors must propagate with their original text.
"""

from pathlib import Path

import duckdb
import pytest

from bathos import query
from bathos.query import run_sql

LEGACY_MSG = "No warm catalog. Run `bth compact` first."


class _LockedConnection:
    """Stands in for a read connection whose file is held by another process."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def execute(self, sql: str):
        if sql.startswith("SET"):
            return self
        raise self._exc

    def close(self) -> None:
        pass


def test_lock_error_is_not_reported_as_no_warm_catalog(tmp_path: Path, monkeypatch):
    """(a) A DuckDB lock error on a catalog with no bathos.db keeps its own text."""
    lock_msg = (
        'IO Error: Could not set lock on file "/x/bathos.db": '
        "Conflicting lock is held in /usr/bin/duckdb"
    )
    locked = duckdb.IOException(lock_msg)
    monkeypatch.setattr(
        query, "connect_read", lambda *_a, **_k: _LockedConnection(locked)
    )

    with pytest.raises(duckdb.IOException) as info:
        run_sql("SELECT * FROM runs", tmp_path)

    assert info.value is locked
    assert "Could not set lock" in str(info.value)
    assert "No warm catalog" not in str(info.value)


def test_syntax_error_is_not_reported_as_no_warm_catalog(tmp_path: Path):
    """A bad query against `runs` surfaces DuckDB's own syntax error."""
    with pytest.raises(duckdb.ParserException) as info:
        run_sql("SELECT * FROM runs WHERE", tmp_path)

    assert "syntax error" in str(info.value)
    assert "No warm catalog" not in str(info.value)


def test_missing_runs_table_gives_legacy_message(tmp_path: Path, monkeypatch):
    """(b) Legacy mode, no bathos.db: the missing table keeps the old message."""
    monkeypatch.setattr("bathos.runlog.mode.is_log_mode", lambda *_a, **_k: False)
    catalog = tmp_path / "catalog"

    with pytest.raises(RuntimeError) as info:
        run_sql("SELECT * FROM runs", catalog)

    assert str(info.value) == LEGACY_MSG


def test_log_mode_missing_runs_table_does_not_mention_compact(
    tmp_path: Path, monkeypatch
):
    """(c) Log mode: the missing-table hint must not tell the user to run compact."""
    monkeypatch.setattr("bathos.runlog.mode.is_log_mode", lambda *_a, **_k: True)
    # An anonymous connection has no `runs` table, reproducing the missing-table error.
    monkeypatch.setattr(
        query, "connect_read", lambda *_a, **_k: duckdb.connect("")
    )

    with pytest.raises(RuntimeError) as info:
        run_sql("SELECT * FROM runs", tmp_path)

    msg = str(info.value)
    assert "bth compact" not in msg
    assert "run_ingest()" in msg
    assert "log mode" in msg
