from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, UTC
from uuid import uuid4

import duckdb

from bathos.sidecar import compute_evalue
from bathos.telemetry import event


class CampaignError(Exception):
    pass


@dataclass
class Campaign:
    id: str
    project_slug: str
    name: str
    mode: str  # "exploration" | "confirmation" | "sequential"
    question: str | None = None
    hypothesis: str | None = None
    status: str = "open"
    started_at: str = ""
    concluded_at: str | None = None
    conclusion: str | None = None
    outcome_label: str | None = None
    parent_campaign_id: str | None = None
    stopping_threshold: float | None = None


def _open_db(catalog_dir) -> duckdb.DuckDBPyConnection:
    from pathlib import Path
    return duckdb.connect(str(Path(catalog_dir) / "bathos.db"))


def create_campaign(db, name: str, project_slug: str, mode: str, question: str | None = None, hypothesis: str | None = None, parent_campaign_id: str | None = None) -> Campaign:
    if mode not in ("exploration", "confirmation", "sequential"):
        raise CampaignError(f"mode must be 'exploration', 'confirmation', or 'sequential', got {mode!r}")
    campaign_id = str(uuid4())
    started_at = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO campaigns (id, project_slug, name, mode, question, hypothesis, status, started_at, parent_campaign_id) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)",
        [campaign_id, project_slug, name, mode, question, hypothesis, started_at, parent_campaign_id]
    )
    # Use campaign_name field since 'name' is reserved by logging.LogRecord
    event("campaign.create", campaign_id=campaign_id, campaign_name=name)
    return Campaign(id=campaign_id, project_slug=project_slug, name=name, mode=mode, question=question, hypothesis=hypothesis, status="open", started_at=started_at, parent_campaign_id=parent_campaign_id)


def add_run_to_campaign(db, campaign_id: str, run_id: str) -> None:
    """Add run to campaign (idempotent). For sequential campaigns, computes e-value and applies threshold lock."""
    campaign_rows = db.execute(
        "SELECT mode, started_at, stopping_threshold FROM campaigns WHERE id = ?",
        [campaign_id]
    ).fetchall()
    if not campaign_rows:
        raise CampaignError(f"Campaign not found: {campaign_id}")
    campaign_mode, campaign_started_at, campaign_threshold = campaign_rows[0]

    run_rows = db.execute(
        "SELECT timestamp, outcome, sidecar_path FROM runs WHERE id = ?",
        [run_id]
    ).fetchall()
    if not run_rows:
        raise CampaignError(f"Run not found: {run_id}")
    run_timestamp, run_outcome, run_sidecar_path = run_rows[0]

    # Enforce temporal ordering for confirmation campaigns
    if campaign_mode == "confirmation":
        try:
            campaign_dt = datetime.fromisoformat(campaign_started_at)
            if campaign_dt.tzinfo is None:
                campaign_dt = campaign_dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            campaign_dt = None

        run_dt = run_timestamp if isinstance(run_timestamp, datetime) else None
        if run_dt is not None and run_dt.tzinfo is None:
            run_dt = run_dt.replace(tzinfo=UTC)

        if campaign_dt is not None and run_dt is not None:
            if run_dt < campaign_dt:
                raise CampaignError(
                    f"Cannot add run {run_id} to confirmation campaign {campaign_id}: "
                    f"run timestamp ({run_dt.isoformat()}) predates campaign creation ({campaign_dt.isoformat()})"
                )

    if campaign_mode == "sequential":
        # Compute e-value from sidecar
        evalue = 1.0
        sidecar_stopping_threshold = None
        if run_sidecar_path:
            from pathlib import Path
            from bathos.sidecar import parse_sidecar, SidecarError
            try:
                sidecar_path_obj = Path(run_sidecar_path)
                if sidecar_path_obj.exists():
                    sidecar = parse_sidecar(sidecar_path_obj)
                    evalue = compute_evalue(sidecar, run_outcome or "unknown")
                    sidecar_stopping_threshold = sidecar.popper_stopping_threshold
            except SidecarError:
                evalue = 1.0

        # Assign seq_position (1-based, monotonically increasing per campaign)
        pos_row = db.execute(
            "SELECT COALESCE(MAX(seq_position), 0) + 1 FROM campaign_runs WHERE campaign_id = ?",
            [campaign_id]
        ).fetchone()
        seq_position = pos_row[0] if pos_row else 1

        # Threshold lock logic (only locks for non-error/non-unknown outcomes)
        is_neutral_outcome = run_outcome in ("error", "unknown", None, "")
        if not is_neutral_outcome:
            if campaign_threshold is None and sidecar_stopping_threshold is not None:
                # Lock threshold from this sidecar
                db.execute(
                    "UPDATE campaigns SET stopping_threshold = ? WHERE id = ?",
                    [sidecar_stopping_threshold, campaign_id]
                )
                campaign_threshold = sidecar_stopping_threshold
            elif campaign_threshold is not None and sidecar_stopping_threshold is not None:
                if sidecar_stopping_threshold != campaign_threshold:
                    n_runs = db.execute(
                        "SELECT COUNT(*) FROM campaign_runs WHERE campaign_id = ? AND seq_position IS NOT NULL",
                        [campaign_id]
                    ).fetchone()[0]
                    raise CampaignError(
                        f"Cannot change stopping_threshold for campaign {campaign_id[:8]}: "
                        f"{n_runs} non-error run(s) already added (threshold locked at {campaign_threshold}). "
                        f"To use a different threshold, create a new campaign with "
                        f"--parent {campaign_id[:8]} to preserve lineage."
                    )

        db.execute(
            "INSERT INTO campaign_runs (campaign_id, run_id, evalue, seq_position) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
            [campaign_id, run_id, evalue, seq_position]
        )
    else:
        db.execute(
            "INSERT INTO campaign_runs (campaign_id, run_id, evalue, seq_position) VALUES (?, ?, NULL, NULL) ON CONFLICT DO NOTHING",
            [campaign_id, run_id]
        )


