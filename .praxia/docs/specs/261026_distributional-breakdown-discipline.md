---
title: Distributional breakdown, amendments and fork records for aggregate verdicts
description: 'Spec for debt #3045: [breakdown] sidecar block + aggregate-verdict lint, postmortem [[amendments]], postmortem [fork] for marginal results; warn everywhere, refuse when claim-bound.'
status: draft
task_id: 261026_distributional-breakdown-debt
date: '261026'
backlog_ids: ''
adversarial_review: 'converged at cycle 9; v10'
---
# Distributional breakdown, amendments and fork records for aggregate verdicts

**v10** (converged after adversarial cycles 1-9; see §6). Debt #3045. Source postmortem: lathex
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

**Acceptance walk-through (78db9e04 under v10).** Before the run, `bth run` prints to stderr
that `e_max_placement` and `e_max_iptm` gate outcomes with no `[breakdown]` (D2). Over MCP,
the same advisories come back in the `run_tool` response's `warnings`. Had the author declared
`[breakdown]` with `bound = 0.5`, the run would have recorded `e_max_placement__n_over = 1`
with `n_units = 24`. It would then have matched the parameter-free `single_unit` near-bound
trigger (D6), printed an advisory, and opened a `near_bound` obligation. That obligation is
default ON and opens for exploratory runs too. It stays open until
`bth postmortem validate` passes on a format-2 postmortem with a `[fork]` that discharges it. The run is never blocked. If the
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
| A11 | `validate_postmortem` errors on `git_dirty`/HEAD drift whenever `run` is passed. `postmortem_validate_tool` and `discharge_from_postmortem` pass none; `postmortem_get_tool` **does** pass `run=` (drift already applies there and is unchanged) | verified (corrected cycle 4) | `postmortem.py:270-288`, `mcp.py:3179,3274`, `obligations.py:277` |
| A12 | `ValidationResult` has no warnings channel | verified | `postmortem.py:20-22` |
| A13 | `EXCEPTION_TO_CODE` is class-name keyed with one value; `campaign_add_tool` returns no `error_code`; `register_claim` raises `RuntimeError` | verified | `errors.py:105-119`, `mcp.py:4355-4395,3550`, `claim.py:645,706` |
| A14 | Each `def check_*` in `linter.py` needs a rule card; `build_context` already holds the parsed sidecar | verified | `tests/test_corpus.py:226,241`, `corpus.py:314` |
| A15 | `linter._columns_referenced` does word-boundary extraction | verified | `linter.py:761-768` |
| A16 | Obligations ledger: per-trigger flags default OFF (`resolve_flag` returns False when env and `.bth.toml [obligations]` are unset, `config.py:60-85`; `trigger_enabled` has no default, `obligations.py:84-95`), opened idempotently, listed at conclude; `TRIGGERS` is a closed set asserted by tests (`test_obligation_config.py:51,55`, `test_obligation_triggers.py:95-110`). **Discharge is UNWIRED**: `discharge_from_postmortem` (`obligations.py:263`) has no caller in `src/`, and `postmortem_validate_tool` (`mcp.py:3179`, wrapped by `cli_cyclopts.py:126`) validates without discharging | verified (cycle 3; orchestrator grep) | as cited |
| A17 | Run results are parsed from `runs.metadata` (JSON string) | verified | `runner._parse_result_dict:387`, `schema.py:195` |
| A18 | Exploratory runs store `outcome = "observed"` plus `tentative_outcome` (`runner.py:472-478`); a postmortem override for observation classes also lands in `tentative_outcome` (`compact.py:1273-1280,1333-1334`) | verified | as cited |
| A19 | The run-end `outcome_failed` open sits behind `epistemic_class not in ("exploratory","calibrating")` (`runner.py:1608`); run obligations are keyed by the full `run.id` (`runner.py:1611-1614`) | verified (orchestrator read) | as cited |
| A20 | `run_tool` calls `run_script` in-process (`mcp.py:1941`) and returns a fixed dict (`mcp.py:1964-1971`), so runner stderr never reaches an MCP caller; `run_uuid_var` (`telemetry.py`, imported at `runner.py:54`) is an existing ContextVar precedent | verified | as cited |
| A21 | Sanity is `[observe].sanity: list[str]` (`sidecar.py:342,517`), not a table; `validate.py:787-790` skips non-str `result_schema` values | verified | as cited |

