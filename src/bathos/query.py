from __future__ import annotations

import copy
import fnmatch
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

import duckdb
import pyarrow.parquet as pq

from bathos.catalog import read_runs
from bathos.index import catalog_readable, connect_read
from bathos.schema import Run
from bathos.telemetry import event

logger = logging.getLogger(__name__)


class CatalogError(Exception):
    """Exception raised when catalog operations fail."""

    pass


def _filter_runs_by_output_file(
    runs: list[Run],
    pattern: str | None = None,
) -> list[Run]:
    """Filter runs by output file glob pattern.

    Checks both cool-tier output_paths and warm-tier output_metadata.
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

        # Then check warm-tier output_metadata if no match in output_paths
        if run.output_metadata and run.output_metadata != "[]":
            try:
                output_files = json.loads(run.output_metadata)
                if isinstance(output_files, list):
                    matches = any(
                        fnmatch.fnmatch(f.get("path", ""), pattern)
                        for f in output_files
                        if f.get("status") == "present"
                    )
                    if matches:
                        filtered.append(run)
            except (json.JSONDecodeError, TypeError):
                pass

    return filtered


# Per-field fallbacks the legacy warm reader has always applied to a missing or falsy
# value. Kept verbatim from the old positional `_row_to_run` so the first 37 columns
# (`_WARM_LEGACY_COLUMNS`) read exactly as before.
_WARM_FALSY_DEFAULTS: dict[str, object] = {
    "argv": [],
    "output_paths": [],
    "tags": [],
    "slurm_job_id": "",
    "hostname": "",
    "metadata": "{}",
    "output_metadata": "[]",
    "outcome": "",
    "sidecar_sha256": "",
    "sidecar_path": "",
    "parent_run_id": "",
    "agent_mode": "",
    "sidecar_mode": "",
    "skill_sha256": "",
    "campaign_id": "",
    "script_sha256": "",
    "postmortem_status": "unassigned",
    "postmortem_override": "none",
    "postmortem_verdict_override": "none",
    "postmortem_author": "",
    "postmortem_path": "",
    "postmortem_hypothesis_status": "unassigned",
    "postmortem_summary": "",
    "postmortem_asset_links": "{}",
}
_WARM_NONE_FALSE = frozenset({"outcome_is_residual", "postmortem_has_anomalies"})
_WARM_LEGACY_COLUMNS = frozenset(
    {
        "id", "project_slug", "command", "argv", "git_hash", "git_branch", "git_dirty",
        "timestamp", "duration_s", "exit_code", "status", "output_paths", "tags",
        "schema_version", "slurm_job_id", "hostname", "metadata", "outcome",
        "output_metadata", "sidecar_sha256", "sidecar_path", "parent_run_id", "agent_mode",
        "sidecar_mode", "outcome_is_residual", "skill_sha256", "campaign_id",
        "script_sha256", "postmortem_status", "postmortem_override",
        "postmortem_verdict_override", "postmortem_author", "postmortem_path",
        "postmortem_hypothesis_status", "postmortem_has_anomalies", "postmortem_summary",
        "postmortem_asset_links",
    }
)  # fmt: skip


def _row_to_run(row: tuple, columns: list[str]) -> Run | None:
    """Convert a warm `runs` row to a Run, keyed by column NAME.

    `columns` is the cursor's `description` names for that row. The previous positional
    version read `row[0..36]` and silently dropped every later column (stage_name,
    claim_*, seed, ..., git_provenance_source), so warm get_run/list_runs lost them.

    Fields are looked up by name from the dataclass. Columns absent from the table are
    skipped (the dataclass default applies). A None in a legacy column passes through
    unchanged, as before; a None in any later column takes the dataclass default.
    """
    from dataclasses import fields

    try:
        values = dict(zip(columns, row, strict=False))
        kwargs: dict = {}
        for f in fields(Run):
            if f.name not in values:
                continue
            value = values[f.name]
            if f.name in _WARM_FALSY_DEFAULTS:
                if not value:
                    value = copy.copy(_WARM_FALSY_DEFAULTS[f.name])
            elif f.name in _WARM_NONE_FALSE:
                if value is None:
                    value = False
            elif value is None and f.name not in _WARM_LEGACY_COLUMNS:
                continue
            kwargs[f.name] = value
        return Run(**kwargs)
    except (ValueError, TypeError, IndexError, KeyError) as e:
        raise RuntimeError(f"Failed to convert DuckDB row to Run: {e}") from e


def _resolve_backend(catalog_dir: Path) -> Literal["cool", "warm", "index"]:
    """Determine which backend to use based on catalog state.

    Returns 'index' when the runlog flag is on (spec "Mode"): once cut over,
    `bathos.db` is frozen/absent and the folded index (`bathos.index.
    connect_read`) is the only live source, regardless of whether a stale
    `bathos.db` happens to still be lying around. Otherwise 'warm' if
    catalog_dir/bathos.db exists, else 'cool'.
    """
    from bathos.runlog.mode import is_log_mode

    if is_log_mode(catalog_dir):
        return "index"
    if (catalog_dir / "bathos.db").exists() and _warm_has_runs_table(catalog_dir):
        return "warm"
    return "cool"


def _warm_has_runs_table(catalog_dir: Path) -> bool:
    """True unless bathos.db is readable and definitively lacks a `runs` table (debt #1477).

    A bathos.db can exist without `runs` (a compact that died right after creating
    the file, a foreign DB); picking "warm" then fails every read with a catalog
    error even though the cool fragments are fine. Opens read-only via
    `connect_read`, so it never blocks or is blocked by a writer more than any other
    read. If the DB cannot be opened at all (lock held by a writer, corrupt file)
    this answers True: that keeps the pre-existing behaviour of surfacing the real
    error from the warm path rather than silently serving cool data for a catalog
    that is merely busy or damaged.
    """
    try:
        con = connect_read(catalog_dir, read_only=True)
    except duckdb.Error:
        return True
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name = 'runs'"
        ).fetchone()
        return bool(row and row[0])
    except duckdb.Error:
        return True
    finally:
        con.close()


def _run_from_index_row(row: dict) -> Run:
    """Build a `Run` from one row of the folded index's `runs` table.

    Column-name keyed, unlike the legacy warm tier's positional
    `_row_to_run` -- `bathos.index.RUNS_DDL`'s column order does not match
    the legacy warm table's, so `SELECT *` position is not portable between
    the two backends; every field is looked up by name here instead.
    """
    from dataclasses import fields

    def _json_list(value: object) -> list:
        if not value:
            return []
        return json.loads(value) if isinstance(value, str) else list(value)

    timestamp = row.get("timestamp")
    if isinstance(timestamp, str) and timestamp:
        timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))

    kwargs: dict = {}
    for f in fields(Run):
        if f.name not in row:
            continue
        if f.name in ("argv", "output_paths", "tags"):
            kwargs[f.name] = _json_list(row[f.name])
        elif f.name == "timestamp":
            kwargs[f.name] = timestamp if timestamp is not None else f.default_factory()
        else:
            kwargs[f.name] = row[f.name]
    return Run(**kwargs)


def _index_query_runs(catalog_dir: Path, query: str, params: list) -> list[Run]:
    con = connect_read(catalog_dir)
    try:
        cur = con.execute(query, params)
        col_names = [d[0] for d in cur.description]
        rows = cur.fetchall()
    finally:
        con.close()
    return [_run_from_index_row(dict(zip(col_names, row, strict=True))) for row in rows]


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

    Dispatches to the index, warm, or cool backend based on catalog state.
    """
    backend = _resolve_backend(catalog_dir)
    if backend == "index":
        return _index_list_runs(catalog_dir, project=project, status=status, limit=limit)
    if backend == "warm":
        return _warm_list_runs(catalog_dir, project=project, status=status, limit=limit)
    return _cool_list_runs(catalog_dir, project=project, status=status, limit=limit)


def _index_list_runs(
    catalog_dir: Path,
    project: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[Run]:
    """List runs from the folded index (flag on; spec AC-3)."""
    query = "SELECT * FROM runs"
    params: list = []
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
    return _index_query_runs(catalog_dir, query, params)


def _warm_list_runs(
    catalog_dir: Path,
    project: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[Run]:
    """List runs using warm tier (DuckDB)."""
    t_start = time.monotonic()

    con = connect_read(catalog_dir)
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

    cur = con.execute(query, params)
    rows = cur.fetchall()
    columns = [d[0] for d in cur.description]

    # Convert rows to Run objects
    runs = []
    for row in rows:
        run = _row_to_run(row, columns)
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


def get_run(run_id: str, catalog_dir: Path, *, allow_cool_fallback: bool = False) -> Run | None:
    """Get a single run by ID, or None if not found.

    Dispatches to the index, warm, or cool backend based on catalog state.

    `allow_cool_fallback`: on a warm miss, also return a run that exists only
    as a cool fragment (written after the last compact), tagged
    `_source="cool_unindexed"`. Such a Run LACKS compact-time fields
    (`output_metadata` is "[]", postmortem overrides are unapplied), so this is
    opt-in for display callers only (`get_run_tool` / `bth show`). Callers
    that act on outputs or outcomes (blast radius, outputs, pin resolution)
    must keep the default and see None.
    """
    backend = _resolve_backend(catalog_dir)
    if backend == "index":
        return _index_get_run(run_id, catalog_dir)
    if backend == "warm":
        return _warm_get_run(run_id, catalog_dir, allow_cool_fallback=allow_cool_fallback)
    return _cool_get_run(run_id, catalog_dir)


def _index_get_run(run_id: str, catalog_dir: Path) -> Run | None:
    """Get a single run by ID from the folded index (flag on; spec AC-3)."""
    runs = _index_query_runs(catalog_dir, "SELECT * FROM runs WHERE id = ?", [run_id])
    return runs[0] if runs else None


COOL_UNINDEXED_SOURCE = "cool_unindexed"
_SOURCE_KEY = "_source"
_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9_-]+")


def run_source(run: Run) -> str | None:
    """Return the `_source` marker a fallback-read run carries in its metadata, or None."""
    try:
        meta = json.loads(run.metadata or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    return meta.get(_SOURCE_KEY) if isinstance(meta, dict) else None


def _cool_fallback_get_run(run_id: str, catalog_dir: Path) -> Run | None:
    """Look up a run that exists only as a cool fragment (written after the last compact).

    Only a single, unambiguous fragment is returned; the run is tagged with
    `_source = "cool_unindexed"` so callers can tell it has not been compacted.
    run_id is restricted to a safe charset before any glob is built.
    """
    if not _SAFE_RUN_ID.fullmatch(run_id or ""):
        return None

    t_start = time.monotonic()
    matches = sorted(
        p
        for p in (catalog_dir / "runs").glob(f"*/run_{run_id}.parquet")
        if not p.name.endswith(".tmp.parquet")
    )
    if not matches:
        return None
    if len(matches) > 1:
        event(
            "catalog.cool_fallback_ambiguous",
            run_id=run_id,
            paths=[str(p) for p in matches],
        )
        return None

    from bathos.compact import _apply_migrations

    try:
        table = pq.read_table(matches[0])
        run = _apply_migrations(Run.from_arrow_row(table.to_pydict(), 0))
    except Exception as exc:
        logger.warning("Cool fallback for run %s failed to read %s: %s", run_id, matches[0], exc)
        return None

    try:
        meta = json.loads(run.metadata or "{}")
    except (json.JSONDecodeError, TypeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta[_SOURCE_KEY] = COOL_UNINDEXED_SOURCE
    run.metadata = json.dumps(meta)

    duration_ms = (time.monotonic() - t_start) * 1000
    event("catalog.query", query_kind="get_cool_fallback", duration_ms=int(duration_ms), rows=1)
    return run


def _warm_get_run(run_id: str, catalog_dir: Path, *, allow_cool_fallback: bool = False) -> Run | None:
    """Get a single run by ID using warm tier (DuckDB), falling back to cool fragments."""
    t_start = time.monotonic()

    con = connect_read(catalog_dir)
    con.execute("SET TimeZone='UTC'")

    cur = con.execute("SELECT * FROM runs WHERE id = ?", [run_id])
    rows = cur.fetchall()
    columns = [d[0] for d in cur.description]

    if not rows:
        con.close()
        # Emit telemetry event (0 rows found)
        duration_ms = (time.monotonic() - t_start) * 1000
        event("catalog.query", query_kind="get", duration_ms=int(duration_ms), rows=0)
        # A run finished after the last `bth compact` lives only as a cool fragment.
        if allow_cool_fallback:
            return _cool_fallback_get_run(run_id, catalog_dir)
        return None

    run = _row_to_run(rows[0], columns)
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

    Dispatches to the index, warm, or cool backend based on catalog state.
    """
    backend = _resolve_backend(catalog_dir)
    if backend == "index":
        return _index_find_runs(
            catalog_dir,
            since=since,
            project=project,
            status=status,
            tags=tags,
            slurm_job_id=slurm_job_id,
        )
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


def _index_find_runs(
    catalog_dir: Path,
    since: datetime | None = None,
    project: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    slurm_job_id: str | None = None,
) -> list[Run]:
    """Find runs in the folded index (flag on; spec AC-3).

    `since` and `tags` are applied in Python after the fetch, like the cool
    backend: the index's `timestamp` column is text (an RFC3339 string, spec
    "Line envelope"), not a DuckDB TIMESTAMP, so comparing it against a
    `datetime` parameter belongs on the already-parsed `Run.timestamp`
    values, not as a SQL predicate; `tags` is JSON-encoded text for the same
    reason `_warm_find_runs` also filters it in Python.
    """
    query = "SELECT * FROM runs"
    params: list = []
    conditions = []
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

    runs = _index_query_runs(catalog_dir, query, params)
    if since:
        runs = [r for r in runs if r.timestamp >= since]
    if tags:
        runs = [r for r in runs if any(t in r.tags for t in tags)]
    return runs


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

    con = connect_read(catalog_dir)
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

    cur = con.execute(query, params)
    rows = cur.fetchall()
    columns = [d[0] for d in cur.description]

    # For tags filtering, need to check each run (tags are stored as arrays)
    runs = []
    for row in rows:
        run = _row_to_run(row, columns)
        if run:
            if tags and not any(t in run.tags for t in tags):
                continue
            runs.append(run)

    con.close()

    # Emit telemetry event
    duration_ms = (time.monotonic() - t_start) * 1000
    event("catalog.query", query_kind="find", duration_ms=int(duration_ms), rows=len(runs))

    return runs


def _is_read_only_violation(exc: BaseException) -> bool:
    """True if `exc` is DuckDB refusing a write on a read-only connection.

    DuckDB rejects the statement at bind time with `InvalidInputException:
    Cannot execute statement of type "INSERT" on database "..." which is
    attached in read-only mode!` -- so the write has NOT been partially
    applied and is safe to retry on a read-write connection. Matching the
    message is the only way to tell this apart from a genuine SQL error:
    both arrive as the same exception type.
    """
    return "read-only mode" in str(exc)


def _is_missing_runs_table(exc: BaseException) -> bool:
    """True only if `exc` is DuckDB reporting that the `runs` table does not exist.

    DuckDB raises `CatalogException` ("Table with name runs does not exist!")
    for this condition. Syntax errors (`ParserException`), binder errors, and
    I/O or lock errors (`IOException`) arrive as other types, so they never
    match. Debt #2812: any SQL error used to be reported as a missing catalog.
    """
    return isinstance(exc, duckdb.CatalogException) and bool(
        re.search(r"\bruns\b", str(exc), re.IGNORECASE)
    )


def run_sql(sql: str, catalog_dir: Path | None = None) -> list[tuple]:
    """Execute a raw DuckDB SQL query and return rows as list of tuples.

    If catalog_dir is provided, attempts to open warm tier catalog (bathos.db).
    If warm DB doesn't exist and query requires it (e.g., SELECT FROM runs),
    DuckDB will raise an appropriate error.
    Otherwise, connects to DuckDB without a specific catalog (for arbitrary queries).
    """
    t_start = time.monotonic()
    result = []

    def _open(read_only: bool) -> duckdb.DuckDBPyConnection:
        if catalog_dir is None:
            return duckdb.connect()
        return connect_read(catalog_dir, read_only=read_only, missing="empty")

    # Open read-only first: a read-write connection holds an EXCLUSIVE file
    # lock and shuts out every concurrent reader, and `bth sql` is almost
    # always a SELECT. Escalate to read-write only once DuckDB tells us the
    # statement actually writes.
    read_only = True
    while True:
        con = _open(read_only)
        con.execute("SET TimeZone='UTC'")
        try:
            result = con.execute(sql).fetchall()
            break
        except Exception as e:
            if read_only and _is_read_only_violation(e):
                # A write. Reopen read-write and run it for real; the
                # read-only attempt rejected the statement before executing
                # it, so nothing has been applied twice.
                read_only = False
                continue
            # A genuinely missing `runs` table gets a remedy hint. Every other
            # error (lock, I/O, syntax, binder) re-raises with its original text.
            if catalog_dir is not None and _is_missing_runs_table(e):
                from bathos.runlog.mode import is_log_mode

                if is_log_mode(catalog_dir):
                    # `bth compact` builds the legacy warm tier only; in log mode
                    # the runs view is folded from the event log instead.
                    raise RuntimeError(
                        "No warm catalog (log mode): the run index for "
                        f"{catalog_dir} has not been folded from the event log yet. "
                        "Ingest with run_ingest()."
                    ) from e
                if not catalog_readable(catalog_dir):
                    raise RuntimeError("No warm catalog. Run `bth compact` first.") from e
            raise
        finally:
            con.close()

    # Emit telemetry event
    duration_ms = (time.monotonic() - t_start) * 1000
    event("catalog.query", query_kind="sql", duration_ms=int(duration_ms), rows=len(result))

    return result


def lineage(run_id: str, catalog_dir: Path, depth: int = 50) -> list[Run]:
    """Return ancestor chain of run_id following parent_run_id links (recursive CTE).

    Args:
        run_id: The run ID to trace ancestry for.
        catalog_dir: Path to catalog directory.
        depth: Maximum number of ancestor hops to traverse. The starting run is
            depth 0, so depth=0 returns only that run and depth=50 (the default)
            reproduces the historical hardcoded cycle guard. Also bounds runaway
            recursion on a cyclic parent_run_id chain.

    Returns:
        List of Run objects in chronological order (oldest ancestor first).
        Returns empty list if run not found or catalog doesn't exist.

    Raises:
        ValueError: If depth is negative.
        CatalogError: If the query fails due to a schema or database error.
    """
    if depth < 0:
        raise ValueError(f"depth must be >= 0, got {depth}")

    db_path = catalog_dir / "bathos.db"
    if not db_path.exists():
        return []

    db = connect_read(catalog_dir, read_only=True)
    db.execute("SET TimeZone='UTC'")
    try:
        # Use recursive CTE to find all ancestors
        # COALESCE handles both NULL and empty string for parent_run_id
        # a.depth < ? bounds the walk: it is both the caller's requested limit and
        # the cycle guard. Bound as a parameter, never interpolated.
        cur = db.execute(
            """
            WITH RECURSIVE ancestors AS (
                SELECT *, 0 AS depth FROM runs WHERE id = ?
                UNION ALL
                SELECT r.*, a.depth + 1 FROM runs r
                INNER JOIN ancestors a ON r.id = a.parent_run_id
                WHERE COALESCE(a.parent_run_id, '') != '' AND a.depth < ?
            )
            SELECT * EXCLUDE (depth) FROM ancestors ORDER BY timestamp
        """,
            [run_id, depth],
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
        runs = [_row_to_run(row, columns) for row in rows]
        return [run for run in runs if run is not None]
    except Exception as e:
        raise CatalogError(f"Failed to query lineage for run {run_id}: {e}") from e
    finally:
        db.close()
