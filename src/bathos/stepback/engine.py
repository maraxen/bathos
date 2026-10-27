"""Core Step-Back Review and Escalation Orchestration Engine (spec 261027).

Provides unified operational logic for both FastMCP tools and Cyclopts CLI commands:
- stepback_status
- stepback_run / stepback_trigger
- stepback_record / stepback_review
- stepback_escalate
- stepback_show
- stepback_resolve
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bathos.breakdown_gate import fetch_member_runs
from bathos.campaigns import (
    CampaignError,
    connect_catalog_db,
    get_campaign,
    union_campaign_member_ids,
)
from bathos.config import default_catalog_dir
from bathos.errors import (
    StepBackAgentOverrideRefusedError,
    StepBackInvalidSchemaError,
    StepBackNotFoundError,
    StepBackUnauthorizedChairError,
)
from bathos.runlog.mode import is_log_mode
from bathos.stepback.analysis import (
    check_reviewer_independence,
    validate_campaign_evidence,
    validate_open_analysis,
)
from bathos.stepback.catalog import (
    get_escalation,
    get_escalation_state,
    get_review,
    list_reviews,
    save_escalation_cool,
    save_review_cool,
)
from bathos.stepback.escalation import (
    is_agent_environment,
    transition_escalation,
    validate_level,
    verify_chair_token,
)
from bathos.stepback.rubrics import (
    evaluate_rubric,
    load_rubric,
)
from bathos.stepback.schema import (
    DissentRecord,
    EscalationRecord,
    OpenAnalysis,
    Recommendation,
    StepBackReview,
)
from bathos.stepback.triggers import evaluate_all_triggers


def resolve_catalog_and_db(
    catalog_dir: Path | str | None = None,
    db: Any | None = None,
    read_only: bool = True,
) -> tuple[Path, Any, bool]:
    """Resolve catalog directory and open DuckDB connection if not provided."""
    if catalog_dir is None:
        cat_dir = Path(os.environ.get("BTH_CATALOG_DIR") or default_catalog_dir())
    else:
        cat_dir = Path(catalog_dir)

    owns_db = False
    if db is None:
        db = connect_catalog_db(cat_dir, read_only=read_only)
        owns_db = True

    return cat_dir, db, owns_db


def compute_input_snapshot(
    campaign: Any,
    member_runs: list[Any],
    workspace_root: Path | None = None,
) -> tuple[str, str]:
    """Compute cryptographic input snapshot JSON and SHA-256 hash."""
    run_ids: list[str] = []
    latest_run_sha = ""
    sidecar_shas: dict[str, str] = {}
    git_head = ""

    for r in member_runs:
        rid = None
        git_sha = None
        if isinstance(r, dict):
            rid = r.get("id") or r.get("run_id") or r.get("uuid")
            git_sha = r.get("git_commit") or r.get("git_sha")
        else:
            rid = getattr(r, "id", None) or getattr(r, "run_id", None) or getattr(r, "uuid", None)
            git_sha = getattr(r, "git_commit", None) or getattr(r, "git_sha", None)
        if rid:
            run_ids.append(str(rid))
        if git_sha:
            git_head = str(git_sha)

    if run_ids:
        latest_run_sha = hashlib.sha256(run_ids[-1].encode()).hexdigest()

    if not git_head and workspace_root is not None:
        try:
            head_file = Path(workspace_root) / ".git" / "HEAD"
            if head_file.is_file():
                content = head_file.read_text().strip()
                if content.startswith("ref: "):
                    ref_path = Path(workspace_root) / ".git" / content[5:]
                    if ref_path.is_file():
                        git_head = ref_path.read_text().strip()
                else:
                    git_head = content
        except Exception:
            pass

    snapshot_data = {
        "campaign_id": getattr(campaign, "id", getattr(campaign, "campaign_id", str(campaign))),
        "evaluated_run_ids": sorted(run_ids),
        "latest_run_sha": latest_run_sha,
        "sidecar_shas": sidecar_shas,
        "git_head_commit": git_head,
    }
    canonical_json = json.dumps(snapshot_data, sort_keys=True)
    snapshot_sha = hashlib.sha256(canonical_json.encode()).hexdigest()
    return canonical_json, snapshot_sha


def get_stepback_status(
    campaign_id: str,
    catalog_dir: Path | str | None = None,
    db: Any | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Query current step-back review and escalation status for a campaign."""
    cat_dir, con, owns_db = resolve_catalog_and_db(catalog_dir, db, read_only=True)
    try:
        camp = get_campaign(con, campaign_id, catalog_dir=cat_dir)
        if camp is None:
            raise CampaignError(f"Campaign not found: {campaign_id}")

        full_id = camp.id
        state = get_escalation_state(full_id, catalog_dir=cat_dir, db=con)
        reviews = [r for r in list_reviews(catalog_dir=cat_dir) if r.campaign_id == full_id]

        members = union_campaign_member_ids(con, full_id, cat_dir)
        raw_runs = (
            fetch_member_runs(con, members, cat_dir, log_mode=is_log_mode(cat_dir))
            if members
            else []
        )
        member_runs = [r for r in raw_runs.values() if r is not None] if isinstance(raw_runs, dict) else [r for r in raw_runs if r is not None]
        triggers_res = evaluate_all_triggers(camp, member_runs, state=state)

        latest_rec = reviews[-1].recommendation if reviews else None
        res: dict[str, Any] = {
            "ok": True,
            "campaign_id": full_id,
            "escalation_level": state.current_level,
            "hold_active": state.current_level >= 2,
            "active_triggers": triggers_res.get("active_triggers", []),
            "review_count": len(reviews),
            "latest_recommendation": latest_rec,
            "epoch": state.review_epoch,
        }
        if verbose:
            res["state"] = dataclasses.asdict(state)
            res["triggers_evaluated"] = triggers_res.get("triggers_evaluated", {})
        return res
    finally:
        if owns_db and con is not None:
            con.close()


