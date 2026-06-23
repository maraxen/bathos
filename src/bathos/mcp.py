"""FastMCP server for bathos experiment tracking.

Mirrors the CLI surface for agent use: query, compact/archive, check/sync/init/run,
campaigns, claims, postmortem, outputs, repair, verify, and lint.
"""

from __future__ import annotations

import functools
import json
import os
import time
import uuid
from pathlib import Path

from fastmcp import FastMCP

# Telemetry imports
from bathos.telemetry import event, mcp_request_id_var
from bathos.telemetry_bridge import init_server_telemetry

# Import core modules
from bathos.archive import archive as archive_runs
from bathos.campaigns import (
    conclude_campaign,
    create_campaign,
    CampaignError,
    add_run_to_campaign,
    get_campaign,
    list_campaigns,
    review_campaign,
)
from bathos.catalog import init_catalog
from bathos.checker import check_runs
from bathos.compact import compact as compact_catalog
from bathos.config import default_catalog_dir, find_project_config, load_project_config
from bathos.errors import BathosErrorCode, RESOLUTION_HINTS
from bathos.init import init_project
from bathos.prereg import GateError
from bathos.query import CatalogError, find_runs, get_run, list_runs, run_sql
from bathos.runner import run_script
from bathos.sidecar import SidecarError
from bathos.sync import sync_catalog
from bathos.export import ExportError

app = FastMCP("bathos")
mcp = app  # Alias for import compatibility


# ============================================================================
# Telemetry instrumentation
# ============================================================================


def _shape_error(tool_name: str, code: BathosErrorCode, exc: BaseException) -> dict:
    """Shape an exception into a structured MCP error envelope.

    Emits mcp.tool_error telemetry event and returns a dict with mandatory keys.
    The four mandatory keys (ok, error_code, error, resolution_hint) always come first
    so tool-specific data keys cannot clobber them.
    """
    event("mcp.tool_error", tool_name=tool_name, error_code=code.value,
          error_class=type(exc).__name__)
    return {
        "ok": False,
        "error_code": code.value,
        "error": str(exc),
        "resolution_hint": RESOLUTION_HINTS.get(code, ""),
    }


