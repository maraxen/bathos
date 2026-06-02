"""Rich console formatters for runs, campaigns, and reviews."""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from datetime import datetime
from pathlib import Path

from rich.text import Text

from bathos.schema import Run
from bathos.campaigns import Campaign


def render_runs_table(runs: list[Run], console: Console | None = None) -> None:
    """Render a table of runs with key columns.

    Args:
        runs: List of Run objects to display
        console: Rich Console instance. If None, creates a default Console.
    """
    if console is None:
        console = Console()

    if not runs:
        console.print("[dim]No runs to display.[/dim]")
        return

    table = Table(title="Runs")
    table.add_column("ID", style="cyan")
    table.add_column("Project", style="magenta")
    table.add_column("Status", style="yellow")
    table.add_column("Outcome", style="green")
    table.add_column("Duration", style="blue")
    table.add_column("Campaign", style="white")
    table.add_column("Branch", style="green")
    table.add_column("Timestamp", style="dim")

    for run in runs:
        # Format ID as first 8 chars
        run_id_short = run.id[:8]

        # Format status with color
        status_color = _get_status_color(run.status)
        status_text = Text(run.status, style=status_color)

        # Format outcome with color
        outcome_color = _get_outcome_color(run.outcome)
        outcome_text = Text(run.outcome or "—", style=outcome_color)

        # Format duration
        duration_str = _format_duration(run.duration_s)

        # Format campaign ID
        campaign_id_short = run.campaign_id[:8] if run.campaign_id else ""

        # Format timestamp
        timestamp_str = run.timestamp.isoformat()[:19]  # First 19 chars (date + time)

        table.add_row(
            run_id_short,
            run.project_slug,
            status_text,
            outcome_text,
            duration_str,
            campaign_id_short,
            run.git_branch,
            timestamp_str,
        )

    console.print(table)


def render_run_detail(run: Run, console: Console | None = None) -> None:
    """Render detailed information about a single run.

    Args:
        run: Run object to display
        console: Rich Console instance. If None, creates a default Console.
    """
    if console is None:
        console = Console()

    # Execution Panel
    execution_info = (
        f"[bold]Command:[/bold] {run.command}\n"
        f"[bold]Args:[/bold] {' '.join(run.argv)}\n"
        f"[bold]Hostname:[/bold] {run.hostname or '—'}\n"
        f"[bold]SLURM Job ID:[/bold] {run.slurm_job_id or '—'}\n"
        f"[bold]Duration:[/bold] {_format_duration(run.duration_s)}\n"
        f"[bold]Exit Code:[/bold] {run.exit_code}"
    )
    console.print(Panel(execution_info, title="Execution", expand=False))

    # Provenance Panel
    git_hash_short = run.git_hash[:8] if run.git_hash else "—"
    git_dirty_str = "[red]dirty[/red]" if run.git_dirty else "[green]clean[/green]"
    parent_id_short = run.parent_run_id[:8] if run.parent_run_id else "—"

    provenance_info = (
        f"[bold]Git Hash:[/bold] {git_hash_short} ({run.git_branch})\n"
        f"[bold]Git Status:[/bold] {git_dirty_str}\n"
        f"[bold]Script SHA256:[/bold] {run.script_sha256 or '—'}\n"
        f"[bold]Sidecar Path:[/bold] {run.sidecar_path or '—'}\n"
        f"[bold]Sidecar Mode:[/bold] {run.sidecar_mode or '—'}\n"
        f"[bold]Agent Mode:[/bold] {run.agent_mode or '—'}\n"
        f"[bold]Parent Run ID:[/bold] {parent_id_short}"
    )
    console.print(Panel(provenance_info, title="Provenance", expand=False))

    # Outcome Panel
    status_color = _get_status_color(run.status)
    outcome_color = _get_outcome_color(run.outcome)

    outcome_info = (
        f"[bold]Status:[/bold] {Text(run.status, style=status_color)}\n"
        f"[bold]Outcome:[/bold] {Text(run.outcome or '—', style=outcome_color)}\n"
        f"[bold]Residual:[/bold] {'yes' if run.outcome_is_residual else 'no'}\n"
        f"[bold]Tags:[/bold] {', '.join(run.tags) if run.tags else '—'}\n"
    )

    # Show output paths (up to 3)
    output_paths_display = ""
    if run.output_paths:
        shown = run.output_paths[:3]
        output_paths_display = "\n".join(shown)
        if len(run.output_paths) > 3:
            output_paths_display += f"\n... and {len(run.output_paths) - 3} more"
        outcome_info += f"[bold]Output Paths:[/bold] {output_paths_display}"
    else:
        outcome_info += "[bold]Output Paths:[/bold] —"

    console.print(Panel(outcome_info, title="Outcome", expand=False))

    # Postmortem Panel (only if postmortem_status != "unassigned")
    if run.postmortem_status != "unassigned":
        postmortem_info = (
            f"[bold]Status:[/bold] {run.postmortem_status}\n"
            f"[bold]Hypothesis Status:[/bold] {run.postmortem_hypothesis_status}\n"
            f"[bold]Author:[/bold] {run.postmortem_author or '—'}\n"
            f"[bold]Summary:[/bold] {run.postmortem_summary or '—'}\n"
            f"[bold]Path:[/bold] {run.postmortem_path or '—'}"
        )
        console.print(Panel(postmortem_info, title="Postmortem", expand=False))


