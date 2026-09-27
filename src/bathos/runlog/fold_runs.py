"""The run fold (spec "Fold rules", AC-1/AC-15/AC-20/AC-26).

Folds every event for a single run entity (``entity == [run_id]``) into one
dict shaped like the legacy warm ``runs`` row. Pure function of its event
list: no filesystem, cwd, or database access, so it can be called identically
by ingest (persisting into ``idx.runs``) and by the read path (re-folding an
unindexed delta) -- "One fold function ... is used by both ingest and the
read path, and it always re-folds an affected entity from its complete event
history ... never by applying new events on top of the stored row."

Scope (delivery step 3, wave a): the RUN fold only. Campaign-derived columns
(`seq_position`, `evalue`) are stubbed to ``None`` here -- they belong to the
campaign fold ("Campaign-derived values" in the spec, `bathos.runlog.
fold_campaigns`) and require campaign membership/threshold data this module
never sees. Wave b fills them in at the ingest layer instead of here
(`bathos.runlog.ingest._refold_run` consults the run's own folded
`campaign_id`'s campaign fold after calling `fold_run`), keeping this module
a pure, single-entity fold exactly as documented below.

Only the event kinds the current write sites (delivery step 2b) actually
emit are exercised in production: `run.started`, `run.finished`,
`run.reaped`, `run.reap_reverted`, `run.outputs_hashed`,
`run.postmortem_applied`. The import kinds `run.imported` /
`run_reap.imported` are also handled here (the "Run status" fold rule
requires ranking across "both stages", i.e. the legacy importer's future
output must already fold correctly) even though the importer itself
(delivery step 4) is not built in this wave -- AC-26 fixtures construct these
events directly.
"""

from __future__ import annotations

from typing import Any

TERMINAL_STATUSES = frozenset({"completed", "failed", "killed"})

# The status-dependent bundle (Fold rules, "Run status"): fields the runner
# sets at finish, which come together from a single winning status claim
# rather than being merged field-by-field. `differential_effect` is included
# alongside its sibling `differential_*` fields for symmetry -- the spec's
# enumerated list omits it, but it is set by the exact same
# `dataclasses.replace()` call as `differential_status` in runner.py, so
# splitting it out into the general-merge path would let it disagree with
# its own status claim. Noted as a spec-ambiguity call, not a redesign.
STATUS_BUNDLE_FIELDS = (
    "status",
    "exit_code",
    "duration_s",
    "output_paths",
    "outcome",
    "outcome_error_reason",
    "outcome_is_residual",
    "adversarial_check_status",
    "adversarial_check_result",
    "differential_status",
    "differential_off_value",
    "differential_on_value",
    "differential_effect",
)

# schema.Run's own field defaults, for a bundle with no winning claim data
# (or whose winning claim's data is missing a field -- e.g. an abandoned
# claim's ledger record carries no exit_code/duration_s/outcome at all,
# matching legacy reap.py, which flips only `run.status` in place).
_STATUS_DEFAULTS: dict[str, Any] = {
    "status": "running",
    "exit_code": -1,
    "duration_s": 0.0,
    "output_paths": [],
    "outcome": "",
    "outcome_error_reason": "",
    "outcome_is_residual": False,
    "adversarial_check_status": "",
    "adversarial_check_result": None,
    "differential_status": None,
    "differential_off_value": None,
    "differential_on_value": None,
    "differential_effect": None,
}

# General (non-status-bundle) fields carried by run.started / run.finished /
# run.imported. "apply on top in (ts, eid) order" (stage 2): later events'
# present keys win over earlier ones. A key a given event's `data` never sets
# is left untouched by that event (never blanked).
GENERAL_FIELDS = (
    "project_slug",
    "command",
    "argv",
    "git_hash",
    "git_branch",
    "git_dirty",
    "timestamp",
    "sidecar_sha256",
    "sidecar_path",
    "parent_run_id",
    "agent_mode",
    "sidecar_mode",
    "campaign_id",
    "script_sha256",
    "stage_name",
    "claim_discriminates",
    "claim_isolates",
    "slurm_job_id",
    "slurm_array_task_id",
    "hostname",
    "git_dirty_content_id",
    "git_provenance_source",
    "dependency_lock_sha256",
    "component_id",
    "component_sidecar_sha256",
    "seed",
    "baseline_hpo_trials",
    "baseline_hpo_compute_budget",
    "stdout_sha256",
    "manifest_sha256",
    "manifest_path",
    "skill_sha256",
    "schema_version",
)

_ORIGIN_PRECEDENCE = {"live": 1, "migration": 0}


def _origin_rank(ev: dict) -> int:
    return _ORIGIN_PRECEDENCE.get(ev.get("origin", "live"), 0)


