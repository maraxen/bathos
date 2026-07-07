# Scientific-Validity Gates: Invariants, Differential Assertions, and Sanity Pre-Flight (debt #200)

## 1. Summary

Every gate bathos enforces today checks **process completion** (sidecar exists, is
structurally valid, run finished, output registered) — never whether the **scientific
content** of a result is trustworthy. The motivating incident: Stage 3 of a campaign
passed every existing gate (`n_cells_complete`, `n_nan_cells=0`) while every cell in its
result grid was a silent-bug artifact (`E == F == G`, a caching/seed bug collapsing what
should have been distinct measurements). Nothing in bathos's evaluation path can express
"this specific numeric relationship must hold, and if it doesn't, the run is a hard FAIL
regardless of what the author's own `[outcomes.*]` conditions concluded."

This spec proposes one primary mechanism — a sidecar-level `[invariants]` block reusing
the existing DuckDB-condition machinery — plus two smaller, related extensions. All three
are opt-in (sidecars without them behave exactly as today) and additive to the schema.

| Debt #200 requirement | This spec's answer |
|---|---|
| (1) Invariant assertions, green run + failed invariant = hard FAIL | §2–3: `[invariants]` block |
| (2) Differential/negative-control ("cells that should differ do differ") | §2–3, same mechanism (a same-run comparison is just an invariant condition); §6 sketches an optional cross-run extension |
| (3) `[sanity]` pre-flight block, cheap synthetic-ground-truth check before the real run is trusted | §7 |

## 2. Recon: why this needs new machinery, not a tweak

(Full findings logged to transduction recon `260707_scientific-validity-gates_r1`;
condensed here for spec self-containment.)

- `prereg.py::gate_check()` (pre-execution) — sidecar presence, structural validity,
  first-of-kind, `adversarial_check` *declared* (autonomous mode only). No result data
  exists yet at this point, so it cannot check validity by construction.
- `sidecar.py::evaluate_outcome()` (post-execution) — evaluates `[outcomes.*].condition`
  DuckDB fragments; **first match wins**. Branches are mutually exclusive by design —
  there is no "all of these must additionally hold" primitive.
- `OutcomeSpec.adversarial_check` — parsed, and *required present* in autonomous mode,
  but **never evaluated against the result** (confirmed via grep — `runner.py` only
  derives a presence string `adversarial_check_status: present|missing|n/a`). The
  `new_experiment.py` scaffold shows the original intent (`adversarial_check` as the
  literal negation of the pass condition) matches exactly what requirement (1) wants —
  this is unfinished wiring, not a design gap, and is the closest existing hook.
- `[controls].positive_outcome/negative_outcome` — validated only for label-existence in
  `[outcomes]` (`validate.py`); never evaluated at runtime; pure metadata consumed by the
  `control_arm_rate` sprint-audit signal.
- `linter.py::check_single_cell_gate` (AC-06) — detects when all campaign runs share
  identical **metadata** values. This is a near-miss for requirement (2), but it's
  `bth lint`-only (advisory `WARNING`), it inspects run *metadata* (parameters), not
  *result* values, and it never blocks anything.
- `claim.py` discriminability + Union Gate — real enforcement (downgrades a campaign
  verdict to `confounded`), but scoped to opt-in claim-tier confirmatory/sequential
  campaigns, and about matching *predicted* outcomes to *actual* outcomes per hypothesis
  pair, not generic "these values must differ" assertions.
- `parity.py::compute_grade()` — exact semantic precedent for requirement (1):
  `invariant_pass=False → FAIL` is a hard floor independent of every other evidence
  dimension (cap-lattice grading). Scoped only to `bth campaign attest-parity` runs, and
  `invariant_pass` there is a single self-reported boolean in the result dict, not an
  arbitrary sidecar-declared predicate.
- `[sanity]` / requirement (3) has **zero prior art** — no "sanity" hits anywhere in
  `src/bathos/`. The closest relative is `check_reproduction_prerequisite`, but that
  checks catalog *history* (a different script passed previously), not a live synthetic
  check of this script's own measurement pipeline.

## 3. `[invariants]` Sidecar Block

### 3.1 Schema

New optional top-level block, valid for `[experiment]`, `[benchmark]`, and `[validation]`
sidecars (mirrors where `[outcomes]` is already valid):

```toml
[invariants.cells_differ]
condition = "cell_E != cell_F AND cell_F != cell_G"
reasoning = "Stage 3 regression: a caching/seed bug can silently collapse all sweep cells to one value"

[invariants.jsd_lower_bound]
condition = "jsd_uncond_cond > 1e-6"
reasoning = "Near-zero JSD means the conditional and unconditional distributions collapsed"
```

