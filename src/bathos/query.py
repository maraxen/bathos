from __future__ import annotations
from datetime import datetime
from pathlib import Path
import duckdb

from bathos.catalog import read_runs
from bathos.schema import Run


def list_runs(
    catalog_dir: Path,
    project: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[Run]:
    """List runs from catalog, with optional filtering and limit."""
    runs = read_runs(catalog_dir)
    if project:
        runs = [r for r in runs if r.project_slug == project]
    if status:
        runs = [r for r in runs if r.status == status]
    return runs[:limit]


def get_run(run_id: str, catalog_dir: Path) -> Run | None:
    """Get a single run by ID, or None if not found."""
    runs = read_runs(catalog_dir)
    for r in runs:
        if r.id == run_id:
            return r
    return None


def find_runs(
    catalog_dir: Path,
    since: datetime | None = None,
    project: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
) -> list[Run]:
    """Find runs with multiple filter criteria."""
    runs = read_runs(catalog_dir)
    if since:
        runs = [r for r in runs if r.timestamp >= since]
    if project:
        runs = [r for r in runs if r.project_slug == project]
    if status:
        runs = [r for r in runs if r.status == status]
    if tags:
        runs = [r for r in runs if any(t in r.tags for t in tags)]
    return runs


def run_sql(sql: str) -> list[tuple]:
    """Execute a raw DuckDB SQL query and return rows as list of tuples."""
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    result = con.execute(sql).fetchall()
    return result
