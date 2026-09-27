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

from bathos.runlog.fold_merge import dedup_import_snapshots, stage1_field_merge

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
    # AC-17 finding: `tags` is a real `Run` field (`bth run --tag`) and a
    # real legacy warm column (`tags TEXT[]`, compact.py); an earlier
    # version of this fold had no column for it at all, so `runs.tags` was
    # silently NULL for every run after cut-over.
    "tags",
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
    # BC-1/BC-2 (spec "Import merge (stage 1)": "so a fragment never blanks
    # a field (e.g. metadata, output_metadata, postmortem fields) that a
    # warm import set"): these have no LIVE general-field write site (only
    # a dedicated event each -- run.outputs_hashed, run.postmortem_applied
    # -- handled below), but the legacy importer's run.imported DOES carry
    # them (they are ordinary warm-row/fragment columns), so they still need
    # the stage-1 import merge. Adding them here is a no-op for the stage-2
    # live loop (run.started/run.finished never set these keys), so it only
    # ever fills in a value the dedicated event mechanism would otherwise
    # leave at its hardcoded default.
    "output_metadata",
    "postmortem_status",
    "postmortem_override",
    "postmortem_verdict_override",
    "postmortem_author",
    "postmortem_path",
    "postmortem_hypothesis_status",
    "postmortem_has_anomalies",
    "postmortem_summary",
    "postmortem_asset_links",
)

# Reap-ledger record fields (spec "Reap ledgers" / "Authoritative writes":
# `run.reaped`'s `data` is exactly `reap.py:296-303`'s ledger record). Used
# to extract `metadata.reaped` from a ledger-shaped claim's own `data`,
# stripping the importer's bookkeeping keys (`source_class`, `source_locator`,
# `source_sha256`, `canon`, `snapshot`) and the `status` marker the importer
# adds to a `run.imported` ledger-sourced claim so `_expand_claims` can
# classify it -- neither belongs in the stored ledger record.
_REAP_LEDGER_FIELDS = ("run_id", "project_slug", "reaped_at", "reason", "window_h", "prior_status")

