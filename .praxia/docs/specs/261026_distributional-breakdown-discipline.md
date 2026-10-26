---
title: Distributional breakdown, amendments and fork records for aggregate verdicts
description: 'Spec for debt #3045: [breakdown] sidecar block + aggregate-verdict lint, postmortem [[amendments]], postmortem [fork] for marginal results; warn everywhere, refuse when claim-bound.'
status: draft
task_id: 261026_distributional-breakdown-debt
date: '261026'
backlog_ids: ''
adversarial_review: 'cycle 1 challenger+defender done; v2 revision'
---
# Distributional breakdown, amendments and fork records for aggregate verdicts

**v2** (after adversarial cycle 1; see §6). Debt #3045. Source postmortem: lathex
`postmortems/bathos-debt-distributional-breakdown.md` (commit a0b1f5a). Companion bug #3046
(scaffold path) is fixed separately (commit 1ed684bb).

## 0. Problem

lathex bth `78db9e04` (`rf3_dtype_probe_compare.bth.toml`) was an **exploration-stage** run
with an `[observe]` block and outcome labels `x64_floor_*`. It recorded `outcome = observed`
and `tentative_outcome = x64_floor_still_exceeds_tolerance`. The verdict rested on one number:
`e_max_placement` = 0.521 Å against a 0.5 Å bound (4 % over). "Marginal" was the human reading,
not a recorded label. A per-binder breakdown afterwards (bth `797b791e`) showed the maximum
came from 1 binder of 24 (median binder maximum 0.004 Å). The deciding statistic there was
`n_binders_over`: 1-2 binders over versus 3 or more.

Gaps:
1. Nothing prompts a per-unit breakdown before a run whose verdict rests on a worst-case
   aggregate, so the breakdown was pre-registered after the verdict.
2. No first-class way to attach later evidence naming the outlier unit to a judged run.
3. No record of what was decided after a near-bound result (stop, extend, replicate) or why.

User decision (261026): **WARNING everywhere; ERROR only where the run is cited (entry into a
claim-bound campaign).** Internal validation runs such as 78db9e04 stay unblocked.

## 1. Assumptions (re-verified in cycle 1)

| # | Assumption | Tag | Evidence |
|---|---|---|---|
| A1 | Outcome conditions are DuckDB SQL over result columns; `OutcomeSpec` fields as listed | verified | `sidecar.py:32-44` |
| A2 | `marginal` is a bookkeeping label: undeclared-allowed, not a pass branch, e-value 1.0 | verified | `validate.py:183`, `sidecar.py:1014-1022,1078` |
| A3 | `validate_sidecar` warnings are logged before every `bth run` executes | verified | `prereg.py:525-533` |
| A4 | Claim-bound iff `campaigns.claim_path IS NOT NULL`; `claim_path` can be NULL while runs are added and is set at register | verified | `claim.py:189-228,716` |
| A5 | `bth run --campaign` resolves the campaign before spawn | verified | `runner.py:788-800` |
| A6 | `_add_run_to_campaign_impl` reads the run row at ≈513; the sequential branch reads the live sidecar at 539-560; `threshold_set` is emitted at ≈591 | verified (defender) | `campaigns.py:502-591` |
| A7 | `link_cool_runs_to_campaigns` is compaction ingest and must not raise; `campaign_run.imported` is another membership path | verified (challenger+defender) | `campaigns.py:652-676` |
| A8 | Log mode: `run.started` carries `asdict(sidecar)`, so new `Sidecar` fields are snapshotted per run; legacy mode has no such snapshot | verified (challenger) | `runlog/emit.py:285` |
| A9 | `refs/bathos/runs/<id>` pins the run's commit (or WIP snapshot); `git show <ref>:<relpath>` can recover a committed sidecar | verified for the ref (`git_pin.py:56`); **unverified** that a dirty WIP snapshot contains an untracked sidecar — T0 |
| A10 | `[result_schema]` completeness is **not** checked on exit 0; `_result_schema_complete` runs only on non-zero exit for label recovery | verified FALSE-premise (v1 assumed enforcement) | `runner.py:316-330,1429,1452,1481-1483` |
| A11 | `validate_postmortem` is called without a run by `postmortem_validate_tool` and `obligations.discharge_from_postmortem`; only `postmortem_get` passes one | verified | `mcp.py:3179,3274`, `obligations.py:277` |
| A12 | `ValidationResult` has no warnings channel | verified | `postmortem.py:20-22` |
| A13 | `campaign_add_tool` returns `{ok: false, error}` with no `error_code`; `register_claim` raises `RuntimeError`; `traced_tool` maps `CampaignError` → `CAMPAIGN_ERROR`; `EXCEPTION_TO_CODE` is keyed by class name and AST-checked | verified | `mcp.py:4355-4395,3550,188`, `claim.py:645,706`, `errors.py:105-119`, `tests/test_errors.py:56` |
| A14 | `tests/test_corpus.py` requires a rule card for every `def check_*` in `linter.py` (`:241`) whose `source_check` names a linter function (`:226`) | verified | — |
| A15 | `linter._columns_referenced` extracts identifiers from a condition | verified (defender) | `linter.py:761` |
| A16 | `parse_postmortem` tolerates unknown tables but the dataclass and `postmortem_get` output drop them | verified | `postmortem.py:57-129`, `mcp.py:3286-3303` |

