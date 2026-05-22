from __future__ import annotations

import json
from typing import Any, TypedDict

from bathos.schema import Run
from bathos.campaigns import Campaign


class RunDisplay(TypedDict):
    """Display representation of a Run for web rendering."""

    id: str
    id_short: str
    project_slug: str
    status: str
    exit_code: int
    duration_s: float
    duration_display: str
    timestamp: str
    command: str
    argv: list[str]
    hostname: str
    slurm_job_id: str
    git_hash: str
    git_hash_short: str
    git_branch: str
    git_dirty: bool
    script_sha256: str
    sidecar_path: str
    sidecar_mode: str
    agent_mode: str
    parent_run_id: str
    tags: list[str]
    output_paths: list[str]
    outcome: str
    outcome_is_residual: bool
    campaign_id: str
    campaign_name: str
    postmortem_status: str
    postmortem_hypothesis_status: str
    postmortem_verdict_override: str
    postmortem_author: str
    postmortem_summary: str
    postmortem_path: str
    postmortem_has_anomalies: bool
    postmortem_asset_links: dict[str, Any]


class CampaignDisplay(TypedDict):
    """Display representation of a Campaign for web rendering."""

    id: str
    id_short: str
    name: str
    mode: str
    question: str
    hypothesis: str
    status: str
    started_at: str
    concluded_at: str
    conclusion: str
    outcome_label: str
    parent_campaign_id: str
    run_count: int
    outcome_distribution: dict[str, int]
    residual_rate: float
    bypass_rate: float
    unknown_rate: float
    anomalies: list[str]


def project_run(run: Run, campaign_name: str = "") -> RunDisplay:
    """Project a Run to a RunDisplay TypedDict for web rendering.

    Args:
        run: Source Run object
        campaign_name: Optional campaign name for display

    Returns:
        RunDisplay TypedDict with formatted and derived fields
    """
    # Format duration as seconds for sub-60s, minutes+seconds for longer
    if run.duration_s < 60:
        duration_display = f"{run.duration_s:.1f}s"
    else:
        minutes = int(run.duration_s // 60)
        seconds = int(run.duration_s % 60)
        duration_display = f"{minutes}m {seconds}s"

    # Format timestamp
    timestamp_str = run.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")

    # Parse postmortem_asset_links JSON if present
    postmortem_asset_links: dict[str, Any] = {}
    if run.postmortem_asset_links and run.postmortem_asset_links.strip():
        try:
            postmortem_asset_links = json.loads(run.postmortem_asset_links)
        except (json.JSONDecodeError, ValueError):
            postmortem_asset_links = {}

    return RunDisplay(
        id=run.id,
        id_short=run.id[:8],
        project_slug=run.project_slug,
        status=run.status,
        exit_code=run.exit_code,
        duration_s=run.duration_s,
        duration_display=duration_display,
        timestamp=timestamp_str,
        command=run.command,
        argv=run.argv,
        hostname=run.hostname or "",
        slurm_job_id=run.slurm_job_id or "",
        git_hash=run.git_hash,
        git_hash_short=run.git_hash[:8],
        git_branch=run.git_branch,
        git_dirty=run.git_dirty,
        script_sha256=run.script_sha256 or "",
        sidecar_path=run.sidecar_path or "",
        sidecar_mode=run.sidecar_mode or "",
        agent_mode=run.agent_mode or "",
        parent_run_id=run.parent_run_id or "",
        tags=run.tags,
        output_paths=run.output_paths,
        outcome=run.outcome or "",
        outcome_is_residual=run.outcome_is_residual,
        campaign_id=run.campaign_id or "",
        campaign_name=campaign_name,
        postmortem_status=run.postmortem_status,
        postmortem_hypothesis_status=run.postmortem_hypothesis_status,
        postmortem_verdict_override=run.postmortem_verdict_override or "",
        postmortem_author=run.postmortem_author or "",
        postmortem_summary=run.postmortem_summary or "",
        postmortem_path=run.postmortem_path or "",
        postmortem_has_anomalies=run.postmortem_has_anomalies,
        postmortem_asset_links=postmortem_asset_links,
    )


def project_campaign(campaign: Campaign, aggregates: dict) -> CampaignDisplay:
    """Project a Campaign to a CampaignDisplay TypedDict for web rendering.

    Args:
        campaign: Source Campaign object
        aggregates: Dict with keys: run_count, outcome_distribution, residual_rate,
                   bypass_rate, unknown_rate, anomalies

    Returns:
        CampaignDisplay TypedDict with formatted fields and aggregates
    """
    return CampaignDisplay(
        id=campaign.id,
        id_short=campaign.id[:8],
        name=campaign.name,
        mode=campaign.mode,
        question=campaign.question or "",
        hypothesis=campaign.hypothesis or "",
        status=campaign.status,
        started_at=campaign.started_at,
        concluded_at=campaign.concluded_at or "",
        conclusion=campaign.conclusion or "",
        outcome_label=campaign.outcome_label or "",
        parent_campaign_id=campaign.parent_campaign_id or "",
        run_count=aggregates.get("run_count", 0),
        outcome_distribution=aggregates.get("outcome_distribution", {}),
        residual_rate=aggregates.get("residual_rate", 0.0),
        bypass_rate=aggregates.get("bypass_rate", 0.0),
        unknown_rate=aggregates.get("unknown_rate", 0.0),
        anomalies=aggregates.get("anomalies", []),
    )
