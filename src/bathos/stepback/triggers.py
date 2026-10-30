"""System-Evaluated Event Trigger Engine (spec 261027).

Implements 5 automated event triggers with mathematical guard clauses:
1. Repeated Marginal Outcomes (T_repeated_marginal)
2. Near-Bound Hits / Boundary Proximity (T_near_bound)
3. Outcome-Entropy Collapse (T_entropy_collapse)
4. Metric Plateaus / Moving Slope & Variance Decay (T_plateau)
5. Campaign Staleness / Abandonment & Wheel-Spinning (T_stale)
"""

from __future__ import annotations

import contextlib
import math
from collections import Counter
from datetime import UTC, datetime
from typing import Any


def _parse_iso_utc(ts: Any) -> datetime | None:
    """Parse ISO-8601 string or numeric timestamp to UTC datetime."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=UTC)
        return ts.astimezone(UTC)
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=UTC)
    if isinstance(ts, str):
        try:
            # Handle ISO string with trailing Z or offset
            s = ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except Exception:
            return None
    return None


def _get_item_attr(item: Any, key: str, default: Any = None) -> Any:
    """Extract attribute from dict or object."""
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _filter_active_runs(runs: list[Any], anchor_run_id: str | None) -> list[Any]:
    """Filter runs executed strictly after the pivot anchor run id."""
    if not anchor_run_id:
        return list(runs)
    anchor_idx = -1
    for i, r in enumerate(runs):
        r_id = _get_item_attr(r, "run_id") or _get_item_attr(r, "id") or _get_item_attr(r, "uuid")
        if r_id == anchor_run_id:
            anchor_idx = i
            break
    if anchor_idx >= 0:
        return runs[anchor_idx + 1:]
    return list(runs)


def _resolve_numeric_metric(run: Any, target_metric: str | None = None, sidecar: dict[str, Any] | None = None) -> float | None:
    """Resolve a primary numeric scalar metric from a run record."""
    # 1. If target_metric is provided, search for it
    candidates = []
    if target_metric:
        candidates.append(target_metric)
    if sidecar:
        stepback_cfg = sidecar.get("stepback", {})
        if "target_metric" in stepback_cfg:
            candidates.append(stepback_cfg["target_metric"])
        benchmark_cfg = sidecar.get("benchmark", {})
        if "metric" in benchmark_cfg:
            candidates.append(benchmark_cfg["metric"])
        result_schema = sidecar.get("result_schema", {})
        # Counts and seeds are not metrics: only an unambiguous single float column is a default.
        float_cols = [k for k, v in result_schema.items() if str(v).lower() in ("float", "double")]
        if len(float_cols) == 1:
            candidates.append(float_cols[0])

    # Check candidates first
    results = _get_item_attr(run, "results") or {}
    metadata = _get_item_attr(run, "metadata") or {}
    outcomes = _get_item_attr(run, "outcomes") or {}

    for cand in candidates:
        for source in (results, metadata, outcomes, run if isinstance(run, dict) else {}):
            val = source.get(cand) if isinstance(source, dict) else getattr(source, cand, None)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass

    # No declared or unambiguous metric: return None rather than guess the first numeric column.

    # Try root level fields
    for field_name in ("metric", "score", "value", "primary_metric"):
        val = _get_item_attr(run, field_name)
        if val is not None and not isinstance(val, bool):
            try:
                return float(val)
            except (ValueError, TypeError):
                pass

    return None


def evaluate_repeated_marginal(
    runs: list[Any],
    window_size: int = 5,
    min_streak: int = 3,
    freq_threshold: float = 0.60,
    anchor_run_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate repeated marginal outcomes trigger (T_repeated_marginal).

    Fires if N >= 3 and:
    streak >= min_streak (3) OR (|R_active| >= window_size and window_freq >= freq_threshold (0.60)).
    """
    active_runs = _filter_active_runs(runs, anchor_run_id)
    n = len(active_runs)

    if n < 3:
        return {
            "trigger": "repeated_marginal",
            "fired": False,
            "reason": "insufficient_runs",
            "run_count": n,
            "streak": 0,
            "frequency": 0.0,
            "window_size": window_size,
        }

    # Extract outcome labels
    outcomes: list[str] = []
    run_ids: list[str] = []
    for r in active_runs:
        out = _get_item_attr(r, "outcome")
        if out is None:
            outcomes_dict = _get_item_attr(r, "outcomes")
            if isinstance(outcomes_dict, dict):
                out = outcomes_dict.get("verdict") or outcomes_dict.get("label")
        out_str = str(out).lower().strip() if out is not None else ""
        outcomes.append(out_str)
        rid = _get_item_attr(r, "run_id") or _get_item_attr(r, "id") or f"run_{len(run_ids)}"
        run_ids.append(str(rid))

    # Calculate recent marginal streak
    streak = 0
    for out in reversed(outcomes):
        if out == "marginal":
            streak += 1
        else:
            break

    # Calculate window frequency over last min(n, window_size) runs
    w_k = outcomes[-window_size:] if n >= window_size else outcomes
    marginal_count = sum(1 for o in w_k if o == "marginal")
    freq = marginal_count / len(w_k) if w_k else 0.0

    fired = False
    reason = None
    offending_run_ids: list[str] = []

    if streak >= min_streak:
        fired = True
        reason = "streak_exceeded"
        offending_run_ids = run_ids[-streak:]
    elif n >= window_size and freq >= freq_threshold:
        fired = True
        reason = "frequency_exceeded"
        # runs in window that are marginal
        offending_run_ids = [run_ids[-len(w_k) + i] for i, o in enumerate(w_k) if o == "marginal"]

    return {
        "trigger": "repeated_marginal",
        "fired": fired,
        "reason": reason,
        "streak": streak,
        "frequency": round(freq, 4),
        "window_size": window_size,
        "run_count": n,
        "offending_run_ids": offending_run_ids,
    }


