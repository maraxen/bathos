from __future__ import annotations

import html
import json
import math
from typing import Any, TypedDict

from bathos.campaigns import Campaign
from bathos.schema import Run


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


class EValuePoint(TypedDict):
    """Single step in a sequential campaign's cumulative E_n trajectory."""

    seq_position: int
    run_id: str
    run_id_short: str
    outcome: str
    evalue: float
    cumulative_en: float


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
    stopping_threshold: float | None
    evalue_trajectory: list[EValuePoint]
    final_en: float | None
    en_chart_svg: str


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


_NEUTRAL_OUTCOMES = frozenset({"error", "unknown", "observed", "invalid_measurement", "invariant_fail"})


def compute_en_trajectory(points: list[dict[str, Any]]) -> list[EValuePoint]:
    """Compute cumulative E_n trajectory points from sequential run records.

    Neutral outcomes (``error``, ``unknown``, ``observed``, ``invalid_measurement``,
    ``invariant_fail``) leave cumulative E_n unchanged (effective multiplier 1.0).
    """
    trajectory: list[EValuePoint] = []
    cumulative = 1.0
    for idx, pt in enumerate(points, start=1):
        raw_e = pt.get("evalue")
        if raw_e is None or isinstance(raw_e, bool):
            continue
        e_val = float(raw_e)
        if not math.isfinite(e_val) or e_val <= 0:
            continue
        outcome = str(pt.get("outcome") or "")
        effective_e = 1.0 if outcome in _NEUTRAL_OUTCOMES else e_val
        cumulative *= effective_e
        run_id = str(pt.get("run_id") or "")
        seq_pos = pt.get("seq_position")
        seq_num = int(seq_pos) if isinstance(seq_pos, int) and not isinstance(seq_pos, bool) else idx
        trajectory.append(
            EValuePoint(
                seq_position=seq_num,
                run_id=run_id,
                run_id_short=run_id[:8],
                outcome=outcome,
                evalue=effective_e,
                cumulative_en=cumulative,
            )
        )
    return trajectory


def _fmt_en(val: float) -> str:
    if val >= 1000 or (0 < val < 0.01):
        return f"{val:.2e}"
    if val >= 10:
        return f"{val:.1f}".rstrip("0").rstrip(".")
    return f"{val:.2f}".rstrip("0").rstrip(".")


