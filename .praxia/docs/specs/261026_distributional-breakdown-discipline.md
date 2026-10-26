---
title: Distributional breakdown, amendments and fork records for aggregate verdicts
description: 'Spec for debt #3045: [breakdown] sidecar block + aggregate-verdict lint, postmortem [[amendments]], postmortem [fork] for marginal results; warn everywhere, refuse when claim-bound.'
status: draft
task_id: 261026_distributional-breakdown-debt
date: '261026'
backlog_ids: ''
adversarial_review: 'cycles 1-2 challenger+defender done; v3 revision'
---
# Distributional breakdown, amendments and fork records for aggregate verdicts

**v3** (after adversarial cycles 1 and 2; see §6). Debt #3045. Source postmortem: lathex
`postmortems/bathos-debt-distributional-breakdown.md` (commit a0b1f5a). Companion bug #3046
(scaffold path) is fixed separately (commit 1ed684bb).

## 0. Problem

lathex bth `78db9e04` (`rf3_dtype_probe_compare.bth.toml`) was an **exploration-stage** run
(`[observe]`, labels `x64_floor_*`, conditions such as `e_max_placement <= 0.5`), with no
`[breakdown]` and no marginal label. It recorded `outcome = observed` and
`tentative_outcome = x64_floor_still_exceeds_tolerance`. The verdict rested on one number,
`e_max_placement` = 0.521 Å against 0.5 Å. "Marginal" was the human reading. A per-binder
breakdown afterwards (bth `797b791e`) showed the maximum came from 1 binder of 24 (median
binder maximum 0.004 Å). The deciding statistic was `n_binders_over`: 1-2 versus 3 or more.

Gaps: (1) nothing prompts a per-unit breakdown before a run whose verdict rests on a worst-case
aggregate; (2) no first-class way to attach later evidence naming the outlier unit; (3) no
record of what was decided after a near-bound result, or why.

User decision (261026): **WARNING everywhere; ERROR only where the run is cited (entry into a
claim-bound campaign).** Internal validation runs stay unblocked.

**Acceptance walk-through (78db9e04 under v3).** Before the run, `bth run` prints to stderr
that `e_max_placement` and `e_max_iptm` gate outcomes with no `[breakdown]` (D2). Had the
author declared `[breakdown]` with `bound = 0.5`, the run would have recorded
`e_max_placement__n_over = 1`, matched the parameter-free `single_unit` near-bound trigger
(D6), printed an advisory, and opened a `near_bound` obligation. That obligation stays open
until a format-2 postmortem with a `[fork]` discharges it. The run is never blocked. If the
author ignores the D2 warning and declares nothing, D6 cannot fire. That residual gap is
accepted, since the warning is the intervention point.

## 1. Assumptions (re-verified in cycles 1-2)

