"""Committed fixtures of historical campaigns and nominal controls for retrospective validation.

Includes real trajectories and testbed fixtures for:
- sp98-gap-augmented-testbed (plateau & guardrail violations)
- sp91-testbed-first-fix-development (entropy collapse / negative rut)
- mentee-convergence-deck (repeated marginals / confound)
- sp85-goalpost-shift (mid-epoch threshold edit)
- sp73-unscaled-array (large array scale-up without clean pilots)
- sp99-near-bound-rut (near-bound hits / critical proximity)
- sp101-temporal-abandonment (campaign staleness / idle days)
- 4 nominal control campaigns (healthy, progressing trajectories)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)


def get_historical_campaign_fixtures() -> list[dict[str, Any]]:
    """Return catalog of historical testbed campaigns and nominal controls with ground truth labels."""
    fixtures = []

    # 1. sp98-gap-augmented-testbed (Ground Truth: Problematic, plateau / guardrails)
    camp_sp98 = {
        "id": "6f792f0d-4ea4-47b6-b024-49c8e07f2e98",
        "name": "sp98-gap-augmented-testbed",
        "status": "concluded",
        "created_at": (NOW - timedelta(days=60)).isoformat(),
        "ground_truth_problematic": True,
        "problem_description": "Metric plateau with variance decay and guardrail violations",
        "runs": [
            {
                "run_id": f"sp98_{i}",
                "outcome": "guardrail_violated" if i in (1, 2, 4) else "marginal",
                "metric": 0.1240 + (0.00001 * (i % 2)),  # Completely flat plateau
                "results": {"primary_metric": 0.1240},
                "ended_at": (NOW - timedelta(days=50 - i)).isoformat(),
            }
            for i in range(8)
        ],
    }
    fixtures.append(camp_sp98)

    # 2. sp91-testbed-first-fix-development (Ground Truth: Problematic, entropy collapse)
    camp_sp91 = {
        "id": "ec49fb00-7d92-4789-ba16-47231a6ee3bc",
        "name": "sp91-testbed-first-fix-development",
        "status": "concluded",
        "created_at": (NOW - timedelta(days=70)).isoformat(),
        "ground_truth_problematic": True,
        "problem_description": "Outcome entropy collapse with 9 identical failed/unknown runs",
        "runs": [
            {
                "run_id": f"sp91_{i}",
                "outcome": "unknown",  # Identical outcome -> entropy collapse
                "metric": 0.0,
                "ended_at": (NOW - timedelta(days=60 - i)).isoformat(),
            }
            for i in range(9)
        ],
    }
    fixtures.append(camp_sp91)

    # 3. mentee-convergence-deck (Ground Truth: Problematic, repeated marginals)
    camp_mentee = {
        "id": "ff94e8bb-5268-488d-b3e3-1c896eb9d6ec",
        "name": "mentee-convergence-deck",
        "status": "concluded",
        "created_at": (NOW - timedelta(days=40)).isoformat(),
        "ground_truth_problematic": True,
        "problem_description": "Repeated marginal outcomes in stochastic convergence testing",
        "runs": [
            {
                "run_id": f"mentee_{i}",
                "outcome": "marginal",  # 4 consecutive marginals
                "metric": 0.45,
                "ended_at": (NOW - timedelta(days=30 - i)).isoformat(),
            }
            for i in range(4)
        ],
    }
    fixtures.append(camp_mentee)

    # 4. sp85-goalpost-shift (Ground Truth: Problematic, threshold edit)
    camp_goalpost = {
        "id": "sp85-goalpost-shift",
        "name": "sp85-goalpost-shift",
        "status": "open",
        "created_at": (NOW - timedelta(days=10)).isoformat(),
        "ground_truth_problematic": True,
        "problem_description": "Mid-campaign loosening of pass condition from val > 10 to val > 5",
        "runs": [
            {"run_id": "gp_1", "outcome": "pass", "pass_condition": "val > 10", "ended_at": (NOW - timedelta(days=5)).isoformat()},
            {"run_id": "gp_2", "outcome": "pass", "pass_condition": "val > 10", "ended_at": (NOW - timedelta(days=4)).isoformat()},
            {"run_id": "gp_3", "outcome": "pass", "pass_condition": "val > 5", "ended_at": (NOW - timedelta(days=3)).isoformat()},
        ],
    }
    fixtures.append(camp_goalpost)

    # 5. sp73-unscaled-array (Ground Truth: Problematic, scale up without pilots)
    camp_scale = {
        "id": "sp73-unscaled-array",
        "name": "sp73-unscaled-array",
        "status": "open",
        "proposed_tasks": 25,
        "created_at": (NOW - timedelta(days=5)).isoformat(),
        "ground_truth_problematic": True,
        "problem_description": "Large array job (25 tasks) launched without clean pilot runs",
        "runs": [
            {"run_id": "sc_1", "outcome": "marginal", "exit_code": 0, "ended_at": (NOW - timedelta(days=2)).isoformat()},
        ],
    }
    fixtures.append(camp_scale)

    # 6. sp99-near-bound-rut (Ground Truth: Problematic, near-bound hits)
    camp_bound = {
        "id": "sp99-near-bound-rut",
        "name": "sp99-near-bound-rut",
        "status": "open",
        "created_at": (NOW - timedelta(days=8)).isoformat(),
        "ground_truth_problematic": True,
        "problem_description": "Repeated critical proximity to decision boundary (bound=0.50)",
        "sidecar": {"benchmark": {"regression_threshold": 0.50}},
        "runs": [
            {"run_id": "nb_1", "outcome": "pass", "metric": 0.495, "ended_at": (NOW - timedelta(days=4)).isoformat()},
            {"run_id": "nb_2", "outcome": "pass", "metric": 0.502, "ended_at": (NOW - timedelta(days=3)).isoformat()},
            {"run_id": "nb_3", "outcome": "pass", "metric": 0.498, "ended_at": (NOW - timedelta(days=2)).isoformat()},
        ],
    }
    fixtures.append(camp_bound)

    # 7. sp101-temporal-abandonment (Ground Truth: Problematic, campaign staleness)
    camp_stale = {
        "id": "sp101-temporal-abandonment",
        "name": "sp101-temporal-abandonment",
        "status": "open",
        "created_at": (NOW - timedelta(days=30)).isoformat(),
        "ground_truth_problematic": True,
        "problem_description": "Abandoned campaign idle for 18 days with unresolved claim",
        "claim_path": ".bth/claims/claim_stale.toml",
        "runs": [
            {
                "run_id": f"stale_{i}",
                "outcome": "marginal" if i % 2 == 0 else "unknown",
                "clause_resolved": False,
                "ended_at": (NOW - timedelta(days=18)).isoformat(),
            }
            for i in range(16)
        ],
    }
    fixtures.append(camp_stale)

    # 8. ctrl-nominal-nvt-pilot (Ground Truth: Healthy / Nominal Control)
    camp_ctrl1 = {
        "id": "ctrl-nominal-nvt-pilot",
        "name": "ctrl-nominal-nvt-pilot",
        "status": "open",
        "created_at": (NOW - timedelta(days=4)).isoformat(),
        "ground_truth_problematic": False,
        "problem_description": "Nominal NVT equilibration line with monotonic temperature stability",
        "sidecar": {"outcomes": {"pass": {"condition": "temp_std < 5"}}},
        "runs": [
            {
                "run_id": f"nvt_ctrl_{i}",
                "outcome": "pass",
                "metric": round(3.5 - (0.5 * i), 2),  # temp_std: 3.5, 3.0, 2.5, 2.0, 1.5, 1.0 (improving)
                "pass_condition": "temp_std < 5",
                "ended_at": (NOW - timedelta(days=3 - (i * 0.4))).isoformat(),
            }
            for i in range(6)
        ],
    }
    fixtures.append(camp_ctrl1)

    # 9. ctrl-nominal-binder-screen (Ground Truth: Healthy / Nominal Control)
    camp_ctrl2 = {
        "id": "ctrl-nominal-binder-screen",
        "name": "ctrl-nominal-binder-screen",
        "status": "open",
        "created_at": (NOW - timedelta(days=6)).isoformat(),
        "ground_truth_problematic": False,
        "problem_description": "Healthy computational binder screening line with diverse progression",
        "runs": [
            {"run_id": "bs_1", "outcome": "marginal", "metric": 12.5, "ended_at": (NOW - timedelta(days=5)).isoformat()},
            {"run_id": "bs_2", "outcome": "pass", "metric": 8.2, "ended_at": (NOW - timedelta(days=4)).isoformat()},
            {"run_id": "bs_3", "outcome": "pass", "metric": 4.1, "ended_at": (NOW - timedelta(days=3)).isoformat()},
            {"run_id": "bs_4", "outcome": "pass", "metric": 2.0, "ended_at": (NOW - timedelta(days=2)).isoformat()},
            {"run_id": "bs_5", "outcome": "pass", "metric": 1.2, "ended_at": (NOW - timedelta(days=1)).isoformat()},
        ],
    }
    fixtures.append(camp_ctrl2)

    # 10. ctrl-nominal-conformation-sampling (Ground Truth: Healthy / Nominal Control)
    camp_ctrl3 = {
        "id": "ctrl-nominal-conformation-sampling",
        "name": "ctrl-nominal-conformation-sampling",
        "status": "open",
        "created_at": (NOW - timedelta(days=7)).isoformat(),
        "ground_truth_problematic": False,
        "problem_description": "Pooled ensemble conformational sampling with proper statistical controls",
        "runs": [
            {
                "run_id": f"conf_{i}",
                "outcome": "pass",
                "metric": 1.5 + (0.3 * i),
                "pass_condition": "rmsd < 3.0",
                "ended_at": (NOW - timedelta(days=5 - (i * 0.5))).isoformat(),
            }
            for i in range(7)
        ],
    }
    fixtures.append(camp_ctrl3)

    # 11. ctrl-nominal-pilot-scale-up (Ground Truth: Healthy / Nominal Control)
    camp_ctrl4 = {
        "id": "ctrl-nominal-pilot-scale-up",
        "name": "ctrl-nominal-pilot-scale-up",
        "status": "open",
        "proposed_tasks": 20,
        "created_at": (NOW - timedelta(days=5)).isoformat(),
        "ground_truth_problematic": False,
        "problem_description": "Array submission with 4 clean preceding pilot passes",
        "runs": [
            {"run_id": f"pilot_{i}", "outcome": "pass", "exit_code": 0, "ended_at": (NOW - timedelta(days=4 - i)).isoformat()}
            for i in range(4)
        ],
    }
    fixtures.append(camp_ctrl4)

    return fixtures