## 2. Design

### D1. `[breakdown]` sidecar block

```toml
[breakdown]
unit = "binder"
artifact = "per_unit.parquet"           # optional, relative to BTH_OUTPUT_DIR
# waive = "reason"                      # alternative to aggregates; must sit here, before any
                                        # sub-table (D3: only the run-time sidecar counts)

[breakdown.aggregates.e_max_placement]
direction = "max"                       # "max" | "min"
bound = 0.5                             # optional judgement bound
margin = 0.05                           # optional absolute near-bound band (D6); see default below
```

Aggregate keys may end in `*` (`e_max_placement_*`). The prefix expands over **float**
`[result_schema]` keys **that contain no `__`**, so a prefix never matches its own summary
columns (`e_max_placement__second`). An unmatched prefix is a validation error, and so is an
explicit aggregate key containing `__`. Each expanded column is an aggregate with the same
settings. Order: expand first, then check the required summary columns.

**Effective label** (used by the emission check and `breakdown_gate`):
`tentative_outcome if outcome == "observed" and tentative_outcome else outcome` (A18). It is
exempt when it is None, `error`, `invalid_measurement` or a residual label.
`_add_run_to_campaign_impl` also selects `tentative_outcome` (`schema.py:251`).

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

1. Identifiers from every `[outcomes.*].condition` and every `[observe].sanity` expression
   string (A21) via `_columns_referenced`, intersected with `[result_schema]` keys.
2. **Aggregate-like** means: a float-typed column, no `__` in the name, matching
   `(?i)(^|_)(max|min|worst|peak)(_|$)|^p_?max`, not starting with `n_|is_|has_`, and not
   ending in `_n|_count|_samples|_steps|_idx|_flag`. Alternatively, any column with
   `aggregate = true` in `[result_schema_meta.<col>]` (the opt-in overrides type and name
   rules). `[result_schema_meta]` is a **new separate top-level table**, not nested in
   `[result_schema]`. T1 adds its parse field and validation: every key must be a
   `[result_schema]` column, and only the bool key `aggregate` is allowed.
3. Uncovered: not an `[breakdown.aggregates]` key or expanded prefix match, and no `waive`.

**Surfaces.**
- `_run_script_impl`, right after the sidecar parse (≈861), **unconditionally** (also for
  non-enforced dirs): call `_advise("aggregate_uncovered", msg, column=c)` for each uncovered
  aggregate, and emit `run.aggregate_uncovered`.
- **Advisory channel (A20).** A new `run_advisories_var: ContextVar[list[dict] | None]` sits
  beside `run_uuid_var`. The runner helper `_advise(code, message, **kw)` prints to stderr in
  the style of `runner.py:813/829` and, if the var is set, appends
  `{"code", "message", **kw}`. `run_tool` sets it to `[]` before calling `run_script`
  (≈`mcp.py:1938`). It always returns `"warnings": [...]`, an empty list when there is
  nothing, and keeps the existing singular `"warning"` key. Codes: `aggregate_uncovered`,
  `breakdown_incomplete`, `near_bound` (with `obligation_id`). The existing 813/829 prints are
  left alone.
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

**Gate points** (each states its claim-bound predicate). `register_claim` and conclude take
membership from `union_campaign_member_ids`, so members that exist only in the cool tier
are gated too.
- `bth run --campaign`: after the sidecar parse (≈861), with a fresh read-only connection
  (the earlier one is closed at ≈806). Predicate: `claim_path IS NOT NULL`. It refuses before
  compute is spent. No run result exists yet, so only the **aggregate half** of
  `breakdown_gate` applies here. The summary-column half runs at add, register and conclude.
- `_add_run_to_campaign_impl`: widen the queries to also select `claim_path`,
  `sidecar_sha256` and `metadata`. Gate after the run-row read, **before** `threshold_set`
  (≈591). Predicate: `claim_path IS NOT NULL`.