def traced_tool(fn):
    """Wrap a FastMCP tool function to catch and shape all exceptions into structured envelopes.

    Each tool invocation:
    - Emits mcp.call_start when entered
    - On success: returns a dict with mandatory keys (ok=True, error_code=None, error=None,
      resolution_hint=None) plus tool-specific data
    - On exception: catches and shapes to a structured error envelope (ok=False, error_code,
      error, resolution_hint), emits mcp.tool_error telemetry, and returns the dict
    - Emits mcp.call_end with duration and result size
    - Never re-raises exceptions to the FastMCP transport layer

    The mcp_request_id_var contextvar is set to a fresh UUID for this call.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        request_id = uuid.uuid4().hex
        token = mcp_request_id_var.set(request_id)
        tool_name = fn.__name__

        # Emit call_start with arg keys only (never values, to avoid leaking payloads)
        event("mcp.call_start", tool=tool_name, request_id=request_id,
              arg_keys=list(kwargs.keys()))

        t0 = time.monotonic_ns()
        try:
            result = await fn(*args, **kwargs)

            # Gate failure detection: runner returns dataclasses.asdict(GateErrorPayload)
            # which has keys: error_code, phase, taxonomy_label, errors, agent_mode,
            # resolution_hint, gate_schema_version. Detect by "phase" key (unique to gate payloads).
            if isinstance(result, dict) and "phase" in result and "error_code" in result:
                gate_code_str = result.get("error_code", "internal")
                try:
                    bathos_code = BathosErrorCode(gate_code_str)
                except ValueError:
                    bathos_code = BathosErrorCode.INTERNAL
                hint = result.get("resolution_hint") or RESOLUTION_HINTS.get(bathos_code, "")
                err_msgs = result.get("errors") or []
                return {
                    "ok": False,
                    "error_code": bathos_code.value,
                    "error": err_msgs[0] if err_msgs else gate_code_str,
                    "resolution_hint": hint,
                }

            # Success path: ensure four mandatory keys present, AFTER **result so they win
            if isinstance(result, dict):
                return {**result, "ok": True, "error_code": None, "error": None,
                        "resolution_hint": None}
            return {"ok": True, "error_code": None, "error": None, "resolution_hint": None}

        except GateError as e:
            return _shape_error(tool_name, BathosErrorCode.INTERNAL, e)
        except CatalogError as e:
            return _shape_error(tool_name, BathosErrorCode.CATALOG_ERROR, e)
        except CampaignError as e:
            return _shape_error(tool_name, BathosErrorCode.CAMPAIGN_ERROR, e)
        except SidecarError as e:
            return _shape_error(tool_name, BathosErrorCode.SIDECAR_ERROR, e)
        except ExportError as e:
            return _shape_error(tool_name, BathosErrorCode.EXPORT_ERROR, e)
        except SystemExit as e:
            return _shape_error(tool_name, BathosErrorCode.INTERNAL, e)
        except BaseException as e:
            return _shape_error(tool_name, BathosErrorCode.INTERNAL, e)
        finally:
            duration_ms = (time.monotonic_ns() - t0) / 1e6
            event("mcp.call_end", tool=tool_name, request_id=request_id,
                  duration_ms=duration_ms)
            mcp_request_id_var.reset(token)

    return wrapper


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
) -> dict:
    """List recent runs from catalog.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        limit: Max runs to return (default 10)

    Returns:
        Dict with runs array and count
    """
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
    return {"runs": runs_json, "count": len(runs_json)}


def find_runs_tool(
    catalog_dir: str = "",
    pattern: str = "",
) -> dict:
    """Find runs by pattern (project, status, tag, etc).

    Args:
        catalog_dir: Catalog directory (empty = use default)
        pattern: Filter pattern (e.g., project name)

    Returns:
        Dict with matching runs
    """
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
    return {"runs": runs_json, "count": len(runs_json)}


def get_run_tool(
    catalog_dir: str = "",
    run_id: str = "",
) -> dict:
    """Get details for a specific run.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        run_id: Run ID to fetch

    Returns:
        Dict with run details
    """
    if not run_id:
        return {"error": "run_id is required"}
    cat_dir = _get_catalog_dir(catalog_dir or None)
    run = get_run(run_id, cat_dir)
    if run is None:
        return {"error": f"Run {run_id} not found"}
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
    return run_dict


def run_sql_tool(
    catalog_dir: str = "",
    sql: str = "",
) -> dict:
    """Execute SQL query against catalog.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        sql: SQL query string

    Returns:
        Dict with rows array
    """
    if not sql:
        return {"error": "sql parameter is required"}
    cat_dir = _get_catalog_dir(catalog_dir or None) if catalog_dir else None
    rows = run_sql(sql, cat_dir)
    rows_json = [list(row) for row in rows]
    return {"rows": rows_json, "count": len(rows_json)}


def compact_tool(
    catalog_dir: str = "",
) -> dict:
    """Compact cool-tier Parquet to warm-tier DuckDB.

    Args:
        catalog_dir: Catalog directory (empty = use default)

    Returns:
        Dict with result summary
    """
    cat_dir = _get_catalog_dir(catalog_dir or None)
    result = compact_catalog(cat_dir)
    result_dict = {
        "ingested": result.ingested,
        "skipped": result.skipped,
        "duration_s": result.duration_s,
    }
    return result_dict


def archive_tool(
    catalog_dir: str = "",
    project: str = "",
) -> dict:
    """Archive warm-tier DuckDB to cold-tier Parquet.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        project: Project slug to archive
        keep_cool: Keep cool-tier Parquet after archiving

    Returns:
        Dict with result summary
    """
    if not project:
        return {"error": "project parameter is required"}
    cat_dir = _get_catalog_dir(catalog_dir or None)
    archive_root = cat_dir / "archive"
    result = archive_runs(cat_dir, archive_root=archive_root, project_slug=project)
    result_dict = {
        "runs_archived": result.runs_archived,
        "partitions_created": result.partitions_created,
        "archive_size_bytes": result.archive_size_bytes,
        "duration_s": result.duration_s,
    }
    return result_dict



def check_tool(
    catalog_dir: str = "",
    project_root: str = "",
    status_filter: str = "",
) -> dict:
    """Check run freshness vs git HEAD.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        project_root: Project root directory (empty = current directory)
        status_filter: Filter by status (e.g., "stale")

    Returns:
        Dict with check results
    """
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
    return {"results": results_json, "count": len(results_json)}


def sync_tool(
    catalog_dir: str = "",
    remote_name: str = "",
    pull: bool = False,
) -> dict:
    """Sync catalog to/from remote.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        remote_name: Remote name from .bth.toml
        pull: Pull from remote (default: push to remote)

    Returns:
        Dict with sync result
    """
    if not remote_name:
        return {"error": "remote_name parameter is required"}
    cat_dir = _get_catalog_dir(catalog_dir or None)
    # Load ProjectConfig from .bth.toml in project root
    config_path = find_project_config(Path.cwd())
    if not config_path:
        return {"error": "Could not find .bth.toml in project hierarchy"}
    config = load_project_config(config_path)
    result = sync_catalog(remote_name, config, cat_dir, pull=pull)
    result_dict = {
        "transferred": result.transferred,
        "duration_s": result.duration_s,
        "remote": result.remote,
        "filtered": result.filtered,
    }
    return result_dict


def init_tool(
    project_root: str = "",
    catalog_dir: str = "",
    slug: str = "",
    remote: str = "",
    slurm_partition: str = "",
) -> dict:
    """Initialize project with bathos.

    Args:
        project_root: Project root directory (empty = current directory)
        catalog_dir: Catalog directory (empty = use default)
        slug: Project slug
        remote: Remote in host:path format
        slurm_partition: Default SLURM partition

    Returns:
        Dict with init result
    """
    if not slug:
        return {"error": "slug parameter is required"}
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
    return {
        "initialized": True,
        "catalog_dir": str(cat_dir),
        "project_root": str(root),
        "slug": slug,
    }


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
) -> dict:
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
        Dict with run result or structured gate error
    """
    if not script_path:
        return {"error": "script_path parameter is required"}
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

    return {
        "script_path": script_path,
        "exit_code": exit_code,
        "success": exit_code == 0,
    }


