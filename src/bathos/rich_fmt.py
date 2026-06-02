"""Rich console formatters for runs, campaigns, and reviews."""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
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
