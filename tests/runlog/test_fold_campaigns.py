"""The campaign fold (spec "Campaign-derived values", AC-17 BC-6/BC-7/BC-9,
AC-22, AC-20). Pure-function tests: every event is a hand-built envelope
dict, no writer/DuckDB/ingest involved -- `test_ingest_wave_b.py` covers the
end-to-end (and real-emitter) path.

Every scenario below is deliberately built so a plausible WRONG rule (e.g.
"intersection of membership sources" instead of the union; "sort by the
membership event's own ts" instead of the member run's folded start time;
"the raw fragment outcome" instead of the postmortem-overridden folded
outcome; "keep whatever was locked before a mismatch" instead of resetting
the whole campaign to NULL) would produce a DIFFERENT result than the one
asserted -- these are this suite's negative controls, called out inline.
"""

from __future__ import annotations

from bathos.runlog.fold_campaigns import fold_campaign


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
T4 = "2026-01-01T04:00:00.000000Z"


def created(
    campaign_id="c1", ts=T0, eid="e_created", mode="sequential", name="camp", **extra
) -> dict:
    data = {
        "id": campaign_id,
        "project_slug": "proj",
        "name": name,
        "mode": mode,
        "question": None,
        "hypothesis": None,
        "status": "open",
        "started_at": ts,
        "concluded_at": None,
        "conclusion": None,
        "outcome_label": None,
        "parent_campaign_id": None,
        "stopping_threshold": None,
        "negative_check": None,
        "claim_path": None,
        "claim_sha256": None,
        "claim_mode": None,
    }
    data.update(extra)
    return ev("campaign.created", [campaign_id], data, ts, eid)


def concluded(campaign_id="c1", ts=T3, eid="e_concl", outcome_label="pass", **extra) -> dict:
    data = {
        "concluded_at": ts,
        "outcome_label": outcome_label,
        "conclusion": "done",
        "negative_check": None,
    }
    data.update(extra)
    return ev("campaign.concluded", [campaign_id], data, ts, eid)


def run_started(run_id, ts, eid, campaign_id=None, sidecar=None, **extra) -> dict:
    data = {
        "project_slug": "proj",
        "command": "python foo.py",
        "argv": ["python", "foo.py"],
        "git_hash": "abc",
        "git_branch": "main",
        "git_dirty": False,
        "timestamp": ts,
        "campaign_id": campaign_id,
        "agent_mode": "manual",
        "sidecar": sidecar or {},
    }
    data.update(extra)
    return ev("run.started", [run_id], data, ts, eid)


def run_finished(run_id, ts, eid, outcome="pass", **extra) -> dict:
    data = {
        "id": run_id,
        "status": "completed",
        "exit_code": 0,
        "duration_s": 1.0,
        "output_paths": [],
        "outcome": outcome,
        "outcome_error_reason": "",
        "outcome_is_residual": False,
        "adversarial_check_status": "",
    }
    data.update(extra)
    return ev("run.finished", [run_id], data, ts, eid)


def run_added(campaign_id, run_id, ts, eid, evalue=1.0, seq_position=1) -> dict:
    return ev(
        "campaign.run_added",
        [campaign_id, run_id],
        {
            "campaign_id": campaign_id,
            "run_id": run_id,
            "evalue": evalue,
            "seq_position": seq_position,
        },
        ts,
        eid,
    )


def campaign_run_imported(campaign_id, run_id, ts, eid, evalue) -> dict:
    return ev(
        "campaign_run.imported",
        [campaign_id, run_id],
        {"campaign_id": campaign_id, "run_id": run_id, "evalue": evalue},
        ts,
        eid,
        origin="migration",
    )


def sidecar_decl(null_rate=0.1, alt_rate=0.9, threshold=0.05, pass_labels=("pass",)) -> dict:
    """A minimal `run.started.data["sidecar"]` payload -- shaped like
    `dataclasses.asdict(Sidecar(...))` (see `fold_campaigns._sidecar_from_
    declaration`), with just the fields evalue computation touches."""
    outcomes = {
        label: {"condition": "true", "decision": "", "is_residual": False} for label in pass_labels
    }
    outcomes["marginal"] = {"condition": "", "decision": "", "is_residual": False}
    return {
        "kind": "experiment",
        "result_schema": {},
        "outcomes": outcomes,
        "popper_null_pass_rate": null_rate,
        "popper_alt_pass_rate": alt_rate,
        "popper_stopping_threshold": threshold,
        "popper_weights": {},
    }


