from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

from bathos.archive import archive
from bathos.config import find_project_config, load_project_config
from bathos.sync import sync_catalog
from bathos.telemetry import init_telemetry

app = typer.Typer(help="bathos — local-first experiment tracking")

remote_app = typer.Typer(help="Manage remote hosts for sync.")
app.add_typer(remote_app, name="remote")

campaign_app = typer.Typer(help="Manage experiment campaigns")
app.add_typer(campaign_app, name="campaign")

postmortem_app = typer.Typer(help="Manage postmortem reviews")
app.add_typer(postmortem_app, name="postmortem")

outputs_app = typer.Typer(help="Inspect and summarise run output files.")
app.add_typer(outputs_app, name="outputs")

report_app = typer.Typer(help="Generate and emit campaign reports and manifests")
app.add_typer(report_app, name="report")

claim_app = typer.Typer(help="Manage claim-tier pre-registration files")
app.add_typer(claim_app, name="claim")

query_app = typer.Typer(help="Read-back/query API (S1): pins, trust state, attestations, figures")
app.add_typer(query_app, name="query")

anchor_app = typer.Typer(
    help="Anchor-insert WRITE seam (S2): anchor arbitrary sidecars by path+sha256"
)
app.add_typer(anchor_app, name="anchor")

attestation_app = typer.Typer(
    help="Attestation sidecar (S4): oracle_match/repro_floor, anchored by sha256 "
    "(a separate kind from `bth claim` — see gate #3488)"
)
app.add_typer(attestation_app, name="attestation")


def _catalog_dir() -> Path:
    override = os.environ.get("BTH_CATALOG_DIR")
    if override:
        return Path(override)
    from bathos.config import default_catalog_dir, find_project_config, load_project_config

    cfg_path = find_project_config()
    if cfg_path is not None:
        return load_project_config(cfg_path).catalog_dir
    return default_catalog_dir()


def _require_project_slug() -> str:
    slug_env = os.environ.get("BTH_PROJECT_SLUG")
    if slug_env:
        return slug_env
    from bathos.config import find_project_config, load_project_config

    cfg_path = find_project_config()
    if cfg_path is None:
        typer.echo("No .bth.toml found. Run `bth init` first.", err=True)
        raise typer.Exit(1)
    return load_project_config(cfg_path).slug


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(False, "--version", "-V", is_eager=True),
):
    init_telemetry()
    if version:
        from bathos import __version__

        typer.echo(f"bathos {__version__}")
        raise typer.Exit()


@app.command()
def init(
    slug: str = typer.Option(..., "--slug", "-s", help="Project slug"),
    remote: str | None = typer.Option(None, "--remote", help="host:remote_path"),
    slurm_partition: str | None = typer.Option(None, "--slurm-partition"),
):
    """Register project, scaffold scripts/ dirs, write .bth.toml."""
    from bathos.init import init_project

    init_project(
        Path.cwd(),
        slug=slug,
        catalog_dir=_catalog_dir(),
        remote=remote,
        slurm_partition=slurm_partition,
    )
    typer.echo(f"Initialized bathos project '{slug}'")


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(
    argv: list[str] = typer.Argument(...),
    out: list[str] = typer.Option([], "--out", help="Output path to register"),
    tag: list[str] = typer.Option([], "--tag", "-t"),
    agent_mode: str | None = typer.Option(None, "--agent-mode", help="collaborative|autonomous"),
    no_sidecar: bool = typer.Option(
        False, "--no-sidecar", help="Bypass sidecar enforcement (logs BYPASSED)"
    ),
    derived_from: str | None = typer.Option(
        None, "--derived-from", help="Parent run ID for lineage"
    ),
    campaign: str | None = typer.Option(
        None, "--campaign", help="Campaign ID to associate this run with"
    ),
):
    """Run a script and record provenance."""
    from bathos.runner import run_script

    slug = _require_project_slug()
    exit_code = run_script(
        argv=argv,
        project_slug=slug,
        catalog_dir=_catalog_dir(),
        output_paths=out,
        tags=tag,
        agent_mode=agent_mode,
        no_sidecar=no_sidecar,
        derived_from=derived_from,
        campaign_id=campaign,
    )
    raise typer.Exit(exit_code)


@app.command("ls")
def ls_cmd(
    project: str | None = typer.Option(None, "--project", "-p"),
    since: str | None = typer.Option(None, "--since", help="e.g. 7d, 24h"),
    status: str | None = typer.Option(None, "--status"),
    limit: int = typer.Option(20, "--limit", "-n"),
):
    """List recent runs."""
    from bathos.compact import _fragment_count, should_compact
    from bathos.query import find_runs

    since_dt = _parse_since(since)
    catalog_dir = _catalog_dir()
    runs = find_runs(catalog_dir, since=since_dt, project=project, status=status)
    runs = runs[:limit]
    from bathos.rich_fmt import render_runs_table

    if not runs:
        typer.echo("No runs found.")
        return
    render_runs_table(runs)

    # Check if compaction is recommended and show banner if needed
    if should_compact(catalog_dir):
        frag_count = _fragment_count(catalog_dir)
        typer.echo()
        typer.echo(f"⚠  {frag_count} uncompacted runs — run 'bth compact' to speed up queries")


@app.command()
def show(run_id: str = typer.Argument(...)):
    """Show full details of a run."""
    from bathos.query import get_run
    from bathos.rich_fmt import render_run_detail

    r = get_run(run_id, _catalog_dir())
    if r is None:
        typer.echo(f"Run not found: {run_id}", err=True)
        raise typer.Exit(1)
    render_run_detail(r)


@app.command()
def lineage(
    run_id: str = typer.Argument(...),
    format: str = typer.Option(
        "text",
        "--format",
        "-f",
        help="Output format: text, prov, or dot",
    ),
    depth: int = typer.Option(
        10,
        "--depth",
        help="Maximum lineage depth to traverse",
    ),
):
    """Show ancestor chain of a run following parent_run_id links."""
    import json

    from bathos.provenance import format_prov_json
    from bathos.query import CatalogError
    from bathos.query import lineage as get_lineage

    try:
        ancestors = get_lineage(run_id, _catalog_dir())
    except CatalogError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    if not ancestors:
        typer.echo(f"Run not found or no lineage: {run_id}", err=True)
        raise typer.Exit(1)

    if format == "prov":
        # W3C PROV-JSON output
        prov_output = format_prov_json(ancestors)
        typer.echo(json.dumps(prov_output, indent=2))
    elif format == "dot":
        # TODO: Graphviz DOT format (future)
        typer.echo("dot format not yet implemented", err=True)
        raise typer.Exit(1)
    else:
        # text (default)
        typer.echo(f"Lineage for {run_id[:8]}:")
        for r in ancestors:
            outcome_str = r.outcome if r.outcome else "-"
            typer.echo(
                f"  {r.id[:8]} {r.timestamp.isoformat()[:19]} "
                f"outcome={outcome_str} {r.command[:40]}"
            )


@app.command()
def cite(
    run_id: str = typer.Argument(..., help="Run ID to cite (full or prefix)"),
    format: str = typer.Option(
        "markdown",
        "--format",
        "-f",
        help="Output format: markdown or json",
    ),
):
    """Emit a structured citation for a run linking output to hypothesis and manifest."""
    from bathos.cite import format_citation
    from bathos.query import get_run

    run = get_run(run_id, _catalog_dir())
    if run is None:
        typer.echo(f"Run not found: {run_id}", err=True)
        raise typer.Exit(1)
    typer.echo(format_citation(run, fmt=format))


@app.command()
def find(
    project: str | None = typer.Option(None, "--project", "-p"),
    since: str | None = typer.Option(None, "--since"),
    status: str | None = typer.Option(None, "--status"),
    tag: list[str] = typer.Option([], "--tag"),
    slurm_job: str | None = typer.Option(None, "--slurm-job", help="SLURM job ID"),
    output_file: str | None = typer.Option(
        None, "--output-file", help="Filter to runs with matching output file path (glob pattern)"
    ),
):
    """Find runs matching filters."""
    from bathos.query import _filter_runs_by_output_file, find_runs

    runs = find_runs(
        _catalog_dir(),
        since=_parse_since(since),
        project=project,
        status=status,
        tags=tag or None,
        slurm_job_id=slurm_job,
    )
    runs = _filter_runs_by_output_file(runs, pattern=output_file)
    for r in runs:
        typer.echo(f"{r.id}  {r.project_slug}  {r.status}  {r.command[:60]}")


@app.command()
def sql(query: str = typer.Argument(...)):
    """Run raw DuckDB SQL against the catalog."""
    from bathos.query import run_sql

    try:
        rows = run_sql(query, catalog_dir=_catalog_dir())
        for row in rows:
            typer.echo("\t".join(str(v) for v in row))
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)


@app.command()
def compact(
    force_rebuild: bool = typer.Option(
        False, "--force-rebuild", help="Rebuild bathos.db from cool fragments if corrupt"
    ),
):
    """Compact cool fragments into warm DuckDB catalog."""
    from bathos.compact import compact as compact_catalog

    catalog_dir = _catalog_dir()
    result = compact_catalog(catalog_dir, force_rebuild=force_rebuild)
    typer.echo(f"Compacted {result.ingested} runs into bathos.db in {result.duration_s:.1f}s")