Each entry has exactly two required fields: `condition` (a DuckDB SQL boolean fragment,
same grammar as `[outcomes.*].condition` — evaluated via the same `_sql_literal`
machinery in `sidecar.py`, so `None`/nested-dict/string result fields already round-trip
correctly per the #478 fix) and `reasoning` (free text, required — an invariant with no
stated reason is a smell, mirrors the existing requirement on `[outcomes.*]`).

`condition` may reference **any** field in the result dict, including ones not declared
in `[result_schema]` — but `validate_sidecar` (§5) will warn (Tier-2) if an invariant
references a field absent from `result_schema`, same as the existing outcome-condition
schema-reference check.

### 3.2 Dataclass

```python
@dataclass
class InvariantSpec:
    condition: str
    reasoning: str = ""
```

```python
@dataclass
class Sidecar:
    ...
    invariants: dict[str, InvariantSpec] = field(default_factory=dict)
```

Parsed in `parse_sidecar()` alongside `_parse_outcomes()` via a new `_parse_invariants(data)`
helper, same shape.

### 3.3 Evaluation semantics

New function in `sidecar.py`, run in `runner.py::run_script()` immediately after
`evaluate_outcome()` succeeds (i.e. only when `exit_code == 0` and a sidecar exists —
invariants add no value on a run that already errored):

```python
def evaluate_invariants(sidecar: Sidecar, result: dict) -> list[str]:
    """Return the list of invariant names whose condition evaluated False (or raised).

    Empty list = all invariants held (or none declared).
    """
```