def _fold(
    campaign_id,
    campaign_events,
    run_added_events=(),
    imported_member_events=(),
    linked_run_ids=(),
    member_run_events=None,
):
    return fold_campaign(
        campaign_id,
        list(campaign_events),
        list(run_added_events),
        list(imported_member_events),
        set(linked_run_ids),
        member_run_events or {},
    )


# --- general field merge ----------------------------------------------------


def test_general_fields_from_created():
    row, _ = _fold("c1", [created(name="my camp", mode="exploration")])
    assert row["name"] == "my camp"
    assert row["mode"] == "exploration"
    assert row["project_slug"] == "proj"
    assert row["status"] == "open"


def test_concluded_forces_status_and_sets_fields():
    row, _ = _fold("c1", [created(mode="exploration"), concluded(outcome_label="confounded")])
    assert row["status"] == "concluded"
    assert row["outcome_label"] == "confounded"
    assert row["concluded_at"] == T3


def test_claim_bypassed_sets_claim_mode():
    claim_bypassed = ev("campaign.claim_bypassed", ["c1"], {"claim_mode": "bypassed"}, T1, "e_byp")
    row, _ = _fold("c1", [created(mode="exploration"), claim_bypassed])
    assert row["claim_mode"] == "bypassed"


def test_threshold_set_event_is_not_a_general_field():
    """Spec-ambiguity resolution (module docstring): `campaign.threshold_set`
    plays no role in the fold's OWN `stopping_threshold` -- it is recomputed
    fresh by the member walk every time, never read back from this event.
    Negative control: a rule that folded `stopping_threshold` as an ordinary
    general field would return 0.99 here; the member walk (no members at
    all) instead yields None."""
    threshold_set = ev("campaign.threshold_set", ["c1"], {"stopping_threshold": 0.99}, T1, "e_ts")
    row, _ = _fold("c1", [created(mode="sequential"), threshold_set])
    assert row["stopping_threshold"] is None
    assert row["stopping_threshold"] != 0.99


# --- BC-6 membership union ---------------------------------------------------


def test_bc6_membership_is_union_of_all_three_sources():
    """Three distinct runs, one per membership source -- a rule using only
    ONE source (e.g. intersection, or "run_added only") would miss at least
    one of them."""
    member_events = {
        "r-added": [run_started("r-added", T1, "e1", campaign_id=None)],
        "r-imported": [run_started("r-imported", T2, "e2", campaign_id=None)],
        "r-linked": [run_started("r-linked", T3, "e3", campaign_id="c1")],
    }
    row, campaign_runs = _fold(
        "c1",
        [created(mode="exploration")],
        run_added_events=[run_added("c1", "r-added", T1, "e_ra")],
        imported_member_events=[campaign_run_imported("c1", "r-imported", T2, "e_ci", evalue=0.5)],
        linked_run_ids={"r-linked"},
        member_run_events=member_events,
    )
    ids = {cr["run_id"] for cr in campaign_runs}
    assert ids == {"r-added", "r-imported", "r-linked"}


# --- ordering (start time, then run_id) -------------------------------------


def test_members_sorted_by_folded_start_time_not_membership_event_ts():
    """r-late's `campaign.run_added` event arrives (ts) BEFORE r-early's, but
    r-early's run actually STARTED earlier -- the spec sorts by the folded
    run's own start time, never the membership event's ts. A rule that
    sorted by the run_added event's own ts would order these the other way
    around, producing a different seq_position assignment."""
    member_events = {
        "r-early": [
            run_started("r-early", T1, "e1", sidecar=sidecar_decl()),
            run_finished("r-early", T1, "e1f", outcome="pass"),
        ],
        "r-late": [
            run_started("r-late", T2, "e2", sidecar=sidecar_decl()),
            run_finished("r-late", T2, "e2f", outcome="pass"),
        ],
    }
    row, campaign_runs = _fold(
        "c1",
        [created(mode="sequential")],
        run_added_events=[
            run_added("c1", "r-late", T0, "e_ra_late"),  # arrives first...
            run_added("c1", "r-early", T3, "e_ra_early"),  # ...but ts is later
        ],
        member_run_events=member_events,
    )
    by_run = {cr["run_id"]: cr["seq_position"] for cr in campaign_runs}
    assert by_run["r-early"] == 1
    assert by_run["r-late"] == 2