def campaign_create_tool(
    name: str = "",
    mode: str = "exploration",
    project_slug: str = "",
    catalog_dir: str = "",
    question: str = "",
    hypothesis: str = "",
) -> dict:
    """Create a new experiment campaign.

    Args:
        name: Campaign name
        mode: Campaign mode ('exploration' or 'confirmation')
        project_slug: Project slug (default: from config or 'default')
        catalog_dir: Catalog directory (empty = use default)
        question: Research question (optional)
        hypothesis: Research hypothesis (optional)

    Returns:
        Dict with campaign details or error
    """
    if not name:
        return {"error": "name parameter is required"}
    if mode not in ("exploration", "confirmation"):
        return {
            "error": f"mode must be 'exploration' or 'confirmation', got {mode!r}"
        }

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
        return {
            "campaign_id": campaign.id,
            "name": campaign.name,
            "mode": campaign.mode,
            "status": campaign.status,
            "started_at": campaign.started_at,
        }
    finally:
        db.close()


def campaign_list_tool(
    catalog_dir: str = "",
    project_slug: str = "",
    status: str = "",
) -> dict:
    """List campaigns with optional filters.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        project_slug: Filter by project slug
        status: Filter by status ('open', 'concluded')

    Returns:
        Dict with campaigns array
    """
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
        return {"campaigns": campaigns_json, "count": len(campaigns_json)}
    finally:
        db.close()


