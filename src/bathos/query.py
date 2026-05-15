from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import Literal
import duckdb

from bathos.catalog import read_runs
from bathos.schema import Run


def _resolve_backend(catalog_dir: Path) -> Literal["cool", "warm"]:
    """Determine which backend to use based on catalog state.

    Returns 'warm' if catalog_dir/bathos.db exists, else 'cool'.
    """
    if (catalog_dir / "bathos.db").exists():
        return "warm"
    return "cool"


def _cool_list_runs(
    catalog_dir: Path,
    project: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[Run]:
    """List runs using cool tier (PyArrow Parquet files)."""
    runs = read_runs(catalog_dir)
    if project:
        runs = [r for r in runs if r.project_slug == project]
    if status:
        runs = [r for r in runs if r.status == status]
    return runs[:limit]


def list_runs(
    catalog_dir: Path,
    project: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[Run]:
    """List runs from catalog, with optional filtering and limit.

    Dispatches to cool or warm backend based on catalog state.
    """
    backend = _resolve_backend(catalog_dir)
    if backend == "warm":
        return _warm_list_runs(catalog_dir, project=project, status=status, limit=limit)
    return _cool_list_runs(catalog_dir, project=project, status=status, limit=limit)


def _warm_list_runs(
    catalog_dir: Path,
    project: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[Run]:
    """List runs using warm tier (DuckDB).

    Not yet implemented; placeholder for Task A3.
    """
    raise NotImplementedError("Warm tier list_runs coming in Task A3")


def _cool_get_run(run_id: str, catalog_dir: Path) -> Run | None:
    """Get a single run by ID using cool tier (PyArrow)."""
    runs = read_runs(catalog_dir)
    for r in runs:
        if r.id == run_id:
            return r
    return None


def get_run(run_id: str, catalog_dir: Path) -> Run | None:
    """Get a single run by ID, or None if not found.

    Dispatches to cool or warm backend based on catalog state.
    """
    backend = _resolve_backend(catalog_dir)
    if backend == "warm":
        return _warm_get_run(run_id, catalog_dir)
    return _cool_get_run(run_id, catalog_dir)


def _warm_get_run(run_id: str, catalog_dir: Path) -> Run | None:
    """Get a single run by ID using warm tier (DuckDB).

    Not yet implemented; placeholder for Task A3.
    """
    raise NotImplementedError("Warm tier get_run coming in Task A3")


def _cool_find_runs(
    catalog_dir: Path,
    since: datetime | None = None,
    project: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    slurm_job_id: str | None = None,
) -> list[Run]:
    """Find runs using cool tier (PyArrow)."""
    runs = read_runs(catalog_dir)
    if since:
        runs = [r for r in runs if r.timestamp >= since]
    if project:
        runs = [r for r in runs if r.project_slug == project]
    if status:
        runs = [r for r in runs if r.status == status]
    if tags:
        runs = [r for r in runs if any(t in r.tags for t in tags)]
    if slurm_job_id:
        runs = [r for r in runs if r.slurm_job_id == slurm_job_id]
    return runs


def find_runs(
    catalog_dir: Path,
    since: datetime | None = None,
    project: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    slurm_job_id: str | None = None,
) -> list[Run]:
    """Find runs with multiple filter criteria.

    Dispatches to cool or warm backend based on catalog state.
    """
    backend = _resolve_backend(catalog_dir)
    if backend == "warm":
        return _warm_find_runs(catalog_dir, since=since, project=project, status=status, tags=tags, slurm_job_id=slurm_job_id)
    return _cool_find_runs(catalog_dir, since=since, project=project, status=status, tags=tags, slurm_job_id=slurm_job_id)


def _warm_find_runs(
    catalog_dir: Path,
    since: datetime | None = None,
    project: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    slurm_job_id: str | None = None,
) -> list[Run]:
    """Find runs using warm tier (DuckDB).

    Not yet implemented; placeholder for Task A3.
    """
    raise NotImplementedError("Warm tier find_runs coming in Task A3")


def run_sql(sql: str, catalog_dir: Path | None = None) -> list[tuple]:
    """Execute a raw DuckDB SQL query and return rows as list of tuples.

    If catalog_dir is provided, attempts to open warm tier catalog (bathos.db).
    If warm DB doesn't exist and query requires it (e.g., SELECT FROM runs),
    DuckDB will raise an appropriate error.
    Otherwise, connects to DuckDB without a specific catalog (for arbitrary queries).
    """
    if catalog_dir is not None:
        db_path = catalog_dir / "bathos.db"
        if not db_path.exists():
            # Don't require warm DB upfront - let DuckDB error if query needs it
            con = duckdb.connect()
        else:
            con = duckdb.connect(str(db_path))
    else:
        con = duckdb.connect()

    con.execute("SET TimeZone='UTC'")
    try:
        result = con.execute(sql).fetchall()
    except Exception as e:
        # If query tried to access the runs table without warm DB, provide helpful error
        if "runs" in sql.lower() and catalog_dir is not None and not (catalog_dir / "bathos.db").exists():
            raise RuntimeError(
                "No warm catalog. Run `bth compact` first."
            ) from e
        raise

    return result
