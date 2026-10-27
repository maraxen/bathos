"""Cool and Warm Tier Storage Integration for Step-Back Reviews and Escalations (spec 261027).

Provides:
- Atomic JSON serialization to cool-tier paths:
  - `<catalog_dir>/stepback/reviews/<campaign_id>/review_<id>.json`
  - `<catalog_dir>/stepback/reviews/<campaign_id>_<id>.json`
  - `<workspace_root>/.bth/stepback/reviews/<campaign_id>/review_<id>.json`
  - `<catalog_dir>/stepback/escalations/<campaign_id>_<escalation_id>.json`
- DuckDB relational projections (CREATE TABLE IF NOT EXISTS stepback_reviews/stepback_escalations)
- Compaction fragment ingestion with idempotent upserts
- Escalation state reconstruction and querying
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import duckdb

from bathos.stepback.schema import (
    EscalationRecord,
    EscalationState,
    StepBackReview,
)
from bathos.telemetry import event

logger = logging.getLogger(__name__)

STEPBACK_REVIEWS_SCHEMA = """
CREATE TABLE IF NOT EXISTS stepback_reviews (
    id VARCHAR PRIMARY KEY,
    campaign_id VARCHAR NOT NULL,
    project_slug VARCHAR NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    dispatched_at TIMESTAMP WITH TIME ZONE,
    reviewer_id VARCHAR NOT NULL,
    reviewer_kind VARCHAR NOT NULL,
    is_independent BOOLEAN NOT NULL,
    path_kind VARCHAR NOT NULL,
    input_snapshot_sha256 VARCHAR NOT NULL,
    input_snapshot TEXT NOT NULL,
    rubric_id VARCHAR,
    rubric_version VARCHAR,
    rubric_sha256 VARCHAR,
    scores_json TEXT,
    composite_score DOUBLE,
    open_analysis_mode VARCHAR,
    open_analysis_json TEXT,
    cited_evidence_json TEXT NOT NULL,
    recommendation VARCHAR NOT NULL,
    escalation_level INTEGER NOT NULL,
    is_dissent BOOLEAN NOT NULL DEFAULT FALSE,
    parent_panel_id VARCHAR,
    dissents_json TEXT,
    metadata TEXT
);
"""

STEPBACK_ESCALATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS stepback_escalations (
    id VARCHAR PRIMARY KEY,
    campaign_id VARCHAR NOT NULL,
    project_slug VARCHAR NOT NULL,
    level INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    opened_at TIMESTAMP WITH TIME ZONE NOT NULL,
    opened_by_kind VARCHAR NOT NULL,
    opened_by_review_id VARCHAR,
    trigger_name VARCHAR,
    trigger_reason VARCHAR NOT NULL,
    resolved_at TIMESTAMP WITH TIME ZONE,
    resolved_by VARCHAR,
    resolved_kind VARCHAR,
    resolution_note TEXT,
    deescalated_to INTEGER,
    review_epoch INTEGER NOT NULL DEFAULT 0,
    pivot_anchor_run_id VARCHAR,
    metadata TEXT
);
"""


