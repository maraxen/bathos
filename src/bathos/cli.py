from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

from bathos.archive import archive
from bathos.config import find_project_config, load_project_config
from bathos.sync import sync_catalog

app = typer.Typer(help="bathos — local-first experiment tracking")

remote_app = typer.Typer(help="Manage remote hosts for sync.")
app.add_typer(remote_app, name="remote")

campaign_app = typer.Typer(help="Manage experiment campaigns")
app.add_typer(campaign_app, name="campaign")


def _catalog_dir() -> Path:
    override = os.environ.get("BTH_CATALOG_DIR")
    if override:
        return Path(override)
    from bathos.config import default_catalog_dir

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
    no_sidecar: bool = typer.Option(False, "--no-sidecar", help="Bypass sidecar enforcement (logs BYPASSED)"),
    derived_from: str | None = typer.Option(None, "--derived-from", help="Parent run ID for lineage"),
    campaign: str | None = typer.Option(None, "--campaign", help="Campaign ID to associate this run with"),
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
    if not runs:
        typer.echo("No runs found.")
        return
    header = f"{'ID':38} {'PROJECT':12} {'STATUS':10} {'EXIT':5} {'OUTCOME':10} {'DURATION':8} COMMAND"
    typer.echo(header)
    typer.echo("-" * len(header))
    for r in runs:
        outcome_str = r.outcome if r.outcome else "-"
        typer.echo(
            f"{r.id:38} {r.project_slug:12} {r.status:10} {r.exit_code:5} "
            f"{outcome_str:10} {r.duration_s:7.1f}s {r.command[:40]}"
        )

    # Check if compaction is recommended and show banner if needed
    if should_compact(catalog_dir):
        frag_count = _fragment_count(catalog_dir)
        typer.echo()
        typer.echo(f"⚠  {frag_count} uncompacted runs — run 'bth compact' to speed up queries")


@app.command()
def show(run_id: str = typer.Argument(...)):
    """Show full details of a run."""
    from bathos.query import get_run

    r = get_run(run_id, _catalog_dir())
    if r is None:
        typer.echo(f"Run not found: {run_id}", err=True)
        raise typer.Exit(1)
    typer.echo(f"id:           {r.id}")
    typer.echo(f"project:      {r.project_slug}")
    typer.echo(f"status:       {r.status}")
    typer.echo(f"exit_code:    {r.exit_code}")
    typer.echo(f"duration:     {r.duration_s:.2f}s")
    typer.echo(f"git_hash:     {r.git_hash}")
    typer.echo(f"git_branch:   {r.git_branch}")
    typer.echo(f"git_dirty:    {r.git_dirty}")
    typer.echo(f"timestamp:    {r.timestamp.isoformat()}")
    typer.echo(f"command:      {r.command}")
    typer.echo(f"output_paths: {r.output_paths}")
    typer.echo(f"tags:         {r.tags}")


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
def compact():
    """Compact cool fragments into warm DuckDB catalog."""
    from bathos.compact import compact as compact_catalog

    catalog_dir = _catalog_dir()
    result = compact_catalog(catalog_dir)
    typer.echo(f"Compacted {result.ingested} runs into bathos.db in {result.duration_s:.1f}s")


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
    from bathos.checker import check_output_files, check_runs
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

    # Exit with error if any STALE runs found
    if stale_count > 0:
        typer.echo()
        typer.echo(f"Warning: {stale_count} stale run(s) detected", err=True)
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
    host_path_width = max(len("HOST:PATH"), max((len(f"{r[1]}:{r[2]}") for r in remotes), default=0), 9)

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
    mode: str = typer.Option("exploration", "--mode", help="exploration|confirmation"),
    question: str | None = typer.Option(None, "--question"),
    hypothesis: str | None = typer.Option(None, "--hypothesis"),
    parent: str | None = typer.Option(None, "--parent", help="Parent campaign ID"),
):
    """Create a new campaign."""
    import duckdb
    from bathos.campaigns import create_campaign
    slug = _require_project_slug()
    db = duckdb.connect(str(_catalog_dir() / "bathos.db"))
    try:
        campaign = create_campaign(db, name=name, project_slug=slug, mode=mode, question=question, hypothesis=hypothesis, parent_campaign_id=parent)
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
    from bathos.campaigns import add_run_to_campaign, CampaignError
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
    outcome: str = typer.Option(..., "--outcome", help="Outcome label (e.g. pass, fail, inconclusive)"),
    note: str = typer.Option("", "--note", help="Conclusion narrative"),
):
    """Conclude a campaign with an outcome label."""
    import duckdb
    from bathos.campaigns import conclude_campaign, CampaignError
    db = duckdb.connect(str(_catalog_dir() / "bathos.db"))
    try:
        conclude_campaign(db, campaign_id, outcome, note)
        typer.echo(f"Concluded campaign {campaign_id[:8]} — outcome: {outcome}")
    except CampaignError as e:
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
    db = duckdb.connect(str(_catalog_dir() / "bathos.db"), read_only=True)
    try:
        campaigns = list_campaigns(db, status=status)
        if not campaigns:
            typer.echo("No campaigns found.")
            return
        for c in campaigns:
            typer.echo(f"{c.id[:8]} {c.name:30} {c.mode:12} {c.status}")
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
    from bathos.campaigns import review_campaign
    db = duckdb.connect(str(_catalog_dir() / "bathos.db"), read_only=True)
    try:
        review = review_campaign(db, campaign_id)
        if "error" in review:
            typer.echo(review["error"], err=True)
            raise typer.Exit(1)
        typer.echo(f"Runs: {review['total_runs']}")
        typer.echo(f"Residual rate: {review['residual_rate']:.1%}")
        typer.echo(f"Bypass rate: {review['bypass_rate']:.1%}")
        typer.echo(f"Unknown rate: {review['unknown_rate']:.1%}")
        typer.echo(f"Outcomes: {review['outcome_distribution']}")
        for anomaly in review["anomalies"]:
            typer.echo(f"  WARNING: {anomaly}")
    finally:
        db.close()


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
    remote: str | None = typer.Argument(None, help="Remote name from .bth.toml (auto-selected if only one configured)"),
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
        direction = "pulled" if pull else "pushed"
        typer.echo(
            f"{direction.capitalize()} {result.transferred} runs to/from '{result.remote}' in {result.duration_s:.1f}s"
        )
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)


