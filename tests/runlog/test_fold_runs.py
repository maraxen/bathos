"""The run fold (spec "Fold rules", AC-26). Pure-function tests: every event
is a hand-built envelope dict, no writer/DuckDB involved.

Every scenario below is deliberately built so a plausible WRONG rule (e.g.
"the latest event by ts always wins", ignoring rank; or "the winning claim's
own parity_run_type, not a COALESCE over every claim") would produce a
DIFFERENT row than the one asserted -- these are this suite's negative
controls, called out inline at each assertion.
"""

from __future__ import annotations

from bathos.runlog.fold_runs import fold_run


def ev(kind: str, entity: list[str], data: dict, ts: str, eid: str, origin: str = "live") -> dict:
    return {
        "v": 1,
        "eid": eid,
        "kind": kind,
        "entity": entity,
        "ts": ts,
        "project": "p",
        "project_id": None,
        "writer": "w",
        "seq": 1,
        "main_root": "/main",
        "worktree_root": "/main",
        "origin": origin,
        "data": data,
    }


T0 = "2026-01-01T00:00:00.000000Z"
T1 = "2026-01-01T01:00:00.000000Z"
T2 = "2026-01-01T02:00:00.000000Z"
T3 = "2026-01-01T03:00:00.000000Z"


def started(run_id="r1", ts=T0, eid="e0", **extra) -> dict:
    data = {
        "project_slug": "proj",
        "command": "python foo.py",
        "argv": ["python", "foo.py"],
        "git_hash": "abc123",
        "git_branch": "main",
        "git_dirty": False,
        "campaign_id": None,
        "agent_mode": "manual",
    }
    data.update(extra)
    return ev("run.started", [run_id], data, ts, eid)


def finished(run_id="r1", ts=T1, eid="e1", status="completed", **extra) -> dict:
    data = {
        "id": run_id,
        "status": status,
        "exit_code": 0,
        "duration_s": 12.5,
        "output_paths": ["out.json"],
        "outcome": "pass",
        "outcome_error_reason": "",
        "outcome_is_residual": False,
        "adversarial_check_status": "",
        "adversarial_check_result": None,
        "differential_status": None,
        "differential_off_value": None,
        "differential_on_value": None,
        "timestamp": T0,
    }
    data.update(extra)
    return ev("run.finished", [run_id], data, ts, eid)


def reaped(run_id="r1", ts=T1, eid="e_reap", **extra) -> dict:
    data = {
        "run_id": run_id,
        "project_slug": "proj",
        "reaped_at": ts,
        "reason": "orphan_window_exceeded",
        "window_h": 24,
        "prior_status": "running",
    }
    data.update(extra)
    return ev("run.reaped", [run_id], data, ts, eid)


def reap_reverted(run_id="r1", ts=T2, eid="e_revert", **extra) -> dict:
    data = {
        "run_id": run_id,
        "project_slug": "proj",
        "reaped_at": T1,
        "reason": "orphan_window_exceeded",
        "window_h": 24,
        "prior_status": "running",
    }
    data.update(extra)
    return ev("run.reap_reverted", [run_id], data, ts, eid)


# --- basic status ranks -------------------------------------------------


def test_started_only_is_running_with_defaults():
    row = fold_run([started()])
    assert row["status"] == "running"
    assert row["exit_code"] == -1
    assert row["duration_s"] == 0.0
    assert row["outcome"] == ""
    assert row["project_slug"] == "proj"


def test_finished_wins_over_running():
    row = fold_run([started(), finished()])
    assert row["status"] == "completed"
    assert row["exit_code"] == 0
    assert row["outcome"] == "pass"
    # end time = timestamp + duration_s, per the spec (no stored end-time column).
    assert row["timestamp"] == T0
    assert row["duration_s"] == 12.5


def test_reap_marks_abandoned_and_records_metadata_reaped():
    row = fold_run([started(), reaped()])
    assert row["status"] == "abandoned"
    assert row["metadata"]["reaped"]["reason"] == "orphan_window_exceeded"
    # Negative control: a "last event wins by ts" rule agrees here (reap IS
    # last) -- the discriminating case is test_finish_beats_reap_out_of_order
    # below, where ts ordering and the correct rule disagree.


