"""Tests for System-Evaluated Event Triggers (spec 261027 Section 2)."""

from datetime import UTC, datetime, timedelta

from bathos.stepback.triggers import (
    evaluate_all_triggers,
    evaluate_campaign_staleness,
    evaluate_entropy_collapse,
    evaluate_metric_plateau,
    evaluate_near_bound_hits,
    evaluate_repeated_marginal,
    evaluate_scale_up,
    evaluate_threshold_edit,
)


def test_repeated_marginal_sample_size_guard():
    # N=0, 1, 2 must not fire
    assert not evaluate_repeated_marginal([])["fired"]
    assert not evaluate_repeated_marginal([{"outcome": "marginal"}])["fired"]
    res = evaluate_repeated_marginal([{"outcome": "marginal"}, {"outcome": "marginal"}])
    assert not res["fired"]
    assert res["reason"] == "insufficient_runs"


def test_repeated_marginal_streak_and_frequency():
    # Streak of 3 with N=3 fires
    runs_streak = [
        {"run_id": "r1", "outcome": "marginal"},
        {"run_id": "r2", "outcome": "marginal"},
        {"run_id": "r3", "outcome": "marginal"},
    ]
    res = evaluate_repeated_marginal(runs_streak)
    assert res["fired"]
    assert res["streak"] == 3
    assert res["reason"] == "streak_exceeded"
    assert res["offending_run_ids"] == ["r1", "r2", "r3"]

    # Frequency of 0.60 in window of 5 (3 out of 5 marginal, streak broken)
    runs_freq = [
        {"run_id": "r1", "outcome": "marginal"},
        {"run_id": "r2", "outcome": "marginal"},
        {"run_id": "r3", "outcome": "pass"},
        {"run_id": "r4", "outcome": "marginal"},
        {"run_id": "r5", "outcome": "fail"},
    ]
    res_freq = evaluate_repeated_marginal(runs_freq)
    assert res_freq["fired"]
    assert res_freq["frequency"] == 0.60
    assert res_freq["reason"] == "frequency_exceeded"

    # Nominal streak and low frequency does not fire
    runs_nominal = [
        {"outcome": "pass"},
        {"outcome": "marginal"},
        {"outcome": "pass"},
        {"outcome": "pass"},
        {"outcome": "marginal"},
    ]
    res_nom = evaluate_repeated_marginal(runs_nominal)
    assert not res_nom["fired"]


def test_repeated_marginal_post_pivot_epoch_reset():
    runs = [
        {"run_id": "r1", "outcome": "marginal"},
        {"run_id": "r2", "outcome": "marginal"},
        {"run_id": "r_pivot", "outcome": "marginal"},
        {"run_id": "r4", "outcome": "marginal"},  # first run after pivot
    ]
    # Active runs after r_pivot is only 1 (r4) -> insufficient runs guard
    res = evaluate_repeated_marginal(runs, anchor_run_id="r_pivot")
    assert not res["fired"]
    assert res["run_count"] == 1


def test_near_bound_hits_guard_and_detection():
    # Insufficient runs guard
    assert not evaluate_near_bound_hits([], bound=0.50)["fired"]
    assert not evaluate_near_bound_hits([{"metric": 0.51}], bound=0.50)["fired"]

    # No decision boundary resolves -> does not fire
    runs = [{"metric": 0.51}, {"metric": 0.52}, {"metric": 0.49}]
    assert not evaluate_near_bound_hits(runs, bound=None)["fired"]

    # Bound = 0.50, epsilon = 0.05 (10% of B). Hits are in [0.45, 0.55].
    runs_hits = [
        {"run_id": "r1", "metric": 0.48},  # |0.48 - 0.50| / 0.05 = 0.40 <= 1.0 (hit)
        {"run_id": "r2", "metric": 0.49},  # |0.49 - 0.50| / 0.05 = 0.20 <= 1.0 (hit & d<=0.25)
        {"run_id": "r3", "metric": 0.70},  # miss
    ]
    res = evaluate_near_bound_hits(runs_hits, bound=0.50)
    assert res["fired"]
    assert res["hits"] == 2


def test_near_bound_critical_proximity():
    # Bound 10.0, epsilon = 1.0. Latest run is 10.15 -> distance = 0.15 <= 0.25 (critical proximity)
    runs = [
        {"run_id": "r1", "metric": 12.0},
        {"run_id": "r2", "metric": 11.5},
        {"run_id": "r3", "metric": 10.15},
    ]
    res = evaluate_near_bound_hits(runs, bound=10.0)
    assert res["fired"]
    assert res["reason"] == "critical_proximity"