## 2. Design

### D1. `[breakdown]` sidecar block

```toml
[breakdown]
unit = "binder"                         # the unit the aggregate is taken over
artifact = "per_unit.parquet"           # optional, relative to BTH_OUTPUT_DIR

[breakdown.aggregates.e_max_placement]
direction = "max"                       # "max" | "min": which tail is the worst case
bound = 0.5                             # optional: the judgement bound this aggregate is read against
near = 0.10                             # optional: relative band around bound that counts as near-bound (D6)

# waive = "reason"                      # top-level alternative; see D3 for when it counts
```

For each aggregate `A`, the run emits these result columns (declared in `[result_schema]`):

| Column | Type | Meaning |
|---|---|---|
| `A__worst_unit` | str | unit at the worst value (argmax for `max`, argmin for `min`) |
| `A__second` | float | the second-worst unit's value |
| `A__median` | float | median over units of the per-unit aggregate (for e_max: median of per-binder maxima) |
| `A__n_units` | int | number of units |
| `A__n_over` | int | units beyond `bound` in the worst direction (required iff `bound` is set) |

On 78db9e04 these would have read `n_over = 1`, `second ≈ 0.0x`, `median = 0.004`, `n_units = 24`
next to 0.521, which is the same information `n_binders_over` gave after the fact.

`validate_sidecar` errors when: `[breakdown]` has neither `aggregates` nor `waive`; `unit` is
empty while `aggregates` is set; `direction` not in {max, min}; `near` set without `bound`;
an aggregate or any required summary column is missing from `[result_schema]`.

**Emission check (new, exit 0 only).** At run end, if the sidecar has `[breakdown]` and the
script exited 0, missing summary columns produce a stderr warning and a
`run.breakdown_incomplete` event. The run's outcome is unchanged. Summary columns are
**excluded** from `_result_schema_complete`, so the non-zero-exit label-recovery path
(debt 1977) is not made harder. D3 re-checks emission from the run's recorded metadata.

### D2. Aggregate detection (WARNING, surfaced before the run)

`uncovered_aggregates(sidecar) -> list[UncoveredAggregate]`, a pure helper in `sidecar.py`:

1. Collect identifiers from every `[outcomes.*].condition` and `[sanity]` condition via
   `linter._columns_referenced` (A15), intersected with `[result_schema]` keys.
2. An identifier is **aggregate-like** if it matches
   `(?i)(^|_)(max|min|worst|peak)(_|$)|^p_?max` and is not of the counter/flag form
   `(?i)^(n|is|has)_`, or if it is declared `aggregate = true` in an optional
   `[result_schema_meta.<col>]` table. The meta table is the opt-in for names the regex
   cannot see (`ratio_placement`, `leak_c12o`, a boolean `within_tol`).