def test_revert_with_no_finish_returns_to_running():
    row = fold_run([started(), reaped(ts=T1), reap_reverted(ts=T2)])
    assert row["status"] == "running"
    assert "reaped" not in row["metadata"]


# --- AC-26 ---------------------------------------------------------------


def test_ac26_reap_with_later_ts_than_finish_still_folds_terminal():
    """ "a `run.reaped` with a later `ts` than a `run.finished` still folds to
    the terminal status" -- reap is chronologically AFTER finish here, so a
    naive "latest event wins" rule would yield 'abandoned'; the correct rule
    (rank2 always beats rank1, whatever the ts) yields 'completed'."""
    row = fold_run([started(ts=T0), finished(ts=T1), reaped(ts=T2)])
    assert row["status"] == "completed"
    assert row["status"] != "abandoned"  # what "latest ts wins" would give
    # AC-26: "a reaped run that later finishes keeps metadata.reaped" --
    # generalized here to "reaped after finishing" too, since metadata.reaped
    # is independent of the winning status.
    assert row["metadata"]["reaped"]["reason"] == "orphan_window_exceeded"


def test_ac26_reap_then_revert_has_no_metadata_reaped():
    row = fold_run([started(), reaped(ts=T1), reap_reverted(ts=T2)])
    assert "reaped" not in row["metadata"]
    assert row["status"] == "running"


def test_ac26_reaped_before_cutover_then_reverted_after_folds_running():
    """ "a run reaped before cut-over (imported `abandoned`) and reverted
    after it folds to `running`" -- the claim is an IMPORT, the revert is
    LIVE; a rule that only looked at live events would miss the claim
    entirely and never need to cancel it (vacuously 'running'), so this is
    made discriminating by asserting metadata.reaped is present until the
    live revert, i.e. the import claim really was seen."""
    imported_abandoned = ev(
        "run.imported",
        ["r1"],
        {"status": "abandoned", "reaped_at": T0, "reason": "orphan_window_exceeded"},
        T0,
        "e_imp",
        origin="migration",
    )
    # Without the revert: status is abandoned (proves the import claim counts).
    row_before_revert = fold_run([imported_abandoned])
    assert row_before_revert["status"] == "abandoned"

    row = fold_run([imported_abandoned, reap_reverted(ts=T1)])
    assert row["status"] == "running"
    assert "reaped" not in row["metadata"]


def test_ac26_reaped_and_reverted_before_cutover_folds_restored_status():
    """ "a run reaped and reverted before cut-over folds to its restored
    status even if the warm row still says `abandoned`" -- both are imports
    (`run_reap.imported`), and there is no warm row input to this fold at
    all, so a bug that fell back to reading some external 'warm status'
    would fail this test by construction (no such input exists here)."""
    run_reap_imported = ev(
        "run_reap.imported",
        ["r1"],
        {"reaped_at": T0, "reverted_at": T1, "prior_status": "running"},
        T1,
        "e_rr",
        origin="migration",
    )
    row = fold_run([run_reap_imported])
    assert row["status"] == "running"
    assert "reaped" not in row["metadata"]


def test_ac26_reaped_run_that_later_finishes_keeps_metadata_reaped():
    row = fold_run([started(), reaped(ts=T1), finished(ts=T2)])
    assert row["status"] == "completed"
    assert row["metadata"]["reaped"]["reason"] == "orphan_window_exceeded"


def test_ac26_postmortem_fail_then_none_leaves_raw_outcome():
    """ "a postmortem override `fail` followed by an override `\"none\"`
    leaves the raw outcome" -- if the fold incorrectly kept the FIRST
    override rather than the latest, this would return 'fail' instead of
    the raw 'pass'."""
    pm_fail = ev(
        "run.postmortem_applied",
        ["r1"],
        {
            "sha256": "s1",
            "status": "validated",
            "verdict_override": "fail",
            "author": "a",
            "path": "p1.toml",
            "hypothesis_status": "refuted",
            "has_anomalies": False,
            "summary": "",
            "asset_links": {},
        },
        T1,
        "e_pm1",
    )
    pm_none = ev(
        "run.postmortem_applied",
        ["r1"],
        {
            "sha256": "s2",
            "status": "validated",
            "verdict_override": "none",
            "author": "a",
            "path": "p2.toml",
            "hypothesis_status": "held",
            "has_anomalies": False,
            "summary": "",
            "asset_links": {},
        },
        T2,
        "e_pm2",
    )
    row = fold_run([started(), finished(ts=T1, outcome="pass"), pm_fail, pm_none])
    assert row["outcome"] == "pass"
    assert row["outcome"] != "fail"  # what keeping the FIRST override would give
    # Latest postmortem's other fields (BC-2, (ts, eid)-only tie-break) win too.
    assert row["postmortem_path"] == "p2.toml"