@app.command()
def migrate(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be migrated without writing"),
):
    """Migrate cool-tier Parquet fragments to current schema."""
    from bathos.migrate import migrate_catalog

    result = migrate_catalog(_catalog_dir(), dry_run=dry_run)
    typer.echo(f"Scanned {result.scanned} fragments.")
    typer.echo(f"  {result.already_current} already at current schema")
    if result.migrated:
        action = "Would migrate" if dry_run else "Migrated"
        typer.echo(f"  {action} {result.migrated} fragment(s).")
    else:
        typer.echo("  Nothing to migrate.")


@app.command()
def lint(
    project_root: Path = typer.Option(Path("."), "--project-root", "-p", help="Project root to lint"),
):
    """Check scripts/ for naming conventions and missing sidecars."""
    from bathos.linter import lint_project, IssueSeverity

    issues = lint_project(project_root.resolve())

    if not issues:
        typer.echo("No issues found.")
        return

    errors = [i for i in issues if i.severity == IssueSeverity.ERROR]
    warnings = [i for i in issues if i.severity == IssueSeverity.WARNING]

    for issue in issues:
        prefix = "error" if issue.severity == IssueSeverity.ERROR else "warning"
        typer.echo(f"{prefix}: {issue.path.relative_to(project_root.resolve())} — {issue.issue}: {issue.detail}")

    typer.echo()
    typer.echo(f"{len(errors)} error(s), {len(warnings)} warning(s).")

    if errors:
        raise typer.Exit(1)


@app.command("new-experiment")
def new_experiment_cmd(
    name: str = typer.Argument(..., help="Experiment name (verb_noun style, e.g. run_nvt_stability)"),
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


@app.command("export")
def export_cmd(
    tool: str = typer.Option("claude", "--tool", "-t", help="Target tool: claude or gemini"),
    level: str = typer.Option("user", "--level", "-l", help="Install level: user, workspace, or system"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would happen without writing"),
):
    """Export the using-bathos skill and register MCP server for a code tool."""
    from bathos.export import export_skill, resolve_target, register_mcp, ExportError

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


@app.command("catalog-version")
def catalog_version_cmd():
    """Show schema version status of the catalog."""
    from bathos.schema import CURRENT_SCHEMA_VERSION
    from bathos.migrate import migrate_catalog

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
