from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, UTC
from uuid import uuid4

import duckdb


class CampaignError(Exception):
    pass


@dataclass
class Campaign:
    id: str
    project_slug: str
    name: str
    mode: str  # "exploration" | "confirmation"
    question: str | None = None
    hypothesis: str | None = None
    status: str = "open"
    started_at: str = ""
    concluded_at: str | None = None
    conclusion: str | None = None
    outcome_label: str | None = None
    parent_campaign_id: str | None = None


def _open_db(catalog_dir) -> duckdb.DuckDBPyConnection:
    from pathlib import Path
    return duckdb.connect(str(Path(catalog_dir) / "bathos.db"))


def create_campaign(db, name: str, project_slug: str, mode: str, question: str | None = None, hypothesis: str | None = None, parent_campaign_id: str | None = None) -> Campaign:
    if mode not in ("exploration", "confirmation"):
        raise CampaignError(f"mode must be 'exploration' or 'confirmation', got {mode!r}")
    campaign_id = str(uuid4())
    started_at = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO campaigns (id, project_slug, name, mode, question, hypothesis, status, started_at, parent_campaign_id) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)",
        [campaign_id, project_slug, name, mode, question, hypothesis, started_at, parent_campaign_id]
    )
    return Campaign(id=campaign_id, project_slug=project_slug, name=name, mode=mode, question=question, hypothesis=hypothesis, status="open", started_at=started_at, parent_campaign_id=parent_campaign_id)


def add_run_to_campaign(db, campaign_id: str, run_id: str) -> None:
    """Add run to campaign (idempotent). Enforces temporal ordering for confirmation campaigns."""
    campaign_rows = db.execute("SELECT mode, started_at FROM campaigns WHERE id = ?", [campaign_id]).fetchall()
    if not campaign_rows:
        raise CampaignError(f"Campaign not found: {campaign_id}")
    campaign_mode, campaign_started_at = campaign_rows[0]

    run_rows = db.execute("SELECT timestamp FROM runs WHERE id = ?", [run_id]).fetchall()
    if not run_rows:
        raise CampaignError(f"Run not found: {run_id}")
    run_timestamp = run_rows[0][0]

    # Enforce temporal ordering for confirmation campaigns
    if campaign_mode == "confirmation":
        # Parse both as strings for comparison (both are ISO strings)
        run_ts_str = run_timestamp.isoformat() if hasattr(run_timestamp, "isoformat") else str(run_timestamp)
        if run_ts_str < campaign_started_at:
            raise CampaignError(
                f"Cannot add run {run_id} to confirmation campaign {campaign_id}: "
                f"run timestamp ({run_ts_str}) predates campaign creation ({campaign_started_at})"
            )

    db.execute(
        "INSERT INTO campaign_runs (campaign_id, run_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
        [campaign_id, run_id]
    )


def conclude_campaign(db, campaign_id: str, outcome_label: str, conclusion: str) -> None:
    """Mark campaign as concluded."""
    rows = db.execute("SELECT id FROM campaigns WHERE id = ?", [campaign_id]).fetchall()
    if not rows:
        raise CampaignError(f"Campaign not found: {campaign_id}")
    concluded_at = datetime.now(UTC).isoformat()
    db.execute(
        "UPDATE campaigns SET status = 'concluded', concluded_at = ?, outcome_label = ?, conclusion = ? WHERE id = ?",
        [concluded_at, outcome_label, conclusion, campaign_id]
    )


def get_campaign(db, campaign_id: str) -> Campaign | None:
    """Fetch campaign by ID."""
    rows = db.execute("SELECT id, project_slug, name, mode, question, hypothesis, status, started_at, concluded_at, conclusion, outcome_label, parent_campaign_id FROM campaigns WHERE id = ?", [campaign_id]).fetchall()
    if not rows:
        return None
    r = rows[0]
    return Campaign(id=r[0], project_slug=r[1], name=r[2], mode=r[3], question=r[4], hypothesis=r[5], status=r[6], started_at=r[7], concluded_at=r[8], conclusion=r[9], outcome_label=r[10], parent_campaign_id=r[11])


def list_campaigns(db, project_slug: str | None = None, status: str | None = None) -> list[Campaign]:
    """List campaigns with optional filters."""
    query = "SELECT id, project_slug, name, mode, question, hypothesis, status, started_at, concluded_at, conclusion, outcome_label, parent_campaign_id FROM campaigns WHERE 1=1"
    params = []
    if project_slug:
        query += " AND project_slug = ?"
        params.append(project_slug)
    if status:
        query += " AND status = ?"
        params.append(status)
    rows = db.execute(query, params).fetchall()
    return [Campaign(id=r[0], project_slug=r[1], name=r[2], mode=r[3], question=r[4], hypothesis=r[5], status=r[6], started_at=r[7], concluded_at=r[8], conclusion=r[9], outcome_label=r[10], parent_campaign_id=r[11]) for r in rows]


def review_campaign(db, campaign_id: str) -> dict:
    """Generate campaign review: residual rate, bypass rate, outcome distribution, anomalies."""
    rows = db.execute("""
        SELECT r.id, r.sidecar_mode, r.outcome, r.outcome_is_residual
        FROM campaign_runs cr
        INNER JOIN runs r ON cr.run_id = r.id
        WHERE cr.campaign_id = ?
    """, [campaign_id]).fetchall()

    if not rows:
        return {"error": f"Campaign {campaign_id} not found or has no runs"}

    total = len(rows)
    residual_count = sum(1 for r in rows if r[3])
    bypassed_count = sum(1 for r in rows if r[1] == "bypassed")
    unknown_count = sum(1 for r in rows if r[2] in ("unknown", ""))

    outcome_dist = {}
    for r in rows:
        outcome_dist[r[2] or "unknown"] = outcome_dist.get(r[2] or "unknown", 0) + 1

    anomalies = []
    residual_rate = residual_count / total
    bypass_rate = bypassed_count / total
    unknown_rate = unknown_count / total
    if residual_rate > 0.10:
        anomalies.append(f"High residual rate: {residual_rate:.1%} ({residual_count}/{total} runs)")
    if bypass_rate > 0.10:
        anomalies.append(f"High bypass rate: {bypass_rate:.1%} ({bypassed_count}/{total} runs)")
    if unknown_count > 0:
        anomalies.append(f"{unknown_count} runs with unknown outcome")

    return {
        "total_runs": total,
        "residual_rate": residual_rate,
        "bypass_rate": bypass_rate,
        "unknown_rate": unknown_rate,
        "outcome_distribution": outcome_dist,
        "anomalies": anomalies,
    }