@app.command()
def verify(
    tier: str = typer.Option(
        "all",
        "--tier",
        "-t",
        help="Tier to verify: cool, warm, archive, or all",
    ),
    archive_dir: Path | None = typer.Option(
        None, "--archive-dir", "-d", help="Archive root (default: ~/.bth/archive)"
    ),
):
    """Verify catalog integrity across cool, warm, and archive tiers."""
    from bathos.verify import verify_all, verify_archive, verify_cool, verify_warm

    catalog_dir = _catalog_dir()
    archive_root = archive_dir or (Path.home() / ".bth" / "archive")

    if tier == "cool":
        results = [verify_cool(catalog_dir)]
    elif tier == "warm":
        results = [verify_warm(catalog_dir)]
    elif tier == "archive":
        results = [verify_archive(archive_root)]
    elif tier == "all":
        results = verify_all(catalog_dir, archive_root)
    else:
        typer.echo(f"Unknown tier: {tier!r}. Choose cool, warm, archive, or all.", err=True)
        raise typer.Exit(1)

    any_errors = False
    for result in results:
        status = "OK" if result.ok else "FAIL"
        color = "green" if result.ok else "red"
        typer.secho(f"[{result.tier}] {status}", fg=color)
        for w in result.warnings:
            typer.secho(f"  WARN  {w}", fg="yellow")
        for e in result.errors:
            typer.secho(f"  ERROR {e}", fg="red")
            any_errors = True

    if any_errors:
        raise typer.Exit(1)


@app.command()
def repair(
    dry_run: bool = typer.Option(
        True, "--dry-run/--apply", help="Show plan without executing (default: dry-run)"
    ),
    tier: str = typer.Option(
        "all",
        "--tier",
        "-t",
        help="Tier to repair: cool, warm, archive, or all",
    ),
    acknowledge_warm_loss: bool = typer.Option(
        False,
        "--acknowledge-warm-loss",
        help="Acknowledge that warm DB rebuild will destroy postmortem annotations and output_metadata",
    ),
    from_warm: bool = typer.Option(
        False, "--from-warm", help="Detect runs in warm DB missing from cool fragments"
    ),
):
    """Scan for catalog corruption and repair it.

    By default, runs in dry-run mode (--dry-run) and shows what would be repaired.
    Pass --apply to execute the repairs.
    """
    from bathos.repair import repair as repair_catalog
    from bathos.repair import scan

    catalog_dir = _catalog_dir()

    # First, scan to get the plan
    actions, warnings = scan(catalog_dir, tier, from_warm=from_warm)

    if not actions:
        typer.echo("No repair actions needed.")
        return

    # Display the plan
    typer.echo(f"\nScan Results ({len(actions)} action(s)):\n")
    for action in actions:
        status_symbol = "→" if not action.detail.startswith("Skip") else "⊘"
        typer.echo(f"  {status_symbol} {action.action}: {action.detail}")

    if warnings:
        typer.echo("\nWarnings:")
        for warn in warnings:
            typer.secho(f"  ⚠  {warn}", fg="yellow")

    if dry_run:
        typer.echo("\n(dry-run mode — no changes made)")
        typer.echo("To execute repairs, run: bth repair --apply")
        return

    # Execute repairs
    typer.echo("\nExecuting repairs...")
    try:
        manifest = repair_catalog(
            catalog_dir,
            tier,
            dry_run=False,
            acknowledge_warm_loss=acknowledge_warm_loss,
            from_warm=from_warm,
        )
        typer.echo(f"\n✓ Repair completed at {manifest.run_ts}")
        typer.echo(
            f"  Actions taken: {len([a for a in manifest.actions if not a.detail.startswith('Skip')])}"
        )
        if manifest.warnings:
            for warn in manifest.warnings:
                typer.secho(f"  ⚠  {warn}", fg="yellow")
    except SystemExit as e:
        if e.code == 1:
            typer.echo(
                "Warm database rebuild would destroy warm-only data.\n"
                "Review the loss, then pass --acknowledge-warm-loss to proceed.",
                err=True,
            )
        raise


@app.command("archive")
def archive_cmd(
    project: str | None = typer.Option(
        None, "--project", "-p", help="Filter to specific project (default: all)"
    ),
    archive_dir: Path | None = typer.Option(
        None, "--archive-dir", "-d", help="Archive root directory (default: ~/.bth/archive)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be archived without writing"
    ),
):
    """Archive old runs to partitioned cold storage."""
    catalog_dir = _catalog_dir()

    if not catalog_dir.exists():
        typer.secho("✗ Catalog not found", fg="red")
        raise typer.Exit(1)

    try:
        result = archive(
            catalog_dir,
            archive_root=archive_dir,
            project_slug=project,
            dry_run=dry_run,
        )

        status = "[dry-run] " if dry_run else ""
        typer.secho(
            f"✓ {status}Archived {result.runs_archived} runs into "
            f"{result.partitions_created} partitions in {result.duration_s:.1f}s",
            fg="green",
        )

        if not dry_run:
            typer.secho(f"  Manifest: {result.manifest_path}", fg="cyan")
    except RuntimeError as e:
        typer.secho(f"✗ {str(e)}", fg="red")
        raise typer.Exit(1)


@app.command()
def check(
    status: str | None = typer.Option(
        None, "--status", help="Filter by status (OK, STALE, DIRTY_RUN, UNKNOWN_CODE)"
    ),
    check_outputs: bool = typer.Option(
        False, "--check-outputs", help="Also verify output files exist and are readable"
    ),
):
    """Check runs for git-drift validity against current HEAD."""
    from bathos.checker import check_output_files, check_output_sha_drift, check_runs
    from bathos.query import get_run

    catalog_dir = _catalog_dir()
    project_root = Path.cwd()

    results = check_runs(catalog_dir, project_root, status_filter=status)

    if not results:
        if status:
            typer.echo(f"No runs found with status={status}")
        else:
            typer.echo("No runs found in catalog")
        return

    # Print header
    header = f"{'RUN_ID':38} {'STATUS':12} {'RUN_HASH':40} {'CURRENT_HASH':40}"
    typer.echo(header)
    typer.echo("-" * len(header))

    # Print results
    stale_count = 0
    for result in results:
        typer.echo(
            f"{result.run_id:38} {result.status:12} {result.run_git_hash:40} {result.current_hash:40}"
        )
        if result.status == "STALE":
            stale_count += 1

    drift_count = 0

    # Check output files if requested
    if check_outputs:
        typer.secho("\n[Output File Status]", fg="cyan")
        for result in results:
            run = get_run(result.run_id, catalog_dir)
            if run is None:
                continue
            output_results = check_output_files(run)
            if output_results:
                for out_result in output_results:
                    status_color = "green" if out_result.status == "present" else "red"
                    typer.secho(
                        f"  {result.run_id}: {out_result.path} ({out_result.status})",
                        fg=status_color,
                    )
            else:
                typer.secho(f"  {result.run_id}: no output files", fg="dim")

        typer.secho("\n[Output SHA Drift]", fg="cyan")
        for result in results:
            run = get_run(result.run_id, catalog_dir)
            if run is None:
                continue
            drift_results = check_output_sha_drift(run)
            recorded = [d for d in drift_results if d.status != "UNRECORDED"]
            if not recorded:
                continue
            for drift in recorded:
                if drift.status == "OK":
                    typer.secho(
                        f"  {result.run_id}: {drift.path} (sha ok)",
                        fg="green",
                    )
                else:
                    drift_count += 1
                    typer.secho(
                        f"  {result.run_id}: {drift.path} ({drift.status.lower()})",
                        fg="red",
                    )

    # Exit with error if any STALE runs or SHA drift found
    if stale_count > 0 or drift_count > 0:
        typer.echo()
        if stale_count > 0:
            typer.echo(f"Warning: {stale_count} stale run(s) detected", err=True)
        if drift_count > 0:
            typer.echo(f"Warning: {drift_count} output SHA drift(s) detected", err=True)
        raise typer.Exit(1)


@remote_app.command("add")
def remote_add(
    name: str = typer.Argument(..., help="Remote name (e.g. 'engaging')"),
    url: str = typer.Argument(..., help="host:path (e.g. 'engaging:~/projects/myproject')"),
) -> None:
    """Add a remote host for sync."""
    from bathos.remote import add_remote

    cfg_path = find_project_config()
    if cfg_path is None:
        typer.echo("No .bth.toml found. Run 'bth init' first.")
        raise typer.Exit(1)

    if ":" not in url:
        typer.echo(f"Invalid URL '{url}': expected 'host:path' format")
        raise typer.Exit(1)

    host, path = url.split(":", 1)

    try:
        add_remote(cfg_path, name, host, path)
        typer.echo(f"Remote '{name}' added ({host}:{path})")
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(1)


@remote_app.command("list")
def remote_list() -> None:
    """List configured remotes."""
    from bathos.remote import list_remotes

    cfg_path = find_project_config()
    if cfg_path is None:
        typer.echo("No .bth.toml found. Run 'bth init' first.")
        raise typer.Exit(1)

    config = load_project_config(cfg_path)
    remotes = list_remotes(config)

    if not remotes:
        typer.echo("No remotes configured. Use 'bth remote add' to add one.")
        return

    # Calculate column widths
    name_width = max(len("NAME"), max((len(r[0]) for r in remotes), default=0), 10)
    host_path_width = max(
        len("HOST:PATH"), max((len(f"{r[1]}:{r[2]}") for r in remotes), default=0), 9
    )

    # Print header
    typer.echo(f"{'NAME':<{name_width}}  {'HOST:PATH':<{host_path_width}}")
    typer.echo("-" * name_width + "  " + "-" * host_path_width)

    # Print rows
    for name, host, remote_root in remotes:
        host_path = f"{host}:{remote_root}"
        typer.echo(f"{name:<{name_width}}  {host_path:<{host_path_width}}")