def test_near_bound_b_zero_stddev_scaling():
    # Bound B = 0.0. Epsilon should scale with sample stddev of y
    runs = [
        {"run_id": "r1", "metric": 0.002},
        {"run_id": "r2", "metric": -0.001},
        {"run_id": "r3", "metric": 0.0015},
    ]
    res = evaluate_near_bound_hits(runs, bound=0.0)
    # stddev is non-zero, margin is scaled by stddev
    assert res["margin"] > 0
    assert res["margin"] < 0.1  # properly scaled, not hardcoded to 1.0


def test_entropy_collapse_guards_and_fixation():
    # N < 5 guard
    assert not evaluate_entropy_collapse([{"outcome": "pass"}] * 4)["fired"]

    # 5 identical passes: entropy = 0, fixation_positive
    res_pos = evaluate_entropy_collapse([{"outcome": "pass"}] * 5)
    assert res_pos["fired"]
    assert res_pos["entropy_nats"] == 0.0
    assert res_pos["annotation"] == "fixation_positive"

    # 5 identical fails: entropy = 0, fixation_negative
    res_neg = evaluate_entropy_collapse([{"outcome": "fail"}] * 5)
    assert res_neg["fired"]
    assert res_neg["entropy_nats"] == 0.0
    assert res_neg["annotation"] == "fixation_negative"

    # Diverse outcomes: entropy >= 0.35 nats
    diverse_runs = [
        {"outcome": "pass"},
        {"outcome": "pass"},
        {"outcome": "fail"},
        {"outcome": "marginal"},
        {"outcome": "inconclusive"},
    ]
    res_div = evaluate_entropy_collapse(diverse_runs)
    assert not res_div["fired"]
    assert res_div["entropy_nats"] > 0.35


def test_metric_plateau_detection():
    # N < 5 guard
    assert not evaluate_metric_plateau([{"metric": 5.0}] * 4)["fired"]

    # Completely flat metric: slope=0, std=0 -> fires
    flat_runs = [{"metric": 10.0}] * 6
    res_flat = evaluate_metric_plateau(flat_runs)
    assert res_flat["fired"]
    assert res_flat["slope"] == 0.0
    assert res_flat["stddev"] == 0.0

    # Improving metric with clear slope -> does not fire
    improving_runs = [{"metric": float(i)} for i in range(6)]
    res_imp = evaluate_metric_plateau(improving_runs)
    assert not res_imp["fired"]
    assert res_imp["slope"] > 0.5


def test_metric_plateau_near_zero_handling():
    # Centered around zero (|mean| < 0.01) with near-zero slope and stddev
    near_zero_runs = [
        {"metric": 0.00010},
        {"metric": 0.00012},
        {"metric": 0.00011},
        {"metric": 0.00009},
        {"metric": 0.00010},
    ]
    res = evaluate_metric_plateau(near_zero_runs)
    assert res["is_near_zero"] is True
    assert res["fired"] is True


def test_campaign_staleness():
    now = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    old_time = now - timedelta(days=15)

    # 1. Temporal Abandonment: open campaign with last run 15 days ago
    camp = {"status": "open", "created_at": old_time.isoformat()}
    old_run = {"ended_at": old_time.isoformat()}
    res_stale = evaluate_campaign_staleness(camp, [old_run], now_dt=now)
    assert res_stale["fired"]
    assert "temporal_abandonment" in res_stale["reasons"]

    # Recent run -> not abandoned
    recent_run = {"ended_at": (now - timedelta(days=2)).isoformat()}
    res_fresh = evaluate_campaign_staleness(camp, [recent_run], now_dt=now)
    assert not res_fresh["fired"]

    # 2. Hypothesis Wheel-Spinning: 15 runs, active claim, 0 clauses resolved
    camp_claim = {"status": "open", "claim_path": "claim.toml"}
    runs_no_progress = [{"ended_at": now.isoformat(), "clause_resolved": False} for _ in range(15)]
    res_spin = evaluate_campaign_staleness(camp_claim, runs_no_progress, now_dt=now)
    assert res_spin["fired"]
    assert "hypothesis_wheel_spinning" in res_spin["reasons"]

    # 3. Uncapped Exploratory Drift: mode="exploration", 25 runs, open
    camp_explore = {"status": "open", "mode": "exploration"}
    explore_runs = [{"ended_at": now.isoformat()} for _ in range(25)]
    res_drift = evaluate_campaign_staleness(camp_explore, explore_runs, now_dt=now)
    assert res_drift["fired"]
    assert "uncapped_exploratory_drift" in res_drift["reasons"]