| # | Assumption | Tag | Evidence |
|---|---|---|---|
| A1 | Outcome conditions are DuckDB SQL over result columns; `OutcomeSpec` has no `is_marginal` | verified | `sidecar.py:32-44` |
| A2 | `marginal` is a bookkeeping label (undeclared-allowed, e-value 1.0) | verified | `validate.py:183`, `sidecar.py:1078` |
| A3 | `validate_sidecar` warnings logged at `prereg.py:531` reach **only** `events.*.jsonl`: the root logger has just the telemetry queue handler, so `lastResort` never prints. The gate is skipped for non-enforced dirs | verified FALSE as a surfacing channel | `telemetry.py:282-285`, `runner.py:742,957`, `prereg.py:503` |
| A4 | Claim-bound predicates differ by site: add/register use `claim_path IS NOT NULL`; the conclude Union Gate uses `claim_path and registered_sha` | verified | `claim.py:716`, `campaigns.py:1203` |
| A5 | `bth run --campaign` resolves the campaign at ≈795 and closes `campaign_db` at ≈806, **before** the sidecar parse at ≈857-861 | verified | `runner.py:795-861` |
| A6 | `_add_run_to_campaign_impl` selects only `(mode, started_at, stopping_threshold)` and `(timestamp, outcome, sidecar_path)`; `threshold_set` is emitted at ≈591 | verified | `campaigns.py:495-513,591` |
| A7 | `link_cool_runs_to_campaigns` is compaction ingest and must not raise | verified | `campaigns.py:652-676` |
| A8 | Log mode `run.started` stores `asdict(sidecar)`; its only reader, `fold_campaigns._sidecar_from_declaration`, rebuilds 7 fields (lossy) | verified | `runner.py:1117`, `runlog/emit.py:285`, `fold_campaigns.py:127-160` |
| A9 | `git show refs/bathos/runs/<id>:<relpath>` recovers a committed sidecar | verified for the ref; **unverified** for an untracked sidecar in a WIP snapshot (T0) |
| A10 | `[result_schema]` completeness is not checked on exit 0 | verified | `runner.py:316-330,1429,1452,1481-1483` |
| A11 | `validate_postmortem` errors on `git_dirty`/HEAD drift whenever `run` is passed; the main callers pass none | verified | `postmortem.py:270-288`, `mcp.py:3179`, `obligations.py:277` |
| A12 | `ValidationResult` has no warnings channel | verified | `postmortem.py:20-22` |
| A13 | `EXCEPTION_TO_CODE` is class-name keyed with one value; `campaign_add_tool` returns no `error_code`; `register_claim` raises `RuntimeError` | verified | `errors.py:105-119`, `mcp.py:4355-4395,3550`, `claim.py:645,706` |
| A14 | Each `def check_*` in `linter.py` needs a rule card; `build_context` already holds the parsed sidecar | verified | `tests/test_corpus.py:226,241`, `corpus.py:314` |
| A15 | `linter._columns_referenced` does word-boundary extraction | verified | `linter.py:761-768` |
| A16 | Obligations ledger: per-trigger flags default OFF, opened idempotently, discharged by a valid postmortem's `discharges`, listed at conclude; `TRIGGERS` is a closed set asserted by tests | verified | `obligations.py:13-64,84-123,181-200,277` |
| A17 | Run results are parsed from `runs.metadata` (JSON string) | verified | `runner._parse_result_dict:387`, `schema.py:195` |

## 2. Design

### D1. `[breakdown]` sidecar block

```toml
[breakdown]
unit = "binder"
artifact = "per_unit.parquet"           # optional, relative to BTH_OUTPUT_DIR

[breakdown.aggregates.e_max_placement]
direction = "max"                       # "max" | "min"
bound = 0.5                             # optional judgement bound
margin = 0.05                           # optional absolute near-bound band (D6); see default below

# waive = "reason"                      # top-level alternative (D3: only the run-time sidecar counts)
```

Aggregate keys may end in `*` (`e_max_placement_*`). The prefix expands over **float**
`[result_schema]` keys; an unmatched prefix is a validation error. Each expanded column is an
aggregate with the same settings.

For each aggregate `A` the run emits (declared in `[result_schema]`):

| Column | Type | Meaning |
|---|---|---|
| `A__worst_unit` | str | unit at the worst value (argmax for `max`, argmin for `min`) |
| `A__second` | float | the second-worst unit's value |
| `A__median` | float | median over units of the per-unit aggregate |
| `A__n_units` | int | number of units |
| `A__n_over` | int | units **strictly** beyond `bound` in the worst direction (required iff `bound` is set) |

`validate_sidecar` errors: no `aggregates` and no `waive`; empty `unit` with `aggregates`;
`direction` outside {max, min}; `margin` without `bound`; `bound = 0` with no explicit
`margin`; an aggregate or required summary column missing from `[result_schema]`. It
**warns** when `bound` does not appear as a literal in any outcome condition referencing `A`.

**Emission check (exit 0).** If the effective label names a declared **non-residual**
`OutcomeSpec` and summary columns are missing from the metadata, `bth run` prints a stderr
warning and emits `run.breakdown_incomplete`. The outcome is unchanged. Residual, `error` and
`invalid_measurement` runs are exempt. Summary columns are excluded from
`_result_schema_complete`, so non-zero-exit label recovery is untouched.

### D2. Aggregate detection, surfaced before the run

`uncovered_aggregates(sidecar) -> list[UncoveredAggregate]` (pure, `sidecar.py`):

1. Identifiers from every `[outcomes.*].condition` and `[sanity]` condition via
   `_columns_referenced`, intersected with `[result_schema]` keys.