def _campaign_threshold_met(db, campaign_id: str, stopping_threshold: float) -> bool:
    """Return True if all scripts in the campaign have E_n >= stopping_threshold."""
    rows = db.execute("""
        SELECT EXP(SUM(LN(cr.evalue)) FILTER (WHERE r.outcome != 'error' AND r.outcome != 'unknown'))
        FROM campaign_runs cr
        INNER JOIN runs r ON cr.run_id = r.id
        WHERE cr.campaign_id = ?
        GROUP BY COALESCE(NULLIF(r.script_sha256, ''), r.sidecar_path, '_ungrouped')
    """, [campaign_id]).fetchall()
    if not rows:
        return False
    return all((row[0] is not None and row[0] >= stopping_threshold) for row in rows)


def _resolve_campaign_id(db, campaign_id: str) -> str:
    """Resolve a full or short (prefix) campaign ID to a full UUID.

    Tries exact match first; falls back to prefix match. Raises CampaignError
    if no match or if the prefix is ambiguous (matches multiple campaigns).
    """
    rows = db.execute("SELECT id FROM campaigns WHERE id = ?", [campaign_id]).fetchall()
    if rows:
        return rows[0][0]
    prefix_rows = db.execute("SELECT id FROM campaigns WHERE id LIKE ?", [campaign_id + "%"]).fetchall()
    if not prefix_rows:
        raise CampaignError(f"Campaign not found: {campaign_id}")
    if len(prefix_rows) > 1:
        matches = ", ".join(r[0][:8] for r in prefix_rows)
        raise CampaignError(f"Ambiguous campaign ID prefix {campaign_id!r} matches: {matches}")
    return prefix_rows[0][0]


def conclude_campaign(db, campaign_id: str, outcome_label: str, conclusion: str) -> None:
    """Mark campaign as concluded."""
    full_id = _resolve_campaign_id(db, campaign_id)
    concluded_at = datetime.now(UTC).isoformat()
    db.execute(
        "UPDATE campaigns SET status = 'concluded', concluded_at = ?, outcome_label = ?, conclusion = ? WHERE id = ?",
        [concluded_at, outcome_label, conclusion, full_id]
    )
    event("campaign.conclude", campaign_id=full_id, verdict=outcome_label)