def campaign_review_tool(
    campaign_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """Review campaign statistics and anomalies.

    Args:
        campaign_id: Campaign ID to review
        catalog_dir: Catalog directory (empty = use default)

    Returns:
        Dict with campaign review (residual rate, outcome distribution, anomalies)
    """
    if not campaign_id:
        return {"error": "campaign_id parameter is required"}

    cat_dir = _get_catalog_dir(catalog_dir or None)

    import duckdb

    db = duckdb.connect(str(cat_dir / "bathos.db"), read_only=True)
    try:
        review = review_campaign(db, campaign_id)
        return review
    finally:
        db.close()


def campaign_conclude_tool(
    campaign_id: str = "",
    outcome_label: str = "",
    conclusion: str = "",
    catalog_dir: str = "",
) -> dict:
    """Conclude a campaign with an outcome label.

    Args:
        campaign_id: Campaign ID to conclude
        outcome_label: Outcome label (e.g., 'success', 'inconclusive', 'failed')
        conclusion: Summary conclusion text
        catalog_dir: Catalog directory (empty = use default)

    Returns:
        Dict with conclusion confirmation
    """
    if not campaign_id:
        return {"error": "campaign_id parameter is required"}
    if not outcome_label:
        return {"error": "outcome_label parameter is required"}

    cat_dir = _get_catalog_dir(catalog_dir or None)

    import duckdb

    db = duckdb.connect(str(cat_dir / "bathos.db"))
    try:
        conclude_campaign(db, campaign_id, outcome_label, conclusion or "")
        return {
            "status": "concluded",
            "campaign_id": campaign_id,
            "outcome_label": outcome_label,
        }
    finally:
        db.close()


# ============================================================================
# MCP server tool registrations
# ============================================================================


@app.tool("list_runs")
@traced_tool
async def mcp_list_runs_tool(
    catalog_dir: str = "",
    limit: int = 10,
) -> dict:
    """List recent runs from catalog."""
    return list_runs_tool(catalog_dir=catalog_dir, limit=limit)


@app.tool("find_runs")
@traced_tool
async def mcp_find_runs_tool(
    catalog_dir: str = "",
    pattern: str = "",
) -> dict:
    """Find runs by pattern."""
    return find_runs_tool(catalog_dir=catalog_dir, pattern=pattern)


@app.tool("get_run")
@traced_tool
async def mcp_get_run_tool(
    catalog_dir: str = "",
    run_id: str = "",
) -> dict:
    """Get details for a specific run."""
    return get_run_tool(catalog_dir=catalog_dir, run_id=run_id)


@app.tool("cite_run")
@traced_tool
async def mcp_cite_run_tool(
    run_id: str,
    catalog_dir: str = "",
    format: str = "markdown",
) -> dict:
    """Return a structured citation for a run linking output to hypothesis and manifest.

    Args:
        run_id: The run ID to cite.
        catalog_dir: Path to catalog directory (uses default if empty).
        format: Output format ('markdown' or 'json').

    Returns:
        Formatted citation string.
    """
    from bathos.cite import format_citation
    from bathos.query import get_run as _get_run

    cat_dir = _get_catalog_dir(catalog_dir)
    run = _get_run(run_id, cat_dir)
    if run is None:
        raise CatalogError(f"Run not found: {run_id}")
    return {"citation": format_citation(run, fmt=format)}


@app.tool("lineage_prov")
@traced_tool
async def mcp_lineage_prov_tool(
    run_id: str,
    catalog_dir: str = "",
    depth: int = 10,
) -> dict:
    """Return W3C PROV-JSON lineage for a run.

    Args:
        run_id: The run ID to trace ancestry for.
        catalog_dir: Path to catalog directory (uses default if empty).
        depth: Maximum lineage depth to traverse.

    Returns:
        W3C PROV-JSON formatted lineage (dict).
    """
    from bathos.query import lineage as get_lineage, CatalogError
    from bathos.provenance import format_prov_json

    cat_dir = _get_catalog_dir(catalog_dir)
    ancestors = get_lineage(run_id, cat_dir)

    if not ancestors:
        raise CatalogError(f"Run not found or no lineage: {run_id}")

    prov_output = format_prov_json(ancestors)
    return {"prov": prov_output}


@app.tool("run_sql")
@traced_tool
async def mcp_run_sql_tool(
    catalog_dir: str = "",
    sql: str = "",
) -> dict:
    """Execute SQL query against catalog."""
    return run_sql_tool(catalog_dir=catalog_dir, sql=sql)


@app.tool("compact")
@traced_tool
async def mcp_compact_tool(
    catalog_dir: str = "",
) -> dict:
    """Compact cool-tier Parquet to warm-tier DuckDB."""
    return compact_tool(catalog_dir=catalog_dir)


@app.tool("archive")
@traced_tool
async def mcp_archive_tool(
    catalog_dir: str = "",
    project: str = "",
    keep_cool: bool = False,
) -> dict:
    """Archive warm-tier DuckDB to cold-tier Parquet."""
    return archive_tool(catalog_dir=catalog_dir, project=project, keep_cool=keep_cool)


@app.tool("check")
@traced_tool
async def mcp_check_tool(
    catalog_dir: str = "",
    project_root: str = "",
    status_filter: str = "",
) -> dict:
    """Check run freshness vs git HEAD."""
    return check_tool(
        catalog_dir=catalog_dir, project_root=project_root, status_filter=status_filter
    )


@app.tool("sync")
@traced_tool
async def mcp_sync_tool(
    catalog_dir: str = "",
    remote_name: str = "",
    pull: bool = False,
) -> dict:
    """Sync catalog to/from remote."""
    return sync_tool(catalog_dir=catalog_dir, remote_name=remote_name, pull=pull)


@app.tool("init")
@traced_tool
async def mcp_init_tool(
    project_root: str = "",
    catalog_dir: str = "",
    slug: str = "",
    remote: str = "",
    slurm_partition: str = "",
) -> dict:
    """Initialize project with bathos."""
    return init_tool(
        project_root=project_root,
        catalog_dir=catalog_dir,
        slug=slug,
        remote=remote,
        slurm_partition=slurm_partition,
    )


@app.tool("run")
@traced_tool
async def mcp_run_tool(
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
) -> dict:
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
@traced_tool
async def mcp_campaign_create_tool(
    name: str = "",
    mode: str = "exploration",
    project_slug: str = "",
    catalog_dir: str = "",
    question: str = "",
    hypothesis: str = "",
) -> dict:
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
@traced_tool
async def mcp_campaign_list_tool(
    catalog_dir: str = "",
    project_slug: str = "",
    status: str = "",
) -> dict:
    """List campaigns with optional filters."""
    return campaign_list_tool(
        catalog_dir=catalog_dir,
        project_slug=project_slug,
        status=status,
    )


@app.tool("campaign_review")
@traced_tool
async def mcp_campaign_review_tool(
    campaign_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """Review campaign statistics and anomalies."""
    return campaign_review_tool(
        campaign_id=campaign_id,
        catalog_dir=catalog_dir,
    )


@app.tool("campaign_conclude")
@traced_tool
async def mcp_campaign_conclude_tool(
    campaign_id: str = "",
    outcome_label: str = "",
    conclusion: str = "",
    catalog_dir: str = "",
) -> dict:
    """Conclude a campaign with an outcome label."""
    return campaign_conclude_tool(
        campaign_id=campaign_id,
        outcome_label=outcome_label,
        conclusion=conclusion,
        catalog_dir=catalog_dir,
    )


@app.tool()
@traced_tool
async def postmortem_scaffold(
    run_id: str,
    catalog_dir: str | None = None,
    workspace_root: str | None = None,
) -> dict:
    """Scaffold a new postmortem TOML template for the given run ID.

    Returns the path where the template was written.
    """
    import shlex
    from bathos.query import get_run as _get_run

    cat_dir = _get_catalog_dir(catalog_dir)
    run = _get_run(run_id, cat_dir)
    if not run:
        return {"error": f"Run '{run_id}' not found"}

    # Explicit workspace_root wins (AC-11); else resolve live fs_root (worktree-aware).
    if workspace_root:
        ws = Path(workspace_root).expanduser().resolve()
    else:
        from bathos.workspace import resolve_workspace

        ws = resolve_workspace().fs_root

    # Derive script path from command
    parts = shlex.split(run.command)
    script_path = None
    for part in parts:
        p = Path(part)
        if p.suffix == ".py":
            script_path = ws / p
            break
    if not script_path:
        script_path = ws / "run.py"

    script_path.parent.mkdir(parents=True, exist_ok=True)
    postmortem_path = script_path.parent / f"{script_path.name}.{run_id}.bth.postmortem.toml"

    toml_content = f"""run_id = "{run_id}"

[postmortem]
hypothesis_status = "unassigned"
summary = ""
unexpected_observations = ""
root_cause = ""
verdict_override = "none"
next_steps = ""
author = ""
status = "draft"

[asset_links]
"""
    postmortem_path.write_text(toml_content)
    return {"path": str(postmortem_path), "run_id": run_id}


@app.tool()
@traced_tool
async def postmortem_validate(
    path: str,
    workspace_root: str | None = None,
    strict_files: bool = False,
) -> dict:
    """Validate a postmortem TOML file.

    Returns {'validation_ok': True} on success or {'validation_ok': False, 'errors': [...]} on failure.
    """
    from bathos.postmortem import parse_postmortem, validate_postmortem

    pm_path = Path(path)
    if not pm_path.exists():
        return {"validation_ok": False, "errors": [f"File not found: {path}"]}

    pm = parse_postmortem(pm_path)

    # Explicit workspace_root wins (AC-11); else resolve live fs_root (worktree-aware).
    if workspace_root:
        ws = Path(workspace_root).expanduser().resolve()
    else:
        from bathos.workspace import resolve_workspace

        ws = resolve_workspace().fs_root

    result = validate_postmortem(pm, workspace_root=ws, strict_files=strict_files)
    if result.ok:
        return {"validation_ok": True, "run_id": pm.run_id, "hypothesis_status": pm.hypothesis_status}
    return {"validation_ok": False, "errors": [e.message for e in result.errors]}


@app.tool()
@traced_tool
async def postmortem_get(
    run_id: str,
    workspace_root: str | None = None,
) -> dict:
    """Retrieve postmortem data for the given run ID by scanning for matching TOML files.

    Returns the parsed postmortem fields or an error dict if not found.
    """
    from bathos.postmortem import parse_postmortem

    # Explicit workspace_root wins (AC-11); else resolve live fs_root (worktree-aware).
    if workspace_root:
        ws = Path(workspace_root).expanduser().resolve()
    else:
        from bathos.workspace import resolve_workspace

        ws = resolve_workspace().fs_root

    for pm_file in ws.rglob("*.bth.postmortem.toml"):
        try:
            pm = parse_postmortem(pm_file)
            if pm.run_id == run_id:
                return {
                    "run_id": pm.run_id,
                    "status": pm.status,
                    "hypothesis_status": pm.hypothesis_status,
                    "verdict_override": pm.verdict_override,
                    "summary": pm.summary,
                    "root_cause": pm.root_cause,
                    "unexpected_observations": pm.unexpected_observations,
                    "next_steps": pm.next_steps,
                    "author": pm.author,
                    "asset_links": pm.asset_links,
                    "anomalies": pm.anomalies,
                    "refutation_criteria_met": pm.refutation_criteria_met,
                    "path": str(pm_file),
                }
        except Exception:
            continue

    return {"error": f"No postmortem found for run_id '{run_id}'"}


@app.tool()
@traced_tool
async def claim_scaffold(
    campaign_id: str,
    catalog_dir: str | None = None,
    workspace_root: str | None = None,
) -> dict:
    """Scaffold a new claim TOML template for the given campaign ID.

    Returns the path where the template was written.
    """
    from bathos.claim import scaffold_claim

    cat_dir = _get_catalog_dir(catalog_dir)
    db_path = cat_dir / "bathos.db"

    if not db_path.exists():
        return {"error": f"Catalog database not found at {db_path}"}

    # Explicit workspace_root wins; else resolve live fs_root (worktree-aware).
    if workspace_root:
        ws = Path(workspace_root).expanduser().resolve()
    else:
        from bathos.workspace import resolve_workspace

        ws = resolve_workspace().fs_root

    try:
        import duckdb

        db = duckdb.connect(str(db_path), read_only=False)
        claim_path = scaffold_claim(campaign_id, db, ws)
        db.close()
        return {
            "ok": True,
            "path": str(claim_path),
            "campaign_id": campaign_id,
            "message": f"Claim template created at {claim_path}",
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "scaffold_failed"}


@app.tool()
@traced_tool
async def claim_validate(
    path: str,
    catalog_dir: str | None = None,
) -> dict:
    """Validate a claim TOML file.

    Returns {'ok': True} on success or {'ok': False, 'errors': [...]} on failure.
    """
    from bathos.claim import parse_claim, validate_claim

    claim_path = Path(path)
    if not claim_path.exists():
        return {"ok": False, "error": f"File not found: {path}"}

    try:
        claim = parse_claim(claim_path)
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e), "error_code": "file_not_found"}
    except ValueError as e:
        return {"ok": False, "error": str(e), "error_code": "parse_error"}

    # Optionally query catalog for regime coverage (Phase 2)
    db = None
    if catalog_dir:
        try:
            import duckdb

            cat_dir = Path(catalog_dir).expanduser().resolve()
            db_path = cat_dir / "bathos.db"
            if db_path.exists():
                db = duckdb.connect(str(db_path), read_only=True)
        except Exception:
            pass

    result = validate_claim(claim, db=db)

    if db:
        db.close()

    if result.ok:
        return {
            "ok": True,
            "path": path,
            "headline": claim.headline,
            "hypotheses_count": len(claim.hypotheses),
            "warnings": result.warnings,
            "infos": result.infos,
        }
    return {
        "ok": False,
        "errors": [e.message for e in result.errors],
        "warnings": result.warnings,
        "infos": result.infos,
        "error_code": "validation_failed",
    }