def test_evaluate_all_triggers_aggregation():
    runs = [{"outcome": "marginal"}, {"outcome": "marginal"}, {"outcome": "marginal"}]
    camp = {"status": "open", "created_at": datetime.now(UTC).isoformat()}
    all_res = evaluate_all_triggers(camp, runs)
    assert all_res["fired"] is True
    assert "repeated_marginal" in all_res["active_triggers"]


def test_threshold_edit_guards_and_mutation():
    # N < 2 guard
    assert not evaluate_threshold_edit([])["fired"]
    assert not evaluate_threshold_edit([{"pass_condition": "val > 10"}])["fired"]

    # Consistent conditions across runs -> does not fire
    runs_consistent = [
        {"run_id": "r1", "pass_condition": "val > 10"},
        {"run_id": "r2", "pass_condition": "val > 10"},
        {"run_id": "r3", "pass_condition": "val > 10"},
    ]
    res_cons = evaluate_threshold_edit(runs_consistent)
    assert not res_cons["fired"]
    assert res_cons["reason"] == "thresholds_consistent"

    # Loosened condition from val > 10 to val > 5 -> fires!
    runs_mutated = [
        {"run_id": "r1", "pass_condition": "val > 10"},
        {"run_id": "r2", "pass_condition": "val > 10"},
        {"run_id": "r3", "pass_condition": "val > 5"},
    ]
    res_mut = evaluate_threshold_edit(runs_mutated)
    assert res_mut["fired"]
    assert res_mut["reason"] == "threshold_condition_mutated"
    assert res_mut["baseline_run_id"] == "r1"
    assert res_mut["offending_run_id"] == "r3"
    assert res_mut["baseline_condition"] == "val > 10"
    assert res_mut["mutated_condition"] == "val > 5"

    # Nested sidecar outcomes mutation
    runs_sidecar = [
        {"run_id": "r1", "sidecar": {"outcomes": {"pass": {"condition": "energy < -50"}}}},
        {"run_id": "r2", "sidecar": {"outcomes": {"pass": {"condition": "energy < -30"}}}},
    ]
    res_sc = evaluate_threshold_edit(runs_sidecar)
    assert res_sc["fired"]
    assert res_sc["offending_run_id"] == "r2"

    # Post-pivot epoch reset
    res_pivot = evaluate_threshold_edit(runs_mutated, anchor_run_id="r2")
    # Only r3 in active epoch -> N=1 -> insufficient runs guard
    assert not res_pivot["fired"]
    assert res_pivot["reason"] == "insufficient_runs"


def test_scale_up_trigger():
    # 1. Not large array (<= 10 tasks)
    camp = {"runs": []}
    assert not evaluate_scale_up(camp, proposed_tasks=10)["fired"]
    assert not evaluate_scale_up(camp, proposed_tasks="1-8")["fired"]

    # 2. Large array (>10 tasks) with insufficient pilot runs (0 or 1 clean runs)
    res_nopilot = evaluate_scale_up(camp, proposed_tasks=25)
    assert res_nopilot["fired"]
    assert res_nopilot["reason"] == "insufficient_pilot_runs"

    # 3. Large array with unclean pilot history (crash or fail)
    camp_dirty = {
        "runs": [
            {"outcome": "pass", "exit_code": 0},
            {"outcome": "fail", "exit_code": 0},
            {"outcome": "pass", "exit_code": 0},
        ]
    }
    res_dirty = evaluate_scale_up(camp_dirty, proposed_tasks=20)
    assert res_dirty["fired"]
    assert res_dirty["reason"] == "unclean_pilot_history"

    # 4. Large array with sufficient clean pilot runs (3 passes, 0 crashes)
    camp_clean = {
        "runs": [
            {"outcome": "pass", "exit_code": 0},
            {"outcome": "promising", "exit_code": 0},
            {"outcome": "pass", "exit_code": 0},
        ]
    }
    res_clean = evaluate_scale_up(camp_clean, proposed_tasks="1-50")
    assert not res_clean["fired"]
    assert res_clean["reason"] == "sufficient_clean_pilots"
    assert res_clean["clean_pilot_runs"] == 3


