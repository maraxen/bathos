"""The campaign fold (spec "Fold rules" / "Campaign-derived values", AC-17's
BC-6/BC-7/BC-9, AC-22, AC-26's postmortem/campaign interaction).

Folds one campaign entity's events, plus its members' full run histories,
into a `(campaigns row, [campaign_runs rows])` pair shaped like the legacy
warm tables `link_cool_runs_to_campaigns` (`campaigns.py:414-521`) produces on
a full rebuild -- the canonical reference state "Fold rules" names for AC-17.

Scope (delivery step 3, wave b): the inputs this module is handed are already
gathered by the caller (`bathos.runlog.ingest._compute_campaign_fold`) --
this module is a pure function of those events, no database or filesystem
access, mirroring `fold_runs.fold_run`'s own purity so it can be called
identically by ingest and (later) the read path.

Spec-ambiguity call, noted rather than resolved by redesign: the spec lists
`stopping_threshold` as clock-skew-sensitive ("every field set by ...
`campaign.threshold_set`"), which would suggest folding it as an ordinary
general field. But the very same "Fold rules" section also states the
`stopping_threshold` DERIVED COLUMN gets "no event: derived by the campaign
fold" (Authoritative-writes table), and "the threshold lock is computed as
today" (`campaigns.py:414-521`), which always re-derives it from the current
membership's sidecars in start-time order, never by reading back a
previously-stored value. Re-deriving fresh from the member walk on every fold
call -- rather than seeding it from the latest `campaign.threshold_set`
event -- is also the only reading consistent with the architecture's central
invariant (AC-1/AC-20): the fold is a pure, arrival-order-independent
function of the *complete* event multiset, never "apply new events on top of
a stored value". `campaign.threshold_set` events are therefore folded for
provenance/audit only (they exist so AC-25's "every write site has an event"
holds) and play no role in this module's own `stopping_threshold` output.
"""

from __future__ import annotations

from typing import Any

from bathos.runlog.fold_runs import fold_run

NEUTRAL_OUTCOMES = frozenset({"error", "unknown", None, ""})

# Direct campaign-entity kinds folded as ordinary general fields (spec
# "Clock-skew-sensitive results": "every field set by campaign.updated,
# campaign.threshold_set, campaign.claim_bound, campaign.claim_bypassed").
# `campaign.threshold_set` is deliberately EXCLUDED -- see module docstring.
_GENERAL_CAMPAIGN_KINDS = frozenset(
    {
        "campaign.created",
        "campaign.claim_bound",
        "campaign.claim_bypassed",
        "campaign.concluded",
    }
)


def _tie_key(ev: dict) -> tuple[str, str]:
    return (ev.get("ts", ""), ev.get("eid", ""))


def _general_merge(campaign_events: list[dict]) -> dict[str, Any]:
    """ "apply on top" in (ts, eid) order -- later events' present (non-None)
    keys win over earlier ones; a key an event never sets is left untouched
    (never blanked). `campaign.concluded`'s own data has no `status` key, so
    `status` is forced to `"concluded"` afterward whenever any such event
    exists (there is no "un-conclude" event, so this is trivially sticky)."""
    row: dict[str, Any] = {}
    ordered = sorted(
        (e for e in campaign_events if e.get("kind") in _GENERAL_CAMPAIGN_KINDS),
        key=_tie_key,
    )
    for ev in ordered:
        data = ev.get("data") or {}
        for key, value in data.items():
            if value is not None:
                row[key] = value
    if any(e.get("kind") == "campaign.concluded" for e in campaign_events):
        row["status"] = "concluded"
    return row


def _sidecar_from_declaration(decl: dict | None):
    """Reconstruct just enough of a `bathos.sidecar.Sidecar` from the
    JSON-safe dict `run.started.data["sidecar"]` carries (spec BC-3: "A run's
    e-value uses the sidecar declaration frozen in run.started.data") to call
    `compute_evalue`/`derive_pass_labels` against it. `None`/`{}` (no sidecar
    at run time, `sidecar_declaration_for_event(None)` in the emitter) means
    "no declaration" -- returns `None`.
    """
    if not decl:
        return None
    from bathos.sidecar import OutcomeSpec, Sidecar

    outcomes_raw = decl.get("outcomes") or {}
    outcomes = {}
    for label, spec in outcomes_raw.items():
        spec = spec or {}
        outcomes[label] = OutcomeSpec(
            condition=spec.get("condition", ""),
            decision=spec.get("decision", ""),
            reasoning=spec.get("reasoning", ""),
            is_residual=bool(spec.get("is_residual", False)),
            adversarial_check=spec.get("adversarial_check"),
            source=spec.get("source", ""),
            multiple_comparisons_correction=spec.get("multiple_comparisons_correction", ""),
        )
    return Sidecar(
        kind=decl.get("kind", "experiment"),
        result_schema=decl.get("result_schema") or {},
        outcomes=outcomes,
        popper_null_pass_rate=decl.get("popper_null_pass_rate"),
        popper_alt_pass_rate=decl.get("popper_alt_pass_rate"),
        popper_stopping_threshold=decl.get("popper_stopping_threshold"),
        popper_weights=decl.get("popper_weights") or {},
    )


