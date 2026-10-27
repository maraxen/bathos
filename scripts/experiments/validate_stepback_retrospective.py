#!/usr/bin/env python3
"""Pre-Registered Retrospective Validation Experiment (R4).

Evaluates whether step-back review triggers, rubric evaluation, and escalation
ladders accurately discriminate problematic historical campaign trajectories
(plateau, entropy collapse, repeated marginals, goalpost shift, scale-up jump,
near-bound skits, staleness) from healthy lines with high precision and recall.

Pre-registered sidecar: scripts/experiments/validate_stepback_retrospective.bth.toml
Pass condition: accuracy >= 0.85 AND recall >= 0.80 AND false_positive_rate <= 0.15
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Add project root and src/ to path
workspace_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(workspace_root / "src"))
sys.path.insert(0, str(workspace_root))

from bathos.stepback.triggers import evaluate_all_triggers  # noqa: E402
from scripts.experiments.fixtures.stepback_historical_campaigns import (  # noqa: E402
    NOW,
    get_historical_campaign_fixtures,
)


def load_campaigns_with_db_enrichment() -> list[dict]:
    """Load campaigns from committed fixtures and enrich with database records if available."""
    campaigns = get_historical_campaign_fixtures()

    db_path = Path("/tmp/claude-1000/e261026b/asr/.bth/catalog/bathos.db")
    if db_path.is_file():
        try:
            import duckdb

            conn = duckdb.connect(str(db_path), read_only=True)
            for camp in campaigns:
                cid = camp["id"]
                # Query actual database for provenance / run count verification
                row = conn.execute(
                    "SELECT id, name, status FROM campaigns WHERE id = ?", [cid]
                ).fetchone()
                if row:
                    camp["db_verified"] = True
                    camp["db_name"] = row[1]
                    camp["db_status"] = row[2]
            conn.close()
        except Exception as exc:
            print(f"[warning] duckdb enrichment skipped: {exc}", file=sys.stderr)

    return campaigns


def main() -> int:
    print("=================================================================")
    print("STEP-BACK RETROSPECTIVE VALIDATION EXPERIMENT (R4)")
    print("Pre-registered Hypothesis: Step-back review triggers accurately")
    print("discriminate problematic historical campaigns from healthy lines.")
    print("=================================================================\n")

    campaigns = load_campaigns_with_db_enrichment()
    total = len(campaigns)

    tp = 0  # Problematic and detected
    fp = 0  # Healthy but flagged
    tn = 0  # Healthy and not flagged
    fn = 0  # Problematic but missed

    details = []

    for camp in campaigns:
        name = camp.get("name", camp.get("id"))
        ground_truth = bool(camp.get("ground_truth_problematic"))
        runs = camp.get("runs", [])
        proposed_tasks = camp.get("proposed_tasks")
        sidecar = camp.get("sidecar")

        # Evaluate triggers
        trigger_res = evaluate_all_triggers(
            camp, runs, sidecar=sidecar, proposed_tasks=proposed_tasks, now_dt=NOW
        )
        triggers_evaluated = trigger_res.get("triggers_evaluated", {})
        problematic_triggers = []
        for t_name, t_info in triggers_evaluated.items():
            if t_info.get("fired"):
                # fixation_positive in entropy_collapse indicates objective fulfilled (conclude/replicate), not a failure rut
                if t_name == "entropy_collapse" and t_info.get("annotation") == "fixation_positive":
                    continue
                problematic_triggers.append(t_name)

        fired = len(problematic_triggers) > 0
        active = problematic_triggers

        # Classification against ground truth
        if ground_truth and fired:
            tp += 1
            classification = "TP (Correct Detection)"
        elif not ground_truth and not fired:
            tn += 1
            classification = "TN (Correct Nominal)"
        elif not ground_truth and fired:
            fp += 1
            classification = "FP (False Alarm)"
        else:
            fn += 1
            classification = "FN (Missed Detection)"

        details.append(
            {
                "name": name,
                "ground_truth": "Problematic" if ground_truth else "Healthy",
                "predicted": "Problematic (Fired)" if fired else "Healthy (Clear)",
                "classification": classification,
                "active_triggers": ", ".join(active) if active else "none",
            }
        )

    # Compute confusion matrix metrics
    accuracy = (tp + tn) / total if total > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    fpr = fp / (fp + tn) if (fp + tn) > 0 else 0.0

    # Print breakdown table
    print(f"{'Campaign Name':<38} | {'Ground Truth':<12} | {'Predicted':<20} | {'Triggers'}")
    print("-" * 90)
    for d in details:
        print(
            f"{d['name']:<38} | {d['ground_truth']:<12} | {d['predicted']:<20} | {d['active_triggers']}"
        )

    print("\n-----------------------------------------------------------------")
    print(f"Total Campaigns Evaluated: {total}")
    print(f"True Positives (TP): {tp}  |  False Positives (FP): {fp}")
    print(f"True Negatives (TN): {tn}  |  False Negatives (FN): {fn}")
    print(f"Accuracy:            {accuracy:.4f}  (Pass threshold: >= 0.85)")
    print(f"Recall (Sensitivity): {recall:.4f}  (Pass threshold: >= 0.80)")
    print(f"Precision:           {precision:.4f}")
    print(f"False Positive Rate: {fpr:.4f}  (Pass threshold: <= 0.15)")
    print("-----------------------------------------------------------------")

    passed = accuracy >= 0.85 and recall >= 0.80 and fpr <= 0.15
    print(f"Pre-Registered Verdict: {'PASS' if passed else 'FAIL'}\n")

    # Construct payload conforming exactly to [result_schema]
    results_payload = {
        "accuracy": round(float(accuracy), 4),
        "recall": round(float(recall), 4),
        "precision": round(float(precision), 4),
        "false_positive_rate": round(float(fpr), 4),
        "n_campaigns_evaluated": int(total),
    }

    # Emit to BTH_RESULTS_PATH if executed under `bth run`
    bth_results_path = os.environ.get("BTH_RESULTS_PATH")
    if bth_results_path:
        Path(bth_results_path).write_text(json.dumps(results_payload, indent=2))
        print(f"Wrote result payload to $BTH_RESULTS_PATH: {bth_results_path}")

    # Also write to script-adjacent fallback
    fallback_path = Path(__file__).parent / "validate_stepback_retrospective.bth-results.json"
    fallback_path.write_text(json.dumps(results_payload, indent=2))
    print(f"Wrote fallback payload to: {fallback_path}")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