def _atomic_write_json(target_path: Path, data: dict[str, Any]) -> None:
    """Write data to target_path atomically using tempfile and os.replace."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=target_path.parent, delete=False, suffix=".tmp") as tf:
        json.dump(data, tf, indent=2, sort_keys=True)
        temp_name = tf.name
    os.replace(temp_name, target_path)


def save_review_cool(
    review: StepBackReview,
    catalog_dir: Path,
    workspace_root: Path | None = None,
) -> Path:
    """Persist review record atomically to catalog cool tier.

    Writes to:
    - <catalog_dir>/stepback/reviews/<campaign_id>/review_<review_id>.json
    - <catalog_dir>/stepback/reviews/<campaign_id>_<review_id>.json (flat alias)
    - Optionally <workspace_root>/.bth/stepback/reviews/<campaign_id>/review_<review_id>.json
    """
    reviews_dir = catalog_dir / "stepback" / "reviews"
    campaign_dir = reviews_dir / review.campaign_id
    nested_path = campaign_dir / f"review_{review.review_id}.json"
    flat_path = reviews_dir / f"{review.campaign_id}_{review.review_id}.json"

    data = review.to_dict()
    _atomic_write_json(nested_path, data)
    _atomic_write_json(flat_path, data)

    if workspace_root is not None:
        ws_path = (
            workspace_root
            / ".bth"
            / "stepback"
            / "reviews"
            / review.campaign_id
            / f"review_{review.review_id}.json"
        )
        try:
            _atomic_write_json(ws_path, data)
        except Exception as e:
            logger.warning(f"Could not mirror review to workspace: {e}")

    event(
        "stepback.review_recorded",
        review_id=review.review_id,
        campaign_id=review.campaign_id,
        project_slug=review.project_slug,
        reviewer_id=review.reviewer_id,
        reviewer_kind=review.reviewer_kind,
        is_independent=review.is_independent,
        path_kind=review.path_kind,
        recommendation=review.recommendation,
        escalation_level=review.escalation_level,
        composite_score=review.composite_score,
        has_dissent=bool(review.dissents),
        cited_evidence_count=len(review.cited_evidence),
    )

    return nested_path


def load_review_cool(path: Path) -> StepBackReview:
    """Load a review record from a cool JSON file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return StepBackReview.from_dict(data)


def save_escalation_cool(
    esc: EscalationRecord,
    catalog_dir: Path,
) -> Path:
    """Persist escalation record atomically to catalog cool tier.

    Writes to:
    - <catalog_dir>/stepback/escalations/<campaign_id>_<escalation_id>.json
    """
    esc_dir = catalog_dir / "stepback" / "escalations"
    target_path = esc_dir / f"{esc.campaign_id}_{esc.escalation_id}.json"
    _atomic_write_json(target_path, esc.to_dict())

    event(
        "stepback.escalation_created",
        escalation_id=esc.escalation_id,
        campaign_id=esc.campaign_id,
        project_slug=esc.project_slug,
        level=esc.level,
        status=esc.status,
        opened_by_kind=esc.opened_by_kind,
        opened_by_review_id=esc.opened_by_review_id,
        trigger_name=esc.trigger_name,
        trigger_reason=esc.trigger_reason,
    )

    return target_path