def get_campaign(db, campaign_id: str) -> Campaign | None:
    """Fetch campaign by ID."""
    try:
        full_id = _resolve_campaign_id(db, campaign_id)
    except CampaignError:
        return None
    rows = db.execute("SELECT id, project_slug, name, mode, question, hypothesis, status, started_at, concluded_at, conclusion, outcome_label, parent_campaign_id, stopping_threshold FROM campaigns WHERE id = ?", [full_id]).fetchall()
    if not rows:
        return None
    r = rows[0]
    return Campaign(id=r[0], project_slug=r[1], name=r[2], mode=r[3], question=r[4], hypothesis=r[5], status=r[6], started_at=r[7], concluded_at=r[8], conclusion=r[9], outcome_label=r[10], parent_campaign_id=r[11], stopping_threshold=r[12])


def list_campaigns(db, project_slug: str | None = None, status: str | None = None) -> list[Campaign]:
    """List campaigns with optional filters."""
    query = "SELECT id, project_slug, name, mode, question, hypothesis, status, started_at, concluded_at, conclusion, outcome_label, parent_campaign_id, stopping_threshold FROM campaigns WHERE 1=1"
    params = []
    if project_slug:
        query += " AND project_slug = ?"
        params.append(project_slug)
    if status:
        query += " AND status = ?"
        params.append(status)
    rows = db.execute(query, params).fetchall()
    return [Campaign(id=r[0], project_slug=r[1], name=r[2], mode=r[3], question=r[4], hypothesis=r[5], status=r[6], started_at=r[7], concluded_at=r[8], conclusion=r[9], outcome_label=r[10], parent_campaign_id=r[11], stopping_threshold=r[12]) for r in rows]


def review_campaign(db, campaign_id: str) -> dict:
    """Generate campaign review: residual rate, bypass rate, outcome distribution, anomalies, and POPPER summary."""
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

    # POPPER sequential test summary
    popper_data = None
    campaign_meta = db.execute(
        "SELECT mode, stopping_threshold FROM campaigns WHERE id = ?",
        [campaign_id]
    ).fetchone()
    if campaign_meta and campaign_meta[0] == "sequential":
        stopping_threshold = campaign_meta[1]
        script_rows = db.execute("""
            SELECT
                COALESCE(NULLIF(r.script_sha256, ''), r.sidecar_path, '_ungrouped') AS script_key,
                COUNT(*) FILTER (WHERE r.outcome != 'error' AND r.outcome != 'unknown') AS n_effective,
                COUNT(*) FILTER (WHERE r.outcome = 'error' OR r.outcome = 'unknown') AS n_excluded,
                EXP(SUM(LN(cr.evalue)) FILTER (WHERE r.outcome != 'error' AND r.outcome != 'unknown')) AS evalue_product
            FROM campaign_runs cr
            INNER JOIN runs r ON cr.run_id = r.id
            WHERE cr.campaign_id = ? AND cr.evalue IS NOT NULL
            GROUP BY script_key
            ORDER BY script_key
        """, [campaign_id]).fetchall()

        scripts = []
        for sr in script_rows:
            ep = sr[3] if sr[3] is not None else 1.0
            met = (stopping_threshold is not None and ep >= stopping_threshold)
            scripts.append({
                "script_key": sr[0],
                "n_effective": sr[1],
                "n_excluded": sr[2],
                "evalue_product": ep,
                "threshold_met": met,
            })

        threshold_met = (
            len(scripts) > 0
            and stopping_threshold is not None
            and all(s["threshold_met"] for s in scripts)
        )
        popper_data = {
            "mode": "sequential",
            "stopping_threshold": stopping_threshold,
            "threshold_met": threshold_met,
            "scripts": scripts,
        }

    return {
        "total_runs": total,
        "residual_rate": residual_rate,
        "bypass_rate": bypass_rate,
        "unknown_rate": unknown_rate,
        "outcome_distribution": outcome_dist,
        "anomalies": anomalies,
        "popper": popper_data,
    }