Reuses the exact `_sql_literal`/`duckdb.execute` pattern from `evaluate_outcome` (extract
that literal-building logic into a shared private helper so the two functions can't drift
the way `_sql_literal`'s bugs did before #478 — see §9, task 1). Unlike `evaluate_outcome`,
**every** declared invariant is checked (no first-match short-circuit) — the return value
is a violations list, not a single label, since a report showing *which* invariant(s)
failed is far more actionable than a single opaque FAIL.

**Hard-fail interaction with `[outcomes.*]`:** if `evaluate_invariants()` returns any
violations, the run's `outcome` column is forced to the new reserved value
`"invariant_fail"` — **regardless** of what `evaluate_outcome()` concluded. The outcome
label the sidecar author's own conditions *would have* produced is preserved for
debugging in `outcome_error_reason` as a JSON payload:

```json
{"violated_invariants": ["cells_differ"], "would_have_been": "pass"}
```

This mirrors the `_gate_failure_payload` pattern already used for `OUTCOME_EVALUATION_ERROR`
in `runner.py` — same shape, new content, no new machinery needed there.

`"invariant_fail"` joins `"error"`/`"unknown"` as a **reserved** outcome label:
`validate_sidecar` must reject a user-declared `[outcomes.invariant_fail]` branch, exactly
as it should already implicitly reject `[outcomes.error]`/`[outcomes.unknown]` (worth
confirming/adding a explicit check for all three reserved names while implementing this,
since I did not find one in `validate.py` today — flagged as a latent gap, not a new one).

**Interaction with POPPER e-values (`compute_evalue`):** `"invariant_fail"` is treated
identically to `"error"`/`"unknown"` — e-value forced to `1.0` (neutral, excluded from
the sequential product). An invariant failure means the *measurement* is untrustworthy,
not that it constitutes evidence against the hypothesis — same philosophy already applied
to `"error"`. One-line addition to the existing tuple check in `compute_evalue`.

**Interaction with claim-tier Union Gate:** no special-casing needed — a run with
`outcome="invariant_fail"` simply won't match any `predicted_outcome` in a discriminability
entry, so it naturally fails to satisfy coverage, which is already the correct behavior
for a run that produced garbage.

### 3.4 Requirement (2), same-run differential/negative-control

A within-run differential assertion ("cells that should differ do differ") is just an
invariant condition that references multiple result fields:

```toml
[invariants.controls_separate]
condition = "positive_control_signal > negative_control_signal + 0.1"
reasoning = "positive and negative control arms must be separated by a clear margin"
```

No separate mechanism is needed for the case in the motivating incident — `E != F AND
F != G` (§3.1) *is* the differential assertion. This is why §3 is framed as satisfying
both requirement (1) and (2) with one primitive.

## 4. Schema Changes

### 4.1 `Run` dataclass / PyArrow schema (`schema.py`)

No new columns required for §3 — `outcome` and `outcome_error_reason` already exist and
already carry exactly the right semantics (reserved label + structured JSON reason,
mirroring the existing `OUTCOME_EVALUATION_ERROR` pattern). This keeps §3 a **zero schema
migration** feature — `CURRENT_SCHEMA_VERSION` stays at `"9"`.

If §6 (cross-run) or §7 (`[sanity]`) are implemented, each needs its own schema bump —
scoped separately below.

### 4.2 Backward compatibility

- Sidecars with no `[invariants]` block: `sidecar.invariants == {}`, `evaluate_invariants()`
  returns `[]` unconditionally, zero behavior change. This must be a fixer's first
  regression test.
- Existing runs already compacted into warm-tier `runs` rows are unaffected —
  `outcome`/`outcome_error_reason` are per-run computed-at-run-time fields, not
  retroactively recomputed by this change.

## 5. Validation Rules (`validate.py`)

New `validate_invariants_block(sidecar, sidecar_path=None) -> list[ValidationError]`,
called from `validate_sidecar` alongside the existing popper/reproduction/controls
validators:

- Each invariant must have non-empty `condition` and `reasoning` (same pattern as
  `[outcomes.*]`).
- `condition` must parse as valid DuckDB SQL against a dummy table built from
  `result_schema` (reuse the exact dummy-table pattern already in `validate_sidecar`
  lines 236–264 — extract to a shared `_validate_sql_condition(condition, result_schema)`
  helper so `[outcomes.*]` and `[invariants.*]` can't drift in how they validate SQL).
- Tier-2 **warning** (not a hard validation error) if an invariant condition references
  no field present in `result_schema` — same rationale as the existing outcome check,
  same severity.
- Reject (hard error) a user-declared `[outcomes.invariant_fail]`, `[outcomes.error]`, or
  `[outcomes.unknown]` branch — reserved-label check noted as a gap in §3.3, closed here.

## 6. Optional extension: cross-run differential (Phase 2, not required for v1)

The motivating incident's `n_cells_complete`/`n_nan_cells` framing suggests the E/F/G
collapse was **within one run's result grid** — §3 covers this. A distinct, weaker
failure mode is cross-run: three separate campaign runs of the same sweep script (e.g.
different config args) all produce identical *output* values, suggesting a fixed-seed or
caching bug across invocations rather than within one. `linter.py::check_single_cell_gate`
(AC-06) already detects this pattern today, but only for run **metadata** (parameters),
only as an advisory `bth lint` warning, never a hard gate.

If wanted, `[controls]` gains one optional field:

```toml
[controls]
positive_outcome = ["pass"]
negative_outcome = ["fail"]
differential_fields = ["swept_cell_value"]
```

`bth campaign conclude` (`campaigns.py::conclude_campaign`) gains a check, run in the
same place as the existing parity-confound check: for each declared
`differential_fields` entry, pull that field's value (parsed from each run's `metadata`
JSON) across every non-`error`/`unknown`/`invariant_fail` run in the campaign sharing the
same `sidecar_path`; if all values are identical, downgrade the verdict exactly the way
the parity-confound check already does (print + `outcome_label = "confounded"`, or a new
`"differential_violated"` label — TBD in planning), respecting the existing
`force_verdict` bypass-with-audit-trail pattern.

This reuses `check_single_cell_gate`'s uniqueness-detection logic (promote it from a
lint-only helper to one also callable from `conclude_campaign`) rather than inventing new
comparison logic. **Recommend deferring this to a follow-up ticket** — it requires a new
outcome label, a schema bump for whatever marks a campaign as "differential-checked", and
campaign-level DDL changes that §3 doesn't need. Flagging it here so the `[invariants]`
naming/reserved-label design in §3 doesn't collide with it later.

## 7. `[sanity]` Pre-Flight Block

### 7.1 Motivation