- `register_claim`: over all members. `--force` does not bypass.
- `conclude_campaign`: inside the claim-bound block, immediately after
  `precomputed_member_ids = members` (`campaigns.py:1228`) and before the BP-3 check (≈1230).
  This is outside any `try`, and before any outcome mutation. The existing obligation scan at
  ≈1460-1472 swallows exceptions and reads only warm `campaign_runs`, so it is not used.
  Iterate `members`, which includes cool-tier members (`union_campaign_member_ids`,
  ≈268-286). Both the breakdown gate and the D6 fork check run here. Predicate:
  `claim_path and registered_sha`, as the Union Gate uses. Confirmation/sequential modes
  **raise**; exploration mode warns. This intentionally departs from the downgrade-to-
  `confounded` style of the existing gates, per the user's decision. `force_verdict` does not
  bypass. The existing obligation gate at ≈1474 is left as is; under ENFORCE it would also
  downgrade on the same open obligation, which is harmless because the raise comes first.
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
`warnings`. `run_lookup` feeds **only** format-2 checks. **Wiring:** the catalog lookup
inside `postmortem_get_tool` (warm query, then `read_runs` fallback, ≈`mcp.py:3240-3270`) is
replaced by a new public core function, **`query.lookup_run(run_id, catalog_dir, *,
cool_runs=None) -> Run | None`**. It lives in `query.py`, beside `get_run` (`query.py:357`),
and `mcp.py`, `campaigns.py`, `obligations.py` and `postmortem.py` import it from there, so no
core module imports `mcp.py` (thin-layer rule, AGENTS.md). In this spec `_lookup_run` means
this function. It does **unique-prefix matching**, which the existing code does not (exact
`WHERE id = ?`, `mcp.py:3259`; `r.id == run_id`, `:3270`):
1. Collect candidate **full ids** whose id starts with `run_id`. This is the **union** of the
   active backend (index or warm, `_resolve_backend`) and the cool fragments, read once or
   taken from `cool_runs`. The backend side needs a **new** prefix query,
   `SELECT id FROM runs WHERE id LIKE ? || '%' ESCAPE '\'`, on both index and warm. None
   exists today; `get_run` is exact-only. The prefix must be non-empty, with `%`, `_` and `\`
   escaped. An empty `run_id` returns None. In **index (log) mode** the cool list holds only
   legacy and pre-cutover fragments: log-mode runs are written as `run.started` and
   `run.finished` events only (`runner.py:1109-1126`), and `write_run` sits in the legacy
   branch.
2. **Dedupe by full id** before counting. Cool fragments are kept after compaction, so a
   compacted run appears in both.
3. No candidate returns None. More than one distinct full id raises `AmbiguousRunId`, which
   validation reports as an error naming the candidates.
4. Exactly one: fetch the backend row with an exact query on the active backend
   (`_index_get_run` / `_warm_get_run`), so the compacted or ingested row wins when it exists.
   **Warm and cool backends:** if that returns None and the full id came from the cool list,
   return **that cool `Run` object itself**, tagged `_source = "cool_unindexed"` as
   `_cool_fallback_get_run` does, with compact-time fields absent. It does not route through
   `get_run(..., allow_cool_fallback=True)`, because the cool backend would rescan
   (`query.py:348-354`). This preserves today's `postmortem_get_tool` `read_runs` fallback.
   **Index (log) backend:** the cool-object fallback does not apply (`get_run` ignores the
   flag there, `query.py:370-381`, and fresh runs have no fragment). On a miss, which
   includes no prefix candidate at all, `lookup_run` folds pending events **once** and
   retries:
   - It calls `runlog.ingest.run_ingest(catalog_dir, require_log_mode=False,
     lock_timeout_s=60.0)` directly, the same call `_fold_pending_events` makes
     (`campaigns.py:248-266`). It is called directly so that `query.py` does not import
     `campaigns.py`.
   - An ingest exception is logged and treated as a miss.
   - Where ingest is a no-op by design (SLURM, `BTH_NO_INGEST=1`), a not-yet-ingested run
     stays unresolved and `lookup_run` returns None. Callers say so. D5's
     "`analysis_run_id` unresolvable" error and the entity check's "not discharged" reason
     append: "if this is a log-mode run not yet ingested (SLURM / `BTH_NO_INGEST`), run
     `bth compact` and retry".
   - Claim-bound conclude is unaffected: `prepare_catalog_for_conclude` ingests before any
     gate, and an unresolved member fails closed (`SIDECAR_UNVERIFIABLE`).
   - **Read tools now fold.** On a miss, `postmortem_get_tool` folds events into `index.db`.
     That is derived data, swapped in atomically by ingest. The tool's "reads never write"
     docstring (`mcp.py:3284`) is reworded: "never emits run events; may fold pending
     events into the index".
   - **One fold per operation.** `lookup_run` takes `ingest_state: dict | None`. A
     validation's `run_lookup` partial and conclude's member loop pass one shared dict, so at
     most one ingest runs per validation or conclude, however many ids miss.

   `metadata`, `sidecar_path` and `sidecar_sha256`, which the resolver and `near_bound` need,
   are present in every tier (index columns at `index.py:117-148`).

The `cool_runs` parameter lets a caller doing many lookups pass one pre-read list, so the
cost is a single `read_runs` scan rather than N. `union_campaign_member_ids` (which calls
`read_runs` itself, `campaigns.py:283`) gains the same optional `cool_runs` parameter, and
conclude reads the list once and passes it to both.

The D5 example `analysis_run_id = "797b791e"` therefore resolves. This one helper serves
`run_lookup`, the near_bound entity check, the scaffold's full-id resolution, and conclude's
Run-row fetch for cool-only members.

Both `postmortem_validate_tool` and `postmortem_get_tool` pass
`run_lookup=partial(_lookup_run, cat_dir)`. `postmortem_validate_tool` keeps `run=None`;
`postmortem_get_tool` keeps its existing `run=` (A11). `discharge_from_postmortem` (no
catalog) keeps `run_lookup=None` and so gets warnings. The drift block (A11) therefore does
not newly fire anywhere. With `run_lookup`
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
- **single_unit** (parameter-free): the bound is crossed strictly in the worst direction,
  `A__n_over == 1` and `A__n_units >= 2`. With one unit, "one unit over" carries no
  distributional information. This fires on 78db9e04.
- **margin**: `|A - bound| <= margin`. The default when `bound != 0` is
  `0.10 * |bound|`. **The 10 % is arbitrary**, a default chosen so the check is never off.
  It is frozen in the run-time sidecar, and a false positive costs one fork record, never a
  block. Authors should set `margin` from the instrument's known noise floor where one exists.
  The band is symmetric on purpose: just under the bound is as fragile as just over.

**At run end (exit 0):** if `near_bound` is non-empty, call `_advise("near_bound", …)` and
open obligation trigger **`near_bound`** for entity `("run", run.id)`, with the full id
(A19). The open sits **outside** the `epistemic_class not in ("exploratory","calibrating")`
guard at `runner.py:1608`, so exploratory runs like 78db9e04 open one. The obligation id is
`run_<fullid>_near_bound` (format `obligations.py:182`).

**Default ON, mechanized.** `near_bound` is added to `TRIGGERS` and `WIRED_TRIGGERS`. A new
`DEFAULT_ON = frozenset({"near_bound"})` lives in `obligations.py`. `resolve_flag` gains
`default: bool = False`, threaded through all three non-env returns (`config.py:82` no config
file, `:83` key absent, `:85` exception). `trigger_enabled` passes `default=trigger in DEFAULT_ON`. The ledger's
default-OFF rationale (`obligations.py:20-24`) is to avoid retroactively burdening every
existing script. `near_bound` fires only for sidecars that declare the new `[breakdown]`
`bound`, so no existing script is touched. The docstrings at `obligations.py:13-24,68-73`
are updated to say so.

**Opt-outs:** `BTH_OBLIGATION_NEAR_BOUND=0` (env wins, `config.py:76-78`), or durably
`[obligations] near_bound = false` in `.bth.toml`. The latter is the form SLURM jobs honour.
The opt-out turns off only the **run-time** advisory and ledger open. It never bypasses the
claim-bound conclude check, which recomputes `near_bound` itself (see Conclude). The
run-time open also sits in a `try` that swallows errors (`runner.py:1632`), so the ledger
alone is not trusted for enforcement.

**Test edits:**
- `test_obligation_config.py:51,55` iterate `TRIGGERS - DEFAULT_ON`.
- `test_obligation_triggers.py:95-98` does the same and is renamed.
- `test_obligation_triggers.py:101-110` is parametrized over `TRIGGERS - DEFAULT_ON`.
- `WIRED_TRIGGERS == TRIGGERS` (≈:133) is unchanged.
- New tests: on with empty config; off with config false; off with env 0; env 0 beats
  config true.

**Discharge wiring (A16 was unwired).** In `postmortem_validate_tool`, after `result.ok`
(≈`mcp.py:3180`), call a new
`obligations.discharge_validated(ws, pm, pm_path, catalog_dir)`. It discharges the ids in
`pm.discharges` without validating a second time. `catalog_dir` comes from
`_get_catalog_dir`, as the tool already resolves it, and serves the entity check.
`discharged_by` is stored **workspace-relative** (POSIX) when the file is under `ws`, and
absolute otherwise. It is never cwd-relative; the existing `discharge()` stores `by_path`
verbatim (`obligations.py:258`), and this fixes that for all triggers. Its response gains
`"discharged": [ids]`. `bth postmortem validate` wraps that tool (`cli_cyclopts.py:126`), so
this one point covers the CLI and MCP. `discharge_from_postmortem` stays as the
run-independent wrapper.

**Entity check (new).** `discharge` does no entity check today. For `near_bound` ids it now
requires one of two things:
- `pm.run_id` resolves to the obligation's run via `_lookup_run(catalog_dir, …)` (unique
  prefix).
- The postmortem is campaign-scoped and its members include that run
  (`union_campaign_member_ids`). Otherwise the id is reported as not discharged,
with a reason.

**Re-discharge (near_bound only).** `discharge()` does nothing on an already-discharged
obligation (`obligations.py:256-259`). Without a change, a run first closed by an internal
`stop`/`cited = false` postmortem could never be re-recorded once it is later cited.
`discharge_validated` therefore **re-points** an already-discharged `near_bound` obligation
when a newer postmortem passes the fork checks and the entity check. It sets `discharged_by`
to the new file and appends the old path to a new `superseded_by_paths: list[str]` field,
defaulting to `[]` so old ledger JSON reads unchanged. Other triggers keep first-discharge-wins.

**Fork checks are run-independent.** They read only `pm.fork` and `pm.discharges`; the
trigger is `any(oid.endswith("_near_bound"))`. So they fire with `run=None`, as at
`obligations.py:277`. `parse_postmortem` (`postmortem.py:109-129`) builds fields explicitly,
so T5 adds `format`, `fork` and `amendments` to it.

**Discharge** therefore needs a format-2 postmortem whose `discharges` names the obligation,
with a valid `[fork]`:

```toml
[fork]
choice = "stop"          # stop | extend | replicate | breakdown | escalate
reason = "internal validation only; not built on"
cited = false
follow_up_run_ids = []
```

Fork errors: missing `[fork]` or empty `reason` while discharging `near_bound`;
`choice = "stop"` with `cited = true`. Format-1 postmortems never discharge `near_bound`.

**Scaffold.** It writes `format = 2`, an uncommented `[fork]` with empty `choice` and `reason`,
and a **commented** checklist of open obligation ids, e.g. `# run_<fullid>_near_bound`. It
never fills `discharges`, per the ledger principle (`postmortem.py:358-361`,
`test_postmortem_campaign_scope.py:140-144`). It resolves a prefix run id to the full id with
`_lookup_run`. `find_run_for_scaffold` (`postmortem.py:397-420`) is left unchanged, so its
2-tuple callers (`mcp.py:3137-3140`, `tests/runlog/test_ac18_catalog_readable.py:183-189`) are
untouched.