def load_escalation_cool(path: Path) -> EscalationRecord:
    """Load an escalation record from a cool JSON file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return EscalationRecord.from_dict(data)


def iter_cool_reviews(catalog_dir: Path, workspace_root: Path | None = None) -> list[Path]:
    """Find all cool-tier review JSON files."""
    paths: list[Path] = []
    seen_ids: set[str] = set()

    search_dirs: list[Path] = []
    if catalog_dir.exists():
        search_dirs.append(catalog_dir / "stepback" / "reviews")
    if workspace_root is not None and workspace_root.exists():
        search_dirs.append(workspace_root / ".bth" / "stepback" / "reviews")

    for sdir in search_dirs:
        if not sdir.exists():
            continue
        for p in sdir.rglob("*.json"):
            if not p.is_file():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                rid = data.get("review_id")
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    paths.append(p)
            except Exception:
                continue
    return sorted(paths)


def iter_cool_escalations(catalog_dir: Path) -> list[Path]:
    """Find all cool-tier escalation JSON files."""
    esc_dir = catalog_dir / "stepback" / "escalations"
    if not esc_dir.exists():
        return []
    paths: list[Path] = []
    seen_ids: set[str] = set()
    for p in esc_dir.glob("*.json"):
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            eid = data.get("escalation_id")
            if eid and eid not in seen_ids:
                seen_ids.add(eid)
                paths.append(p)
        except Exception:
            continue
    return sorted(paths)


def list_reviews(
    campaign_id: str | None = None,
    catalog_dir: Path | None = None,
    workspace_root: Path | None = None,
    db: duckdb.DuckDBPyConnection | None = None,
) -> list[StepBackReview]:
    """List reviews optionally filtered by campaign_id."""
    reviews: list[StepBackReview] = []

    # Try warm DB first if available
    if db is not None:
        try:
            query = "SELECT * FROM stepback_reviews"
            params: list[Any] = []
            if campaign_id:
                query += " WHERE campaign_id = ? OR campaign_id LIKE ?"
                params.extend([campaign_id, f"{campaign_id}%"])
            query += " ORDER BY created_at ASC"
            cursor = db.execute(query, params)
            col_names = [desc[0] for desc in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(col_names, row, strict=False))
                # Reconstruct review
                rev_dict: dict[str, Any] = {
                    "review_id": row_dict["id"],
                    "campaign_id": row_dict["campaign_id"],
                    "project_slug": row_dict["project_slug"],
                    "created_at": str(row_dict["created_at"]),
                    "dispatched_at": str(row_dict["dispatched_at"]) if row_dict.get("dispatched_at") else None,
                    "reviewer_id": row_dict["reviewer_id"],
                    "reviewer_kind": row_dict["reviewer_kind"],
                    "is_independent": bool(row_dict["is_independent"]),
                    "path_kind": row_dict["path_kind"],
                    "input_snapshot_sha256": row_dict["input_snapshot_sha256"],
                    "input_snapshot": row_dict["input_snapshot"],
                    "rubric_id": row_dict.get("rubric_id"),
                    "rubric_version": row_dict.get("rubric_version"),
                    "rubric_sha256": row_dict.get("rubric_sha256"),
                    "composite_score": row_dict.get("composite_score"),
                    "recommendation": row_dict["recommendation"],
                    "escalation_level": int(row_dict["escalation_level"]),
                    "cited_evidence": json.loads(row_dict.get("cited_evidence_json") or "[]"),
                    "rubric_scores": json.loads(row_dict.get("scores_json") or "{}"),
                    "dissents": json.loads(row_dict.get("dissents_json") or "[]"),
                    "metadata": json.loads(row_dict.get("metadata") or "{}"),
                }
                if row_dict.get("open_analysis_mode") and row_dict.get("open_analysis_json"):
                    rev_dict["open_analysis"] = {
                        "mode": row_dict["open_analysis_mode"],
                        "sections": json.loads(row_dict["open_analysis_json"]),
                    }
                reviews.append(StepBackReview.from_dict(rev_dict))
            if reviews:
                return reviews
        except Exception:
            pass

    # Cool tier fallback
    if catalog_dir is not None:
        file_paths = iter_cool_reviews(catalog_dir, workspace_root=workspace_root)
        for p in file_paths:
            try:
                rev = load_review_cool(p)
                if campaign_id is None or rev.campaign_id == campaign_id or rev.campaign_id.startswith(campaign_id):
                    reviews.append(rev)
            except Exception:
                continue

    reviews.sort(key=lambda r: r.created_at)
    return reviews


def get_review(
    review_id: str,
    catalog_dir: Path | None = None,
    workspace_root: Path | None = None,
    db: duckdb.DuckDBPyConnection | None = None,
) -> StepBackReview | None:
    """Retrieve review by full ID or prefix."""
    reviews = list_reviews(catalog_dir=catalog_dir, workspace_root=workspace_root, db=db)
    for r in reviews:
        if r.review_id == review_id or r.review_id.startswith(review_id):
            return r
    return None


def list_escalations(
    campaign_id: str | None = None,
    catalog_dir: Path | None = None,
    db: duckdb.DuckDBPyConnection | None = None,
) -> list[EscalationRecord]:
    """List escalation events optionally filtered by campaign_id."""
    records: list[EscalationRecord] = []

    if db is not None:
        try:
            query = "SELECT * FROM stepback_escalations"
            params: list[Any] = []
            if campaign_id:
                query += " WHERE campaign_id = ? OR campaign_id LIKE ?"
                params.extend([campaign_id, f"{campaign_id}%"])
            query += " ORDER BY opened_at ASC"
            cursor = db.execute(query, params)
            col_names = [desc[0] for desc in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(col_names, row, strict=False))
                esc_dict = {
                    "escalation_id": row_dict["id"],
                    "campaign_id": row_dict["campaign_id"],
                    "project_slug": row_dict["project_slug"],
                    "level": int(row_dict["level"]),
                    "status": row_dict["status"],
                    "opened_at": str(row_dict["opened_at"]),
                    "opened_by_kind": row_dict["opened_by_kind"],
                    "opened_by_review_id": row_dict.get("opened_by_review_id"),
                    "trigger_name": row_dict.get("trigger_name"),
                    "trigger_reason": row_dict["trigger_reason"],
                    "resolved_at": str(row_dict["resolved_at"]) if row_dict.get("resolved_at") else None,
                    "resolved_by": row_dict.get("resolved_by"),
                    "resolved_kind": row_dict.get("resolved_kind"),
                    "resolution_note": row_dict.get("resolution_note") or "",
                    "deescalated_to": row_dict.get("deescalated_to"),
                    "review_epoch": int(row_dict.get("review_epoch") or 0),
                    "pivot_anchor_run_id": row_dict.get("pivot_anchor_run_id"),
                    "metadata": json.loads(row_dict.get("metadata") or "{}"),
                }
                records.append(EscalationRecord.from_dict(esc_dict))
            if records:
                return records
        except Exception:
            pass

    if catalog_dir is not None:
        file_paths = iter_cool_escalations(catalog_dir)
        for p in file_paths:
            try:
                rec = load_escalation_cool(p)
                if campaign_id is None or rec.campaign_id == campaign_id or rec.campaign_id.startswith(campaign_id):
                    records.append(rec)
            except Exception:
                continue

    records.sort(key=lambda r: r.opened_at)
    return records


def get_escalation(
    escalation_id: str,
    catalog_dir: Path | None = None,
    db: duckdb.DuckDBPyConnection | None = None,
) -> EscalationRecord | None:
    """Retrieve escalation record by full ID or prefix."""
    escalations = list_escalations(catalog_dir=catalog_dir, db=db)
    for esc in escalations:
        if esc.escalation_id == escalation_id or esc.escalation_id.startswith(escalation_id):
            return esc
    return None


def get_escalation_state(
    campaign_id: str,
    catalog_dir: Path | None = None,
    db: duckdb.DuckDBPyConnection | None = None,
) -> EscalationState:
    """Reconstruct current EscalationState for a campaign from escalation and review history."""
    escalations = list_escalations(campaign_id=campaign_id, catalog_dir=catalog_dir, db=db)
    reviews = list_reviews(campaign_id=campaign_id, catalog_dir=catalog_dir, db=db)

    state = EscalationState(campaign_id=campaign_id)

    if reviews:
        latest_rev = reviews[-1]
        state.latest_review_id = latest_rev.review_id
        state.latest_recommendation = latest_rev.recommendation

    if not escalations:
        # If there are reviews with hold/escalate recommendations that weren't resolved, reflect that
        if reviews:
            latest_rev = reviews[-1]
            if latest_rev.escalation_level > 0:
                state.current_level = latest_rev.escalation_level
                status_map = {1: "warning", 2: "hold", 3: "escalated"}
                state.status = status_map.get(latest_rev.escalation_level, "nominal")
        return state

    # Replay escalations
    status_map = {0: "nominal", 1: "warning", 2: "hold", 3: "escalated"}
    active_triggers: list[str] = []

    for esc in escalations:
        state.current_level = esc.level
        state.status = status_map.get(esc.level, "nominal")
        state.review_epoch = max(state.review_epoch, esc.review_epoch)
        if esc.pivot_anchor_run_id:
            state.pivot_anchor_run_id = esc.pivot_anchor_run_id

        if esc.status == "active" and esc.level > 0:
            state.active_escalation_id = esc.escalation_id
            if esc.trigger_name and esc.trigger_name not in active_triggers:
                active_triggers.append(esc.trigger_name)
        elif esc.status == "resolved" or esc.level == 0:
            state.active_escalation_id = None
            active_triggers = []

    state.active_triggers = active_triggers
    return state


def ingest_stepback_fragments(
    db: duckdb.DuckDBPyConnection,
    catalog_dir: Path,
) -> tuple[int, int]:
    """Ingest cool-tier reviews and escalations into warm DuckDB tables.

    Returns:
        tuple[int, int]: (reviews_ingested, escalations_ingested)
    """
    db.execute(STEPBACK_REVIEWS_SCHEMA)
    db.execute(STEPBACK_ESCALATIONS_SCHEMA)

    reviews_ingested = 0
    escalations_ingested = 0

    # 1. Ingest reviews
    review_files = iter_cool_reviews(catalog_dir)
    for p in review_files:
        try:
            rev = load_review_cool(p)
            existing = db.execute("SELECT id FROM stepback_reviews WHERE id = ?", [rev.review_id]).fetchone()
            if existing:
                continue

            scores_json = json.dumps(rev.rubric_scores) if rev.rubric_scores else None
            open_analysis_mode = rev.open_analysis.mode if rev.open_analysis else None
            open_analysis_json = json.dumps(rev.open_analysis.sections) if rev.open_analysis else None
            cited_json = json.dumps(rev.cited_evidence)
            dissents_json = json.dumps([d.to_dict() for d in rev.dissents]) if rev.dissents else None
            metadata_json = json.dumps(rev.metadata) if rev.metadata else None

            db.execute(
                """
                INSERT OR REPLACE INTO stepback_reviews (
                    id, campaign_id, project_slug, created_at, dispatched_at,
                    reviewer_id, reviewer_kind, is_independent, path_kind,
                    input_snapshot_sha256, input_snapshot, rubric_id, rubric_version,
                    rubric_sha256, scores_json, composite_score, open_analysis_mode,
                    open_analysis_json, cited_evidence_json, recommendation,
                    escalation_level, is_dissent, parent_panel_id, dissents_json, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    rev.review_id,
                    rev.campaign_id,
                    rev.project_slug,
                    rev.created_at,
                    rev.dispatched_at,
                    rev.reviewer_id,
                    rev.reviewer_kind,
                    rev.is_independent,
                    rev.path_kind,
                    rev.input_snapshot_sha256,
                    rev.input_snapshot,
                    rev.rubric_id,
                    rev.rubric_version,
                    rev.rubric_sha256,
                    scores_json,
                    rev.composite_score,
                    open_analysis_mode,
                    open_analysis_json,
                    cited_json,
                    rev.recommendation,
                    rev.escalation_level,
                    False,
                    None,
                    dissents_json,
                    metadata_json,
                ],
            )
            reviews_ingested += 1
        except Exception as e:
            logger.warning(f"Failed to ingest review fragment {p}: {e}")

    # 2. Ingest escalations
    esc_files = iter_cool_escalations(catalog_dir)
    for p in esc_files:
        try:
            esc = load_escalation_cool(p)
            existing = db.execute("SELECT id FROM stepback_escalations WHERE id = ?", [esc.escalation_id]).fetchone()
            if existing:
                continue

            metadata_json = json.dumps(esc.metadata) if esc.metadata else None

            db.execute(
                """
                INSERT OR REPLACE INTO stepback_escalations (
                    id, campaign_id, project_slug, level, status, opened_at,
                    opened_by_kind, opened_by_review_id, trigger_name, trigger_reason,
                    resolved_at, resolved_by, resolved_kind, resolution_note,
                    deescalated_to, review_epoch, pivot_anchor_run_id, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    esc.escalation_id,
                    esc.campaign_id,
                    esc.project_slug,
                    esc.level,
                    esc.status,
                    esc.opened_at,
                    esc.opened_by_kind,
                    esc.opened_by_review_id,
                    esc.trigger_name,
                    esc.trigger_reason,
                    esc.resolved_at,
                    esc.resolved_by,
                    esc.resolved_kind,
                    esc.resolution_note,
                    esc.deescalated_to,
                    esc.review_epoch,
                    esc.pivot_anchor_run_id,
                    metadata_json,
                ],
            )
            escalations_ingested += 1
        except Exception as e:
            logger.warning(f"Failed to ingest escalation fragment {p}: {e}")

    return reviews_ingested, escalations_ingested
