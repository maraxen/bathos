---
title: Distributional breakdown, amendments and fork records for aggregate verdicts
description: 'Spec for debt #3045: [breakdown] sidecar block + aggregate-verdict lint, postmortem [[amendments]], postmortem [fork] for marginal results; warn everywhere, refuse when claim-bound.'
status: draft
task_id: 261026_distributional-breakdown-debt
date: '261026'
backlog_ids: ''
adversarial_review: ''
---
# Distributional breakdown, amendments and fork records for aggregate verdicts

Debt #3045. Source postmortem: lathex `postmortems/bathos-debt-distributional-breakdown.md`
(commit a0b1f5a). The companion bug #3046 (scaffold path) is fixed on branch
`worktree-fix-postmortem-scaffold-path` (commit 1ed684bb) and is not part of this spec.

## 0. Problem

lathex bth `78db9e04` judged a dtype-probe floor compare on one number: `e_max_placement`
= 0.521 Å against a 0.5 Å bound, labelled marginal. A per-binder breakdown run afterwards
(bth `797b791e`, cross-checked to 1e-9) showed the maximum came from 1 of 24 binders; the
median binder maximum was 0.004 Å. Three gaps in bathos made this possible:

1. Nothing prompts a per-unit breakdown when a verdict rests on a maximum (or any single
   worst-case aggregate), so the breakdown was pre-registered *after* the verdict.
2. There is no first-class way to amend a judged run with a later analysis that names the
   outlier unit. The options are a verdict override or free text.
3. A marginal result carries no record of what was decided next (stop, spend more compute,
   replicate) or why.

User decision (261026): **lint WARNING everywhere; ERROR only where the run is cited, i.e.
when it enters a claim-bound campaign.** Internal validation checks such as 78db9e04 must
stay unblocked.

## 1. Assumptions (verified = read at file:line in this checkout)

| # | Assumption | Tag | Evidence |
|---|---|---|---|
| A1 | Outcome conditions are DuckDB SQL fragments over result columns; `OutcomeSpec` holds `condition`, `decision`, `reasoning`, `is_residual`, `adversarial_check`, `source`, `multiple_comparisons_correction` | verified | `src/bathos/sidecar.py:32-44` |
| A2 | `marginal` is a recognised bookkeeping label: allowed undeclared, excluded from pass branches, e-value 1.0 | verified | `validate.py:183`, `sidecar.py:1014-1022`, `sidecar.py:1078` |
| A3 | Tier-2 sidecar lints follow the `check_threshold_basis` pattern (iterate `iter_project_sidecars`, emit `LintIssue` WARNING) and are wired in `lint_project` | verified | `linter.py:873-947`, `linter.py:110-190` |
| A4 | A campaign is claim-bound iff `campaigns.claim_path IS NOT NULL` | verified | `claim.py:189-228` |
| A5 | `add_run_to_campaign` already refuses on mode-specific conditions with `CampaignError` (e.g. a confirmation run that predates its campaign) | verified | `campaigns.py:533-537` |
| A6 | Postmortems are TOML with `[postmortem]`, `[decisions]`, `[asset_links]`, `[anomalies]`, read by `parse_postmortem` | verified | `postmortem.py:57-130` |
| A6b | Unknown tables in a postmortem are tolerated everywhere one is read (validate, compact fold, MCP get) | **unverified** — probe in T0 | — |
| A7 | `validate_postmortem` receives the `Run`, so it can read `run.outcome` | verified (the parameter exists; **unverified** that every caller passes it) | `postmortem.py:153-159` |
| A8 | The postmortem-applied event carries the file's sha256, so successive applied versions are distinguishable | verified | `postmortem.py:132-150` |
| A9 | Rule cards are `agent_assets/corpus/<domain>/<ID>_<slug>.md` with `+++` TOML frontmatter; `stat/` has STAT-001..004 | verified | `corpus.py:110-133`, `agent_assets/corpus/stat/` |
| A10 | At run end the runner checks emitted result keys against `[result_schema]` | **unverified** — probe in T0; if false, D1's summary columns are checked only statically | — |
| A11 | `add_run_to_campaign` currently accepts a `marginal` run into a claim-bound campaign without comment | **unverified** — probe in T0 | — |
| A12 | Previously applied postmortem versions (their amendment lists) can be read back from the event log at validate/compact time | **unverified** — decides whether D5's append-only rule is enforced or advisory | — |

## 2. Design

### D1. `[breakdown]` sidecar block