**Conclude** (at the D3 insertion point): open `near_bound` obligations are listed for every
campaign, as all obligations are. On a claim-bound confirmation/sequential campaign, the
check runs per member. Run rows come from `_lookup_run`, which covers cool-only members.
Conclude calls `read_runs` **once** and passes the list as `cool_runs`. A member id that does
not resolve (None) on a claim-bound campaign raises `BreakdownRefused(SIDECAR_UNVERIFIABLE)`:
fail closed, matching the D3 resolver's `Unresolvable` rule. It recomputes `near_bound(resolve_run_sidecar(run), results)` from the
run-time sidecar and `runs.metadata`, independent of whether the ledger entry was ever opened.
If the recomputation is non-empty:
- If no `run_<fullid>_near_bound` entry exists (opt-out, swallowed failure, or a pre-feature
  run), it is opened now with `open_obligation`, which bypasses the flag, and the conclude
  raises.
- An open entry raises `BreakdownRefused(FORK_RECORD_MISSING)`. The message names
  `bth postmortem validate <path>`.
- A discharged entry is checked against the postmortem in `Obligation.discharged_by`
  (`obligations.py:137`), resolved against the workspace root and re-parsed. That catches a
  file edited after discharge, and the postmortem is not looked up via `find_postmortem`.
  The conclude raises if that file is missing, unreadable or unparseable, if it lacks a valid
  `[fork]`, or if it has `cited = false`. The message names the `discharged_by` file and
  gives both remedies: edit that file, or validate a new format-2 postmortem, which
  re-points the obligation (Re-discharge).