def render_campaign_table(campaigns: list[Campaign], console: Console | None = None) -> None:
    """Render a table of campaigns.

    Args:
        campaigns: List of Campaign objects to display
        console: Rich Console instance. If None, creates a default Console.
    """
    if console is None:
        console = Console()

    if not campaigns:
        console.print("[dim]No campaigns to display.[/dim]")
        return

    table = Table(title="Campaigns")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="magenta")
    table.add_column("Mode", style="yellow")
    table.add_column("Status", style="green")
    table.add_column("Started", style="blue")

    for campaign in campaigns:
        # Format ID as first 8 chars
        campaign_id_short = campaign.id[:8]

        # Format started_at (first 19 chars)
        started_str = campaign.started_at[:19] if campaign.started_at else "—"

        table.add_row(
            campaign_id_short,
            campaign.name,
            campaign.mode,
            campaign.status,
            started_str,
        )

    console.print(table)


def render_campaign_review(
    campaign: Campaign, review: dict, console: Console | None = None
) -> None:
    """Render a campaign review with outcome distribution and anomalies.

    Args:
        campaign: Campaign object being reviewed
        review: Dictionary with keys 'outcome_distribution' and optionally 'anomalies'
        console: Rich Console instance. If None, creates a default Console.
    """
    if console is None:
        console = Console()

    # Campaign header
    header = f"{campaign.name} ({campaign.mode})"
    console.print(Panel(header, title="Campaign Review", expand=False))

    # Outcome distribution table
    outcome_dist = review.get("outcome_distribution", {})
    if outcome_dist:
        dist_table = Table(title="Outcome Distribution")
        dist_table.add_column("Outcome", style="cyan")
        dist_table.add_column("Count", style="magenta")

        for outcome, count in outcome_dist.items():
            dist_table.add_row(outcome, str(count))

        console.print(dist_table)

    # Anomalies panel
    anomalies = review.get("anomalies", [])
    if anomalies:
        anomaly_text = "\n".join(anomalies)
        anomaly_panel = Panel(
            Text(anomaly_text, style="yellow"),
            title="Anomalies",
            expand=False,
        )
        console.print(anomaly_panel)


def _get_status_color(status: str) -> str:
    """Return Rich color style for status."""
    if status == "completed":
        return "green"
    elif status == "failed":
        return "red"
    elif status == "running":
        return "yellow"
    else:
        return "dim"