@remote_app.command("remove")
def remote_remove(
    name: str = typer.Argument(..., help="Remote name to remove"),
) -> None:
    """Remove a configured remote."""
    from bathos.remote import remove_remote

    cfg_path = find_project_config()
    if cfg_path is None:
        typer.echo("No .bth.toml found. Run 'bth init' first.")
        raise typer.Exit(1)

    try:
        remove_remote(cfg_path, name)
        typer.echo(f"Remote '{name}' removed.")
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(1)


@campaign_app.command("create")
def campaign_create(
    name: str = typer.Argument(...),
    mode: str = typer.Option("exploration", "--mode", help="exploration|confirmation|sequential"),
    sequential: bool = typer.Option(False, "--sequential", help="Shorthand for --mode sequential"),
    question: str | None = typer.Option(None, "--question"),
    hypothesis: str | None = typer.Option(None, "--hypothesis"),
    parent: str | None = typer.Option(None, "--parent", help="Parent campaign ID"),
):
    """Create a new campaign."""
    import duckdb

    from bathos.campaigns import create_campaign, list_campaigns

    # Resolve effective mode from --sequential shorthand
    if sequential and mode != "exploration":
        raise typer.BadParameter(
            "--sequential and --mode are mutually exclusive", param_hint="--mode/--sequential"
        )
    if sequential:
        mode = "sequential"

    slug = _require_project_slug()
    db = duckdb.connect(str(_catalog_dir() / "bathos.db"))
    try:
        existing = [
            c for c in list_campaigns(db, project_slug=slug, status="open") if c.name == name
        ]
        if existing:
            ids = ", ".join(c.id[:8] for c in existing)
            typer.echo(
                f"Warning: {len(existing)} open campaign(s) named {name!r} already exist: {ids}",
                err=True,
            )
        campaign = create_campaign(
            db,
            name=name,
            project_slug=slug,
            mode=mode,
            question=question,
            hypothesis=hypothesis,
            parent_campaign_id=parent,
        )
        typer.echo(f"Created campaign {campaign.id[:8]} — {campaign.name} ({campaign.mode})")
    finally:
        db.close()


@campaign_app.command("add")
def campaign_add(
    run_id: str = typer.Argument(..., help="Run ID to add"),
    campaign: str = typer.Option(..., "--campaign", "-c", help="Campaign ID"),
):
    """Add a run to a campaign."""
    import duckdb

    from bathos.campaigns import CampaignError, add_run_to_campaign

    db = duckdb.connect(str(_catalog_dir() / "bathos.db"))
    try:
        add_run_to_campaign(db, campaign, run_id)
        typer.echo(f"Added run {run_id[:8]} to campaign {campaign[:8]}")
    except CampaignError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        db.close()


@campaign_app.command("conclude")
def campaign_conclude(
    campaign_id: str = typer.Argument(..., help="Campaign ID"),
    outcome: str = typer.Option(
        ..., "--outcome", help="Outcome label (e.g. pass, fail, inconclusive)"
    ),
    note: str = typer.Option("", "--note", help="Conclusion narrative"),
    force: bool = typer.Option(
        False, "--force", help="Skip threshold warning for sequential campaigns"
    ),
    abort_if_below_threshold: bool = typer.Option(
        False, "--abort-if-below-threshold", help="Exit 1 if threshold not met"
    ),
):
    """Conclude a campaign with an outcome label."""
    import duckdb

    from bathos.campaigns import (
        CampaignError,
        _campaign_threshold_met,
        conclude_campaign,
        get_campaign,
    )

    db = duckdb.connect(str(_catalog_dir() / "bathos.db"))
    try:
        campaign = get_campaign(db, campaign_id)
        if campaign is None:
            typer.echo(f"Campaign not found: {campaign_id}", err=True)
            raise typer.Exit(1)

        # For sequential campaigns, check threshold before concluding
        if campaign.mode == "sequential" and campaign.stopping_threshold is not None:
            threshold_met = _campaign_threshold_met(db, campaign.id, campaign.stopping_threshold)
            if not threshold_met:
                if abort_if_below_threshold:
                    ep_rows = db.execute(
                        """
                        SELECT EXP(SUM(LN(cr.evalue)) FILTER (WHERE r.outcome != 'error' AND r.outcome != 'unknown'))
                        FROM campaign_runs cr INNER JOIN runs r ON cr.run_id = r.id
                        WHERE cr.campaign_id = ? AND cr.evalue IS NOT NULL
                    """,
                        [campaign.id],
                    ).fetchone()
                    ep = ep_rows[0] if ep_rows and ep_rows[0] is not None else 1.0
                    typer.echo(
                        f"Error: E_n has not reached stopping_threshold "
                        f"({ep:.1f} < {campaign.stopping_threshold:.1f}). Aborting.",
                        err=True,
                    )
                    raise typer.Exit(1)
                if not force:
                    ep_rows = db.execute(
                        """
                        SELECT EXP(SUM(LN(cr.evalue)) FILTER (WHERE r.outcome != 'error' AND r.outcome != 'unknown'))
                        FROM campaign_runs cr INNER JOIN runs r ON cr.run_id = r.id
                        WHERE cr.campaign_id = ? AND cr.evalue IS NOT NULL
                    """,
                        [campaign.id],
                    ).fetchone()
                    ep = ep_rows[0] if ep_rows and ep_rows[0] is not None else 1.0
                    typer.echo(
                        f"WARNING: E_n has not reached stopping_threshold "
                        f"({ep:.1f} < {campaign.stopping_threshold:.1f}). "
                        f"This will be flagged as premature stopping in sprint-audit.",
                        err=True,
                    )

        conclude_campaign(db, campaign_id, outcome, note)
        typer.echo(f"Concluded campaign {campaign_id[:8]} — outcome: {outcome}")
    except CampaignError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        db.close()


@claim_app.command("scaffold")
def claim_scaffold_cmd(
    campaign_id: str = typer.Argument(..., help="Campaign ID (or prefix)"),
):
    """Scaffold a claim.bth.toml template for a campaign."""
    import duckdb

    from bathos.claim import scaffold_claim
    from bathos.workspace import resolve_workspace

    catalog_dir = _catalog_dir()
    db_path = catalog_dir / "bathos.db"
    if not db_path.exists():
        typer.echo(f"Catalog database not found at {db_path}. Run 'bth compact' first.", err=True)
        raise typer.Exit(1)

    ws = resolve_workspace().fs_root
    db = duckdb.connect(str(db_path), read_only=False)
    try:
        claim_path = scaffold_claim(campaign_id, db, ws)
        typer.echo(f"Created: {claim_path}")
    except RuntimeError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        db.close()


