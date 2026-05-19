from __future__ import annotations

import fnmatch
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

import duckdb

from bathos.catalog import read_runs
from bathos.schema import Run


def _filter_runs_by_output_file(
    runs: list[Run],
    pattern: str | None = None,
) -> list[Run]:
    """Filter runs by output file glob pattern.

    Checks both cool-tier output_paths and warm-tier metadata output_files.
    Missing output files are ignored (status != 'present' is filtered out).

    Args:
        runs: List of Run objects
        pattern: Glob pattern for output file paths (e.g., "*.json")

    Returns:
        Filtered list of runs matching the pattern, or all runs if pattern is None
    """
    if not pattern:
        return runs

    filtered = []
    for run in runs:
        # First check output_paths (cool tier)
        if run.output_paths:
            matches = any(fnmatch.fnmatch(path, pattern) for path in run.output_paths)
            if matches:
                filtered.append(run)
                continue

        # Then check warm-tier metadata if no match in output_paths
        if run.metadata and run.metadata != "{}":
            try:
                metadata = json.loads(run.metadata)
                if isinstance(metadata, dict) and "output_files" in metadata:
                    matches = any(
                        fnmatch.fnmatch(f.get("path", ""), pattern)
                        for f in metadata.get("output_files", [])
                        if f.get("status") == "present"
                    )
                    if matches:
                        filtered.append(run)
            except (json.JSONDecodeError, TypeError):
                pass

    return filtered


def _row_to_run(row: tuple) -> Run | None:
    """Convert a DuckDB row (from runs table) to a Run object.

    DuckDB returns rows as tuples; we need to map back to Run dataclass.
    Column order must match: id, project_slug, command, argv, git_hash, git_branch,
    git_dirty, timestamp, duration_s, exit_code, status, output_paths, tags,
    schema_version, slurm_job_id, hostname, metadata, outcome, output_metadata
    """
    try:
        (
            id_,
            project_slug,
            command,
            argv,
            git_hash,
            git_branch,
            git_dirty,
            timestamp,
            duration_s,
            exit_code,
            status,
            output_paths,
            tags,
            schema_version,
            slurm_job_id,
            hostname,
            metadata,
            outcome,
            output_metadata,
        ) = row

        return Run(
            id=id_,
            project_slug=project_slug,
            command=command,
            argv=argv if argv else [],
            git_hash=git_hash,
            git_branch=git_branch,
            git_dirty=git_dirty,
            timestamp=timestamp,
            duration_s=duration_s,
            exit_code=exit_code,
            status=status,
            output_paths=output_paths if output_paths else [],
            tags=tags if tags else [],
            schema_version=schema_version,
            slurm_job_id=slurm_job_id if slurm_job_id else "",
            hostname=hostname if hostname else "",
            metadata=metadata if metadata else "{}",
            outcome=outcome if outcome else "",
        )
    except (ValueError, TypeError) as e:
        raise RuntimeError(f"Failed to convert DuckDB row to Run: {e}") from e


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
    """List runs using warm tier (DuckDB)."""
    db_path = catalog_dir / "bathos.db"
    con = duckdb.connect(str(db_path))
    con.execute("SET TimeZone='UTC'")

    # Build query
    query = "SELECT * FROM runs"
    params = []
    conditions = []

    if project:
        conditions.append("project_slug = ?")
        params.append(project)
    if status:
        conditions.append("status = ?")
        params.append(status)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += f" LIMIT {limit}"

    rows = con.execute(query, params).fetchall()

    # Convert rows to Run objects
    runs = []
    for row in rows:
        run = _row_to_run(row)
        if run:
            runs.append(run)

    con.close()
    return runs


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
    """Get a single run by ID using warm tier (DuckDB)."""
    db_path = catalog_dir / "bathos.db"
    con = duckdb.connect(str(db_path))
    con.execute("SET TimeZone='UTC'")

    rows = con.execute("SELECT * FROM runs WHERE id = ?", [run_id]).fetchall()

    if not rows:
        con.close()
        return None

    run = _row_to_run(rows[0])
    con.close()
    return run


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
        return _warm_find_runs(
            catalog_dir,
            since=since,
            project=project,
            status=status,
            tags=tags,
            slurm_job_id=slurm_job_id,
        )
    return _cool_find_runs(
        catalog_dir,
        since=since,
        project=project,
        status=status,
        tags=tags,
        slurm_job_id=slurm_job_id,
    )


def _warm_find_runs(
    catalog_dir: Path,
    since: datetime | None = None,
    project: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    slurm_job_id: str | None = None,
) -> list[Run]:
    """Find runs using warm tier (DuckDB)."""
    db_path = catalog_dir / "bathos.db"
    con = duckdb.connect(str(db_path))
    con.execute("SET TimeZone='UTC'")

    query = "SELECT * FROM runs"
    params = []
    conditions = []

    if since:
        conditions.append("timestamp >= ?")
        params.append(since)
    if project:
        conditions.append("project_slug = ?")
        params.append(project)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if slurm_job_id:
        conditions.append("slurm_job_id = ?")
        params.append(slurm_job_id)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    rows = con.execute(query, params).fetchall()

    # For tags filtering, need to check each run (tags are stored as arrays)
    runs = []
    for row in rows:
        run = _row_to_run(row)
        if run:
            if tags and not any(t in run.tags for t in tags):
                continue
            runs.append(run)

    con.close()
    return runs


def run_sql(sql: str, catalog_dir: Path | None = None) -> list[tuple]:
    """Execute a raw DuckDB SQL query and return rows as list of tuples.

    If catalog_dir is provided, attempts to open warm tier catalog (bathos.db).
    If warm DB doesn't exist and query requires it (e.g., SELECT FROM runs),
    DuckDB will raise an appropriate error.
    Otherwise, connects to DuckDB without a specific catalog (for arbitrary queries).
    """
    if catalog_dir is not None:
        db_path = catalog_dir / "bathos.db"
        con = duckdb.connect(str(db_path) if db_path.exists() else "")
    else:
        con = duckdb.connect()

    con.execute("SET TimeZone='UTC'")
    try:
        result = con.execute(sql).fetchall()
    except Exception as e:
        # If query tried to access the runs table without warm DB, provide helpful error
        if (
            "runs" in sql.lower()
            and catalog_dir is not None
            and not (catalog_dir / "bathos.db").exists()
        ):
            raise RuntimeError("No warm catalog. Run `bth compact` first.") from e
        raise

    return result