```toml
[breakdown]
unit = "binder"                       # the unit the aggregate is taken over
aggregates = ["e_max_placement"]      # result columns that are a max/min/worst case over units
artifact = "per_unit.parquet"         # optional, relative to BTH_OUTPUT_DIR: unit + one column per aggregate
# waive = "reason"                    # alternative to the above; recorded and surfaced, never silent
```

For each name in `aggregates` the run also emits three summary result columns:
`<agg>__argmax_unit` (str), `<agg>__median` (float), `<agg>__n_units` (int). They are declared
in `[result_schema]` as usual, so they land in the run record and stay queryable and citable.
In the motivating case the record would have shown `argmax_unit = <binder>` and
`median = 0.004` next to the 0.521 maximum.

`validate_sidecar` errors when: `[breakdown]` has neither `aggregates` nor `waive`; `unit` is
empty while `aggregates` is set; an `aggregates` entry or one of its summary columns is
missing from `[result_schema]`.

### D2. Lint `aggregate_without_breakdown` (Tier-2, WARNING)

For each sidecar, collect the identifiers referenced in `[outcomes.*].condition`. A condition
is **aggregate-gated** if it references a column that:

- matches `(?i)(^|_)(max|min|worst|peak|sup|inf)(_|$)` or `(?i)^p_?max`, or
- is declared `aggregate = true` in the new optional `[result_schema_meta.<col>]` table (an
  opt-in for names the regex misses, e.g. `floor_err`), or
- is wrapped in SQL `greatest(` / `least(` / `max(` / `min(` inside the condition.

It warns when an aggregate-gated column is neither in `[breakdown].aggregates` nor covered
by `[breakdown].waive`; the detail names the column and the outcome label. The regex is a
heuristic, not a measured classifier. Its job is to prompt a decision, and the opt-in and the
waiver are the escape hatches in each direction. `min` is included because a worst case can
be a minimum (e.g. min contact distance).

### D3. Enforcement where cited (ERROR)