@claim_app.command("register")
def claim_register_cmd(
    path: Path = typer.Argument(..., help="Path to claim .toml file"),
    campaign: str = typer.Option(..., "--campaign", "-c", help="Campaign ID (or prefix)"),
    force: bool = typer.Option(False, "--force", help="Re-register if claim already bound"),
):
    """Register a claim file with a campaign (records path + SHA256 anchor)."""
    import duckdb

    from bathos.claim import register_claim
    from bathos.workspace import resolve_workspace

    catalog_dir = _catalog_dir()
    db_path = catalog_dir / "bathos.db"
    if not db_path.exists():
        typer.echo(f"Catalog database not found at {db_path}. Run 'bth compact' first.", err=True)
        raise typer.Exit(1)

    ws = resolve_workspace().fs_root
    db = duckdb.connect(str(db_path), read_only=False)
    try:
        register_claim(path, campaign, db, ws, force=force)
        typer.echo(f"Registered claim for campaign {campaign[:8]}")
    except (RuntimeError, FileNotFoundError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        db.close()


@claim_app.command("validate")
def claim_validate_cmd(
    path: Path = typer.Argument(..., help="Path to claim .toml file"),
):
    """Validate a claim TOML file (structural + optional catalog-backed checks)."""
    import duckdb

    from bathos.claim import parse_claim, validate_claim

    try:
        claim = parse_claim(path)
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    db = None
    db_path = _catalog_dir() / "bathos.db"
    if db_path.exists():
        db = duckdb.connect(str(db_path), read_only=True)
    try:
        result = validate_claim(claim, db=db)
    finally:
        if db is not None:
            db.close()

    for info in result.infos:
        typer.secho(f"info: {info}", fg="cyan")
    for warning in result.warnings:
        typer.secho(f"warning: {warning}", fg="yellow")
    if result.errors:
        for error in result.errors:
            typer.echo(f"error: {error.message}", err=True)
        raise typer.Exit(1)

    typer.echo(f"✓ {path} is valid")


@attestation_app.command("scaffold")
def attestation_scaffold_cmd(
    kind: str = typer.Argument(..., help="'oracle_match' or 'repro_floor'"),
    label: str | None = typer.Option(None, "--label", help="Optional filename label"),
):
    """Scaffold an attestation.bth.toml template (S4; separate kind from `bth claim`)."""
    from bathos.attestation import scaffold_attestation
    from bathos.workspace import resolve_workspace

    ws = resolve_workspace().fs_root
    try:
        path = scaffold_attestation(kind, ws, label=label)
        typer.echo(f"Created: {path}")
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


@attestation_app.command("validate")
def attestation_validate_cmd(
    path: Path = typer.Argument(..., help="Path to attestation .toml file"),
):
    """Validate an attestation TOML file's kind-specific required fields."""
    from bathos.attestation import parse_attestation, validate_attestation

    try:
        attestation = parse_attestation(path)
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    result = validate_attestation(attestation)

    for warning in result.warnings:
        typer.secho(f"warning: {warning}", fg="yellow")
    if result.errors:
        for error in result.errors:
            typer.echo(f"error: {error.message}", err=True)
        raise typer.Exit(1)

    typer.echo(f"✓ {path} is valid ({attestation.kind}, verdict={attestation.verdict})")


@attestation_app.command("register")
def attestation_register_cmd(
    path: Path = typer.Argument(..., help="Path to attestation .toml file"),
    campaign_id: str | None = typer.Option(
        None, "--campaign-id", help="Optional campaign this attestation belongs to"
    ),
):
    """Register an attestation file: anchor it by its own SHA256 (durable by default).

    Anchors kind=oracle_match|repro_floor, content_hash=attested product's
    content_hash, label=verdict — a SEPARATE anchored sidecar kind from `bth claim`
    (see gate #3488 sign-off); reuses the S2 anchor-insert seam via
    `bathos.anchor.DurableAnchorStore` so the attestation survives
    `bth compact --force-rebuild`.
    """
    import dataclasses
    import json as json_mod

    from bathos.attestation import register_attestation

    try:
        record = register_attestation(path, _catalog_dir(), campaign_id=campaign_id)
        typer.echo(json_mod.dumps(dataclasses.asdict(record), indent=2))
    except (FileNotFoundError, ValueError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


@campaign_app.command("attest-parity")
def campaign_attest_parity(
    campaign_id: str = typer.Argument(..., help="Campaign ID (or prefix)"),
    parity_run_id: str = typer.Argument(..., help="Parity run ID to bind to the campaign claim"),
):
    """Bind a passing literature-parity run to the campaign's claim (F4)."""
    import duckdb

    from bathos.claim import attest_parity
    from bathos.workspace import resolve_workspace

    workspace_root = resolve_workspace().fs_root
    db = duckdb.connect(str(_catalog_dir() / "bathos.db"))
    try:
        attest_parity(campaign_id, parity_run_id, db, workspace_root)
        typer.echo(f"Attested parity run {parity_run_id[:8]} on campaign {campaign_id[:8]}")
    except (ValueError, RuntimeError, FileNotFoundError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        db.close()


@campaign_app.command("ls")
def campaign_ls(
    status: str | None = typer.Option(None, "--status", help="Filter by status: open|concluded"),
):
    """List campaigns."""
    import duckdb

    from bathos.campaigns import list_campaigns
    from bathos.rich_fmt import render_campaign_table

    db = duckdb.connect(str(_catalog_dir() / "bathos.db"), read_only=True)
    try:
        campaigns = list_campaigns(db, status=status)
        render_campaign_table(campaigns)
    finally:
        db.close()


@campaign_app.command("show")
def campaign_show(
    campaign_id: str = typer.Argument(..., help="Campaign ID (or prefix)"),
):
    """Show campaign details."""
    import duckdb

    from bathos.campaigns import get_campaign

    db = duckdb.connect(str(_catalog_dir() / "bathos.db"), read_only=True)
    try:
        campaign = get_campaign(db, campaign_id)
        if not campaign:
            typer.echo(f"Campaign not found: {campaign_id}", err=True)
            raise typer.Exit(1)
        typer.echo(f"Campaign: {campaign.name} ({campaign.id})")
        typer.echo(f"Mode:     {campaign.mode}")
        typer.echo(f"Status:   {campaign.status}")
        if campaign.question:
            typer.echo(f"Question: {campaign.question}")
        if campaign.hypothesis:
            typer.echo(f"Hypothesis: {campaign.hypothesis}")
        if campaign.concluded_at:
            typer.echo(f"Concluded: {campaign.concluded_at} — {campaign.outcome_label}")
            if campaign.conclusion:
                typer.echo(f"Conclusion: {campaign.conclusion}")
    finally:
        db.close()


@campaign_app.command("review")
def campaign_review(
    campaign_id: str = typer.Argument(..., help="Campaign ID"),
):
    """Review campaign: residual rate, bypass rate, outcome distribution."""
    import duckdb

    from bathos.campaigns import get_campaign, review_campaign
    from bathos.rich_fmt import render_campaign_review, render_popper_summary

    db = duckdb.connect(str(_catalog_dir() / "bathos.db"), read_only=True)
    try:
        campaign = get_campaign(db, campaign_id)
        if campaign is None:
            typer.echo(f"Campaign not found: {campaign_id}", err=True)
            raise typer.Exit(1)
        review = review_campaign(db, campaign_id)
        if "error" in review:
            typer.echo(review["error"], err=True)
            raise typer.Exit(1)
        render_campaign_review(campaign, review)
        render_popper_summary(review.get("popper"))
    finally:
        db.close()


@report_app.command("emit")
def report_emit(
    campaign_id: str = typer.Argument(..., help="Campaign ID"),
):
    """Generate and emit campaign report and figure manifest sidecars.

    Creates both campaign_report.json and figure_manifest.json at
    <catalog>/sidecars/<campaign_id>/ for a concluded campaign.
    """
    import duckdb

    from bathos.campaigns import (
        CampaignError,
        emit_campaign_report,
        emit_figure_manifest,
        get_campaign,
    )

    catalog_dir = _catalog_dir()
    db = duckdb.connect(str(catalog_dir / "bathos.db"))
    try:
        # Verify campaign exists and is concluded
        campaign = get_campaign(db, campaign_id)
        if campaign is None:
            typer.echo(f"Campaign not found: {campaign_id}", err=True)
            raise typer.Exit(1)
        if campaign.status != "concluded":
            typer.echo(
                f"Campaign {campaign_id[:8]} is not concluded (status: {campaign.status})", err=True
            )
            raise typer.Exit(1)

        # Emit both artifacts
        manifest_ref = f"sidecars/{campaign_id}/figure_manifest.json"
        emit_figure_manifest(db, str(catalog_dir), campaign_id)
        emit_campaign_report(db, str(catalog_dir), campaign_id, figure_manifest_ref=manifest_ref)

        # Report success
        report_path = catalog_dir / "sidecars" / campaign_id / "campaign_report.json"
        manifest_path = catalog_dir / "sidecars" / campaign_id / "figure_manifest.json"
        typer.echo(f"✓ Emitted campaign report and manifest for {campaign_id[:8]}")
        typer.echo(f"  Report:   {report_path}")
        typer.echo(f"  Manifest: {manifest_path}")
    except CampaignError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        db.close()


@report_app.command("show")
def report_show(
    campaign_id: str = typer.Argument(..., help="Campaign ID (full, exact match)"),
):
    """Read the campaign_report.json sidecar for a campaign (S1 read-back API; real).

    Thin CLI wrapper over bathos.readback.read_campaign_report. Emit the sidecar first
    with `bth report emit <campaign_id>`.
    """
    import json as json_mod

    from bathos.readback import read_campaign_report

    try:
        report = read_campaign_report(_catalog_dir(), campaign_id)
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(json_mod.dumps(report.model_dump(), indent=2))


@report_app.command("show-manifest")
def report_show_manifest(
    campaign_id: str = typer.Argument(..., help="Campaign ID (full, exact match)"),
):
    """Read the figure_manifest.json sidecar for a campaign (S1 read-back API; real).

    Thin CLI wrapper over bathos.readback.read_figure_manifest. Emit the sidecar first
    with `bth report emit <campaign_id>`.
    """
    import json as json_mod

    from bathos.readback import read_figure_manifest

    try:
        manifest = read_figure_manifest(_catalog_dir(), campaign_id)
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(json_mod.dumps(manifest.model_dump(), indent=2))


@query_app.command("resolve-pin")
def query_resolve_pin(
    run_id: str = typer.Argument(..., help="Run ID that produced the output"),
    output_path: str = typer.Argument(..., help="Output path as recorded on the run"),
):
    """Resolve a run_id + output_path pin to content_hash/trust_state/freshness (S1; real)."""
    import dataclasses
    import json as json_mod

    from bathos.query import CatalogError
    from bathos.readback import resolve_pin

    try:
        pin = resolve_pin(_catalog_dir(), run_id, output_path)
    except CatalogError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(json_mod.dumps(dataclasses.asdict(pin), indent=2))


@query_app.command("trust-state")
def query_trust_state(
    content_hash: str = typer.Argument(..., help="Content hash of the product"),
):
    """Look up trust_state for a content_hash (S1; null-stub pending trust ledger S3)."""
    from bathos.readback import get_trust_state

    typer.echo(str(get_trust_state(_catalog_dir(), content_hash)))


@query_app.command("attestation")
def query_attestation_cmd(
    content_hash: str = typer.Argument(..., help="Content hash of the attested product"),
    min_strength: str | None = typer.Option(
        None, "--min-strength", help="'oracle_match' or 'repro_floor'"
    ),
):
    """Query attestation for a content_hash (S1; real, backed by the S4 attestation store)."""
    import json as json_mod

    from bathos.readback import query_attestation

    result = query_attestation(_catalog_dir(), content_hash, min_strength)
    typer.echo(json_mod.dumps(result, indent=2) if result is not None else "null")


@query_app.command("figures")
def query_figures(
    asset_sha256: str | None = typer.Option(None, "--asset-sha256"),
    input_hash: str | None = typer.Option(None, "--input-hash"),
):
    """Look up figure registry entries (S1; real, backed by the S2 anchor store)."""
    import json as json_mod

    from bathos.readback import figure_lookup

    figures = figure_lookup(_catalog_dir(), asset_sha256=asset_sha256, input_hash=input_hash)
    typer.echo(json_mod.dumps(figures, indent=2))


@query_app.command("candidates")
def query_candidates(
    campaign_id: str = typer.Argument(..., help="Campaign ID to list candidates for"),
):
    """List candidate-tier products for a campaign (S1; null-stub pending trust ledger S3)."""
    import json as json_mod

    from bathos.readback import list_candidates

    candidates = list_candidates(_catalog_dir(), campaign_id)
    typer.echo(json_mod.dumps(candidates, indent=2))


@anchor_app.command("insert")
def anchor_insert_cmd(
    path: str = typer.Argument(..., help="Sidecar path to anchor (not resolved/verified against disk)"),
    sha256: str = typer.Argument(..., help="SHA256 of the sidecar file's contents"),
    kind: str = typer.Option(..., "--kind", "-k", help="Free-form anchor kind, e.g. 'figure', 'attestation'"),
    label: str | None = typer.Option(None, "--label", help="Optional human-readable label"),
    content_hash: str | None = typer.Option(
        None, "--content-hash", help="Optional hash of the underlying data product"
    ),
    campaign_id: str | None = typer.Option(
        None, "--campaign-id", help="Optional campaign this anchor belongs to"
    ),
):
    """Anchor a sidecar by (path, sha256) into the catalog (S2 write seam; real).

    Re-anchoring the same (path, sha256) upserts the other fields — there is no
    --force gate, unlike `bth claim register`, because this seam makes no durability
    claim in the first place (see bathos.anchor module docstring).
    """
    import dataclasses
    import json as json_mod

    from bathos.anchor import register_anchor

    record = register_anchor(
        _catalog_dir(),
        path,
        sha256,
        kind,
        label=label,
        content_hash=content_hash,
        campaign_id=campaign_id,
    )
    typer.echo(json_mod.dumps(dataclasses.asdict(record), indent=2))


@anchor_app.command("get")
def anchor_get_cmd(
    path: str = typer.Argument(..., help="Path used at anchor time"),
    sha256: str = typer.Argument(..., help="SHA256 used at anchor time"),
):
    """Read back an anchored sidecar by (path, sha256) (S2; real; round-trips insert)."""
    import dataclasses
    import json as json_mod

    from bathos.anchor import get_anchor

    record = get_anchor(_catalog_dir(), path, sha256)
    typer.echo(json_mod.dumps(dataclasses.asdict(record), indent=2) if record else "null")


@anchor_app.command("find")
def anchor_find_cmd(
    kind: str | None = typer.Option(None, "--kind", "-k", help="Filter by anchor kind"),
    sha256: str | None = typer.Option(None, "--sha256", help="Filter by the anchor's own sha256"),
    content_hash: str | None = typer.Option(
        None, "--content-hash", help="Filter by the underlying data product's hash"
    ),
    campaign_id: str | None = typer.Option(None, "--campaign-id", help="Filter by campaign"),
):
    """Find anchored sidecars matching filters (S2; real)."""
    import dataclasses
    import json as json_mod

    from bathos.anchor import find_anchors

    records = find_anchors(
        _catalog_dir(),
        kind=kind,
        sha256=sha256,
        content_hash=content_hash,
        campaign_id=campaign_id,
    )
    typer.echo(json_mod.dumps([dataclasses.asdict(r) for r in records], indent=2))


@remote_app.command("test")
def remote_test(
    name: str = typer.Argument(..., help="Remote name to test"),
) -> None:
    """Test SSH connectivity to a remote."""
    from bathos.remote import test_remote

    cfg_path = find_project_config()
    if cfg_path is None:
        typer.echo("No .bth.toml found. Run 'bth init' first.")
        raise typer.Exit(1)

    config = load_project_config(cfg_path)

    try:
        result = test_remote(config, name)
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(1)

    if result.success:
        typer.echo(f"{name}: ok ({result.latency_ms:.0f}ms)")
    else:
        typer.echo(f"{name}: unreachable — {result.error}")
        raise typer.Exit(1)


@app.command()
def sync(
    remote: str | None = typer.Argument(
        None, help="Remote name from .bth.toml (auto-selected if only one configured)"
    ),
    pull: bool = typer.Option(False, "--pull", help="Pull from remote (default: push)"),
):
    """Sync cool-tier catalog to/from remote."""
    cfg_path = find_project_config()
    if cfg_path is None:
        typer.echo("No .bth.toml found. Run `bth init` first.", err=True)
        raise typer.Exit(1)

    config = load_project_config(cfg_path)

    if remote is None:
        remotes = list(config.remotes.keys())
        if len(remotes) == 0:
            typer.echo("No remotes configured. Use 'bth remote add' to add one.")
            raise typer.Exit(1)
        elif len(remotes) == 1:
            remote = remotes[0]
        else:
            names = ", ".join(f"'{r}'" for r in sorted(remotes))
            typer.echo(f"Multiple remotes configured ({names}). Specify one explicitly.")
            raise typer.Exit(1)

    catalog_dir = _catalog_dir()

    try:
        result = sync_catalog(remote, config, catalog_dir, pull=pull)
        direction = "Pulled" if pull else "Pushed"
        filter_msg = (
            f" (filtered {result.filtered} from other projects)" if result.filtered > 0 else ""
        )
        typer.echo(
            f"{direction} {result.transferred} runs{filter_msg} to/from '{result.remote}' in {result.duration_s:.1f}s"
        )
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)


@app.command()
def submit(
    command: list[str] = typer.Argument(..., help="Command to submit (after --)"),
    preset: str | None = typer.Option(None, "--preset", help="Override preset"),
    remote: str | None = typer.Option(None, "--remote", help="Override remote"),
    array: str = typer.Option("", "--array", help="SLURM array spec e.g. 0-9%4"),
    dependency: str = typer.Option("", "--dependency", help="SLURM dependency e.g. afterok:12345"),
    name: str = typer.Option("", "--name", help="Job name (default: first token of command)"),
    sbatch_arg: list[str] = typer.Option(
        [], "--sbatch-arg", help="Passthrough raw sbatch arg; repeatable"
    ),
    sidecar: str | None = typer.Option(
        None, "--sidecar", help="Explicit path to experiment sidecar (.bth.toml)"
    ),
    push_first: bool = typer.Option(
        True, "--push-first/--no-push-first", help="Push project before submitting"
    ),
    wait: bool = typer.Option(
        False, "--wait/--no-wait", help="Block until job reaches terminal state"
    ),
    then_pull: bool = typer.Option(
        False, "--then-pull", help="Pull results after job completes (implies --wait)"
    ),
    then_sync: bool = typer.Option(
        False, "--then-sync", help="Run bth sync after pull (implies --then-pull --wait)"
    ),
):
    """Submit a command to the cluster using a configured preset."""
    import tomllib

    from bathos.cluster import (
        job_wait,
        pull_project,
        push_project,
        resolve_cluster_config,
        submit_job,
    )
    from bathos.config import find_project_config, load_project_config

    # 1. Validate flag implications
    if then_sync:
        then_pull = True
    if then_pull:
        wait = True

    # 2. Load project config
    cfg_path = find_project_config()
    if cfg_path is None:
        typer.echo("No .bth.toml found. Run `bth init` first.", err=True)
        raise typer.Exit(1)
    config = load_project_config(cfg_path)

    # 3. Load sidecar [cluster] override (optional)
    sidecar_data = None
    sidecar_path = None
    if sidecar:
        try:
            sidecar_path = Path(sidecar)
            with open(sidecar, "rb") as f:
                sidecar_data = tomllib.load(f)
        except (FileNotFoundError, OSError) as e:
            typer.echo(f"Failed to parse sidecar: {e}", err=True)
            raise typer.Exit(1)
    else:
        # Scan command list for .py file
        for cmd_token in command:
            if cmd_token.endswith(".py"):
                candidate_py = Path(cmd_token)
                if candidate_py.exists():
                    candidate_sidecar = candidate_py.with_suffix(".bth.toml")
                    if candidate_sidecar.exists():
                        try:
                            sidecar_path = candidate_sidecar
                            with open(candidate_sidecar, "rb") as f:
                                sidecar_data = tomllib.load(f)
                        except (FileNotFoundError, OSError):
                            pass  # Silently continue
                break

    # 3a. Check reproduction prerequisite gate (before cluster submission)
    parsed_sidecar = None
    if sidecar_data:
        from bathos.prereg import check_reproduction_prerequisite
        from bathos.sidecar import parse_sidecar

        try:
            # Parse sidecar to get reproduction and stage_name
            if sidecar_path:
                parsed_sidecar = parse_sidecar(sidecar_path)
            else:
                # If we couldn't locate the sidecar file, skip the gate check
                parsed_sidecar = None

            if parsed_sidecar and parsed_sidecar.reproduction:
                requires_pass_stem = parsed_sidecar.reproduction.requires_pass_stem
                stage_name = parsed_sidecar.stage_name or "exploration"

                # Only enforce hard gate for validation/production stages
                if requires_pass_stem and stage_name in ("validation", "production"):
                    found = check_reproduction_prerequisite(requires_pass_stem, _catalog_dir())
                    if not found:
                        typer.echo(
                            f"REPRODUCTION_PREREQUISITE_UNMET: no passing run of '{requires_pass_stem}' found",
                            err=True,
                        )
                        raise typer.Exit(1)
                # Advisory warning for exploration/calibration stages
                elif requires_pass_stem and stage_name in ("exploration", "calibration"):
                    found = check_reproduction_prerequisite(requires_pass_stem, _catalog_dir())
                    if not found:
                        typer.echo(
                            f"WARNING: no passing run of '{requires_pass_stem}' found (advisory for {stage_name} stage)",
                            err=True,
                        )
        except typer.Exit:
            # Let typer.Exit exceptions through (hard gate failures)
            raise
        except Exception as e:
            # Log but don't fail on gate check exceptions
            typer.echo(f"Warning: reproduction prerequisite check failed: {e}", err=True)

    # 3b. Check parity confound gate (F3 submit-gate, mirrors reproduction gate)
    if parsed_sidecar:
        from bathos.parity import check_parity_confounds_for_submit

        try:
            result = check_parity_confounds_for_submit(parsed_sidecar, _catalog_dir())
            stage_name = parsed_sidecar.stage_name or "exploration"

            # Hard-block for validation/production if not satisfied and determinable
            if result["satisfied"] is False and result["tier_enforced"]:
                typer.echo(
                    f"PARITY_PREREQUISITE_UNMET: no passing parity run for '{parsed_sidecar.reproduction.requires_parity_stem}' found",
                    err=True,
                )
                raise typer.Exit(1)
            # Advisory warning for exploration/calibration if not satisfied
            elif result["satisfied"] is False and not result["tier_enforced"]:
                typer.echo(
                    f"WARNING: no passing parity run for '{parsed_sidecar.reproduction.requires_parity_stem}' found (advisory for {stage_name} stage)",
                    err=True,
                )
            # Indeterminate case (warm DB absent): fail open with advisory
            elif result["satisfied"] is None:
                typer.echo(
                    f"WARNING: could not determine parity prerequisite status for '{parsed_sidecar.reproduction.requires_parity_stem}' (warm DB absent); proceeding with caution",
                    err=True,
                )
        except typer.Exit:
            raise
        except Exception as e:
            # Log but don't fail on gate check exceptions
            typer.echo(f"Warning: parity prerequisite check failed: {e}", err=True)

    # 4. Resolve cluster config
    try:
        cluster = resolve_cluster_config(
            config,
            sidecar_data=sidecar_data,
            cli_remote=remote,
            cli_preset=preset,
        )
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)

    # 5. Derive job_name
    job_name = name or (command[0].split("/")[-1] if command else "bth-submit")

    # 6. Push if requested
    if push_first:
        try:
            push_project(cluster.remote, cluster.project)
        except RuntimeError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1)

    # 7. Submit
    cmd_str = " ".join(command)
    try:
        result = submit_job(
            cluster.remote,
            cluster.project,
            cluster.preset,
            cmd_str,
            job_name=job_name,
            array=array,
            dependency=dependency,
            sbatch_args=sbatch_arg or None,
        )
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)

    slurm_job_id = result["slurm_job_id"]
    typer.echo(f"Submitted {slurm_job_id} on {cluster.remote} using preset {cluster.preset}")

    # 7a. Write submit-provenance record (AC-9 Part 1)
    try:
        import hashlib

        from bathos.catalog import write_submit_provenance

        sidecar_sha256 = ""
        stage_name = "exploration"

        if sidecar_path and sidecar_path.exists():
            # Compute SHA256 of sidecar file
            sha256_hash = hashlib.sha256()
            with open(sidecar_path, "rb") as f:
                sha256_hash.update(f.read())
            sidecar_sha256 = sha256_hash.hexdigest()

        # Extract stage_name from sidecar if available
        if sidecar_data:
            experiment_section = sidecar_data.get("experiment", {})
            if isinstance(experiment_section, dict):
                stage_name = experiment_section.get("stage_name", "exploration")

        # Write submit provenance to catalog
        write_submit_provenance(
            project_slug=cluster.project,
            command=cmd_str,
            sidecar_sha256=sidecar_sha256,
            myxcel_job_id=slurm_job_id,
            stage_name=stage_name,
            catalog_dir=_catalog_dir(),
        )
    except Exception as e:
        # Log but don't fail on provenance write errors
        typer.echo(f"Warning: submit-provenance write failed: {e}", err=True)

    # 8. Exit if not waiting
    if not wait:
        raise typer.Exit(0)

    # 9. Wait for completion
    try:
        wait_result_dict = job_wait(cluster.remote, slurm_job_id)
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)

    # 10. Pull if requested
    if then_pull:
        try:
            pull_project(cluster.remote, cluster.project)
        except RuntimeError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1)

    # 11. Sync if requested
    if then_sync:
        try:
            sync_catalog(cluster.remote, config, _catalog_dir(), pull=True)
        except (ValueError, RuntimeError) as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1)

    # 12. Handle exit codes
    wait_result = wait_result_dict.get("wait_result", "")
    failure_class = wait_result_dict.get("failure_class", "")

    if wait_result == "timeout":
        typer.echo(
            f"Job {slurm_job_id} still running on {cluster.remote}. "
            f"Re-run with --wait --no-push-first to resume polling, or cancel with: "
            f"myxcel cancel-job --remote {cluster.remote} {slurm_job_id}",
            err=True,
        )
        raise typer.Exit(2)

    if failure_class and failure_class != "SUCCESS":
        raise typer.Exit(1)

    raise typer.Exit(0)