def evaluate_near_bound_hits(
    runs: list[Any],
    window_size: int = 5,
    bound_proximity: float = 1.0,
    min_hits: int = 2,
    anchor_run_id: str | None = None,
    bound: float | None = None,
    target_metric: str | None = None,
    epsilon: float | None = None,
    sidecar: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate boundary proximity / near-bound hits trigger (T_near_bound).

    Fires if N >= 3 and:
    near_bound_hits >= min_hits (2) OR (N >= 1 and d_N <= 0.25).
    """
    active_runs = _filter_active_runs(runs, anchor_run_id)
    n = len(active_runs)

    if n < 3:
        return {
            "trigger": "near_bound",
            "fired": False,
            "reason": "insufficient_runs",
            "run_count": n,
            "hits": 0,
            "window_size": window_size,
        }

    # Resolve decision boundary B
    resolved_bound = bound
    if resolved_bound is None and sidecar:
        benchmark = sidecar.get("benchmark", {})
        if "regression_threshold" in benchmark:
            with contextlib.suppress(ValueError, TypeError):
                resolved_bound = float(benchmark["regression_threshold"])
        if resolved_bound is None and "stepback" in sidecar and "bound" in sidecar["stepback"]:
            with contextlib.suppress(ValueError, TypeError):
                resolved_bound = float(sidecar["stepback"]["bound"])

    if resolved_bound is None:
        return {
            "trigger": "near_bound",
            "fired": False,
            "reason": "no_decision_boundary",
            "run_count": n,
            "hits": 0,
            "window_size": window_size,
        }

    # Extract metrics for active runs
    metric_values: list[float] = []
    run_ids: list[str] = []
    for r in active_runs:
        val = _resolve_numeric_metric(r, target_metric=target_metric, sidecar=sidecar)
        if val is not None:
            metric_values.append(val)
            rid = _get_item_attr(r, "run_id") or _get_item_attr(r, "id") or f"run_{len(run_ids)}"
            run_ids.append(str(rid))

    if len(metric_values) < 3:
        return {
            "trigger": "near_bound",
            "fired": False,
            "reason": "no_numeric_metric",
            "run_count": len(metric_values),
            "hits": 0,
            "window_size": window_size,
        }

    # Calculate rolling sample stddev
    mean_y = sum(metric_values) / len(metric_values)
    var_y = sum((y - mean_y) ** 2 for y in metric_values) / max(1, len(metric_values) - 1)
    s_y = math.sqrt(var_y)

    # Determine tolerance margin epsilon
    if epsilon is not None:
        eff_epsilon = float(epsilon)
    elif sidecar and "stepback" in sidecar and "epsilon" in sidecar["stepback"]:
        eff_epsilon = float(sidecar["stepback"]["epsilon"])
    elif resolved_bound != 0.0:
        eff_epsilon = 0.10 * abs(resolved_bound)
    else:
        eff_epsilon = max(0.50 * s_y, 1e-6)

    # Ensure epsilon > 0
    if eff_epsilon <= 0:
        eff_epsilon = 1e-6

    # Evaluate window of recent runs
    w_metrics = metric_values[-window_size:]
    w_run_ids = run_ids[-window_size:]
    distances = [abs(y - resolved_bound) / eff_epsilon for y in w_metrics]

    hits = sum(1 for d in distances if d <= bound_proximity)
    offending_run_ids = [w_run_ids[i] for i, d in enumerate(distances) if d <= bound_proximity]

    d_last = distances[-1] if distances else float("inf")
    critical_proximity = d_last <= 0.25

    fired = (hits >= min_hits) or critical_proximity
    reason = None
    if critical_proximity:
        reason = "critical_proximity"
    elif hits >= min_hits:
        reason = "hits_exceeded"

    return {
        "trigger": "near_bound",
        "fired": fired,
        "reason": reason,
        "hits": hits,
        "window_size": len(w_metrics),
        "bound": resolved_bound,
        "margin": eff_epsilon,
        "recent_distances": [round(d, 4) for d in distances],
        "offending_run_ids": offending_run_ids,
    }


def evaluate_entropy_collapse(
    runs: list[Any],
    window_size: int = 5,
    entropy_threshold_nats: float = 0.35,
    anchor_run_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate outcome-entropy collapse trigger (T_entropy_collapse).

    Fires if N >= 5 and Shannon entropy (in nats) across window < threshold (0.35 nats) or == 0.
    """
    active_runs = _filter_active_runs(runs, anchor_run_id)
    n = len(active_runs)

    if n < 5:
        return {
            "trigger": "entropy_collapse",
            "fired": False,
            "reason": "insufficient_runs",
            "run_count": n,
            "entropy_nats": 0.0,
            "threshold_nats": entropy_threshold_nats,
            "window_size": window_size,
        }

    # Extract outcome labels
    outcomes: list[str] = []
    for r in active_runs:
        out = _get_item_attr(r, "outcome")
        if out is None:
            outcomes_dict = _get_item_attr(r, "outcomes")
            if isinstance(outcomes_dict, dict):
                out = outcomes_dict.get("verdict") or outcomes_dict.get("label")
        outcomes.append(str(out).lower().strip() if out is not None else "")

    w_k = outcomes[-window_size:]
    k = len(w_k)
    if k < 5:
        return {
            "trigger": "entropy_collapse",
            "fired": False,
            "reason": "insufficient_window",
            "run_count": k,
            "entropy_nats": 0.0,
            "threshold_nats": entropy_threshold_nats,
            "window_size": window_size,
        }

    counts = Counter(w_k)
    entropy_nats = 0.0
    for c in counts.values():
        p = c / k
        if p > 0:
            entropy_nats -= p * math.log(p)

    fired = (entropy_nats < entropy_threshold_nats) or (entropy_nats == 0.0)

    annotation = None
    message = None
    if fired:
        if all(o == "pass" for o in w_k):
            annotation = "fixation_positive"
            message = "Objective fulfilled; recommend conclude or rigorous replication."
        else:
            annotation = "fixation_negative"
            message = "Unproductive rut; recommend pivot or stop."

    return {
        "trigger": "entropy_collapse",
        "fired": fired,
        "entropy_nats": round(entropy_nats, 4),
        "threshold_nats": entropy_threshold_nats,
        "window_size": k,
        "outcome_counts": dict(counts),
        "annotation": annotation,
        "message": message,
    }


def evaluate_metric_plateau(
    runs: list[Any],
    window_size: int = 5,
    slope_threshold: float = 0.01,
    cv_threshold: float = 0.02,
    anchor_run_id: str | None = None,
    target_metric: str | None = None,
    sidecar: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate metric plateau trigger (T_plateau).

    OLS slope beta and sample variance across last n >= 5 runs.
    - Standard case (|mean_y| >= 0.01): |beta|/|mean_y| < 0.01 and s_y/|mean_y| < 0.02.
    - Near-zero case (|mean_y| < 0.01): |beta| < 0.001 and s_y < 0.001.
    """
    active_runs = _filter_active_runs(runs, anchor_run_id)
    metric_values: list[float] = []
    for r in active_runs:
        val = _resolve_numeric_metric(r, target_metric=target_metric, sidecar=sidecar)
        if val is not None:
            metric_values.append(val)

    n_total = len(metric_values)
    if n_total < 5:
        return {
            "trigger": "metric_plateau",
            "fired": False,
            "reason": "insufficient_runs",
            "run_count": n_total,
            "slope": 0.0,
            "stddev": 0.0,
        }

    # Use window of points (at least 5)
    effective_window = max(5, window_size)
    y_window = metric_values[-effective_window:]
    n = len(y_window)

    t_mean = (n + 1) / 2.0
    y_mean = sum(y_window) / n

    # OLS Slope beta: 12 * sum((t - t_bar)*(y - y_bar)) / (n * (n^2 - 1))
    numerator = sum((i + 1 - t_mean) * (y - y_mean) for i, y in enumerate(y_window))
    denom = n * (n * n - 1) / 12.0
    beta = numerator / denom if denom > 0 else 0.0

    # Sample standard deviation
    var_y = sum((y - y_mean) ** 2 for y in y_window) / (n - 1)
    s_y = math.sqrt(max(0.0, var_y))

    abs_y_mean = abs(y_mean)
    is_near_zero = abs_y_mean < 0.01

    if not is_near_zero:
        beta_rel = abs(beta) / abs_y_mean
        cv = s_y / abs_y_mean
        fired = (beta_rel < slope_threshold) and (cv < cv_threshold)
    else:
        beta_rel = None
        cv = None
        # Absolute threshold for near-zero metrics
        fired = (abs(beta) < 0.001) and (s_y < 0.001)

    return {
        "trigger": "metric_plateau",
        "fired": fired,
        "slope": round(beta, 6),
        "relative_slope": round(beta_rel, 6) if beta_rel is not None else None,
        "stddev": round(s_y, 6),
        "cv": round(cv, 6) if cv is not None else None,
        "mean_y": round(y_mean, 6),
        "is_near_zero": is_near_zero,
        "n_points": n,
    }


def evaluate_campaign_staleness(
    campaign: Any,
    member_runs: list[Any],
    idle_days_threshold: float = 14.0,
    now_dt: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate campaign staleness trigger (T_stale).

    Fires on:
    1. Temporal Abandonment: status == "open" and idle >= 14 days.
    2. Hypothesis Wheel-Spinning: status == "open", active claim registered, N >= 15, and Delta U_10 == 0.
    3. Uncapped Exploratory Drift: mode == "exploration", N >= 25, and no conclusion.
    """
    if isinstance(member_runs, dict):
        member_runs = list(member_runs.values())
    now = now_dt or datetime.now(UTC)
    status = str(_get_item_attr(campaign, "status", "open")).lower()
    mode = str(_get_item_attr(campaign, "mode", "confirmatory")).lower()
    claim_path = _get_item_attr(campaign, "claim_path")

    # Find last run timestamp
    last_run_dt = None
    if member_runs:
        latest_run = member_runs[-1]
        raw_ts = (
            _get_item_attr(latest_run, "ended_at")
            or _get_item_attr(latest_run, "completed_at")
            or _get_item_attr(latest_run, "created_at")
            or _get_item_attr(latest_run, "timestamp")
        )
        last_run_dt = _parse_iso_utc(raw_ts)

    if last_run_dt is None:
        camp_created = _get_item_attr(campaign, "created_at")
        last_run_dt = _parse_iso_utc(camp_created) or now

    idle_seconds = max(0.0, (now - last_run_dt).total_seconds())
    idle_days = idle_seconds / 86400.0
    n_runs = len(member_runs)

    fired = False
    reasons = []

    # Rule 1: Temporal Abandonment
    if status == "open" and idle_days >= idle_days_threshold:
        fired = True
        reasons.append("temporal_abandonment")

    # Rule 2: Hypothesis Wheel-Spinning (active claim file registered and N >= 15)
    if status == "open" and claim_path and n_runs >= 15:
        # Check clause resolution rate over last 10 runs
        # If campaign has resolved_clauses or claim_progress tracking
        progress = _get_item_attr(campaign, "clause_progress") or []
        if isinstance(progress, list) and len(progress) >= 10:
            delta_u10 = progress[-1] - progress[-10]
            if delta_u10 <= 0:
                fired = True
                reasons.append("hypothesis_wheel_spinning")
        else:
            # If no progress recorded across last 10 runs
            recent_runs = member_runs[-10:]
            resolved_in_recent = any(_get_item_attr(r, "clause_resolved") for r in recent_runs)
            if not resolved_in_recent:
                fired = True
                reasons.append("hypothesis_wheel_spinning")

    # Rule 3: Uncapped Exploratory Drift
    if mode == "exploration" and n_runs >= 25 and status == "open":
        fired = True
        reasons.append("uncapped_exploratory_drift")

    return {
        "trigger": "campaign_staleness",
        "fired": fired,
        "reasons": reasons,
        "idle_days": round(idle_days, 2),
        "idle_days_threshold": idle_days_threshold,
        "run_count": n_runs,
        "status": status,
        "mode": mode,
    }


def _extract_run_condition(run: Any) -> str | None:
    """Extract condition or threshold representation from a run record."""
    cond = _get_item_attr(run, "pass_condition")
    if cond is not None:
        return str(cond).strip()

    thresh = _get_item_attr(run, "threshold")
    if thresh is not None:
        return f"threshold={thresh}"

    outcomes = _get_item_attr(run, "outcomes")
    if isinstance(outcomes, dict):
        pass_item = outcomes.get("pass")
        if isinstance(pass_item, dict):
            c = pass_item.get("condition")
            if c:
                return str(c).strip()
        elif isinstance(pass_item, str):
            return pass_item.strip()
        cond_items = []
        for k in sorted(outcomes.keys()):
            v = outcomes[k]
            if isinstance(v, dict) and "condition" in v:
                cond_items.append(f"{k}:{v['condition']}")
            else:
                cond_items.append(f"{k}:{v}")
        if cond_items:
            return "; ".join(cond_items)

    sidecar = _get_item_attr(run, "sidecar")
    if isinstance(sidecar, dict):
        sc_outcomes = sidecar.get("outcomes", {})
        if isinstance(sc_outcomes, dict):
            pass_item = sc_outcomes.get("pass", {})
            if isinstance(pass_item, dict) and "condition" in pass_item:
                return str(pass_item["condition"]).strip()
        benchmark = sidecar.get("benchmark", {})
        if isinstance(benchmark, dict) and "regression_threshold" in benchmark:
            return f"regression_threshold={benchmark['regression_threshold']}"

    metadata = _get_item_attr(run, "metadata")
    if isinstance(metadata, dict):
        for k in ("pass_condition", "condition", "threshold"):
            if k in metadata:
                return str(metadata[k]).strip()
        if "outcomes" in metadata and isinstance(metadata["outcomes"], dict):
            pass_item = metadata["outcomes"].get("pass")
            if isinstance(pass_item, dict) and "condition" in pass_item:
                return str(pass_item["condition"]).strip()

    sidecar_sha = _get_item_attr(run, "sidecar_sha256")
    if sidecar_sha:
        return f"sidecar_sha256={sidecar_sha}"

    return None


def evaluate_threshold_edit(
    runs: list[Any],
    anchor_run_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate threshold / goalpost edit trigger (T_threshold_edit).

    Detects mid-campaign mutation of outcome threshold conditions between runs
    within the active epoch.
    """
    active_runs = _filter_active_runs(runs, anchor_run_id)
    n = len(active_runs)

    if n < 2:
        return {
            "trigger": "threshold_edit",
            "fired": False,
            "reason": "insufficient_runs",
            "run_count": n,
        }

    baseline_run_id: str | None = None
    baseline_condition: str | None = None
    baseline_idx = -1

    for idx, r in enumerate(active_runs):
        c = _extract_run_condition(r)
        if c:
            baseline_condition = c
            baseline_run_id = str(
                _get_item_attr(r, "run_id") or _get_item_attr(r, "id") or f"run_{idx}"
            )
            baseline_idx = idx
            break

    if baseline_condition is None:
        return {
            "trigger": "threshold_edit",
            "fired": False,
            "reason": "no_conditions_recorded",
            "run_count": n,
        }

    for idx in range(baseline_idx + 1, n):
        r = active_runs[idx]
        cur_cond = _extract_run_condition(r)
        if cur_cond and cur_cond != baseline_condition:
            cur_run_id = str(
                _get_item_attr(r, "run_id") or _get_item_attr(r, "id") or f"run_{idx}"
            )
            return {
                "trigger": "threshold_edit",
                "fired": True,
                "reason": "threshold_condition_mutated",
                "baseline_run_id": baseline_run_id,
                "offending_run_id": cur_run_id,
                "baseline_condition": baseline_condition,
                "mutated_condition": cur_cond,
                "epoch_runs_checked": n,
            }

    return {
        "trigger": "threshold_edit",
        "fired": False,
        "reason": "thresholds_consistent",
        "baseline_condition": baseline_condition,
        "epoch_runs_checked": n,
    }


def evaluate_scale_up(
    campaign: Any,
    proposed_tasks: int | list[Any] | str | None = None,
    pilot_run_count: int = 3,
) -> dict[str, Any]:
    """Evaluate scale-up acceleration trigger (T_scale_up).

    Flags large array jobs (>10 tasks) launched without sufficient clean pilot history.
    """
    task_count: int = 0
    if isinstance(proposed_tasks, int):
        task_count = proposed_tasks
    elif isinstance(proposed_tasks, (list, tuple, set)):
        task_count = len(proposed_tasks)
    elif isinstance(proposed_tasks, str):
        s = proposed_tasks.strip()
        if "-" in s:
            try:
                parts = s.split("-")
                start = int(parts[0])
                end = int(parts[1].split(":")[0])
                task_count = max(0, end - start + 1)
            except Exception:
                task_count = 0
        else:
            try:
                task_count = int(s)
            except Exception:
                task_count = 0
    elif proposed_tasks is None:
        for attr in ("proposed_tasks", "array_tasks", "requested_tasks", "tasks"):
            val = _get_item_attr(campaign, attr)
            if val is not None:
                if isinstance(val, int):
                    task_count = val
                    break
                elif isinstance(val, (list, tuple, set)):
                    task_count = len(val)
                    break
                elif isinstance(val, str):
                    try:
                        task_count = int(val)
                        break
                    except ValueError:
                        pass

    if task_count <= 10:
        return {
            "trigger": "scale_up",
            "fired": False,
            "reason": "not_large_array",
            "task_count": task_count,
            "pilot_run_count": pilot_run_count,
        }

    member_runs = _get_item_attr(campaign, "runs") or _get_item_attr(campaign, "member_runs") or []
    if isinstance(member_runs, dict):
        member_runs = list(member_runs.values())
    elif not isinstance(member_runs, list):
        member_runs = []

    total_runs = len(member_runs)
    clean_pilot_count = 0
    has_unclean_history = False
    unclean_reasons = []

    for r in member_runs:
        outcome = str(_get_item_attr(r, "outcome", "")).lower().strip()
        exit_code = _get_item_attr(r, "exit_code")

        if exit_code not in (0, None):
            has_unclean_history = True
            unclean_reasons.append(f"exit_code_{exit_code}")
        elif outcome in ("pass", "promising", "clean_run", "sound", "headroom_confirmed"):
            clean_pilot_count += 1
        elif outcome in ("fail", "error", "guardrail_violated", "invalid"):
            has_unclean_history = True
            unclean_reasons.append(f"outcome_{outcome}")

    esc_level = _get_item_attr(campaign, "escalation_level") or _get_item_attr(campaign, "level") or 0
    if esc_level >= 1:
        has_unclean_history = True
        unclean_reasons.append(f"active_escalation_level_{esc_level}")

    if has_unclean_history:
        return {
            "trigger": "scale_up",
            "fired": True,
            "reason": "unclean_pilot_history",
            "task_count": task_count,
            "pilot_run_count": pilot_run_count,
            "clean_pilot_runs": clean_pilot_count,
            "unclean_reasons": unclean_reasons,
            "total_runs": total_runs,
        }

    if clean_pilot_count < pilot_run_count:
        return {
            "trigger": "scale_up",
            "fired": True,
            "reason": "insufficient_pilot_runs",
            "task_count": task_count,
            "pilot_run_count": pilot_run_count,
            "clean_pilot_runs": clean_pilot_count,
            "total_runs": total_runs,
        }

    return {
        "trigger": "scale_up",
        "fired": False,
        "reason": "sufficient_clean_pilots",
        "task_count": task_count,
        "pilot_run_count": pilot_run_count,
        "clean_pilot_runs": clean_pilot_count,
        "total_runs": total_runs,
    }


def evaluate_all_triggers(
    campaign: Any,
    member_runs: list[Any],
    state: Any | None = None,
    sidecar: dict[str, Any] | None = None,
    now_dt: datetime | None = None,
    window_size: int = 5,
    proposed_tasks: int | list[Any] | str | None = None,
    snoozed_triggers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate all event triggers for a campaign and member runs.

    If a trigger has fired but is currently snoozed (by run count and epoch window),
    it is marked with `snoozed: True` and excluded from `active_triggers`.
    """
    if isinstance(member_runs, dict):
        member_runs = list(member_runs.values())
    anchor_id = getattr(state, "pivot_anchor_run_id", None) if state else None
    if anchor_id is None and isinstance(state, dict):
        anchor_id = state.get("pivot_anchor_run_id")

    res_marginal = evaluate_repeated_marginal(
        member_runs, window_size=window_size, anchor_run_id=anchor_id
    )
    res_near_bound = evaluate_near_bound_hits(
        member_runs, window_size=window_size, anchor_run_id=anchor_id, sidecar=sidecar
    )
    res_entropy = evaluate_entropy_collapse(
        member_runs, window_size=window_size, anchor_run_id=anchor_id
    )
    res_plateau = evaluate_metric_plateau(
        member_runs, window_size=window_size, anchor_run_id=anchor_id, sidecar=sidecar
    )
    res_stale = evaluate_campaign_staleness(campaign, member_runs, now_dt=now_dt)
    res_threshold = evaluate_threshold_edit(member_runs, anchor_run_id=anchor_id)

    camp_for_scale = campaign
    if isinstance(campaign, dict):
        camp_for_scale = dict(campaign)
        if "runs" not in camp_for_scale and "member_runs" not in camp_for_scale:
            camp_for_scale["runs"] = member_runs
    res_scale_up = evaluate_scale_up(
        camp_for_scale, proposed_tasks=proposed_tasks, pilot_run_count=3
    )

    evaluated = {
        "repeated_marginal": res_marginal,
        "near_bound": res_near_bound,
        "entropy_collapse": res_entropy,
        "metric_plateau": res_plateau,
        "campaign_staleness": res_stale,
        "threshold_edit": res_threshold,
        "scale_up": res_scale_up,
    }

    # Resolve snooze configuration
    snooze_dict = snoozed_triggers
    if snooze_dict is None:
        snooze_dict = getattr(state, "snoozed_triggers", None)
    if snooze_dict is None and isinstance(state, dict):
        snooze_dict = state.get("snoozed_triggers")
    snooze_dict = snooze_dict or {}

    current_runs = len(member_runs)
    now = now_dt or datetime.now(UTC)

    active_triggers: list[str] = []
    snoozed_list: list[str] = []

    for name, res in evaluated.items():
        if res.get("fired"):
            s_info = snooze_dict.get(name)
            is_snoozed = False
            if s_info:
                until_runs = s_info.get("snooze_until_runs")
                until_epoch_raw = s_info.get("snooze_until_epoch")
                until_epoch = _parse_iso_utc(until_epoch_raw)

                # Snooze is active if not expired by runs and not expired by epoch
                snooze_active = True
                if until_runs is not None and current_runs > until_runs:
                    snooze_active = False
                if until_epoch is not None and now > until_epoch:
                    snooze_active = False

                if snooze_active:
                    is_snoozed = True
                    res["snoozed"] = True
                    res["snooze_info"] = s_info
                    snoozed_list.append(name)

            if not is_snoozed:
                active_triggers.append(name)

    return {
        "triggers_evaluated": evaluated,
        "active_triggers": active_triggers,
        "snoozed_triggers": snoozed_list,
        "fired": len(active_triggers) > 0,
    }


def snooze_stepback_trigger(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Snooze a step-back event trigger for a bounded run/epoch window."""
    from bathos.stepback.engine import snooze_stepback_trigger as _snooze

    return _snooze(*args, **kwargs)