def _tie_key(ev: dict) -> tuple[int, str, str]:
    """(source precedence, ts, eid): "live counts as highest", then later
    ts, then eid -- the generic tie-break for equal-rank status claims and
    for `parity_run_type`."""
    return (_origin_rank(ev), ev.get("ts", ""), ev.get("eid", ""))


class _Claim:
    """One status claim (`run.started`/`.finished`/`.reaped`/`.imported`) or
    one revert (`run.reap_reverted`, or the revert half of a
    `run_reap.imported`), with the (ts, is_revert, eid) sort key the "Run
    status" rule's revert-ordering uses."""

    __slots__ = ("rank", "is_revert", "ev", "ts", "eid", "sort_key", "tie_key")

    def __init__(self, rank: int, is_revert: bool, ev: dict, ts: str, eid: str):
        self.rank = rank
        self.is_revert = is_revert
        self.ev = ev
        self.ts = ts
        self.eid = eid
        self.sort_key = (ts, 1 if is_revert else 0, eid)
        self.tie_key = _tie_key(ev)


def _expand_claims(events: list[dict]) -> list[_Claim]:
    """One raw event -> one or two `_Claim`s.

    `run_reap.imported` expands to a claim at `data.reaped_at` plus a revert
    at `data.reverted_at` (spec "Reap ledgers": "the fold treats it as an
    abandoned claim at reaped_at followed by a revert at reverted_at, the
    revert always after its own claim").
    """
    claims: list[_Claim] = []
    for ev in events:
        kind = ev.get("kind")
        data = ev.get("data") or {}
        ts = ev.get("ts", "")
        eid = ev.get("eid", "")
        if kind == "run.started":
            claims.append(_Claim(0, False, ev, ts, eid))
        elif kind == "run.finished":
            claims.append(_Claim(2, False, ev, ts, eid))
        elif kind == "run.reaped":
            claims.append(_Claim(1, False, ev, ts, eid))
        elif kind == "run.reap_reverted":
            claims.append(_Claim(1, True, ev, ts, eid))
        elif kind == "run.imported":
            status = data.get("status")
            if status in TERMINAL_STATUSES:
                claims.append(_Claim(2, False, ev, ts, eid))
            elif status == "abandoned":
                claims.append(_Claim(1, False, ev, ts, eid))
            else:
                # "running" (or an absent status, which today's importer
                # never emits, but is treated the same defensively).
                claims.append(_Claim(0, False, ev, ts, eid))
        elif kind == "run_reap.imported":
            reaped_at = data.get("reaped_at", ts)
            reverted_at = data.get("reverted_at", ts)
            claims.append(_Claim(1, False, ev, reaped_at, eid + ":claim"))
            claims.append(_Claim(1, True, ev, reverted_at, eid + ":revert"))
        # Every other kind (run.outputs_hashed, run.postmortem_applied, ...)
        # is not a status claim.
    return claims


def _winning_terminal_claim(claims: list[_Claim]) -> _Claim | None:
    """ "a real finish beats a reap whatever their ts": if any rank-2 claim
    exists, it always wins over rank 0/1, whatever the timing. Among several
    rank-2 claims, the tie-break (source precedence, then ts, then eid)
    picks which one's bundle wins."""
    terminal = [c for c in claims if c.rank == 2]
    if not terminal:
        return None
    return max(terminal, key=lambda c: c.tie_key)


def _abandoned_state(claims: list[_Claim]) -> tuple[bool, _Claim | None]:
    """(is_currently_abandoned, latest_not_cancelled_claim).

    Rank-1 claims and reverts sorted by (ts, is_revert, eid) -- a revert
    always sorts after every claim at the same ts, so "a revert cancels
    every earlier or same-ts abandoned claim" is exactly: the state after
    the LAST rank-1 event in this order. If that last event is a claim, an
    abandon is active and that claim is the one `metadata.reaped` uses (it
    cannot have been cancelled -- nothing sorts after it). If it is a
    revert, no claim survives.
    """
    rank1 = sorted((c for c in claims if c.rank == 1), key=lambda c: c.sort_key)
    if not rank1:
        return False, None
    last = rank1[-1]
    if last.is_revert:
        return False, None
    return True, last


def _running_claim(claims: list[_Claim]) -> _Claim | None:
    rank0 = [c for c in claims if c.rank == 0]
    if not rank0:
        return None
    return max(rank0, key=lambda c: c.tie_key)


def _parity_run_type(claims: list[_Claim]) -> Any:
    """BC-4: the first non-null `parity_run_type` over ALL status claims (not
    just the winning one), taken in rank-desc then tie-desc order -- "a
    winning claim with null never blanks it"."""
    by_tie = sorted(claims, key=lambda c: c.tie_key, reverse=True)
    ordered = sorted(by_tie, key=lambda c: c.rank, reverse=True)  # stable: preserves tie order
    for c in ordered:
        val = (c.ev.get("data") or {}).get("parity_run_type")
        if val is not None:
            return val
    return None