def render_en_chart_svg(
    trajectory: list[EValuePoint],
    stopping_threshold: float | None = None,
    *,
    width: int = 560,
    height: int = 200,
) -> str:
    """Render a self-contained inline SVG E_n trajectory chart on a log10 scale.

    Includes horizontal reference lines for the null baseline (E = 1.0) and, when set,
    the campaign's stopping threshold. Returns ``""`` when ``trajectory`` is empty.
    """
    if not trajectory:
        return ""

    pad_left = 56
    pad_right = 24
    pad_top = 20
    pad_bottom = 32
    plot_w = max(width - pad_left - pad_right, 40)
    plot_h = max(height - pad_top - pad_bottom, 40)

    vals = [max(pt["cumulative_en"], 1e-12) for pt in trajectory]
    vals.append(1.0)
    has_threshold = (
        stopping_threshold is not None
        and math.isfinite(stopping_threshold)
        and stopping_threshold > 0
    )
    if has_threshold and stopping_threshold is not None:
        vals.append(stopping_threshold)

    log_vals = [math.log10(v) for v in vals]
    min_log = min(log_vals)
    max_log = max(log_vals)
    if math.isclose(min_log, max_log, abs_tol=1e-9):
        min_log -= 0.5
        max_log += 0.5
    else:
        span = max_log - min_log
        min_log -= span * 0.12
        max_log += span * 0.12

    def _y_for(val: float) -> float:
        lv = math.log10(max(val, 1e-12))
        frac = (lv - min_log) / (max_log - min_log)
        return pad_top + plot_h * (1.0 - frac)

    n_pts = len(trajectory)

    def _x_for(idx: int) -> float:
        if n_pts == 1:
            return pad_left + plot_w / 2.0
        return pad_left + (idx / (n_pts - 1)) * plot_w

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        'role="img" aria-label="E_n sequential trajectory chart" '
        'style="max-width: 100%; height: auto; display: block; background: #ffffff; '
        'border: 1px solid #e5e7eb; border-radius: 0.375rem;">',
        f'<line x1="{pad_left}" y1="{pad_top}" x2="{pad_left}" y2="{pad_top + plot_h}" '
        'stroke="#d1d5db" stroke-width="1"/>',
        f'<line x1="{pad_left}" y1="{pad_top + plot_h}" x2="{pad_left + plot_w}" '
        f'y2="{pad_top + plot_h}" stroke="#d1d5db" stroke-width="1"/>',
    ]

    # Null reference line at E = 1.0
    y_null = _y_for(1.0)
    parts.append(
        f'<line x1="{pad_left}" y1="{y_null:.2f}" x2="{pad_left + plot_w}" y2="{y_null:.2f}" '
        'stroke="#6b7280" stroke-width="1.2" stroke-dasharray="4,4"/>'
    )
    parts.append(
        f'<text x="{pad_left + 4}" y="{max(pad_top + 10, y_null - 4):.2f}" '
        'font-size="10" fill="#4b5563" font-family="monospace">Null (E=1.0)</text>'
    )
    parts.append(
        f'<text x="{pad_left - 6}" y="{y_null + 3:.2f}" text-anchor="end" '
        'font-size="10" fill="#6b7280" font-family="monospace">1</text>'
    )

    # Stopping threshold reference line
    if has_threshold and stopping_threshold is not None:
        y_thr = _y_for(stopping_threshold)
        thr_label = _fmt_en(stopping_threshold)
        parts.append(
            f'<line x1="{pad_left}" y1="{y_thr:.2f}" x2="{pad_left + plot_w}" y2="{y_thr:.2f}" '
            'stroke="#dc2626" stroke-width="1.5" stroke-dasharray="6,3"/>'
        )
        parts.append(
            f'<text x="{pad_left + plot_w - 4}" y="{max(pad_top + 10, y_thr - 4):.2f}" '
            'text-anchor="end" font-size="10" fill="#b91c1c" font-family="monospace">'
            f"Threshold ({html.escape(thr_label)})</text>"
        )
        if abs(y_thr - y_null) > 11:
            parts.append(
                f'<text x="{pad_left - 6}" y="{y_thr + 3:.2f}" text-anchor="end" '
                f'font-size="10" fill="#b91c1c" font-family="monospace">{html.escape(thr_label)}</text>'
            )

    # Trajectory polyline
    coords = [(_x_for(i), _y_for(pt["cumulative_en"])) for i, pt in enumerate(trajectory)]
    if len(coords) >= 2:
        pts_attr = " ".join(f"{x:.2f},{y:.2f}" for x, y in coords)
        parts.append(
            f'<polyline fill="none" stroke="#2563eb" stroke-width="2" points="{pts_attr}"/>'
        )

    # Data points with hover tooltips and x-axis step labels
    label_step = max(1, math.ceil(n_pts / 10))
    for i, (pt, (cx, cy)) in enumerate(zip(trajectory, coords, strict=True)):
        met = (
            has_threshold
            and stopping_threshold is not None
            and pt["cumulative_en"] >= stopping_threshold
        )
        fill = "#059669" if met else "#2563eb"
        tooltip = (
            f"n={pt['seq_position']} {pt['run_id_short']} ({pt['outcome'] or '—'}): "
            f"e={_fmt_en(pt['evalue'])}, E_n={_fmt_en(pt['cumulative_en'])}"
        )
        parts.append(
            f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="3.5" fill="{fill}" stroke="#ffffff" stroke-width="1">'
            f"<title>{html.escape(tooltip)}</title></circle>"
        )
        if i == 0 or i == n_pts - 1 or (i % label_step == 0):
            parts.append(
                f'<text x="{cx:.2f}" y="{pad_top + plot_h + 15}" text-anchor="middle" '
                f'font-size="10" fill="#6b7280" font-family="monospace">{pt["seq_position"]}</text>'
            )

    parts.append(
        f'<text x="{pad_left + plot_w / 2:.1f}" y="{height - 4}" text-anchor="middle" '
        'font-size="10" fill="#6b7280">Sequential step (n)</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts)


def project_campaign(campaign: Campaign, aggregates: dict) -> CampaignDisplay:
    """Project a Campaign to a CampaignDisplay TypedDict for web rendering.

    Args:
        campaign: Source Campaign object
        aggregates: Dict with keys: run_count, outcome_distribution, residual_rate,
                   bypass_rate, unknown_rate, anomalies, and optional stopping_threshold,
                   evalue_trajectory

    Returns:
        CampaignDisplay TypedDict with formatted fields and aggregates
    """
    stopping_threshold = aggregates.get("stopping_threshold", campaign.stopping_threshold)
    raw_trajectory = aggregates.get("evalue_trajectory") or []
    if raw_trajectory and all("cumulative_en" in pt for pt in raw_trajectory):
        trajectory: list[EValuePoint] = [
            EValuePoint(
                seq_position=int(pt["seq_position"]),
                run_id=str(pt.get("run_id") or ""),
                run_id_short=str(pt.get("run_id_short") or str(pt.get("run_id") or "")[:8]),
                outcome=str(pt.get("outcome") or ""),
                evalue=float(pt["evalue"]),
                cumulative_en=float(pt["cumulative_en"]),
            )
            for pt in raw_trajectory
        ]
    else:
        trajectory = compute_en_trajectory(raw_trajectory)

    final_en = trajectory[-1]["cumulative_en"] if trajectory else None
    en_chart_svg = render_en_chart_svg(trajectory, stopping_threshold=stopping_threshold)

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
        stopping_threshold=stopping_threshold,
        evalue_trajectory=trajectory,
        final_en=final_en,
        en_chart_svg=en_chart_svg,
    )