@app.tool()
@traced_tool
async def claim_register(
    path: str,
    campaign_id: str,
    catalog_dir: str | None = None,
    workspace_root: str | None = None,
    force: bool = False,
) -> dict:
    """Register a claim TOML file with a campaign (path + SHA256 anchor)."""
    from bathos.claim import register_claim

    cat_dir = _get_catalog_dir(catalog_dir)
    db_path = cat_dir / "bathos.db"

    if not db_path.exists():
        return {"ok": False, "error": f"Catalog database not found at {db_path}"}

    if workspace_root:
        ws = Path(workspace_root).expanduser().resolve()
    else:
        from bathos.workspace import resolve_workspace

        ws = resolve_workspace().fs_root

    try:
        import duckdb

        db = duckdb.connect(str(db_path), read_only=False)
        register_claim(Path(path), campaign_id, db, ws, force=force)
        db.close()
        return {
            "ok": True,
            "path": path,
            "campaign_id": campaign_id,
            "force": force,
            "message": f"Registered claim for campaign {campaign_id}",
        }
    except (RuntimeError, FileNotFoundError) as e:
        return {"ok": False, "error": str(e), "error_code": "register_failed"}


@app.tool()
@traced_tool
async def claim_attest_parity(
    campaign_id: str,
    parity_run_id: str,
    catalog_dir: str | None = None,
    workspace_root: str | None = None,
) -> dict:
    """Bind a passing literature-parity run to a campaign's registered claim (F4).

    Wraps attest_parity(): validates the run, updates the claim file atomically,
    and re-anchors the campaign claim_sha256 in the warm catalog.
    """
    from bathos.claim import attest_parity

    cat_dir = _get_catalog_dir(catalog_dir)
    db_path = cat_dir / "bathos.db"

    if not db_path.exists():
        return {"ok": False, "error": f"Catalog database not found at {db_path}"}

    if workspace_root:
        ws = Path(workspace_root).expanduser().resolve()
    else:
        from bathos.workspace import resolve_workspace

        ws = resolve_workspace().fs_root

    try:
        import duckdb

        db = duckdb.connect(str(db_path), read_only=False)
        attest_parity(campaign_id, parity_run_id, db, ws)
        db.close()
        return {
            "campaign_id": campaign_id,
            "parity_run_id": parity_run_id,
            "message": (
                f"Attested parity run {parity_run_id} on campaign {campaign_id}"
            ),
        }
    except (ValueError, RuntimeError, FileNotFoundError) as e:
        raise ValueError(str(e)) from e


