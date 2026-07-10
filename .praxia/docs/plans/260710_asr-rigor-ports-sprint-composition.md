# Sprint composition — port asr's C1–C5 rigor mechanisms into bathos core

**Date:** 2026-07-10 · **Origin:** asr project retrospective (2026-07-09) forward discipline C1–C5
**Author-context:** asr shipped all five as asr-local MVPs (2026-07-10); this composition ports the
generic ones upstream. asr-side references are absolute paths into `~/projects/asr`.

## Why this exists

The asr retrospective diagnosed one dominant failure class — **trusted-broken-pipeline**: a
generator/baseline/metric ran hundreds of times (GW: 483×/~37 days) on corrupted ground truth
before its *invalidity* (not its result) was caught. It issued five forward commitments (C1–C5)
and asr built each as a local mechanism first. Three are generic experiment-tracking discipline
that belong in bathos core; one needs a schema field first; one is genuinely project-coupled and
stays in asr.

The dividing test: **does the mechanism operate on bathos-owned data (runs/campaigns/claims) or on
project-owned data (a backlog DAG, a project's verdict files)?** The former port; the latter don't.

| asr commitment | bathos home | this composition |
|---|---|---|
| C3 concentration alarm | `linter.py` (catalog-backed lint) | **BP-1** (do first — smallest, zero coupling) |
| C1 pre-run invariant gate | `claim.py` confounds + `parity.py` submit-gate | **BP-2** (anchor, highest leverage) |
| C5 negative-claim check | `claim.py:validate_claim` | **BP-3** |
| C2 invalidity-latency | `schema.py` (needs a new column first) | **Appendix A** (schema sprint, then a query) |
| C4 dead-arm auto-cut | praxia backlog / asr verdicts — **NOT bathos** | **Appendix B** (stays out) |

asr-side reference implementations (the MVPs these ports generalize):
`~/projects/asr/scripts/gates/{catalog_hygiene,claim_hygiene,c1_invariant_gate}.py`,
`~/projects/asr/.praxia/docs/decisions/260710_c{1,2c3,4c5}-*.md`.

---

## BP-1 — C3 concentration alarm as a catalog-backed lint rule

**Goal.** Flag any campaign OR script that has accumulated more than N runs whose outcome is still
unrecorded — silent accumulation of unvalidated work — as a `bth lint` finding.

**Why bathos, not asr.** Pure catalog hygiene on bathos's *own* outcome vocabulary; zero
project-specific knowledge. asr's `catalog_hygiene.py concentration` is just SQL over the run
catalog.

**Entry point.** `src/bathos/linter.py`. It already has the exact shape: catalog-backed checks
returning `list[LintIssue]` —
- `check_unfired_branches(catalog_dir, min_runs=5)` (WARNING if all runs share one outcome),
- `check_residual_rates(catalog_dir, threshold=0.10)`,
- `check_bypass_trend(catalog_dir)`.

**Change.**
1. Add `check_run_concentration(catalog_dir: Path, threshold: int = 20) -> list[LintIssue]`
   mirroring `check_unfired_branches`. Two aggregations over the runs table:
   - per `campaign_id` (COALESCE empty → an `<uncampaigned>` bucket),
   - per `script_sha256` (with a `command` label),
   counting `outcome IS NULL OR trim(outcome) IN ('', 'unknown', 'none')`. Emit one
   `LintIssue(severity=WARNING, issue="run-concentration", detail="<id>: N/total unvalidated")`
   per bucket with count strictly `> threshold`.
2. Wire it into the catalog-backed lint path (same call site as `check_residual_rates`), gated on a
   catalog being present. Expose `--concentration-threshold` on `bth lint` (default 20).

**Acceptance.**
- New unit test `test_check_run_concentration` with a synthetic in-memory catalog: a bucket of
  N+1 unvalidated runs → one WARNING; a bucket of N → none (strict `>`); a bucket of N+1 runs all
  bearing a real outcome → none (predicate correctness).
- Regression against a real asr catalog fixture: flags the known runaways (481/166/132 …), spares
  the small all-outcome campaigns. (asr's `test_catalog_hygiene.py` is the reference oracle.)

**Dogfood.** The check is itself catalog-analysis logic → its known-answer test is the invariant.

---

## BP-2 — C1 pre-run synthetic-invariant gate as `[confounds.synthetic_recovery]`

**Goal.** A claim/campaign that exercises a pipeline component cannot be validated/submitted unless
that component's known-answer synthetic-recovery invariant test is registered and green — the
enforcement the retro said was missing ("BATHOS.md states the rule; the gap is enforcement").

**Why bathos.** The *mechanism* (bind a component → invariant test, gate commitment on it) is
generic; only the *registry contents* are per-project (they stay in asr's
`.praxia/c1_invariant_gates.toml`). This is the direct analog of the existing reference-parity
confound.

**Entry points (templates).**
- `src/bathos/claim.py` — `validate_claim()` **AC-13** already validates
  `[confounds.reference_parity]` sub-blocks: `for confound in claim.confounds: ref_par =
  confound.get("reference_parity", {})` → require a catalog run establishing control.
- `src/bathos/parity.py` — `check_parity_confounds_for_submit(sidecar, catalog_dir)` is the
  **F3 submit-gate**, keyed on `sidecar.reproduction.requires_parity_stem`.

**Change.**
1. Schema: define a `[confounds.synthetic_recovery]` sub-block — `{invariant_test, guards,
   green_sha}` (component→known-answer test + guarded sources + the sha it was last green at).
2. `claim.py` — add **AC-14** mirroring AC-13: for each `confound.get("synthetic_recovery", {})`,
   assert the invariant test is registered and its recorded green is fresh (guarded sources
   unchanged since `green_sha` — a git-diff check, exactly asr's `guards_changed_since`).
3. `parity.py` (or a sibling `synthetic_recovery.py`) — add
   `check_synthetic_recovery_confounds_for_submit(sidecar, ...)` templated on the parity submit
   gate: block submit/campaign-create if a declared component's invariant is not green-fresh.
4. Fail-closed: unknown component / no recorded green → block (never a vacuous pass), matching
   asr's exit-3 semantics.

**Acceptance.**
- `validate_claim` unit tests: synthetic_recovery block with a green-fresh invariant → pass;
  stale (guard changed since green_sha) → error; missing/unregistered → error.
- Submit-gate test: a sidecar declaring `requires_synthetic_recovery` is blocked when the invariant
  is not green; allowed when it is.
- Cross-check parity with asr's `test_c1_invariant_gate.py` state machine
  (UNKNOWN/RED/GREEN/STALE) — same decision boundary.

**Migration.** asr keeps its `.praxia/c1_invariant_gates.toml` registry + `just c1-campaign`; once
BP-2 lands, `just c1-campaign` delegates to the bathos confound gate instead of the local runner.

---

## BP-3 — C5 negative-claim falsification check in `validate_claim`

**Goal.** A claim that asserts a strong *negative* (fail / void / null / "dead end") must carry the
same quantitative/synthetic backing it would demand of a positive, or be explicitly hedged —
catching the over-call-a-negative-from-partial-evidence lapse.

**Why bathos.** Bathos already owns claims and their validation (`bth claim validate` does
"structural + optional catalog-backed checks"); this is one more structural rule. (asr's version
lints its own `.praxia/verdicts/*.toml`; the bathos-native form operates on claim files, and asr
verdicts can later migrate onto claims.)

**Entry point.** `src/bathos/claim.py:validate_claim()` — same AC-list the other checks live in.
The `discriminability` entries already carry `predicted_outcome`; the confounds already carry
evidence blocks.

**Change.**
1. Add **AC-15**: if a claim's declared outcome (or a discriminability `predicted_outcome`) is
   negative (`fail|void|no-go|null|neutral|refuted`), require at least one of: a confound/evidence
   block with a quantitative marker (gate result, CI, bootstrap, self-check), OR an explicit
   `hedged = true` / `hypothesis = true` field. Otherwise emit a `ValidationError`
   ("strong negative asserted with no falsification backing and no hedge").
2. Reuse asr's marker heuristic (`~/projects/asr/scripts/gates/claim_hygiene.py:has_backing`) as
   the starting recognizer set; refine to claim-schema field names.

**Acceptance.**
- `validate_claim` tests: negative claim + quantitative confound → pass; negative + `hedged=true`
  → pass; bare negative → error; positive claim → unaffected.

**Caveat (document in the rule).** Backing detection is *presence*, not *adequacy* — a pass means
"evidence exists to review," not "the negative is correct." Same limitation asr documented.

---

## Appendix A — C2 invalidity-latency (needs a schema field first)

C2's KPI (span first-run → invalidity-caught; GW = 37d) is **not inferable from run timestamps** —
a pipeline's runs cluster in hours but invalidity is caught days-to-weeks later, often in another
campaign. asr curates it in a local `invalidity_events.toml` ledger. The bathos-native form needs a
**first-class invalidity marker on the run/campaign schema** before it can become a query:

1. **Schema sprint (prerequisite).** Add an invalidity marker to `src/bathos/schema.py` Run —
   e.g. `invalidated_at: str | None` + `invalidated_component: str | None`, exactly as
   `parity_run_type` was added as a first-class v9 column (see the `pa.field(..., nullable=True)`
   + `from_arrow_row` precedent). Bump schema version + migration.
2. **Then** the KPI is a `bth` query (per-component `min(timestamp) → invalidated_at`), and the
   automatic outcome-lag *proxy* (asr already ships it as pure catalog SQL) ports immediately with
   no schema change. Port the proxy in BP-1's slipstream; defer the true KPI to after the schema
   sprint.

## Appendix B — C4 dead-arm auto-cut (stays out of bathos)

C4 reads asr's verdict files and the **praxia backlog DAG** — neither is bathos-owned data. If it
is ever generalized it belongs in **praxia backlog tooling** ("cut open items depending on a NO-GO
verdict"), not bathos. Recommendation: leave asr's `claim_hygiene.py dead-arms` as-is; do not port.

---

## Suggested sequence & sizing

1. **BP-1** (C3 lint) — smallest, self-contained, immediate cross-project value. ~½ day.
2. **BP-2** (C1 synthetic_recovery confound) — the anchor; touches claim schema + submit gate. ~1–2 days.
3. **BP-3** (C5 negative-claim AC) — small, rides BP-2's claim-validation familiarity. ~½ day.
4. **Appendix A schema sprint** — only if the invalidity-latency KPI is wanted natively; otherwise
   port just the proxy query with BP-1.

Backlog: file BP-1/BP-2/BP-3 as bathos infrastructure items; link back to this composition and to
the asr decision docs `260710_c{1,2c3,4c5}-*.md`.
