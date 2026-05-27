from __future__ import annotations

import fnmatch
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

import duckdb

from bathos.catalog import read_runs
from bathos.schema import Run
from bathos.telemetry import event


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
    Supports dynamic schema lengths by providing fallback defaults.
    """
    try:
        # Extract fields sequentially, using defaults if row has fewer elements
        id_ = row[0]
        project_slug = row[1]
        command = row[2]
        argv = row[3]
        git_hash = row[4]
        git_branch = row[5]
        git_dirty = row[6]
        timestamp = row[7]
        duration_s = row[8]
        exit_code = row[9]
        status = row[10]
        output_paths = row[11]
        tags = row[12]
        schema_version = row[13]
        
        slurm_job_id = row[14] if len(row) > 14 else ""
        hostname = row[15] if len(row) > 15 else ""
        metadata = row[16] if len(row) > 16 else "{}"
        outcome = row[17] if len(row) > 17 else ""
        output_metadata = row[18] if len(row) > 18 else "[]"
        
        sidecar_sha256 = row[19] if len(row) > 19 else ""
        sidecar_path = row[20] if len(row) > 20 else ""
        parent_run_id = row[21] if len(row) > 21 else ""
        agent_mode = row[22] if len(row) > 22 else ""
        sidecar_mode = row[23] if len(row) > 23 else ""
        outcome_is_residual = row[24] if len(row) > 24 else False
        skill_sha256 = row[25] if len(row) > 25 else ""
        campaign_id = row[26] if len(row) > 26 else ""
        
        script_sha256 = row[27] if len(row) > 27 else ""
        postmortem_status = row[28] if len(row) > 28 else "unassigned"
        postmortem_override = row[29] if len(row) > 29 else "none"
        postmortem_verdict_override = row[30] if len(row) > 30 else "none"
        postmortem_author = row[31] if len(row) > 31 else ""
        postmortem_path = row[32] if len(row) > 32 else ""
        postmortem_hypothesis_status = row[33] if len(row) > 33 else "unassigned"
        postmortem_has_anomalies = row[34] if len(row) > 34 else False
        postmortem_summary = row[35] if len(row) > 35 else ""
        postmortem_asset_links = row[36] if len(row) > 36 else "{}"

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
            sidecar_sha256=sidecar_sha256 if sidecar_sha256 else "",
            sidecar_path=sidecar_path if sidecar_path else "",
            parent_run_id=parent_run_id if parent_run_id else "",
            agent_mode=agent_mode if agent_mode else "",
            sidecar_mode=sidecar_mode if sidecar_mode else "",
            outcome_is_residual=outcome_is_residual if outcome_is_residual is not None else False,
            skill_sha256=skill_sha256 if skill_sha256 else "",
            campaign_id=campaign_id if campaign_id else "",
            script_sha256=script_sha256 if script_sha256 else "",
            postmortem_status=postmortem_status if postmortem_status else "unassigned",
            postmortem_override=postmortem_override if postmortem_override else "none",
            postmortem_verdict_override=postmortem_verdict_override if postmortem_verdict_override else "none",
            postmortem_author=postmortem_author if postmortem_author else "",
            postmortem_path=postmortem_path if postmortem_path else "",
            postmortem_hypothesis_status=postmortem_hypothesis_status if postmortem_hypothesis_status else "unassigned",
            postmortem_has_anomalies=postmortem_has_anomalies if postmortem_has_anomalies is not None else False,
            postmortem_summary=postmortem_summary if postmortem_summary else "",
            postmortem_asset_links=postmortem_asset_links if postmortem_asset_links else "{}",
        )
    except (ValueError, TypeError, IndexError) as e:
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
    t_start = time.monotonic()

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

    # Emit telemetry event
    duration_ms = (time.monotonic() - t_start) * 1000
    event("catalog.query", query_kind="ls", duration_ms=int(duration_ms), rows=len(runs))

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
    t_start = time.monotonic()

    db_path = catalog_dir / "bathos.db"
    con = duckdb.connect(str(db_path))
    con.execute("SET TimeZone='UTC'")

    rows = con.execute("SELECT * FROM runs WHERE id = ?", [run_id]).fetchall()

    if not rows:
        con.close()
        # Emit telemetry event (0 rows found)
        duration_ms = (time.monotonic() - t_start) * 1000
        event("catalog.query", query_kind="get", duration_ms=int(duration_ms), rows=0)
        return None

    run = _row_to_run(rows[0])
    con.close()

    # Emit telemetry event
    duration_ms = (time.monotonic() - t_start) * 1000
    event("catalog.query", query_kind="get", duration_ms=int(duration_ms), rows=len(rows))

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
    t_start = time.monotonic()

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

    # Emit telemetry event
    duration_ms = (time.monotonic() - t_start) * 1000
    event("catalog.query", query_kind="find", duration_ms=int(duration_ms), rows=len(runs))

    return runs


def run_sql(sql: str, catalog_dir: Path | None = None) -> list[tuple]:
    """Execute a raw DuckDB SQL query and return rows as list of tuples.

    If catalog_dir is provided, attempts to open warm tier catalog (bathos.db).
    If warm DB doesn't exist and query requires it (e.g., SELECT FROM runs),
    DuckDB will raise an appropriate error.
    Otherwise, connects to DuckDB without a specific catalog (for arbitrary queries).
    """
    t_start = time.monotonic()
    result = []

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
    finally:
        con.close()

    # Emit telemetry event
    duration_ms = (time.monotonic() - t_start) * 1000
    event("catalog.query", query_kind="sql", duration_ms=int(duration_ms), rows=len(result))

    return result


def lineage(run_id: str, catalog_dir: Path) -> list[Run]:
    """Return ancestor chain of run_id following parent_run_id links (recursive CTE).

    Args:
        run_id: The run ID to trace ancestry for.
        catalog_dir: Path to catalog directory.

    Returns:
        List of Run objects in chronological order (oldest ancestor first).
        Returns empty list if run not found or catalog doesn't exist.
    """
    db_path = catalog_dir / "bathos.db"
    if not db_path.exists():
        return []

    db = duckdb.connect(str(db_path), read_only=True)
    db.execute("SET TimeZone='UTC'")
    try:
        # Use recursive CTE to find all ancestors
        # COALESCE handles both NULL and empty string for parent_run_id
        # depth < 50 prevents runaway cycles
        rows = db.execute(
            """
            WITH RECURSIVE ancestors AS (
                SELECT *, 0 AS depth FROM runs WHERE id = ?
                UNION ALL
                SELECT r.*, a.depth + 1 FROM runs r
                INNER JOIN ancestors a ON r.id = a.parent_run_id
                WHERE COALESCE(a.parent_run_id, '') != '' AND a.depth < 50
            )
            SELECT * EXCLUDE (depth) FROM ancestors ORDER BY timestamp
        """,
            [run_id],
        ).fetchall()
        return [_row_to_run(row) for row in rows if _row_to_run(row) is not None]
    except Exception:
        return []
    finally:
        db.close()