def _entity_id(events: list[dict]) -> str | None:
    for ev in events:
        entity = ev.get("entity") or []
        if entity:
            return entity[0]
    return None


def fold_run(events: list[dict]) -> dict[str, Any]:
    """Fold every event of one run entity into a legacy-`runs`-shaped dict.

    `events` need not be pre-sorted; every ordering decision is made here
    from each event's own `(ts, eid)` (or, for status claims, `(ts,
    is_revert, eid)`), so re-folding the same multiset in any order or any
    batching yields the same row (AC-1, AC-20).
    """
    row: dict[str, Any] = {"id": _entity_id(events)}
    if not events:
        return row

    claims = _expand_claims(events)

    terminal = _winning_terminal_claim(claims)
    abandoned_active, reaped_claim = _abandoned_state(claims)
    if terminal is not None:
        winning, winning_kind = terminal, "terminal"
    elif abandoned_active:
        winning, winning_kind = reaped_claim, "abandoned"
    else:
        winning, winning_kind = _running_claim(claims), "running"

    bundle = dict(_STATUS_DEFAULTS)
    if winning is not None:
        data = winning.ev.get("data") or {}
        for field in STATUS_BUNDLE_FIELDS:
            if data.get(field) is not None:
                bundle[field] = data[field]
    if winning_kind == "abandoned":
        bundle["status"] = "abandoned"
    elif winning_kind == "running":
        bundle["status"] = "running"
    elif winning_kind == "terminal" and bundle.get("status") not in TERMINAL_STATUSES:
        # Defensive: a run.finished/run.imported terminal claim should always
        # carry its own `status`; fall back rather than leave a rank-0/1
        # default in place of a genuine terminal winner.
        bundle["status"] = "completed"
    row.update(bundle)

    # General fields: "apply on top" in (ts, eid) order.
    general_sources = sorted(
        (ev for ev in events if ev.get("kind") in ("run.started", "run.finished", "run.imported")),
        key=lambda ev: (ev.get("ts", ""), ev.get("eid", "")),
    )
    for ev in general_sources:
        data = ev.get("data") or {}
        for field in GENERAL_FIELDS:
            if field in data and data[field] is not None:
                row[field] = data[field]

    row["parity_run_type"] = _parity_run_type(claims)

    # output_metadata: latest run.outputs_hashed (clock-skew-sensitive field).
    outputs_events = [ev for ev in events if ev.get("kind") == "run.outputs_hashed"]
    if outputs_events:
        latest_outputs = max(outputs_events, key=lambda ev: (ev.get("ts", ""), ev.get("eid", "")))
        row["output_metadata"] = (latest_outputs.get("data") or {}).get("output_metadata")
    else:
        row["output_metadata"] = None

    # Postmortem fields: latest run.postmortem_applied, BC-2's (ts, eid)-only
    # tie-break (source precedence does not apply here per the spec text).
    pm_events = [ev for ev in events if ev.get("kind") == "run.postmortem_applied"]
    postmortem_verdict_override: str | None = None
    if pm_events:
        latest_pm = max(pm_events, key=lambda ev: (ev.get("ts", ""), ev.get("eid", "")))
        pdata = latest_pm.get("data") or {}
        row["postmortem_status"] = pdata.get("status")
        row["postmortem_verdict_override"] = pdata.get("verdict_override")
        row["postmortem_author"] = pdata.get("author")
        row["postmortem_path"] = pdata.get("path")
        row["postmortem_hypothesis_status"] = pdata.get("hypothesis_status")
        row["postmortem_has_anomalies"] = pdata.get("has_anomalies")
        row["postmortem_summary"] = pdata.get("summary")
        row["postmortem_asset_links"] = pdata.get("asset_links")
        postmortem_verdict_override = pdata.get("verdict_override")

    # The stored (non-sticky) outcome: the latest postmortem override if not
    # "none", else the winning claim's raw outcome.
    raw_outcome = bundle.get("outcome", "")
    if postmortem_verdict_override and postmortem_verdict_override != "none":
        row["outcome"] = postmortem_verdict_override
    else:
        row["outcome"] = raw_outcome

    # metadata.reaped: the latest not-cancelled abandoned claim, independent
    # of the winning status (AC-26: a reaped run that later finishes keeps
    # metadata.reaped; a reap then revert has none).
    metadata: dict[str, Any] = {}
    if reaped_claim is not None:
        metadata["reaped"] = reaped_claim.ev.get("data")
    row["metadata"] = metadata

    # Campaign-derived fields -- STUBBED, wave b (the campaign fold).
    row["seq_position"] = None
    row["evalue"] = None

    return row


__all__ = ["STATUS_BUNDLE_FIELDS", "TERMINAL_STATUSES", "fold_run"]
