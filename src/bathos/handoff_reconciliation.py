"""Staged rollout for handoff reconciliation: advisory -> automatic (#1179).

Provides pure core types and functions for:
1. Evaluating handoff-vs-catalog agreement per record and in batches.
2. Tracking rolling agreement metrics over a bounded window.
3. Gating promotion from ``advisory`` to ``automatic`` mode once measured agreement
   meets configured thresholds and minimum sample counts, with safe fallback/demotion
   to ``advisory`` if rolling agreement drops or consecutive disagreements occur.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class ReconciliationMode(str, Enum):  # noqa: UP042 - match bathos str Enum convention for JSON serialization
    """Staged rollout mode for handoff-vs-catalog reconciliation."""

    ADVISORY = "advisory"
    AUTOMATIC = "automatic"


DEFAULT_COMPARISON_FIELDS: tuple[str, ...] = (
    "outcome",
    "status",
    "campaign_id",
    "sidecar_sha256",
    "claim_sha256",
)


@dataclass(frozen=True)
class RolloutPolicy:
    """Configuration gating promotion and fallback between advisory and automatic modes."""

    promotion_threshold: float = 0.95
    demotion_threshold: float = 0.90
    min_samples_for_promotion: int = 80
    min_samples_for_demotion: int = 5
    window_size: int = 100
    max_consecutive_disagreements: int = 3

    def __post_init__(self) -> None:
        if not (0.0 < self.demotion_threshold <= self.promotion_threshold <= 1.0):
            raise ValueError(
                "Thresholds must satisfy 0.0 < demotion_threshold <= promotion_threshold <= 1.0; "
                f"got demotion={self.demotion_threshold}, promotion={self.promotion_threshold}"
            )
        if self.min_samples_for_promotion < 1:
            raise ValueError(
                f"min_samples_for_promotion must be >= 1, got {self.min_samples_for_promotion}"
            )
        if self.min_samples_for_demotion < 1:
            raise ValueError(
                f"min_samples_for_demotion must be >= 1, got {self.min_samples_for_demotion}"
            )
        if self.window_size < 1:
            raise ValueError(f"window_size must be >= 1, got {self.window_size}")
        if self.min_samples_for_promotion > self.window_size:
            raise ValueError(
                f"min_samples_for_promotion ({self.min_samples_for_promotion}) must be <= "
                f"window_size ({self.window_size})"
            )
        if self.min_samples_for_demotion > self.window_size:
            raise ValueError(
                f"min_samples_for_demotion ({self.min_samples_for_demotion}) must be <= "
                f"window_size ({self.window_size})"
            )
        if self.max_consecutive_disagreements < 1:
            raise ValueError(
                "max_consecutive_disagreements must be >= 1, "
                f"got {self.max_consecutive_disagreements}"
            )


@dataclass(frozen=True)
class FieldDiscrepancy:
    """Single mismatched field between a handoff record and the catalog."""

    field_name: str
    handoff_value: Any
    catalog_value: Any
    reason: str


@dataclass(frozen=True)
class AgreementSample:
    """Measured agreement result for one handoff record compared against the catalog."""

    sample_id: str
    agreed: bool
    discrepancies: tuple[FieldDiscrepancy, ...] = ()
    timestamp: str = ""


@dataclass(frozen=True)
class AgreementMetrics:
    """Rolling and lifetime agreement summary."""

    total_samples: int
    window_samples: int
    agreed_count: int
    disagreed_count: int
    agreement_rate: float
    lifetime_agreement_rate: float
    consecutive_disagreements: int
    wilson_lower_bound: float = 0.0
    clean_streak: int = 0


@dataclass(frozen=True)
class RolloutTransition:
    """Decision produced by evaluating or updating the staged rollout gate."""

    previous_mode: ReconciliationMode
    new_mode: ReconciliationMode
    transition: Literal["promoted", "demoted", "unchanged"]
    reason: str
    metrics: AgreementMetrics
    auto_apply_allowed: bool
    advisories: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconciliationOutcome:
    """Result of reconciling a batch of handoff records against catalog state."""

    mode: ReconciliationMode
    auto_applied: bool
    transition: RolloutTransition
    samples: tuple[AgreementSample, ...]
    advisories: tuple[str, ...] = ()


def _values_match(a: Any, b: Any, numeric_tolerance: float) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return abs(float(a) - float(b)) <= numeric_tolerance
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_values_match(a[k], b[k], numeric_tolerance) for k in a)
    return a == b


def compare_handoff_to_catalog(
    handoff_record: Mapping[str, Any],
    catalog_record: Mapping[str, Any] | None,
    *,
    sample_id: str | None = None,
    fields: Sequence[str] | None = None,
    numeric_tolerance: float = 1e-6,
    timestamp: str = "",
) -> AgreementSample:
    """Compare one handoff record against its corresponding catalog record."""
    resolved_id = (
        sample_id
        or str(handoff_record.get("run_id") or handoff_record.get("entity_id") or handoff_record.get("id") or "unknown")
    )
    if catalog_record is None:
        return AgreementSample(
            sample_id=resolved_id,
            agreed=False,
            discrepancies=(
                FieldDiscrepancy(
                    field_name="__record__",
                    handoff_value=resolved_id,
                    catalog_value=None,
                    reason="missing_in_catalog",
                ),
            ),
            timestamp=timestamp,
        )

    if fields is not None:
        check_fields = tuple(fields)
    else:
        present = [k for k in DEFAULT_COMPARISON_FIELDS if k in handoff_record or k in catalog_record]
        if "metrics" in handoff_record or "metrics" in catalog_record:
            present.append("metrics")
        check_fields = tuple(present) if present else DEFAULT_COMPARISON_FIELDS

    discrepancies: list[FieldDiscrepancy] = []
    for key in check_fields:
        if key not in handoff_record and key not in catalog_record:
            continue
        if key not in handoff_record:
            discrepancies.append(
                FieldDiscrepancy(
                    field_name=key,
                    handoff_value=None,
                    catalog_value=catalog_record.get(key),
                    reason="missing_in_handoff",
                )
            )
            continue
        if key not in catalog_record:
            discrepancies.append(
                FieldDiscrepancy(
                    field_name=key,
                    handoff_value=handoff_record.get(key),
                    catalog_value=None,
                    reason="missing_in_catalog_field",
                )
            )
            continue
        h_val = handoff_record[key]
        c_val = catalog_record[key]
        if not _values_match(h_val, c_val, numeric_tolerance):
            discrepancies.append(
                FieldDiscrepancy(
                    field_name=key,
                    handoff_value=h_val,
                    catalog_value=c_val,
                    reason="value_mismatch",
                )
            )

    return AgreementSample(
        sample_id=resolved_id,
        agreed=len(discrepancies) == 0,
        discrepancies=tuple(discrepancies),
        timestamp=timestamp,
    )


def evaluate_agreement_rate(samples: Sequence[AgreementSample]) -> float:
    """Compute agreement rate in ``[0.0, 1.0]`` for a sequence of samples."""
    if not samples:
        return 0.0
    agreed = sum(1 for sample in samples if sample.agreed)
    return agreed / len(samples)


def wilson_score_lower_bound(
    agreed_count: int,
    total_count: int,
    *,
    z: float = 1.96,
) -> float:
    """Compute the one-sided Wilson score lower bound (95% default, z=1.96)."""
    if total_count <= 0:
        return 0.0
    if agreed_count <= 0:
        return 0.0
    p_hat = min(1.0, agreed_count / total_count)
    z2 = z * z
    denom = 1.0 + z2 / total_count
    center = p_hat + z2 / (2.0 * total_count)
    radius = z * math.sqrt((p_hat * (1.0 - p_hat) + z2 / (4.0 * total_count)) / total_count)
    return max(0.0, (center - radius) / denom)


def compute_agreement_metrics(
    samples: Sequence[AgreementSample],
    *,
    window_size: int = 50,
) -> AgreementMetrics:
    """Compute rolling-window and lifetime agreement metrics from ordered samples."""
    if window_size < 1:
        raise ValueError(f"window_size must be >= 1, got {window_size}")
    total = len(samples)
    window = samples[-window_size:] if total > window_size else samples
    agreed_count = sum(1 for s in window if s.agreed)
    disagreed_count = len(window) - agreed_count
    rolling_rate = (agreed_count / len(window)) if window else 0.0
    lifetime_rate = (sum(1 for s in samples if s.agreed) / total) if total else 0.0
    wilson_lb = wilson_score_lower_bound(agreed_count, len(window))

    consecutive_disagreements = 0
    for sample in reversed(samples):
        if sample.agreed:
            break
        consecutive_disagreements += 1

    clean_streak = 0
    for sample in reversed(samples):
        if not sample.agreed:
            break
        clean_streak += 1

    return AgreementMetrics(
        total_samples=total,
        window_samples=len(window),
        agreed_count=agreed_count,
        disagreed_count=disagreed_count,
        agreement_rate=rolling_rate,
        lifetime_agreement_rate=lifetime_rate,
        consecutive_disagreements=consecutive_disagreements,
        wilson_lower_bound=wilson_lb,
        clean_streak=clean_streak,
    )


def evaluate_rollout_gate(
    current_mode: ReconciliationMode,
    metrics: AgreementMetrics,
    policy: RolloutPolicy,
    *,
    samples_since_demotion: int | None = None,
) -> RolloutTransition:
    """Evaluate whether staged rollout mode should promote, demote, or stay unchanged."""
    advisories: list[str] = []

    if current_mode == ReconciliationMode.ADVISORY:
        if metrics.window_samples < policy.min_samples_for_promotion:
            needed = policy.min_samples_for_promotion - metrics.window_samples
            reason = (
                f"staying in advisory: {metrics.window_samples}/"
                f"{policy.min_samples_for_promotion} samples collected ({needed} more required)"
            )
            return RolloutTransition(
                previous_mode=current_mode,
                new_mode=ReconciliationMode.ADVISORY,
                transition="unchanged",
                reason=reason,
                metrics=metrics,
                auto_apply_allowed=False,
                advisories=(reason,),
            )

        required_clean_streak = min(
            policy.min_samples_for_promotion, policy.min_samples_for_demotion
        )
        effective_clean_streak = (
            metrics.window_samples
            if metrics.disagreed_count == 0 and metrics.consecutive_disagreements == 0
            else (0 if metrics.consecutive_disagreements > 0 else metrics.clean_streak)
        )
        if (
            samples_since_demotion is not None
            and samples_since_demotion < policy.min_samples_for_demotion
        ):
            reason = (
                f"staying in advisory: demotion cooldown active "
                f"({samples_since_demotion}/{policy.min_samples_for_demotion} new samples)"
            )
            advisories.append(reason)
            return RolloutTransition(
                previous_mode=current_mode,
                new_mode=ReconciliationMode.ADVISORY,
                transition="unchanged",
                reason=reason,
                metrics=metrics,
                auto_apply_allowed=False,
                advisories=tuple(advisories),
            )

        if metrics.consecutive_disagreements > 0 or effective_clean_streak < required_clean_streak:
            reason = (
                f"staying in advisory: clean streak {effective_clean_streak} "
                f"< required {required_clean_streak} after disagreement"
            )
            advisories.append(reason)
            return RolloutTransition(
                previous_mode=current_mode,
                new_mode=ReconciliationMode.ADVISORY,
                transition="unchanged",
                reason=reason,
                metrics=metrics,
                auto_apply_allowed=False,
                advisories=tuple(advisories),
            )

        wilson_lb = wilson_score_lower_bound(metrics.agreed_count, metrics.window_samples)
        if wilson_lb >= policy.promotion_threshold and metrics.agreement_rate >= policy.promotion_threshold:
            reason = (
                f"promoted advisory -> automatic: wilson 95% lower bound {wilson_lb:.3f} "
                f"(raw {metrics.agreement_rate:.3f}) >= {policy.promotion_threshold:.3f} "
                f"across {metrics.window_samples} samples"
            )
            return RolloutTransition(
                previous_mode=current_mode,
                new_mode=ReconciliationMode.AUTOMATIC,
                transition="promoted",
                reason=reason,
                metrics=metrics,
                auto_apply_allowed=True,
                advisories=(),
            )
        reason = (
            f"staying in advisory: wilson 95% lower bound {wilson_lb:.3f} "
            f"(raw {metrics.agreement_rate:.3f}) < promotion_threshold {policy.promotion_threshold:.3f}"
        )
        advisories.append(reason)
        return RolloutTransition(
            previous_mode=current_mode,
            new_mode=ReconciliationMode.ADVISORY,
            transition="unchanged",
            reason=reason,
            metrics=metrics,
            auto_apply_allowed=False,
            advisories=tuple(advisories),
        )

    # Currently in AUTOMATIC mode: check safety fallbacks / demotion gates.
    if metrics.consecutive_disagreements >= policy.max_consecutive_disagreements:
        reason = (
            f"demoted automatic -> advisory: {metrics.consecutive_disagreements} consecutive "
            f"disagreements reached circuit-breaker limit ({policy.max_consecutive_disagreements})"
        )
        return RolloutTransition(
            previous_mode=current_mode,
            new_mode=ReconciliationMode.ADVISORY,
            transition="demoted",
            reason=reason,
            metrics=metrics,
            auto_apply_allowed=False,
            advisories=(reason,),
        )

    if (
        metrics.window_samples >= policy.min_samples_for_demotion
        and metrics.agreement_rate < policy.demotion_threshold
    ):
        reason = (
            f"demoted automatic -> advisory: rolling agreement {metrics.agreement_rate:.3f} "
            f"< demotion_threshold {policy.demotion_threshold:.3f}"
        )
        return RolloutTransition(
            previous_mode=current_mode,
            new_mode=ReconciliationMode.ADVISORY,
            transition="demoted",
            reason=reason,
            metrics=metrics,
            auto_apply_allowed=False,
            advisories=(reason,),
        )

    reason = (
        f"remaining in automatic: rolling agreement {metrics.agreement_rate:.3f} "
        f">= demotion_threshold {policy.demotion_threshold:.3f}"
    )
    return RolloutTransition(
        previous_mode=current_mode,
        new_mode=ReconciliationMode.AUTOMATIC,
        transition="unchanged",
        reason=reason,
        metrics=metrics,
        auto_apply_allowed=True,
        advisories=(),
    )


@dataclass
class HandoffReconciliationTracker:
    """Stateful tracker for rolling handoff-vs-catalog agreement and rollout gating."""

    policy: RolloutPolicy = field(default_factory=RolloutPolicy)
    mode: ReconciliationMode = ReconciliationMode.ADVISORY
    history: list[AgreementSample] = field(default_factory=list)
    last_demotion_sample_index: int | None = None

    def metrics(self) -> AgreementMetrics:
        """Compute current rolling and lifetime agreement metrics."""
        return compute_agreement_metrics(self.history, window_size=self.policy.window_size)

    def evaluate_gate(self) -> RolloutTransition:
        """Evaluate rollout transition for the current state and update mode."""
        metrics = self.metrics()
        samples_since_demotion = (
            len(self.history) - self.last_demotion_sample_index
            if self.last_demotion_sample_index is not None
            else None
        )
        decision = evaluate_rollout_gate(
            self.mode,
            metrics,
            self.policy,
            samples_since_demotion=samples_since_demotion,
        )
        self.mode = decision.new_mode
        if decision.transition == "demoted":
            self.last_demotion_sample_index = len(self.history)
        elif decision.transition == "promoted":
            self.last_demotion_sample_index = None
        return decision

    def record_sample(self, sample: AgreementSample) -> RolloutTransition:
        """Append a measured agreement sample and evaluate the rollout gate."""
        self.history.append(sample)
        return self.evaluate_gate()

    def record_comparison(
        self,
        handoff_record: Mapping[str, Any],
        catalog_record: Mapping[str, Any] | None,
        *,
        sample_id: str | None = None,
        fields: Sequence[str] | None = None,
        numeric_tolerance: float = 1e-6,
        timestamp: str = "",
    ) -> tuple[AgreementSample, RolloutTransition]:
        """Compare one handoff record to catalog, record it, and evaluate the gate."""
        sample = compare_handoff_to_catalog(
            handoff_record,
            catalog_record,
            sample_id=sample_id,
            fields=fields,
            numeric_tolerance=numeric_tolerance,
            timestamp=timestamp,
        )
        transition = self.record_sample(sample)
        return sample, transition

    def reconcile_batch(
        self,
        handoff_records: Iterable[Mapping[str, Any]],
        catalog_by_id: Mapping[str, Mapping[str, Any]],
        *,
        id_key: str = "run_id",
        fields: Sequence[str] | None = None,
        numeric_tolerance: float = 1e-6,
        timestamp: str = "",
    ) -> ReconciliationOutcome:
        """Evaluate a batch of handoff records against catalog and gate automatic reconciliation."""
        samples: list[AgreementSample] = []
        advisories: list[str] = []
        for record in handoff_records:
            rec_id = str(record.get(id_key) or record.get("entity_id") or record.get("id") or "")
            catalog_rec = catalog_by_id.get(rec_id) if rec_id else None
            sample = compare_handoff_to_catalog(
                record,
                catalog_rec,
                sample_id=rec_id or "unknown",
                fields=fields,
                numeric_tolerance=numeric_tolerance,
                timestamp=timestamp,
            )
            samples.append(sample)
            self.history.append(sample)
            if not sample.agreed:
                desc = ", ".join(
                    f"{d.field_name}: handoff={d.handoff_value!r} vs catalog={d.catalog_value!r} ({d.reason})"
                    for d in sample.discrepancies
                )
                advisories.append(f"{sample.sample_id}: {desc}")

        transition = self.evaluate_gate()
        advisories.extend(transition.advisories)
        auto_applied = (
            transition.auto_apply_allowed and all(sample.agreed for sample in samples)
        )
        return ReconciliationOutcome(
            mode=self.mode,
            auto_applied=auto_applied,
            transition=transition,
            samples=tuple(samples),
            advisories=tuple(advisories),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize tracker state to a JSON-compatible dictionary."""
        return {
            "mode": self.mode.value,
            "last_demotion_sample_index": self.last_demotion_sample_index,
            "policy": {
                "promotion_threshold": self.policy.promotion_threshold,
                "demotion_threshold": self.policy.demotion_threshold,
                "min_samples_for_promotion": self.policy.min_samples_for_promotion,
                "min_samples_for_demotion": self.policy.min_samples_for_demotion,
                "window_size": self.policy.window_size,
                "max_consecutive_disagreements": self.policy.max_consecutive_disagreements,
            },
            "history": [
                {
                    "sample_id": s.sample_id,
                    "agreed": s.agreed,
                    "timestamp": s.timestamp,
                    "discrepancies": [
                        {
                            "field_name": d.field_name,
                            "handoff_value": d.handoff_value,
                            "catalog_value": d.catalog_value,
                            "reason": d.reason,
                        }
                        for d in s.discrepancies
                    ],
                }
                for s in self.history
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> HandoffReconciliationTracker:
        """Restore tracker state from :meth:`to_dict` output with strict validation."""
        if not isinstance(payload, Mapping):
            raise ValueError(f"Tracker payload must be a mapping, got {type(payload).__name__}")

        raw_policy_obj = payload.get("policy")
        if raw_policy_obj is None:
            raw_policy: Mapping[str, Any] = {}
        elif isinstance(raw_policy_obj, Mapping):
            raw_policy = raw_policy_obj
        else:
            raise ValueError(
                f"Tracker payload 'policy' must be a mapping, got {type(raw_policy_obj).__name__}"
            )

        def _req_float(key: str, default: float) -> float:
            val = raw_policy.get(key, default)
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(f"policy.{key} must be a number, got {val!r}")
            fval = float(val)
            if not math.isfinite(fval):
                raise ValueError(f"policy.{key} must be finite, got {val!r}")
            return fval

        def _req_int(key: str, default: int) -> int:
            val = raw_policy.get(key, default)
            if isinstance(val, bool) or not isinstance(val, int):
                raise ValueError(f"policy.{key} must be an integer, got {val!r}")
            return val

        policy = RolloutPolicy(
            promotion_threshold=_req_float("promotion_threshold", 0.95),
            demotion_threshold=_req_float("demotion_threshold", 0.90),
            min_samples_for_promotion=_req_int("min_samples_for_promotion", 80),
            min_samples_for_demotion=_req_int("min_samples_for_demotion", 5),
            window_size=_req_int("window_size", 100),
            max_consecutive_disagreements=_req_int("max_consecutive_disagreements", 3),
        )

        raw_mode = payload.get("mode", ReconciliationMode.ADVISORY.value)
        if not isinstance(raw_mode, str):
            raise ValueError(f"Tracker payload 'mode' must be a string, got {raw_mode!r}")
        try:
            persisted_mode = ReconciliationMode(raw_mode)
        except ValueError as exc:
            raise ValueError(f"Invalid reconciliation mode {raw_mode!r}") from exc

        raw_history = payload.get("history")
        if raw_history is None:
            raw_history = []
        elif not isinstance(raw_history, (list, tuple)):
            raise ValueError(
                f"Tracker payload 'history' must be a list, got {type(raw_history).__name__}"
            )

        history: list[AgreementSample] = []
        for idx, raw_sample in enumerate(raw_history):
            if not isinstance(raw_sample, Mapping):
                raise ValueError(f"history[{idx}] must be a mapping, got {type(raw_sample).__name__}")
            sample_id = raw_sample.get("sample_id")
            if not isinstance(sample_id, str) or not sample_id:
                raise ValueError(f"history[{idx}].sample_id must be a non-empty string, got {sample_id!r}")
            agreed = raw_sample.get("agreed")
            if not isinstance(agreed, bool):
                raise ValueError(f"history[{idx}].agreed must be a bool, got {agreed!r}")
            timestamp = raw_sample.get("timestamp", "")
            if not isinstance(timestamp, str):
                raise ValueError(f"history[{idx}].timestamp must be a string, got {timestamp!r}")
            raw_discs = raw_sample.get("discrepancies")
            if raw_discs is None:
                raw_discs = []
            elif not isinstance(raw_discs, (list, tuple)):
                raise ValueError(
                    f"history[{idx}].discrepancies must be a list, got {type(raw_discs).__name__}"
                )
            discs_list: list[FieldDiscrepancy] = []
            for d_idx, d in enumerate(raw_discs):
                if not isinstance(d, Mapping):
                    raise ValueError(
                        f"history[{idx}].discrepancies[{d_idx}] must be a mapping"
                    )
                field_name = d.get("field_name")
                if not isinstance(field_name, str) or not field_name:
                    raise ValueError(
                        f"history[{idx}].discrepancies[{d_idx}].field_name must be a non-empty string"
                    )
                reason = d.get("reason", "")
                if not isinstance(reason, str):
                    raise ValueError(
                        f"history[{idx}].discrepancies[{d_idx}].reason must be a string"
                    )
                discs_list.append(
                    FieldDiscrepancy(
                        field_name=field_name,
                        handoff_value=d.get("handoff_value"),
                        catalog_value=d.get("catalog_value"),
                        reason=reason,
                    )
                )
            history.append(
                AgreementSample(
                    sample_id=sample_id,
                    agreed=agreed,
                    discrepancies=tuple(discs_list),
                    timestamp=timestamp,
                )
            )

        raw_demoted_idx = payload.get("last_demotion_sample_index")
        if raw_demoted_idx is not None:
            if isinstance(raw_demoted_idx, bool) or not isinstance(raw_demoted_idx, int):
                raise ValueError(
                    f"last_demotion_sample_index must be an integer or null, got {raw_demoted_idx!r}"
                )
            if raw_demoted_idx < 0 or raw_demoted_idx > len(history):
                raise ValueError(
                    f"last_demotion_sample_index ({raw_demoted_idx}) out of bounds for history length {len(history)}"
                )

        # Replay history to verify whether AUTOMATIC mode is actually earned by qualifying history.
        replay = cls(policy=policy, mode=ReconciliationMode.ADVISORY)
        for sample in history:
            replay.record_sample(sample)

        if persisted_mode == ReconciliationMode.AUTOMATIC:
            mode = replay.mode
            last_demotion_idx = (
                replay.last_demotion_sample_index
                if mode == ReconciliationMode.ADVISORY
                else None
            )
        else:
            mode = ReconciliationMode.ADVISORY
            last_demotion_idx = (
                raw_demoted_idx
                if raw_demoted_idx is not None
                else replay.last_demotion_sample_index
            )

        return cls(
            policy=policy,
            mode=mode,
            history=history,
            last_demotion_sample_index=last_demotion_idx,
        )