def _get_outcome_color(outcome: str) -> str:
    """Return Rich color style for outcome."""
    if outcome == "pass":
        return "green"
    elif outcome == "fail":
        return "red"
    elif outcome == "error":
        return "bold red"
    elif outcome == "marginal":
        return "yellow"
    else:
        return "dim"


def _format_duration(duration_s: float) -> str:
    """Format duration in seconds as human-readable string."""
    if duration_s < 60:
        return f"{duration_s:.1f}s"
    else:
        minutes = int(duration_s // 60)
        seconds = int(duration_s % 60)
        return f"{minutes}m {seconds}s"


def render_popper_summary(popper_data: dict | None, console: Console | None = None) -> None:
    """Render POPPER sequential test summary table.

    Prints nothing if popper_data is None (non-sequential campaign).

    Args:
        popper_data: Dictionary with keys 'mode', 'stopping_threshold', 'threshold_met', 'scripts'
        console: Rich Console instance. If None, creates a default Console.
    """
    if popper_data is None:
        return

    if console is None:
        console = Console()

    threshold = popper_data.get("stopping_threshold")
    scripts = popper_data.get("scripts", [])
    overall_met = popper_data.get("threshold_met", False)

    alpha_str = f"≈ {1.0/threshold:.3f}" if threshold and threshold > 0 else "?"

    table = Table(title="POPPER Sequential Test", show_header=True, header_style="bold cyan")
    table.add_column("script_key", style="dim", no_wrap=True, max_width=20)
    table.add_column("n_eff", justify="right")
    table.add_column("n_excl", justify="right")
    table.add_column("E_n", justify="right")
    table.add_column("threshold_met", justify="center")

    for s in scripts:
        ep = s.get("evalue_product", 1.0)
        met = s.get("threshold_met", False)
        met_text = Text("YES", style="bold green") if met else Text("NO", style="bold red")
        key = str(s.get("script_key", ""))
        key_display = key[:18] + ".." if len(key) > 20 else key
        table.add_row(
            key_display,
            str(s.get("n_effective", 0)),
            str(s.get("n_excluded", 0)),
            f"{ep:.2f}" if ep is not None else "—",
            met_text,
        )

    console.print(table)

    if threshold is not None:
        console.print(f"  Stopping threshold : {threshold:.1f}  (alpha {alpha_str})")
        console.print(f"  Scripts in campaign: {len(scripts)}")

    if overall_met:
        console.print(
            Text("  Campaign conclusion: THRESHOLD REACHED — eligible to conclude", style="bold green")
        )
    elif scripts:
        below = sum(1 for s in scripts if not s.get("threshold_met", False))
        console.print(
            Text(
                f"  Campaign conclusion: NOT YET REACHED ({below} of {len(scripts)} script(s) below threshold)",
                style="yellow",
            )
        )
    else:
        console.print(Text("  Campaign conclusion: NO RUNS YET", style="dim"))



def render_output_list(run_id: str, files: list[dict], live: bool = False) -> None:
    """Render a list of output files for a run.

    Each file dict contains: path, status, size_bytes, mtime_unix, sha256.
    Status values: "present", "missing", "unreadable".
    If live=True, re-stat each file from filesystem (display only, not persistent).

    Args:
        run_id: Run ID for context
        files: List of file dicts from output_metadata
        live: If True, re-stat files from filesystem
    """
    console = Console()

    if not files:
        console.print(f"[dim]Run {run_id[:8]} has no registered output files.[/dim]")
        return

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

    # Build table
    table = Table(title=f"Output Files: {run_id[:8]}")
    table.add_column("Path", style="cyan")
    table.add_column("Status", style="yellow")
    table.add_column("Size", style="blue")
    table.add_column("Modified", style="dim")
    table.add_column("SHA256", style="white")

    for f in files:
        path = f.get("path", "")
        path_obj = Path(path)
        # Truncate path: show parent dir + basename
        if len(path) > 50:
            basename = path_obj.name
            parent = path_obj.parent.name
            path_display = f".../{parent}/{basename}"
        else:
            path_display = path

        status = f.get("status", "unknown")
        # Color code status
        if status == "present":
            status_text = Text(status, style="green")
        elif status == "missing":
            status_text = Text(status, style="red")
        elif status == "unreadable":
            status_text = Text(status, style="yellow")
        else:
            status_text = Text(status, style="dim")

        # Format size
        size_bytes = f.get("size_bytes", 0)
        if size_bytes < 1024:
            size_str = f"{size_bytes}B"
        elif size_bytes < 1024 * 1024:
            size_str = f"{size_bytes / 1024:.1f}KB"
        else:
            size_str = f"{size_bytes / (1024 * 1024):.1f}MB"

        # Format mtime
        mtime_unix = f.get("mtime_unix", 0)
        if mtime_unix > 0:
            mtime_str = datetime.fromtimestamp(mtime_unix).isoformat()[:19]
        else:
            mtime_str = "—"

        # Format SHA256
        sha256 = f.get("sha256")
        if sha256:
            sha256_display = str(sha256)[:8]
        else:
            sha256_display = "—"

        table.add_row(path_display, status_text, size_str, mtime_str, sha256_display)

    console.print(table)


def render_outputs_summary(rows: list[dict], since: str | None = None) -> None:
    """Render a summary of output files across runs.

    Each row: {project: str, run_count: int, file_count: int, total_bytes: int, missing_count: int}

    Args:
        rows: List of aggregated output rows
        since: Time filter description (e.g. "7d", "30d") for context
    """
    console = Console()

    if not rows:
        console.print("[yellow]No output data found.[/yellow]")
        console.print("[dim]Hint: Run 'bth compact' to aggregate output metadata into the warm tier.[/dim]")
        return

    # Build table
    since_str = f" (since {since})" if since else ""
    table = Table(title=f"Output Summary{since_str}")
    table.add_column("Project", style="cyan")
    table.add_column("Runs", style="magenta")
    table.add_column("Files", style="blue")
    table.add_column("Total Size", style="green")
    table.add_column("Missing", style="red")

    total_runs = 0
    total_files = 0
    total_bytes = 0
    total_missing = 0

    for row in rows:
        project = row.get("project", "?")
        run_count = row.get("run_count", 0)
        file_count = row.get("file_count", 0)
        total_b = row.get("total_bytes", 0)
        missing_count = row.get("missing_count", 0)

        total_runs += run_count
        total_files += file_count
        total_bytes += total_b
        total_missing += missing_count

        # Format size
        if total_b < 1024:
            size_str = f"{total_b}B"
        elif total_b < 1024 * 1024:
            size_str = f"{total_b / 1024:.1f}KB"
        elif total_b < 1024 * 1024 * 1024:
            size_str = f"{total_b / (1024 * 1024):.1f}MB"
        else:
            size_str = f"{total_b / (1024 * 1024 * 1024):.1f}GB"

        missing_text = Text(str(missing_count), style="red" if missing_count > 0 else "green")

        table.add_row(project, str(run_count), str(file_count), size_str, missing_text)

    # Add footer with totals
    if len(rows) > 1:
        if total_bytes < 1024:
            total_size_str = f"{total_bytes}B"
        elif total_bytes < 1024 * 1024:
            total_size_str = f"{total_bytes / 1024:.1f}KB"
        elif total_bytes < 1024 * 1024 * 1024:
            total_size_str = f"{total_bytes / (1024 * 1024):.1f}MB"
        else:
            total_size_str = f"{total_bytes / (1024 * 1024 * 1024):.1f}GB"

        total_missing_text = Text(
            str(total_missing), style="red" if total_missing > 0 else "green"
        )
        table.add_row(
            "[bold]TOTAL[/bold]",
            str(total_runs),
            str(total_files),
            total_size_str,
            total_missing_text,
        )

    console.print(table)