def test_evaluate_all_triggers_with_scale_up_and_threshold():
    camp = {
        "status": "open",
        "created_at": datetime.now(UTC).isoformat(),
        "runs": [{"outcome": "pass", "pass_condition": "val > 10"}],
    }
    runs = [
        {"run_id": "r1", "outcome": "pass", "pass_condition": "val > 10"},
        {"run_id": "r2", "outcome": "pass", "pass_condition": "val > 5"},
    ]
    res = evaluate_all_triggers(camp, runs, proposed_tasks=20)
    assert res["fired"] is True
    assert "threshold_edit" in res["active_triggers"]
    assert "scale_up" in res["active_triggers"]


def test_evaluate_all_triggers_snooze_filtering():
    camp = {"status": "open"}
    # 3 consecutive marginal runs normally fires repeated_marginal
    runs = [
        {"run_id": "r1", "outcome": "marginal"},
        {"run_id": "r2", "outcome": "marginal"},
        {"run_id": "r3", "outcome": "marginal"},
    ]

    now = datetime.now(UTC)
    # Active snooze: until run 5, epoch 24h in future
    snooze_active = {
        "repeated_marginal": {
            "snooze_until_runs": 5,
            "snooze_until_epoch": (now + timedelta(hours=24)).isoformat(),
        }
    }

    res_active = evaluate_all_triggers(
        camp, runs, snoozed_triggers=snooze_active, now_dt=now
    )
    # repeated_marginal fired internally, but is snoozed, so excluded from active_triggers
    assert res_active["triggers_evaluated"]["repeated_marginal"]["fired"] is True
    assert res_active["triggers_evaluated"]["repeated_marginal"]["snoozed"] is True
    assert "repeated_marginal" not in res_active["active_triggers"]
    assert "repeated_marginal" in res_active["snoozed_triggers"]
    assert res_active["fired"] is False

    # Expired by run count: runs length 6 > snooze_until_runs 5
    runs_more = runs + [
        {"run_id": "r4", "outcome": "marginal"},
        {"run_id": "r5", "outcome": "marginal"},
        {"run_id": "r6", "outcome": "marginal"},
    ]
    res_run_expired = evaluate_all_triggers(
        camp, runs_more, snoozed_triggers=snooze_active, now_dt=now
    )
    assert "repeated_marginal" in res_run_expired["active_triggers"]
    assert "repeated_marginal" not in res_run_expired["snoozed_triggers"]
    assert res_run_expired["fired"] is True

    # Expired by epoch: now is 25h later
    res_epoch_expired = evaluate_all_triggers(
        camp, runs, snoozed_triggers=snooze_active, now_dt=now + timedelta(hours=25)
    )
    assert "repeated_marginal" in res_epoch_expired["active_triggers"]
    assert "repeated_marginal" not in res_epoch_expired["snoozed_triggers"]
    assert res_epoch_expired["fired"] is True