@app.tool()
@traced_tool
async def validate_sidecar(
    path: str,
) -> dict:
    """Validate a sidecar TOML file for structural integrity.

    Args:
        path: Path to .bth.toml sidecar file

    Returns:
        {'validation_ok': True} on success or {'validation_ok': False, 'errors': [...]} on failure.
    """
    from bathos.sidecar import parse_sidecar, SidecarError
    from bathos.validate import validate_sidecar as validate_sidecar_impl

    sidecar_path = Path(path)
    if not sidecar_path.exists():
        return {"validation_ok": False, "errors": [f"File not found: {path}"]}

    try:
        sidecar = parse_sidecar(sidecar_path)
    except SidecarError as e:
        return {"validation_ok": False, "errors": [str(e)]}

    result = validate_sidecar_impl(sidecar, sidecar_path=sidecar_path)

    if result.errors:
        error_msgs = [f"{e.field}: {e.message}" for e in result.errors]
        return {"validation_ok": False, "errors": error_msgs}

    return {"validation_ok": True, "path": path}


def list_outputs_tool(
    run_id: str,
    workspace_root: str | None = None,
    live: bool = False,
    catalog_dir: str | None = None,
) -> dict:
    """List output files for a given run ID.

    Returns parsed output metadata from the run, optionally re-stated if live=True.
    """
    from bathos.query import get_run
    from bathos.config import find_project_config, load_project_config, default_catalog_dir
    import json

    if catalog_dir:
        cat = Path(catalog_dir).expanduser().resolve()
    elif workspace_root:
        ws = Path(workspace_root)
        config_path = find_project_config(ws)
        if config_path:
            cat = load_project_config(config_path).catalog_dir
        else:
            cat = default_catalog_dir()
    else:
        config_path = find_project_config()
        if config_path:
            cat = load_project_config(config_path).catalog_dir
        else:
            cat = default_catalog_dir()

    run = get_run(run_id, cat)
    if not run:
        return {"error": f"Run not found: {run_id}"}

    # Parse output_metadata
    try:
        if run.output_metadata and run.output_metadata != "[]":
            files = json.loads(run.output_metadata)
        else:
            files = []
    except (json.JSONDecodeError, TypeError):
        files = []

    # If live, re-stat files
    if live:
        for f in files:
            path_obj = Path(f.get("path", ""))
            try:
                if path_obj.exists():
                    stat = path_obj.stat()
                    f["status"] = "present"
                    f["size_bytes"] = stat.st_size
                    f["mtime_unix"] = stat.st_mtime
                else:
                    f["status"] = "missing"
                    f["size_bytes"] = 0
            except (PermissionError, OSError):
                f["status"] = "unreadable"
                f["size_bytes"] = 0

    return {"run_id": run_id, "files": files, "live": live}


