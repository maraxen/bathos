"""Escalation Ladder, State Machine, and Human Chair Invariance Engine (spec 261027).

Implements:
- 4-Tier Escalation Ladder (Levels 0-3)
- Validated state transitions with direct escalation paths (0->2, 0->3, 1->3)
- Inviolable Human Chair Invariance: unconditional agent override refusal
- TTY and Chair Token verification gates
- Decision D1 posture helpers (execution advisory vs conclusion downgrade)
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bathos.errors import (
    EscalationLevelInvalidError,
    StepBackAgentOverrideRefusedError,
    StepBackUnauthorizedChairError,
)
from bathos.stepback.schema import (
    EscalationRecord,
    EscalationState,
)

AGENT_ENV_VARS = (
    "BTH_AGENT_MODE",
    "CLAUDE_AGENT",
    "CLAUDE_AGENT_ID",
    "AGENT_NAME",
    "SUBAGENT_ID",
    "ANTIGRAVITY_AGENT",
)

CHAIR_TOKEN_PATH = Path.home() / ".bth" / "chair_token"


def is_agent_environment() -> bool:
    """Detect whether caller is executing within an autonomous agent session."""
    return any(bool(os.environ.get(var)) for var in AGENT_ENV_VARS)


def verify_chair_token(token: str | None = None) -> bool:
    """Verify chair token against ~/.bth/chair_token."""
    if not CHAIR_TOKEN_PATH.is_file():
        return False
    try:
        expected = CHAIR_TOKEN_PATH.read_text().strip()
        if not expected:
            return False
        return bool(token and token.strip() == expected)
    except Exception:
        return False


def validate_level(level: int) -> int:
    """Validate that escalation level is within 0-3 integer range."""
    if not isinstance(level, int) or level < 0 or level > 3:
        raise EscalationLevelInvalidError(f"Invalid escalation level: {level}. Must be 0, 1, 2, or 3.")
    return level


def can_transition(
    from_level: int,
    to_level: int,
    is_human_chair: bool = False,
    is_agent: bool = False,
) -> tuple[bool, str]:
    """Validate whether an escalation transition from -> to is permitted.

    Transition Matrix:
    - 0 -> 1: Permitted (single trigger)
    - 0 -> 2: Permitted (direct escalation: streak >= 3, multiple triggers, floor veto)
    - 0 -> 3: Permitted (direct escalation: severe confound / direct referral)
    - 1 -> 0: Permitted (3 passes or approved review)
    - 1 -> 2: Permitted (multiple triggers, streak >= 3, low composite, dissent)
    - 1 -> 3: Permitted (direct escalation: severe unmitigated confound)
    - 2 -> 1: Permitted (independent review approving CONTINUE or PIVOT)
    - 2 -> 3: Permitted (ignored > 7d or >= 3 runs, fatal confound)
    - 3 -> 0/1/2: Permitted ONLY by HUMAN CHAIR (Agents unconditionally forbidden)
    """
    validate_level(from_level)
    validate_level(to_level)

    # Identical level is a no-op / update
    if from_level == to_level:
        return True, "No-op transition"

    # De-escalation from Level 3 strictly requires human chair
    if from_level == 3 and to_level < 3:
        if is_agent:
            return False, "Autonomous agents cannot de-escalate Level 3 Human Chair escalations"
        if not is_human_chair:
            return False, "De-escalation from Level 3 requires human chair authorization"
        return True, "Human chair de-escalation"

    # Escalating to Level 3
    if to_level == 3:
        # System, human, or committee can escalate to Level 3
        return True, "Escalation to Level 3"

    # Moving between 0, 1, 2
    if from_level in (0, 1) and to_level in (1, 2):
        return True, "Escalation to Warning or Hold"
    if from_level == 2 and to_level == 1:
        return True, "De-escalation from Hold to Warning via approved review"
    if from_level == 1 and to_level == 0:
        return True, "De-escalation from Warning to Nominal"
    if from_level == 2 and to_level == 0:
        if is_human_chair or not is_agent:
            return True, "Direct de-escalation from Hold to Nominal"
        return True, "De-escalation from Hold to Nominal"

    return True, "Permitted transition"


def transition_escalation(
    state: EscalationState,
    target_level: int,
    reason: str,
    opened_by_kind: str = "system",
    reviewer_id: str | None = None,
    as_human_chair: bool = False,
    chair_token: str | None = None,
    interactive_tty: bool | None = None,
) -> tuple[EscalationState, EscalationRecord]:
    """Execute a state transition for campaign escalation state.

    Enforces Human Chair Invariance when transitioning from Level 3.
    """
    target_level = validate_level(target_level)
    from_level = state.current_level

    agent_detected = is_agent_environment()

    # If de-escalating from Level 3, apply strict human chair gates
    if from_level == 3 and target_level < 3:
        if agent_detected:
            raise StepBackAgentOverrideRefusedError(
                "Autonomous agents are strictly forbidden from overriding or resolving Level 3 Human Chair escalations"
            )

        tty_ok = sys.stdin.isatty() if interactive_tty is None else interactive_tty
        token_ok = verify_chair_token(chair_token)

        if not as_human_chair and not tty_ok and not token_ok:
            raise StepBackUnauthorizedChairError(
                "Human chair resolution requires interactive TTY CLI or valid chair token"
            )
        if as_human_chair and not tty_ok and not token_ok:
            raise StepBackUnauthorizedChairError(
                "Cannot claim human chair authority without interactive TTY or valid chair token"
            )

    permitted, msg = can_transition(
        from_level,
        target_level,
        is_human_chair=as_human_chair,
        is_agent=agent_detected,
    )
    if not permitted:
        if agent_detected and from_level == 3:
            raise StepBackAgentOverrideRefusedError(msg)
        raise StepBackUnauthorizedChairError(msg)

    # Determine status string
    status_map = {0: "nominal", 1: "warning", 2: "hold", 3: "escalated"}
    now_iso = datetime.now(UTC).isoformat()
    esc_id = f"esc_{os.urandom(6).hex()}"

    esc_record = EscalationRecord(
        escalation_id=esc_id,
        campaign_id=state.campaign_id,
        project_slug="",
        level=target_level,
        status="active" if target_level > 0 else "resolved",
        opened_at=now_iso,
        opened_by_kind=opened_by_kind,
        trigger_reason=reason,
        review_epoch=state.review_epoch,
        pivot_anchor_run_id=state.pivot_anchor_run_id,
    )

    # If de-escalating, record resolution on old state
    if from_level > target_level:
        esc_record.deescalated_to = target_level
        esc_record.resolved_at = now_iso
        esc_record.resolved_by = reviewer_id or ("human_chair" if as_human_chair else "reviewer")
        esc_record.resolved_kind = "chair" if as_human_chair else "human"
        esc_record.resolution_note = reason

    # Update state
    state.current_level = target_level
    state.status = status_map[target_level]
    if target_level > 0:
        state.active_escalation_id = esc_id
    else:
        state.active_escalation_id = None
        state.active_triggers = []

    return state, esc_record


def get_execution_advisory(
    campaign_id: str,
    state: EscalationState,
) -> dict[str, Any] | None:
    """Generate advisory warning banner or blocking error per Decision D1.

    Returns dict with banner text and whether execution should be blocked.
    """
    if state.current_level == 0:
        return None

    level_names = {1: "Advisory Warning", 2: "Committee Hold", 3: "Human Chair Escalation"}
    level_name = level_names.get(state.current_level, f"Level {state.current_level}")

    banner = (
        f"┌────────────────────────────────────────────────────────────────────────┐\n"
        f"│ WARNING: Campaign '{campaign_id}' is at Step-Back {level_name.upper()} (Level {state.current_level}). │\n"
        f"│ Active Triggers: {', '.join(state.active_triggers) or 'Manual/Review'}                           │\n"
        f"│ Action: Conduct independent step-back review via `bth stepback review`.│\n"
        f"└────────────────────────────────────────────────────────────────────────┘"
    )

    # Opt-in blocking via BTH_STEP_BACK_ENFORCE=1
    enforce = os.environ.get("BTH_STEP_BACK_ENFORCE", "").strip().lower() in ("1", "true", "yes")

    return {
        "level": state.current_level,
        "banner": banner,
        "blocked": enforce and state.current_level >= 2,
    }


def check_conclude_posture(
    campaign: Any,
    state: EscalationState,
    force_verdict: bool = False,
) -> tuple[bool, str | None, str | None]:
    """Evaluate Decision D1 posture at `bth campaign conclude`.

    Returns:
    (should_downgrade, downgraded_verdict, stepback_status)
    """
    if state.current_level < 2:
        return False, None, "nominal"

    mode = "confirmatory"
    if isinstance(campaign, dict):
        mode = campaign.get("mode", "confirmatory")
    else:
        mode = getattr(campaign, "mode", "confirmatory")

    # Exploration campaigns only emit warning, no automatic conclusion downgrade
    if mode == "exploration":
        return False, None, "warning_unaddressed"

    # Confirmatory / sequential campaigns:
    if force_verdict:
        # Bypassed via force
        return False, None, "bypassed"

    # Default Decision D1: downgrade verdict to 'confounded'
    return True, "confounded", "held"