@app.command()
def migrate(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be migrated without writing"
    ),
    classify: bool = typer.Option(
        False, "--classify", help="Classify flat scripts into subdirs (Phase 2)"
    ),
):
    """Migrate cool-tier Parquet fragments to current schema, optionally classifying scripts."""
    if classify:
        # Delegate to classify command
        from bathos.classifier import apply_classify_plan, build_move_plan, classify_flat_scripts

        project_root = Path.cwd()
        if not (project_root / "scripts").exists():
            typer.echo(f"Error: no scripts/ directory found at {project_root}", err=True)
            raise typer.Exit(1)

        results = classify_flat_scripts(project_root)
        if results:
            plan = build_move_plan(project_root, results)
            try:
                apply_classify_plan(plan, scaffold_sidecars=True)
                typer.echo(f"Classified and moved {len(plan.actions)} script(s).")
            except RuntimeError as e:
                typer.echo(f"Error: {e}", err=True)
                raise typer.Exit(1)
        else:
            typer.echo("No flat scripts found to classify.")
        return

    from bathos.migrate import migrate_catalog

    result = migrate_catalog(_catalog_dir(), dry_run=dry_run)
    typer.echo(f"Scanned {result.scanned} fragments.")
    typer.echo(f"  {result.already_current} already at current schema")
    if result.migrated:
        action = "Would migrate" if dry_run else "Migrated"
        typer.echo(f"  {action} {result.migrated} fragment(s).")
    else:
        typer.echo("  Nothing to migrate.")


