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
from bathos.campaigns import (
    conclude_campaign,
    create_campaign,
    CampaignError,
    get_campaign,
    list_campaigns,
    review_campaign,
)
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


def _get_project_slug(project_slug: str = "") -> str:
    """Resolve project slug from parameter or environment."""
    if project_slug:
        return project_slug
    override = os.environ.get("BTH_PROJECT_SLUG")
    if override:
        return override
    # Try to load from .bth.toml
    try:
        config_path = find_project_config(Path.cwd())
        if config_path:
            config = load_project_config(config_path)
            return config.slug
    except Exception:
        pass
    return "default"


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
    project_slug: str = "",
    catalog_dir: str = "",
    output_paths: list[str] | None = None,
    tags: list[str] | None = None,
    agent_mode: str = "",
    derived_from: str = "",
    campaign_id: str = "",
    no_sidecar: bool = False,
) -> str:
    """Run a script and record provenance.

    Args:
        script_path: Path to script
        args: Command-line arguments
        project_slug: Project slug (default: 'default')
        catalog_dir: Catalog directory (empty = use default)
        output_paths: Registered output files
        tags: Search tags
        agent_mode: Agent mode ('collaborative', 'autonomous', or '')
        derived_from: Parent run ID (for parametric sweeps)
        campaign_id: Campaign ID to associate run with
        no_sidecar: Skip sidecar requirement (for exploratory runs)

    Returns:
        JSON string with run result or structured gate error
    """
    try:
        if not script_path:
            return json.dumps({"error": "script_path parameter is required"})
        if args is None:
            args = []
        if output_paths is None:
            output_paths = []
        if tags is None:
            tags = []

        # Construct argv: ['python', script_path, ...args]
        argv = ["python", script_path] + args

        # Resolve parameters
        cat_dir = _get_catalog_dir(catalog_dir or None)
        slug = project_slug or "default"

        # Call run_script with new parameters
        exit_code = run_script(
            argv=argv,
            project_slug=slug,
            catalog_dir=cat_dir,
            output_paths=output_paths,
            tags=tags,
            agent_mode=agent_mode or None,
            no_sidecar=no_sidecar,
            derived_from=derived_from or None,
            campaign_id=campaign_id or None,
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


def campaign_create_tool(
    name: str = "",
    mode: str = "exploration",
    project_slug: str = "",
    catalog_dir: str = "",
    question: str = "",
    hypothesis: str = "",
) -> str:
    """Create a new experiment campaign.

    Args:
        name: Campaign name
        mode: Campaign mode ('exploration' or 'confirmation')
        project_slug: Project slug (default: from config or 'default')
        catalog_dir: Catalog directory (empty = use default)
        question: Research question (optional)
        hypothesis: Research hypothesis (optional)

    Returns:
        JSON string with campaign details or error
    """
    try:
        if not name:
            return json.dumps({"error": "name parameter is required"})
        if mode not in ("exploration", "confirmation"):
            return json.dumps(
                {
                    "error": f"mode must be 'exploration' or 'confirmation', got {mode!r}"
                }
            )

        cat_dir = _get_catalog_dir(catalog_dir or None)
        slug = project_slug or _get_project_slug()

        import duckdb

        db = duckdb.connect(str(cat_dir / "bathos.db"))
        try:
            campaign = create_campaign(
                db,
                name=name,
                project_slug=slug,
                mode=mode,
                question=question or None,
                hypothesis=hypothesis or None,
            )
            return json.dumps(
                {
                    "campaign_id": campaign.id,
                    "name": campaign.name,
                    "mode": campaign.mode,
                    "status": campaign.status,
                    "started_at": campaign.started_at,
                },
                indent=2,
            )
        finally:
            db.close()
    except CampaignError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": str(e)})


def campaign_list_tool(
    catalog_dir: str = "",
    project_slug: str = "",
    status: str = "",
) -> str:
    """List campaigns with optional filters.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        project_slug: Filter by project slug
        status: Filter by status ('open', 'concluded')

    Returns:
        JSON string with campaigns array
    """
    try:
        cat_dir = _get_catalog_dir(catalog_dir or None)
        slug = project_slug or _get_project_slug()

        import duckdb

        db = duckdb.connect(str(cat_dir / "bathos.db"), read_only=True)
        try:
            campaigns = list_campaigns(db, project_slug=slug, status=status or None)
            campaigns_json = [
                {
                    "id": c.id,
                    "name": c.name,
                    "mode": c.mode,
                    "status": c.status,
                    "started_at": c.started_at,
                    "concluded_at": c.concluded_at,
                    "outcome_label": c.outcome_label,
                }
                for c in campaigns
            ]
            return json.dumps({"campaigns": campaigns_json, "count": len(campaigns_json)}, indent=2)
        finally:
            db.close()
    except Exception as e:
        return json.dumps({"error": str(e)})


def campaign_review_tool(
    campaign_id: str = "",
    catalog_dir: str = "",
) -> str:
    """Review campaign statistics and anomalies.

    Args:
        campaign_id: Campaign ID to review
        catalog_dir: Catalog directory (empty = use default)

    Returns:
        JSON string with campaign review (residual rate, outcome distribution, anomalies)
    """
    try:
        if not campaign_id:
            return json.dumps({"error": "campaign_id parameter is required"})

        cat_dir = _get_catalog_dir(catalog_dir or None)

        import duckdb

        db = duckdb.connect(str(cat_dir / "bathos.db"), read_only=True)
        try:
            review = review_campaign(db, campaign_id)
            return json.dumps(review, indent=2)
        finally:
            db.close()
    except CampaignError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": str(e)})


def campaign_conclude_tool(
    campaign_id: str = "",
    outcome_label: str = "",
    conclusion: str = "",
    catalog_dir: str = "",
) -> str:
    """Conclude a campaign with an outcome label.

    Args:
        campaign_id: Campaign ID to conclude
        outcome_label: Outcome label (e.g., 'success', 'inconclusive', 'failed')
        conclusion: Summary conclusion text
        catalog_dir: Catalog directory (empty = use default)

    Returns:
        JSON string with conclusion confirmation
    """
    try:
        if not campaign_id:
            return json.dumps({"error": "campaign_id parameter is required"})
        if not outcome_label:
            return json.dumps({"error": "outcome_label parameter is required"})

        cat_dir = _get_catalog_dir(catalog_dir or None)

        import duckdb

        db = duckdb.connect(str(cat_dir / "bathos.db"))
        try:
            conclude_campaign(db, campaign_id, outcome_label, conclusion or "")
            return json.dumps(
                {
                    "status": "concluded",
                    "campaign_id": campaign_id,
                    "outcome_label": outcome_label,
                },
                indent=2,
            )
        finally:
            db.close()
    except CampaignError as e:
        return json.dumps({"error": str(e)})
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
    project_slug: str = "",
    catalog_dir: str = "",
    output_paths: list[str] | None = None,
    tags: list[str] | None = None,
    agent_mode: str = "",
    derived_from: str = "",
    campaign_id: str = "",
    no_sidecar: bool = False,
) -> str:
    """Run a script and record provenance."""
    return run_tool(
        script_path=script_path,
        args=args,
        project_slug=project_slug,
        catalog_dir=catalog_dir,
        output_paths=output_paths,
        tags=tags,
        agent_mode=agent_mode,
        derived_from=derived_from,
        campaign_id=campaign_id,
        no_sidecar=no_sidecar,
    )


