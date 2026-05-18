"""FastMCP server for bathos experiment tracking.

Provides 10 tools that mirror the CLI:
- Query: list_runs, find_runs, get_run, run_sql
- Compact/Archive: compact, archive
- Check/Sync/Init/Run: check, sync, init, run
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastmcp import FastMCP

# Import core modules
from bathos.archive import archive as archive_runs
from bathos.catalog import init_catalog
from bathos.checker import check_runs
from bathos.compact import compact as compact_catalog
from bathos.config import default_catalog_dir, find_project_config, load_project_config
from bathos.init import init_project
from bathos.query import find_runs, get_run, list_runs, run_sql
from bathos.runner import run_script
from bathos.sync import sync_catalog

app = FastMCP("bathos")


def _get_catalog_dir(catalog_dir: str | None = None) -> Path:
    """Resolve catalog directory from parameter or environment."""
    if catalog_dir:
        return Path(catalog_dir)
    override = os.environ.get("BTH_CATALOG_DIR")
    if override:
        return Path(override)
    return default_catalog_dir()


# ============================================================================
# Core tool implementations (testable functions)
# ============================================================================


def list_runs_tool(
    catalog_dir: str = "",
    limit: int = 10,
) -> str:
    """List recent runs from catalog.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        limit: Max runs to return (default 10)

    Returns:
        JSON string with runs array and count
    """
    try:
        cat_dir = _get_catalog_dir(catalog_dir or None)
        runs = list_runs(cat_dir, limit=limit)
        runs_json = [
            {
                "id": r.id,
                "project_slug": r.project_slug,
                "command": r.command,
                "status": r.status,
                "exit_code": r.exit_code,
                "duration_s": r.duration_s,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            }
            for r in runs
        ]
        return json.dumps({"runs": runs_json, "count": len(runs_json)}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


def find_runs_tool(
    catalog_dir: str = "",
    pattern: str = "",
) -> str:
    """Find runs by pattern (project, status, tag, etc).

    Args:
        catalog_dir: Catalog directory (empty = use default)
        pattern: Filter pattern (e.g., project name)

    Returns:
        JSON string with matching runs
    """
    try:
        cat_dir = _get_catalog_dir(catalog_dir or None)
        # For simplicity, treat pattern as project filter
        runs = find_runs(cat_dir, project=pattern if pattern else None)
        runs_json = [
            {
                "id": r.id,
                "project_slug": r.project_slug,
                "command": r.command,
                "status": r.status,
                "exit_code": r.exit_code,
                "duration_s": r.duration_s,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            }
            for r in runs
        ]
        return json.dumps({"runs": runs_json, "count": len(runs_json)}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


def get_run_tool(
    catalog_dir: str = "",
    run_id: str = "",
) -> str:
    """Get details for a specific run.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        run_id: Run ID to fetch

    Returns:
        JSON string with run details
    """
    try:
        if not run_id:
            return json.dumps({"error": "run_id is required"})
        cat_dir = _get_catalog_dir(catalog_dir or None)
        run = get_run(run_id, cat_dir)
        if run is None:
            return json.dumps({"error": f"Run {run_id} not found"})
        run_dict = {
            "id": run.id,
            "project_slug": run.project_slug,
            "command": run.command,
            "argv": run.argv,
            "git_hash": run.git_hash,
            "git_branch": run.git_branch,
            "git_dirty": run.git_dirty,
            "status": run.status,
            "exit_code": run.exit_code,
            "duration_s": run.duration_s,
            "timestamp": run.timestamp.isoformat() if run.timestamp else None,
            "output_paths": run.output_paths,
            "tags": run.tags,
            "hostname": run.hostname,
        }
        return json.dumps(run_dict, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


def run_sql_tool(
    catalog_dir: str = "",
    sql: str = "",
) -> str:
    """Execute SQL query against catalog.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        sql: SQL query string

    Returns:
        JSON string with rows array
    """
    try:
        if not sql:
            return json.dumps({"error": "sql parameter is required"})
        cat_dir = _get_catalog_dir(catalog_dir or None) if catalog_dir else None
        rows = run_sql(sql, cat_dir)
        rows_json = [list(row) for row in rows]
        return json.dumps({"rows": rows_json, "count": len(rows_json)}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


def compact_tool(
    catalog_dir: str = "",
) -> str:
    """Compact cool-tier Parquet to warm-tier DuckDB.

    Args:
        catalog_dir: Catalog directory (empty = use default)

    Returns:
        JSON string with result summary
    """
    try:
        cat_dir = _get_catalog_dir(catalog_dir or None)
        result = compact_catalog(cat_dir)
        result_dict = {
            "ingested": result.ingested,
            "skipped": result.skipped,
            "duration_s": result.duration_s,
        }
        return json.dumps(result_dict, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


def archive_tool(
    catalog_dir: str = "",
    project: str = "",
) -> str:
    """Archive warm-tier DuckDB to cold-tier Parquet.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        project: Project slug to archive
        keep_cool: Keep cool-tier Parquet after archiving

    Returns:
        JSON string with result summary
    """
    try:
        if not project:
            return json.dumps({"error": "project parameter is required"})
        cat_dir = _get_catalog_dir(catalog_dir or None)
        archive_root = cat_dir / "archive"
        result = archive_runs(cat_dir, archive_root=archive_root, project_slug=project)
        result_dict = {
            "runs_archived": result.runs_archived,
            "partitions_created": result.partitions_created,
            "archive_size_bytes": result.archive_size_bytes,
            "duration_s": result.duration_s,
        }
        return json.dumps(result_dict, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


def check_tool(
    catalog_dir: str = "",
    project_root: str = "",
    status_filter: str = "",
) -> str:
    """Check run freshness vs git HEAD.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        project_root: Project root directory (empty = current directory)
        status_filter: Filter by status (e.g., "stale")

    Returns:
        JSON string with check results
    """
    try:
        cat_dir = _get_catalog_dir(catalog_dir or None)
        proj_root = Path(project_root) if project_root else Path.cwd()
        results = check_runs(cat_dir, proj_root, status_filter=status_filter)
        results_json = [
            {
                "run_id": r.run_id,
                "status": r.status,
            }
            for r in results
        ]
        return json.dumps({"results": results_json, "count": len(results_json)}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


def sync_tool(
    catalog_dir: str = "",
    remote_name: str = "",
    pull: bool = False,
) -> str:
    """Sync catalog to/from remote.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        remote_name: Remote name from .bth.toml
        pull: Pull from remote (default: push to remote)

    Returns:
        JSON string with sync result
    """
    try:
        if not remote_name:
            return json.dumps({"error": "remote_name parameter is required"})
        cat_dir = _get_catalog_dir(catalog_dir or None)
        # Load ProjectConfig from .bth.toml in project root
        config_path = find_project_config(Path.cwd())
        if not config_path:
            return json.dumps({"error": "Could not find .bth.toml in project hierarchy"})
        config = load_project_config(config_path)
        result = sync_catalog(remote_name, config, cat_dir, pull=pull)
        result_dict = {
            "transferred": result.transferred,
            "duration_s": result.duration_s,
            "remote": result.remote,
        }
        return json.dumps(result_dict, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


def init_tool(
    project_root: str = "",
    catalog_dir: str = "",
    slug: str = "",
    remote: str = "",
    slurm_partition: str = "",
) -> str:
    """Initialize project with bathos.

    Args:
        project_root: Project root directory (empty = current directory)
        catalog_dir: Catalog directory (empty = use default)
        slug: Project slug
        remote: Remote in host:path format
        slurm_partition: Default SLURM partition

    Returns:
        JSON string with init result
    """
    try:
        if not slug:
            return json.dumps({"error": "slug parameter is required"})
        root = Path(project_root) if project_root else Path.cwd()
        cat_dir = _get_catalog_dir(catalog_dir or None)
        init_project(
            root,
            slug=slug,
            catalog_dir=cat_dir,
            remote=remote or None,
            slurm_partition=slurm_partition or None,
        )
        init_catalog(cat_dir)
        return json.dumps(
            {
                "initialized": True,
                "catalog_dir": str(cat_dir),
                "project_root": str(root),
                "slug": slug,
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})


def run_tool(
    script_path: str = "",
    args: list[str] | None = None,
) -> str:
    """Run a script and record provenance.

    Args:
        script_path: Path to script
        args: Command-line arguments

    Returns:
        JSON string with run result
    """
    try:
        if not script_path:
            return json.dumps({"error": "script_path parameter is required"})
        if args is None:
            args = []
        # Construct argv: ['python', script_path, ...args]
        argv = ["python", script_path] + args
        exit_code = run_script(
            argv=argv,
            project_slug="default",
            catalog_dir=_get_catalog_dir(None),
            output_paths=[],
            tags=[],
        )
        return json.dumps(
            {
                "script_path": script_path,
                "exit_code": exit_code,
                "success": exit_code == 0,
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})


# ============================================================================
# MCP server tool registrations
# ============================================================================


@app.tool("list_runs")
def mcp_list_runs_tool(
    catalog_dir: str = "",
    limit: int = 10,
) -> str:
    """List recent runs from catalog."""
    return list_runs_tool(catalog_dir=catalog_dir, limit=limit)


@app.tool("find_runs")
def mcp_find_runs_tool(
    catalog_dir: str = "",
    pattern: str = "",
) -> str:
    """Find runs by pattern."""
    return find_runs_tool(catalog_dir=catalog_dir, pattern=pattern)


@app.tool("get_run")
def mcp_get_run_tool(
    catalog_dir: str = "",
    run_id: str = "",
) -> str:
    """Get details for a specific run."""
    return get_run_tool(catalog_dir=catalog_dir, run_id=run_id)


@app.tool("run_sql")
def mcp_run_sql_tool(
    catalog_dir: str = "",
    sql: str = "",
) -> str:
    """Execute SQL query against catalog."""
    return run_sql_tool(catalog_dir=catalog_dir, sql=sql)


@app.tool("compact")
def mcp_compact_tool(
    catalog_dir: str = "",
) -> str:
    """Compact cool-tier Parquet to warm-tier DuckDB."""
    return compact_tool(catalog_dir=catalog_dir)


@app.tool("archive")
def mcp_archive_tool(
    catalog_dir: str = "",
    project: str = "",
    keep_cool: bool = False,
) -> str:
    """Archive warm-tier DuckDB to cold-tier Parquet."""
    return archive_tool(catalog_dir=catalog_dir, project=project, keep_cool=keep_cool)


@app.tool("check")
def mcp_check_tool(
    catalog_dir: str = "",
    project_root: str = "",
    status_filter: str = "",
) -> str:
    """Check run freshness vs git HEAD."""
    return check_tool(
        catalog_dir=catalog_dir, project_root=project_root, status_filter=status_filter
    )


@app.tool("sync")
def mcp_sync_tool(
    catalog_dir: str = "",
    remote_name: str = "",
    pull: bool = False,
) -> str:
    """Sync catalog to/from remote."""
    return sync_tool(catalog_dir=catalog_dir, remote_name=remote_name, pull=pull)


@app.tool("init")
def mcp_init_tool(
    project_root: str = "",
    catalog_dir: str = "",
    slug: str = "",
    remote: str = "",
    slurm_partition: str = "",
) -> str:
    """Initialize project with bathos."""
    return init_tool(
        project_root=project_root,
        catalog_dir=catalog_dir,
        slug=slug,
        remote=remote,
        slurm_partition=slurm_partition,
    )


@app.tool("run")
def mcp_run_tool(
    script_path: str = "",
    args: list[str] | None = None,
) -> str:
    """Run a script and record provenance."""
    return run_tool(script_path=script_path, args=args)


def mcp_server():
    """Entry point for MCP server (stdio transport).

    Called by pyproject.toml entry point: bth-mcp
    """
    app.run()


if __name__ == "__main__":
    app.run()