There is no refusal at add, since refusing there would bias which runs get included.

## 3. Out of scope

Estimand discipline (#2677; keep `unit` reusable), exploratory thresholds (#2945),
auto-computed breakdowns, aggregates in `[differential].metric` and benchmark metrics
(follow-up debt).

## 4. Fixer tasks (one commit each)

| Task | Scope | Tests |
|---|---|---|
| T0 | Probe A9 (WIP snapshot with untracked sidecar) | spike |
| T1 | `BreakdownBlock` parse/validate incl. prefix expansion (`__` excluded, expand-then-check); `[result_schema_meta]` parse/validate; `uncovered_aggregates` (outcomes + `observe.sanity`); effective label; `run_advisories_var` + `_advise` + `run_tool` `warnings`; stderr surfacing at ≈861; `validate_sidecar` warnings; emission check; `_result_schema_complete` exclusion | `tests/test_breakdown_sidecar.py`: lathex names hit (`e_max_placement`, `p_max_iptm`, `max_abs_delta`, `min_dist`) and miss (`n_max`, `is_inf`, `min_samples`, `kendall_tau`, `e_max_placement__n_over`, int columns); prefix `e_max_placement_*` with summary columns declared validates; `capsys` asserts stderr from a real `bth run` in a non-enforced dir; `run_tool` returns `warnings` with `aggregate_uncovered`; exploratory run uses `tentative_outcome` for the emission check; residual run exempt |
| T2 | Lint + STAT-005 + `has_uncovered_aggregate` | `tests/test_breakdown_lint.py`; corpus tests green |
| T3 | `Sidecar.from_declaration` (full) replacing the lossy rebuild | round-trip test against a pre-v3 `run.started` snapshot |
| T4 | Resolver, `breakdown_gate`, `BreakdownRefused` and codes/hints/catch sites, four gate points | `tests/test_breakdown_gate.py`: each gate; unbound accepts; run-time waiver accepts; post-hoc waiver refused; residual run accepted; no `threshold_set` after a refused add; link path never raises and the register backstop catches it; MCP `error_code`; `force_verdict` does not bypass; exploration conclude warns |
| T5 | `ValidationResult.warnings`, `run_lookup` (format-2 only); `parse_postmortem` gains `format`, `fork`, `amendments` | format-1 files unchanged under drift |
| T6 | D5 amendments + rendering | `tests/test_postmortem_amendments.py` |
| T7 | D6 `near_bound` (pure fn, run-end advisory outside the exploratory guard, `TRIGGERS` gains `near_bound` — `open_obligation` rejects unknown triggers, `obligations.py:200` — `DEFAULT_ON` + `resolve_flag(default=)`, fork checks, scaffold) | 78db9e04 fixture (exploration, `bound = 0.5`, 0.521, `n_over = 1`, `n_units = 24`) opens `near_bound` without blocking; `n_units = 1` does not fire single_unit; margin trigger; NaN unevaluated; `bound = 0` validation; default-ON/opt-out tests and the edited closed-set tests (incl. the `TRIGGERS - DEFAULT_ON` comprehension at `test_obligation_triggers.py:110`); scaffold leaves `discharges` empty with a full-id checklist |
| T7a (after T7) | Discharge wiring: `discharge_validated(ws, pm, pm_path, catalog_dir)` from `postmortem_validate_tool`; `"discharged"` in the response; workspace-relative `discharged_by`; near_bound entity check; `_lookup_run` factored and passed as `run_lookup`; conclude recomputation | `tests/test_obligation_discharge_wiring.py`: `bth postmortem validate` discharges; a postmortem for run X cannot discharge `run_Y_near_bound`; legacy triggers still discharge; D5 errors fire through the tool (not warnings); **end-to-end: open near_bound → claim-bound confirmation conclude raises `FORK_RECORD_MISSING` → validate a format-2 `[fork]` postmortem → conclude proceeds**; opt-out run (`near_bound = false`) still raises at claim-bound conclude; moved/deleted discharging postmortem raises; `cited = false` raises; **re-discharge: `cited = false` discharge → conclude raises → new `cited = true` postmortem validated → `discharged_by` re-pointed, old path in `superseded_by_paths` → conclude proceeds**; `query.lookup_run` resolves an 8-char prefix, errors on a genuinely ambiguous one, and does **not** report a compacted run (present in warm and cool) as ambiguous; unresolvable claim-bound member raises `SIDECAR_UNVERIFIABLE`; cool-only member is checked; **warm mode: a cool-only (uncompacted) run resolves via the cool object; log/index mode: a just-finished `analysis_run_id` resolves after `lookup_run`'s one-shot ingest, and with `BTH_NO_INGEST=1` returns None and the validate error names `bth compact`**; prefix with `_`/`%` is matched literally; empty prefix → None; conclude reads `read_runs` once and passes it to `union_campaign_member_ids` and `lookup_run` (assert one scan); exploration-mode conclude warns |
| T8 | Skills (`using-bathos`, `bathos-rigor-gates`; edit both the source and the exported copy), CHANGELOG | — |

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

**Cycle 3**: challenger on v3 (1 blocking, 4 major, 6 minor), then defender (9 conceded, 2
partial, none rebutted). The orchestrator confirmed by grep that `discharge_from_postmortem`
has no `src/` caller, and read the mcp.py:3180 hook, the runner.py:1608 guard and the
campaigns.py:1228 insertion point. v4 changes:
- B1 discharge unwired, so `FORK_RECORD_MISSING` was permanent → T7a wires `discharge_validated` into `postmortem_validate_tool` (CLI + MCP); fork checks are run-independent; conclude reads `discharged_by`; end-to-end test; A16 retagged.
- M2 default-ON unmechanized → `DEFAULT_ON` + `resolve_flag(default=)`; `.bth.toml` opt-out; named test edits.
- M3 stderr invisible over MCP → `run_advisories_var` + `_advise`; `run_tool` returns `warnings`.
- M4 prefix matched its own summary columns → `__` excluded from expansion; expand then check.
- M5 effective label undefined for exploratory runs → `tentative_outcome` counts when `outcome = observed`.
- m6 `waive` placement; m7 `observe.sanity` and a separate `[result_schema_meta]` table with validation; m8 `n_units >= 2`; m9 scaffold never fills `discharges`; m10 exact conclude insertion point outside the swallowing `try`; m11 full-id resolution.
- New (defender): near_bound open sits outside the exploratory guard; discharge entity check.

**Cycle 4**: challenger on v4 (`toolu_01PpRTzv2SY1dbnaLjv6LRH8.1.md`; 0 blocking, 3 major, 5
minor). The orchestrator verified the anchors (`obligations.py:258`, the `runner.py:1632`
swallow, `postmortem_get_tool` passing `run=` at `mcp.py:3274`) and revised without a
separate defender, since every finding was a confirmed gap with no design dispute. v5
changes:
- M1 opt-out or swallowed open let a near-bound run into a claim-bound conclude → conclude recomputes `near_bound` from the resolved sidecar; opt-out is run-time only.
- M2 no catalog handle; verbatim `discharged_by` → `catalog_dir` param; workspace-relative storage; unreadable discharging postmortem raises.
- M3 nothing supplied `run_lookup` → `_lookup_run` factored and passed by both postmortem tools.
- minors: T7 before T7a; `test_obligation_triggers.py:110`; aggregate-half-only at ≈861; cool-only members via `union_campaign_member_ids`; A11 corrected; T8 both skill copies.

**Cycle 5**: challenger on v5 (`toolu_01Sa1fwisGqkWheE3AfbRE95.1.md`; 0 blocking, 2 major, 1
minor). The orchestrator confirmed the anchors: exact-match `mcp.py:3259,3270`; the
`find_run_for_scaffold` 2-tuple at `mcp.py:3140` and in the ac18 test; `discharge()`
first-wins. v6 changes:
- M1 prefix vs exact lookup → `_lookup_run` does unique-prefix matching with an ambiguity error, as the single helper for all lookups.
- M2 already-discharged obligation cannot be re-recorded → near_bound re-discharge with `superseded_by_paths`; the conclude message names both remedies.
- m3 → `find_run_for_scaffold` unchanged; the scaffold resolves via `_lookup_run`.
- Unstated cool-only Run-row source → `_lookup_run` via `read_runs`. `resolve_flag` default is threaded through all three returns. The stale "callers keep passing `run=None`" sentence is corrected.

**Cycle 6**: challenger on v6 (agent_result `toolu_01JVn7aH5FMc3FBhWBjnKpfi`; 0 blocking, 2
major). It confirmed every cycle-5 anchor. The orchestrator read `query.get_run`
(`query.py:357-375`, backend dispatch with `allow_cool_fallback`). v7 changes:
- M1 warm+cool double count → union of full ids deduped before counting; `get_run(..., allow_cool_fallback=True)` for the row.
- M2 no home, no not-found contract, N×scan → public `query.lookup_run` (no core→mcp import); unresolvable claim-bound member fails closed with `SIDECAR_UNVERIFIABLE`; `cool_runs` read once per conclude.

**Cycle 7**: challenger on v7 (0 blocking, 1 major, 1 minor). The cycle-6 union/dedupe and
fail-closed rules held, and the index columns were confirmed (`index.py:117-148`). The
orchestrator had already read `query.py:370-381`, so it revised directly. v8 changes:
- M1 `get_run(allow_cool_fallback=True)` is ignored by the index backend, so a not-yet-ingested cool-only run resolved to None in log mode (and regressed the postmortem tools) → step 4 returns the cool `Run` object itself when the backend row is absent; new prefix queries on index/warm stated; log-mode no-ingest test.
- m `union_campaign_member_ids` scanned `read_runs` separately → it takes the same `cool_runs` list.

**Cycle 8**: challenger on v8 (0 blocking, 1 major, 1 minor). The orchestrator confirmed
that log mode writes events only (`runner.py:1109-1126`) and read `_fold_pending_events`
(`campaigns.py:248-266`). v9 changes:
- M1 the cool-object fallback cannot find log-mode runs (no fragment is written) → it is restricted to the warm and cool backends; the index backend does a one-shot `run_ingest` on a miss; the no-ingest residual is stated, with a `bth compact` hint in the errors; claim-bound conclude stays fail-closed; T7a tests rewritten.
- m LIKE wildcards and empty prefix → escaped, non-empty.

**Cycle 9**: challenger on v9: **converged**, with no blocking or major findings. It
verified the `run_ingest` signature (`ingest.py:982`), the no-op guards (`:1011`), the flock
(`:153-179`), the atomic generation swap (`:1055-1081`), fresh read-only attaches
(`index.py:436-458`) and the empty-schema behaviour on a missing index. v10 polish from its
advisories, with no further cycle needed:
- the read-tool docstring is reworded because the tool can now fold;
- one fold per validation or conclude, via a shared `ingest_state`.