BATHOS.md's own standing rule: *"When introducing or first using a metric/eval function,
always run a sanity-check on synthetic ground truth before trusting it... These checks
take 30 seconds. They would have caught the SP-0.6 `update_seq` temperature-inversion
bug."* Bathos has no mechanism to enforce or even record that this happened — it's
currently pure human/agent discipline, unlogged and unverified. The skill doc already
documents an informal `--smoke` convention ("Smoke-test validation runs... should be
executed directly, not via `bth run`, so they are not tracked") — `[sanity]` formalizes
exactly this pattern into something bathos actually gates on and records.

### 7.2 Schema

```toml
[sanity]
argv_suffix = ["--sanity-check"]

[sanity.assertions.temperature_sharpens]
condition = "max_prob > 0.9"
reasoning = "temperature=0.01 with one-hot logits must produce a sharp softmax; catches the inverted-temperature-convention bug class"
```

### 7.3 Execution semantics (pre-execution gate, extends `gate_check`)

Before the real subprocess in `run_script()`:

1. If `sidecar.sanity is None` → no-op, identical to today.
2. Otherwise, `bth run` first invokes the **same script** with `argv_suffix` appended
   (e.g. `[..., "--sanity-check"]`) — the script is responsible for interpreting that
   flag and emitting a result via the same `$BTH_RESULTS_PATH` mechanism (§ the #485/#487
   fix already makes this reliable — see `_read_result_emission`).
3. `evaluate_invariants`-style evaluation runs `[sanity.assertions.*]` conditions
   (identical grammar/machinery to §3) against the sanity-run's result dict.
4. Any assertion failing **blocks the real run** — a new pre-execution `GateResult(ok=False)`
   with a new `GateErrorCode.SANITY_CHECK_FAILED`, following the exact pattern of every
   other `gate_check` denial branch (structured payload, `resolution_hint`, `prereg.gate_deny`
   telemetry event).
5. The sanity sub-run is **not** cataloged as its own `Run` row (matching the existing
   "smoke tests aren't tracked" convention) — but its pass/fail and the script SHA it ran
   against should be recorded somewhere queryable, so "was the pipeline sanity-checked
   before this run" is answerable later. Simplest: a new `Run` column,
   `sanity_check_status: str = ""` (`"passed" | "failed" | "n/a"`), set on the **real**
   run's row once the pre-flight completes — mirrors how `adversarial_check_status`
   already records presence-without-a-dedicated-row today.

### 7.4 Caching (avoid doubling wall-clock cost on every run)

Given BATHOS.md's own framing ("these checks take 30 seconds"), the sanity check is
assumed cheap — but re-running it on *every* invocation of a hot-looped script is still
wasted time. Cache keyed on `script_sha256` (already computed in `run_script()` for
every run): skip re-running the sanity sub-process if a prior run against the same
`script_sha256` already recorded `sanity_check_status="passed"` within some TTL (default:
no TTL — script content hasn't changed, so the sanity result is still valid; only a
script edit invalidates the cache, since the hash changes). This is architecturally the
same lookup shape as `check_first_of_kind` — a `SELECT ... FROM runs WHERE script_sha256 = ?
AND sanity_check_status = 'passed' LIMIT 1` warm-then-cool fallback.

### 7.5 Schema changes

- New `Run.sanity_check_status: str = ""` column → PyArrow schema, DDL `ALTER TABLE runs
  ADD COLUMN IF NOT EXISTS sanity_check_status TEXT`, `CURRENT_SCHEMA_VERSION` bump
  `"9" → "10"`, migration function `_migrate_v9_to_v10` (mirrors every prior migration in
  `compact.py`).
- New `GateErrorCode.SANITY_CHECK_FAILED` + `_RESOLUTION_HINTS` entry in `prereg.py`, and
  the mirrored `BathosErrorCode`/`RESOLUTION_HINTS` entry in `errors.py` (the MCP error
  taxonomy — #43's typed-payload work already established this dual-registry pattern).

## 8. CLI/MCP Surface

- No new commands required for §3 — it's inline in the existing `bth run` /
  `mcp__bathos__run` path.
- `bth check --check-outputs`-style visibility: `bth show <run_id>` should render
  `outcome_error_reason`'s `violated_invariants` list when `outcome == "invariant_fail"`
  (small `rich_fmt.py` addition, same place `render_run_detail` already special-cases
  `outcome_error_reason` JSON for `OUTCOME_EVALUATION_ERROR`).
- §7's sanity pre-flight needs no new CLI surface either — it's an automatic pre-flight
  inside `bth run` when `[sanity]` is declared. Consider a `bth run --skip-sanity` escape
  hatch for debugging loops (bypasses the pre-flight, does **not** bypass validity —
  logged at WARNING telemetry, analogous to `--no-sidecar`'s `BYPASSED` logging).

## 9. Linter Changes (`linter.py`)

- New Tier-2 advisory: warn when a sidecar in `scripts/experiments/` or
  `scripts/benchmarks/` has **no** `[invariants]` block at all — mirrors the existing
  `check_adversarial_checks` advisory shape exactly (same file-scan pattern, same
  `LintIssue` construction). Opt-in feature, so this stays advisory forever, not a
  blocking Tier-1.
- New sprint-audit signal (next available slot — Signal 14, following 13/parity):
  `invariant_fail_rate` — fraction of runs where `outcome == "invariant_fail"`. This is
  the feature's own value-tracking metric: a non-zero rate means the mechanism is
  actually catching real bugs; a persistently-zero rate across many declared invariants
  is itself a signal worth surfacing (either the invariants are too weak, or genuinely
  nothing has broken yet).

## 10. Open Questions for Planning

1. Exact reserved label spelling: `"invariant_fail"` vs `"invariant_failed"` vs
   `"invariant_violated"` — pick one, grep for hardcoded outcome-string comparisons
   across `campaigns.py`/`linter.py`/`rich_fmt.py` that assume the closed set
   `{pass, marginal, fail, error, unknown, ""}` before landing (there are several — e.g.
   `campaigns.py`'s `is_neutral_outcome = run_outcome in ("error", "unknown", None, "")`
   at line 115 — decide whether `invariant_fail` belongs in that neutral set too; §3.3
   says yes, for e-value purposes).
2. Should `[invariants]` be allowed to reference `[reproduction]`/`[controls]`-declared
   labels, or only raw `result_schema` fields? (Recommend: raw fields only, v1 — keep the
   condition grammar identical to `[outcomes.*].condition`, no new cross-referencing.)
3. §6 (cross-run) and §7 (`[sanity]`) are independent of each other and of §3 — they can
   ship as separate PRs/sprints. Confirm priority order with the user before scoping a
   plan (recommend: §3 first, alone, since it's zero-schema-migration and directly
   answers the debt's core complaint; §7 next since it has no prior art and highest
   novel-risk; §6 last, as an explicit follow-up ticket).
4. `argv_suffix` convention in §7.2 assumes every script accepts an extra CLI flag and
   knows how to respond to it — this is a **script-author contract**, not something
   bathos can enforce structurally. Worth an explicit skill-doc convention (a documented
   `--sanity-check` argparse pattern, analogous to the existing `--smoke` convention)
   rather than leaving `argv_suffix` fully freeform.

## 11. Acceptance Criteria (v1 scope = §3 only)

- A sidecar with `[invariants.X]` whose condition evaluates `False` against an otherwise
  passing run's result produces `outcome == "invariant_fail"`, not `"pass"`.
- `outcome_error_reason` contains `{"violated_invariants": [...], "would_have_been": "<label>"}`
  as valid JSON.
- A sidecar with no `[invariants]` block is byte-for-byte unaffected (regression test:
  existing `test_evaluate_outcome_*` tests all still pass unmodified).
- `compute_evalue()` returns `1.0` for `outcome == "invariant_fail"`, verified with a
  synthetic-ground-truth test per BATHOS.md's own rule (feed a sequential campaign a
  known e-value scenario, confirm the invariant-failed run doesn't move the product).
- `validate_sidecar` rejects a sidecar declaring `[outcomes.invariant_fail]`.
- `bth lint` warns (Tier-2) on an enforced-dir sidecar with zero `[invariants]` declared.

## 12. Implementation Task Breakdown (§3 v1 scope)

1. `sidecar.py` — extract shared `_sql_literal`/dummy-table-building helpers so
   `[outcomes.*]` and `[invariants.*]` evaluation/validation can't independently drift
   the way #478 showed they already can; add `InvariantSpec`, `Sidecar.invariants`,
   `_parse_invariants()`, `evaluate_invariants()`.
2. `runner.py` — call `evaluate_invariants()` after a successful `evaluate_outcome()`;
   override `outcome`/`outcome_error_reason` on violation per §3.3.
3. `validate.py` — `validate_invariants_block()`; reserved-label rejection for
   `invariant_fail`/`error`/`unknown` as declared `[outcomes.*]` branches.
4. `campaigns.py` — one-line addition to `compute_evalue`'s neutral-outcome check (or
   confirm it already lives in `sidecar.py` — verify exact current location before
   editing) and to `is_neutral_outcome` in `add_run_to_campaign`.
5. `linter.py` — new Tier-2 `check_invariants_declared` (mirrors
   `check_adversarial_checks`).
6. `sprint_audit.py` — Signal 14 `invariant_fail_rate`.
7. `rich_fmt.py` — render `violated_invariants` in `render_run_detail` when present.
8. `new_experiment.py` — add a commented `[invariants]` example to the scaffold
   template, same treatment as the existing commented `[reproduction]` block.
9. Tests: parse/validate/evaluate unit tests (mirror the `test_sidecar.py` structure
   used for `[popper]`/`[reproduction]`/`[controls]`), one `test_runner.py` end-to-end
   (sidecar with a failing invariant on an otherwise-passing script → `outcome ==
   "invariant_fail"`), one POPPER e-value neutrality test.
10. `agent_assets/using_bathos/SKILL.md` — document `[invariants]` alongside the
    existing `[outcomes]`/`[controls]` sections, including the reserved-label list.