def member_sidecar_declaration(run_events: list[dict]) -> dict | None:
    """The sidecar declaration a member run's OWN `run.started`/`run.imported`
    event(s) carry (BC-3: frozen at run start, never re-read from a live
    sidecar file). Ties among several such events (should not occur in
    practice -- one run has one start) resolve the same way `fold_run`'s
    GENERAL_FIELDS do: `(ts, eid)` order, later wins."""
    sources = sorted(
        (e for e in run_events if e.get("kind") in ("run.started", "run.imported")),
        key=_tie_key,
    )
    decl: dict | None = None
    for ev in sources:
        candidate = (ev.get("data") or {}).get("sidecar")
        if candidate:
            decl = candidate
    return decl


def resolve_run_campaign_id(
    run_events: list[dict], run_added_events_for_run: list[dict]
) -> str:
    """The single campaign a run currently belongs to, for the denormalized
    `runs.campaign_id` column (review finding, HIGH -- spec BC-6, lines
    374-379: "`add_run_to_campaign` overwrites the fragment's single
    `campaign_id` ... so the canonical state keeps only B").

    The LATEST assignment by `(ts, eid)` wins, taken across BOTH:
    - the run's own `run.started`/`run.imported` event(s)' `data.campaign_id`
      (its original assignment, at that event's own `(ts, eid)`), and
    - every `campaign.run_added` event naming this run (`entity[0]` is the
      newly-assigned campaign, at THAT event's own `(ts, eid)`).

    This does NOT decide `campaign_runs` membership -- that stays the union
    of the same sources (computed in `fold_campaign`, BC-6): a run started in
    A and later added to B is still a member of both, but `runs.campaign_id`
    (and, downstream, `runs.seq_position`/`runs.evalue`) follows only the
    latest assignment, matching the canonical legacy state's single
    `campaign_id` column.
    """
    candidates: list[tuple[str, str, str]] = []
    for ev in run_events:
        if ev.get("kind") not in ("run.started", "run.imported"):
            continue
        cid = (ev.get("data") or {}).get("campaign_id")
        if cid:
            candidates.append((ev.get("ts", ""), ev.get("eid", ""), cid))
    for ev in run_added_events_for_run:
        entity = ev.get("entity") or []
        if len(entity) >= 2:
            candidates.append((ev.get("ts", ""), ev.get("eid", ""), entity[0]))
    if not candidates:
        # AC-17 finding: the legacy `schema.Run.campaign_id` default is `""`
        # (never `NULL`) -- `compact.py`'s fresh-row INSERT always writes
        # `run.campaign_id` verbatim, so a run with no campaign assignment
        # gets the empty string, not NULL, in the canonical legacy state.
        # Returning `None` here (an earlier version of this function did)
        # left `runs.campaign_id` NULL for every unaffiliated run, diverging
        # from that default representation.
        return ""
    return max(candidates, key=lambda c: (c[0], c[1]))[2]


def _run_sort_key(folded_run: dict) -> tuple[str, str]:
    """ "members are sorted by the folded run start time `runs.timestamp`
    (null sorts first, as `datetime.min` UTC), then `run_id`" -- RFC3339
    strings compare lexically in chronological order, so the empty string
    sorts before every real timestamp, matching `datetime.min`."""
    ts = folded_run.get("timestamp") or ""
    return (ts, folded_run.get("id") or "")