def test_snooze_stepback_trigger_persistence_and_refusal(tmp_path):
    import duckdb
    import pytest

    from bathos.campaigns import create_campaign
    from bathos.errors import StepBackAgentOverrideRefusedError
    from bathos.stepback.catalog import get_escalation_state, save_escalation_cool
    from bathos.stepback.engine import snooze_stepback_trigger
    from bathos.stepback.schema import EscalationRecord

    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir(parents=True)
    db_path = cat_dir / "bathos.db"
    con = duckdb.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE campaigns (
            id TEXT PRIMARY KEY,
            project_slug TEXT,
            name TEXT,
            mode TEXT,
            question TEXT,
            hypothesis TEXT,
            status TEXT,
            started_at TEXT,
            concluded_at TEXT,
            conclusion TEXT,
            outcome_label TEXT,
            parent_campaign_id TEXT,
            stopping_threshold REAL,
            negative_check TEXT,
            claim_path TEXT,
            claim_sha256 TEXT,
            claim_mode TEXT
        );
        CREATE TABLE campaign_runs (campaign_id TEXT, run_id TEXT);
        CREATE TABLE runs (id TEXT PRIMARY KEY, outcome TEXT, duration_s REAL);
        CREATE TABLE IF NOT EXISTS stepback_escalations (
            id TEXT PRIMARY KEY,
            campaign_id TEXT,
            project_slug TEXT,
            level INTEGER,
            status TEXT,
            opened_at TEXT,
            opened_by_kind TEXT,
            trigger_name TEXT,
            trigger_reason TEXT,
            resolution_note TEXT,
            review_epoch INTEGER,
            metadata TEXT
        );
        """
    )
    camp = create_campaign(
        con,
        name="Snooze Test",
        project_slug="snooze_proj",
        mode="exploration",
        catalog_dir=cat_dir,
    )
    con.close()

    # 1. Validation: Rationale required
    with pytest.raises(ValueError, match="Rationale is required"):
        snooze_stepback_trigger(
            camp.id,
            trigger_name="repeated_marginal",
            rationale="   ",
            catalog_dir=cat_dir,
        )

    # 2. Refusal: Cannot snooze Level 3 trigger name
    with pytest.raises(StepBackAgentOverrideRefusedError, match="Cannot snooze Level 3"):
        snooze_stepback_trigger(
            camp.id,
            trigger_name="level_3",
            rationale="Trying to bypass chair",
            catalog_dir=cat_dir,
        )

    # 3. Successful snooze
    snooze_res = snooze_stepback_trigger(
        camp.id,
        trigger_name="repeated_marginal",
        runs_window=5,
        epoch_hours=24,
        rationale="Expected noise in pilot phase",
        reviewer_id="reviewer_01",
        catalog_dir=cat_dir,
    )
    assert snooze_res["ok"] is True
    assert snooze_res["status"] == "snoozed"
    assert snooze_res["trigger_name"] == "repeated_marginal"
    assert snooze_res["snooze_until_runs"] == 5

    # 4. Refusal when campaign is at Level 3
    esc3 = EscalationRecord(
        escalation_id="esc_l3_test",
        campaign_id=camp.id,
        project_slug="snooze_proj",
        level=3,
        status="active",
        opened_at=datetime.now(UTC).isoformat(),
        opened_by_kind="chair",
        trigger_reason="Human Chair escalation",
    )
    save_escalation_cool(esc3, cat_dir)
    assert get_escalation_state(camp.id, catalog_dir=cat_dir).current_level == 3

    with pytest.raises(StepBackAgentOverrideRefusedError, match="Cannot snooze Level 3"):
        snooze_stepback_trigger(
            camp.id,
            trigger_name="repeated_marginal",
            rationale="Attempting to snooze during L3 hold",
            catalog_dir=cat_dir,
        )


def test_cli_and_mcp_snooze(tmp_path, monkeypatch):
    import asyncio

    import duckdb

    from bathos.campaigns import create_campaign
    from bathos.cli_cyclopts import app
    from bathos.mcp import stepback_snooze
    from tests._cyclopts_runner import CyclopticRunner

    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir(parents=True)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(cat_dir))

    db_path = cat_dir / "bathos.db"
    con = duckdb.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE campaigns (
            id TEXT PRIMARY KEY,
            project_slug TEXT,
            name TEXT,
            mode TEXT,
            question TEXT,
            hypothesis TEXT,
            status TEXT,
            started_at TEXT,
            concluded_at TEXT,
            conclusion TEXT,
            outcome_label TEXT,
            parent_campaign_id TEXT,
            stopping_threshold REAL,
            negative_check TEXT,
            claim_path TEXT,
            claim_sha256 TEXT,
            claim_mode TEXT
        );
        CREATE TABLE campaign_runs (campaign_id TEXT, run_id TEXT);
        CREATE TABLE runs (id TEXT PRIMARY KEY, outcome TEXT, duration_s REAL);
        CREATE TABLE IF NOT EXISTS stepback_escalations (
            id TEXT PRIMARY KEY,
            campaign_id TEXT,
            project_slug TEXT,
            level INTEGER,
            status TEXT,
            opened_at TEXT,
            opened_by_kind TEXT,
            trigger_name TEXT,
            trigger_reason TEXT,
            resolution_note TEXT,
            review_epoch INTEGER,
            metadata TEXT
        );
        """
    )
    camp = create_campaign(
        con,
        name="CLI Snooze Test",
        project_slug="cli_snooze",
        mode="exploration",
        catalog_dir=cat_dir,
    )
    con.close()

    # Test CLI snooze
    runner = CyclopticRunner()
    res = runner.invoke(
        app,
        [
            "stepback",
            "snooze",
            camp.id,
            "--trigger",
            "repeated_marginal",
            "--runs",
            "5",
            "--rationale",
            "Acknowledged for exploratory sweep",
            "--catalog-dir",
            str(cat_dir),
        ],
    )
    assert res.exit_code == 0
    assert "Snoozed trigger 'repeated_marginal'" in res.stdout

    # Test MCP snooze
    mcp_res = asyncio.run(
        stepback_snooze(
            campaign_id=camp.id,
            trigger_name="near_bound",
            runs_window=3,
            rationale="Known boundary testing",
        )
    )
    assert mcp_res["ok"] is True
    assert mcp_res["status"] == "snoozed"
    assert mcp_res["trigger_name"] == "near_bound"