# `schema.Run()`'s own defaults for the GENERAL_FIELDS whose canonical value
# is an empty string or empty list, never `None` (AC-17 finding: a warm
# row/fragment genuinely holding this literal default -- e.g. a run that
# never set `hostname` -- is `is_empty()`-empty in EVERY import source, so
# `stage1_field_merge` correctly leaves the field unset per the spec's "first
# non-empty value" rule; but `compact.py`'s fresh-row INSERT always writes
# the literal default, never SQL NULL, for these columns, so leaving them
# absent here would diverge from the canonical legacy state for the common
# case of a run that never set them. `setdefault` below re-applies exactly
# `schema.Run()`'s own default, once, only for a field the merge left unset
# -- it never overwrites a real value (including a stage-2 live "" -- not
# reachable for these fields today, but this stays additive either way).
# Fields whose `schema.Run()` default is `None` (e.g. `stage_name`,
# `claim_discriminates`) are deliberately absent from this dict: `None` IS
# their canonical default, so leaving them unset is already correct.
_GENERAL_FIELD_DEFAULTS: dict[str, Any] = {
    "tags": [],
    "sidecar_sha256": "",
    "sidecar_path": "",
    "parent_run_id": "",
    "agent_mode": "",
    "sidecar_mode": "",
    "script_sha256": "",
    "slurm_job_id": "",
    "slurm_array_task_id": "",
    "hostname": "",
    "manifest_sha256": "",
    "manifest_path": "",
    "skill_sha256": "",
}

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
    abandon is active (this decides the STATUS bundle only -- see
    `_ledger_shaped_reaped_claim` for what `metadata.reaped` uses). If it is
    a revert, no claim survives.
    """
    rank1 = sorted((c for c in claims if c.rank == 1), key=lambda c: c.sort_key)
    if not rank1:
        return False, None
    last = rank1[-1]
    if last.is_revert:
        return False, None
    return True, last


def _is_ledger_shaped_claim(ev: dict) -> bool:
    """True for a rank-1 event whose `data` IS a reap-ledger record (spec
    "Reap ledgers"/"Authoritative writes"): a live `run.reaped`, or a
    `run.imported` sourced from the reap ledger JSON itself
    (`source_class="ledger_json"`).

    Distinguishes these from a `run.imported` sourced from a full warm-row
    or fragment snapshot whose OWN `status` column happens to be
    `"abandoned"` -- that event's `data` is the run's entire row (dozens of
    unrelated columns), not a ledger record, so it must never seed
    `metadata.reaped` (see `_ledger_shaped_reaped_claim`). Per the "Reap
    ledgers" import rule, the importer always emits the ledger file itself
    as its own dedicated `run.imported` event for the same entity whenever
    one exists, so this filter never silently drops real reap information --
    it only excludes a coincidental `status=="abandoned"` on an unrelated
    full-row import.
    """
    kind = ev.get("kind")
    if kind in ("run.reaped", "run.reap_reverted", "run_reap.imported"):
        return True
    if kind == "run.imported":
        return (ev.get("data") or {}).get("source_class") == "ledger_json"
    return False


def _ledger_shaped_reaped_claim(claims: list[_Claim]) -> _Claim | None:
    """Like `_abandoned_state`, but restricted to ledger-shaped rank-1
    events (claims and their cancelling reverts) -- spec: "`metadata.reaped`
    is the ledger record carried by the latest abandoned claim not
    cancelled by a revert". A full-row `run.imported` claim never counts
    here (see `_is_ledger_shaped_claim`), so it can neither seed nor cancel
    a ledger-derived `metadata.reaped`.
    """
    rank1 = sorted(
        (c for c in claims if c.rank == 1 and _is_ledger_shaped_claim(c.ev)),
        key=lambda c: c.sort_key,
    )
    if not rank1:
        return None
    last = rank1[-1]
    if last.is_revert:
        return None
    return last


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

    # Snapshot chains (spec "Snapshots"): drop every superseded import
    # snapshot before anything else sees this entity's events -- both the
    # status-claim ranking and the general-field merge below must only ever
    # see the newest snapshot of each concrete legacy source.
    events = dedup_import_snapshots(events)

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

    # General fields, two-stage (spec "Fold rules": "An entity folds in two
    # stages: (1) its `*.imported` events merge into a base state by the
    # import merge rule below; (2) its live events apply on top"):
    #
    # Stage 1: every `run.imported` event, merged by source-class precedence
    # then (ts, eid) -- "keeps the first non-empty value in order of source
    # precedence" (`stage1_field_merge`). A NEGATIVE CONTROL matters here: a
    # plain (ts, eid) merge across imports+live together (the pre-importer
    # code above this comment) would let a LOWER-precedence import (e.g. a
    # `fragment`) with a LATER timestamp silently overwrite a HIGHER-
    # precedence import (e.g. `warm`) with an earlier one -- precedence must
    # win regardless of which import happens to be newer.
    #
    # Stage 2: live `run.started`/`run.finished` events apply on top, in
    # (ts, eid) order, later wins -- unchanged from the pre-importer
    # behaviour, just now layered onto the stage-1 base instead of sorted in
    # among the imports.
    imported_general = [ev for ev in events if ev.get("kind") == "run.imported"]
    live_general = sorted(
        (ev for ev in events if ev.get("kind") in ("run.started", "run.finished")),
        key=lambda ev: (ev.get("ts", ""), ev.get("eid", "")),
    )
    row.update(stage1_field_merge(imported_general, GENERAL_FIELDS))
    for ev in live_general:
        data = ev.get("data") or {}
        for field in GENERAL_FIELDS:
            if field in data and data[field] is not None:
                row[field] = data[field]
    for field, default in _GENERAL_FIELD_DEFAULTS.items():
        row.setdefault(field, default)

    row["parity_run_type"] = _parity_run_type(claims)

    # output_metadata: latest run.outputs_hashed (clock-skew-sensitive field)
    # always wins when present; otherwise the stage-1 import merge above
    # already filled it from a warm/fragment source's own column, if any
    # (BC-1's "never blanks a field ... that a warm import set" applied to
    # a field with no live GENERAL write site).
    outputs_events = [ev for ev in events if ev.get("kind") == "run.outputs_hashed"]
    if outputs_events:
        latest_outputs = max(outputs_events, key=lambda ev: (ev.get("ts", ""), ev.get("eid", "")))
        row["output_metadata"] = (latest_outputs.get("data") or {}).get("output_metadata")
    else:
        row.setdefault("output_metadata", None)

    # Postmortem fields: latest run.postmortem_applied, BC-2's (ts, eid)-only
    # tie-break (source precedence does not apply here per the spec text),
    # always wins when present. Otherwise the stage-1 import merge above
    # already filled these from a warm/fragment row's own postmortem_*
    # columns (BC-2: "The importer imports what the walk found (postmortem
    # fields of warm rows)") -- `setdefault` below only supplies the
    # legacy `schema.Run()` dataclass's non-NULL defaults for a run with
    # NEITHER an import NOR a live postmortem event (AC-17 finding:
    # compact.py's fresh-row INSERT always writes these columns, whether or
    # not a postmortem was ever registered).
    row.setdefault("postmortem_status", "unassigned")
    row.setdefault("postmortem_override", "none")
    row.setdefault("postmortem_verdict_override", "none")
    row.setdefault("postmortem_author", "")
    row.setdefault("postmortem_path", "")
    row.setdefault("postmortem_hypothesis_status", "unassigned")
    row.setdefault("postmortem_has_anomalies", False)
    row.setdefault("postmortem_summary", "")
    row.setdefault("postmortem_asset_links", "{}")
    pm_events = [ev for ev in events if ev.get("kind") == "run.postmortem_applied"]
    # Seed from whatever stage 1 (or the setdefault above) already put in
    # `postmortem_verdict_override` -- an IMPORTED postmortem override (BC-2)
    # must feed the stored-outcome computation below just as a live one
    # does; a live `run.postmortem_applied` (if any) overwrites it again
    # further down.
    postmortem_verdict_override: str | None = row.get("postmortem_verdict_override")
    if postmortem_verdict_override == "none":
        postmortem_verdict_override = None
    if pm_events:
        latest_pm = max(pm_events, key=lambda ev: (ev.get("ts", ""), ev.get("eid", "")))
        pdata = latest_pm.get("data") or {}
        row["postmortem_status"] = pdata.get("status")
        # AC-17 finding: `postmortem_override` is a distinct legacy warm
        # column from `postmortem_verdict_override` (schema.py's Run has
        # both), and compact.py always sets them to the SAME value
        # (`run.postmortem_override = pm.verdict_override`, compact.py:1068)
        # -- there is no separate live-write-site event for it (the spec's
        # Authoritative-writes table only names `verdict_override`).
        row["postmortem_override"] = pdata.get("verdict_override")
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

    # metadata.reaped: the latest not-cancelled LEDGER-SHAPED abandoned claim
    # (`_ledger_shaped_reaped_claim`, distinct from `reaped_claim`/
    # `abandoned_active` above, which decide the STATUS bundle and also
    # count a full-row import whose own `status` column is "abandoned"),
    # independent of the winning status (AC-26: a reaped run that later
    # finishes keeps metadata.reaped; a reap then revert has none). Only the
    # known ledger-record fields are kept -- never the claim event's whole
    # `data` -- so neither the importer's bookkeeping keys (source_class,
    # source_locator, source_sha256, canon, snapshot) nor the `status`
    # marker a ledger_json-sourced `run.imported` carries leak into the
    # stored value.
    metadata: dict[str, Any] = {}
    ledger_claim = _ledger_shaped_reaped_claim(claims)
    if ledger_claim is not None:
        cdata = ledger_claim.ev.get("data") or {}
        metadata["reaped"] = {k: cdata[k] for k in _REAP_LEDGER_FIELDS if k in cdata}
    row["metadata"] = metadata

    # Campaign-derived fields -- STUBBED, wave b (the campaign fold).
    row["seq_position"] = None
    row["evalue"] = None

    # AC-17 finding: `compact.py`'s fresh-row INSERT normalizes a falsy
    # `outcome_error_reason`/`outcome` to SQL NULL for these two columns
    # specifically (`run.outcome_error_reason or None`, `run.outcome or
    # None # preserve evaluated outcome label from cool fragment`) -- most
    # other TEXT columns are inserted as-is. Matching that quirk
    # bit-for-bit here (the `_STATUS_DEFAULTS`/bundle machinery otherwise
    # gives `""`) rather than leaving it a permanent, spurious "" vs NULL
    # divergence for every run with no outcome yet (e.g. `running`,
    # `abandoned`).
    row["outcome_error_reason"] = row.get("outcome_error_reason") or None
    row["outcome"] = row.get("outcome") or None

    return row


__all__ = ["STATUS_BUNDLE_FIELDS", "TERMINAL_STATUSES", "fold_run"]
