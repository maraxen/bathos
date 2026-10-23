"""init_index_schema must add v17 columns to an index.db created before they existed.

RUNS_DDL is `CREATE TABLE IF NOT EXISTS`, which never alters an existing table, so an older
index.db would otherwise lack epistemic_class / tentative_outcome / agent_session and fail
ingest with a binder error.
"""

from pathlib import Path

import duckdb

from bathos.index import RUNS_DDL, init_index_schema

V17_COLUMNS = ("epistemic_class", "tentative_outcome", "agent_session")


def _old_runs_ddl() -> str:
    """The RUNS_DDL as it stood before the v17 columns were added."""
    kept = [
        line
        for line in RUNS_DDL.splitlines()
        if not line.strip().startswith(tuple(f"{c} " for c in V17_COLUMNS))
    ]
    return "\n".join(kept)


def _columns(con: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [table]
    ).fetchall()
    return {r[0] for r in rows}


def test_init_index_schema_adds_missing_v17_columns(tmp_path: Path):
    db = tmp_path / "index.db"
    old = duckdb.connect(str(db))
    old.execute(_old_runs_ddl())
    assert not set(V17_COLUMNS) & _columns(old, "runs")
    old.close()

    con = duckdb.connect(str(db))
    try:
        init_index_schema(con)
        assert set(V17_COLUMNS) <= _columns(con, "runs")
        con.execute(
            "INSERT INTO runs (id, outcome, epistemic_class, tentative_outcome, agent_session) "
            "VALUES ('r1', 'observed', 'exploratory', 'fail', 'sess-1')"
        )
        row = con.execute(
            "SELECT epistemic_class, tentative_outcome, agent_session FROM runs WHERE id = 'r1'"
        ).fetchone()
        assert row == ("exploratory", "fail", "sess-1")
    finally:
        con.close()


def test_init_index_schema_is_idempotent(tmp_path: Path):
    con = duckdb.connect(str(tmp_path / "index.db"))
    try:
        init_index_schema(con)
        init_index_schema(con)
        assert set(V17_COLUMNS) <= _columns(con, "runs")
    finally:
        con.close()


def test_init_index_schema_adds_missing_dependency_provenance(tmp_path: Path):
    """An index.db from before schema v18 gains dependency_provenance on init (#2946)."""
    kept = [
        line
        for line in RUNS_DDL.splitlines()
        if not line.strip().startswith("dependency_provenance ")
    ]
    db = tmp_path / "index.db"
    old = duckdb.connect(str(db))
    old.execute("\n".join(kept))
    assert "dependency_provenance" not in _columns(old, "runs")
    old.close()

    con = duckdb.connect(str(db))
    try:
        init_index_schema(con)
        assert "dependency_provenance" in _columns(con, "runs")
    finally:
        con.close()