2. **Aggregate-like** means: a float-typed column, no `__` in the name, matching
   `(?i)(^|_)(max|min|worst|peak)(_|$)|^p_?max`, not starting with `n_|is_|has_`, and not
   ending in `_n|_count|_samples|_steps|_idx|_flag`. Alternatively, any column with
   `aggregate = true` in `[result_schema_meta.<col>]` (the opt-in overrides type and name
   rules).
3. Uncovered: not an `[breakdown.aggregates]` key or expanded prefix match, and no `waive`.

**Surfaces.**
- `_run_script_impl`, right after the sidecar parse (≈861), **unconditionally** (also for
  non-enforced dirs): print each uncovered aggregate to stderr in the style of
  `runner.py:813/829`, and emit `run.aggregate_uncovered`.
- The same entries as `validate_sidecar` warnings, for MCP/`bth validate`.
- `bth lint` `check_aggregate_without_breakdown` and STAT-005 (D4).

No SQL-wrapper heuristic. Known misses (derived ratios, booleans computed from a maximum) are
covered by the meta opt-in, and STAT-005 names them.

### D3. Enforcement where cited (ERROR)

**Resolver** `resolve_run_sidecar(run, workspace_root) -> Sidecar | Unresolvable`:
1. log mode: `Sidecar.from_declaration(run.started.sidecar)`. This is a new full
   rehydration (breakdown, meta, waive, sanity, observe), replacing the lossy
   `_sidecar_from_declaration`. The append-only declaration needs no sha recheck;
2. else the live file at `run.sidecar_path` whose sha256 equals `runs.sidecar_sha256`;
3. else `git show refs/bathos/runs/<run_id>:<relpath>` with a matching sha;
4. else `Unresolvable`.
Only the run-time sidecar counts, so a later waiver or `[breakdown]` never passes. Runs with
no sidecar are outside this gate.

**Check** `breakdown_gate(run, sidecar)` fails if `uncovered_aggregates(sidecar)` is
non-empty with no run-time `waive`, or if the effective label names a non-residual
`OutcomeSpec`, `[breakdown]` is declared, and summary columns are missing from the metadata.

**Gate points** (each states its claim-bound predicate):
- `bth run --campaign`: after the sidecar parse (≈861), with a fresh read-only connection
  (the earlier one is closed at ≈806). Predicate: `claim_path IS NOT NULL`. It refuses before
  compute is spent.
- `_add_run_to_campaign_impl`: widen the queries to also select `claim_path`,
  `sidecar_sha256` and `metadata`. Gate after the run-row read, **before** `threshold_set`
  (≈591). Predicate: `claim_path IS NOT NULL`.
- `register_claim`: over all members. `--force` does not bypass.
- `conclude_campaign`: after the member list is built (≈1228). Predicate:
  `claim_path and registered_sha`, as the Union Gate uses. Confirmation/sequential modes
  **raise**; exploration mode warns. This intentionally departs from the downgrade-to-
  `confounded` style of the existing gates, per the user's decision. `force_verdict` does not
  bypass.
- `link_cool_runs_to_campaigns` is not gated (A7); register and conclude backstop it.

**Errors.** `class BreakdownRefused(CampaignError, RuntimeError)`, registered in
`EXCEPTION_TO_CODE` as `CAMPAIGN_ERROR`. Its `.code` is one of `BREAKDOWN_MISSING`,
`SIDECAR_UNVERIFIABLE`, `FORK_RECORD_MISSING`; all three are added to `BathosErrorCode` and
`RESOLUTION_HINTS`. It is caught first at `campaign_add_tool` (which now returns
`error_code = exc.code`), `_claim_register_sync`, `traced_tool` and the CLI handlers.

**Visibility.** `bth campaign review` lists each member's run-time waiver, read from the
resolved sidecar.

### D4. Rule card STAT-005 (same commit as the lint)

`source_check = "check_aggregate_without_breakdown"`, context column
`has_uncovered_aggregate` (one line in `build_context`, A14), and
`applies_when = "has_uncovered_aggregate"`. The body covers the 78db9e04 story, reading
`n_over`/`second`/`median` before the maximum, and the known misses.

### D5. Postmortem `[[amendments]]` (format 2 only)

```toml
format = 2
[[amendments]]
analysis_run_id = "797b791e"
amends_run_id = ""                       # required in campaign-scoped postmortems
kind = "distributional_outlier"          # distributional_outlier | reanalysis | correction
outlier_units = ["binder_17"]
effect = "verdict_contextualized"        # verdict_unchanged | verdict_contextualized | verdict_changed
note = "1 of 24 binders over; median binder max 0.004 A"
```

