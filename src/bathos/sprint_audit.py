"""Cross-project audit of recent runs and campaigns."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from bathos.schema import CURRENT_SCHEMA_VERSION


@dataclass
class SignalResult:
    """Result of a single sprint-audit signal computation.

    Attributes:
        signal: Signal name (e.g., 'control_arm_rate').
        value: Numeric value (float or None if unavailable).
        level: Severity level ('INFO', 'OK', or 'WARNING').
        message: Human-readable message describing the result.
    """

    signal: str
    value: float | None
    level: str
    message: str


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

def _load_sidecar_schema_keys(sidecar_path: str) -> set[str] | None:
    """Load result_schema field names declared in a sidecar file.

    Args:
        sidecar_path: Path to .bth.toml file.

    Returns:
        Set of declared result_schema field names, or None if the sidecar
        could not be read or parsed (caller should exclude this run from the
        schema_overflow_rate denominator rather than counting it as clean).
    """
    try:
        import tomllib

        path = Path(sidecar_path)
        if not path.exists():
            return set()
        data = tomllib.loads(path.read_text())
        return set(data.get("result_schema", {}).keys())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    except Exception:
        return None


def signal_control_arm_rate(project_slug: str, db_path: Path) -> SignalResult:
    """Signal 9: control_arm_rate — fraction of runs with ctrl_* outcome labels.

    Args:
        project_slug: Project identifier.
        db_path: Path to warm DB (bathos.db).

    Returns:
        SignalResult with value (control_arm_rate as float or None),
        level ('INFO', 'OK', or 'WARNING'), and message.

    Semantics:
        - If warm DB does not exist: value=None, level='INFO'
        - If no runs exist: value=0.0, level='INFO'
        - If control_arm_rate > 0.0: level='OK'
        - If control_arm_rate == 0.0 AND validation/production runs exist: level='WARNING'
          (no control arm found in validation/production campaigns)
    """
    if not db_path.exists():
        return SignalResult(
            signal="control_arm_rate",
            value=None,
            level="INFO",
            message="warm DB not available",
        )

    try:
        with duckdb.connect(str(db_path), read_only=True) as conn:
            # Count total runs
            total = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE project_slug = ?", [project_slug]
            ).fetchone()[0]

            if total == 0:
                return SignalResult(
                    signal="control_arm_rate",
                    value=0.0,
                    level="INFO",
                    message="no runs in project",
                )

            # Count control arm runs (outcome LIKE 'ctrl_%')
            ctrl_count = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE project_slug = ? AND outcome LIKE 'ctrl_%'",
                [project_slug],
            ).fetchone()[0]

            rate = ctrl_count / total

            # Check if any validation/production campaign runs exist
            has_val_prod = conn.execute(
                "SELECT 1 FROM runs WHERE project_slug = ? AND stage_name IN ('validation','production') LIMIT 1",
                [project_slug],
            ).fetchone()

            level = "OK"
            message = f"control_arm_rate={rate:.2%} ({ctrl_count}/{total} runs with ctrl_* outcomes)"

            if rate == 0.0 and has_val_prod:
                level = "WARNING"
                message += " — no control arm runs found in validation/production campaigns"

            return SignalResult(
                signal="control_arm_rate",
                value=rate,
                level=level,
                message=message,
            )
    except Exception as e:
        return SignalResult(
            signal="control_arm_rate",
            value=None,
            level="INFO",
            message=f"error computing control_arm_rate: {e}",
        )


def signal_submit_bypass_rate(project_slug: str, db_path: Path, catalog_dir: Path) -> SignalResult:
    """Signal 10: submit_bypass_rate — fraction of validation/production runs not submitted via bth submit.

    Args:
        project_slug: Project identifier.
        db_path: Path to warm DB (bathos.db).
        catalog_dir: Path to catalog directory (~/.bth/catalog).

    Returns:
        SignalResult with value (submit_bypass_rate as float or None),
        level ('INFO', 'OK', or 'WARNING'), and message.

    Semantics:
        - Loads all submit-provenance records from ~/.bth/catalog/submits/<project_slug>/**/*.parquet.
        - For each validation/production run with slurm_job_id populated, checks if matching
          submit-provenance exists (joined on runs.slurm_job_id = submits.myxcel_job_id).
        - If match found: run was submitted via bth submit.
        - If no match: run bypassed bth submit (e.g., direct sbatch call).
        - value = (bypassed runs) / (total validation/production runs with slurm_job_id).
        - WARNING if rate > 5% (threshold: submit_bypass_rate > 0.05).
        - OK if rate <= 5%.
        - INFO if no runs or no provenance records found yet.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq_lib

    # Load provenance records
    provenance_dir = catalog_dir / "submits" / project_slug
    provenance_files = []
    if provenance_dir.exists():
        provenance_files = list(provenance_dir.glob("**/*.parquet"))

    submitted_job_ids: set[str] = set()
    if provenance_files:
        try:
            tables = [pq_lib.read_table(str(f)) for f in provenance_files]
            if tables:
                combined = pa.concat_tables(tables)
                submitted_job_ids = set(
                    jid for jid in combined.column("myxcel_job_id").to_pylist() if jid
                )
        except Exception as e:
            # Silently continue if provenance read fails
            # (but could log: f"Warning reading provenance: {e}")
            pass

    # Query warm DB for validation/production runs with slurm_job_id
    if not db_path.exists():
        return SignalResult(
            signal="submit_bypass_rate",
            value=None,
            level="INFO",
            message="warm DB not available",
        )

    try:
        with duckdb.connect(str(db_path), read_only=True) as conn:
            rows = conn.execute(
                "SELECT slurm_job_id FROM runs"
                " WHERE project_slug = ?"
                " AND stage_name IN ('validation', 'production')"
                " AND slurm_job_id IS NOT NULL AND slurm_job_id != ''",
                [project_slug],
            ).fetchall()

            if not rows:
                return SignalResult(
                    signal="submit_bypass_rate",
                    value=0.0,
                    level="INFO",
                    message="no validation/production cluster runs found",
                )

            total = len(rows)
            bypassed = sum(1 for (jid,) in rows if jid not in submitted_job_ids)
            rate = bypassed / total

            level = "WARNING" if rate > 0.05 else "OK"
            message = (
                f"submit_bypass_rate={rate:.2%} ({bypassed}/{total} "
                f"validation/production runs bypassed bth submit)"
            )

            return SignalResult(
                signal="submit_bypass_rate",
                value=rate,
                level=level,
                message=message,
            )
    except Exception as e:
        return SignalResult(
            signal="submit_bypass_rate",
            value=None,
            level="INFO",
            message=f"error computing submit_bypass_rate: {e}",
        )


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
                       agent_mode, metadata, sidecar_path, stage_name
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
                stage_name,
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
                        "stage_name": stage_name or "",
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
            # Counts runs where metadata contains keys NOT declared in result_schema.
            # metadata is the script's primary output channel (runner.py:_read_result_emission);
            # result_schema in the sidecar declares which keys are expected.
            # Any metadata key NOT in result_schema = genuine schema overflow
            # (possible metric substitution — arXiv 2510.21652 AstaBench).
            # Runs without sidecar_path are skipped (no schema to compare against);
            # those are already tracked by bypass_explicit / bypass_in_agent_mode.
            # Denominator is runs_with_sidecar, not total_all.
            if total_all > 0:
                overflow_count = 0
                runs_with_sidecar = 0
                for run in all_runs_flat:
                    sidecar_path_run = run.get("sidecar_path", "")
                    if not sidecar_path_run:
                        continue
                    declared_keys = _load_sidecar_schema_keys(sidecar_path_run)
                    if declared_keys is None:
                        # Sidecar exists but could not be parsed; exclude from denominator
                        # rather than inflating overflow_rate by treating all keys as undeclared.
                        continue
                    runs_with_sidecar += 1
                    try:
                        meta = json.loads(run.get("metadata", "{}") or "{}")
                        undeclared_keys = set(meta.keys()) - declared_keys
                        if undeclared_keys:
                            overflow_count += 1
                    except (json.JSONDecodeError, TypeError):
                        pass
                if runs_with_sidecar > 0:
                    signals["schema_overflow_rate"] = overflow_count / runs_with_sidecar
                else:
                    signals["schema_overflow_rate"] = 0.0
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

            # Signal 8: premature_stopping_rate
            # Fraction of concluded sequential campaigns where final E_n < stopping_threshold.
            # Domain rationale: POPPER (arXiv 2502.09858) — sequential stopping below pre-specified
            # threshold invalidates the anytime-valid guarantee.
            # Calibration-target warning, consistent with threshold ADR
            # (260601_sprint-audit-threshold-rationale.md).
            n_sequential_concluded = 0
            n_premature = 0
            try:
                seq_rows = db.execute(
                    "SELECT id, stopping_threshold FROM campaigns WHERE mode='sequential' AND status='concluded' AND stopping_threshold IS NOT NULL"
                ).fetchall()
                n_sequential_concluded = len(seq_rows)
                from bathos.campaigns import _campaign_threshold_met
                for camp_id, camp_threshold in seq_rows:
                    if not _campaign_threshold_met(db, camp_id, camp_threshold):
                        n_premature += 1
            except Exception:
                pass
            signals["premature_stopping_rate"] = n_premature / max(n_sequential_concluded, 1) if n_sequential_concluded > 0 else 0.0

            # Signal 9: control_arm_rate
            # Compute via standalone function for atomic SignalResult + testability.
            signal_result = signal_control_arm_rate(project["slug"], db_path)
            signals["control_arm_rate"] = signal_result.value if signal_result.value is not None else 0.0

            # Signal 10: submit_bypass_rate
            # Compute via standalone function for atomic SignalResult + testability.
            signal_result_submit_bypass = signal_submit_bypass_rate(project["slug"], db_path, catalog_dir)
            signals["submit_bypass_rate"] = signal_result_submit_bypass.value if signal_result_submit_bypass.value is not None else 0.0

            # Check signal thresholds and add anomalies.
            # All thresholds are CALIBRATION TARGETS (v0.6), not hard gates.
            # See ADR .praxia/docs/decisions/260601_sprint-audit-threshold-rationale.md
            # error_rate > 0.10: >10% error outcomes indicates infrastructure/env problems;
            #   uncalibrated — domain reasoning, no empirical study (spec Item 5)
            if signals["error_rate"] > 0.10:
                anomalies.append(
                    f"Project: error_rate {signals['error_rate']:.1%} > 10%"
                )
            # bypass_explicit > 0.30: arXiv 2509.08713 constraint violation rates (1.3–71.4%);
            #   0.30 is midpoint heuristic, not derived from bypass-rate empirical data
            if signals["bypass_explicit"] > 0.30:
                anomalies.append(
                    f"Project: bypass_explicit {signals['bypass_explicit']:.1%} > 30%"
                )
            # bypass_in_agent_mode > 0.05: tighter than bypass_explicit; agents have zero
            #   incremental cost to include a sidecar, so agent-mode bypass is harder to justify
            #   (ADR 260526_bypass-rate-split, spec D4)
            if signals["bypass_in_agent_mode"] > 0.05:
                anomalies.append(
                    f"Project: bypass_in_agent_mode {signals['bypass_in_agent_mode']:.1%} > 5%"
                )
            # outcome_entropy < 0.5 nats: arXiv 2501.10421 hivemind experiment;
            #   0.5 nats < ln(2)=0.693 nats (balanced 2-outcome); flags label compression
            if signals["outcome_entropy"] < 0.5:
                anomalies.append(
                    f"Project: outcome_entropy {signals['outcome_entropy']:.2f} nats < 0.5"
                )
            # unfired_branches > 0.40: arXiv 2509.08713 revision-agent unfired branches;
            #   >40% unfired suggests hypothesis space not fully tested
            if signals["unfired_branches"] > 0.40:
                anomalies.append(
                    f"Project: unfired_branches {signals['unfired_branches']:.1%} > 40%"
                )
            # schema_overflow_rate > 0.20: arXiv 2510.21652 AstaBench metric misuse;
            #   counts runs where metadata has keys NOT in result_schema (see ADR 260601 bug fix)
            if signals["schema_overflow_rate"] > 0.20:
                anomalies.append(
                    f"Project: schema_overflow_rate {signals['schema_overflow_rate']:.1%} > 20%"
                )
            # post_hoc_bias_flag: arXiv 2510.21652 AstaBench chi2(4,200)=61.99, p<1e-10;
            #   worst-label count in first third > 10% of total flags post-hoc experiment culling
            if signals["post_hoc_bias_flag"]:
                anomalies.append(
                    f"Project: post_hoc_bias_flag detected"
                )
            # premature_stopping_rate: arXiv 2502.09858 POPPER;
            #   any concluded sequential campaign below threshold invalidates anytime-valid guarantee
            if signals["premature_stopping_rate"] > 0.0:
                anomalies.append(
                    f"Project: premature_stopping_rate {signals['premature_stopping_rate']:.1%} — "
                    f"{n_premature} sequential campaign(s) concluded before reaching stopping_threshold "
                    f"(sequential test validity compromised)"
                )
            # control_arm_rate: AC-5 experimental controls discipline
            #   WARNING when rate==0.0 and validation/production campaigns exist
            if signal_result.level == "WARNING":
                anomalies.append(signal_result.message)

            # submit_bypass_rate: AC-9 submit provenance discipline
            #   WARNING when rate > 5% in validation/production stage
            if signal_result_submit_bypass.level == "WARNING":
                anomalies.append(signal_result_submit_bypass.message)

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