@app.tool("campaign_create")
def mcp_campaign_create_tool(
    name: str = "",
    mode: str = "exploration",
    project_slug: str = "",
    catalog_dir: str = "",
    question: str = "",
    hypothesis: str = "",
) -> str:
    """Create a new experiment campaign."""
    return campaign_create_tool(
        name=name,
        mode=mode,
        project_slug=project_slug,
        catalog_dir=catalog_dir,
        question=question,
        hypothesis=hypothesis,
    )


@app.tool("campaign_list")
def mcp_campaign_list_tool(
    catalog_dir: str = "",
    project_slug: str = "",
    status: str = "",
) -> str:
    """List campaigns with optional filters."""
    return campaign_list_tool(
        catalog_dir=catalog_dir,
        project_slug=project_slug,
        status=status,
    )


@app.tool("campaign_review")
def mcp_campaign_review_tool(
    campaign_id: str = "",
    catalog_dir: str = "",
) -> str:
    """Review campaign statistics and anomalies."""
    return campaign_review_tool(
        campaign_id=campaign_id,
        catalog_dir=catalog_dir,
    )


@app.tool("campaign_conclude")
def mcp_campaign_conclude_tool(
    campaign_id: str = "",
    outcome_label: str = "",
    conclusion: str = "",
    catalog_dir: str = "",
) -> str:
    """Conclude a campaign with an outcome label."""
    return campaign_conclude_tool(
        campaign_id=campaign_id,
        outcome_label=outcome_label,
        conclusion=conclusion,
        catalog_dir=catalog_dir,
    )


def mcp_server():
    """Entry point for MCP server (stdio transport).

    Called by pyproject.toml entry point: bth-mcp
    """
    app.run()


if __name__ == "__main__":
    app.run()