**Validation plumbing.** `validate_postmortem(..., run_lookup=None)`; `ValidationResult` gains
`warnings`. `run_lookup` feeds **only** format-2 checks. Callers keep passing `run=None`, so
the existing drift block (A11) does not newly fire on format-1 files. With `run_lookup`
absent, run-dependent checks become warnings ("skipped: no catalog"). `[fork]` or
`[[amendments]]` present without `format = 2` is a warning ("not enforced: add format = 2").

Errors (format 2): `analysis_run_id` unresolvable or equal to the amended run;
`distributional_outlier` with empty `outlier_units`; `verdict_changed` with
`verdict_override = "none"`. Amendments never alter `runs.outcome`; only the existing
override does (`compact.py:1270`).

**Rendering** from the file in `bth postmortem show`, `postmortem_get` and campaign review.
Amendments show regardless of drift, with a banner. `amendment_removed` is a warning in log
mode only.

### D6. Near-bound runs and `[fork]` (format 2), enforced by the obligations ledger

`near_bound(sidecar, results) -> list[NearBound]` (pure; `results` parsed from
`runs.metadata`, A17; a missing, None, NaN or non-numeric value means "unevaluated", never
near-bound). For each aggregate with `bound`:
- **single_unit** (parameter-free): the bound is crossed strictly in the worst direction and
  `A__n_over == 1`. This fires on 78db9e04.
- **margin**: `|A - bound| <= margin`. The default when `bound != 0` is
  `0.10 * |bound|`. **The 10 % is arbitrary**, a default chosen so the check is never off.
  It is frozen in the run-time sidecar, and a false positive costs one fork record, never a
  block. Authors should set `margin` from the instrument's known noise floor where one exists.
  The band is symmetric on purpose: just under the bound is as fragile as just over.

**At run end (exit 0):** if `near_bound` is non-empty, print a stderr advisory and open
obligation trigger **`near_bound`** for the run. `near_bound` is added to `TRIGGERS` and
`WIRED_TRIGGERS`, and the closed-set tests are updated. It **defaults ON**, unlike the four
existing triggers. The ledger's default-OFF rationale (`obligations.py:20-24`) is to avoid
retroactively burdening every existing script; `near_bound` fires only for sidecars that
declare the new `[breakdown]` `bound`, so no existing script is touched.
`BTH_OBLIGATION_NEAR_BOUND=0` disables it.

**Discharge:** a format-2 postmortem whose `discharges` names the obligation, with a valid
`[fork]`:

```toml
[fork]
choice = "stop"          # stop | extend | replicate | breakdown | escalate
reason = "internal validation only; not built on"
cited = false
follow_up_run_ids = []
```

Fork errors: missing `[fork]` or empty `reason` while discharging `near_bound`;
`choice = "stop"` with `cited = true`. Format-1 postmortems never discharge `near_bound`.
The scaffold pre-fills `format = 2`, an uncommented empty `[fork]`, and the obligation id in
`discharges` when one is open.

**Conclude:** open `near_bound` obligations are listed for every campaign, as all
obligations are. On a claim-bound confirmation/sequential campaign, an open `near_bound`
obligation for a member, or a discharging postmortem with `cited = false`, raises
`BreakdownRefused(FORK_RECORD_MISSING)`. No refusal at add, which would bias which runs get
included.

## 3. Out of scope