def emit_campaign_report(db, catalog_dir: str, campaign_id: str, figure_manifest_ref: str | None = None) -> None:
    """Emit a campaign report JSON sidecar at <catalog>/sidecars/<campaign_id>/campaign_report.json.

    This function generates a truth-only report capturing summary stats from the campaign,
    closing the recon gap where campaign_review renders stats to console and discards them.

    Args:
        db: DuckDB connection.
        catalog_dir: Path to the bathos catalog root (where sidecars/ lives).
        campaign_id: Campaign ID to generate the report for.
        figure_manifest_ref: Optional path reference to the figure manifest
            (e.g., "sidecars/<campaign_id>/figure_manifest.json").

    Raises:
        CampaignError: If campaign not found or has no runs.
    """
    from pathlib import Path

    from bathos.campaign_report import CampaignReport

    # Fetch campaign metadata
    campaign_rows = db.execute(
        "SELECT conclusion FROM campaigns WHERE id = ?",
        [campaign_id]
    ).fetchall()
    if not campaign_rows:
        raise CampaignError(f"Campaign {campaign_id} not found")

    campaign_conclusion = campaign_rows[0][0]

    # Check if campaign has any runs
    run_count = db.execute(
        "SELECT COUNT(*) FROM campaign_runs WHERE campaign_id = ?",
        [campaign_id]
    ).fetchone()[0]

    # Handle zero-run campaign: emit a valid report with defaults
    if run_count == 0:
        review_data = {
            "total_runs": 0,
            "residual_rate": 0.0,
            "bypass_rate": 0.0,
            "unknown_rate": 0.0,
            "outcome_distribution": {},
            "anomalies": [],
            "popper": None,
        }
        stage_breakdown = {}
    else:
        # Generate the review stats (includes total_runs, residual_rate, etc.)
        review_data = review_campaign(db, campaign_id)

        # Build stage_breakdown: count runs by stage_name with None as explicit bucket
        stage_rows = db.execute("""
            SELECT COALESCE(NULLIF(r.stage_name, ''), NULL) AS stage_key, COUNT(*) AS count
            FROM campaign_runs cr
            INNER JOIN runs r ON cr.run_id = r.id
            WHERE cr.campaign_id = ?
            GROUP BY stage_key
        """, [campaign_id]).fetchall()

        stage_breakdown = {}
        for stage_key, count in stage_rows:
            # Use None as the key for null/empty stage_name (explicit bucket)
            stage_breakdown[stage_key] = count

    # Create the campaign report
    report = CampaignReport(
        report_version="1.0",
        campaign_id=campaign_id,
        total_runs=review_data["total_runs"],
        residual_rate=review_data["residual_rate"],
        bypass_rate=review_data["bypass_rate"],
        unknown_rate=review_data["unknown_rate"],
        outcome_distribution=review_data["outcome_distribution"],
        anomalies=review_data["anomalies"],
        popper=review_data["popper"],
        conclude=campaign_conclusion,
        figure_manifest_ref=figure_manifest_ref,
        stage_breakdown=stage_breakdown,
    )

    # Write the report to the sidecar path
    sidecar_dir = Path(catalog_dir) / "sidecars" / campaign_id
    report_path = sidecar_dir / "campaign_report.json"
    report.write_report(report_path)

    event("campaign.report.emit", campaign_id=campaign_id, report_path=str(report_path))


def emit_figure_manifest(db, catalog_dir: str, campaign_id: str) -> None:
    """Emit an empty figure manifest JSON sidecar at <catalog>/sidecars/<campaign_id>/figure_manifest.json.

    This function generates a truth-only figure manifest that declares figure INTENT
    (which runs/data a figure derives from) without rendering artifacts. Rendering remains
    maraxiom's concern.

    For now, bathos emits an empty manifest (zero figures) since all rendering is delegated
    to maraxiom. The manifest structure is prepared for future figure pinning if needed.

    Args:
        db: DuckDB connection.
        catalog_dir: Path to the bathos catalog root (where sidecars/ lives).
        campaign_id: Campaign ID to generate the manifest for.

    Raises:
        CampaignError: If campaign not found.
    """
    from pathlib import Path

    from bathos.figure_manifest import FigureManifest

    # Verify campaign exists
    campaign_rows = db.execute(
        "SELECT id FROM campaigns WHERE id = ?",
        [campaign_id]
    ).fetchall()
    if not campaign_rows:
        raise CampaignError(f"Campaign {campaign_id} not found")

    # Create an empty figure manifest (bathos truth-only: no rendering)
    manifest = FigureManifest(
        manifest_version="1.0",
        campaign_id=campaign_id,
        figures=[],  # Empty: all rendering delegated to maraxiom
    )

    # Write the manifest to the sidecar path
    sidecar_dir = Path(catalog_dir) / "sidecars" / campaign_id
    manifest_path = sidecar_dir / "figure_manifest.json"
    manifest.write_manifest(manifest_path)

    event("campaign.manifest.emit", campaign_id=campaign_id, manifest_path=str(manifest_path))
