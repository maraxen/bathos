"""Cross-project audit of recent runs and campaigns."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from bathos.schema import CURRENT_SCHEMA_VERSION


def _compute_outcome_entropy(outcomes: list[str]) -> float:
    """Compute Shannon entropy (in nats) of outcome label distribution.

    Args:
        outcomes: List of outcome labels.

    Returns:
        Shannon entropy H = -Σ p_i * ln(p_i) in nats (natural log).
        Returns 0.0 if list is empty or has only one unique label.
    """
    if not outcomes:
        return 0.0

    outcome_counts: dict[str, int] = {}
    for outcome in outcomes:
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1

    if len(outcome_counts) == 1:
        return 0.0

    total = len(outcomes)
    entropy = 0.0
    for count in outcome_counts.values():
        if count > 0:
            p = count / total
            entropy -= p * math.log(p)

    return entropy


def _load_sidecar_outcomes(sidecar_path: str) -> set[str]:
    """Load outcome labels declared in a sidecar file.

    Args:
        sidecar_path: Path to .bth.toml file.

    Returns:
        Set of declared outcome labels, or empty set if unable to load.
    """
    try:
        import tomllib

        path = Path(sidecar_path)
        if not path.exists():
            return set()
        data = tomllib.loads(path.read_text())

        # Extract outcomes from [outcomes.*] sections
        outcomes = set()
        for key in data:
            if key == "outcomes" and isinstance(data[key], dict):
                outcomes.update(data[key].keys())
        return outcomes
    except Exception:
        return set()


def sprint_audit(hours: int = 24) -> dict:
    """Cross-project audit of recent runs and campaigns.

    Queries each registered project's warm DB (read-only, ATTACH).
    Skips projects with incompatible schema versions with a warning.

    Args:
        hours: Lookback window in hours (default 24).

    Returns:
        Dict with 'audit_results' (by project) and 'warnings' list.
    """
    from bathos.config import list_registered_projects

    projects = list_registered_projects()
    audit_results: dict = {}
    warnings: list[str] = []

    for project in projects:
        catalog_dir = Path(project["catalog_dir"])
        db_path = catalog_dir / "bathos.db"

        if not db_path.exists():
            warnings.append(
                f"Project {project['slug']}: no warm DB found "
                f"(run bth compact first). Skipping."
            )
            continue

        # Check schema version before querying
        try:
            db_check = duckdb.connect(str(db_path), read_only=True)
            version_rows = db_check.execute(
                "SELECT value FROM _schema_meta WHERE key = 'warm_version'"
            ).fetchall()
            db_check.close()

            if version_rows:
                version = version_rows[0][0]
                if version != CURRENT_SCHEMA_VERSION:
                    warnings.append(
                        f"Project {project['slug']}: schema version mismatch "
                        f"(has {version!r}, need {CURRENT_SCHEMA_VERSION!r}) — "
                        f"run bth compact first. Skipping."
                    )
                    continue
        except Exception as e:
            warnings.append(
                f"Project {project['slug']}: failed schema check — {e}. Skipping."
            )
            continue

        # Safe to query
        try:
            db = duckdb.connect(str(db_path), read_only=True)
            db.execute("SET TimeZone='UTC'")
            cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()

            rows = db.execute(
                """
                SELECT id, campaign_id, sidecar_mode, outcome, outcome_is_residual, timestamp,
                       agent_mode, metadata, sidecar_path
                FROM runs
                WHERE timestamp > ?
                ORDER BY timestamp ASC
            """,
                [cutoff],
            ).fetchall()

            # Group by campaign
            by_campaign: dict[str, list] = {}
            for (
                run_id,
                campaign_id,
                sidecar_mode,
                outcome,
                is_residual,
                ts,
                agent_mode,
                metadata,
                sidecar_path,
            ) in rows:
                key = campaign_id or "_uncampaigned"
                if key not in by_campaign:
                    by_campaign[key] = []
                by_campaign[key].append(
                    {
                        "run_id": run_id,
                        "sidecar_mode": sidecar_mode,
                        "outcome": outcome,
                        "is_residual": bool(is_residual),
                        "agent_mode": agent_mode or "",
                        "metadata": metadata or "{}",
                        "sidecar_path": sidecar_path or "",
                    }
                )

            # Compute anomalies and signals
            anomalies: list[str] = []
            signals: dict[str, float | bool] = {}

            # Global signals across all campaigns
            all_outcomes = [r["outcome"] for r in sum(by_campaign.values(), [])]
            all_runs_flat = sum(by_campaign.values(), [])
            total_all = len(all_runs_flat)

            # Signal 1: error_rate
            if total_all > 0:
                error_count = sum(1 for r in all_runs_flat if r["outcome"] == "error")
                signals["error_rate"] = error_count / total_all
            else:
                signals["error_rate"] = 0.0

            # Signal 2 & 3: bypass_explicit and bypass_in_agent_mode
            if total_all > 0:
                # bypass_explicit: sidecar_mode="bypassed" AND agent_mode="" (empty/falsy)
                bypass_explicit_count = sum(
                    1
                    for r in all_runs_flat
                    if r["sidecar_mode"] == "bypassed" and not r.get("agent_mode")
                )
                signals["bypass_explicit"] = bypass_explicit_count / total_all

                # bypass_in_agent_mode: sidecar_mode="bypassed" AND agent_mode non-empty
                agent_mode_runs = [r for r in all_runs_flat if r.get("agent_mode")]
                if agent_mode_runs:
                    bypass_in_agent_count = sum(
                        1 for r in agent_mode_runs if r["sidecar_mode"] == "bypassed"
                    )
                    signals["bypass_in_agent_mode"] = bypass_in_agent_count / len(agent_mode_runs)
                else:
                    signals["bypass_in_agent_mode"] = 0.0
            else:
                signals["bypass_explicit"] = 0.0
                signals["bypass_in_agent_mode"] = 0.0

            # Signal 4: outcome_entropy
            signals["outcome_entropy"] = _compute_outcome_entropy(all_outcomes)

            # Signal 5: unfired_branches
            # Collect all declared outcome labels from available sidecars
            all_declared_outcomes = set()
            for run in all_runs_flat:
                if run.get("sidecar_path"):
                    declared = _load_sidecar_outcomes(run["sidecar_path"])
                    all_declared_outcomes.update(declared)

            if all_declared_outcomes:
                actual_outcomes = set(o for o in all_outcomes if o)
                never_fired = len(all_declared_outcomes - actual_outcomes)
                signals["unfired_branches"] = never_fired / len(all_declared_outcomes)
            else:
                signals["unfired_branches"] = 0.0

            # Signal 6: schema_overflow_rate
            # Parse metadata JSON for each run; check if extra keys beyond empty/standard fields
            if total_all > 0:
                overflow_count = 0
                for run in all_runs_flat:
                    try:
                        metadata = json.loads(run.get("metadata", "{}") or "{}")
                        # If metadata has any keys at all, consider it overflow
                        # (expected is empty dict for standard runs)
                        if metadata:
                            overflow_count += 1
                    except (json.JSONDecodeError, TypeError):
                        # Parse error; don't count as overflow
                        pass
                signals["schema_overflow_rate"] = overflow_count / total_all
            else:
                signals["schema_overflow_rate"] = 0.0

            # Signal 7: post_hoc_bias_flag
            # Check if worst-outcome label (e.g., "fail" or "error") appears > 10% in first third
            worst_outcome_labels = {"fail", "error"}
            outcomes_present = set(o for o in all_outcomes if o)
            worst_present = worst_outcome_labels & outcomes_present

            signals["post_hoc_bias_flag"] = False
            if worst_present and len(all_outcomes) >= 3:
                worst_label = list(worst_present)[0]
                third = max(1, len(all_outcomes) // 3)
                early_worst_count = sum(1 for i in range(third) if all_outcomes[i] == worst_label)
                if early_worst_count > 0.1 * len(all_outcomes):
                    signals["post_hoc_bias_flag"] = True

            # Check signal thresholds and add anomalies
            if signals["error_rate"] > 0.10:
                anomalies.append(
                    f"Project: error_rate {signals['error_rate']:.1%} > 10%"
                )
            if signals["bypass_explicit"] > 0.30:
                anomalies.append(
                    f"Project: bypass_explicit {signals['bypass_explicit']:.1%} > 30%"
                )
            if signals["bypass_in_agent_mode"] > 0.05:
                anomalies.append(
                    f"Project: bypass_in_agent_mode {signals['bypass_in_agent_mode']:.1%} > 5%"
                )
            if signals["outcome_entropy"] < 0.5:
                anomalies.append(
                    f"Project: outcome_entropy {signals['outcome_entropy']:.2f} nats < 0.5"
                )
            if signals["unfired_branches"] > 0.40:
                anomalies.append(
                    f"Project: unfired_branches {signals['unfired_branches']:.1%} > 40%"
                )
            if signals["schema_overflow_rate"] > 0.20:
                anomalies.append(
                    f"Project: schema_overflow_rate {signals['schema_overflow_rate']:.1%} > 20%"
                )
            if signals["post_hoc_bias_flag"]:
                anomalies.append(
                    f"Project: post_hoc_bias_flag detected"
                )

            # Legacy anomalies per campaign (kept for backward compatibility)
            for campaign_id, runs in by_campaign.items():
                total = len(runs)
                unknown_count = sum(1 for r in runs if r["outcome"] in ("unknown", "", None))
                bypassed_count = sum(1 for r in runs if r["sidecar_mode"] == "bypassed")
                residual_count = sum(1 for r in runs if r["is_residual"])

                if unknown_count > 0:
                    anomalies.append(
                        f"Campaign {campaign_id}: {unknown_count} runs with unknown outcome"
                    )
                if total > 0 and bypassed_count / total > 0.1:
                    anomalies.append(
                        f"Campaign {campaign_id}: {bypassed_count}/{total} "
                        f"bypassed (>{10:.0f}%)"
                    )
                if total > 0 and residual_count / total > 0.1:
                    anomalies.append(
                        f"Campaign {campaign_id}: residual rate "
                        f"{residual_count/total:.1%} > 10%"
                    )

            audit_results[project["slug"]] = {
                "runs": len(rows),
                "campaigns": len(by_campaign),
                "signals": signals,
                "anomalies": anomalies,
            }
            db.close()
        except Exception as e:
            warnings.append(f"Project {project['slug']}: query failed — {e}. Skipping.")

    return {"audit_results": audit_results, "warnings": warnings}