def run_stepback_triggers(
    campaign_id: str,
    catalog_dir: Path | str | None = None,
    db: Any | None = None,
    window: int = 5,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Evaluate event triggers for a campaign and report active triggers."""
    cat_dir, con, owns_db = resolve_catalog_and_db(catalog_dir, db, read_only=True)
    try:
        camp = get_campaign(con, campaign_id, catalog_dir=cat_dir)
        if camp is None:
            raise CampaignError(f"Campaign not found: {campaign_id}")

        full_id = camp.id
        state = get_escalation_state(full_id, catalog_dir=cat_dir, db=con)
        members = union_campaign_member_ids(con, full_id, cat_dir)
        raw_runs = (
            fetch_member_runs(con, members, cat_dir, log_mode=is_log_mode(cat_dir))
            if members
            else []
        )
        member_runs = [r for r in raw_runs.values() if r is not None] if isinstance(raw_runs, dict) else [r for r in raw_runs if r is not None]

        eval_res = evaluate_all_triggers(camp, member_runs, state=state, window_size=window)
        active = eval_res.get("active_triggers", [])
        fired = len(active) > 0

        if fired and not dry_run and state.current_level == 0:
            reason = f"Event triggers fired: {', '.join(active)}"
            new_state, esc_rec = transition_escalation(
                state, target_level=1, reason=reason, opened_by_kind="system"
            )
            save_escalation_cool(esc_rec, cat_dir)

        return {
            "ok": True,
            "triggers_evaluated": eval_res.get("triggers_evaluated", {}),
            "active_triggers": active,
            "fired": fired,
        }
    finally:
        if owns_db and con is not None:
            con.close()


def record_stepback_review(
    campaign_id: str,
    path_kind: str = "rubric",
    rubric_id: str | None = None,
    scores: dict[str, int] | None = None,
    mode: str | None = None,
    narrative: dict[str, str] | None = None,
    evidence: list[str] | None = None,
    reviewer_id: str | None = None,
    reviewer_kind: str = "agent",
    recommendation: str | None = None,
    dissents: list[dict[str, Any]] | None = None,
    input_snapshot_sha256: str | None = None,
    catalog_dir: Path | str | None = None,
    db: Any | None = None,
    dispatched_at: str | None = None,
) -> dict[str, Any]:
    """Execute and record a full step-back review."""
    cat_dir, con, owns_db = resolve_catalog_and_db(catalog_dir, db, read_only=True)
    try:
        camp = get_campaign(con, campaign_id, catalog_dir=cat_dir)
        if camp is None:
            raise CampaignError(f"Campaign not found: {campaign_id}")

        full_id = camp.id
        project_slug = camp.project_slug or "default"
        state = get_escalation_state(full_id, catalog_dir=cat_dir, db=con)

        members = union_campaign_member_ids(con, full_id, cat_dir)
        raw_runs = (
            fetch_member_runs(con, members, cat_dir, log_mode=is_log_mode(cat_dir))
            if members
            else []
        )
        member_runs = [r for r in raw_runs.values() if r is not None] if isinstance(raw_runs, dict) else [r for r in raw_runs if r is not None]

        eff_reviewer = reviewer_id or os.environ.get("AGENT_NAME") or os.environ.get("USER") or "reviewer"
        is_indep = check_reviewer_independence(
            eff_reviewer, camp, member_runs, raise_on_conflict=True
        )

        final_composite: float | None = None
        final_rec: str = ""
        esc_level: int = 0
        rubric_meta_id: str | None = None
        rubric_version: str | None = None
        rubric_sha: str | None = None
        rubric_scores_dict: dict[str, int] = {}
        open_analysis_obj: OpenAnalysis | None = None

        norm_path_kind = str(path_kind).lower().strip()
        if norm_path_kind in ("rubric", "path_a"):
            if not scores:
                raise StepBackInvalidSchemaError("Scores dictionary is required for Path A rubric evaluation")
            rubric_scores_dict = {str(k): int(v) for k, v in scores.items()}
            composite, calc_rec, calc_lvl, r_sha = evaluate_rubric(
                rubric_scores_dict, rubric_path=rubric_id
            )
            final_composite = composite
            final_rec = calc_rec
            esc_level = calc_lvl
            rubric_dict, _ = load_rubric(rubric_path=rubric_id)
            rubric_meta_id = rubric_dict.get("id", "stepback_v1")
            rubric_version = rubric_dict.get("version", "1.0.0")
            rubric_sha = r_sha

        elif norm_path_kind in ("open_analysis", "path_b"):
            if not recommendation:
                raise StepBackInvalidSchemaError(
                    "Recommendation parameter is strictly required for Path B open analysis"
                )
            analysis_dict = {"mode": mode, "sections": narrative or {}}
            open_analysis_obj = validate_open_analysis(analysis_dict, recommendation=recommendation)
            final_rec = str(recommendation).upper().strip()
            rec_to_lvl = {
                Recommendation.CONTINUE.value: 0,
                Recommendation.PIVOT.value: 1,
                Recommendation.HOLD.value: 2,
                Recommendation.ESCALATE.value: 2,
                Recommendation.CONCLUDE_ABANDON.value: 2,
            }
            esc_level = rec_to_lvl.get(final_rec, 2)

            # Validate cited evidence
            ev_list = evidence or []
            validate_campaign_evidence(
                ev_list, camp, member_runs, min_citations=2, raise_on_invalid=True
            )

        elif norm_path_kind == "hybrid":
            if not scores:
                raise StepBackInvalidSchemaError("Scores dictionary is required for hybrid evaluation")
            rubric_scores_dict = {str(k): int(v) for k, v in scores.items()}
            composite, calc_rec, calc_lvl, r_sha = evaluate_rubric(rubric_scores_dict)
            final_composite = composite
            final_rec = recommendation.upper() if recommendation else calc_rec
            esc_level = calc_lvl
            rubric_dict, _ = load_rubric()
            rubric_meta_id = rubric_dict.get("id", "stepback_v1")
            rubric_version = rubric_dict.get("version", "1.0.0")
            rubric_sha = r_sha
            if mode or narrative:
                analysis_dict = {"mode": mode, "sections": narrative or {}}
                open_analysis_obj = validate_open_analysis(analysis_dict, recommendation=final_rec)
        else:
            raise StepBackInvalidSchemaError(
                f"Unknown path_kind '{path_kind}'. Must be 'rubric', 'open_analysis', or 'hybrid'"
            )

        # Snapshot computation
        snap_json, snap_sha = compute_input_snapshot(camp, member_runs)
        if input_snapshot_sha256 and input_snapshot_sha256 != snap_sha:
            pass  # Allowed if passed, but computed is canonical

        # Process dissents
        dissent_objs: list[DissentRecord] = []
        if dissents:
            for d in dissents:
                d_obj = DissentRecord(
                    dissenting_reviewer_id=d.get("dissenting_reviewer_id", "dissenter"),
                    minority_recommendation=d.get("minority_recommendation", "HOLD"),
                    minority_composite_score=d.get("minority_composite_score"),
                    dissent_summary=d.get("dissent_summary", ""),
                    cited_counter_evidence=d.get("cited_counter_evidence", []),
                    majority_rebuttal=d.get("majority_rebuttal", ""),
                )
                dissent_objs.append(d_obj)

        now_iso = datetime.now(UTC).isoformat()
        rev_id = f"rev_{os.urandom(6).hex()}"
        review = StepBackReview(
            review_id=rev_id,
            campaign_id=full_id,
            project_slug=project_slug,
            created_at=now_iso,
            reviewer_id=eff_reviewer,
            reviewer_kind=reviewer_kind,
            is_independent=is_indep,
            path_kind=norm_path_kind,
            input_snapshot_sha256=snap_sha,
            input_snapshot=snap_json,
            recommendation=final_rec,
            escalation_level=esc_level,
            cited_evidence=list(evidence or []),
            rubric_id=rubric_meta_id,
            rubric_version=rubric_version,
            rubric_sha256=rubric_sha,
            rubric_scores=rubric_scores_dict,
            composite_score=final_composite,
            open_analysis=open_analysis_obj,
            dissents=dissent_objs,
            dispatched_at=dispatched_at,
        )

        save_review_cool(review, cat_dir)

        # Update escalation state if recommendation modifies level
        if esc_level != state.current_level:
            # If review recommends CONTINUE (0) and state was Warning (1), de-escalate
            # If review recommends HOLD (2), escalate to Hold
            reason = f"Step-back review {rev_id} recommendation {final_rec}"
            new_state, esc_rec = transition_escalation(
                state,
                target_level=esc_level,
                reason=reason,
                opened_by_kind="reviewer",
                reviewer_id=eff_reviewer,
            )
            save_escalation_cool(esc_rec, cat_dir)

        return {
            "ok": True,
            "review_id": rev_id,
            "composite_score": final_composite,
            "recommendation": final_rec,
            "escalation_level": esc_level,
        }
    finally:
        if owns_db and con is not None:
            con.close()


def escalate_stepback(
    campaign_id: str,
    target_level: int,
    reason: str,
    catalog_dir: Path | str | None = None,
    db: Any | None = None,
    opened_by_kind: str = "agent",
) -> dict[str, Any]:
    """Escalate a campaign to a target level."""
    target_level = validate_level(target_level)
    cat_dir, con, owns_db = resolve_catalog_and_db(catalog_dir, db, read_only=True)
    try:
        camp = get_campaign(con, campaign_id, catalog_dir=cat_dir)
        if camp is None:
            raise CampaignError(f"Campaign not found: {campaign_id}")

        full_id = camp.id
        state = get_escalation_state(full_id, catalog_dir=cat_dir, db=con)
        prev_level = state.current_level

        new_state, esc_rec = transition_escalation(
            state,
            target_level=target_level,
            reason=reason,
            opened_by_kind=opened_by_kind,
        )
        save_escalation_cool(esc_rec, cat_dir)

        return {
            "ok": True,
            "escalation_id": esc_rec.escalation_id,
            "previous_level": prev_level,
            "current_level": target_level,
        }
    finally:
        if owns_db and con is not None:
            con.close()


def show_stepback(
    campaign_id: str | None = None,
    review_id: str | None = None,
    catalog_dir: Path | str | None = None,
    db: Any | None = None,
) -> dict[str, Any]:
    """Retrieve details for a review or all reviews in a campaign."""
    cat_dir, con, owns_db = resolve_catalog_and_db(catalog_dir, db, read_only=True)
    try:
        if review_id:
            rev = get_review(review_id, catalog_dir=cat_dir, db=con)
            if rev is None:
                raise StepBackNotFoundError(f"Review not found: {review_id}")
            return {"ok": True, "review": dataclasses.asdict(rev)}

        if campaign_id:
            camp = get_campaign(con, campaign_id, catalog_dir=cat_dir)
            if camp is None:
                raise CampaignError(f"Campaign not found: {campaign_id}")
            full_id = camp.id
            reviews = [r for r in list_reviews(catalog_dir=cat_dir) if r.campaign_id == full_id]
            return {"ok": True, "reviews": [dataclasses.asdict(r) for r in reviews]}

        raise StepBackInvalidSchemaError("Either campaign_id or review_id must be provided")
    finally:
        if owns_db and con is not None:
            con.close()


def resolve_stepback(
    campaign_id: str | None = None,
    escalation_id: str | None = None,
    deescalate_to: int = 0,
    note: str = "",
    as_human_chair: bool = False,
    chair_token: str | None = None,
    interactive_tty: bool | None = None,
    catalog_dir: Path | str | None = None,
    db: Any | None = None,
    strict_agent_refusal: bool = False,
) -> dict[str, Any]:
    """Resolve or de-escalate an active campaign escalation.

    Enforces Human Chair Invariance:
    If caller is an autonomous agent or strict_agent_refusal is True,
    any attempt to resolve Level 3 fails with StepBackAgentOverrideRefusedError.
    """
    deescalate_to = validate_level(deescalate_to)
    cat_dir, con, owns_db = resolve_catalog_and_db(catalog_dir, db, read_only=True)
    try:
        camp_full_id: str | None = None
        target_esc: EscalationRecord | None = None

        if escalation_id:
            target_esc = get_escalation(escalation_id, catalog_dir=cat_dir, db=con)
            if target_esc is None:
                raise StepBackNotFoundError(f"Escalation not found: {escalation_id}")
            camp_full_id = target_esc.campaign_id
        elif campaign_id:
            camp = get_campaign(con, campaign_id, catalog_dir=cat_dir)
            if camp is None:
                raise CampaignError(f"Campaign not found: {campaign_id}")
            camp_full_id = camp.id
        else:
            raise StepBackInvalidSchemaError("Either escalation_id or campaign_id must be provided")

        state = get_escalation_state(camp_full_id, catalog_dir=cat_dir, db=con)

        # Check current level
        current_lvl = target_esc.level if target_esc else state.current_level

        # STRICT AGENT INVARIANCE CHECK
        if current_lvl == 3:
            if strict_agent_refusal or is_agent_environment():
                raise StepBackAgentOverrideRefusedError(
                    "Autonomous agents are strictly forbidden from overriding or resolving Level 3 Human Chair escalations"
                )
            if not as_human_chair and not verify_chair_token(chair_token) and not interactive_tty:
                raise StepBackUnauthorizedChairError(
                    "Resolving Level 3 Human Chair escalation requires interactive TTY or chair token"
                )

        new_state, esc_rec = transition_escalation(
            state,
            target_level=deescalate_to,
            reason=note or "Resolved",
            as_human_chair=as_human_chair,
            chair_token=chair_token,
            interactive_tty=interactive_tty,
        )
        save_escalation_cool(esc_rec, cat_dir)

        return {
            "ok": True,
            "escalation_id": target_esc.escalation_id if target_esc else esc_rec.escalation_id,
            "status": "resolved" if deescalate_to == 0 else "deescalated",
            "deescalated_to": deescalate_to,
        }
    finally:
        if owns_db and con is not None:
            con.close()