def fold_campaign(
    campaign_id: str,
    campaign_events: list[dict],
    run_added_events: list[dict],
    imported_member_events: list[dict],
    linked_run_ids: set[str],
    member_run_events: dict[str, list[dict]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fold one campaign entity into `(campaigns row, [campaign_runs rows])`.

    Args:
        campaign_id: the entity key.
        campaign_events: every event with `entity == [campaign_id]`.
        run_added_events: every `campaign.run_added` event with
            `entity[0] == campaign_id` (any `run_id`).
        imported_member_events: every `campaign_run.imported` event with
            `entity[0] == campaign_id`.
        linked_run_ids: run ids whose OWN `run.started`/`run.imported` event
            carries `data.campaign_id == campaign_id` (BC-6's third
            membership source).
        member_run_events: `{run_id: [every event for that run entity]}`,
            covering every run id this campaign's membership touches (from
            `run_added_events`, `imported_member_events`, and
            `linked_run_ids`).
    """
    row = _general_merge(campaign_events)
    row["id"] = campaign_id

    member_ids: set[str] = set(linked_run_ids)
    for ev in run_added_events:
        entity = ev.get("entity") or []
        if len(entity) >= 2:
            member_ids.add(entity[1])
    for ev in imported_member_events:
        entity = ev.get("entity") or []
        if len(entity) >= 2:
            member_ids.add(entity[1])

    # Imported evalue per run_id (spec: "an imported member keeps its stored
    # evalue"; latest by (ts, eid) if more than one such import exists).
    imported_by_run: dict[str, list[dict]] = {}
    for ev in imported_member_events:
        entity = ev.get("entity") or []
        if len(entity) >= 2:
            imported_by_run.setdefault(entity[1], []).append(ev)
    imported_evalue: dict[str, float | None] = {
        run_id: (max(evs, key=_tie_key).get("data") or {}).get("evalue")
        for run_id, evs in imported_by_run.items()
    }

    folded_members: dict[str, dict] = {}
    for run_id in member_ids:
        revents = member_run_events.get(run_id, [])
        folded_members[run_id] = fold_run(revents) if revents else {"id": run_id}

    ordered = sorted(member_ids, key=lambda rid: _run_sort_key(folded_members[rid]))

    mode = row.get("mode")
    if mode != "sequential":
        # "if not mode_row or mode_row[0] != 'sequential': continue" --
        # non-sequential campaigns get no evalue/seq_position walk at all,
        # and (review finding, LOW) `stopping_threshold` is left exactly as
        # the general merge produced it -- legacy `continue`s past the whole
        # walk without ever touching the column, so a non-sequential
        # campaign's stopping_threshold (however it got a value -- e.g. a
        # recovery-insert of a cool JSON snapshot) is never reset to NULL
        # here.
        campaign_runs_rows = [
            {"campaign_id": campaign_id, "run_id": rid, "evalue": None, "seq_position": None}
            for rid in ordered
        ]
        return row, campaign_runs_rows

    from bathos.sidecar import compute_evalue

    pending_threshold: float | None = None
    mismatch = False
    planned: dict[str, dict[str, Any]] = {}
    for i, run_id in enumerate(ordered, start=1):
        folded = folded_members[run_id]
        outcome = folded.get("outcome")
        decl = member_sidecar_declaration(member_run_events.get(run_id, []))
        sidecar_obj = _sidecar_from_declaration(decl)
        if sidecar_obj is not None:
            evalue = compute_evalue(sidecar_obj, outcome or "unknown")
            threshold_candidate = sidecar_obj.popper_stopping_threshold
        else:
            evalue = imported_evalue.get(run_id)
            threshold_candidate = None

        is_neutral = outcome in NEUTRAL_OUTCOMES
        if not is_neutral and threshold_candidate is not None:
            if pending_threshold is None:
                pending_threshold = threshold_candidate
            elif threshold_candidate != pending_threshold:
                mismatch = True
                break

        planned[run_id] = {"evalue": evalue, "seq_position": i}

    if mismatch:
        # "the campaign is skipped as today" -- the whole planned batch
        # (including entries computed before the mismatch was hit) is
        # discarded, matching link_cool_runs_to_campaigns's own
        # `continue` before its UPDATE loop.
        row["stopping_threshold"] = None
        campaign_runs_rows = [
            {"campaign_id": campaign_id, "run_id": rid, "evalue": None, "seq_position": None}
            for rid in ordered
        ]
        return row, campaign_runs_rows

    row["stopping_threshold"] = pending_threshold
    campaign_runs_rows = [
        {
            "campaign_id": campaign_id,
            "run_id": rid,
            "evalue": planned.get(rid, {}).get("evalue"),
            "seq_position": planned.get(rid, {}).get("seq_position"),
        }
        for rid in ordered
    ]
    return row, campaign_runs_rows


__all__ = ["fold_campaign", "member_sidecar_declaration", "resolve_run_campaign_id"]