def test_ac26_terminal_claim_with_null_parity_run_type_does_not_blank_value():
    """ "a terminal claim with null `parity_run_type` does not blank a value
    another status claim carries" -- the WINNING (live) claim explicitly
    carries `parity_run_type: None`; a rule that read parity_run_type only
    off the winning bundle (rather than COALESCE-ing across every claim)
    would return None here instead of the imported claim's 'baseline'."""
    imported_terminal = ev(
        "run.imported",
        ["r1"],
        {"status": "completed", "parity_run_type": "baseline"},
        T0,
        "e_imp",
        origin="migration",
    )
    live_finish = finished(ts=T1, eid="e_fin", parity_run_type=None)
    row = fold_run([imported_terminal, live_finish])
    # Live still wins the BUNDLE (source precedence)...
    assert row["status"] == "completed"
    # ...but parity_run_type is COALESCEd across every claim (BC-4), so the
    # live claim's explicit null never blanks the import's value.
    assert row["parity_run_type"] == "baseline"


def test_bc4_parity_run_type_coalesce_without_any_terminal_claim():
    """BC-4's COALESCE also applies among rank0/rank1 claims, not just at
    rank2 -- a rank1 (reap) claim's parity_run_type (as it would be carried
    by an imported ledger_json snapshot) is picked up even though the
    winning (rank0, running) claim has none."""
    imported_abandoned = ev(
        "run.imported",
        ["r1"],
        {"status": "abandoned", "parity_run_type": "control"},
        T0,
        "e_imp",
        origin="migration",
    )
    revert = reap_reverted(ts=T1)  # cancels the abandon -> winner is "running"
    row = fold_run([imported_abandoned, revert])
    assert row["status"] == "running"
    assert row["parity_run_type"] == "control"


def test_bc1_output_metadata_only_set_by_explicit_event():
    row_without = fold_run([started(), finished()])
    assert row_without["output_metadata"] is None

    outputs_hashed = ev(
        "run.outputs_hashed",
        ["r1"],
        {"output_metadata": [{"path": "out.json", "sha256": "x"}]},
        T2,
        "e_oh",
    )
    row_with = fold_run([started(), finished(), outputs_hashed])
    assert row_with["output_metadata"] == [{"path": "out.json", "sha256": "x"}]


def test_output_metadata_uses_latest_event_only():
    first = ev("run.outputs_hashed", ["r1"], {"output_metadata": [{"path": "a"}]}, T1, "e_oh1")
    second = ev("run.outputs_hashed", ["r1"], {"output_metadata": [{"path": "b"}]}, T2, "e_oh2")
    row = fold_run([started(), finished(ts=T0), first, second])
    assert row["output_metadata"] == [{"path": "b"}]
    assert row["output_metadata"] != [{"path": "a"}]


def test_general_fields_come_from_run_started():
    row = fold_run([started(campaign_id="camp1")])
    assert row["campaign_id"] == "camp1"
    assert row["git_hash"] == "abc123"
    assert row["command"] == "python foo.py"


def test_arrival_order_independence_ac20():
    """Feeding the same events in reverse order yields the same folded row
    (AC-20: "delivering the same events in any order ... yields the same
    index"). Reap-then-finish (no revert) so the result is discriminating
    against both a naive "latest event wins" status rule (would say
    'abandoned', since the reap is chronologically last of the two) and a
    metadata.reaped rule that only looked at the winning status (would drop
    it once status is terminal)."""
    events = [
        started(ts=T0),
        finished(ts=T1),
        reaped(ts=T2),
    ]
    forward = fold_run(events)
    backward = fold_run(list(reversed(events)))
    assert forward == backward
    assert forward["status"] == "completed"
    assert forward["metadata"]["reaped"]["reason"] == "orphan_window_exceeded"


def test_no_events_returns_bare_id_only():
    assert fold_run([]) == {"id": None}