- `add_run_to_campaign`: when the target campaign is claim-bound (A4) and the run's sidecar
  (resolved by the sha recorded on the run) has an uncovered aggregate-gated column, raise
  `CampaignError` with a new `BathosErrorCode.BREAKDOWN_MISSING` and a RESOLUTION_HINTS entry
  ("declare [breakdown] with the summary columns and re-run, or set [breakdown].waive with a
  reason"). A waiver passes and is recorded in the campaign-add event.
- `register_claim`: the same check over runs already in the campaign; refuse on the first
  failure. `--force` does **not** bypass it; a waiver does.
- `bth campaign review` lists each waiver with its reason.

One pure helper, `breakdown_coverage(sidecar) -> list[UncoveredAggregate]`, is shared by the
lint, campaign add and claim register, so the three surfaces cannot drift. This is the same
reasoning as the shared `scaffold_postmortem_template` (debt #479).

### D4. Rule card STAT-005

`agent_assets/corpus/stat/STAT-005_aggregate-verdict-breakdown.md`,
`source_check = "check_aggregate_without_breakdown"`, severity warning. Body: the 78db9e04
story in three sentences; what to do (declare `[breakdown]` before the run and read argmax and
median alongside the maximum); when a waiver is honest (a genuinely single-unit quantity).
Add the context column `has_breakdown` (bool) to `CONTEXT_COLUMNS` / `build_context`, with
`applies_when = "has_sidecar AND NOT has_breakdown"`. The lint is the precise trigger; the
card carries the reasoning.

### D5. Postmortem `[[amendments]]`

```toml
[[amendments]]
analysis_run_id = "797b791e"               # the run that produced the new evidence
kind = "distributional_outlier"            # distributional_outlier | reanalysis | correction
outlier_units = ["binder_17"]              # required when kind = distributional_outlier
effect = "verdict_contextualized"          # verdict_unchanged | verdict_contextualized | verdict_changed
preregistered_after_verdict = true         # honest flag: true is allowed, never hidden
note = "max is one binder of 24; median binder max 0.004 A"
```

Semantics: an amendment **adds evidence and never replaces the verdict**; the run's recorded
outcome is untouched. `validate_postmortem` errors when:
- `analysis_run_id` does not resolve in the catalog (unique-prefix match, as elsewhere);
- `analysis_run_id == run_id`;
- `kind = distributional_outlier` and `outlier_units` is empty;
- `effect = verdict_changed` and `verdict_override` is `none`. Changing a verdict uses the
  existing override, which leaves its own trail; an amendment cannot do it quietly.

Append-only (contingent on A12): if an earlier applied version of the same postmortem listed
an amendment (keyed by `analysis_run_id` + `kind`) that the current version drops, validate
warns `amendment_removed`. If A12 is false, this becomes a follow-up debt.
`postmortem_applied_event_data` gains `n_amendments` and `amendment_run_ids`; `bth show <run>`
and campaign review render amendments under the outcome.

### D6. Postmortem `[fork]` for marginal results

```toml
[fork]
choice = "stop"          # stop | extend | replicate | breakdown | escalate
reason = "internal validation only; not built on"
cited = false            # will this result be cited or built on?
follow_up_run_ids = []   # runs spawned by extend / replicate / breakdown
```

- `validate_postmortem` errors when the run is marginal and `[fork]` is absent or `reason` is
  empty. "Marginal" means the label `marginal` (A2), plus any outcome whose `OutcomeSpec`
  sets a new optional `is_marginal = true` (for projects that use other names, e.g.
  `borderline`).
- `choice = "stop"` with `cited = true` is a validate error (a contradiction).
- The scaffold pre-fills a commented `[fork]` when the run's outcome is marginal.
- Claim-bound (contingent on A11): `add_run_to_campaign` into a claim-bound campaign refuses
  a marginal run unless an applied postmortem has a `[fork]` with `cited = true` and
  `choice != "stop"` (code `FORK_RECORD_MISSING`).

The fork record states the decision without judging it. Stopping is a valid choice; it just
has to be written down.

## 3. Out of scope

- General estimand / null-role discipline (#2677). `[breakdown].unit` is a small step toward an
  estimand declaration; keep it compatible so it can become `estimand.unit` when #2677 is specced.
- Threshold-setting for exploratory staging (#2945).
- Auto-computing breakdowns. bathos never runs analysis code; the project's script emits the
  columns.

## 4. Fixer tasks (one commit each)

| Task | Scope | Tests |
|---|---|---|
| T0 | Probe A6b, A7 callers, A10, A11, A12; update the §1 tags before any code | spike, throwaway |
| T1 | `BreakdownBlock` dataclass + parse in `sidecar.py`; `validate.py` rules (D1); `breakdown_coverage()` helper | `tests/test_breakdown_sidecar.py`: parse, each validate error, waiver, regex hits (`e_max_placement`, `p_max`, `worst_rmsd`, `min_dist`, `max(` in SQL) and misses (`maximize_flag`, `n_iter`) |
| T2 | `check_aggregate_without_breakdown` wired in `lint_project`; the MCP `lint` tool mirrors it | `tests/test_breakdown_lint.py`: warns on a 78db9e04-shaped sidecar; silent with breakdown or waiver |
| T3 | D3 gates in `add_run_to_campaign` and `register_claim`; error codes + RESOLUTION_HINTS; `tests/test_errors.py` maintenance trap updated | `tests/test_breakdown_gate.py`: claim-bound refuses, unbound accepts, waiver accepts, `--force` does not bypass |
| T4 | STAT-005 card + `has_breakdown` context column | corpus load test; `applicable_cards` returns STAT-005 for an uncovered sidecar |
| T5 | D5 amendments: parse, validate, event data, `bth show` rendering | `tests/test_postmortem_amendments.py` |
| T6 | D6 fork: parse, validate, scaffold pre-fill, `is_marginal`, claim-bound gate | `tests/test_postmortem_fork.py` |
| T7 | `using-bathos` and `bathos-rigor-gates` skill sections; CHANGELOG | skill lint |

Gates: narrow pytest per task locally (never the whole suite; see local-compute-limits); the
full suite runs on CI. A reviewer with a different lens from the implementer signs off.

## 5. Risks

| Risk | Mitigation |
|---|---|
| Regex false positives (`max_iter`, `n_max`) spam lint | WARNING only outside claim-bound; a waiver silences it; T1 pins negative cases; only columns referenced in an outcome condition are scanned |
| Regex false negatives (`floor_err`) | `[result_schema_meta.<col>].aggregate = true` opt-in; STAT-005 asks the author directly |
| Existing claim-bound campaigns break on the next `campaign add` | D3 checks only runs added and claims registered from now on; concluded campaigns are untouched; CHANGELOG entry |
| Waivers become rubber stamps | Waiver text is surfaced in campaign review; a sprint-audit signal is a follow-up |
| An amendment is used to soften a failed verdict | `effect = verdict_changed` requires the existing override path; amendments never touch `runs.outcome` |

## 6. Adversarial review log

(filled by the challenger → defender cycles)