def outputs_summary_tool(
    workspace_root: str | None = None,
    project: str | None = None,
    since: str | None = None,
    catalog_dir: str | None = None,
) -> dict:
    """Aggregate output file statistics across runs.

    Returns summary rows grouped by project.
    """
    from bathos.config import find_project_config, load_project_config, default_catalog_dir
    import json
    import duckdb

    if catalog_dir:
        cat = Path(catalog_dir).expanduser().resolve()
    elif workspace_root:
        ws = Path(workspace_root)
        config_path = find_project_config(ws)
        if config_path:
            cat = load_project_config(config_path).catalog_dir
        else:
            cat = default_catalog_dir()
    else:
        config_path = find_project_config()
        if config_path:
            cat = load_project_config(config_path).catalog_dir
        else:
            cat = default_catalog_dir()

    db_path = cat / "bathos.db"
    if not db_path.exists():
        return {"rows": [], "since": since, "note": "Catalog not yet compacted. Run 'bth compact' first."}

    # Query warm tier
    con = duckdb.connect(str(db_path))
    con.execute("SET TimeZone='UTC'")

    query = "SELECT project_slug, id, output_metadata FROM runs WHERE output_metadata IS NOT NULL AND output_metadata != '[]'"
    params = []

    if project:
        query += " AND project_slug = ?"
        params.append(project)

    if since:
        import re
        match = re.match(r"(\d+)([dhm])", since)
        if match:
            num, unit = match.groups()
            num = int(num)
            if unit == "d":
                hours = num * 24
            elif unit == "h":
                hours = num
            elif unit == "m":
                hours = num / 60
            else:
                hours = num * 24
            query += " AND timestamp > now() - interval '" + str(int(hours)) + " hour'"

    rows_data = con.execute(query, params).fetchall()
    con.close()

    # Aggregate by project
    aggregated = {}
    for project_slug, run_id, output_metadata_json in rows_data:
        try:
            files = json.loads(output_metadata_json) if output_metadata_json else []
            if project_slug not in aggregated:
                aggregated[project_slug] = {
                    "project": project_slug,
                    "run_count": 0,
                    "file_count": 0,
                    "total_bytes": 0,
                    "missing_count": 0,
                }
            agg = aggregated[project_slug]
            agg["run_count"] += 1
            agg["file_count"] += len(files)
            agg["total_bytes"] += sum(f.get("size_bytes", 0) for f in files)
            agg["missing_count"] += sum(1 for f in files if f.get("status") == "missing")
        except (json.JSONDecodeError, TypeError):
            pass

    return {"rows": list(aggregated.values()), "since": since}


@app.tool("list_outputs")
@traced_tool
async def mcp_list_outputs_tool(
    run_id: str,
    catalog_dir: str | None = None,
    workspace_root: str | None = None,
    live: bool = False,
) -> dict:
    """List output files registered for a run."""
    return list_outputs_tool(
        run_id=run_id,
        catalog_dir=catalog_dir,
        workspace_root=workspace_root,
        live=live,
    )


@app.tool("outputs_summary")
@traced_tool
async def mcp_outputs_summary_tool(
    workspace_root: str | None = None,
    project: str | None = None,
    since: str | None = None,
    catalog_dir: str | None = None,
) -> dict:
    """Aggregate output file statistics across runs."""
    return outputs_summary_tool(
        workspace_root=workspace_root,
        project=project,
        since=since,
        catalog_dir=catalog_dir,
    )