def test_null_start_time_sorts_first():
    """A member with no folded timestamp (no run.started ever indexed for it,
    only a bare campaign_run.imported) sorts BEFORE every real timestamp."""
    member_events = {
        "r-no-ts": [],
        "r-ts": [run_started("r-ts", T1, "e1", sidecar=sidecar_decl())],
    }
    row, campaign_runs = _fold(
        "c1",
        [created(mode="sequential")],
        imported_member_events=[campaign_run_imported("c1", "r-no-ts", T0, "e_ci", evalue=1.0)],
        linked_run_ids={"r-ts"},
        member_run_events=member_events,
    )
    by_run = {cr["run_id"]: cr["seq_position"] for cr in campaign_runs}
    assert by_run["r-no-ts"] == 1
    assert by_run["r-ts"] == 2


# --- evalue ------------------------------------------------------------------


def test_evalue_computed_from_declaration_when_present():
    member_events = {
        "r1": [
            run_started("r1", T1, "e1", sidecar=sidecar_decl(null_rate=0.1, alt_rate=0.9)),
            run_finished("r1", T2, "e1f", outcome="pass"),
        ],
    }
    row, campaign_runs = _fold(
        "c1",
        [created(mode="sequential")],
        linked_run_ids={"r1"},
        member_run_events=member_events,
    )
    assert campaign_runs[0]["evalue"] == 9.0  # alt/null = 0.9/0.1


def test_evalue_none_without_declaration_and_without_import():
    """Spec: "a live member with no declaration has evalue NULL" -- NOT the
    live single-add path's 1.0 default. Negative control: a rule mirroring
    `_add_run_to_campaign_impl`'s OWN default (1.0) would fail this."""
    member_events = {"r1": [run_started("r1", T1, "e1", sidecar=None)]}
    row, campaign_runs = _fold(
        "c1", [created(mode="sequential")], linked_run_ids={"r1"}, member_run_events=member_events
    )
    assert campaign_runs[0]["evalue"] is None
    assert campaign_runs[0]["evalue"] != 1.0


def test_evalue_keeps_imported_value_without_declaration():
    """ "a member without one keeps its stored evalue from campaign_run.
    imported if imported" -- mirrors `evalue = COALESCE(?, evalue)`."""
    member_events = {"r1": [run_started("r1", T1, "e1", sidecar=None)]}
    row, campaign_runs = _fold(
        "c1",
        [created(mode="sequential")],
        imported_member_events=[campaign_run_imported("c1", "r1", T0, "e_ci", evalue=0.42)],
        member_run_events=member_events,
    )
    assert campaign_runs[0]["evalue"] == 0.42


def test_bc7_evalue_uses_folded_outcome_not_raw():
    """BC-7: "campaign e-values ... use the member's folded (postmortem-
    overridden) outcome; the legacy campaign pass always sees the fragment's
    raw outcome." Build a run whose RAW outcome is 'fail' but whose
    postmortem overrides it to 'pass' -- pass_labels=('pass',) means a
    'fail'-outcome evalue would be (1-alt)/(1-null), while a 'pass'-outcome
    evalue is alt/null. These differ (9.0 vs ~0.111), so using the wrong
    outcome is a hard failure here, not a coincidental match."""
    pm = ev(
        "run.postmortem_applied",
        ["r1"],
        {
            "sha256": "s",
            "status": "validated",
            "verdict_override": "pass",
            "author": "a",
            "path": "p.toml",
            "hypothesis_status": "held",
            "has_anomalies": False,
            "summary": "",
            "asset_links": {},
        },
        T3,
        "e_pm",
    )
    member_events = {
        "r1": [
            run_started("r1", T1, "e1", sidecar=sidecar_decl(null_rate=0.1, alt_rate=0.9)),
            run_finished("r1", T2, "e1f", outcome="fail"),
            pm,
        ],
    }
    row, campaign_runs = _fold(
        "c1", [created(mode="sequential")], linked_run_ids={"r1"}, member_run_events=member_events
    )
    assert campaign_runs[0]["evalue"] == 9.0
    assert campaign_runs[0]["evalue"] != 1.0 / 9.0  # what the RAW 'fail' outcome would give


# --- threshold lock + mismatch ------------------------------------------------