@app.command("migrate-to-project-subdirs")
def migrate_to_subdirs_cmd(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be moved without writing"
    ),
):
    """Move flat cool-tier run parquets into per-project subdirectories.

    Reads each run's project_slug and moves it to runs/<slug>/run_<uuid>.parquet.
    Run this on both local and remote before using per-project sync filtering.
    """
    from bathos.migrate import migrate_to_project_subdirs

    result = migrate_to_project_subdirs(_catalog_dir(), dry_run=dry_run)
    action = "Would move" if dry_run else "Moved"
    typer.echo(f"{action} {result.moved} run(s) into per-project subdirectories.")
    if result.skipped:
        typer.echo(f"  {result.skipped} already in place (skipped).")
    if result.by_slug:
        for slug, count in sorted(result.by_slug.items()):
            typer.echo(f"  {slug}: {count}")


@app.command()
def classify(
    min_confidence: str = typer.Option(
        "low",
        "--min-confidence",
        help="Only include classifications at or above this level (high|medium|low)",
    ),
    no_content: bool = typer.Option(
        False, "--no-content", help="Skip content-augmented classification"
    ),
    no_scaffold: bool = typer.Option(
        False, "--no-scaffold", help="Do not scaffold sidecar stubs when applying"
    ),
    apply: bool = typer.Option(False, "--apply", help="Execute git mv commands and write sidecars"),
    project: Path = typer.Option(Path.cwd(), "--project", help="Project root (defaults to cwd)"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON (machine-readable)"),
):
    """Classify flat scripts into the correct scripts/ subdirectory.

    Scans scripts/ root for .py files not already in a subdirectory,
    infers the correct target directory, and prints a git mv plan.
    Apply the plan with --apply.
    """
    import json

    from rich import print as rprint
    from rich.table import Table

    from bathos.classifier import (
        ClassificationConfidence,
        apply_classify_plan,
        build_move_plan,
        classify_flat_scripts,
    )

    if min_confidence.lower() not in ("high", "medium", "low"):
        typer.echo(
            f"Error: min-confidence must be high, medium, or low (got {min_confidence!r})", err=True
        )
        raise typer.Exit(1)

    project_root = project.resolve()
    if not (project_root / "scripts").exists():
        typer.echo(f"Error: no scripts/ directory found at {project_root}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Scanning {project_root / 'scripts'} for unclassified files...")

    # Classify all flat scripts
    results = classify_flat_scripts(project_root)

    if not results:
        typer.echo("No flat scripts found.")
        raise typer.Exit(0)

    # Build move plan
    plan = build_move_plan(project_root, results)

    # Filter by min_confidence if needed
    min_conf_enum = ClassificationConfidence(min_confidence.lower())
    confidence_order = [
        ClassificationConfidence.HIGH,
        ClassificationConfidence.MEDIUM,
        ClassificationConfidence.LOW,
    ]
    min_conf_idx = confidence_order.index(min_conf_enum)

    filtered_actions = [
        a
        for a in plan.actions
        if confidence_order.index(a.classification.confidence) <= min_conf_idx
    ]

    if not filtered_actions:
        typer.echo(f"No classifications found at or above {min_confidence} confidence.")
        raise typer.Exit(0)

    # Output as JSON if requested
    if json_output:
        output = {
            "project_root": str(project_root),
            "total_files": len(results),
            "high_confidence": plan.high_confidence,
            "medium_confidence": plan.medium_confidence,
            "low_confidence": plan.low_confidence,
            "conflicts": plan.conflicts,
            "sidecars_to_scaffold": plan.sidecars_to_scaffold,
            "actions": [
                {
                    "source": str(a.source),
                    "destination": str(a.destination),
                    "confidence": a.classification.confidence.value,
                    "rationale": a.classification.rationale,
                    "rename_required": a.classification.rename_required,
                    "suggested_stem": a.classification.suggested_stem,
                    "sidecar_required": a.classification.sidecar_required,
                    "conflict": a.conflict,
                }
                for a in filtered_actions
            ],
        }
        typer.echo(json.dumps(output, indent=2))
        raise typer.Exit(0)

    # Build and display table
    table = Table(title="Script Classification Plan")
    table.add_column("Source", style="cyan")
    table.add_column("Target", style="green")
    table.add_column("Confidence", style="yellow")
    table.add_column("Rename", style="magenta")
    table.add_column("Sidecar", style="blue")

    for action in filtered_actions:
        rename_str = "yes" if action.classification.rename_required else "no"
        sidecar_str = "scaffold" if action.classification.sidecar_required else "no"
        table.add_row(
            str(action.source),
            f"scripts/{action.classification.target_dir}/",
            action.classification.confidence.value,
            rename_str,
            sidecar_str,
        )

    rprint(table)
    typer.echo()

    # Summary line
    summary_parts = [
        f"{len(filtered_actions)} script(s)",
        f"{plan.high_confidence} HIGH",
        f"{plan.medium_confidence} MEDIUM",
        f"{plan.low_confidence} LOW",
    ]
    if plan.conflicts:
        summary_parts.append(f"{plan.conflicts} conflict(s)")
    if plan.sidecars_to_scaffold:
        summary_parts.append(f"{plan.sidecars_to_scaffold} sidecar(s) to scaffold")

    typer.echo(" | ".join(summary_parts))

    if apply:
        if plan.conflicts:
            typer.echo(
                f"Error: {plan.conflicts} conflict(s) detected. Resolve them manually before retrying.",
                err=True,
            )
            raise typer.Exit(1)

        scaffold = not no_scaffold
        try:
            apply_classify_plan(plan, scaffold_sidecars=scaffold)
            typer.echo(f"Applied: moved {len(filtered_actions)} script(s).")
            if scaffold:
                typer.echo(f"Scaffolded: {plan.sidecars_to_scaffold} sidecar(s).")
        except RuntimeError as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(1)
    else:
        typer.echo("Run with --apply to execute.")


@app.command("sprint-audit")
def sprint_audit_cmd(
    hours: int = typer.Option(24, "--hours", help="Lookback window in hours"),
):
    """Audit recent runs and campaigns across all registered projects."""
    from bathos.sprint_audit import sprint_audit

    result = sprint_audit(hours)
    if result["warnings"]:
        typer.echo("Warnings:")
        for w in result["warnings"]:
            typer.echo(f"  WARNING: {w}")
        typer.echo()
    if not result["audit_results"]:
        typer.echo("No projects found. Run 'bth init' in each project first.")
        return
    for slug, data in result["audit_results"].items():
        typer.echo(f"{slug}: {data['runs']} runs, {data['campaigns']} campaigns")
        for anomaly in data["anomalies"]:
            typer.echo(f"  WARNING: {anomaly}")


@app.command()
def lint(
    project_root: Path = typer.Option(
        Path("."), "--project-root", "-p", help="Project root to lint"
    ),
    concentration_threshold: int = typer.Option(
        20,
        "--concentration-threshold",
        help="Unvalidated-run count threshold for run-concentration lint (strict >)",
    ),
):
    """Check scripts/ for naming conventions and missing sidecars."""
    from bathos.linter import (
        IssueSeverity,
        check_adversarial_checks,
        check_baseline_ref_exists,
        check_bypass_trend,
        check_canonical_stage_names,
        check_claim_opaque_labels,
        check_ephemeral_output_paths,
        check_residual_rates,
        check_run_concentration,
        check_threshold_basis,
        check_todo_strings_in_scaffold,
        check_unfired_branches,
        lint_project,
    )

    issues = lint_project(project_root.resolve())

    # Tier-1 claim label checks under .bth/claims/
    issues.extend(check_claim_opaque_labels(project_root.resolve()))

    # Add Tier-2 file-based checks
    issues.extend(check_adversarial_checks(project_root.resolve()))
    issues.extend(check_threshold_basis(project_root.resolve()))
    issues.extend(check_todo_strings_in_scaffold(project_root.resolve()))

    # Add warm-catalog Tier-2 checks if catalog exists
    catalog_dir = _catalog_dir()
    db_path = catalog_dir / "bathos.db"
    if db_path.exists():
        issues.extend(check_residual_rates(catalog_dir))
        issues.extend(check_bypass_trend(catalog_dir))
        issues.extend(check_unfired_branches(catalog_dir))
        issues.extend(check_run_concentration(catalog_dir, threshold=concentration_threshold))
        issues.extend(check_ephemeral_output_paths(catalog_dir))
        issues.extend(check_canonical_stage_names(catalog_dir))
        issues.extend(check_baseline_ref_exists(project_root.resolve(), catalog_dir, db_path))

    if not issues:
        typer.echo("No issues found.")
        return

    errors = [i for i in issues if i.severity == IssueSeverity.ERROR]
    warnings = [i for i in issues if i.severity == IssueSeverity.WARNING]
    infos = [i for i in issues if i.severity == IssueSeverity.INFO]

    for issue in issues:
        if issue.severity == IssueSeverity.ERROR:
            prefix = "error"
        elif issue.severity == IssueSeverity.WARNING:
            prefix = "warning"
        else:
            prefix = "info"
        try:
            display_path = issue.path.relative_to(project_root.resolve())
        except ValueError:
            display_path = issue.path
        typer.echo(f"{prefix}: {display_path} — {issue.issue}: {issue.detail}")

    typer.echo()
    typer.echo(f"{len(errors)} error(s), {len(warnings)} warning(s).")

    if errors:
        raise typer.Exit(1)


@app.command("new-experiment")
def new_experiment_cmd(
    name: str = typer.Argument(
        ..., help="Experiment name (verb_noun style, e.g. run_nvt_stability)"
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files"),
):
    """Scaffold a new experiment script and sidecar in scripts/experiments/."""
    from bathos.new_experiment import scaffold_experiment

    try:
        result = scaffold_experiment(name=name, project_root=Path.cwd(), force=force)
    except FileExistsError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    if result.name_warning:
        typer.echo(f"Warning: {result.name_warning}")
    typer.echo(f"Created: {result.script}")
    typer.echo(f"Created: {result.sidecar}")


@app.command("validate-sidecar")
def validate_sidecar_cmd(
    path: Path = typer.Argument(..., help="Path to .bth.toml sidecar file"),
):
    """Validate a sidecar TOML file for structural integrity."""
    from bathos.sidecar import SidecarError, parse_sidecar
    from bathos.validate import validate_sidecar

    try:
        sidecar = parse_sidecar(path)
    except SidecarError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    result = validate_sidecar(sidecar, sidecar_path=path)

    if result.errors:
        for error in result.errors:
            typer.echo(f"{error.field}: {error.message}")
        raise typer.Exit(1)

    typer.echo(f"✓ {path} is valid")


@app.command("export")
def export_cmd(
    tool: str = typer.Option("claude", "--tool", "-t", help="Target tool: claude or gemini"),
    level: str = typer.Option(
        "user", "--level", "-l", help="Install level: user, workspace, or system"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print what would happen without writing"
    ),
    html: bool = typer.Option(
        False, "--html", help="Export catalog as a self-contained HTML report"
    ),
    out: str = typer.Option("report.html", "--out", "-o", help="Output file for --html export"),
    project: str | None = typer.Option(None, "--project", help="Filter by project (--html only)"),
    campaign: str | None = typer.Option(
        None, "--campaign", help="Filter by campaign (--html only)"
    ),
    surface: str | None = typer.Option(
        None, "--surface", help="Plugin surface (e.g., claude_code)"
    ),
):
    """Export the using-bathos skill and register MCP server, or export catalog as HTML."""
    # Phase 3: plugin surface post-step hook
    if surface:
        from bathos import __version__

        typer.echo(f"Plugin export hook: surface={surface}, level={level}, bathos v{__version__}")
        raise typer.Exit(0)

    if html:
        try:
            from bathos.viz.html import export_html as do_export
        except ImportError:
            typer.echo(
                "Error: bathos[viz] is not installed.\nInstall with: uv tool install 'bathos[viz]'",
                err=True,
            )
            raise typer.Exit(1)

        from bathos.query import list_runs

        catalog = _catalog_dir()
        runs = list_runs(catalog, project=project)
        if campaign:
            runs = [r for r in runs if r.campaign_id == campaign]

        if not runs:
            typer.echo(f"No matching runs. Writing empty report to {out}.", err=True)

        path, size_warned = do_export(runs, output_path=out, catalog_dir=catalog)
        typer.echo(f"Exported to {path}")
        if size_warned:
            typer.echo("(Use --project or --campaign to reduce file size)", err=True)
        return

    from bathos.export import ExportError, export_skill, register_mcp, resolve_target

    try:
        target = resolve_target(tool=tool, level=level)
    except ExportError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    result = export_skill(target=target, dry_run=dry_run)
    mcp_target = register_mcp(tool=tool, level=level, dry_run=dry_run)

    if dry_run:
        typer.echo(f"Dry run — would write skill to:  {result.target}")
        typer.echo(f"Dry run — would register MCP at: {mcp_target}")
    else:
        typer.echo(f"Exported skill to:    {result.target}")
        typer.echo(f"Registered MCP at:   {mcp_target}")


@app.command()
def view(
    port: int = typer.Option(8080, "--port", "-p", help="Port to bind to"),
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind to"),
    no_open: bool = typer.Option(False, "--no-open", help="Do not open browser automatically"),
    project: str | None = typer.Option(None, "--project", help="Scope to single project"),
):
    """Launch a local FastAPI dashboard to visualize runs and campaigns."""
    try:
        from bathos.viz.server import run_server
    except ImportError:
        typer.echo(
            "Error: bathos[viz] is not installed.\nInstall with: uv tool install 'bathos[viz]'",
            err=True,
        )
        raise typer.Exit(1)

    from bathos.query import list_runs

    catalog = _catalog_dir()
    runs = list_runs(catalog, project=project, limit=1001)
    total_run_count = len(runs)
    runs = runs[:1000]

    try:
        run_server(
            runs, total_run_count=total_run_count, host=host, port=port, open_browser=not no_open
        )
    except OSError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


@app.command("catalog-version")
def catalog_version_cmd():
    """Show schema version status of the catalog."""
    from bathos.migrate import migrate_catalog
    from bathos.schema import CURRENT_SCHEMA_VERSION

    catalog_dir = _catalog_dir()
    typer.echo(f"Current schema version: {CURRENT_SCHEMA_VERSION}")

    result = migrate_catalog(catalog_dir, dry_run=True)
    typer.echo(f"Cool-tier fragments: {result.scanned} scanned, {result.migrated} need migration.")

    db_path = catalog_dir / "bathos.db"
    if db_path.exists():
        import duckdb

        con = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = con.execute(
                "SELECT warm_version, migrated_at FROM _schema_migrations ORDER BY migrated_at DESC LIMIT 1"
            ).fetchall()
            if rows:
                typer.echo(f"Warm DB version: {rows[0][0]} (last migration: {rows[0][1]})")
            else:
                typer.echo("Warm DB: no migration history found.")
        except Exception:
            typer.echo("Warm DB: no migration history found.")
        finally:
            con.close()
    else:
        typer.echo("Warm DB: not yet created (run bth compact).")


def _parse_since(since: str | None) -> datetime | None:
    if since is None:
        return None
    if since.endswith("d"):
        return datetime.now(UTC) - timedelta(days=float(since[:-1]))
    if since.endswith("h"):
        return datetime.now(UTC) - timedelta(hours=float(since[:-1]))
    return None


@postmortem_app.command()
def scaffold(
    run_id: str = typer.Argument(..., help="Run ID to scaffold a postmortem template for"),
):
    """Scaffold a new postmortem template for the given Run ID."""
    from bathos.postmortem import find_run_for_scaffold, scaffold_postmortem_template
    from bathos.workspace import resolve_workspace

    run_row = find_run_for_scaffold(run_id, _catalog_dir())
    if not run_row:
        typer.echo("Run not found", err=True)
        raise typer.Exit(1)

    command, _project_slug = run_row
    workspace_root = resolve_workspace().fs_root
    postmortem_path = scaffold_postmortem_template(command, run_id, workspace_root)
    typer.echo(f"Scaffolded postmortem template at {postmortem_path}")


@postmortem_app.command()
def show(
    run_id: str = typer.Argument(..., help="Run ID of the postmortem to show"),
    strict_files: bool = typer.Option(
        False, "--strict-files", help="Fail if files in asset_links do not exist"
    ),
):
    """Display and validate the postmortem for the given Run ID."""
    import duckdb

    from bathos.postmortem import parse_postmortem, validate_postmortem
    from bathos.schema import Run
    from bathos.workspace import resolve_workspace

    workspace_root = resolve_workspace().fs_root

    # Find the postmortem TOML file in workspace
    postmortem_file = None
    if workspace_root.exists():
        for pm_file in workspace_root.rglob("*.bth.postmortem.toml"):
            try:
                pm = parse_postmortem(pm_file)
                if pm.run_id == run_id:
                    postmortem_file = pm_file
                    break
            except Exception:
                pass

    if not postmortem_file:
        typer.echo("Postmortem not found", err=True)
        raise typer.Exit(1)

    pm = parse_postmortem(postmortem_file)

    # Search for run in DB
    run_obj = None
    db_path = _catalog_dir() / "bathos.db"
    if db_path.exists():
        con = duckdb.connect(str(db_path))
        try:
            arrow_tbl = con.execute("SELECT * FROM runs WHERE id = ?", [run_id]).arrow()
            if arrow_tbl.num_rows > 0:
                pydict = arrow_tbl.to_pydict()
                run_obj = Run.from_arrow_row(pydict, 0)
        except Exception:
            pass
        finally:
            con.close()

    if not run_obj:
        # Check cool fragments
        from bathos.catalog import read_runs

        cool_runs = read_runs(_catalog_dir())
        for r in cool_runs:
            if r.id == run_id:
                run_obj = r
                break

    # Perform validation
    result = validate_postmortem(
        pm, workspace_root=workspace_root, run=run_obj, strict_files=strict_files
    )
    if not result.ok:
        typer.echo("Validation failed", err=True)
        for err in result.errors:
            typer.echo(f"- {err.message}", err=True)
        typer.echo(f"Hypothesis status: {pm.hypothesis_status}", err=True)
        typer.echo(f"Verdict override: {pm.verdict_override}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Run ID: {pm.run_id}")
    typer.echo(f"Status: {pm.status}")
    typer.echo(f"Hypothesis Status: {pm.hypothesis_status}")
    typer.echo(f"Verdict Override: {pm.verdict_override}")
    typer.echo(f"Summary: {pm.summary}")
    if pm.asset_links:
        typer.echo(f"Asset Links: {pm.asset_links}")


@postmortem_app.command()
def validate(
    file: Path = typer.Argument(..., help="Path to .bth.postmortem.toml file to validate"),
    strict: bool = typer.Option(False, "--strict", help="Treat missing run ID as error"),
    strict_files: bool = typer.Option(
        False, "--strict-files", help="Treat missing asset files as error"
    ),
):
    """Validate a postmortem TOML file for structural and logical correctness."""
    from bathos.postmortem import parse_postmortem, validate_postmortem
    from bathos.workspace import resolve_workspace

    if not file.exists():
        typer.echo(f"File not found: {file}", err=True)
        raise typer.Exit(1)

    try:
        pm = parse_postmortem(file)
    except Exception as e:
        typer.echo(f"Parse error: {e}", err=True)
        raise typer.Exit(1)

    workspace_root = resolve_workspace().fs_root

    result = validate_postmortem(
        pm, workspace_root=workspace_root, strict=strict, strict_files=strict_files
    )
    if result.ok:
        typer.echo(f"✓ {file.name} is valid")
    else:
        typer.echo(f"✗ Validation failed for {file.name}:", err=True)
        for err in result.errors:
            typer.echo(f"  - {err.message}", err=True)
        raise typer.Exit(1)


@outputs_app.command("list")
def outputs_list(
    run_id: str = typer.Argument(..., help="Run ID to display outputs for"),
    live: bool = typer.Option(
        False, "--live", help="Re-stat files from filesystem instead of using catalog snapshot."
    ),
):
    """Display output files registered for a run."""
    import json

    from bathos.query import get_run

    catalog = _catalog_dir()
    run = get_run(run_id, catalog)

    if not run:
        typer.echo(f"Run not found: {run_id}", err=True)
        raise typer.Exit(1)

    # Parse output_metadata JSON
    try:
        if run.output_metadata and run.output_metadata != "[]":
            files = json.loads(run.output_metadata)
        else:
            files = []
    except (json.JSONDecodeError, TypeError):
        files = []

    if not files:
        typer.echo(f"Run {run_id[:8]} has no registered output files.")
        return

    from bathos.rich_fmt import render_output_list

    render_output_list(run_id, files, live=live)


@outputs_app.command("summary")
def outputs_summary(
    project: str | None = typer.Option(None, "--project", help="Filter by project slug"),
    since: str | None = typer.Option(None, "--since", help="Time filter (e.g. 30d, 7d, 1d)"),
):
    """Display summary of output files across runs."""
    import json

    import duckdb

    catalog = _catalog_dir()
    db_path = catalog / "bathos.db"

    if not db_path.exists():
        typer.echo("[yellow]Catalog not yet compacted.[/yellow]")
        typer.echo("[dim]Run 'bth compact' to aggregate output metadata into the warm tier.[/dim]")
        return

    # Query warm tier
    con = duckdb.connect(str(db_path))
    con.execute("SET TimeZone='UTC'")

    # Build query
    query = "SELECT project_slug, id, output_metadata FROM runs WHERE output_metadata IS NOT NULL AND output_metadata != '[]'"
    params = []

    # Add project filter
    if project:
        query += " AND project_slug = ?"
        params.append(project)

    # Add time filter
    if since:
        import re as regex_mod

        match = regex_mod.match(r"(\d+)([dhm])", since)
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
                hours = num * 24  # default to days

            query += " AND timestamp > now() - interval '" + str(int(hours)) + " hour'"

    rows = con.execute(query, params).fetchall()
    con.close()

    if not rows:
        from bathos.rich_fmt import render_outputs_summary

        render_outputs_summary([], since=since)
        return

    # Aggregate by project
    aggregated = {}
    for project_slug, run_id, output_metadata_json in rows:
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

    from bathos.rich_fmt import render_outputs_summary

    render_outputs_summary(list(aggregated.values()), since=since)