def campaign_add_tool(
    run_id: str = "",
    campaign_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """Add a run to a campaign."""
    if not run_id:
        return {"error": "run_id parameter is required"}
    if not campaign_id:
        return {"error": "campaign_id parameter is required"}

    cat_dir = _get_catalog_dir(catalog_dir or None)
    import duckdb

    db = duckdb.connect(str(cat_dir / "bathos.db"))
    try:
        add_run_to_campaign(db, campaign_id, run_id)
        return {
            "ok": True,
            "run_id": run_id,
            "campaign_id": campaign_id,
            "message": f"Added run {run_id} to campaign {campaign_id}",
        }
    except CampaignError as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


def campaign_show_tool(
    campaign_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """Show campaign details."""
    if not campaign_id:
        return {"error": "campaign_id parameter is required"}

    cat_dir = _get_catalog_dir(catalog_dir or None)
    import duckdb

    db = duckdb.connect(str(cat_dir / "bathos.db"), read_only=True)
    try:
        campaign = get_campaign(db, campaign_id)
        if campaign is None:
            return {"ok": False, "error": f"Campaign not found: {campaign_id}"}
        return {
            "ok": True,
            "campaign_id": campaign.id,
            "name": campaign.name,
            "mode": campaign.mode,
            "status": campaign.status,
            "question": campaign.question,
            "hypothesis": campaign.hypothesis,
            "started_at": campaign.started_at,
            "concluded_at": campaign.concluded_at,
            "outcome_label": campaign.outcome_label,
            "conclusion": campaign.conclusion,
        }
    finally:
        db.close()


def verify_tool(
    tier: str = "all",
    catalog_dir: str = "",
    archive_dir: str = "",
) -> dict:
    """Verify catalog integrity across cool, warm, and archive tiers."""
    from bathos.verify import verify_all, verify_archive, verify_cool, verify_warm

    cat_dir = _get_catalog_dir(catalog_dir or None)
    archive_root = Path(archive_dir).expanduser() if archive_dir else Path.home() / ".bth" / "archive"

    if tier == "cool":
        results = [verify_cool(cat_dir)]
    elif tier == "warm":
        results = [verify_warm(cat_dir)]
    elif tier == "archive":
        results = [verify_archive(archive_root)]
    elif tier == "all":
        results = verify_all(cat_dir, archive_root)
    else:
        return {"ok": False, "error": f"Unknown tier: {tier!r}"}

    payload = {
        "ok": all(r.ok for r in results),
        "results": [
            {"tier": r.tier, "ok": r.ok, "warnings": r.warnings, "errors": r.errors}
            for r in results
        ],
    }
    return payload


def lint_tool(project_root: str = "") -> dict:
    """Lint scripts/ and claim files for naming and structural issues."""
    from bathos.linter import (
        IssueSeverity,
        check_adversarial_checks,
        check_baseline_ref_exists,
        check_bypass_trend,
        check_canonical_stage_names,
        check_claim_opaque_labels,
        check_ephemeral_output_paths,
        check_residual_rates,
        check_threshold_basis,
        check_todo_strings_in_scaffold,
        check_unfired_branches,
        lint_project,
    )

    root = Path(project_root) if project_root else Path.cwd()
    issues = lint_project(root.resolve())
    issues.extend(check_claim_opaque_labels(root.resolve()))
    issues.extend(check_adversarial_checks(root.resolve()))
    issues.extend(check_threshold_basis(root.resolve()))
    issues.extend(check_todo_strings_in_scaffold(root.resolve()))

    catalog_dir = _get_catalog_dir(None)
    db_path = catalog_dir / "bathos.db"
    if db_path.exists():
        issues.extend(check_residual_rates(catalog_dir))
        issues.extend(check_bypass_trend(catalog_dir))
        issues.extend(check_unfired_branches(catalog_dir))
        issues.extend(check_ephemeral_output_paths(catalog_dir))
        issues.extend(check_canonical_stage_names(catalog_dir))
        issues.extend(check_baseline_ref_exists(root.resolve(), catalog_dir, db_path))

    errors = [i for i in issues if i.severity == IssueSeverity.ERROR]
    warnings = [i for i in issues if i.severity == IssueSeverity.WARNING]
    return {
        "ok": len(errors) == 0,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "issues": [
            {
                "severity": i.severity.value,
                "path": str(i.path),
                "issue": i.issue,
                "detail": i.detail,
            }
            for i in issues
        ],
    }


@app.tool("campaign_add")
@traced_tool
async def mcp_campaign_add_tool(
    run_id: str = "",
    campaign_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """Add a run to a campaign."""
    return campaign_add_tool(run_id=run_id, campaign_id=campaign_id, catalog_dir=catalog_dir)


@app.tool("campaign_show")
@traced_tool
async def mcp_campaign_show_tool(
    campaign_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """Show campaign details."""
    return campaign_show_tool(campaign_id=campaign_id, catalog_dir=catalog_dir)


@app.tool("verify")
@traced_tool
async def mcp_verify_tool(
    tier: str = "all",
    catalog_dir: str = "",
    archive_dir: str = "",
) -> dict:
    """Verify catalog integrity."""
    return verify_tool(tier=tier, catalog_dir=catalog_dir, archive_dir=archive_dir)


@app.tool("lint")
@traced_tool
async def mcp_lint_tool(
    project_root: str = "",
) -> dict:
    """Lint project scripts and claim files."""
    return lint_tool(project_root=project_root)


@app.tool("repair_scan")
@traced_tool
async def mcp_repair_scan_tool(
    catalog_dir: str | None = None,
    tier: str = "all",
) -> dict:
    """Scan the catalog for repairable issues without making any changes.

    Returns a list of findings with action type, path, and detail.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        tier: Scan scope: 'cool', 'warm', 'archive', or 'all'

    Returns:
        JSON dict with findings (list of action dicts) and count
    """
    from bathos.repair import scan

    cat_dir = _get_catalog_dir(catalog_dir)
    actions, warnings = scan(catalog_dir=cat_dir, tier=tier)
    findings = [vars(a) for a in actions]
    return {
        "findings": findings,
        "count": len(findings),
        "warnings": warnings,
    }


@app.tool("repair")
@traced_tool
async def mcp_repair_tool(
    catalog_dir: str | None = None,
    tier: str = "all",
    dry_run: bool = True,
    acknowledge_warm_loss: bool = False,
) -> dict:
    """Run catalog repair. dry_run=True (default) is safe; set dry_run=False to apply.

    Pass acknowledge_warm_loss=True only when warm-tier data loss is acceptable.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        tier: Repair scope: 'cool', 'warm', 'archive', or 'all'
        dry_run: If True (default), plan actions without executing
        acknowledge_warm_loss: If True, proceed with warm DB rebuild even if data loss

    Returns:
        JSON dict with manifest (actions and metadata) or error
    """
    from bathos.repair import repair as _repair

    cat_dir = _get_catalog_dir(catalog_dir)
    try:
        manifest = _repair(
            catalog_dir=cat_dir,
            tier=tier,
            dry_run=dry_run,
            acknowledge_warm_loss=acknowledge_warm_loss,
        )
        manifest_dict = {
            "run_ts": manifest.run_ts,
            "catalog_dir": manifest.catalog_dir,
            "dry_run": manifest.dry_run,
            "tier": manifest.tier,
            "actions": [vars(a) for a in manifest.actions],
            "warnings": manifest.warnings,
            "action_count": len(manifest.actions),
        }
        return manifest_dict
    except SystemExit as e:
        # repair() raises SystemExit(1) when warm rebuild needed but not acknowledged
        return {
            "error": "Warm database rebuild required but not acknowledged",
            "exit_code": e.code,
            "actions": [],
            "warnings": [],
        }


def mcp_server():
    """Entry point for MCP server (stdio transport).

    Called by pyproject.toml entry point: bth-mcp
    """
    init_server_telemetry()
    app.run()


if __name__ == "__main__":
    init_server_telemetry()
    app.run()
