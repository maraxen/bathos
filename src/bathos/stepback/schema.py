"""Step-Back Review and Escalation Data Model (spec 261027).

Provides Python dataclasses and enums for:
- ReviewRubricScore, OpenAnalysis, DissentRecord
- StepBackReview with cryptographic snapshot and signature binding
- EscalationRecord, EscalationState, StepBackVerdict
- Enums: ReviewPath, OpenAnalysisMode, Recommendation, EscalationLevel, EscalationStatus
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import IntEnum, StrEnum
from typing import Any


class ReviewPath(StrEnum):
    """Review pathway selection."""
    RUBRIC = "rubric"
    OPEN_ANALYSIS = "open_analysis"
    HYBRID = "hybrid"


class OpenAnalysisMode(StrEnum):
    """Cognitive modes for qualitative Path B inquiry."""
    ADVISOR_CONVO = "advisor_convo"
    PRE_MORTEM = "pre_mortem"
    SO_WHAT = "so_what"
    OUTSIDER = "outsider"
    STEELMAN = "steelman"

    @classmethod
    def normalize(cls, mode_str: str) -> OpenAnalysisMode:
        """Normalize mode strings including common aliases."""
        s = mode_str.lower().strip()
        if s in ("advisor", "advisor_convo", "advisor_conversation"):
            return cls.ADVISOR_CONVO
        if s in ("pre_mortem", "premortem"):
            return cls.PRE_MORTEM
        if s in ("so_what", "sowhat"):
            return cls.SO_WHAT
        if s in ("outsider", "outsider_read"):
            return cls.OUTSIDER
        if s in ("steelman", "steelman_the_stop", "steelman_stop"):
            return cls.STEELMAN
        raise ValueError(f"Unknown OpenAnalysisMode: {mode_str}")


class Recommendation(StrEnum):
    """Standard recommendations issued by reviews."""
    CONTINUE = "CONTINUE"
    PIVOT = "PIVOT"
    HOLD = "HOLD"
    ESCALATE = "ESCALATE"
    CONCLUDE_ABANDON = "CONCLUDE_ABANDON"


class EscalationLevel(IntEnum):
    """4-tier escalation hierarchy."""
    NOTICE = 0
    WARNING = 1
    HOLD = 2
    CHAIR = 3


class EscalationStatus(StrEnum):
    """Status lifecycle of an escalation record."""
    ACTIVE = "active"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"


@dataclass
class ReviewRubricScore:
    """Individual dimension score in Path A rubric evaluation."""
    dimension_id: str
    score: int  # 1, 2, 3, 4, 5
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewRubricScore:
        return cls(**data)


@dataclass
class OpenAnalysis:
    """Qualitative narrative sections for Path B cognitive modes."""
    mode: str  # 'advisor_convo', 'pre_mortem', 'so_what', 'outsider', 'steelman'
    sections: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OpenAnalysis:
        return cls(**data)


@dataclass
class DissentRecord:
    """Immutable minority dissent record preserved in committee reviews."""
    dissenting_reviewer_id: str
    minority_recommendation: str  # 'CONTINUE', 'PIVOT', 'HOLD', 'ESCALATE', 'CONCLUDE_ABANDON'
    minority_composite_score: float | None = None
    dissent_summary: str = ""
    cited_counter_evidence: list[str] = field(default_factory=list)
    majority_rebuttal: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DissentRecord:
        return cls(**data)


def compute_content_sha256(payload: Any) -> str:
    """Calculate deterministic SHA-256 over canonical JSON representation."""
    canonical_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


@dataclass
class StepBackReview:
    """Full step-back review record persisted to cool and warm tiers."""
    review_id: str  # rev_<12-hex>
    campaign_id: str
    project_slug: str
    created_at: str  # ISO-8601 UTC
    reviewer_id: str  # Authenticated identity string
    reviewer_kind: str  # 'human', 'agent', 'panel'
    is_independent: bool
    path_kind: str  # 'rubric', 'open_analysis', 'hybrid'
    input_snapshot_sha256: str  # Cryptographic hash of campaign state
    input_snapshot: str  # JSON string of input snapshot state
    recommendation: str  # 'CONTINUE', 'PIVOT', 'HOLD', 'ESCALATE', 'CONCLUDE_ABANDON'
    escalation_level: int  # 0, 1, 2, 3
    cited_evidence: list[str] = field(default_factory=list)
    rubric_id: str | None = None
    rubric_version: str | None = None
    rubric_sha256: str | None = None
    rubric_scores: dict[str, int | float] = field(default_factory=dict)
    composite_score: float | None = None
    open_analysis: OpenAnalysis | None = None
    dissents: list[DissentRecord] = field(default_factory=list)
    dispatched_at: str | None = None  # ISO-8601 UTC for agent telemetry
    signature_sha256: str | None = None  # Content hash of the review record
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.signature_sha256 is None:
            self.signature_sha256 = self.compute_signature()

    def compute_signature(self) -> str:
        """Compute SHA256 of review content (excluding signature_sha256 itself)."""
        d = self.to_dict()
        d.pop("signature_sha256", None)
        return compute_content_sha256(d)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StepBackReview:
        data_copy = dict(data)
        if "open_analysis" in data_copy and isinstance(data_copy["open_analysis"], dict):
            data_copy["open_analysis"] = OpenAnalysis.from_dict(data_copy["open_analysis"])
        if "dissents" in data_copy and isinstance(data_copy["dissents"], list):
            data_copy["dissents"] = [
                DissentRecord.from_dict(d) if isinstance(d, dict) else d
                for d in data_copy["dissents"]
            ]
        return cls(**data_copy)


@dataclass
class EscalationRecord:
    """Historical escalation event recorded in catalog."""
    escalation_id: str  # esc_<12-hex>
    campaign_id: str
    project_slug: str
    level: int  # 0, 1, 2, 3
    status: str  # 'active', 'resolved', 'superseded'
    opened_at: str  # ISO-8601 UTC
    opened_by_kind: str  # 'trigger', 'review', 'manual', 'chair'
    opened_by_review_id: str | None = None
    trigger_name: str | None = None  # 'repeated_marginal', 'near_bound', etc.
    trigger_reason: str = ""
    resolved_at: str | None = None
    resolved_by: str | None = None
    resolved_kind: str | None = None  # 'human', 'chair'
    resolution_note: str = ""
    deescalated_to: int | None = None
    review_epoch: int = 0
    pivot_anchor_run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EscalationRecord:
        return cls(**dict(data))


@dataclass
class EscalationState:
    """Current in-memory escalation status for a campaign."""
    campaign_id: str
    current_level: int = 0
    status: str = "nominal"  # 'nominal', 'warning', 'hold', 'escalated'
    active_escalation_id: str | None = None
    active_triggers: list[str] = field(default_factory=list)
    review_epoch: int = 0
    pivot_anchor_run_id: str | None = None
    latest_review_id: str | None = None
    latest_recommendation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EscalationState:
        return cls(**dict(data))


@dataclass
class StepBackVerdict:
    """Result of evaluating step-back constraints for a campaign."""
    ok: bool
    hold_active: bool
    escalation_level: int
    recommendation: str | None
    active_triggers: list[str]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StepBackVerdict:
        return cls(**dict(data))


def build_input_snapshot(
    campaign_id: str,
    evaluated_run_ids: list[str],
    latest_run_sha: str = "",
    sidecar_shas: dict[str, str] | None = None,
    git_head_commit: str = "",
    extra: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Build canonical input snapshot JSON string and its SHA-256 hash."""
    snapshot_dict: dict[str, Any] = {
        "campaign_id": campaign_id,
        "evaluated_run_ids": sorted(evaluated_run_ids),
        "latest_run_sha": latest_run_sha,
        "sidecar_shas": sidecar_shas or {},
        "git_head_commit": git_head_commit,
    }
    if extra:
        snapshot_dict.update(extra)
    snapshot_json = json.dumps(snapshot_dict, sort_keys=True, separators=(",", ":"))
    snapshot_sha = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
    return snapshot_json, snapshot_sha


@dataclass
class StepBackEvaluation:
    """Post-hoc downstream outcome evaluation of a step-back review."""
    evaluation_id: str  # eval_<12-hex>
    review_id: str
    campaign_id: str
    evaluated_at: str  # ISO-8601 UTC
    excess_spend_avoided: float = 0.0  # Estimated compute hours/units saved
    predictive_validity: bool = True  # True if recommendation matched subsequent trajectory
    trajectory_slope: float | None = None  # Subsequent slope if continued
    vacuous_pass_intercepted: bool = False  # Caught tautology/unfalsifiable check
    subsequent_runs_count: int = 0
    subsequent_outcomes: list[str] = field(default_factory=list)
    notes: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StepBackEvaluation:
        return cls(**dict(data))
