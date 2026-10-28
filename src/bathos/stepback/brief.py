"""Advisor Brief and Project Status Matrix Integration (spec 261027).

Provides:
- `render_advisor_brief(campaign_id, catalog_dir, db) -> str`:
  Generates an 8-section Markdown brief for human advisor / committee review
  synthesizing primary records (stakes & gap, trajectory, threshold basis,
  alternative hypotheses & dissents, exit criteria & sunk cost, escalation status,
  and project status matrix entry).
- `format_project_status_entry(campaign_id, catalog_dir, db) -> str`:
  Formats the structured string feeding the "End / reconsider" column of
  PROJECT_STATUS.advisor.md:
  * L0: [NOMINAL: N runs, pass rate X.XX]
  * L1: [ADVISORY L1: <trigger_name> active; review recommended]
  * L2: [HOLD L2: <trigger_reason>; review <review_id> recommends <rec>]
  * L3: [ESCALATE L3: Human Chair disposition required; <reason>]
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from bathos.campaigns import CampaignError, get_campaign, union_campaign_member_ids
from bathos.runlog.mode import is_log_mode
from bathos.stepback.catalog import get_escalation_state, list_escalations, list_reviews
from bathos.stepback.engine import fetch_member_runs, resolve_catalog_and_db
from bathos.stepback.triggers import _get_item_attr, evaluate_all_triggers


def format_project_status_entry(
    campaign_id: str,
    catalog_dir: Path | str | None = None,
    db: Any | None = None,
    *,
    include_nominal: bool = True,
) -> str:
    """Format structured status string feeding the 'End / reconsider' column of PROJECT_STATUS.advisor.md.

    When `include_nominal=False` (default for the PROJECT_STATUS.advisor.md matrix column),
    Level 0 returns `""` (concise/blank) so only campaigns at Level >= 1 surface tags.

    Formats:
    - L0: [NOMINAL: N runs, pass rate X.XX] (or "" when include_nominal=False)
    - L1: [ADVISORY L1: <trigger_name> active; review recommended]
    - L2: [HOLD L2: <trigger_reason>; review <review_id> recommends <rec>]
    - L3: [ESCALATE L3: Human Chair disposition required; <reason>]
    """
    cat_dir, con, owns_db = resolve_catalog_and_db(catalog_dir, db, read_only=True)
    try:
        camp = get_campaign(con, campaign_id, catalog_dir=cat_dir)
        if camp is None:
            raise CampaignError(f"Campaign not found: {campaign_id}")
        full_id = camp.id

        state = get_escalation_state(full_id, catalog_dir=cat_dir, db=con)
        escalations = list_escalations(campaign_id=full_id, catalog_dir=cat_dir, db=con)
        reviews = [r for r in list_reviews(catalog_dir=cat_dir) if r.campaign_id == full_id]

        level = state.current_level
        if level == 0:
            if not include_nominal:
                return ""
            members = union_campaign_member_ids(con, full_id, cat_dir)
            raw_runs = (
                fetch_member_runs(con, members, cat_dir, log_mode=is_log_mode(cat_dir))
                if members
                else []
            )
            member_runs = [r for r in raw_runs.values() if r is not None] if isinstance(raw_runs, dict) else [r for r in raw_runs if r is not None]
            n_runs = len(member_runs)
            passes = sum(1 for r in member_runs if _get_item_attr(r, "outcome") == "pass")
            pass_rate = (passes / n_runs) if n_runs > 0 else 0.0
            return f"[NOMINAL: {n_runs} runs, pass rate {pass_rate:.2f}]"

        if level == 1:
            trigger_name = "trigger"
            if state.active_triggers:
                trigger_name = state.active_triggers[0]
            else:
                active_escs = [e for e in escalations if e.level == 1 and e.status == "active"]
                if active_escs and active_escs[-1].trigger_name:
                    trigger_name = active_escs[-1].trigger_name
            return f"[ADVISORY L1: {trigger_name} active; review recommended]"

        if level == 2:
            reason = "active hold"
            active_escs = [e for e in escalations if e.level == 2 and e.status == "active"]
            if active_escs and active_escs[-1].trigger_reason:
                reason = active_escs[-1].trigger_reason
            elif state.active_triggers:
                reason = f"trigger {state.active_triggers[0]}"

            rev_id = state.latest_review_id or (reviews[-1].review_id if reviews else "pending")
            rec = state.latest_recommendation or (reviews[-1].recommendation if reviews else "HOLD")
            return f"[HOLD L2: {reason}; review {rev_id} recommends {rec}]"

        # level == 3
        reason = "Human Chair review requested"
        active_escs = [e for e in escalations if e.level == 3 and e.status == "active"]
        if active_escs and active_escs[-1].trigger_reason:
            reason = active_escs[-1].trigger_reason
        elif state.active_triggers:
            reason = f"trigger {state.active_triggers[0]}"
        return f"[ESCALATE L3: Human Chair disposition required; {reason}]"
    finally:
        if owns_db and con is not None:
            con.close()


def render_advisor_brief(
    campaign_id: str,
    catalog_dir: Path | str | None = None,
    db: Any | None = None,
) -> str:
    """Render an 8-section Markdown brief for an advisor or committee review.

    Sections:
    1. Header: # Advisor Brief: <campaign_name> (<campaign_id>)
    2. Stakes & Scientific Question (Stakes & Gap)
    3. Trajectory & Outcomes
    4. Threshold Basis & Drift
    5. Alternative Hypotheses & Dissents
    6. Exit Criteria & Sunk Cost
    7. Escalation Status & Chair Disposition
    8. Project Status Matrix Entry ("End / reconsider")
    """
    cat_dir, con, owns_db = resolve_catalog_and_db(catalog_dir, db, read_only=True)
    try:
        camp = get_campaign(con, campaign_id, catalog_dir=cat_dir)
        if camp is None:
            raise CampaignError(f"Campaign not found: {campaign_id}")
        full_id = camp.id

        state = get_escalation_state(full_id, catalog_dir=cat_dir, db=con)
        escalations = list_escalations(campaign_id=full_id, catalog_dir=cat_dir, db=con)
        reviews = [r for r in list_reviews(catalog_dir=cat_dir) if r.campaign_id == full_id]

        members = union_campaign_member_ids(con, full_id, cat_dir)
        raw_runs = (
            fetch_member_runs(con, members, cat_dir, log_mode=is_log_mode(cat_dir))
            if members
            else []
        )
        member_runs = [r for r in raw_runs.values() if r is not None] if isinstance(raw_runs, dict) else [r for r in raw_runs if r is not None]

        # Evaluate triggers
        triggers_res = evaluate_all_triggers(camp, member_runs, state=state)
        active_triggers = triggers_res.get("active_triggers", [])
        snoozed_triggers = triggers_res.get("snoozed_triggers", [])

        # Run statistics
        n_runs = len(member_runs)
        outcomes = [_get_item_attr(r, "outcome") or "unknown" for r in member_runs]
        outcome_counts = Counter(outcomes)
        passes = outcome_counts.get("pass", 0)
        pass_rate = (passes / n_runs) if n_runs > 0 else 0.0

        # Duration / compute sunk cost
        total_duration_s = sum(
            float(_get_item_attr(r, "duration_s") or 0.0)
            for r in member_runs
        )
        if total_duration_s == 0.0 and con is not None and members:
            try:
                marks = ", ".join("?" for _ in members)
                dur_row = con.execute(
                    f"SELECT SUM(duration_s) FROM runs WHERE id IN ({marks})",  # noqa: S608
                    members,
                ).fetchone()
                if dur_row and dur_row[0] is not None:
                    total_duration_s = float(dur_row[0])
            except Exception:
                pass
        duration_hours = total_duration_s / 3600.0

        # Status matrix entry
        status_entry = format_project_status_entry(full_id, catalog_dir=cat_dir, db=con)

        level_names = {
            0: "Nominal (Level 0)",
            1: "Advisory Warning (Level 1)",
            2: "Committee Hold (Level 2)",
            3: "Human Chair Escalation (Level 3)",
        }
        current_level_name = level_names.get(state.current_level, f"Level {state.current_level}")

        lines: list[str] = []
        # Title
        lines.append(f"# Advisor Brief: {camp.name} ({full_id})")
        lines.append("")

        # Section 1: Stakes & Scientific Question
        lines.append("## 1. Stakes & Scientific Question (Stakes & Gap)")
        lines.append(f"- **Campaign Mode**: `{camp.mode}`")
        lines.append(f"- **Central Question**: {camp.question or 'Not formally recorded'}")
        lines.append(f"- **Hypothesis**: {camp.hypothesis or 'Not formally recorded'}")
        if camp.claim_path:
            lines.append(f"- **Claim Path**: `{camp.claim_path}`")
        data_status = "Concluded" if camp.concluded_at else ("In Progress (preliminary)" if n_runs < 5 else "In Progress (mature)")
        lines.append(f"- **Data Status**: {data_status} ({n_runs} runs)")
        lines.append("")

        # Section 2: Trajectory & Outcomes
        lines.append("## 2. Trajectory & Outcomes")
        lines.append(f"- **Total Runs Evaluated**: {n_runs}")
        lines.append(f"- **Overall Pass Rate**: {pass_rate:.2%}")
        if n_runs > 0:
            counts_str = ", ".join(f"`{k}`: {v}" for k, v in outcome_counts.most_common())
            lines.append(f"- **Outcome Distribution**: {counts_str}")
            recent = outcomes[-5:]
            lines.append(f"- **Recent Outcome Trajectory (last {len(recent)})**: {' → '.join(f'`{o}`' for o in recent)}")
        else:
            lines.append("- *No runs recorded for this campaign.*")
        lines.append("")

        # Section 3: Threshold Basis & Drift
        lines.append("## 3. Threshold Basis & Drift")
        if camp.stopping_threshold is not None:
            lines.append(f"- **Stopping Threshold**: `{camp.stopping_threshold}`")
        else:
            lines.append("- **Stopping Threshold**: *None locked*")
        if getattr(camp, "target_metric", None):
            lines.append(f"- **Target Metric**: `{camp.target_metric}`")
        # Check threshold edits
        thresh_eval = triggers_res.get("triggers_evaluated", {}).get("threshold_edit", {})
        if thresh_eval.get("fired"):
            lines.append(f"- **Threshold Drift Warning**: Detected {thresh_eval.get('edit_count', 0)} sidecar threshold edits during execution.")
        else:
            lines.append("- **Threshold Stability**: Stable (no mid-campaign threshold edits detected).")
        lines.append("")

        # Section 4: Alternative Hypotheses & Dissents
        lines.append("## 4. Alternative Hypotheses & Dissents")
        # Extract alternatives from open analyses
        alternatives_found: list[str] = []
        all_dissents = []
        for r in reviews:
            if r.open_analysis and r.open_analysis.sections:
                for k in ("alternatives", "alternative_hypotheses", "rival_hypotheses", "so_what"):
                    if k in r.open_analysis.sections:
                        alternatives_found.append(f"Review `{r.review_id}` ({r.open_analysis.mode}): {r.open_analysis.sections[k]}")
            if r.dissents:
                all_dissents.extend(r.dissents)

        if alternatives_found:
            lines.append("- **Alternative Explanations Considered**:")
            for alt in alternatives_found:
                lines.append(f"  - {alt}")
        else:
            lines.append("- **Alternative Explanations**: No alternative hypothesis narratives recorded in step-back reviews.")

        if all_dissents:
            lines.append("- **Recorded Minority Dissents**:")
            for d in all_dissents:
                lines.append(f"  - **Reviewer**: `{d.dissenting_reviewer_id}` | **Recommendation**: `{d.minority_recommendation}`")
                lines.append(f"    - *Summary*: {d.dissent_summary}")
                if d.cited_counter_evidence:
                    lines.append(f"    - *Counter-evidence*: {', '.join(d.cited_counter_evidence)}")
                if d.majority_rebuttal:
                    lines.append(f"    - *Rebuttal*: {d.majority_rebuttal}")
        else:
            lines.append("- **Minority Dissents**: No minority dissents recorded.")
        lines.append("")

        # Section 5: Exit Criteria & Sunk Cost
        lines.append("## 5. Exit Criteria & Sunk Cost")
        exit_criteria = "Conclude upon sequential stopping threshold, validation failure, or committee recommendation."
        for r in reviews:
            if r.open_analysis and r.open_analysis.sections:
                for k in ("exit_criteria", "stopping_condition", "what_would_change_plan"):
                    if k in r.open_analysis.sections:
                        exit_criteria = r.open_analysis.sections[k]
        lines.append(f"- **Exit Criteria ('What would change your plan?')**: {exit_criteria}")
        lines.append(f"- **Sunk Cost (Runs)**: {n_runs} runs")
        lines.append(f"- **Cumulative Compute Duration**: {total_duration_s:.1f}s ({duration_hours:.2f} hours)")
        lines.append("")

        # Section 6: Escalation Status & Chair Disposition
        lines.append("## 6. Escalation Status & Chair Disposition")
        lines.append(f"- **Current Escalation Level**: **{current_level_name}**")
        if active_triggers:
            lines.append(f"- **Active Triggers**: {', '.join(active_triggers)}")
        else:
            lines.append("- **Active Triggers**: None")
        if snoozed_triggers:
            lines.append(f"- **Snoozed Triggers**: {', '.join(snoozed_triggers)}")

        if state.current_level == 3:
            lines.append("- **Human Chair Disposition**: ⚠️ **ACTION REQUIRED** — Human Committee Chair disposition is required to resolve or conclude this campaign. Autonomous agents cannot override.")
        elif state.current_level == 2:
            lines.append("- **Committee Disposition**: Active Committee Hold (Level 2). Independent review sign-off recommended before continuing.")
        elif state.current_level == 1:
            lines.append("- **Advisory Disposition**: Advisory Warning (Level 1). Review recommended; non-blocking.")
        else:
            lines.append("- **Human Chair Disposition**: Nominal trajectory; no chair intervention requested.")

        if escalations:
            recent_escs = escalations[-3:]
            lines.append(f"- **Recent Escalation History ({len(recent_escs)})**:")
            for e in recent_escs:
                lines.append(f"  - `{e.opened_at}`: Level {e.level} ({e.status}) by {e.opened_by_kind}: {e.trigger_reason}")
        lines.append("")

        # Section 7: Project Status Matrix Entry
        lines.append("## 7. Project Status Matrix Entry")
        lines.append("`PROJECT_STATUS.advisor.md` ('End / reconsider' column):")
        lines.append(f"```text\n{status_entry}\n```")
        lines.append("")

        return "\n".join(lines)
    finally:
        if owns_db and con is not None:
            con.close()
