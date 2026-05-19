from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

from bathos.archive import archive

app = typer.Typer(help="bathos — local-first experiment tracking")


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


@app.command()
def sync(
    remote: str = typer.Argument(..., help="Remote name from .bth.toml"),
    pull: bool = typer.Option(False, "--pull", help="Pull from remote (default: push)"),
):
    """Sync cool-tier catalog to/from remote."""
    from bathos.config import find_project_config, load_project_config
    from bathos.sync import sync_catalog

    cfg_path = find_project_config()
    if cfg_path is None:
        typer.echo("No .bth.toml found. Run `bth init` first.", err=True)
        raise typer.Exit(1)

    config = load_project_config(cfg_path)
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
    """Export the using-bathos skill to a code tool (Claude Code or Gemini CLI)."""
    from bathos.export import export_skill, resolve_target, ExportError

    try:
        target = resolve_target(tool=tool, level=level)
    except ExportError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    result = export_skill(target=target, dry_run=dry_run)

    if dry_run:
        typer.echo(f"Dry run — would write skill to: {result.target}")
    else:
        typer.echo(f"Exported using-bathos skill to: {result.target}")


def _parse_since(since: str | None) -> datetime | None:
    if since is None:
        return None
    if since.endswith("d"):
        return datetime.now(UTC) - timedelta(days=float(since[:-1]))
    if since.endswith("h"):
        return datetime.now(UTC) - timedelta(hours=float(since[:-1]))
    return None