Estimand discipline (#2677; keep `unit` reusable), exploratory thresholds (#2945),
auto-computed breakdowns, aggregates in `[differential].metric` and benchmark metrics
(follow-up debt).

## 4. Fixer tasks (one commit each)

| Task | Scope | Tests |
|---|---|---|
| T0 | Probe A9 (WIP snapshot with untracked sidecar) | spike |
| T1 | `BreakdownBlock` parse/validate incl. prefix expansion; `uncovered_aggregates`; stderr surfacing at ≈861; `validate_sidecar` warnings; emission check; `_result_schema_complete` exclusion | `tests/test_breakdown_sidecar.py`: lathex names hit (`e_max_placement`, `p_max_iptm`, `max_abs_delta`, `min_dist`) and miss (`n_max`, `is_inf`, `min_samples`, `kendall_tau`, `e_max_placement__n_over`, int columns); `capsys` asserts stderr from a real `bth run` in a non-enforced dir; residual run exempt from the emission check |
| T2 | Lint + STAT-005 + `has_uncovered_aggregate` | `tests/test_breakdown_lint.py`; corpus tests green |
| T3 | `Sidecar.from_declaration` (full) replacing the lossy rebuild | round-trip test against a pre-v3 `run.started` snapshot |
| T4 | Resolver, `breakdown_gate`, `BreakdownRefused` and codes/hints/catch sites, four gate points | `tests/test_breakdown_gate.py`: each gate; unbound accepts; run-time waiver accepts; post-hoc waiver refused; residual run accepted; no `threshold_set` after a refused add; link path never raises and the register backstop catches it; MCP `error_code`; `force_verdict` does not bypass; exploration conclude warns |
| T5 | `ValidationResult.warnings`, `run_lookup` (format-2 only) | format-1 files unchanged under drift |
| T6 | D5 amendments + rendering | `tests/test_postmortem_amendments.py` |
| T7 | D6 `near_bound` (pure fn, run-end advisory, obligation trigger default ON, fork discharge, scaffold, conclude) | 78db9e04 fixture (exploration, `bound = 0.5`, 0.521, `n_over = 1`) opens `near_bound` without blocking; margin trigger; NaN unevaluated; `bound = 0` validation; discharge by format-2 fork; claim-bound conclude raises |
| T8 | Skills (`using-bathos`, `bathos-rigor-gates`), CHANGELOG | — |

Narrow pytest per task locally; full suite on CI; a reviewer with a different lens.

## 5. Risks

| Risk | Mitigation |
|---|---|
| Regex noise / misses | Type and name exclusions, meta opt-in, waiver; tested on real lathex names |
| Arbitrary 10 % margin | Stated as arbitrary; author-settable; costs a record, never a block; single_unit trigger is parameter-free |
| `near_bound` defaulting ON breaks the ledger convention | Scoped to new `[breakdown]` bounds; env opt-out; reasoning recorded |
| Existing claim-bound campaigns break at register/conclude | CHANGELOG; the remedy is a re-run or a narrowed claim, intended for cited results |
| Unresolvable legacy sidecars | `SIDECAR_UNVERIFIABLE` on claim-bound only; T0 sizes it |
| Amendments soften verdicts | Cannot touch `runs.outcome`; `verdict_changed` requires the override |

## 6. Adversarial review log

**Cycle 1**: challenger `toolu_01CntgWuq7hyxt7jFYQcoDby.1.md`, defender
`toolu_01DRbSo7x16MbZnrK9oCR5LV.1.md`. 13 findings (2 blocking, 9 major, 2 minor), none
rebutted. v2 fixed: campaign-entry bypass, missing sha resolver and post-hoc waivers,
postmortem validation without a run, the 78db9e04 premise, weak summary columns, selective
inclusion, retroactive invalidation, card scope, error reachability, rendering, regex.

**Cycle 2**: challenger (no blocking; 4 major, 6 minor), defender
`toolu_01K4UFnV7Ef88g2VwRxYCoxh.1.md` (8 conceded, 2 partial; STAT-005 context sub-point
rebutted). v3 changes:
- M1 warnings never reached the terminal → explicit stderr at ≈861, unconditional; A3 retagged false.
- M2 no fork forced for unblocked runs; `is_marginal` nonexistent; arbitrary `near` → parameter-free `single_unit` trigger, absolute `margin` with an honest default, `near_bound` obligation (default ON, reasoned), conclude enforcement.
- M3 regex self-flagging → `__` skip, float-only, trailing-token exclusions.
- M4 residual runs refused → summary columns required only for non-residual labels.
- m5 three codes / name-keyed map → `CAMPAIGN_ERROR` registration + `.code`.
- m6 gate anchors → after ≈861 with a fresh read-only connection; widened queries.
- m7 lossy declaration rebuild → `Sidecar.from_declaration`.
- m8 conclude placement and predicates → named per site; raise vs downgrade stated; `force_verdict` no bypass.
- m9 drift block → `run_lookup` format-2 only; format-less warning.
- m10 value source, NaN, strictness, `bound = 0`, wildcard families → specified.
- New (defender): obligations ledger reused instead of a parallel mechanism.
