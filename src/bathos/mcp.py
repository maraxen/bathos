"""FastMCP server for bathos experiment tracking.

Mirrors the CLI surface for agent use: query, compact/archive, check/sync/init/run,
campaigns, claims, postmortem, outputs, repair, verify, and lint.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Annotated

import cisternal
import cyclopts
from fastmcp import FastMCP

from bathos.anchor import find_anchors, get_anchor, register_anchor

# Import core modules
from bathos.archive import archive as archive_runs
from bathos.authoring.models import ClaimPayload
from bathos.blast_radius import _UNBOUNDED_SCAN_LIMIT
from bathos.campaigns import (
    CAMPAIGN_MODES,
    BreakdownRefused,
    CampaignError,
    add_run_to_campaign,
    conclude_campaign,
    create_campaign,
    get_campaign,
    list_campaigns,
    review_campaign,
)
from bathos.capability import probe_capabilities
from bathos.catalog import init_catalog
from bathos.checker import check_runs
from bathos.cli_common import normalise_output_paths, normalise_tags, require_project_slug
from bathos.compact import compact as compact_catalog
from bathos.config import default_catalog_dir, find_project_config, load_project_config
from bathos.errors import RESOLUTION_HINTS, BathosErrorCode
from bathos.export import ExportError
from bathos.figure_registry import FigureEntrySchemaError, register_figure_entry
from bathos.index import connect_read
from bathos.init import init_project
from bathos.mcp_auth import McpAuthError, check_token, mark_stdio_transport, token_required
from bathos.prereg import GateError
from bathos.progress import progress_path_for, read_progress
from bathos.query import (
    COOL_UNINDEXED_SOURCE,
    CatalogError,
    find_runs,
    get_run,
    list_runs,
    run_source,
    run_sql,
)
from bathos.readback import (
    figure_lookup as readback_figure_lookup,
)
from bathos.readback import (
    get_trust_state as readback_get_trust_state,
)
from bathos.readback import (
    list_candidates as readback_list_candidates,
)
from bathos.readback import (
    query_attestation as readback_query_attestation,
)
from bathos.readback import (
    read_campaign_report as readback_read_campaign_report,
)
from bathos.readback import (
    read_figure_manifest as readback_read_figure_manifest,
)
from bathos.readback import (
    resolve_pin as readback_resolve_pin,
)
from bathos.runner import run_script
from bathos.sidecar import SidecarError
from bathos.sync import sync_catalog

# Telemetry imports
from bathos.telemetry import event, mcp_request_id_var, run_advisories_var, run_uuid_var
from bathos.telemetry_bridge import init_server_telemetry

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
    event(
        "mcp.tool_error", tool_name=tool_name, error_code=code.value, error_class=type(exc).__name__
    )
    return {
        "ok": False,
        "error_code": code.value,
        "error": str(exc),
        "resolution_hint": RESOLUTION_HINTS.get(code, ""),
    }


def _review_workspace() -> Path | None:
    """The live workspace root for postmortem amendments in a campaign review, or None."""
    try:
        from bathos.workspace import resolve_workspace

        return resolve_workspace().fs_root
    except Exception:  # noqa: BLE001 -- a review without amendments beats no review
        return None


def _breakdown_refusal_result(exc: BreakdownRefused) -> dict:
    """Error envelope for a tool that catches `BreakdownRefused` itself: the specific code
    (breakdown_missing / sidecar_unverifiable / fork_record_missing), never the generic one."""
    return {
        "ok": False,
        "error": str(exc),
        "error_code": exc.code.value,
        "resolution_hint": RESOLUTION_HINTS.get(exc.code, ""),
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
        event("mcp.call_start", tool=tool_name, request_id=request_id, arg_keys=list(kwargs.keys()))

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

            # Success path: fill in the four mandatory keys as defaults, but let the
            # wrapped function's own values win where it explicitly set them — a tool
            # that returns its own {"ok": False, "error": "..."} (rather than raising)
            # must not have that silently clobbered into a fake success.
            if isinstance(result, dict):
                merged = {
                    "ok": True,
                    "error_code": None,
                    "error": None,
                    "resolution_hint": None,
                    **result,
                }
                if "ok" not in result and result.get("error") is not None:
                    merged["ok"] = False
                return merged
            return {"ok": True, "error_code": None, "error": None, "resolution_hint": None}

        except McpAuthError as e:
            return _shape_error(tool_name, BathosErrorCode.AUTH_ERROR, e)
        except GateError as e:
            return _shape_error(tool_name, BathosErrorCode.INTERNAL, e)
        except CatalogError as e:
            return _shape_error(tool_name, BathosErrorCode.CATALOG_ERROR, e)
        except BreakdownRefused as e:
            return _shape_error(tool_name, e.code, e)
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
            event("mcp.call_end", tool=tool_name, request_id=request_id, duration_ms=duration_ms)
            mcp_request_id_var.reset(token)

    return wrapper


def require_write_token(fn):
    """Require a valid shared-secret MCP token on a write-verb tool (debt #619).

    Skipped when `bth-mcp` serves over stdio unless `BTH_MCP_REQUIRE_TOKEN=1`
    (debt #2841; see `bathos.mcp_auth.token_required`).

    Applied to every mutating MCP tool (anchor_insert, attestation_register,
    figure_entry_register, run, init, compact, archive, sync, repair, the
    scaffold/register/conclude family, etc. — see mcp_auth module docstring
    for why this is a token check rather than full RBAC). Read-only tools
    (figure_lookup, anchor_get, anchor_find, query_attestation, list_runs,
    ...) are NOT wrapped with this decorator and remain token-free.

    Must sit closer to the wrapped function than @traced_tool so that a
    missing/invalid token raises McpAuthError *inside* traced_tool's
    try/except, producing a normal structured error envelope
    (error_code="auth_error") instead of an unhandled exception.

    The decorated function must declare its own `token: str = ""` parameter
    so FastMCP's schema introspection (which follows `__wrapped__` through
    functools.wraps, same as traced_tool relies on today) exposes it to
    callers.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        if token_required() and not check_token(kwargs.get("token", "")):
            raise McpAuthError(
                "Missing or invalid MCP write token. Outside the stdio server (or with "
                "BTH_MCP_REQUIRE_TOKEN=1), write-verb MCP tools require token= to match "
                "the local ~/.bth/mcp_token file (debt #619, #2841)."
            )
        return await fn(*args, **kwargs)

    return wrapper


def _get_catalog_dir(catalog_dir: str | None = None) -> Path:
    """Resolve catalog directory from parameter, environment, or .bth.toml."""
    if catalog_dir:
        return Path(catalog_dir)
    override = os.environ.get("BTH_CATALOG_DIR")
    if override:
        return Path(override)
    config_path = find_project_config(Path.cwd())
    if config_path:
        return load_project_config(config_path).catalog_dir
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


@cisternal.tool(registry="bathos-cli", name="list_runs", cli_name="ls")
def list_runs_tool(
    catalog_dir: str = "",
    limit: int = 20,
    project: str = "",
    since: str = "",
    status: str = "",
) -> dict:
    """List recent runs from catalog.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        limit: Max runs to return (default 20). CLI aliases: --limit/-n.
        project: Filter by project slug. CLI aliases: --project/-p.
        since: Relative time filter, e.g. '7d' or '24h'
        status: Filter by run status

    Returns:
        Dict with runs array, count, the effective limit, and truncated (True when
        more matching runs exist than were returned)
    """
    from bathos.cli_common import parse_since

    cat_dir = _get_catalog_dir(catalog_dir or None)
    if project or since or status:
        runs = find_runs(
            cat_dir,
            since=parse_since(since or None),
            project=project or None,
            status=status or None,
        )
    else:
        # Fetch one extra row so truncation is detectable without a COUNT query.
        runs = list_runs(cat_dir, limit=limit + 1)
    truncated = len(runs) > limit
    runs = runs[:limit]
    runs_json = [
        {
            "id": r.id,
            "project_slug": r.project_slug,
            "command": r.command,
            "status": r.status,
            "outcome": r.outcome,
            "exit_code": r.exit_code,
            "duration_s": r.duration_s,
            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
        }
        for r in runs
    ]
    return {
        "runs": runs_json,
        "count": len(runs_json),
        "limit": limit,
        "truncated": truncated,
    }


# Same wire()-closure/PEP-563 workaround as run_cli_tool's `args` above: a
# source-level Annotated[...] here would be stored as a string (this module
# has `from __future__ import annotations`) and fail with NameError once
# cyclopts resolves it against wired.py's globals, which don't import
# cyclopts/Annotated. Mutating __annotations__ after the def stores the
# already-evaluated object instead, restoring the old Typer `ls` command's
# -n/-p short-flag aliases.
list_runs_tool.__annotations__["limit"] = Annotated[
    int, cyclopts.Parameter(name=["--limit", "-n"])
]
list_runs_tool.__annotations__["project"] = Annotated[
    str, cyclopts.Parameter(name=["--project", "-p"])
]


@cisternal.tool(registry="bathos-cli", name="find_runs", cli_name="find")
def find_runs_tool(
    catalog_dir: str = "",
    project: str = "",
    tags: list[str] | None = None,
    since: str = "",
    status: str = "",
    slurm_job: str = "",
    output_file: str = "",
) -> dict:
    """Find runs matching filters.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        project: Project-slug filter (exact match against project_slug)
        tags: Optional tag filter — matches runs with any of these tags
        since: Relative time filter, e.g. '7d' or '24h'
        status: Filter by run status
        slurm_job: Filter by SLURM job ID
        output_file: Filter to runs with a matching output file path (glob pattern)

    Returns:
        Dict with matching runs
    """
    from bathos.cli_common import parse_since
    from bathos.query import _filter_runs_by_output_file

    cat_dir = _get_catalog_dir(catalog_dir or None)
    runs = find_runs(
        cat_dir,
        since=parse_since(since or None),
        project=project or None,
        status=status or None,
        tags=tags or None,
        slurm_job_id=slurm_job or None,
    )
    if output_file:
        runs = _filter_runs_by_output_file(runs, pattern=output_file)
    runs_json = [
        {
            "id": r.id,
            "project_slug": r.project_slug,
            "command": r.command,
            "status": r.status,
            "outcome": r.outcome,
            "exit_code": r.exit_code,
            "duration_s": r.duration_s,
            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            "tags": r.tags,
        }
        for r in runs
    ]
    return {"runs": runs_json, "count": len(runs_json)}


@cisternal.tool(registry="bathos-cli", name="get_run", cli_name="show")
def get_run_tool(
    run_id: str = "",
    catalog_dir: str = "",
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
    # Display path only: other get_run callers must not see uncompacted runs.
    run = get_run(run_id, cat_dir, allow_cool_fallback=True)
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
        # Verification fields (rule: verify a run by its record, not its exit
        # code). Empty strings are emitted as-is so an unset value is explicit.
        "outcome": run.outcome,
        "sidecar_mode": run.sidecar_mode,
        "sidecar_path": run.sidecar_path,
        "sidecar_sha256": run.sidecar_sha256,
    }
    if run_source(run) == COOL_UNINDEXED_SOURCE:
        notice = (
            "not yet compacted: fields computed at compact time "
            "(output_metadata, postmortem overrides) are absent"
        )
        run_dict["notice"] = notice
    if run.status == "running":
        # Advisory progress (#105): present only while running and only when a usable file exists.
        progress = read_progress(progress_path_for(run.id))
        if progress is not None:
            run_dict["progress"] = progress
    return run_dict


@cisternal.tool(registry="bathos-cli", name="run_sql", cli_name="sql")
def run_sql_tool(
    sql: str = "",
    catalog_dir: str = "",
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
    cat_dir = _get_catalog_dir(catalog_dir or None)
    try:
        rows = run_sql(sql, cat_dir)
    except RuntimeError as e:
        return {"error": str(e)}
    rows_json = [list(row) for row in rows]
    return {"rows": rows_json, "count": len(rows_json)}


# ============================================================================
# Read-back / query API (S1) — bathos.readback adapter
#
# resolve_pin / read_campaign_report / read_figure_manifest are REAL (wrap existing
# catalog + disk readers). figure_lookup is now REAL too, backed by the S2 sidecar
# anchor store (bathos.anchor, item 3483). query_attestation is now REAL too, backed by
# the S4 attestation sidecar store (bathos.attestation, item 3492) — see below.
# get_trust_state / list_candidates are now REAL too, backed by the durable trust
# ledger (bathos.trust_ledger, S3, item 3491) — see bathos.readback module docstring.
# ============================================================================


@cisternal.tool(registry="bathos-cli", name="resolve_pin", cli_group="query", cli_name="resolve-pin")
def resolve_pin_tool(
    run_id: str = "",
    output_path: str = "",
    catalog_dir: str = "",
) -> dict:
    """Resolve a run_id + output_path pin to content_hash/trust_state/freshness.

    Args:
        run_id: Run ID that produced (or is expected to produce) the output.
        output_path: Path to the output file, as recorded in the run.
        catalog_dir: Catalog directory (empty = use default)

    Returns:
        Dict with content_hash, trust_state, fresh (plus run_id/output_path echoed back).
    """
    if not run_id:
        return {"error": "run_id is required"}
    if not output_path:
        return {"error": "output_path is required"}
    cat_dir = _get_catalog_dir(catalog_dir or None)
    try:
        pin = readback_resolve_pin(cat_dir, run_id, output_path)
    except CatalogError as e:
        return {"error": str(e)}
    return dataclasses.asdict(pin)


@cisternal.tool(registry="bathos-cli", name="get_trust_state", cli_group="query", cli_name="trust-state")
def get_trust_state_tool(
    content_hash: str = "",
    catalog_dir: str = "",
) -> dict:
    """Look up the trust_state (unknown/candidate/promoted) of a product by content_hash.

    REAL as of seam S3 (bathos.trust_ledger, backlog item 3491) — see
    bathos.readback.get_trust_state's docstring for the 3-state model.

    Args:
        content_hash: Content hash of the product to look up.
        catalog_dir: Catalog directory (empty = use default)

    Returns:
        Dict with content_hash and trust_state.
    """
    if not content_hash:
        return {"error": "content_hash is required"}
    cat_dir = _get_catalog_dir(catalog_dir or None)
    return {
        "content_hash": content_hash,
        "trust_state": str(readback_get_trust_state(cat_dir, content_hash)),
    }


@cisternal.tool(registry="bathos-cli", name="query_attestation", cli_group="query", cli_name="attestation")
def query_attestation_tool(
    content_hash: str = "",
    min_strength: str = "",
    catalog_dir: str = "",
) -> dict:
    """Look up a verdict-checked attestation for a product by content_hash.

    REAL as of seam S4 (bathos.attestation, backlog item 3492): backed by the
    attestation sidecar kind (oracle_match / repro_floor). Returns attestation=None
    unless a matching-strength PASS attestation has been registered (WARN/FAIL
    attestations are present in the catalog but never satisfy this query) — see
    bathos.readback.query_attestation docstring.

    Args:
        content_hash: Content hash of the attested product.
        min_strength: Minimum required certification strength ('oracle_match' or
            'repro_floor'); empty = no minimum.
        catalog_dir: Catalog directory (empty = use default)

    Returns:
        Dict with content_hash and attestation (None if no qualifying PASS exists).
    """
    if not content_hash:
        return {"error": "content_hash is required"}
    cat_dir = _get_catalog_dir(catalog_dir or None)
    attestation = readback_query_attestation(cat_dir, content_hash, min_strength or None)
    return {"content_hash": content_hash, "attestation": attestation}


def read_campaign_report_tool(
    campaign_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """Read the campaign_report.json sidecar for a campaign.

    Real: wraps CampaignReport.read_report (previously Python-only, no tool surface).

    Args:
        campaign_id: Full campaign ID (must match the sidecar directory name exactly).
        catalog_dir: Catalog directory (empty = use default)

    Returns:
        Dict with the campaign report fields, or an error if not yet emitted.
    """
    if not campaign_id:
        return {"error": "campaign_id is required"}
    cat_dir = _get_catalog_dir(catalog_dir or None)
    try:
        report = readback_read_campaign_report(cat_dir, campaign_id)
    except FileNotFoundError as e:
        return {"error": str(e)}
    return report.model_dump()


def read_figure_manifest_tool(
    campaign_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """Read the figure_manifest.json sidecar for a campaign.

    Real: wraps FigureManifest.read_manifest (previously Python-only, no tool surface).

    Args:
        campaign_id: Full campaign ID (must match the sidecar directory name exactly).
        catalog_dir: Catalog directory (empty = use default)

    Returns:
        Dict with the figure manifest fields, or an error if not yet emitted.
    """
    if not campaign_id:
        return {"error": "campaign_id is required"}
    cat_dir = _get_catalog_dir(catalog_dir or None)
    try:
        manifest = readback_read_figure_manifest(cat_dir, campaign_id)
    except FileNotFoundError as e:
        return {"error": str(e)}
    return manifest.model_dump()


@cisternal.tool(registry="bathos-cli", name="figure_lookup", cli_group="query", cli_name="figures")
def figure_lookup_tool(
    asset_sha256: str = "",
    input_hash: str = "",
    catalog_dir: str = "",
) -> dict:
    """Look up figure registry entries by asset_sha256 or input_hash.

    REAL as of seam S2 (bathos.anchor, item 3483): backed by the generic sidecar
    anchor store, filtered to kind="figure". Returns [] until a producer anchors a
    matching figure — see bathos.readback.figure_lookup docstring.

    Args:
        asset_sha256: Anchor key of the rendered figure asset.
        input_hash: Content hash of the underlying data product.
        catalog_dir: Catalog directory (empty = use default)

    Returns:
        Dict with figures array (empty if nothing anchored/matching yet).
    """
    cat_dir = _get_catalog_dir(catalog_dir or None)
    figures = readback_figure_lookup(
        cat_dir,
        asset_sha256=asset_sha256 or None,
        input_hash=input_hash or None,
    )
    return {"figures": figures, "count": len(figures)}


@cisternal.tool(registry="bathos-cli", name="list_candidates", cli_group="query", cli_name="candidates")
def list_candidates_tool(
    campaign_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """List candidate-tier (not-yet-promoted) products for a campaign.

    REAL as of seam S3 (bathos.trust_ledger, backlog item 3491) — see
    bathos.readback.list_candidates's docstring for the anchors+runs join.

    Args:
        campaign_id: Campaign ID to list candidates for.
        catalog_dir: Catalog directory (empty = use default)

    Returns:
        Dict with candidates array (empty until S3 ships).
    """
    if not campaign_id:
        return {"error": "campaign_id is required"}
    cat_dir = _get_catalog_dir(catalog_dir or None)
    candidates = readback_list_candidates(cat_dir, campaign_id)
    return {"campaign_id": campaign_id, "candidates": candidates, "count": len(candidates)}


# ============================================================================
# Blast-radius assessment (backlog #4551) — bathos.blast_radius adapter
#
# Answers "a bug was found/fixed here -- which past runs does it implicate?" via
# commit / commit-range / file anchors. Phase 1: run-level flagging only, manual
# invocation, no gating. See .praxia/docs/specs/260826_blast-radius-assessment-skill.md.
# ============================================================================


@cisternal.tool(
    registry="bathos-cli", name="blast_radius_assess", cli_group="blast-radius", cli_name="assess"
)
def blast_radius_assess_tool(
    catalog_dir: str = "",
    project_root: str = "",
    commit: str = "",
    commit_range: str = "",
    files: str = "",
    dependency: bool = False,
    project: str = "",
    flag: bool = True,
) -> dict:
    """Assess which runs a bug/fix implicates (backlog #4551/#4552; real).

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841) at the MCP
    wrapper layer — see mcp_blast_radius_assess_tool. Gated because flag=True by
    default performs a durable ledger write (security-audit finding, PR #54).

    Args:
        catalog_dir: Catalog directory (empty = use default).
        project_root: Git repo to diff/query ancestry against (empty = cwd).
        commit: Single fix commit SHA anchor.
        commit_range: "<base>..<tip>" range anchor.
        files: Comma-separated file/symbol path anchor (no ancestry check).
        dependency: If True, dependency-lock-drift anchor (AC-3) instead of a
            commit/file anchor.
        project: Optional project_slug filter (empty = whole catalog).
        flag: If True (default), also durably record affected/unverifiable runs
            in the blast_radius_ledger (AC-6), and propagate to campaign/claim
            records (AC-7/AC-8). Exactly one of commit/commit_range/files/
            dependency=True must be given.

    Returns:
        Dict with anchor_kind/anchor_value/changed_files/affected/unverifiable/
        unaffected_run_ids/warnings (report; warnings lists coverage caveats such
        as a file anchor that matched no run, which is unverified rather than
        clean, and a locked-catalog fallback), plus flagged_count/campaign_flagged_count/
        claim_flagged_count if flag=True. Or {"error": ...} if the anchor
        arguments are malformed.
    """
    import dataclasses

    from bathos.blast_radius import (
        assess_blast_radius,
        flag_blast_radius,
        propagate_to_campaigns,
        propagate_to_claims,
    )

    n = sum(bool(x) for x in (commit, commit_range, files)) + int(dependency)
    if n != 1:
        return {
            "error": "exactly one of commit, commit_range, files, or dependency=True is required"
        }

    cat_dir = _get_catalog_dir(catalog_dir or None)
    proj_root = Path(project_root) if project_root else Path.cwd()
    file_list = [f.strip() for f in files.split(",") if f.strip()] or None

    try:
        report = assess_blast_radius(
            cat_dir,
            proj_root,
            commit=commit or None,
            commit_range=commit_range or None,
            files=file_list,
            dependency=dependency,
            project=project or None,
        )
    except ValueError as e:
        return {"error": str(e)}

    result = dataclasses.asdict(report)
    if flag:
        from bathos.runlog.emit import unit_of_work

        # One unit of work across flag + both propagations: the mode is read once.
        with unit_of_work(Path(cat_dir)):
            records = flag_blast_radius(report, cat_dir)
            result["flagged_count"] = len(records)
            campaign_records = propagate_to_campaigns(report, cat_dir)
            result["campaign_flagged_count"] = len(campaign_records)
            claim_records = propagate_to_claims(report, cat_dir, workspace_root=proj_root)
            result["claim_flagged_count"] = len(claim_records)
    return result


@cisternal.tool(
    registry="bathos-cli", name="blast_radius_clear", cli_group="blast-radius", cli_name="clear"
)
def blast_radius_clear_tool(
    entity_type: str = "",
    entity_id: str = "",
    reason: str = "",
    catalog_dir: str = "",
) -> dict:
    """Manually clear a blast-radius flag (backlog #4551, AC-9; real).

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841) at the MCP
    wrapper layer — see mcp_blast_radius_clear_tool.

    Args:
        catalog_dir: Catalog directory (empty = use default).
        entity_type: 'run', 'campaign', or 'claim'.
        entity_id: Entity ID to clear.
        reason: Required non-empty justification.

    Returns:
        Dict with the appended ledger record, or {"error": ...} if reason is empty.
    """
    import dataclasses

    from bathos.blast_radius import clear_blast_radius_flag

    if not entity_type or not entity_id:
        return {"error": "entity_type and entity_id are required"}
    cat_dir = _get_catalog_dir(catalog_dir or None)
    try:
        record = clear_blast_radius_flag(cat_dir, entity_type, entity_id, reason=reason)
    except ValueError as e:
        return {"error": str(e)}
    return dataclasses.asdict(record)


@cisternal.tool(
    registry="bathos-cli",
    name="get_blast_radius_status",
    cli_group="query",
    cli_name="blast-status",
)
def get_blast_radius_status_tool(
    entity_type: str = "",
    entity_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """Look up blast-radius status for an entity (backlog #4551; real).

    Args:
        catalog_dir: Catalog directory (empty = use default).
        entity_type: 'run', 'campaign', or 'claim'.
        entity_id: Entity ID to look up.

    Returns:
        Dict with entity_type, entity_id, and status
        (clean/affected/unverifiable/cleared).
    """
    from bathos.blast_radius import fold_blast_radius_state

    if not entity_type or not entity_id:
        return {"error": "entity_type and entity_id are required"}
    cat_dir = _get_catalog_dir(catalog_dir or None)
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "status": fold_blast_radius_state(cat_dir, entity_type, entity_id),
    }


# ============================================================================
# Anchor-insert WRITE API (S2) — bathos.anchor adapter (backlog item 3483)
#
# Generic sidecar anchor by (path, sha256), mirroring the claim_register pattern
# (bathos.claim.register_claim) but not tied to the claim schema. Composes with the
# S1 read-back seam: figure_lookup now resolves kind="figure" anchors registered here.
# Makes NO durability or cross-boundary-callability guarantee — see bathos.anchor
# module docstring.
# ============================================================================


@cisternal.tool(registry="bathos-cli", name="anchor_insert", cli_group="anchor", cli_name="insert")
def anchor_insert_tool(
    path: str = "",
    sha256: str = "",
    kind: str = "",
    label: str = "",
    content_hash: str = "",
    campaign_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """Anchor a sidecar by (path, sha256) into the catalog (S2 write seam; real).

    Args:
        path: Path to the sidecar file (not resolved/verified against disk).
        sha256: SHA256 of the sidecar file's contents.
        kind: Free-form anchor kind, e.g. "figure", "attestation".
        label: Optional human-readable label.
        content_hash: Optional hash of the underlying data product.
        campaign_id: Optional campaign this anchor belongs to.
        catalog_dir: Catalog directory (empty = use default)

    Returns:
        Dict with ok + the inserted anchor record, or ok=False + error.
    """
    if not path:
        return {"ok": False, "error": "path is required"}
    if not sha256:
        return {"ok": False, "error": "sha256 is required"}
    if not kind:
        return {"ok": False, "error": "kind is required"}
    cat_dir = _get_catalog_dir(catalog_dir or None)
    record = register_anchor(
        cat_dir,
        path,
        sha256,
        kind,
        label=label or None,
        content_hash=content_hash or None,
        campaign_id=campaign_id or None,
    )
    return {"ok": True, "anchor": dataclasses.asdict(record)}


@cisternal.tool(registry="bathos-cli", name="anchor_get", cli_group="anchor", cli_name="get")
def anchor_get_tool(
    path: str = "",
    sha256: str = "",
    catalog_dir: str = "",
) -> dict:
    """Read back an anchored sidecar by (path, sha256) (S2; real; round-trips insert).

    Args:
        path: Path used at anchor time.
        sha256: SHA256 used at anchor time.
        catalog_dir: Catalog directory (empty = use default)

    Returns:
        Dict with ok + anchor (None if not found), or ok=False + error.
    """
    if not path:
        return {"ok": False, "error": "path is required"}
    if not sha256:
        return {"ok": False, "error": "sha256 is required"}
    cat_dir = _get_catalog_dir(catalog_dir or None)
    record = get_anchor(cat_dir, path, sha256)
    return {"ok": True, "anchor": dataclasses.asdict(record) if record else None}


@cisternal.tool(registry="bathos-cli", name="anchor_find", cli_group="anchor", cli_name="find")
def anchor_find_tool(
    kind: str = "",
    sha256: str = "",
    content_hash: str = "",
    campaign_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """Find anchored sidecars matching filters (S2; real).

    Args:
        kind: Filter by anchor kind (e.g. "figure"); empty = no filter.
        sha256: Filter by the anchor's own sha256; empty = no filter.
        content_hash: Filter by the underlying data product's hash; empty = no filter.
        campaign_id: Filter by campaign; empty = no filter.
        catalog_dir: Catalog directory (empty = use default)

    Returns:
        Dict with anchors array + count.
    """
    cat_dir = _get_catalog_dir(catalog_dir or None)
    records = find_anchors(
        cat_dir,
        kind=kind or None,
        sha256=sha256 or None,
        content_hash=content_hash or None,
        campaign_id=campaign_id or None,
    )
    anchors = [dataclasses.asdict(r) for r in records]
    return {"anchors": anchors, "count": len(anchors)}


# ============================================================================
# Figure registry WRITE API (S7) — bathos.figure_registry adapter (backlog item 3490)
#
# Typed, pointer-only figure_entry, built on the S2 anchor-insert seam. Registers
# under kind="figure_entry" (distinct from S2's bare kind="figure"); figure_lookup
# (above) composes both. Rejects any inline verdict/strength/content_hash/outcome/gate
# field — see bathos.figure_registry module docstring.
# ============================================================================


@cisternal.tool(
    registry="bathos-cli",
    name="figure_entry_register",
    cli_group="anchor",
    cli_name="figure-register",
)
def figure_entry_register_tool(
    asset_sha256: str = "",
    sidecar_ref: str = "",
    figure_kind: str = "",
    render_state: str = "ready",
    fig_trust_state: str = "draft",
    attestation_ref: str = "",
    input_hash: str = "",
    campaign_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """Register a typed figure_entry, anchored by asset_sha256 (S7 write seam; real).

    Args:
        asset_sha256: Anchor key — SHA256 of the rendered figure asset.
        sidecar_ref: Pointer to the `.figure.toml` sidecar describing this figure.
        figure_kind: Free-form figure kind (e.g. 'chord_diagram').
        render_state: 'ready' or 'deferred'.
        fig_trust_state: 'draft' or 'final'.
        attestation_ref: Optional POINTER (sha256) to a bathos-side attestation.
        input_hash: Optional hash of the underlying data product (query key only —
            never a field of the returned entry).
        campaign_id: Optional campaign this figure belongs to.
        catalog_dir: Catalog directory (empty = use default)

    Returns:
        Dict with ok + the registered figure_entry, or ok=False + error (e.g. a
        rejected forbidden inline field).
    """
    if not asset_sha256:
        return {"ok": False, "error": "asset_sha256 is required"}
    if not sidecar_ref:
        return {"ok": False, "error": "sidecar_ref is required"}
    if not figure_kind:
        return {"ok": False, "error": "figure_kind is required"}
    cat_dir = _get_catalog_dir(catalog_dir or None)
    try:
        entry = register_figure_entry(
            cat_dir,
            asset_sha256=asset_sha256,
            sidecar_ref=sidecar_ref,
            figure_kind=figure_kind,
            render_state=render_state,
            fig_trust_state=fig_trust_state,
            attestation_ref=attestation_ref or None,
            input_hash=input_hash or None,
            campaign_id=campaign_id or None,
        )
    except (FigureEntrySchemaError, TypeError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "figure_entry": dataclasses.asdict(entry)}


# ============================================================================
# Attestation sidecar (S4) — bathos.attestation adapter (backlog item 3492)
#
# A SEPARATE, analogous anchored sidecar kind (oracle_match / repro_floor), NOT an
# extension of the claim system's literature_parity attestation path (bathos.claim) —
# see gate #3488 sign-off
# (`.praxia/docs/decisions/260714_spike-claim-schema-extensibility.md`, maraxiom repo)
# and the bathos.attestation module docstring. Reuses the scaffold/register/validate
# pattern + SHA256-anchor-at-registration shape, and the S2 anchor-insert seam
# (register_anchor via DurableAnchorStore for durability), but shares no code, table,
# or column with bathos.claim. These are plain, synchronously-callable functions (the
# @cisternal.tool-decorated async wrappers below just forward to them), mirroring the S2
# anchor tool convention (anchor_insert_tool / anchor_get_tool / anchor_find_tool)
# rather than the claim tools' async-only style, per the dispatch brief ("Wire
# register + query on MCP + CLI following the anchor/query patterns").
# ============================================================================


@cisternal.tool(
    registry="bathos-cli", name="attestation_scaffold", cli_group="attestation", cli_name="scaffold"
)
def attestation_scaffold_tool(
    kind: str = "",
    workspace_root: str = "",
    label: str = "",
) -> dict:
    """Scaffold a new attestation.bth.toml template (S4; real).

    Args:
        kind: "oracle_match" or "repro_floor".
        workspace_root: Project workspace root (empty = resolve live fs_root).
        label: Optional filename label.

    Returns:
        Dict with ok + path, or ok=False + error.
    """
    from bathos.attestation import scaffold_attestation

    if workspace_root:
        ws = Path(workspace_root).expanduser().resolve()
    else:
        from bathos.workspace import resolve_workspace

        ws = resolve_workspace().fs_root

    try:
        path = scaffold_attestation(kind, ws, label=label or None)
        return {"ok": True, "path": str(path), "kind": kind}
    except ValueError as e:
        return {"ok": False, "error": str(e), "error_code": "invalid_kind"}


@cisternal.tool(
    registry="bathos-cli", name="attestation_validate", cli_group="attestation", cli_name="validate"
)
def attestation_validate_tool(
    path: str = "",
) -> dict:
    """Validate an attestation TOML file's kind-specific required fields (S4; real).

    Args:
        path: Path to the attestation TOML file.

    Returns:
        Dict with ok + kind/verdict/warnings, or ok=False + errors.
    """
    from bathos.attestation import parse_attestation, validate_attestation

    attestation_path = Path(path)
    if not attestation_path.exists():
        return {"ok": False, "error": f"File not found: {path}"}

    try:
        attestation = parse_attestation(attestation_path)
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e), "error_code": "file_not_found"}
    except ValueError as e:
        return {"ok": False, "error": str(e), "error_code": "parse_error"}

    result = validate_attestation(attestation)
    if result.ok:
        return {
            "ok": True,
            "path": path,
            "kind": attestation.kind,
            "verdict": attestation.verdict,
            "warnings": result.warnings,
        }
    return {
        "ok": False,
        "errors": [e.message for e in result.errors],
        "warnings": result.warnings,
        "error_code": "validation_failed",
    }


@cisternal.tool(
    registry="bathos-cli", name="attestation_register", cli_group="attestation", cli_name="register"
)
def attestation_register_tool(
    path: str = "",
    catalog_dir: str = "",
    campaign_id: str = "",
) -> dict:
    """Register an attestation file: anchor it by its own SHA256 (S4; real; durable by
    default via DurableAnchorStore).

    Args:
        path: Path to the attestation TOML file to register.
        catalog_dir: Catalog directory (empty = use default).
        campaign_id: Optional campaign this attestation belongs to.

    Returns:
        Dict with ok + the inserted anchor record, or ok=False + error.
    """
    from bathos.attestation import register_attestation

    if not path:
        return {"ok": False, "error": "path is required"}
    cat_dir = _get_catalog_dir(catalog_dir or None)
    try:
        record = register_attestation(Path(path), cat_dir, campaign_id=campaign_id or None)
        return {"ok": True, "anchor": dataclasses.asdict(record)}
    except (FileNotFoundError, ValueError) as e:
        return {"ok": False, "error": str(e), "error_code": "register_failed"}


def graduate_product_tool(
    content_hash: str = "",
    attestation_ref: str = "",
    catalog_dir: str = "",
    min_strength: str = "",
    run_id: str = "",
    output_path: str = "",
    reason: str = "",
) -> dict:
    """Graduate a product from candidate to promoted in the trust ledger (S3; real).

    Args:
        content_hash: Content hash of the product to graduate.
        attestation_ref: Caller-supplied reference recorded on the ledger entry for
            audit purposes (e.g. the attestation's own sha256) — NOT itself trusted
            as proof; graduate_product independently re-queries query_attestation.
        catalog_dir: Catalog directory (empty = use default).
        min_strength: Minimum attestation strength required ("oracle_match" or
            "repro_floor"); empty = either strength qualifies.
        run_id: Optional run_id to record on the ledger entry.
        output_path: Optional output_path to record on the ledger entry.
        reason: Optional free-form reason/justification to record.

    Returns:
        Dict with ok + the appended (or, if already promoted, existing) ledger
        record, or ok=False + error_code="graduation_refused" if no PASS
        attestation exists for content_hash at the requested min_strength.
    """
    from bathos.trust_ledger import GraduationRefused, graduate_product

    if not content_hash or not attestation_ref:
        return {"ok": False, "error": "content_hash and attestation_ref are required"}
    cat_dir = _get_catalog_dir(catalog_dir or None)
    try:
        record = graduate_product(
            cat_dir,
            content_hash,
            attestation_ref,
            min_strength=min_strength or None,
            run_id=run_id or None,
            output_path=output_path or None,
            reason=reason or None,
        )
        return {"ok": True, "record": dataclasses.asdict(record)}
    except GraduationRefused as e:
        return {"ok": False, "error": str(e), "error_code": "graduation_refused"}


@cisternal.tool(registry="bathos-cli", name="compact", cli_name="compact")
def compact_tool(
    catalog_dir: str = "",
    force_rebuild: bool = False,
    strict: bool = False,
) -> dict:
    """Compact cool-tier Parquet to warm-tier DuckDB.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        force_rebuild: Rebuild bathos.db from cool fragments if corrupt
        strict: Exit non-zero if any cool-tier fragment is corrupt/unreadable
            (default: skip corrupt fragments and report them, so maintenance
            always completes)

    Returns:
        Dict with result summary
    """
    cat_dir = _get_catalog_dir(catalog_dir or None)
    result = compact_catalog(cat_dir, force_rebuild=force_rebuild)
    result_dict = {
        "ingested": result.ingested,
        "skipped": result.skipped,
        "duration_s": result.duration_s,
        "corrupt_fragments": [str(cf.path) for cf in result.corrupt_fragments],
    }
    if result.log_mode:
        result_dict["log_mode"] = True
        result_dict["ingest_ran"] = result.ingest_ran
        result_dict["quarantined"] = result.quarantined
        if not result.ingest_ran:
            result_dict["note"] = (
                "log mode: ingest skipped (SLURM_JOB_ID set or BTH_NO_INGEST=1); "
                "index.db was not refreshed"
            )
        else:
            result_dict["note"] = (
                "log mode: folded new log events into index.db "
                "(ingested = new events, skipped = already-known events)"
            )
    if result.corrupt_fragments and strict:
        # Singular "error" key -- see cli_render.render_or_exit -- matches the shipped
        # Typer `compact --strict` command's `if strict: raise typer.Exit(1)` behavior.
        result_dict["error"] = (
            f"{len(result.corrupt_fragments)} corrupt/unreadable fragment(s) found "
            "(--strict). Run 'bth repair --tier cool' to quarantine them."
        )
    return result_dict


@cisternal.tool(registry="bathos-cli", name="archive", cli_name="archive")
def archive_tool(
    catalog_dir: str = "",
    project: str = "",
    archive_dir: str = "",
    dry_run: bool = False,
) -> dict:
    """Archive warm-tier DuckDB to cold-tier Parquet.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        project: Project slug to archive (empty = all projects, matching the shipped
            Typer `archive --project` flag's own optional default)
        archive_dir: Archive root directory (empty = ~/.bth/archive)
        dry_run: Show what would be archived without writing

    Returns:
        Dict with result summary
    """
    cat_dir = _get_catalog_dir(catalog_dir or None)
    if not cat_dir.exists():
        return {"error": "Catalog not found"}
    archive_root = Path(archive_dir) if archive_dir else None
    result = archive_runs(
        cat_dir, archive_root=archive_root, project_slug=project or None, dry_run=dry_run
    )
    result_dict = {
        "runs_archived": result.runs_archived,
        "partitions_created": result.partitions_created,
        "archive_size_bytes": result.archive_size_bytes,
        "duration_s": result.duration_s,
    }
    return result_dict


@cisternal.tool(registry="bathos-cli", name="archive_artifact", cli_name="archive-artifact")
def archive_artifact_tool(
    script_path: str = "",
    project_root: str = "",
    catalog_dir: str = "",
    project_slug: str = "",
    verdict: str = "",
    reason: str = "",
    superseded_by: str = "",
    dry_run: bool = False,
) -> dict:
    """Archive a script + its sidecar + its tracked outputs (stub-in-place, git-native).

    Never invoked automatically -- lint_tool's check_archival_candidates only proposes
    candidates; this is the explicit action a caller takes.

    Args:
        script_path: Path to the script to archive
        project_root: Project root directory (empty = current directory)
        catalog_dir: Catalog directory (empty = use default)
        project_slug: Project slug (default: 'default')
        verdict: One-sentence verdict on this bundle
        reason: Why this is being archived
        superseded_by: Run id / claim id / archive item id that supersedes this
        dry_run: Show what would be archived without writing

    Returns:
        Dict with the archived item's fields, or {"error": ..., "error_code": ...} on failure
    """
    from bathos.artifact_archive import (
        ArchiveError,
        ArtifactNotFoundError,
        DirtyTreeError,
        archive_experiment_bundle,
    )

    if not script_path:
        return {
            "error": "script_path parameter is required",
            "error_code": BathosErrorCode.INVALID_PARAM.value,
            "resolution_hint": RESOLUTION_HINTS[BathosErrorCode.INVALID_PARAM],
        }
    if not verdict or not reason:
        return {
            "error": "verdict and reason parameters are required",
            "error_code": BathosErrorCode.INVALID_PARAM.value,
            "resolution_hint": RESOLUTION_HINTS[BathosErrorCode.INVALID_PARAM],
        }

    cat_dir = _get_catalog_dir(catalog_dir or None)
    proj_root = Path(project_root) if project_root else Path.cwd()
    # Hard-fail on a missing slug, like `run` -- see require_project_slug's
    # call in run_tool above for the same rationale (soft_project_slug's own
    # docstring documents `archive-artifact` as slug-required, unlike `lint`).
    slug = project_slug or require_project_slug()

    try:
        item = archive_experiment_bundle(
            project_root=proj_root,
            script_path=Path(script_path),
            catalog_dir=cat_dir,
            project_slug=slug,
            verdict=verdict,
            reason=reason,
            superseded_by=superseded_by,
            dry_run=dry_run,
        )
    except DirtyTreeError as e:
        code = BathosErrorCode.ARCHIVE_DIRTY_TREE
        return {
            "error": str(e),
            "error_code": code.value,
            "resolution_hint": RESOLUTION_HINTS[code],
        }
    except ArtifactNotFoundError as e:
        code = BathosErrorCode.ARCHIVE_ITEM_NOT_FOUND
        return {
            "error": str(e),
            "error_code": code.value,
            "resolution_hint": RESOLUTION_HINTS[code],
        }
    except ArchiveError as e:
        code = BathosErrorCode.ARCHIVE_ERROR
        return {
            "error": str(e),
            "error_code": code.value,
            "resolution_hint": RESOLUTION_HINTS[code],
        }

    return {"ok": True, "item": dataclasses.asdict(item)}


@cisternal.tool(registry="bathos-cli", name="restore", cli_name="restore")
def restore_tool(
    item_id: str = "",
    project_root: str = "",
    catalog_dir: str = "",
    dry_run: bool = False,
    stub_path: str = "",
) -> dict:
    """Restore a previously archived script+output bundle.

    Args:
        item_id: Archived item id
        project_root: Project root directory (empty = current directory)
        catalog_dir: Catalog directory (empty = use default)
        dry_run: Show what would be restored without writing
        stub_path: Path to one of the stub files bth archive-artifact wrote, used to
            recover via its own embedded pre_archive_sha when there's no local
            ~/.bth/catalog record for item_id (e.g. a fresh clone). Without a ledger,
            restores only this one file.

    Returns:
        Dict with restored paths, or {"error": ..., "error_code": ...} on failure
    """
    from bathos.artifact_archive import ArchiveError, ArtifactNotFoundError, restore_archived_item

    if not item_id:
        return {
            "error": "item_id parameter is required",
            "error_code": BathosErrorCode.INVALID_PARAM.value,
            "resolution_hint": RESOLUTION_HINTS[BathosErrorCode.INVALID_PARAM],
        }

    cat_dir = _get_catalog_dir(catalog_dir or None)
    proj_root = Path(project_root) if project_root else Path.cwd()

    try:
        result = restore_archived_item(
            project_root=proj_root,
            item_id=item_id,
            catalog_dir=cat_dir,
            dry_run=dry_run,
            stub_path=Path(stub_path) if stub_path else None,
        )
    except ArtifactNotFoundError as e:
        code = BathosErrorCode.ARCHIVE_ITEM_NOT_FOUND
        return {
            "error": str(e),
            "error_code": code.value,
            "resolution_hint": RESOLUTION_HINTS[code],
        }
    except ArchiveError as e:
        code = BathosErrorCode.ARCHIVE_ERROR
        return {
            "error": str(e),
            "error_code": code.value,
            "resolution_hint": RESOLUTION_HINTS[code],
        }

    return {"ok": True, "result": dataclasses.asdict(result)}


@cisternal.tool(registry="bathos-cli", name="reap", cli_group="catalog", cli_name="reap")
def reap_tool(
    catalog_dir: str = "",
    older_than_h: float = 24,
    dry_run: bool = True,
    apply: bool = False,
    revert: bool = False,
    revert_ids: str = "",
) -> dict:
    """Reap orphaned runs by marking them abandoned.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        older_than_h: Minimum age in hours to reap (default 24, floor enforced at 24)
        dry_run: Show what would be reaped without writing (default True)
        apply: Actually write reaped runs (default False)
        revert: Restore prior_status instead of marking abandoned
        revert_ids: Comma-separated run IDs to revert (only with --revert)

    Returns:
        Dict with reap results
    """
    from bathos.reap import reap_runs, ReapError  # noqa: I001

    cat_dir = _get_catalog_dir(catalog_dir or None)

    try:
        revert_ids_list = [x.strip() for x in revert_ids.split(",")] if revert_ids else []
        candidates, skipped = reap_runs(
            cat_dir,
            older_than_h=older_than_h,
            dry_run=dry_run,
            apply=apply,
            revert=revert,
            revert_ids=revert_ids_list if revert else None,
        )
    except ReapError as e:
        return {
            "error": str(e),
            "error_code": BathosErrorCode.INVALID_PARAM.value,
        }

    # No warm-tier reconcile here: reap_runs does it itself (legacy only).
    skipped_list = [{"run_id": r.id, "reason": reason} for r, reason in skipped]

    return {
        "candidates": len(candidates),
        "reaped": len(candidates) if apply else 0,
        "skipped": len(skipped),
        "skipped_details": skipped_list,
        "candidate_ids": [c.id for c in candidates],
    }


@cisternal.tool(registry="bathos-cli", name="check", cli_name="check")
def check_tool(
    catalog_dir: str = "",
    project_root: str = "",
    status_filter: str = "",
    check_outputs: bool = False,
    rebaseline: bool = False,
) -> dict:
    """Check run freshness vs git HEAD.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        project_root: Project root directory (empty = current directory)
        status_filter: Filter by status (e.g., "stale")
        check_outputs: Also verify output files exist, are readable, and match their
            recorded SHA256 (output SHA drift)
        rebaseline: Recompute the output-metadata drift baseline (run.outputs_hashed,
            delivery step 2b / AC-25) for every run in `results`, instead of relying
            on the baseline compact.py refreshes automatically. Behind the runlog
            flag: on, emits the event only; off, writes the warm `runs.output_metadata`
            column directly (this command's own "legacy" path -- there is no prior
            `--rebaseline` to preserve).

    Returns:
        Dict with check results
    """
    from bathos.checker import check_output_files, check_output_sha_drift, rebaseline_run_outputs
    from bathos.query import get_run

    cat_dir = _get_catalog_dir(catalog_dir or None)
    proj_root = Path(project_root) if project_root else Path.cwd()
    # check_runs() defaults to limit=50 over an unordered scan, which would silently drop
    # an arbitrary subset of runs on a larger catalog (debt #1476) -- scan them all.
    results = check_runs(
        cat_dir, proj_root, status_filter=status_filter, limit=_UNBOUNDED_SCAN_LIMIT
    )
    stale_count = sum(1 for r in results if r.status == "STALE")
    results_json = [
        {
            "run_id": r.run_id,
            "status": r.status,
            "run_git_hash": r.run_git_hash,
            "current_hash": r.current_hash,
        }
        for r in results
    ]

    drift_count = 0
    output_status: list[dict] = []
    if check_outputs:
        for r in results:
            run = get_run(r.run_id, cat_dir)
            if run is None:
                continue
            for out_result in check_output_files(run):
                output_status.append(
                    {"run_id": r.run_id, "path": out_result.path, "status": out_result.status}
                )
            for drift in check_output_sha_drift(run):
                if drift.status == "UNRECORDED":
                    continue
                if drift.status != "OK":
                    drift_count += 1
                output_status.append(
                    {
                        "run_id": r.run_id,
                        "path": drift.path,
                        "sha_drift_status": drift.status,
                    }
                )

    rebaselined_count = 0
    if rebaseline:
        for r in results:
            run = get_run(r.run_id, cat_dir)
            if run is None or not run.output_paths:
                continue
            rebaseline_run_outputs(cat_dir, run, cwd=proj_root)
            rebaselined_count += 1

    result_dict = {
        "results": results_json,
        "count": len(results_json),
        "stale_count": stale_count,
        "drift_count": drift_count,
        "output_status": output_status,
        "rebaselined_count": rebaselined_count,
    }
    if stale_count > 0 or drift_count > 0:
        # Singular "error" key -- see cli_render.render_or_exit -- matches the shipped
        # Typer `check` command's `if stale_count or drift_count: raise typer.Exit(1)`.
        result_dict["error"] = (
            f"{stale_count} stale run(s), {drift_count} output SHA drift(s) detected"
        )
    return result_dict


def capability_probe_tool(catalog_dir: str = "") -> dict:
    """Machine-checkable liveness of Run.seed + the stats battery (B2-06, AC-20).

    REAL, catalog-specific probe (not a hand-maintained attestation) — a loop controller can
    call this before starting a confirmatory campaign to check whether this bathos instance
    can actually support seed-tracking (B2-02, requires a compacted warm DB carrying the
    seed/baseline_hpo_* columns) and the statistical battery (B2-01, requires scipy installed).

    Args:
        catalog_dir: Catalog directory (empty = use default)

    Returns:
        Dict with seed_live, missing_seed_columns, stats_battery_live,
        stats_unavailable_reason.
    """
    cat_dir = _get_catalog_dir(catalog_dir or None)
    report = probe_capabilities(cat_dir)
    return {
        "seed_live": report.seed_live,
        "missing_seed_columns": list(report.missing_seed_columns),
        "stats_battery_live": report.stats_battery_live,
        "stats_unavailable_reason": report.stats_unavailable_reason,
    }


@cisternal.tool(registry="bathos-cli", name="sync", cli_name="sync")
def sync_tool(
    remote_name: str = "",
    pull: bool = False,
    catalog_dir: str = "",
) -> dict:
    """Sync catalog to/from remote.

    Args:
        catalog_dir: Catalog directory (empty = use default)
        remote_name: Remote name from .bth.toml (auto-selected if only one configured)
        pull: Pull from remote (default: push to remote)

    Returns:
        Dict with sync result. With `pull=True` in log mode, also carries
        `cluster_log_pulled` (bool: at least one of the log/fallback/mirror
        sub-pulls actually succeeded -- NOT merely "no exception raised";
        False when the installed myxcel predates `pull-path`, myxcel
        backlog #5880) and, when anything
        failed, `cluster_log_pull_failures` (list of per-subpath reasons) or
        `cluster_log_pull_error` (a single top-level failure, e.g. an
        unconfigured remote).
    """
    cat_dir = _get_catalog_dir(catalog_dir or None)
    # Load ProjectConfig from .bth.toml in project root
    config_path = find_project_config(Path.cwd())
    if not config_path:
        return {"error": "Could not find .bth.toml in project hierarchy"}
    config = load_project_config(config_path)

    if not remote_name:
        remotes = list(config.remotes.keys())
        if len(remotes) == 0:
            return {"error": "No remotes configured. Use 'bth remote add' to add one."}
        elif len(remotes) == 1:
            remote_name = remotes[0]
        else:
            names = ", ".join(f"'{r}'" for r in sorted(remotes))
            return {"error": f"Multiple remotes configured ({names}). Specify one explicitly."}

    result = sync_catalog(remote_name, config, cat_dir, pull=pull)
    result_dict = {
        "transferred": result.transferred,
        "duration_s": result.duration_s,
        "remote": result.remote,
        "filtered": result.filtered,
    }

    # Cluster log pull (spec "Cluster", delivery step 4 wave d): only on
    # --pull, and only once the local flag is on -- with the flag off, sync
    # behaves exactly as today (spec "Cluster" + "Mode").
    if pull:
        from bathos.runlog.mode import is_log_mode

        if is_log_mode(cat_dir):
            from bathos.runlog.project_id import read_project_id
            from bathos.runlog.resolve import resolve_log_root
            from bathos.sync import pull_cluster_log

            resolution = resolve_log_root(Path.cwd())
            if not resolution.unaffiliated:
                project_id = read_project_id(resolution.main_root / ".bth.toml")
                try:
                    pull_result = pull_cluster_log(
                        remote_name, config, resolution.main_root, project_id
                    )
                except Exception as e:
                    result_dict["cluster_log_pulled"] = False
                    result_dict["cluster_log_pull_error"] = str(e)
                else:
                    # A normal return is NOT success: pull_cluster_log catches
                    # each of the three sub-pulls individually and always
                    # returns, even when every one of them failed (e.g. an
                    # installed myxcel that predates `pull-path`). Report
                    # honestly from the per-subpath result, not from the
                    # absence of a raised exception.
                    result_dict["cluster_log_pulled"] = pull_result.any_pulled
                    if pull_result.failures:
                        result_dict["cluster_log_pull_failures"] = pull_result.failures

    return result_dict


@cisternal.tool(registry="bathos-cli", name="init", cli_name="init")
def init_tool(
    project_root: str = "",
    catalog_dir: str = "",
    slug: str = "",
    remote: str = "",
    slurm_partition: str = "",
    assign_id: bool = False,
) -> dict:
    """Initialize project with bathos, or retrofit a project id (D7).

    Args:
        project_root: Project root directory (empty = current directory)
        catalog_dir: Catalog directory (empty = use default)
        slug: Project slug (ignored when assign_id is set)
        remote: Remote in host:path format
        slurm_partition: Default SLURM partition
        assign_id: Retrofit a `[project] id` onto an EXISTING project's
            `.bth.toml` (spec 260925 D7) instead of running full init.
            Idempotent: a project that already has an id keeps it.

    Returns:
        Dict with init result
    """
    root = Path(project_root) if project_root else Path.cwd()

    if assign_id:
        from bathos.init import assign_id_to_existing_project

        project_id, minted, registered = assign_id_to_existing_project(root)
        return {
            "project_root": str(root),
            "project_id": project_id,
            "minted": minted,
            "registered": registered,
        }

    if not slug:
        return {"error": "slug parameter is required"}
    cat_dir = _get_catalog_dir(catalog_dir or None)
    report = init_project(
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
        # Debt #1952: on an already-initialized project, .bth.toml is merged rather than
        # overwritten -- this reports exactly what changed vs. what survived untouched.
        "bth_toml_created": report.created,
        "bth_toml_added": report.added,
        "bth_toml_preserved": report.preserved,
        "bth_toml_skipped_requests": report.skipped_requests,
    }


@cisternal.tool(registry="bathos-cli", name="log_restore", cli_group="log", cli_name="restore")
def log_restore_tool(project_root: str = "") -> dict:
    """Restore a project's run log from its mirror (spec 260925 D7, AC-14, AC-27).

    Copies back into `<main root>/.bth/log/` every event present in
    `~/.bth/log-mirror/` for this project that the project log lacks, except
    one that belongs to another currently live root sharing the same project
    id (a genuine fork never receives the other copy's own events).

    Args:
        project_root: A path inside the project to restore (empty = resolve
            from cwd via the same worktree-aware ladder `bth run` uses)

    Returns:
        Dict with restored/already_present/skipped_other_live_root counts
    """
    from bathos.runlog.project_id import read_project_id
    from bathos.runlog.resolve import resolve_log_root
    from bathos.runlog.restore import restore_from_mirror

    root = Path(project_root) if project_root else None
    resolution = resolve_log_root(root)
    if resolution.unaffiliated:
        return {
            "error": "no git repository and no .bth.toml found under this root; nothing to restore"
        }

    project_id = read_project_id(resolution.main_root / ".bth.toml")
    slug = None
    if project_id is None:
        cfg_path = find_project_config(resolution.main_root)
        if cfg_path is not None:
            try:
                slug = load_project_config(cfg_path).slug
            except Exception:
                slug = None

    report = restore_from_mirror(resolution.main_root, project_id=project_id, slug=slug)
    return {
        "main_root": str(resolution.main_root),
        "project_id": project_id,
        "restored": report.restored,
        "already_present": report.already_present,
        "skipped_other_live_root": report.skipped_other_live_root,
        "segments_written": [str(p) for p in report.segments_written],
    }


def _bare_script_argv(script_path: str, args: list[str], cwd: Path) -> tuple[list[str], str | None]:
    """argv for a bare ``.py`` script, plus an optional warning (debt #2236).

    ``sys.executable`` is bathos's OWN tool-isolated interpreter, so a project script that
    imports project-local packages dies with ModuleNotFoundError. When the workspace (cwd)
    has an existing ``.venv`` and ``uv`` is on PATH, run through ``uv run --no-sync python``
    (``--no-sync``: a bare ``uv run`` silently re-syncs and reverts hand-installed wheels).
    A ``pyproject.toml`` alone is not enough: ``uv run`` would then create a fresh, empty
    ``.venv`` in the project as a side effect. Otherwise keep the old behaviour but say so.
    """
    import shutil

    in_project = (cwd / ".venv").is_dir()
    if in_project and shutil.which("uv"):
        return ["uv", "run", "--no-sync", "python", script_path] + args, None
    if in_project:
        reason = "`uv` was not found on PATH, so the project venv could not be used"
    else:
        reason = (
            "no .venv found in the working directory (create one with `uv sync`; "
            "a pyproject.toml alone is not used, to avoid creating a venv implicitly)"
        )
    return [sys.executable, script_path] + args, (
        f"script ran under bathos's own interpreter ({sys.executable}), not a project "
        f"venv: {reason}. Project-local imports may fail; pass an explicit command "
        "(e.g. script_path='uv', args=['run', 'python', 'script.py']) to choose one."
    )


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
    allow_stale: bool = False,
    component_id: str = "",
    component_sidecar_sha256: str = "",
    seed: int | None = None,
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
        allow_stale: Run anyway despite the sidecar's [status] stale=true flag
        component_id: xtrax composition-node/StageBundle-slot ID this run binds to (B2-08 bridge)
        component_sidecar_sha256: SHA256 of the component-level sidecar for
            component_id (B2-08 bridge)
        seed: RNG seed this run was launched with, recorded as Run.seed so seed-floor gates can
            see it. bathos does not seed anything itself: the value is exported to the script as
            $BTH_SEED and the script must honour it. A `--seed` placed AFTER the script path
            belongs to the script and is not read here.

    Returns:
        Dict with run result or structured gate error
    """
    if not script_path:
        return {"error": "script_path parameter is required"}
    if args is None:
        args = []
    output_paths = normalise_output_paths(output_paths)
    tags = normalise_tags(tags)

    # Construct argv. Two shapes reach this function under the one `script_path`
    # + `*args` signature, and they need DIFFERENT treatment (debt: `bth run`
    # blocks all tracked runs in projects whose scripts need their own venv,
    # e.g. protamer/proxide -- see using-bathos skill's documented
    # `bth run -- uv run python scripts/train.py ...` usage):
    #
    # 1. A bare `.py` script (`bth run scripts/train.py ...`, no interpreter of
    #    its own named): prepend THIS process's interpreter, not a bare
    #    "python" -- machines without an unprefixed `python` on PATH
    #    (python3-only systems) made Popen raise FileNotFoundError, which
    #    run_script swallowed into a bare exit_code=1. sys.executable is also
    #    the interpreter guaranteed to satisfy BTH_RESULTS_PATH plumbing.
    # 2. An already-a-command first token (`bth run -- uv run python
    #    scripts/train.py ...` -- script_path="uv", args=("run", "python",
    #    ...)): exec it AS GIVEN. Prepending sys.executable here silently
    #    replaced the user's whole command with `<bathos's own interpreter>
    #    uv run python ...`, which tries to open a file literally named "uv"
    #    and ignores everything the user meant to run -- and, worse, runs
    #    ANY bare `.py` target under bathos's own tool-isolated interpreter
    #    even when the user named a different one (`bth run -- python3 -c
    #    "..."` : script_path="python3" [not sys.executable], "-c" and its
    #    payload silently discarded as a script-name-that-doesn't-exist
    #    lookup). A project script that imports project-local packages
    #    (e.g. proxide) is never installed into bathos's own tool venv, so
    #    case 1's prepend fails every such script with a ModuleNotFoundError
    #    that has nothing to do with the script itself.
    interpreter_warning: str | None = None
    if script_path.endswith(".py"):
        argv, interpreter_warning = _bare_script_argv(script_path, args, Path.cwd())
    else:
        argv = [script_path] + args

    # Resolve parameters. Unlike _get_project_slug's other call sites, `run`
    # must hard-fail with no configured slug rather than silently defaulting
    # to "default" -- matching the pre-cutover Typer `run` command's
    # _require_project_slug() and cli_common.soft_project_slug()'s own
    # docstring, which documents `run` as a slug-required command.
    cat_dir = _get_catalog_dir(catalog_dir or None)
    slug = project_slug or require_project_slug()

    # Clear any stale value before calling run_script: two early-return paths in
    # run_script (invalid sidecar, gate-check failure) bail before a Run is ever
    # constructed/persisted, and thus never touch run_uuid_var themselves — without
    # this reset a failed call in the same context could report a prior call's ID.
    run_uuid_var.set(None)

    # run_script runs in-process, so its stderr advisories never reach an MCP caller: collect
    # them through run_advisories_var and return them as `warnings` (spec 261026 D2).
    advisories: list[dict] = []
    advisories_token = run_advisories_var.set(advisories)

    # Call run_script with new parameters
    try:
        exit_code = run_script(
            argv=argv,
            project_slug=slug,
            catalog_dir=cat_dir,
            output_paths=output_paths,
            tags=tags,
            agent_mode=agent_mode or None,
            no_sidecar=no_sidecar,
            allow_stale=allow_stale,
            derived_from=derived_from or None,
            campaign_id=campaign_id or None,
            component_id=component_id or None,
            component_sidecar_sha256=component_sidecar_sha256 or None,
            seed=seed,
        )
    finally:
        run_advisories_var.reset(advisories_token)

    # run_script doesn't return the Run it creates (its return type is a plain
    # exit_code int, relied on elsewhere — cli.py's typer.Exit(exit_code) and 29
    # assertions in tests/test_runner.py), but it does stash the run's id in this
    # contextvar as a side effect (runner.py) — the same mechanism harness_run.py
    # already reads post-call to recover the id without changing run_script's shape.
    run_id = run_uuid_var.get(None) or ""

    response = {
        "script_path": script_path,
        "run_id": run_id,
        "exit_code": exit_code,
        "success": exit_code == 0,
        "warnings": advisories,
    }
    if interpreter_warning:
        response["warning"] = interpreter_warning
    return response


@cisternal.tool(registry="bathos-cli", name="run", cli_name="run")
def run_cli_tool(
    script_path: str = "",
    *args: str,
    project_slug: str = "",
    catalog_dir: str = "",
    output_paths: list[str] | None = None,
    tags: list[str] | None = None,
    agent_mode: str = "",
    derived_from: str = "",
    campaign_id: str = "",
    no_sidecar: bool = False,
    allow_stale: bool = False,
    component_id: str = "",
    component_sidecar_sha256: str = "",
    bth_seed: int | None = None,
) -> dict:
    """CLI-facing wrapper over run_tool: propagates the run script's own exit
    code as the `bth run` process's exit code (matching the shipped Typer
    `run` command's `raise typer.Exit(exit_code)` behavior).

    The CLI flag is `--bth-seed`, not `--seed`: cyclopts consumes a known option anywhere on the
    line, so a `--seed` flag would silently steal a script's own `bth run train.py --seed 3`,
    leaving Run.seed recorded as 3 while the script ran with its default (a false provenance
    claim). The MCP `run` tool passes script args as a separate list, so it keeps `seed`.

    A separate function from run_tool (rather than folding this into run_tool
    itself) because cli_render.render_or_exit's generic dict-error convention
    only supports a binary exit 0/1 via a singular "error" key -- it cannot
    pass through an arbitrary exit code, and get_run_tool (the `show`
    command) also has a top-level "exit_code" field with unrelated meaning (a
    run's OWN recorded exit_code, not the `bth show` process's), so a generic
    "exit_code"-in-any-result convention in render_or_exit would silently
    break `bth show` on any failed run.
    """
    result = run_tool(
        script_path=script_path,
        args=list(args),
        project_slug=project_slug,
        catalog_dir=catalog_dir,
        output_paths=output_paths,
        tags=tags,
        agent_mode=agent_mode,
        derived_from=derived_from,
        campaign_id=campaign_id,
        no_sidecar=no_sidecar,
        allow_stale=allow_stale,
        component_id=component_id,
        component_sidecar_sha256=component_sidecar_sha256,
        seed=bth_seed,
    )
    print(json.dumps(result, indent=2, default=str))
    raise SystemExit(1 if result.get("error") else result.get("exit_code", 0))


# cisternal.wire()'s CLI closure (wired.py's _make_cli_cmd) copies
# __annotations__ verbatim from this function and rebuilds a Signature via
# plain inspect.signature() (no eval_str) against that closure's OWN globals
# -- which don't include this module's `cyclopts`/`Annotated` names. Because
# this module has `from __future__ import annotations`, a source-level
# `*args: Annotated[str, cyclopts.Parameter(allow_leading_hyphen=True)]`
# would be stored as a *string* and later fail with `NameError: name
# 'Annotated' is not defined` when cyclopts resolves it via
# get_type_hints(cli_cmd) (verified empirically -- this is the same
# limitation documented for `--campaign` -> `--campaign-id`). Mutating
# __annotations__ here, after the def, stores the real (already-evaluated)
# Annotated object instead of a string, so no name resolution is needed
# downstream. Without allow_leading_hyphen, cyclopts' positional/variadic
# parser rejects any leading-hyphen token (e.g. `--lr`) as an unrecognized
# option before it ever reaches this function -- the exact `bth run
# --script-path x --lr 0.01` regression this restores passthrough for.
run_cli_tool.__annotations__["args"] = Annotated[
    str, cyclopts.Parameter(allow_leading_hyphen=True)
]


@cisternal.tool(
    registry="bathos-cli", name="campaign_create", cli_group="campaign", cli_name="create"
)
def campaign_create_tool(
    name: str = "",
    mode: str = "exploration",
    project_slug: str = "",
    catalog_dir: str = "",
    question: str = "",
    hypothesis: str = "",
    parent_campaign_id: str = "",
) -> dict:
    """Create a new experiment campaign.

    Args:
        name: Campaign name
        mode: Campaign mode ('exploration', 'confirmation', or 'sequential')
        project_slug: Project slug (default: from config or 'default')
        catalog_dir: Catalog directory (empty = use default)
        question: Research question (optional)
        hypothesis: Research hypothesis (optional)
        parent_campaign_id: Parent campaign ID, for lineage (optional)

    Returns:
        Dict with campaign details or error
    """
    if not name:
        return {"error": "name parameter is required"}
    if mode not in CAMPAIGN_MODES:
        return {"error": f"mode must be one of {list(CAMPAIGN_MODES)}, got {mode!r}"}

    cat_dir = _get_catalog_dir(catalog_dir or None)
    slug = project_slug or _get_project_slug()

    from bathos.campaigns import connect_catalog_db, list_campaigns
    from bathos.catalog import init_catalog
    from bathos.compact import compact
    from bathos.runlog.emit import unit_of_work

    # One unit of work: connecting and creating see the same mode (#2843).
    with unit_of_work(cat_dir):
        db = connect_catalog_db(cat_dir, read_only=False)
        if db is None:
            init_catalog(cat_dir)
            compact(cat_dir)
            db = connect_catalog_db(cat_dir, read_only=False)
        if db is None:
            return {"error": f"Catalog database not found at {cat_dir / 'bathos.db'}"}
        try:
            existing = [
                c
                for c in list_campaigns(db, project_slug=slug, status="open", catalog_dir=cat_dir)
                if c.name == name
            ]
            campaign = create_campaign(
                db,
                name=name,
                project_slug=slug,
                mode=mode,
                question=question or None,
                hypothesis=hypothesis or None,
                parent_campaign_id=parent_campaign_id or None,
                catalog_dir=cat_dir,
            )
            result = {
                "campaign_id": campaign.id,
                "name": campaign.name,
                "mode": campaign.mode,
                "status": campaign.status,
                "started_at": campaign.started_at,
            }
            if existing:
                ids = ", ".join(c.id[:8] for c in existing)
                result["warning"] = (
                    f"{len(existing)} open campaign(s) named {name!r} already exist: {ids}"
                )
            return result
        finally:
            db.close()


@cisternal.tool(registry="bathos-cli", name="campaign_list", cli_group="campaign", cli_name="ls")
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

    from bathos.campaigns import connect_catalog_db

    db = connect_catalog_db(cat_dir, read_only=True)
    try:
        campaigns = list_campaigns(
            db, project_slug=slug, status=status or None, catalog_dir=cat_dir
        )
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
        if db is not None:
            db.close()


@cisternal.tool(
    registry="bathos-cli", name="campaign_review", cli_group="campaign", cli_name="review"
)
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

    from bathos.campaigns import connect_catalog_db

    db = connect_catalog_db(cat_dir, read_only=True)
    try:
        review = review_campaign(
            db, campaign_id, catalog_dir=cat_dir, workspace_root=_review_workspace()
        )
        return review
    finally:
        if db is not None:
            db.close()


@cisternal.tool(
    registry="bathos-cli", name="campaign_conclude", cli_group="campaign", cli_name="conclude"
)
def campaign_conclude_tool(
    campaign_id: str = "",
    outcome_label: str = "",
    conclusion: str = "",
    catalog_dir: str = "",
    negative_check: str = "",
    workspace_root: str = "",
) -> dict:
    """Conclude a campaign with an outcome label.

    Args:
        campaign_id: Campaign ID to conclude
        outcome_label: Outcome label (e.g., 'success', 'inconclusive', 'failed')
        conclusion: Summary conclusion text
        catalog_dir: Catalog directory (empty = use default)
        negative_check: Falsification backing / hedge for a negative outcome label (BP-3);
            required when outcome_label is a negative claim and a claim is registered.
        workspace_root: Project workspace root that registered claim paths resolve
            against (empty = auto-resolve from the server's cwd)

    Returns:
        Dict with conclusion confirmation
    """
    if not campaign_id:
        return {"error": "campaign_id parameter is required"}
    if not outcome_label:
        return {"error": "outcome_label parameter is required"}

    cat_dir = _get_catalog_dir(catalog_dir or None)

    from bathos.campaigns import CampaignError, connect_catalog_db, prepare_catalog_for_conclude
    from bathos.runlog.emit import unit_of_work

    # One unit of work: prepare, connect and conclude see the same mode (#2843).
    with unit_of_work(cat_dir):
        prepare_catalog_for_conclude(cat_dir)
        db = connect_catalog_db(cat_dir, read_only=False)
        if db is None:
            return {"error": f"Catalog database not found at {cat_dir / 'bathos.db'}"}
        try:
            conclude_campaign(
                db,
                campaign_id,
                outcome_label,
                conclusion or "",
                workspace_root=(
                    Path(workspace_root).expanduser().resolve() if workspace_root else None
                ),
                negative_check=negative_check or None,
                catalog_dir=cat_dir,
            )
            return {
                "status": "concluded",
                "campaign_id": campaign_id,
                "outcome_label": outcome_label,
            }
        except BreakdownRefused as e:
            return _breakdown_refusal_result(e)
        except CampaignError as e:
            return {"error": str(e)}
        finally:
            db.close()


@cisternal.tool(
    registry="bathos-cli",
    name="claim_attest_parity",
    cli_group="campaign",
    cli_name="attest-parity",
)
def campaign_attest_parity_tool(
    campaign_id: str = "",
    parity_run_id: str = "",
    catalog_dir: str = "",
    workspace_root: str = "",
) -> dict:
    """Bind a passing literature-parity run to a campaign's registered claim (F4).

    Wraps attest_parity(): validates the run, updates the claim file atomically,
    and re-anchors the campaign claim_sha256 in the warm catalog. Shared by the
    MCP `claim_attest_parity` tool and the `bathos-cli` registry's CLI form
    (backlog #4702 Milestone 1) — previously each transport duplicated this
    logic independently.

    Args:
        campaign_id: Campaign ID (or prefix)
        parity_run_id: Parity run ID to bind to the campaign claim
        catalog_dir: Catalog directory (empty = use default)
        workspace_root: Workspace root (empty = auto-resolve)

    Returns:
        Dict with campaign_id/parity_run_id/message, or {"error": ...} on failure.
    """
    if not campaign_id:
        return {"error": "campaign_id parameter is required"}
    if not parity_run_id:
        return {"error": "parity_run_id parameter is required"}

    cat_dir = _get_catalog_dir(catalog_dir or None)

    if workspace_root:
        ws = Path(workspace_root).expanduser().resolve()
    else:
        from bathos.workspace import resolve_workspace

        ws = resolve_workspace().fs_root

    from bathos.campaigns import connect_catalog_db
    from bathos.claim import attest_parity
    from bathos.runlog.emit import unit_of_work

    # After cut-over this is the index, not the frozen `bathos.db`; attest_parity
    # flag-gates its write via emit_or_legacy (debt #2843).
    with unit_of_work(cat_dir):
        db = connect_catalog_db(cat_dir, read_only=False)
        if db is None:
            return {"error": f"Catalog database not found at {cat_dir / 'bathos.db'}"}
        try:
            attest_parity(campaign_id, parity_run_id, db, ws, catalog_dir=cat_dir)
            return {
                "campaign_id": campaign_id,
                "parity_run_id": parity_run_id,
                "message": f"Attested parity run {parity_run_id} on campaign {campaign_id}",
            }
        except (ValueError, RuntimeError, FileNotFoundError) as e:
            return {"error": str(e)}
        finally:
            db.close()


# ============================================================================
# MCP server tool registrations
# ============================================================================


@cisternal.tool(registry="bathos", name="list_runs")
@traced_tool
async def mcp_list_runs_tool(
    catalog_dir: str = "",
    limit: int = 10,
) -> dict:
    """List recent runs from catalog."""
    return list_runs_tool(catalog_dir=catalog_dir, limit=limit)


@cisternal.tool(registry="bathos", name="find_runs")
@traced_tool
async def mcp_find_runs_tool(
    catalog_dir: str = "",
    pattern: str = "",
    tags: list[str] | None = None,
) -> dict:
    """Find runs by pattern (project) and/or tags."""
    return find_runs_tool(catalog_dir=catalog_dir, project=pattern, tags=tags)


@cisternal.tool(registry="bathos", name="get_run")
@traced_tool
async def mcp_get_run_tool(
    catalog_dir: str = "",
    run_id: str = "",
) -> dict:
    """Get details for a specific run."""
    return get_run_tool(catalog_dir=catalog_dir, run_id=run_id)


@cisternal.tool(registry="bathos-cli", name="cite_run", cli_name="cite")
def cite_run_tool(
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


@cisternal.tool(registry="bathos", name="cite_run")
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
    return cite_run_tool(run_id=run_id, catalog_dir=catalog_dir, format=format)


@cisternal.tool(registry="bathos-cli", name="lineage_prov", cli_name="lineage")
def lineage_prov_tool(
    run_id: str,
    catalog_dir: str = "",
    depth: int = 50,
) -> dict:
    """Return W3C PROV-JSON lineage for a run.

    Args:
        run_id: The run ID to trace ancestry for.
        catalog_dir: Path to catalog directory (uses default if empty).
        depth: Maximum number of ancestor hops to traverse. The starting run is
            depth 0; the default of 50 matches the historical cycle guard.

    Returns:
        W3C PROV-JSON formatted lineage (dict).
    """
    from bathos.provenance import format_prov_json
    from bathos.query import CatalogError
    from bathos.query import lineage as get_lineage

    cat_dir = _get_catalog_dir(catalog_dir)
    ancestors = get_lineage(run_id, cat_dir, depth=depth)

    if not ancestors:
        raise CatalogError(f"Run not found or no lineage: {run_id}")

    prov_output = format_prov_json(ancestors)
    return {"prov": prov_output}


@cisternal.tool(registry="bathos", name="lineage_prov")
@traced_tool
async def mcp_lineage_prov_tool(
    run_id: str,
    catalog_dir: str = "",
    depth: int = 50,
) -> dict:
    """Return W3C PROV-JSON lineage for a run.

    Args:
        run_id: The run ID to trace ancestry for.
        catalog_dir: Path to catalog directory (uses default if empty).
        depth: Maximum number of ancestor hops to traverse. The starting run is
            depth 0; the default of 50 matches the historical cycle guard.

    Returns:
        W3C PROV-JSON formatted lineage (dict).
    """
    return lineage_prov_tool(run_id=run_id, catalog_dir=catalog_dir, depth=depth)


@cisternal.tool(registry="bathos", name="run_sql")
@traced_tool
async def mcp_run_sql_tool(
    catalog_dir: str = "",
    sql: str = "",
) -> dict:
    """Execute SQL query against catalog."""
    return run_sql_tool(catalog_dir=catalog_dir, sql=sql)


@cisternal.tool(registry="bathos", name="resolve_pin")
@traced_tool
async def mcp_resolve_pin_tool(
    run_id: str = "",
    output_path: str = "",
    catalog_dir: str = "",
) -> dict:
    """Resolve a run_id + output_path pin to content_hash/trust_state/freshness (S1)."""
    return resolve_pin_tool(run_id=run_id, output_path=output_path, catalog_dir=catalog_dir)


@cisternal.tool(registry="bathos", name="get_trust_state")
@traced_tool
async def mcp_get_trust_state_tool(
    content_hash: str = "",
    catalog_dir: str = "",
) -> dict:
    """Look up trust_state for a content_hash (S1; null-stub pending trust ledger S3)."""
    return get_trust_state_tool(content_hash=content_hash, catalog_dir=catalog_dir)


@cisternal.tool(registry="bathos", name="query_attestation")
@traced_tool
async def mcp_query_attestation_tool(
    content_hash: str = "",
    min_strength: str = "",
    catalog_dir: str = "",
) -> dict:
    """Query attestation for a content_hash (S1; real, backed by the S4 attestation store)."""
    return query_attestation_tool(
        content_hash=content_hash, min_strength=min_strength, catalog_dir=catalog_dir
    )


@cisternal.tool(registry="bathos", name="read_campaign_report")
@traced_tool
async def mcp_read_campaign_report_tool(
    campaign_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """Read the campaign_report.json sidecar for a campaign (S1; real)."""
    return read_campaign_report_tool(campaign_id=campaign_id, catalog_dir=catalog_dir)


@cisternal.tool(registry="bathos", name="read_figure_manifest")
@traced_tool
async def mcp_read_figure_manifest_tool(
    campaign_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """Read the figure_manifest.json sidecar for a campaign (S1; real)."""
    return read_figure_manifest_tool(campaign_id=campaign_id, catalog_dir=catalog_dir)


@cisternal.tool(registry="bathos", name="figure_lookup")
@traced_tool
async def mcp_figure_lookup_tool(
    asset_sha256: str = "",
    input_hash: str = "",
    catalog_dir: str = "",
) -> dict:
    """Look up figure registry entries (S1; real, backed by the S2 anchor store)."""
    return figure_lookup_tool(
        asset_sha256=asset_sha256, input_hash=input_hash, catalog_dir=catalog_dir
    )


@cisternal.tool(registry="bathos", name="list_candidates")
@traced_tool
async def mcp_list_candidates_tool(
    campaign_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """List candidate-tier products for a campaign (S1; null-stub pending trust ledger S3)."""
    return list_candidates_tool(campaign_id=campaign_id, catalog_dir=catalog_dir)


@cisternal.tool(registry="bathos", name="anchor_insert")
@traced_tool
@require_write_token
async def mcp_anchor_insert_tool(
    path: str = "",
    sha256: str = "",
    kind: str = "",
    label: str = "",
    content_hash: str = "",
    campaign_id: str = "",
    catalog_dir: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Anchor a sidecar by (path, sha256) into the catalog (S2 write seam; real).

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
    return anchor_insert_tool(
        path=path,
        sha256=sha256,
        kind=kind,
        label=label,
        content_hash=content_hash,
        campaign_id=campaign_id,
        catalog_dir=catalog_dir,
    )


@cisternal.tool(registry="bathos", name="anchor_get")
@traced_tool
async def mcp_anchor_get_tool(
    path: str = "",
    sha256: str = "",
    catalog_dir: str = "",
) -> dict:
    """Read back an anchored sidecar by (path, sha256) (S2; real)."""
    return anchor_get_tool(path=path, sha256=sha256, catalog_dir=catalog_dir)


@cisternal.tool(registry="bathos", name="anchor_find")
@traced_tool
async def mcp_anchor_find_tool(
    kind: str = "",
    sha256: str = "",
    content_hash: str = "",
    campaign_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """Find anchored sidecars matching filters (S2; real)."""
    return anchor_find_tool(
        kind=kind,
        sha256=sha256,
        content_hash=content_hash,
        campaign_id=campaign_id,
        catalog_dir=catalog_dir,
    )


@cisternal.tool(registry="bathos", name="figure_entry_register")
@traced_tool
@require_write_token
async def mcp_figure_entry_register_tool(
    asset_sha256: str = "",
    sidecar_ref: str = "",
    figure_kind: str = "",
    render_state: str = "ready",
    fig_trust_state: str = "draft",
    attestation_ref: str = "",
    input_hash: str = "",
    campaign_id: str = "",
    catalog_dir: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Register a typed figure_entry, anchored by asset_sha256 (S7 write seam; real).

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
    return figure_entry_register_tool(
        asset_sha256=asset_sha256,
        sidecar_ref=sidecar_ref,
        figure_kind=figure_kind,
        render_state=render_state,
        fig_trust_state=fig_trust_state,
        attestation_ref=attestation_ref,
        input_hash=input_hash,
        campaign_id=campaign_id,
        catalog_dir=catalog_dir,
    )


@cisternal.tool(registry="bathos", name="attestation_scaffold")
@traced_tool
@require_write_token
async def mcp_attestation_scaffold_tool(
    kind: str = "",
    workspace_root: str = "",
    label: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Scaffold a new attestation.bth.toml template (S4; real; separate kind from
    `bath.claim` — see gate #3488).

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
    return attestation_scaffold_tool(kind=kind, workspace_root=workspace_root, label=label)


@cisternal.tool(registry="bathos", name="attestation_validate")
@traced_tool
async def mcp_attestation_validate_tool(
    path: str = "",
) -> dict:
    """Validate an attestation TOML file's kind-specific required fields (S4; real)."""
    return attestation_validate_tool(path=path)


@cisternal.tool(registry="bathos", name="attestation_register")
@traced_tool
@require_write_token
async def mcp_attestation_register_tool(
    path: str = "",
    catalog_dir: str = "",
    campaign_id: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Register an attestation file: anchor it by its own SHA256 (S4; real; durable by
    default via DurableAnchorStore).

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
    return attestation_register_tool(path=path, catalog_dir=catalog_dir, campaign_id=campaign_id)


@cisternal.tool(registry="bathos", name="graduate_product")
@traced_tool
@require_write_token
async def mcp_graduate_product_tool(
    content_hash: str = "",
    attestation_ref: str = "",
    catalog_dir: str = "",
    min_strength: str = "",
    run_id: str = "",
    output_path: str = "",
    reason: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Graduate a product from candidate to promoted in the trust ledger (S3; real).

    Refuses (error_code="graduation_refused") unless a PASS attestation already
    exists for content_hash at the requested min_strength — the ratchet invariant
    (bathos.trust_ledger.graduate_product's own docstring).

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
    return graduate_product_tool(
        content_hash=content_hash,
        attestation_ref=attestation_ref,
        catalog_dir=catalog_dir,
        min_strength=min_strength,
        run_id=run_id,
        output_path=output_path,
        reason=reason,
    )


@cisternal.tool(registry="bathos", name="blast_radius_assess")
@traced_tool
@require_write_token
async def mcp_blast_radius_assess_tool(
    catalog_dir: str = "",
    project_root: str = "",
    commit: str = "",
    commit_range: str = "",
    files: str = "",
    dependency: bool = False,
    project: str = "",
    flag: bool = True,
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Assess which runs a bug/fix implicates, then flag + propagate to
    campaign/claim records (backlog #4551/#4552).

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841) — gated because
    flag=True by default performs a durable ledger write (security-audit finding,
    PR #54: this was previously the only mutating tool in this file without the gate)."""
    return blast_radius_assess_tool(
        catalog_dir=catalog_dir,
        project_root=project_root,
        commit=commit,
        commit_range=commit_range,
        files=files,
        dependency=dependency,
        project=project,
        flag=flag,
    )


@cisternal.tool(registry="bathos", name="blast_radius_clear")
@traced_tool
@require_write_token
async def mcp_blast_radius_clear_tool(
    catalog_dir: str = "",
    entity_type: str = "",
    entity_id: str = "",
    reason: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Manually clear a blast-radius flag (backlog #4551, AC-9).

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
    return blast_radius_clear_tool(
        catalog_dir=catalog_dir, entity_type=entity_type, entity_id=entity_id, reason=reason
    )


@cisternal.tool(registry="bathos", name="get_blast_radius_status")
@traced_tool
async def mcp_get_blast_radius_status_tool(
    catalog_dir: str = "",
    entity_type: str = "",
    entity_id: str = "",
) -> dict:
    """Look up blast-radius status for an entity (backlog #4551)."""
    return get_blast_radius_status_tool(
        catalog_dir=catalog_dir, entity_type=entity_type, entity_id=entity_id
    )


@cisternal.tool(registry="bathos", name="compact")
@traced_tool
@require_write_token
async def mcp_compact_tool(
    catalog_dir: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Compact cool-tier Parquet to warm-tier DuckDB.

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
    return compact_tool(catalog_dir=catalog_dir)


@cisternal.tool(registry="bathos", name="archive")
@traced_tool
@require_write_token
async def mcp_archive_tool(
    catalog_dir: str = "",
    project: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Archive warm-tier DuckDB to cold-tier Parquet.

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
    # `keep_cool` used to be declared here and forwarded to archive_tool(), which
    # has no such parameter — so every authenticated call raised TypeError, which
    # @traced_tool shaped into an ok=False envelope rather than letting it crash.
    # The tool was 100% broken and reported it as an ordinary error. Nothing
    # downstream can honour the flag either: bathos.archive.archive() only exports
    # warm→cold and never removes cool-tier fragments, so there is no deletion for
    # a "keep" switch to suppress. Reinstating it means implementing that first.
    return archive_tool(catalog_dir=catalog_dir, project=project)


@cisternal.tool(registry="bathos", name="archive_artifact")
@traced_tool
@require_write_token
async def mcp_archive_artifact_tool(
    script_path: str = "",
    project_root: str = "",
    catalog_dir: str = "",
    project_slug: str = "",
    verdict: str = "",
    reason: str = "",
    superseded_by: str = "",
    dry_run: bool = False,
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Archive a script + its sidecar + its tracked outputs (stub-in-place, git-native).

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
    return archive_artifact_tool(
        script_path=script_path,
        project_root=project_root,
        catalog_dir=catalog_dir,
        project_slug=project_slug,
        verdict=verdict,
        reason=reason,
        superseded_by=superseded_by,
        dry_run=dry_run,
    )


@cisternal.tool(registry="bathos", name="restore")
@traced_tool
@require_write_token
async def mcp_restore_tool(
    item_id: str = "",
    project_root: str = "",
    catalog_dir: str = "",
    dry_run: bool = False,
    stub_path: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Restore a previously archived script+output bundle.

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
    return restore_tool(
        item_id=item_id,
        project_root=project_root,
        catalog_dir=catalog_dir,
        dry_run=dry_run,
        stub_path=stub_path,
    )


@cisternal.tool(registry="bathos", name="reap")
@traced_tool
@require_write_token
async def mcp_reap_tool(
    catalog_dir: str = "",
    older_than_h: float = 24,
    dry_run: bool = True,
    apply: bool = False,
    revert: bool = False,
    revert_ids: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Reap orphaned runs by marking them abandoned.

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
    return reap_tool(
        catalog_dir=catalog_dir,
        older_than_h=older_than_h,
        dry_run=dry_run,
        apply=apply,
        revert=revert,
        revert_ids=revert_ids,
    )


@cisternal.tool(registry="bathos", name="check")
@traced_tool
async def mcp_check_tool(
    catalog_dir: str = "",
    project_root: str = "",
    status_filter: str = "",
    rebaseline: bool = False,
) -> dict:
    """Check run freshness vs git HEAD."""
    return check_tool(
        catalog_dir=catalog_dir,
        project_root=project_root,
        status_filter=status_filter,
        rebaseline=rebaseline,
    )


@cisternal.tool(registry="bathos", name="capability_probe")
@traced_tool
async def mcp_capability_probe_tool(
    catalog_dir: str = "",
) -> dict:
    """Machine-checkable liveness of Run.seed + the stats battery (B2-06, AC-20; real)."""
    return capability_probe_tool(catalog_dir=catalog_dir)


@cisternal.tool(registry="bathos", name="sync")
@traced_tool
@require_write_token
async def mcp_sync_tool(
    catalog_dir: str = "",
    remote_name: str = "",
    pull: bool = False,
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Sync catalog to/from remote.

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
    return sync_tool(catalog_dir=catalog_dir, remote_name=remote_name, pull=pull)


@cisternal.tool(registry="bathos", name="init")
@traced_tool
@require_write_token
async def mcp_init_tool(
    project_root: str = "",
    catalog_dir: str = "",
    slug: str = "",
    remote: str = "",
    slurm_partition: str = "",
    assign_id: bool = False,
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Initialize project with bathos, or retrofit a project id (D7).

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
    return init_tool(
        project_root=project_root,
        catalog_dir=catalog_dir,
        slug=slug,
        remote=remote,
        slurm_partition=slurm_partition,
        assign_id=assign_id,
    )


@cisternal.tool(registry="bathos", name="log_restore")
@traced_tool
@require_write_token
async def mcp_log_restore_tool(
    project_root: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Restore a project's run log from its mirror (spec 260925 D7, AC-14, AC-27).

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
    return log_restore_tool(project_root=project_root)


@cisternal.tool(registry="bathos", name="run")
@traced_tool
@require_write_token
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
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Run a script and record provenance.

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
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


@cisternal.tool(registry="bathos", name="campaign_create")
@traced_tool
@require_write_token
async def mcp_campaign_create_tool(
    name: str = "",
    mode: str = "exploration",
    project_slug: str = "",
    catalog_dir: str = "",
    question: str = "",
    hypothesis: str = "",
    parent_campaign_id: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Create a new experiment campaign.

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
    return campaign_create_tool(
        name=name,
        mode=mode,
        project_slug=project_slug,
        catalog_dir=catalog_dir,
        question=question,
        hypothesis=hypothesis,
        parent_campaign_id=parent_campaign_id,
    )


@cisternal.tool(registry="bathos", name="campaign_list")
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


@cisternal.tool(registry="bathos", name="campaign_review")
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


@cisternal.tool(registry="bathos", name="campaign_conclude")
@traced_tool
@require_write_token
async def mcp_campaign_conclude_tool(
    campaign_id: str = "",
    outcome_label: str = "",
    conclusion: str = "",
    catalog_dir: str = "",
    negative_check: str = "",
    workspace_root: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Conclude a campaign with an outcome label.

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
    return campaign_conclude_tool(
        campaign_id=campaign_id,
        outcome_label=outcome_label,
        conclusion=conclusion,
        catalog_dir=catalog_dir,
        negative_check=negative_check,
        workspace_root=workspace_root,
    )


@cisternal.tool(
    registry="bathos-cli", name="postmortem_scaffold", cli_group="postmortem", cli_name="scaffold"
)
def postmortem_scaffold_tool(
    run_id: str = "",
    campaign_id: str = "",
    catalog_dir: str | None = None,
    workspace_root: str | None = None,
) -> dict:
    """Scaffold a new postmortem TOML template for a run ID or a campaign ID.

    Pass exactly one of run_id / campaign_id. Returns the path where the template was written.
    """
    from bathos.postmortem import (
        find_run_for_scaffold,
        scaffold_campaign_postmortem_template,
        scaffold_postmortem_template,
    )

    if bool(run_id) == bool(campaign_id):
        return {"error": "Pass exactly one of run_id or campaign_id"}

    cat_dir = _get_catalog_dir(catalog_dir)

    # Explicit workspace_root wins (AC-11); else resolve live fs_root (worktree-aware).
    if workspace_root:
        ws = Path(workspace_root).expanduser().resolve()
    else:
        from bathos.workspace import resolve_workspace

        ws = resolve_workspace().fs_root

    if campaign_id:
        from bathos.campaigns import connect_catalog_db, get_campaign, union_campaign_member_ids
        from bathos.obligations import list_obligations

        db = connect_catalog_db(cat_dir, read_only=True)
        try:
            campaign = get_campaign(db, campaign_id, catalog_dir=cat_dir)
            if campaign is None:
                return {"error": f"Campaign '{campaign_id}' not found"}
            member_ids = union_campaign_member_ids(db, campaign.id, cat_dir)
        finally:
            if db is not None:
                db.close()

        scoped = {campaign.id, *member_ids}
        open_ids = [o.obligation_id for o in list_obligations(ws) if o.entity_id in scoped]
        pm_path = scaffold_campaign_postmortem_template(campaign.id, ws, open_ids)
        return {"path": str(pm_path), "campaign_id": campaign.id, "open_obligations": open_ids}

    run_row = find_run_for_scaffold(run_id, cat_dir)
    if not run_row:
        return {"error": f"Run '{run_id}' not found"}
    command, _project_slug = run_row

    postmortem_path = scaffold_postmortem_template(command, run_id, ws)
    return {"path": str(postmortem_path), "run_id": run_id}


@cisternal.tool(
    registry="bathos-cli", name="postmortem_validate", cli_group="postmortem", cli_name="validate"
)
def postmortem_validate_tool(
    path: str,
    workspace_root: str | None = None,
    strict_files: bool = False,
    catalog_dir: str = "",
) -> dict:
    """Validate a postmortem TOML file.

    Returns {'validation_ok': True} on success or {'validation_ok': False, 'errors': [...]} on failure.
    """
    from bathos.postmortem import (
        parse_postmortem,
        postmortem_applied_event_data,
        validate_postmortem,
    )

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
    warnings = list(result.warnings)
    if result.ok:
        if pm.run_id:
            # run.postmortem_applied (delivery step 2b / AC-25): behind the flag
            # only -- flag off leaves this validate command exactly as before
            # (the legacy fold still comes from compact.py re-reading the file).
            from bathos.runlog.emit import current_mode, emit_event, unit_of_work

            cat_dir = _get_catalog_dir(catalog_dir or None)
            with unit_of_work(cat_dir):
                if current_mode():
                    # amendment_removed (spec 261026 D5, log mode only): an amendment an earlier
                    # validation recorded that this file has dropped.
                    from bathos.postmortem import removed_amendments

                    dropped = removed_amendments(_previous_amendments(cat_dir, pm.run_id), pm)
                    warnings += [f"amendment_removed: {item}" for item in dropped]
                    event_data = postmortem_applied_event_data(pm, pm_path)
                    # AC-17 finding: the legacy fold (compact.py's cwd walk)
                    # always stores `postmortem_path` workspace-RELATIVE
                    # (`rel_path = pm_file.relative_to(workspace_root)`,
                    # compact.py) so the fold must match that shape rather
                    # than whatever the caller happened to pass as `path` --
                    # otherwise `runs.postmortem_path` silently changes
                    # representation (absolute vs relative) across cut-over.
                    with contextlib.suppress(ValueError):
                        event_data["path"] = str(pm_path.resolve().relative_to(ws.resolve()))
                    emit_event(
                        kind="run.postmortem_applied",
                        entity=[pm.run_id],
                        data=event_data,
                        cwd=ws,
                    )
        return {
            "validation_ok": True,
            "run_id": pm.run_id,
            "campaign_id": pm.campaign_id,
            "hypothesis_status": pm.hypothesis_status,
            "warnings": warnings,
        }
    return {
        "validation_ok": False,
        "errors": [e.message for e in result.errors],
        "warnings": warnings,
    }


def _previous_amendments(cat_dir: Path, run_id: str) -> list[dict]:
    """Amendments recorded by the latest `run.postmortem_applied` event for `run_id` ([] if none
    or unreadable): the baseline `amendment_removed` is measured against."""
    from bathos.runlog.index import connect_read
    from bathos.runlog.ingest import _fetch_events_by_entity_key

    try:
        db = connect_read(cat_dir)
    except Exception:  # noqa: BLE001 -- no readable index: nothing to compare against
        return []
    try:
        events = [
            e
            for e in _fetch_events_by_entity_key(db, [run_id])
            if e.get("kind") == "run.postmortem_applied"
        ]
    except Exception:  # noqa: BLE001
        return []
    finally:
        db.close()
    if not events:
        return []
    latest = max(events, key=lambda e: (e.get("ts", ""), e.get("eid", "")))
    recorded = (latest.get("data") or {}).get("amendments")
    return [a for a in recorded if isinstance(a, dict)] if isinstance(recorded, list) else []


@cisternal.tool(registry="bathos-cli", name="postmortem_get", cli_group="postmortem", cli_name="show")
def postmortem_get_tool(
    run_id: str = "",
    campaign_id: str = "",
    workspace_root: str | None = None,
    strict_files: bool = False,
) -> dict:
    """Retrieve and validate postmortem data for a run ID or a campaign ID.

    Pass exactly one of run_id / campaign_id. Returns the parsed postmortem fields or an
    error dict if not found or if validation fails (e.g. a refutation-mapping violation:
    hypothesis_status='refuted' with verdict_override='pass').
    """
    from bathos.postmortem import find_postmortem, validate_postmortem

    if bool(run_id) == bool(campaign_id):
        return {"error": "Pass exactly one of run_id or campaign_id"}

    # Explicit workspace_root wins (AC-11); else resolve live fs_root (worktree-aware).
    if workspace_root:
        ws = Path(workspace_root).expanduser().resolve()
    else:
        from bathos.workspace import resolve_workspace

        ws = resolve_workspace().fs_root

    found = find_postmortem(ws, run_id=run_id, campaign_id=campaign_id)
    if found is None:
        scope = f"run_id '{run_id}'" if run_id else f"campaign_id '{campaign_id}'"
        return {"error": f"No postmortem found for {scope}"}

    pm_file, pm = found

    # Drift detection needs the Run row, so it only applies to a run-scoped postmortem
    # -- a campaign has no single git hash or dirty flag to compare against.
    run_obj = None
    if run_id:
        cat_dir = _get_catalog_dir(None)
        db_path = cat_dir / "bathos.db"
        if db_path.exists():
            from bathos.schema import Run

            con = connect_read(cat_dir)
            try:
                arrow_tbl = con.execute("SELECT * FROM runs WHERE id = ?", [run_id]).arrow()
                if arrow_tbl.num_rows > 0:
                    run_obj = Run.from_arrow_row(arrow_tbl.to_pydict(), 0)
            except Exception:
                pass
            finally:
                con.close()
        if not run_obj:
            from bathos.catalog import read_runs

            for r in read_runs(cat_dir):
                if r.id == run_id:
                    run_obj = r
                    break

    validation = validate_postmortem(
        pm, workspace_root=ws, run=run_obj, strict_files=strict_files
    )
    # Amendments render from the file whether or not validation passes (drift included), under a
    # banner saying they never change the recorded outcome (spec 261026 D5).
    from bathos.postmortem import AMENDMENTS_BANNER, amendments_as_dicts

    amendment_fields = {
        "format": pm.format,
        "amendments": amendments_as_dicts(pm),
        "amendments_banner": AMENDMENTS_BANNER if pm.amendments else None,
        "warnings": list(validation.warnings),
    }
    if not validation.ok:
        return {
            "error": "Validation failed: " + "; ".join(e.message for e in validation.errors),
            "hypothesis_status": pm.hypothesis_status,
            "verdict_override": pm.verdict_override,
            **amendment_fields,
        }

    # Read-only: run.postmortem_applied is emitted by postmortem validate/register
    # (spec BC-2), never by a read.
    return {
        "run_id": pm.run_id,
        "campaign_id": pm.campaign_id,
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
        "discharges": pm.discharges,
        "violated_cards": pm.violated_cards,
        "path": str(pm_file),
        **amendment_fields,
    }


@cisternal.tool(registry="bathos")
@traced_tool
@require_write_token
async def postmortem_scaffold(
    run_id: str = "",
    campaign_id: str = "",
    catalog_dir: str | None = None,
    workspace_root: str | None = None,
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Scaffold a new postmortem TOML template for a run ID or a campaign ID.

    Pass exactly one of run_id / campaign_id. Returns the path where the template was written.

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841).
    """
    return postmortem_scaffold_tool(
        run_id=run_id,
        campaign_id=campaign_id,
        catalog_dir=catalog_dir,
        workspace_root=workspace_root,
    )


@cisternal.tool(registry="bathos")
@traced_tool
async def postmortem_validate(
    path: str,
    workspace_root: str | None = None,
    strict_files: bool = False,
    catalog_dir: str = "",
) -> dict:
    """Validate a postmortem TOML file.

    Returns {'validation_ok': True} on success or {'validation_ok': False, 'errors': [...]} on failure.
    """
    return postmortem_validate_tool(
        path, workspace_root=workspace_root, strict_files=strict_files, catalog_dir=catalog_dir
    )


@cisternal.tool(registry="bathos")
@traced_tool
async def postmortem_get(
    run_id: str = "",
    campaign_id: str = "",
    workspace_root: str | None = None,
) -> dict:
    """Retrieve postmortem data for a run ID or a campaign ID.

    Pass exactly one of run_id / campaign_id. Returns the parsed postmortem fields or an
    error dict if not found.
    """
    return postmortem_get_tool(run_id=run_id, campaign_id=campaign_id, workspace_root=workspace_root)


@cisternal.tool(registry="bathos-cli", name="claim_scaffold", cli_group="claim", cli_name="scaffold")
def claim_scaffold_tool(
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
        return {"error": _missing_catalog_message(db_path, cat_dir)}

    # Explicit workspace_root wins; else resolve live fs_root (worktree-aware).
    if workspace_root:
        ws = Path(workspace_root).expanduser().resolve()
    else:
        from bathos.workspace import resolve_workspace

        ws = resolve_workspace().fs_root

    try:
        db = connect_read(cat_dir)
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


@cisternal.tool(registry="bathos-cli", name="claim_validate", cli_group="claim", cli_name="validate")
def claim_validate_tool(
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
            cat_dir = Path(catalog_dir).expanduser().resolve()
            db_path = cat_dir / "bathos.db"
            if db_path.exists():
                db = connect_read(cat_dir, read_only=True)
        except Exception:
            pass

    from bathos.workspace import resolve_workspace

    ws_root = resolve_workspace().fs_root
    result = validate_claim(claim, db=db, workspace_root=ws_root)

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


@cisternal.tool(registry="bathos")
@traced_tool
@require_write_token
async def claim_scaffold(
    campaign_id: str,
    catalog_dir: str | None = None,
    workspace_root: str | None = None,
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Scaffold a new claim TOML template for the given campaign ID.

    Returns the path where the template was written.

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841).
    """
    return claim_scaffold_tool(
        campaign_id=campaign_id, catalog_dir=catalog_dir, workspace_root=workspace_root
    )


@cisternal.tool(registry="bathos")
@traced_tool
async def claim_validate(
    path: str,
    catalog_dir: str | None = None,
) -> dict:
    """Validate a claim TOML file.

    Returns {'ok': True} on success or {'ok': False, 'errors': [...]} on failure.
    """
    return claim_validate_tool(path, catalog_dir=catalog_dir)


def _missing_catalog_message(db_path: Path, cat_dir: Path) -> str:
    """Explain a missing catalog database; name the .bth.toml declaration when it caused it.

    A declared `[project] catalog_dir` that does not exist is never replaced by the global
    catalog. The message says so, so the user creates the catalog or removes the key.
    """
    message = f"Catalog database not found at {db_path}"
    cfg = find_project_config(Path.cwd())
    if cfg is None:
        return message
    try:
        from bathos.runlog.migrate import explicit_catalog_dir

        declared = explicit_catalog_dir(cfg.parent)
    except Exception:
        return message
    if declared is None or Path(declared) != Path(cat_dir):
        return message
    return (
        f"{message}: the catalog_dir declared in {cfg} ({declared}) does not exist; "
        "bth takes no fallback to the global catalog. Create that catalog "
        "(bth init in this project) or remove catalog_dir from .bth.toml."
    )


def _claim_register_sync(
    path: str,
    campaign_id: str,
    cat_dir: Path,
    workspace_root: Path,
    *,
    force: bool = False,
) -> dict:
    """Register a claim; always close the DuckDB write connection."""
    from bathos.campaigns import connect_catalog_db
    from bathos.claim import register_claim
    from bathos.runlog.emit import unit_of_work

    # After cut-over this is the index, not the frozen `bathos.db`; register_claim
    # flag-gates its writes via emit_or_legacy (debt #2843).
    with unit_of_work(cat_dir):
        db = connect_catalog_db(Path(cat_dir), read_only=False)
        if db is None:
            db_path = Path(cat_dir) / "bathos.db"
            return {"ok": False, "error": _missing_catalog_message(db_path, cat_dir)}
        try:
            register_claim(
                Path(path), campaign_id, db, workspace_root, force=force, catalog_dir=cat_dir
            )
            return {
                "ok": True,
                "path": path,
                "campaign_id": campaign_id,
                "force": force,
                "message": f"Registered claim for campaign {campaign_id}",
            }
        except BreakdownRefused as e:
            return _breakdown_refusal_result(e)
        except (RuntimeError, FileNotFoundError) as e:
            return {"ok": False, "error": str(e), "error_code": "register_failed"}
        finally:
            db.close()


@cisternal.tool(registry="bathos-cli", name="claim_register", cli_group="claim", cli_name="register")
def claim_register_tool(
    path: str,
    campaign_id: str,
    catalog_dir: str | None = None,
    workspace_root: str | None = None,
    force: bool = False,
) -> dict:
    """Register a claim TOML file with a campaign (path + SHA256 anchor).

    NOT auto-classified as a direct decoration by the codegen audit's
    ast.walk-based delegate scan -- `claim_register`'s async body resolves
    catalog_dir/workspace_root (env-var fallback, live workspace
    resolution) before its trailing `return _claim_register_sync(...)`,
    which the walk finds regardless of what precedes it. Decorating
    `_claim_register_sync` directly would have silently dropped that
    resolution for the CLI (it takes already-resolved Path args with no
    defaults). This wrapper restores it; `_claim_register_sync` itself
    stays the shared low-level implementation both callers delegate to.
    """
    cat_dir = _get_catalog_dir(catalog_dir)

    if workspace_root:
        ws = Path(workspace_root).expanduser().resolve()
    else:
        from bathos.workspace import resolve_workspace

        ws = resolve_workspace().fs_root

    return _claim_register_sync(path, campaign_id, cat_dir, ws, force=force)


@cisternal.tool(registry="bathos-cli", name="gate_stamp", cli_group="gate", cli_name="stamp")
def gate_stamp_tool(
    gate_name: str,
    result: str,
    workspace_root: str | None = None,
) -> dict:
    """Record a self-attested pass/fail for a BP-2 synthetic-recovery gate at current git HEAD.

    bathos does not run the test itself — run your project's own invariant test first, then
    stamp the result here.
    """
    from bathos.gate import stamp_gate
    from bathos.git import capture_git_state

    if result not in ("pass", "fail"):
        return {"ok": False, "error": f"result must be 'pass' or 'fail', got {result!r}"}

    if workspace_root:
        ws = Path(workspace_root).expanduser().resolve()
    else:
        from bathos.workspace import resolve_workspace

        ws = resolve_workspace().fs_root

    git_state = capture_git_state(ws)
    if git_state.hash == "unknown":
        return {"ok": False, "error": "could not resolve git HEAD sha — is this a git repository?"}

    entry = stamp_gate(ws, gate_name, result, git_state.hash)
    return {
        "ok": True,
        "gate_name": entry.gate_name,
        "result": entry.result,
        "sha": entry.sha,
        "dirty_worktree": git_state.dirty,
    }


@cisternal.tool(registry="bathos-cli", name="gate_status", cli_group="gate", cli_name="status")
def gate_status_tool(
    gate_name: str,
    guards: list[str],
    workspace_root: str | None = None,
) -> dict:
    """Report GREEN/STALE/RED/UNKNOWN for a named BP-2 synthetic-recovery gate."""
    from bathos.gate import gate_state

    if workspace_root:
        ws = Path(workspace_root).expanduser().resolve()
    else:
        from bathos.workspace import resolve_workspace

        ws = resolve_workspace().fs_root

    state = gate_state(ws, gate_name, guards)
    return {"ok": True, "gate_name": gate_name, "state": state}


@cisternal.tool(registry="bathos")
@traced_tool
@require_write_token
async def claim_register(
    path: str,
    campaign_id: str,
    catalog_dir: str | None = None,
    workspace_root: str | None = None,
    force: bool = False,
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Register a claim TOML file with a campaign (path + SHA256 anchor).

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
    return claim_register_tool(
        path, campaign_id, catalog_dir=catalog_dir, workspace_root=workspace_root, force=force
    )


@cisternal.tool(registry="bathos")
@traced_tool
@require_write_token
async def gate_stamp(
    gate_name: str,
    result: str,
    workspace_root: str | None = None,
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Record a self-attested pass/fail for a BP-2 synthetic-recovery gate at current git HEAD.

    bathos does not run the test itself — run your project's own invariant test first, then
    stamp the result here.
    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
    return gate_stamp_tool(gate_name, result, workspace_root=workspace_root)


@cisternal.tool(registry="bathos")
@traced_tool
async def gate_status(
    gate_name: str,
    guards: list[str],
    workspace_root: str | None = None,
) -> dict:
    """Report GREEN/STALE/RED/UNKNOWN for a named BP-2 synthetic-recovery gate."""
    return gate_status_tool(gate_name, guards, workspace_root=workspace_root)


@cisternal.tool(registry="bathos")
@traced_tool
@require_write_token
async def claim_attest_parity(
    campaign_id: str,
    parity_run_id: str,
    catalog_dir: str | None = None,
    workspace_root: str | None = None,
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Bind a passing literature-parity run to a campaign's registered claim (F4).

    Wraps attest_parity(): validates the run, updates the claim file atomically,
    and re-anchors the campaign claim_sha256 in the warm catalog.

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841).
    """
    return campaign_attest_parity_tool(
        campaign_id=campaign_id,
        parity_run_id=parity_run_id,
        catalog_dir=catalog_dir or "",
        workspace_root=workspace_root or "",
    )


# ── structured document authoring (bathos.authoring) ─────────────────────────


@cisternal.tool(registry="bathos")
@traced_tool
async def doc_schema(kind: str = "claim") -> dict:
    """Field schema for an authored document kind, with a worked example.

    Read-only discovery: call this after a `document_invalid` refusal to see the exact
    field names, types and required-ness, rather than guessing at TOML structure.
    Progressive disclosure -- the schema is fetched on demand instead of being carried
    in every tool definition.
    """
    schemas = {"claim": ClaimPayload}
    model = schemas.get(kind)
    if model is None:
        return {
            "ok": False,
            "error": f"unknown document kind {kind!r}; known kinds: {sorted(schemas)}",
            "error_code": BathosErrorCode.INVALID_PARAM.value,
            "resolution_hint": RESOLUTION_HINTS[BathosErrorCode.INVALID_PARAM],
        }

    example = None
    if kind == "claim":
        from bathos.authoring.scaffolds import scaffold_claim_payload

        example = scaffold_claim_payload().model_dump(exclude_none=True)

    return {
        "ok": True,
        "kind": kind,
        "schema": model.model_json_schema(),
        "example": example,
        "notes": (
            "Sub-blocks are flattened with prefixed keys (reference_parity_paper rather "
            "than a nested reference_parity object); the renderer restores the real TOML "
            "nesting. parity_run_id is not author-supplied -- bind it later with "
            "claim_attest_parity."
        ),
    }


def claim_author_tool(
    claim: ClaimPayload | dict,
    path: str = "",
    campaign_id: str = "",
    catalog_dir: str | None = None,
    workspace_root: str | None = None,
    force: bool = False,
    actor: str = "mcp",
    reason: str = "",
) -> dict:
    """Author a claim from a structured payload -- no hand-written TOML.

    Shared core for both callers: the MCP tool (`claim_author`, below) takes
    a typed `ClaimPayload` for schema discoverability, for callers that
    validate it at the FastMCP protocol boundary -- but a direct in-process
    caller can and does pass a raw dict (see test_authoring_parity.py's
    `_author_via_mcp`, which bypasses that boundary entirely), so this
    accepts either and passes it straight through unchanged; `author_claim`
    itself does the ClaimPayload.model_validate() coercion (do NOT call
    .model_dump() here first -- that breaks the raw-dict path, since a plain
    dict has no .model_dump()). The CLI (`claim_author_cli_tool`) reads a raw
    JSON blob via --from-json/stdin (matching the shipped Typer command's
    UX, cli.py's claim_author_cmd) and passes the parsed dict through the
    same way.

    The payload is rendered to canonical TOML, re-parsed with the same reader the read
    path uses, and validated BEFORE anything is written. A document that would fail
    validation is not written at all, so the target file is either absent or valid --
    there is no write-then-flag state.

    Supply either `path` (relative to the workspace root) or `campaign_id`, in which case
    the claim lands at `.bth/claims/<campaign name>.claim.toml`. Passing `campaign_id`
    also lets validation run its catalog-aware checks (baseline parity, gate state).

    On refusal, `error_code` is `document_invalid` (fix the fields) or `document_conflict`
    (a document already exists -- pass force=true to overwrite). Call `doc_schema` for the
    field list.
    """
    from bathos.authoring.write import author_claim
    from bathos.claim import resolve_claim_path

    if not path and not campaign_id:
        return {
            "ok": False,
            "error": "supply either path= or campaign_id=",
            "error_code": BathosErrorCode.INVALID_PARAM.value,
            "resolution_hint": RESOLUTION_HINTS[BathosErrorCode.INVALID_PARAM],
        }

    if workspace_root:
        ws = Path(workspace_root).expanduser().resolve()
    else:
        from bathos.workspace import resolve_workspace

        ws = resolve_workspace().fs_root

    db = None
    try:
        if campaign_id:
            cat_dir = _get_catalog_dir(catalog_dir)
            db_path = cat_dir / "bathos.db"
            if not db_path.exists():
                return {
                    "ok": False,
                    "error": f"Catalog database not found at {db_path}",
                    "error_code": BathosErrorCode.CATALOG_ERROR.value,
                    "resolution_hint": RESOLUTION_HINTS[BathosErrorCode.CATALOG_ERROR],
                }

            from bathos.campaigns import CampaignError, _resolve_campaign_id

            db = connect_read(cat_dir)
            try:
                full_id = _resolve_campaign_id(db, campaign_id)
            except CampaignError as e:
                return {
                    "ok": False,
                    "error": str(e),
                    "error_code": BathosErrorCode.CAMPAIGN_ERROR.value,
                    "resolution_hint": RESOLUTION_HINTS[BathosErrorCode.CAMPAIGN_ERROR],
                }
            if not path:
                rows = db.execute("SELECT name FROM campaigns WHERE id = ?", [full_id]).fetchall()
                if not rows:
                    return {
                        "ok": False,
                        "error": f"Campaign {campaign_id} not found",
                        "error_code": BathosErrorCode.CAMPAIGN_ERROR.value,
                        "resolution_hint": RESOLUTION_HINTS[BathosErrorCode.CAMPAIGN_ERROR],
                    }
                path = str(Path(".bth") / "claims" / f"{rows[0][0]}.claim.toml")

        target = resolve_claim_path(path, ws)
        result = author_claim(
            claim,
            target,
            force=force,
            actor=actor,
            reason=reason,
            catalog_db=db,
            workspace_root=ws,
            campaign_id=campaign_id or None,
        )
    finally:
        if db is not None:
            db.close()

    envelope = result.as_envelope()
    if result.ok and campaign_id:
        envelope["campaign_id"] = campaign_id
        envelope["message"] = (
            f"Wrote claim to {result.path}. Register it with claim_register to anchor "
            "its sha256 against the campaign."
        )
    return envelope


@cisternal.tool(registry="bathos-cli", name="claim_author", cli_group="claim", cli_name="author")
def claim_author_cli_tool(
    path: str = "",
    campaign_id: str = "",
    from_json: str = "",
    catalog_dir: str | None = None,
    workspace_root: str | None = None,
    force: bool = False,
    reason: str = "",
) -> dict:
    """CLI entry point for `claim author`.

    Reads a JSON claim payload from a file (--from-json PATH) or stdin
    (--from-json -), matching the shipped Typer command's UX exactly
    (cli.py's claim_author_cmd), then delegates to claim_author_tool -- the
    same shared implementation the MCP tool wraps after a ClaimPayload
    model_dump().
    """
    import json
    import sys

    if not from_json:
        return {"ok": False, "error": "--from-json is required (a file path, or '-' for stdin)"}

    try:
        raw = sys.stdin.read() if from_json == "-" else Path(from_json).read_text()
    except OSError as e:
        return {"ok": False, "error": str(e), "error_code": "file_not_found"}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"payload is not valid JSON: {e}"}

    return claim_author_tool(
        claim=payload,
        path=path,
        campaign_id=campaign_id,
        catalog_dir=catalog_dir,
        workspace_root=workspace_root,
        force=force,
        actor="cli",
        reason=reason,
    )


@cisternal.tool(registry="bathos")
@traced_tool
@require_write_token
async def claim_author(
    claim: ClaimPayload,
    path: str = "",
    campaign_id: str = "",
    catalog_dir: str | None = None,
    workspace_root: str | None = None,
    force: bool = False,
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Author a claim from a structured payload -- no hand-written TOML.

    The payload is rendered to canonical TOML, re-parsed with the same reader the read
    path uses, and validated BEFORE anything is written. A document that would fail
    validation is not written at all, so the target file is either absent or valid --
    there is no write-then-flag state.

    Supply either `path` (relative to the workspace root) or `campaign_id`, in which case
    the claim lands at `.bth/claims/<campaign name>.claim.toml`. Passing `campaign_id`
    also lets validation run its catalog-aware checks (baseline parity, gate state).

    On refusal, `error_code` is `document_invalid` (fix the fields) or `document_conflict`
    (a document already exists -- pass force=true to overwrite). Call `doc_schema` for the
    field list.

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841).
    """
    return claim_author_tool(
        claim=claim,
        path=path,
        campaign_id=campaign_id,
        catalog_dir=catalog_dir,
        workspace_root=workspace_root,
        force=force,
        actor="mcp",
    )


@cisternal.tool(registry="bathos-cli", name="new_experiment", cli_name="new-experiment")
def new_experiment_tool(
    name: str,
    workspace_root: str | None = None,
    force: bool = False,
    mode: str = "explore",
    workload: str | None = None,
) -> dict:
    """Scaffold an experiment script and its pre-registration sidecar.

    `mode="explore"` (default) writes an exploratory sidecar: a `question`, an
    `[observe]` block naming the outputs you will look at, and no uncommented
    `[outcomes]`. It validates clean as written. `mode="confirm"` writes a
    confirmatory sidecar with `${THRESHOLD}` / `${SOURCE}` placeholders; those
    show up in `sidecar_validation.needs_filling`, not as errors. Any other
    mode is rejected. `workload="jax"` appends the launch-gate tables: a live
    `[requires]` (a probe record under outputs/profiling/<name>/ at HEAD) and a
    commented `[budget]` to fill from the `xtrax profile` report. Any other
    workload is rejected.

    The scaffold's optional blocks ship commented out on purpose: an
    uncommented `[[review.literature]]` with empty fields is a validation
    ERROR on a file the author has not touched yet.

    Returns the two paths written plus the sidecar's validation state, so
    the caller can see immediately which placeholders still need filling.
    """
    from bathos.new_experiment import scaffold_experiment

    if workspace_root:
        ws = Path(workspace_root).expanduser().resolve()
    else:
        from bathos.workspace import resolve_workspace

        ws = resolve_workspace().fs_root

    try:
        scaffolded = scaffold_experiment(name, ws, force=force, mode=mode, workload=workload)
    except FileExistsError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": BathosErrorCode.DOCUMENT_CONFLICT.value,
            "resolution_hint": RESOLUTION_HINTS[BathosErrorCode.DOCUMENT_CONFLICT],
        }
    except (OSError, ValueError) as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": BathosErrorCode.EXPORT_ERROR.value,
            "resolution_hint": RESOLUTION_HINTS[BathosErrorCode.EXPORT_ERROR],
        }

    # scaffold_experiment already validated the written sidecar: unfilled ${...} placeholders
    # come back as needs_filling; anything else would have raised and removed the files.
    validation = {
        "checked": True,
        "ok": not scaffolded.needs_filling,
        "errors": [],
        "needs_filling": scaffolded.needs_filling,
    }

    return {
        "ok": True,
        "error_code": None,
        "error": None,
        "resolution_hint": None,
        "name": name,
        "mode": scaffolded.mode,
        "epistemic_class": scaffolded.epistemic_class,
        "script": str(scaffolded.script),
        "sidecar": str(scaffolded.sidecar),
        "name_warning": scaffolded.name_warning or None,
        "sidecar_validation": validation,
        "message": (
            f"Scaffolded {scaffolded.script} and {scaffolded.sidecar} ({scaffolded.mode} mode). "
            + (
                f"Fill the {len(scaffolded.needs_filling)} placeholder(s) in the sidecar, "
                "then validate_sidecar before running."
                if scaffolded.needs_filling
                else "Sidecar validates as written."
            )
        ),
    }


@cisternal.tool(registry="bathos")
@traced_tool
@require_write_token
async def new_experiment(
    name: str,
    workspace_root: str | None = None,
    force: bool = False,
    mode: str = "explore",
    workload: str | None = None,
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Scaffold an experiment script and its pre-registration sidecar.

    `mode` is "explore" (default: exploratory sidecar with [observe], validates as written)
    or "confirm" (confirmatory sidecar with ${THRESHOLD}/${SOURCE} placeholders listed in
    sidecar_validation.needs_filling). Any other value is rejected. `workload` is None
    (default) or "jax" (adds the launch-gate [requires] table and a commented [budget]).

    Closes a real parity gap: `bth new-experiment` existed only on the CLI, so an agent
    driving bathos through MCP had to hand-author `<stem>.bth.toml` from memory -- the
    exact situation the structured authoring layer exists to remove.

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841).
    """
    return new_experiment_tool(
        name=name, workspace_root=workspace_root, force=force, mode=mode, workload=workload
    )


@cisternal.tool(registry="bathos-cli", name="validate_sidecar", cli_name="validate-sidecar")
def validate_sidecar_tool(
    path: str,
    campaign_id: str | None = None,
) -> dict:
    """Validate a sidecar TOML file for structural integrity.

    Args:
        path: Path to .bth.toml sidecar file
        campaign_id: Optional campaign ID (or prefix). If given, cross-checks
            claim_discriminates/claim_isolates against the campaign's registered claim (#3719).

    Returns:
        {'validation_ok': True} on success or {'validation_ok': False, 'errors': [...],
        'error': '; '.join(errors)} on failure. The extra 'error' key (absent on success)
        is CLI-convention normalization -- render_or_exit/cyclopts_result_action key off
        "error", not this tool's own "validation_ok" field.
    """
    from bathos.campaigns import CampaignError
    from bathos.claim import load_registered_claim
    from bathos.sidecar import SidecarError, parse_sidecar
    from bathos.validate import validate_sidecar as validate_sidecar_impl

    def _fail(errors: list[str], warnings: list[str] | None = None) -> dict:
        out = {"validation_ok": False, "errors": errors, "error": "; ".join(errors)}
        if warnings:
            out["warnings"] = warnings
        return out

    sidecar_path = Path(path)
    if not sidecar_path.exists():
        return _fail([f"File not found: {path}"])

    try:
        sidecar = parse_sidecar(sidecar_path)
    except SidecarError as e:
        return _fail([str(e)])

    claim = None
    if campaign_id:
        db = connect_read(_get_catalog_dir(None), missing="empty")
        try:
            claim = load_registered_claim(db, campaign_id)
        except (CampaignError, FileNotFoundError, ValueError) as e:
            return _fail([str(e)])
        finally:
            db.close()

    # Same prior-run answer `bth run` gets (prereg.gate_check): looked up only for a stage that
    # needs it, so legacy exploration/calibration sidecars validate as they do at run time.
    from bathos.prereg import _stem_has_prior_runs

    stem_has_runs = sidecar.stage_name_raw in ("exploration", "calibration") and (
        _stem_has_prior_runs(sidecar_path, _get_catalog_dir(None))
    )
    result = validate_sidecar_impl(
        sidecar,
        sidecar_path=sidecar_path,
        claim=claim,
        stem_has_prior_runs=stem_has_runs,
    )

    if result.errors:
        error_msgs = [f"{e.field}: {e.message}" for e in result.errors]
        return _fail(error_msgs, result.warnings)

    ok_result: dict = {"validation_ok": True, "path": path}
    if result.warnings:
        ok_result["warnings"] = result.warnings
    return ok_result


@cisternal.tool(registry="bathos")
@traced_tool
async def validate_sidecar(
    path: str,
    campaign_id: str | None = None,
) -> dict:
    """Validate a sidecar TOML file for structural integrity.

    Args:
        path: Path to .bth.toml sidecar file
        campaign_id: Optional campaign ID (or prefix). If given, cross-checks
            claim_discriminates/claim_isolates against the campaign's registered claim (#3719).

    Returns:
        {'validation_ok': True} on success or {'validation_ok': False, 'errors': [...]} on failure.
    """
    result = validate_sidecar_tool(path=path, campaign_id=campaign_id)
    # validate_sidecar_tool injects an "error" key for CLI rendering purposes
    # (render_or_exit's uniform contract) -- strip it here so this MCP tool's
    # envelope is byte-identical to its pre-extraction shape: traced_tool
    # would otherwise read the presence of "error" and flip its default
    # ok=True to ok=False, a real behavior change this refactor must not
    # introduce as a side effect.
    result.pop("error", None)
    return result


@cisternal.tool(registry="bathos-cli", name="list_outputs", cli_group="outputs", cli_name="list")
def list_outputs_tool(
    run_id: str,
    workspace_root: str | None = None,
    live: bool = False,
    catalog_dir: str | None = None,
) -> dict:
    """List output files for a given run ID.

    Returns parsed output metadata from the run, optionally re-stated if live=True.
    """
    from bathos.config import default_catalog_dir, find_project_config, load_project_config
    from bathos.query import get_run

    if catalog_dir:
        cat = Path(catalog_dir).expanduser().resolve()
    elif workspace_root:
        ws = Path(workspace_root)
        config_path = find_project_config(ws)
        cat = load_project_config(config_path).catalog_dir if config_path else default_catalog_dir()
    else:
        config_path = find_project_config()
        cat = load_project_config(config_path).catalog_dir if config_path else default_catalog_dir()

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


@cisternal.tool(registry="bathos-cli", name="outputs_summary", cli_group="outputs", cli_name="summary")
def outputs_summary_tool(
    workspace_root: str | None = None,
    project: str | None = None,
    since: str | None = None,
    catalog_dir: str | None = None,
) -> dict:
    """Aggregate output file statistics across runs.

    Returns summary rows grouped by project.
    """
    from bathos.config import default_catalog_dir, find_project_config, load_project_config

    if catalog_dir:
        cat = Path(catalog_dir).expanduser().resolve()
    elif workspace_root:
        ws = Path(workspace_root)
        config_path = find_project_config(ws)
        cat = load_project_config(config_path).catalog_dir if config_path else default_catalog_dir()
    else:
        config_path = find_project_config()
        cat = load_project_config(config_path).catalog_dir if config_path else default_catalog_dir()

    db_path = cat / "bathos.db"
    if not db_path.exists():
        return {
            "rows": [],
            "since": since,
            "note": "Catalog not yet compacted. Run 'bth compact' first.",
        }

    # Query warm tier
    con = connect_read(cat)
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


@cisternal.tool(registry="bathos", name="list_outputs")
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


@cisternal.tool(registry="bathos", name="outputs_summary")
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


def _output_tier_error_result(exc: Exception) -> dict:
    """Shape an output-tiering refusal as the tool error dict: the `error` text leads with the code (the CLI
    prints only that text), `error_code` carries it for MCP callers."""
    from bathos.cluster import MyxcelCapabilityGapError
    from bathos.output_tiers.config import OutputTiersConfigError
    from bathos.output_tiers.reference import OutputTierError
    from bathos.runlog.emit import RunNotLaunchedError

    if isinstance(exc, OutputTierError):
        try:
            code = BathosErrorCode(exc.error_code)
        except ValueError:
            code = BathosErrorCode.INVALID_PARAM
        message = f"{exc.error_code}: {exc.detail}"
    elif isinstance(exc, OutputTiersConfigError | MyxcelCapabilityGapError):
        try:
            code = BathosErrorCode(exc.error_code)
        except ValueError:
            code = BathosErrorCode.INVALID_PARAM
        message = f"{exc.error_code}: {exc}"
    elif isinstance(exc, RunNotLaunchedError):
        code = BathosErrorCode.CATALOG_ERROR
        message = f"catalog_error: {exc}"
    else:  # callers only pass the two types above; never mislabel anything else as a refusal
        code = BathosErrorCode.INTERNAL
        message = f"internal: {exc}"
    return {"error": message, "error_code": code.value, "resolution_hint": RESOLUTION_HINTS[code]}


@cisternal.tool(registry="bathos", name="outputs_hash")
@traced_tool
async def mcp_outputs_hash_tool(
    run_id: str,
    worktree_root: str = "",
    catalog_dir: str | None = None,
) -> dict:
    """Re-hash a run's live output files against its reference hashes (read-only)."""
    return outputs_hash_tool(
        run_id=run_id, worktree_root=worktree_root, catalog_dir=catalog_dir or ""
    )


@cisternal.tool(registry="bathos", name="outputs_adopt")
@traced_tool
async def mcp_outputs_adopt_tool(
    run_id: str,
    root: list[str] | None = None,
    reason: str = "",
    retract: str = "",
    worktree_root: str = "",
    dry_run: bool = False,
    catalog_dir: str | None = None,
) -> dict:
    """Adopt output files of a finished pre-standard run into its reference hashes, or retract an adopt."""
    return outputs_adopt_tool(
        run_id=run_id,
        root=root,
        reason=reason,
        retract=retract,
        worktree_root=worktree_root,
        dry_run=dry_run,
        catalog_dir=catalog_dir or "",
    )


@cisternal.tool(registry="bathos-cli", name="outputs_hash", cli_group="outputs", cli_name="hash")
def outputs_hash_tool(
    run_id: str = "",
    worktree_root: str = "",
    catalog_dir: str = "",
) -> dict:
    """Re-hash a run's live output files against its reference hashes (read-only).

    Reports match / drift / missing per path. `run_id` may be a unique prefix. `worktree_root` is required
    only for an imported run, whose recorded root is not trustworthy.
    """
    from bathos.output_tiers.adopt import hash_outputs
    from bathos.output_tiers.reference import OutputTierError
    from bathos.runlog.emit import RunNotLaunchedError

    if not run_id:
        return {"error": "run_id parameter is required"}
    try:
        return hash_outputs(
            _get_catalog_dir(catalog_dir or None),
            run_id,
            worktree_root=worktree_root or None,
        )
    except (OutputTierError, RunNotLaunchedError) as e:
        return _output_tier_error_result(e)


@cisternal.tool(registry="bathos-cli", name="outputs_adopt", cli_group="outputs", cli_name="adopt")
def outputs_adopt_tool(
    run_id: str = "",
    root: list[str] | None = None,
    reason: str = "",
    retract: str = "",
    worktree_root: str = "",
    dry_run: bool = False,
    catalog_dir: str = "",
) -> dict:
    """Adopt output files of a finished pre-standard run into its reference hashes.

    Hashes each `root` now and records the paths that have no run_end entry as an `adopted` event. A relative
    root resolves against the run's worktree root. Closed to a run that declared `[outputs] roots`
    (adopt_closed), and never re-references a path whose bytes differ from the run's own record
    (run_end_conflict). `retract` takes the eid of an earlier adopt event and withdraws it instead.
    `dry_run` lists the files and byte total and writes nothing.
    """
    from bathos.output_tiers.adopt import adopt_outputs, retract_adopt
    from bathos.output_tiers.reference import OutputTierError
    from bathos.runlog.emit import RunNotLaunchedError

    if not run_id:
        return {"error": "run_id parameter is required"}
    try:
        cat_dir = _get_catalog_dir(catalog_dir or None)
        if retract:
            if root:
                raise OutputTierError("invalid_param", "--retract takes no --root")
            return retract_adopt(cat_dir, run_id, retract, reason, dry_run=dry_run)
        return adopt_outputs(
            cat_dir,
            run_id,
            list(root or []),
            reason,
            worktree_root=worktree_root or None,
            dry_run=dry_run,
        )
    except (OutputTierError, RunNotLaunchedError) as e:
        return _output_tier_error_result(e)


@cisternal.tool(registry="bathos", name="cold_outputs")
@traced_tool
async def mcp_cold_outputs_tool(
    run_id: str = "",
    all_uncopied: bool = False,
    profile: str = "",
    mirror: str = "",
    dry_run: bool = False,
    supersede: bool = False,
    reason: str = "",
    break_lock: bool = False,
    clean_partials: bool = False,
    worktree_root: str = "",
    catalog_dir: str | None = None,
) -> dict:
    """Write a run's reference-hashed outputs to the cold profile (and a mirror) through myxcel."""
    return cold_outputs_tool(
        run_id=run_id,
        all_uncopied=all_uncopied,
        profile=profile,
        mirror=mirror,
        dry_run=dry_run,
        supersede=supersede,
        reason=reason,
        break_lock=break_lock,
        clean_partials=clean_partials,
        worktree_root=worktree_root,
        catalog_dir=catalog_dir or "",
    )


@cisternal.tool(registry="bathos", name="warm_outputs")
@traced_tool
async def mcp_warm_outputs_tool(
    run_id: str,
    to: str,
    worktree_root: str = "",
    catalog_dir: str | None = None,
) -> dict:
    """Restore a run's reference-hashed outputs from a verified cold/mirror copy into a new local directory."""
    return warm_outputs_tool(
        run_id=run_id, to=to, worktree_root=worktree_root, catalog_dir=catalog_dir or ""
    )


@cisternal.tool(registry="bathos", name="outputs_where")
@traced_tool
async def mcp_outputs_where_tool(
    run_id: str,
    catalog_dir: str | None = None,
) -> dict:
    """List the recorded copies (warm, cold, mirror) of a run's outputs and the config layer that chose each."""
    return outputs_where_tool(run_id=run_id, catalog_dir=catalog_dir or "")


@cisternal.tool(registry="bathos-cli", name="cold_outputs", cli_name="cold")
def cold_outputs_tool(
    run_id: str = "",
    all_uncopied: bool = False,
    profile: str = "",
    mirror: str = "",
    dry_run: bool = False,
    supersede: bool = False,
    reason: str = "",
    break_lock: bool = False,
    clean_partials: bool = False,
    worktree_root: str = "",
    catalog_dir: str = "",
) -> dict:
    """Write a run's reference-hashed outputs to the cold profile, then the mirror, through myxcel.

    Builds the archive and manifest from the run's reference hashes (a drifted live file refuses with
    outputs_drifted before anything is uploaded), takes the remote lock first, resumes an interrupted write,
    never overwrites a final name. `run_id` may be a unique prefix; `all_uncopied` covers the current project's
    completed runs with no verified copy on the profile and continues past a failing run. `profile` / `mirror`
    name myxcel profiles (see `bth outputs where` for the layer that chose them). `dry_run` lists files, bytes
    and destinations and writes nothing. `break_lock` removes a dead writer's lock and then resumes.
    `supersede` + `reason` renames an existing pair aside and writes a fresh one. `worktree_root` is required
    only for an imported run. Under SLURM each run prints one JSON progress line on stdout.
    """
    from bathos.cluster import MyxcelCapabilityGapError
    from bathos.output_tiers.cold import cold_outputs
    from bathos.output_tiers.config import OutputTiersConfigError
    from bathos.output_tiers.reference import OutputTierError
    from bathos.runlog.emit import RunNotLaunchedError

    if not run_id and not all_uncopied:
        return {"error": "run_id parameter is required (or all_uncopied)"}
    try:
        result = cold_outputs(
            _get_catalog_dir(catalog_dir or None),
            run_id,
            all_uncopied=all_uncopied,
            profile=profile or None,
            mirror=mirror or None,
            dry_run=dry_run,
            supersede=supersede,
            reason=reason,
            break_lock=break_lock,
            clean_partials=clean_partials,
            worktree_root=worktree_root or None,
        )
    except (
        OutputTierError,
        OutputTiersConfigError,
        MyxcelCapabilityGapError,
        RunNotLaunchedError,
    ) as e:
        return _output_tier_error_result(e)
    if result.get("error_code") and "resolution_hint" not in result:
        with contextlib.suppress(ValueError):
            result["resolution_hint"] = RESOLUTION_HINTS[BathosErrorCode(result["error_code"])]
    return result


@cisternal.tool(registry="bathos-cli", name="warm_outputs", cli_name="warm")
def warm_outputs_tool(
    run_id: str = "",
    to: str = "",
    worktree_root: str = "",
    catalog_dir: str = "",
) -> dict:
    """Restore a run's reference-hashed outputs from a verified cold (or mirror) copy into a NEW local directory.

    `to` must not exist. The archive and manifest are fetched through `myxcel cold get`, both sha256s are
    checked before the tar is opened, the member set must equal the manifest exactly (no `..`, absolute, link or
    device members), the files are extracted into a sibling `<to>.bth-warm-<id>.partial/`, every file is hashed
    against the manifest and the run's reference hashes, and the directory is renamed into place with
    `renameat2(RENAME_NOREPLACE)`, so it never replaces anything. Records the copy as tier=warm. `run_id` may be
    a unique prefix. `worktree_root` is required only for an imported run.
    """
    from bathos.cluster import MyxcelCapabilityGapError
    from bathos.output_tiers.config import OutputTiersConfigError
    from bathos.output_tiers.reference import OutputTierError
    from bathos.output_tiers.warm import warm_outputs
    from bathos.runlog.emit import RunNotLaunchedError

    if not run_id:
        return {"error": "run_id parameter is required"}
    if not to:
        return {"error": "to parameter is required"}
    try:
        return warm_outputs(
            _get_catalog_dir(catalog_dir or None),
            run_id,
            to,
            worktree_root=worktree_root or None,
        )
    except (
        OutputTierError,
        OutputTiersConfigError,
        MyxcelCapabilityGapError,
        RunNotLaunchedError,
    ) as e:
        return _output_tier_error_result(e)


@cisternal.tool(registry="bathos-cli", name="outputs_where", cli_group="outputs", cli_name="where")
def outputs_where_tool(
    run_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """List the recorded copies of a run's outputs (warm, cold, mirror) and the config layer that chose each.

    Reads `output_copies` from the folded index; a catalog whose index predates the table shows no copies
    until the next ingest. `run_id` may be a unique prefix.
    """
    from bathos.output_tiers.cold import where_outputs
    from bathos.output_tiers.reference import OutputTierError
    from bathos.runlog.emit import RunNotLaunchedError

    if not run_id:
        return {"error": "run_id parameter is required"}
    try:
        return where_outputs(_get_catalog_dir(catalog_dir or None), run_id)
    except (OutputTierError, RunNotLaunchedError) as e:
        return _output_tier_error_result(e)


@cisternal.tool(registry="bathos-cli", name="campaign_add", cli_group="campaign", cli_name="add")
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
    from bathos.campaigns import connect_catalog_db, prepare_catalog_for_conclude
    from bathos.runlog.emit import unit_of_work

    # One unit of work, so the mode is read once for the whole add (#2843).
    with unit_of_work(cat_dir) as log_mode:
        # After cut-over add_run_to_campaign folds pending events, reads the
        # index and writes events only, so the frozen `bathos.db` is never
        # opened or locked (debts #2841, #2843).
        db = None
        if not log_mode:
            prepare_catalog_for_conclude(cat_dir)
            db = connect_catalog_db(cat_dir, read_only=False)
            if db is None:
                return {
                    "ok": False,
                    "error": f"Catalog database not found at {cat_dir / 'bathos.db'}; "
                    "run `bth compact` first",
                }
        try:
            add_run_to_campaign(db, campaign_id, run_id, catalog_dir=cat_dir)
            return {
                "ok": True,
                "run_id": run_id,
                "campaign_id": campaign_id,
                "message": f"Added run {run_id} to campaign {campaign_id}",
            }
        except BreakdownRefused as e:
            return _breakdown_refusal_result(e)
        except CampaignError as e:
            return {"ok": False, "error": str(e)}
        finally:
            if db is not None:
                db.close()


@cisternal.tool(registry="bathos-cli", name="campaign_show", cli_group="campaign", cli_name="show")
def campaign_show_tool(
    campaign_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """Show campaign details."""
    if not campaign_id:
        return {"error": "campaign_id parameter is required"}

    cat_dir = _get_catalog_dir(catalog_dir or None)
    from bathos.campaigns import connect_catalog_db

    db = connect_catalog_db(cat_dir, read_only=True)
    try:
        campaign = get_campaign(db, campaign_id, catalog_dir=cat_dir)
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
        if db is not None:
            db.close()


@dataclasses.dataclass
class _AuthoringVerifyResult:
    """Adapts an authoring-ledger VerifyResult to the tier-result shape `bth verify` returns."""

    ok: bool
    warnings: list[str]
    errors: list[str]
    tier: str = "authoring"


@cisternal.tool(registry="bathos-cli", name="verify", cli_name="verify")
def verify_tool(
    tier: str = "all",
    catalog_dir: str = "",
    archive_dir: str = "",
    authoring: bool = False,
    run_id: str = "",
    profile: str = "",
    strict: bool = False,
) -> dict:
    """Verify catalog integrity across cool, warm, archive, and runlog tiers.

    `tier="outputs"` is opt-in and is never part of `all`, because it needs the network: it re-reads each live
    cold/mirror copy of a run's outputs on its host through `myxcel cold verify-file` and records the measured
    state (`verified` / `failed` / `unverifiable`) so `bth outputs where` shows it. `run_id` (a unique prefix)
    limits it to one run, `profile` to one myxcel profile; `strict` makes an `unverifiable` copy a failure too.
    """
    from bathos.verify import (
        VerifyResult,
        verify_all,
        verify_archive,
        verify_cool,
        verify_runlog,
        verify_warm,
    )

    cat_dir = _get_catalog_dir(catalog_dir or None)
    archive_root = (
        Path(archive_dir).expanduser() if archive_dir else Path.home() / ".bth" / "archive"
    )

    outputs_report: dict | None = None
    if tier == "outputs":
        from bathos.cluster import MyxcelCapabilityGapError
        from bathos.output_tiers.config import OutputTiersConfigError
        from bathos.output_tiers.reference import OutputTierError
        from bathos.output_tiers.verify import verify_outputs
        from bathos.runlog.emit import RunNotLaunchedError

        try:
            outputs_report = verify_outputs(cat_dir, run_id, profile=profile or None, strict=strict)
        except (
            OutputTierError,
            OutputTiersConfigError,
            MyxcelCapabilityGapError,
            RunNotLaunchedError,
        ) as e:
            return _output_tier_error_result(e)
        failing = [
            f"{c['tier']}:{c['profile']} {c['path']} {c['state']} ({c['detail']})"
            for run in outputs_report["runs"]
            for c in run["copies"]
            if c["state"] == "failed" or (strict and c["state"] == "unverifiable")
        ]
        disagreeing = [
            f"{run['run_id']}: {run['agree_detail']}"
            for run in outputs_report["runs"]
            if not run["agree"]
        ]
        warnings = [
            f"{c['tier']}:{c['profile']} {c['path']} unverifiable ({c['detail']})"
            for run in outputs_report["runs"]
            for c in run["copies"]
            if c["state"] == "unverifiable" and not strict
        ]
        results: list = [
            VerifyResult(
                tier="outputs",
                ok=outputs_report["ok"],
                warnings=warnings,
                errors=[*failing, *disagreeing],
            )
        ]
    elif tier == "cool":
        results = [verify_cool(cat_dir)]
    elif tier == "warm":
        results = [verify_warm(cat_dir)]
    elif tier == "archive":
        results = [verify_archive(archive_root)]
    elif tier == "runlog":
        results = [verify_runlog(cat_dir)]
    elif tier == "all":
        results = verify_all(cat_dir, archive_root)
    else:
        return {"ok": False, "error": f"Unknown tier: {tier!r}"}

    if authoring:
        from bathos.authoring.ledger import verify_authoring_ledger
        from bathos.workspace import resolve_workspace

        ledger = verify_authoring_ledger(resolve_workspace().fs_root)
        ledger_warnings = list(ledger.warnings)
        if ledger.entries_checked:
            ledger_warnings.insert(0, f"{ledger.entries_checked} ledger entries checked")
        results = [
            *results,
            _AuthoringVerifyResult(ok=ledger.ok, warnings=ledger_warnings, errors=list(ledger.errors)),
        ]

    payload = {
        "ok": all(r.ok for r in results),
        "results": [
            {"tier": r.tier, "ok": r.ok, "warnings": r.warnings, "errors": r.errors}
            for r in results
        ],
    }
    all_errors = [e for r in results for e in r.errors]
    if all_errors:
        # Singular "error" key -- see cli_render.render_or_exit -- matches the shipped
        # Typer `verify` command's `if any_errors: raise typer.Exit(1)`.
        payload["error"] = "; ".join(all_errors)
    if outputs_report is not None:
        # The per-run, per-copy JSON: the CLI prints it on stdout even on failure (cli_render), MCP callers get it
        # as a field, and `error_code` is what a caller branches on (not the exit status).
        payload["outputs_report"] = outputs_report
        code = outputs_report.get("error_code")
        if code:
            payload["error_code"] = code
            payload["resolution_hint"] = RESOLUTION_HINTS[BathosErrorCode(code)]
            payload["error"] = f"{outputs_report['error']}: {payload.get('error', '')}"
    return payload


@cisternal.tool(registry="bathos-cli", name="lint", cli_name="lint")
def lint_tool(project_root: str = "", concentration_threshold: int = 20) -> dict:
    """Lint scripts/ and claim files for naming and structural issues."""
    from bathos.cli_common import soft_project_slug
    from bathos.linter import (
        IssueSeverity,
        check_adversarial_checks,
        check_archival_candidates,
        check_baseline_ref_exists,
        check_bypass_trend,
        check_canonical_stage_names,
        check_claim_opaque_labels,
        check_ephemeral_output_paths,
        check_popper_adversarial,
        check_residual_rates,
        check_run_concentration,
        check_stale_scripts_without_reason,
        check_threshold_basis,
        check_todo_strings_in_scaffold,
        check_undeclared_resume_dirs,
        check_unfired_branches,
        lint_project,
    )

    root = Path(project_root) if project_root else Path.cwd()
    issues = lint_project(root.resolve())
    issues.extend(check_claim_opaque_labels(root.resolve()))
    issues.extend(check_adversarial_checks(root.resolve()))
    issues.extend(check_popper_adversarial(root.resolve()))
    issues.extend(check_threshold_basis(root.resolve()))
    issues.extend(check_todo_strings_in_scaffold(root.resolve()))
    issues.extend(check_stale_scripts_without_reason(root.resolve()))
    # check_archival_candidates works with no warm catalog at all (every stale sidecar is
    # a candidate) as well as with one present -- unlike the checks below, it must not be
    # gated behind db_path.exists(). project_slug scopes the archived_items lookup so a
    # same-relative-path archived record in a DIFFERENT project (catalog_dir is commonly
    # shared) doesn't silently suppress this warning here.
    issues.extend(
        check_archival_candidates(
            root.resolve(), _get_catalog_dir(None), project_slug=soft_project_slug()
        )
    )

    catalog_dir = _get_catalog_dir(None)
    # Reads run.started events, which only a log-mode index has: gated by catalog_readable inside, not by bathos.db.
    issues.extend(check_undeclared_resume_dirs(catalog_dir, project_slug=soft_project_slug() or ""))
    db_path = catalog_dir / "bathos.db"
    if db_path.exists():
        issues.extend(check_residual_rates(catalog_dir))
        issues.extend(check_bypass_trend(catalog_dir))
        issues.extend(check_unfired_branches(catalog_dir))
        issues.extend(check_run_concentration(catalog_dir, threshold=concentration_threshold))
        issues.extend(check_ephemeral_output_paths(catalog_dir))
        issues.extend(check_canonical_stage_names(catalog_dir))
        issues.extend(check_baseline_ref_exists(root.resolve(), db_path))

    errors = [i for i in issues if i.severity == IssueSeverity.ERROR]
    warnings = [i for i in issues if i.severity == IssueSeverity.WARNING]
    result = {
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
    if errors:
        # Singular "error" key (not the plural "errors" some other tools use) is what
        # cli_render.render_or_exit checks to decide CLI exit(1) -- matches the shipped
        # Typer `lint` command's `if errors: raise typer.Exit(1)` behavior.
        result["error"] = f"{len(errors)} error(s), {len(warnings)} warning(s)."
    elif not issues:
        result["message"] = "No issues found."
    return result


@cisternal.tool(registry="bathos", name="campaign_add")
@traced_tool
@require_write_token
async def mcp_campaign_add_tool(
    run_id: str = "",
    campaign_id: str = "",
    catalog_dir: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Add a run to a campaign.

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841)."""
    return campaign_add_tool(run_id=run_id, campaign_id=campaign_id, catalog_dir=catalog_dir)


@cisternal.tool(registry="bathos", name="campaign_show")
@traced_tool
async def mcp_campaign_show_tool(
    campaign_id: str = "",
    catalog_dir: str = "",
) -> dict:
    """Show campaign details."""
    return campaign_show_tool(campaign_id=campaign_id, catalog_dir=catalog_dir)


@cisternal.tool(registry="bathos", name="verify")
@traced_tool
async def mcp_verify_tool(
    tier: str = "all",
    catalog_dir: str = "",
    archive_dir: str = "",
    run_id: str = "",
    profile: str = "",
    strict: bool = False,
) -> dict:
    """Verify catalog integrity. `tier="outputs"` re-reads cold/mirror output copies on their hosts (opt-in)."""
    result = verify_tool(
        tier=tier,
        catalog_dir=catalog_dir,
        archive_dir=archive_dir,
        run_id=run_id,
        profile=profile,
        strict=strict,
    )
    # verify_tool injects an "error" key for CLI rendering purposes
    # (render_or_exit's uniform contract) -- strip it here like
    # validate_sidecar's wrapper does, since "ok"/"results" already carry
    # this tool's real failure detail.
    result.pop("error", None)
    return result


@cisternal.tool(registry="bathos", name="lint")
@traced_tool
async def mcp_lint_tool(
    project_root: str = "",
) -> dict:
    """Lint project scripts and claim files."""
    result = lint_tool(project_root=project_root)
    # lint_tool injects an "error" key for CLI rendering purposes
    # (render_or_exit's uniform contract) -- strip it here like
    # validate_sidecar's wrapper does, since "ok"/"issues" already carry
    # this tool's real failure detail.
    result.pop("error", None)
    return result


@cisternal.tool(registry="bathos", name="repair_scan")
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


@cisternal.tool(registry="bathos-cli", name="repair", cli_name="repair")
def repair_tool(
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


@cisternal.tool(registry="bathos", name="repair")
@traced_tool
@require_write_token
async def mcp_repair_tool(
    catalog_dir: str | None = None,
    tier: str = "all",
    dry_run: bool = True,
    acknowledge_warm_loss: bool = False,
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
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

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841) — including
    for dry_run=True scans, since the same tool can also apply real changes.
    """
    return repair_tool(
        catalog_dir=catalog_dir,
        tier=tier,
        dry_run=dry_run,
        acknowledge_warm_loss=acknowledge_warm_loss,
    )


@cisternal.tool(registry="bathos", name="migrate_to_log")
@traced_tool
@require_write_token
async def mcp_migrate_to_log_tool(
    catalog_dir: str = "",
    force: bool = False,
    accept_residual: str = "",
    import_legacy: bool = False,
    dry_run: bool = False,
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """`bth migrate --to-log` (Migration steps 0-4): the project-local run
    log cut-over. `import_legacy=True` instead runs the post-cut-over
    `--import-legacy` re-import (AC-23; refused before cut-over). `dry_run=
    True` previews steps 1-3's residual report with no data FILES written
    anywhere (status `dry_run`; see `MigrateToLogResult.dry_run`'s
    docstring — the empty `writers.lock` mutex file may still be created) —
    rejected together with `accept_residual`.

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841) — this
    can mutate the real catalog (pulls remotes, reaps, and can rename
    bathos.db to bathos.db.frozen) unless dry_run=True, in which case it
    writes no data files (only the empty writers.lock mutex, if absent) but
    still requires the token, like every other write-verb tool that also has
    a dry-run mode (e.g. repair_tool).

    Args:
        catalog_dir: Catalog directory (empty = use default)
        force: Proceed past a squeue/submit-record conflict instead of refusing
        accept_residual: sha256 of a previously-reviewed residual report; proceeds to
            the switch only if a fresh run reproduces that exact hash
        import_legacy: Run `--import-legacy` instead of `--to-log`
        dry_run: Preview the residual report with no data files written anywhere
            (the empty writers.lock mutex may still be created); incompatible
            with accept_residual (there is nothing to accept a residual report for)

    Returns:
        Dict form of `MigrateToLogResult` (status, attempt, report_path,
        report_sha256, residual_lines, unclassified, missing_project_ids,
        foreign_catalogs, conflicting_jobs, locked_source, would_pull,
        detail). `status="foreign_catalogs"` (checked before
        `missing_project_ids`'s dry-run branch, so a dry run reports it
        too) means at least one registered root still sets `[project]
        catalog_dir` to somewhere other than `catalog_dir` -- consolidate
        each with `consolidate_catalog` (below) before retrying.
    """
    import dataclasses

    from bathos.runlog.migrate import import_legacy_post_cutover, migrate_to_log

    cat_dir = _get_catalog_dir(catalog_dir or None)
    if import_legacy:
        result = import_legacy_post_cutover(cat_dir)
    else:
        result = migrate_to_log(
            cat_dir,
            force=force,
            accept_residual=accept_residual or None,
            dry_run=dry_run,
        )
    return dataclasses.asdict(result)


def consolidate_catalog_tool(
    root: str,
    catalog_dir: str = "",
    dry_run: bool = False,
    source_catalog: str = "",
) -> dict:
    """`bth migrate --consolidate-catalog <root>`: additively copies a
    registered root's foreign `[project] catalog_dir` (`runs/`,
    `campaigns/`, `submits/`, `reaped/`, `sidecars/`, `anchors/`, `ledger/`,
    `blast_radius/`, `archived_items/` only) into the catalog being
    migrated. Never modifies `root`'s catalog. `dry_run=True` computes the
    identical plan and counts without writing anything.

    The result's `status` is `"ok"` only when the run is fully clean;
    `"needs_review"` means the additive copy still ran (when `dry_run` is
    False) but `conflicts` or `unplaceable` is non-empty and needs a human
    look before `bth migrate --to-log` is retried. Procedure: consolidate ->
    remove `catalog_dir` from that root's `.bth.toml` and commit -> call this
    tool again with `source_catalog` set (the key is now gone, so there is
    nothing left in `.bth.toml` to resolve) and confirm it copies 0 files
    with `status == "ok"` -> only then retry `--to-log`.

    Args:
        root: Path to the registered root whose `.bth.toml` sets an explicit
            `[project] catalog_dir`.
        catalog_dir: The destination catalog directory (empty = use default)
        dry_run: If True, compute the plan without writing
        source_catalog: Explicit source catalog path to use when `root`'s
            `.bth.toml` no longer has an explicit `[project] catalog_dir`
            (the second pass of the procedure above).

    Returns:
        Dict form of `ConsolidateResult` (status, applied, copied,
        already_present, already_present_differs, already_present_differs_count,
        conflicts, conflict_paths, unplaceable, skipped_unknown, per_subtree,
        detail), or `{"error": ...}` if `root` has no explicit `catalog_dir`
        to consolidate and no `source_catalog` override was given.
    """
    import dataclasses

    from bathos.runlog.migrate import consolidate_project_catalog, explicit_catalog_dir

    root_path = Path(root).expanduser()
    source = explicit_catalog_dir(root_path)
    if source is None:
        if source_catalog:
            source = Path(source_catalog).expanduser()
        else:
            return {
                "error": (
                    f"{root_path}'s .bth.toml has no explicit [project] catalog_dir; "
                    "nothing to consolidate (pass source_catalog to re-run consolidation "
                    "a second time after the key has already been removed)"
                )
            }
    cat_dir = _get_catalog_dir(catalog_dir or None)
    result = consolidate_project_catalog(source, cat_dir, apply=not dry_run)
    return dataclasses.asdict(result)


@cisternal.tool(registry="bathos", name="consolidate_catalog")
@traced_tool
@require_write_token
async def mcp_consolidate_catalog_tool(
    root: str,
    catalog_dir: str = "",
    dry_run: bool = False,
    source_catalog: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """MCP wrapper for `consolidate_catalog_tool` — see its docstring.

    Requires token= (~/.bth/mcp_token) except over stdio (debt #619, #2841) — this
    can write into the real catalog (the destination) unless dry_run=True,
    like every other write-verb tool that also has a dry-run mode.

    Args:
        root: Path to the registered root whose `.bth.toml` sets an explicit
            `[project] catalog_dir`.
        catalog_dir: The destination catalog directory (empty = use default)
        dry_run: If True, compute the plan without writing
        source_catalog: Explicit source catalog path to use when `root`'s
            `.bth.toml` no longer has an explicit `[project] catalog_dir`
            (the second pass of the consolidate-then-remove-key procedure).

    Returns:
        Dict form of `ConsolidateResult`, or `{"error": ...}`.
    """
    return consolidate_catalog_tool(
        root=root, catalog_dir=catalog_dir, dry_run=dry_run, source_catalog=source_catalog
    )


# ============================================================================
# Wire the "bathos" registry snapshot onto the FastMCP server.
#
# Must run after every @cisternal.tool registration above (C6 snapshot
# semantics: cisternal.wire() only sees tools registered before this call).
# `expected=` lists every tool name so a missing/misspelled registration
# raises CisternalWireError at import time instead of silently dropping a
# tool. Telemetry and error-shaping remain owned by @traced_tool — no
# CisternalMiddleware is installed on `app`, so wire()'s own zero-telemetry
# passthrough invariant is a no-op here, not a behavior change from the
# previous @app.tool(...) registrations.
# ============================================================================


# ── rule-card corpus (mirrors `bth ref`) ─────────────────────────────────────


@cisternal.tool(registry="bathos-cli", name="reference_list", cli_group="ref", cli_name="list")
def reference_list_tool() -> dict:
    """List every rule card in the shipped corpus.

    Returns {'ok': True, 'cards': [{id, title, severity, domain, source_check,
    has_applies_when}], 'errors': [...]}.
    """
    from bathos.corpus import load_corpus

    load = load_corpus()
    return {
        "ok": True,
        "cards": [
            {
                "id": c.id,
                "title": c.title,
                "severity": c.severity,
                "domain": c.domain,
                "source_check": c.source_check,
                "has_applies_when": bool(c.applies_when),
            }
            for c in load.cards
        ],
        "errors": [{"path": str(p), "error": e} for p, e in load.errors],
    }


@cisternal.tool(registry="bathos-cli", name="reference_get", cli_group="ref", cli_name="show")
def reference_get_tool(card_id: str) -> dict:
    """Fetch one rule card by id, including its markdown body."""
    from bathos.corpus import CorpusError, get_card

    try:
        card = get_card(card_id)
    except CorpusError as e:
        return {"ok": False, "error": str(e), "error_code": "not_found"}

    return {
        "ok": True,
        "card": {
            "id": card.id,
            "title": card.title,
            "severity": card.severity,
            "domain": card.domain,
            "source_check": card.source_check,
            "applies_when": card.applies_when,
            "tags": list(card.tags),
            "see_also": list(card.see_also),
            "body": card.body,
        },
    }


@cisternal.tool(registry="bathos-cli", name="reference_search", cli_group="ref", cli_name="search")
def reference_search_tool(query: str) -> dict:
    """Substring search over card id, title, tags and body."""
    from bathos.corpus import search_cards

    hits = search_cards(query)
    return {
        "ok": True,
        "query": query,
        "count": len(hits),
        "cards": [
            {"id": c.id, "title": c.title, "severity": c.severity, "domain": c.domain} for c in hits
        ],
    }


@cisternal.tool(
    registry="bathos-cli", name="reference_applicable", cli_group="ref", cli_name="applicable"
)
def reference_applicable_tool(script: str, catalog_dir: str | None = None) -> dict:
    """Evaluate every card's applies_when against a script's sidecar.

    `unevaluable` is always returned alongside `fired`: a card that could NOT be checked must
    never be indistinguishable from one that was checked and did not match.
    """
    from bathos.corpus import applicable_cards, build_context

    ctx = build_context(Path(script), Path(catalog_dir) if catalog_dir else _get_catalog_dir())
    fired, unevaluable = applicable_cards(ctx)
    return {
        "ok": True,
        "script": script,
        "context": ctx,
        "fired": [
            {"id": c.id, "title": c.title, "severity": c.severity, "source_check": c.source_check}
            for c in fired
        ],
        "unevaluable": [{"id": c.id, "error": e} for c, e in unevaluable],
    }


@cisternal.tool(registry="bathos")
@traced_tool
async def reference_list() -> dict:
    """List every rule card in the shipped corpus.

    Returns {'ok': True, 'cards': [{id, title, severity, domain, source_check,
    has_applies_when}], 'errors': [...]}.
    """
    return reference_list_tool()


@cisternal.tool(registry="bathos")
@traced_tool
async def reference_get(card_id: str) -> dict:
    """Fetch one rule card by id, including its markdown body."""
    return reference_get_tool(card_id)


@cisternal.tool(registry="bathos")
@traced_tool
async def reference_search(query: str) -> dict:
    """Substring search over card id, title, tags and body."""
    return reference_search_tool(query)


@cisternal.tool(registry="bathos")
@traced_tool
async def reference_applicable(script: str, catalog_dir: str | None = None) -> dict:
    """Evaluate every card's applies_when against a script's sidecar.

    `unevaluable` is always returned alongside `fired`: a card that could NOT be checked must
    never be indistinguishable from one that was checked and did not match.
    """
    return reference_applicable_tool(script, catalog_dir=catalog_dir)


if __name__ == "__main__":
    init_server_telemetry()
    app.run()


_WIRED = cisternal.wire(
    app,
    registry="bathos",
    expected=[
        "list_runs",
        "find_runs",
        "get_run",
        "cite_run",
        "lineage_prov",
        "run_sql",
        "resolve_pin",
        "get_trust_state",
        "query_attestation",
        "read_campaign_report",
        "read_figure_manifest",
        "figure_lookup",
        "list_candidates",
        "anchor_insert",
        "anchor_get",
        "anchor_find",
        "figure_entry_register",
        "attestation_scaffold",
        "attestation_validate",
        "attestation_register",
        "graduate_product",
        "compact",
        "archive",
        "check",
        "capability_probe",
        "sync",
        "init",
        "log_restore",
        "run",
        "campaign_create",
        "campaign_list",
        "campaign_review",
        "campaign_conclude",
        "postmortem_scaffold",
        "postmortem_validate",
        "postmortem_get",
        "claim_scaffold",
        "claim_validate",
        "claim_register",
        "gate_stamp",
        "gate_status",
        "claim_attest_parity",
        "validate_sidecar",
        "list_outputs",
        "outputs_summary",
        "outputs_hash",
        "outputs_adopt",
        "outputs_where",
        "cold_outputs",
        "warm_outputs",
        "campaign_add",
        "campaign_show",
        "verify",
        "lint",
        "repair_scan",
        "repair",
        "reference_list",
        "reference_get",
        "reference_search",
        "reference_applicable",
        "archive_artifact",
        "restore",
        "reap",
        "blast_radius_assess",
        "blast_radius_clear",
        "get_blast_radius_status",
        "doc_schema",
        "claim_author",
        "new_experiment",
        "migrate_to_log",
        "consolidate_catalog",
    ],
)


def mcp_server():
    """Entry point for MCP server (stdio transport).

    Called by pyproject.toml entry point: bth-mcp
    """
    init_server_telemetry()
    # The caller owns this process, so write tools need no token (debt #2841).
    # Pin the transport: a bare run() follows FASTMCP_TRANSPORT, which could
    # serve HTTP while the process is marked as stdio.
    mark_stdio_transport()
    app.run(transport="stdio")