3. An aggregate-like identifier is uncovered if it is not a key of `[breakdown.aggregates]`
   (exact name, or a declared prefix: an aggregate key ending in `*` such as
   `e_max_placement_*` covers the suffixed family) and there is no `waive`.

Surfaces: (a) a `validate_sidecar` **warning**, so it is printed before every `bth run` (A3),
which is the point where 78db9e04 would have been caught; (b) `bth lint`
`check_aggregate_without_breakdown`; (c) rule card STAT-005 (D4). No SQL-wrapper heuristic
(v1's `greatest(`/`max(` clause): it misfires on threshold floors such as
`greatest(0.005, 10 * x)` and does not say which column is aggregated.

Known misses, accepted and documented in STAT-005: derived ratios of maxima, booleans
computed in the script from a maximum. The opt-in is the remedy; the card asks the author.

### D3. Enforcement where cited (ERROR)

**Resolver.** `resolve_run_sidecar(run, workspace_root) -> Sidecar | Unresolvable`:
1. log mode: the `run.started` snapshot (A8);
2. else the live file at `run.sidecar_path`, if its sha256 equals `runs.sidecar_sha256`;
3. else `git show refs/bathos/runs/<run_id>:<sidecar relpath>`, if its sha matches;
4. else `Unresolvable`.

Only the run-time sidecar counts, so a waiver or `[breakdown]` added after the run never
passes. Runs with no sidecar (`--no-sidecar`, empty sha) are outside this gate. They cannot
carry a pre-registered aggregate verdict, and the existing bypass signals already count them.

**Check.** `breakdown_gate(run, sidecar)` fails if `uncovered_aggregates(sidecar)` is
non-empty and there is no run-time `waive`, or if `[breakdown]` is declared and the run's
recorded metadata lacks a required summary column.

**Gate points**, only when the campaign is claim-bound at that moment:
- `bth run --campaign`: after sidecar parse, before spawn (`runner.py` ≈795). It refuses
  before compute is spent.
- `_add_run_to_campaign_impl`: after the run-row read (≈513), **before** the `threshold_set`
  emit (≈591), so a refused run never locks a threshold.
- Backstops over **all** members (they cover `link_cool_runs_to_campaigns`, which must not
  raise, and `campaign_run.imported`): `register_claim` (claim-bound starts here) and
  `conclude_campaign`. `--force` on register does **not** bypass; a run-time waiver does.
- `Unresolvable` on a claim-bound campaign refuses with its own code.

**Errors.** `class BreakdownRefused(CampaignError, RuntimeError)` with `.code` in
{`BREAKDOWN_MISSING`, `SIDECAR_UNVERIFIABLE`}. Both codes are added to `BathosErrorCode`,
`EXCEPTION_TO_CODE` and `RESOLUTION_HINTS`. The exception is caught **first** at
`campaign_add_tool` (which now returns `error_code`), `_claim_register_sync`, `traced_tool`'s
`CampaignError` branch, and the CLI handlers.

**Visibility.** `bth campaign review` lists each member's run-time waiver text, read from the
resolved sidecar (no event-schema change).

### D4. Rule card STAT-005 (same commit as the lint, A14)

`agent_assets/corpus/stat/STAT-005_aggregate-verdict-breakdown.md`,
`source_check = "check_aggregate_without_breakdown"`. New context column
`has_uncovered_aggregate` (bool, from `uncovered_aggregates`), with
`applies_when = "has_uncovered_aggregate"`. The body covers the 78db9e04 story, reading
`n_over` / `second` / `median` before the maximum, and the regex's known misses.

### D5. Postmortem `[[amendments]]` (format 2 only)

```toml
format = 2                                  # top-level; written by the new scaffold
[[amendments]]
analysis_run_id = "797b791e"
amends_run_id = ""                          # required in campaign-scoped postmortems; defaults to run_id
kind = "distributional_outlier"             # distributional_outlier | reanalysis | correction
outlier_units = ["binder_17"]               # required for distributional_outlier
effect = "verdict_contextualized"           # verdict_unchanged | verdict_contextualized | verdict_changed
note = "1 of 24 binders over; median binder max 0.004 A"
```

Amendments are post-verdict by definition. The v1 self-reported
`preregistered_after_verdict` is dropped as uninformative.

**Validation signature.** `validate_postmortem(..., run_lookup: Callable[[str], Run | None] | None)`.
`ValidationResult` gains `warnings: list[str]`. When `run_lookup` is None, run-dependent
checks are emitted as `warnings` ("skipped: no catalog"), never silently skipped.
`postmortem_validate_tool` and `obligations.discharge_from_postmortem` pass a lookup over
the log index or warm tier.

Errors (format 2): `analysis_run_id` unresolvable or equal to the amended run;
`distributional_outlier` with empty `outlier_units`; `effect = verdict_changed` while
`verdict_override = "none"`. An amendment never alters `runs.outcome`. Changing a verdict goes
through the existing override, which itself pins the outcome (`compact.py:1270`); that is the
only path, and it leaves its own trail.

**Rendering.** `bth postmortem show`, `postmortem_get` and `bth campaign review` render
amendments from the postmortem file, with no run-schema change. They show regardless of
HEAD drift (`postmortem.py:280`), with a drift banner. `amendment_removed` (a previously
applied sha listed an amendment the current file drops) is a **warning**, enforced only in
log mode, where applied shas are events.

### D6. Postmortem `[fork]` for near-bound results (format 2 only)

```toml
[fork]
choice = "stop"          # stop | extend | replicate | breakdown | escalate
reason = "internal validation only; not built on"
cited = false
follow_up_run_ids = []
```

**Trigger: the run is near-bound.** Take the effective label: `verdict_override` if not
`none`, else `tentative_outcome` for observation classes (`compact.py:1273-1280,1333-1334`),
else `outcome`. The run is near-bound if either:
- that label is `marginal`, or its `OutcomeSpec` sets `is_marginal = true`; or
- for some `[breakdown.aggregates.A]` with `bound` and `near`, the recorded value satisfies
  `|A - bound| <= near * |bound|`. This fires on 78db9e04 (0.521 against 0.5 ± 0.05).

**Validation** (format 2, run resolvable): near-bound and no `[fork]`, or empty `reason` →
error. `choice = "stop"` with `cited = true` → error. Format-1 postmortems are untouched (no
retroactive invalidation; `discharge_from_postmortem` keeps working).

**Scaffold.** When the run is near-bound, the scaffold writes `format = 2` and an uncommented
`[fork]` with `choice = ""` and `reason = ""`, which validation then requires to be filled.

**Claim-bound enforcement at conclude, not at add.** Refusing near-bound runs at add would be
selective inclusion, and the postmortem is normally written after add. Instead,
`conclude_campaign` on a claim-bound campaign refuses (`BreakdownRefused`, code
`FORK_RECORD_MISSING`) while any near-bound member lacks a postmortem with
`status != "draft"` and a valid `[fork]` with `cited = true`. The conclude report records
that postmortem's sha256.

## 3. Out of scope

- Estimand / null-role discipline (#2677). Keep `[breakdown].unit` reusable as `estimand.unit`.
- Exploratory threshold-setting (#2945).
- Auto-computing breakdowns: the project's script emits the columns.
- Scanning `[differential].metric` and benchmark metrics for aggregates (follow-up debt).

## 4. Fixer tasks (one commit each)

| Task | Scope | Tests |
|---|---|---|
| T0 | Probe A9 (WIP snapshot contains an untracked sidecar?); confirm `run.started` snapshot shape for a new dataclass field | spike, throwaway |
| T1 | `BreakdownBlock` parse + validate (D1); `uncovered_aggregates`; `validate_sidecar` warning (D2a); `_result_schema_complete` excludes summary columns; run-end emission check | `tests/test_breakdown_sidecar.py`: parse/validate errors; regex on real lathex names: hits `e_max_placement`, `p_max_iptm`, `max_abs_delta`, `min_dist`; misses `n_max`, `is_inf`, `min_samples`, `kendall_tau`; prefix family; meta opt-in; warning printed by `bth run`; exit-0 missing column → event, outcome unchanged; non-zero exit recovery unchanged |
| T2 | `check_aggregate_without_breakdown` + STAT-005 + `has_uncovered_aggregate` (single commit, A14) | `tests/test_breakdown_lint.py` + corpus tests stay green |
| T3 | `resolve_run_sidecar`; `breakdown_gate`; `BreakdownRefused` + codes + hints + catch sites; gates at run --campaign, add, register, conclude | `tests/test_breakdown_gate.py`: claim-bound refuses at each point; unbound accepts; run-time waiver accepts; post-hoc waiver refused; sha mismatch → git-show path; unresolvable → `SIDECAR_UNVERIFIABLE`; refused add leaves no `threshold_set`; `link_cool_runs_to_campaigns` never raises and the register backstop catches it; MCP returns `error_code` |
| T4 | `ValidationResult.warnings`; `run_lookup` plumbing at both callers | `tests/test_postmortem_validate_lookup.py` |
| T5 | D5 amendments (format 2) + rendering | `tests/test_postmortem_amendments.py` |
| T6 | D6 near-bound, fork validation, scaffold, conclude gate | `tests/test_postmortem_fork.py`, incl. a 78db9e04-shaped fixture: exploration stage, `e_max_placement = 0.521`, `bound = 0.5`, `near = 0.1` → near-bound |
| T7 | Skill sections (`using-bathos`, `bathos-rigor-gates`), CHANGELOG | — |

Narrow pytest per task locally; full suite on CI. A reviewer with a different lens signs off.

## 5. Risks

| Risk | Mitigation |
|---|---|
| Regex noise | WARNING outside claim-bound; counter/flag exclusion; waiver; lathex-name tests |
| Regex misses | Meta opt-in; STAT-005 names the known misses |
| `near` chosen after the fact | `near` lives in the run-time sidecar; the resolver ignores later edits |
| Existing claim-bound campaigns break at next register/conclude | CHANGELOG; a run-time waiver is not retrofittable, so the honest remedy for an old run is a re-run or narrowing the claim. This is intended for cited results |
| Legacy-mode runs whose sidecar changed and whose WIP snapshot lacks it | `SIDECAR_UNVERIFIABLE` on claim-bound only; T0 sizes the population |
| Amendments soften verdicts | They cannot touch `runs.outcome`; `verdict_changed` requires the override path |

## 6. Adversarial review log

**Cycle 1** (challenger then defender, both Sonnet; reports in `.praxia/subagent_outputs/`
`toolu_01CntgWuq7hyxt7jFYQcoDby.1.md`, `toolu_01DRbSo7x16MbZnrK9oCR5LV.1.md`). 13 findings
(2 blocking, 9 major, 2 minor); defender conceded or partly conceded all, rebutted none. v2 changes:
- B1 run --campaign bypass → gates at run --campaign + add; register/conclude backstops; compaction link not gated.
- B2 no sha resolver / post-hoc waiver / A10 false → `resolve_run_sidecar`, run-time-only waiver, exit-0 emission check.
- M postmortem validate lacks run → `run_lookup` + warnings channel.
- M 78db9e04 was exploration, not marginal → effective-label + near-bound trigger.
- M summary columns could not separate 1 from 3 of 24 → `n_over`, `second`, defined `median`, `direction`.
- M selective inclusion / ordering → fork enforced at conclude, non-draft, sha-bound.
- M retroactive invalidation → `format = 2` scoping.
- M card applies_when too broad; T2/T4 split breaks corpus test → `has_uncovered_aggregate`; merged task.
- M unreachable error code → `BreakdownRefused` subclass, catch-first sites.
- M rendering/drift → render from file, show despite drift.
- M regex FP/FN → `_columns_referenced`, counter exclusion, prefix families, no SQL-wrapper clause.
- m A6b verified; verdict_changed semantics stated.
- New (defender): event keys fixed → waivers read from the resolved sidecar.