def test_threshold_locks_on_first_non_neutral_member_with_declaration():
    member_events = {
        "r1": [
            run_started("r1", T1, "e1", sidecar=sidecar_decl(threshold=0.05)),
            run_finished("r1", T2, "e1f", outcome="pass"),
        ],
    }
    row, _ = _fold(
        "c1", [created(mode="sequential")], linked_run_ids={"r1"}, member_run_events=member_events
    )
    assert row["stopping_threshold"] == 0.05


def test_neutral_outcome_member_does_not_lock_threshold_but_still_gets_seq_position():
    member_events = {
        "r1": [
            run_started("r1", T1, "e1", sidecar=sidecar_decl(threshold=0.05)),
            run_finished("r1", T2, "e1f", outcome="unknown"),
        ],
        "r2": [
            run_started("r2", T3, "e2", sidecar=sidecar_decl(threshold=0.07)),
            run_finished("r2", T4, "e2f", outcome="pass"),
        ],
    }
    row, campaign_runs = _fold(
        "c1",
        [created(mode="sequential")],
        linked_run_ids={"r1", "r2"},
        member_run_events=member_events,
    )
    # r1 (neutral) never locks -- r2 (first non-neutral) does.
    assert row["stopping_threshold"] == 0.07
    by_run = {cr["run_id"]: cr for cr in campaign_runs}
    assert by_run["r1"]["seq_position"] == 1
    assert by_run["r2"]["seq_position"] == 2
    assert by_run["r1"]["evalue"] is not None  # still computed despite neutrality


def test_threshold_mismatch_resets_whole_campaign_to_none():
    """A second non-neutral member's sidecar disagrees with the first's
    locked threshold -- "the campaign is skipped as today": every member's
    evalue/seq_position resets to NULL, not just the mismatching one, and the
    stopping_threshold reverts to None. Negative control: a rule that kept
    the FIRST member's already-"planned" evalue/seq_position (rather than
    discarding the whole batch) would show a non-None value for r1."""
    member_events = {
        "r1": [
            run_started("r1", T1, "e1", sidecar=sidecar_decl(threshold=0.05)),
            run_finished("r1", T2, "e1f", outcome="pass"),
        ],
        "r2": [
            run_started("r2", T3, "e2", sidecar=sidecar_decl(threshold=0.10)),
            run_finished("r2", T4, "e2f", outcome="pass"),
        ],
    }
    row, campaign_runs = _fold(
        "c1",
        [created(mode="sequential")],
        linked_run_ids={"r1", "r2"},
        member_run_events=member_events,
    )
    assert row["stopping_threshold"] is None
    for cr in campaign_runs:
        assert cr["evalue"] is None
        assert cr["seq_position"] is None


# --- non-sequential mode ------------------------------------------------------


def test_non_sequential_mode_never_computes_evalue_or_threshold():
    member_events = {
        "r1": [
            run_started("r1", T1, "e1", sidecar=sidecar_decl(threshold=0.05)),
            run_finished("r1", T2, "e1f", outcome="pass"),
        ],
    }
    row, campaign_runs = _fold(
        "c1", [created(mode="exploration")], linked_run_ids={"r1"}, member_run_events=member_events
    )
    assert row["stopping_threshold"] is None
    assert campaign_runs[0]["evalue"] is None
    assert campaign_runs[0]["seq_position"] is None


# --- AC-20: arrival-order independence ---------------------------------------


def test_ac20_arrival_order_independence():
    member_events = {
        "r1": [
            run_started("r1", T1, "e1", sidecar=sidecar_decl(threshold=0.05)),
            run_finished("r1", T2, "e1f", outcome="pass"),
        ],
        "r2": [
            run_started("r2", T3, "e2", sidecar=sidecar_decl(threshold=0.05)),
            run_finished("r2", T4, "e2f", outcome="pass"),
        ],
    }
    campaign_events = [created(mode="sequential"), concluded()]
    run_added_events = [run_added("c1", "r1", T1, "e_ra1"), run_added("c1", "r2", T3, "e_ra2")]

    forward = _fold(
        "c1",
        campaign_events,
        run_added_events=run_added_events,
        member_run_events=member_events,
    )
    backward = _fold(
        "c1",
        list(reversed(campaign_events)),
        run_added_events=list(reversed(run_added_events)),
        member_run_events={k: list(reversed(v)) for k, v in member_events.items()},
    )
    assert forward == backward
    assert forward[0]["status"] == "concluded"
    assert forward[0]["stopping_threshold"] == 0.05
