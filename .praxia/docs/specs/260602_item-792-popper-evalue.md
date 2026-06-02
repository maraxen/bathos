# Design: POPPER e-value Multi-Run Campaign Primitive (Backlog #792)

**Date:** 2026-06-02
**Track:** Design (exploratory — no implementation scope yet)
**Task ID:** 260602_bathos-v08-sprint
**Status:** Open for review

> **Cross-cutting note (oracle, 260602):** POPPER campaigns require per-run output artifact provenance for the e-value audit trail. The expected CLI surface is `bth outputs list <run_id>` (from backlog #791 results management). Do not design POPPER storage in isolation — coordinate with #791 before either ships. Specifically: `bth outputs prune` must not be implemented until POPPER's storage model is locked.

---

## 1. Motivation

The current bathos campaign primitive is a bookkeeping construct. A `Campaign` records which runs are associated with a named investigation and supports two modes (`exploration`, `confirmation`), but it accumulates no statistical state. `review_campaign()` reports descriptive rates — residual fraction, bypass fraction, outcome distribution — but cannot answer the question: *"is there sufficient sequential evidence to reject the null hypothesis?"*

POPPER (arXiv 2502.09858) formalizes a falsification-first protocol for agentic science using **e-values**: per-run test statistics whose product across sequential runs is a valid test of a pre-specified null hypothesis. When the product crosses a threshold, the hypothesis has been statistically falsified (or confirmed, depending on direction). The key property is that e-values remain valid under sequential stopping — the researcher can stop as soon as evidence is sufficient without inflating Type-I error, unlike p-values which require a fixed sample size.

The research synthesis (`.praxia/docs/research/260526_agentic-science-nlm-synthesis.md`, §2.6) identified this as a natural bathos extension: transform `bth campaign` from a bookkeeping construct into a statistical validity accumulator. This document explores that design space.

---

## 2. What an E-value Is (and Isn't)

An **e-value** for a single observation is a non-negative real number `e` such that `E[e] <= 1` under the null hypothesis. A sequence of e-values `e_1, e_2, ..., e_n` from independent experiments has the property that their product `E_n = prod(e_i)` is a valid sequential test statistic: `Pr(E_n >= 1/alpha) <= alpha` for any stopping time and any `n`.

Concretely: if the null is "this intervention has no effect" and each run produces `e_i = likelihood_ratio(observed | H1) / likelihood_ratio(observed | H0)`, then `E_n = 100` means you have 100x evidence against the null — strong enough to reject at `alpha = 0.01`.

What makes this appealing for bathos:
- **Anytime-valid**: you can stop whenever `E_n >= threshold`, not just after a fixed number of runs
- **Multiplicative composition**: each new run updates the product with one scalar multiplication — no re-analysis required
- **Interpretable**: "I have E = 32x evidence" is more meaningful than "p = 0.031"
- **Error-tolerant (by design)**: errored runs contribute `e = 1` (no information), leaving the product unchanged

What it requires that bathos currently lacks:
- A specified **null model** (at minimum: a base rate for the outcome label under H0)
- A mapping from run results to an **e-value scalar**
- Storage of the **running product** and iteration count across campaign runs

---

## 3. Current State

### 3.1 Campaign model

`Campaign` dataclass (`campaigns.py`) holds: `id`, `project_slug`, `name`, `mode`, `question`, `hypothesis`, `status`, `outcome_label`, `conclusion`, `parent_campaign_id`. No statistical fields.

`campaigns` DB table: same fields, no `evalue_product`, `stopping_threshold`, or sequential state.

`campaign_runs` join table: `(campaign_id, run_id)` only. No per-run weight, e-value contribution, or sequence position.

### 3.2 Sidecar model

`SidecarKind` enum: `EXPERIMENT | BENCHMARK | VALIDATION | DEBUG`. No POPPER kind.

`Sidecar` dataclass: outcome conditions are DuckDB SQL strings returning a label. `evaluate_outcome()` returns a label string, not a numeric test statistic.

### 3.3 Outcome evaluation

`evaluate_outcome()` in `sidecar.py` evaluates SQL conditions and returns the first matching label. There is no path from "run outcome is `pass`" to "e-value contribution for this run is X".

### 3.4 Gate and audit

`prereg.py` enforces the pre-registration gate per-run. `sprint_audit.py` computes seven rigor signals across all runs. Neither module has any concept of sequential accumulation or stopping rules.

---

## 4. Design Space

### 4.1 Where does the null model live?

The most consequential design question. E-value computation requires knowing the probability of the observed outcome under H0. Possible placements:

**Option A: In the campaign itself** — the campaign declaration includes `null_base_rate: float` specifying the expected pass rate under the null. Simple, but requires the researcher to specify this when creating the campaign, which may be unknown.

**Option B: In the sidecar** — a `[popper]` section in the sidecar declares the null model. This is more expressive (can include per-outcome weights) but complicates the sidecar's role, which is currently script-level rather than campaign-level. Multiple scripts may contribute to one campaign with potentially different null models.

**Option C: Derived from prior runs** — the null rate is computed from historical runs on the same script before the campaign started. This is automatic but circular if early runs are already part of the campaign, and brittle if the historical base is small.

**Option D: Fixed conventional rate** — `null_base_rate = 0.5` by default (fair coin under H0), with the researcher overriding per campaign. Pragmatic but potentially misleading if 0.5 is not a sensible null for the domain.

The choice also determines whether e-value computation is symmetric (testing pass vs. fail) or directional (testing a specific outcome label).

### 4.2 E-value formula per run

Given an observed outcome label (e.g., `pass`) and a null base rate `p_0`, the simplest e-value is the **likelihood ratio**:

```
e_i = p_1(outcome_i) / p_0(outcome_i)
```

where `p_1` is the probability under H1 (the researcher's model). For a binary pass/fail with `p_0 = 0.5` and `p_1 = 0.8` (the researcher believes the intervention works):

- Run passes: `e_i = 0.8 / 0.5 = 1.6`
- Run fails: `e_i = 0.2 / 0.5 = 0.4`
- Run errors: `e_i = 1.0` (neutral, no information)

This requires the researcher to specify both `p_0` (null rate) and `p_1` (alternative rate), which may feel onerous. Alternatives:

**Betting score / Kelly e-value**: the researcher chooses a "bet" `lambda` per run and the e-value is `1 + lambda * (outcome - p_0)`. Simpler to specify but less interpretable.

**Universal inference e-value**: derived from the running empirical proportion, no H1 needed. More robust but converges more slowly and is harder to explain to a non-statistician.

**Label-based e-value from sidecar outcomes**: `pass` maps to some e > 1, `fail` maps to 1/e, `marginal` maps to 1, `error` maps to 1. Requires the researcher to declare the e-value weight per outcome label in the sidecar. The most bathos-idiomatic approach since it extends existing sidecar structure.

### 4.3 Where does per-run e-value get stored?

Per-run e-values need to be attributable to a specific campaign, since the same run might join multiple campaigns with different null models. Options:

**Option A: `campaign_runs` table** — add `evalue` REAL column to the join table. The product is computed on query. Clean separation: the run record is immutable, the campaign membership record carries the statistical weight.

**Option B: Cool-tier run record** — add `evalue_contribution` to the `Run` dataclass and cool schema. Simpler tooling path (one write), but the e-value is campaign-specific, so one run contributing to two POPPER campaigns with different null models would need two records or a JSON blob.

**Option C: Separate `evalue_log` table** — `(campaign_id, run_id, seq_position, evalue, evalue_product_at_step)`. Append-only, stores full sequence history. Most auditable but adds a third table and makes `compact.py` more complex.

### 4.4 Stopping threshold

The threshold `T` for `E_n >= T` is equivalent to testing at `alpha = 1/T`. Common choices: `T = 20` (alpha = 0.05), `T = 100` (alpha = 0.01). The question is whether bathos should:

- Accept `stopping_threshold` as a campaign parameter
- Enforce a minimum threshold (e.g., `T >= 10`) to prevent low-power stopping
- Warn but not block when threshold is below a floor

This connects to the existing threshold calibration concern (backlog #143): hardcoding a minimum threshold imports unjustified epistemic commitments. A sensible default with a lint warning is less prescriptive than a hard gate.

### 4.5 Campaign mode: new mode vs. extension

**Option A: New mode `"sequential"`** — add a third mode to the existing `mode` field. Requires gating all e-value accumulation logic on `mode == "sequential"`. Backward-compatible: existing `exploration` / `confirmation` campaigns are unaffected.

**Option B: Campaign-level `protocol` field** — add `protocol: str | None` to `Campaign`, where `protocol = "popper"` activates e-value accumulation. The existing `mode` field retains its meaning. More expressive but adds a second classification axis.

**Option C: Separate `EvalueCampaign` subtype** — a new class that inherits from or wraps `Campaign` with statistical fields. Cleanest from a type standpoint but adds indirection and may not be worth it for what is currently one protocol.

### 4.6 Sidecar changes: new kind vs. extension section

The `SidecarKind` enum currently drives parsing in `parse_sidecar()`. Two approaches:

**Option A: New kind `POPPER`** — add `SidecarKind.POPPER` with a `[popper]` top-level section replacing `[experiment]`. The sidecar becomes the single source of truth for null model, alternative model, and per-outcome e-value weights. Requires a new parsing branch and validate.py update.

**Option B: `[popper]` sub-section inside `[experiment]`** — the sidecar is still `EXPERIMENT` kind but carries an optional `[popper]` block. This is more backward-compatible: existing sidecars work unchanged; adding `[popper]` opts into sequential accumulation. The gate checks for presence of `[popper]` before activating e-value logic.

Option B seems lower-risk. A `[popper]` block might look like:

```toml
[experiment]
hypothesis = "..."

[popper]
null_pass_rate = 0.5
alt_pass_rate = 0.8
stopping_threshold = 20.0

# per-outcome e-value weights (optional, overrides derived likelihood ratio):
[popper.weights]
pass = 1.6
fail = 0.4
marginal = 1.0
error = 1.0
```

### 4.7 Error-outcome handling

The research synthesis (section 1.2, arXiv 2511.06701 "Research monad") is clear: `outcome="error"` contributes factor 1.0 (neutral, no evidence). This avoids corrupting the product with partial data. The design question is whether to make this a hard rule:

- **Hard rule**: `error` maps to e = 1.0, always. The researcher cannot override. Safest.
- **Configurable**: the `[popper.weights]` block can override error weight. Flexible, but allowing e > 1 for error runs would be statistically unsound.
- **Excluded from product**: error runs do not appear in the sequence at all, as if they never ran. Has the same mathematical effect as e = 1 but different semantics: the sequence count `n` does not increment, which matters if reporting "based on N successful runs."

There is also the question of what "error" means here: `outcome="error"` (first-class since v0.6) vs. non-zero exit code with partial results vs. gate denial. These should likely all map to e = 1.0, but the definition should be explicit.

### 4.8 Adversarial check interaction

The current adversarial check is a per-run gate: in autonomous mode, a `pass` outcome without a matching adversarial check is denied. Inside a POPPER campaign, the adversarial check serves a different role: it is the mechanism by which the run itself is subjected to falsification pressure. Two design options:

**Option A: Unchanged** — adversarial check continues to operate per-run at the gate level. POPPER campaigns do not change the per-run check behavior. The researcher is responsible for ensuring that runs in a POPPER campaign include well-designed adversarial checks.

**Option B: Campaign-level adversarial requirement** — a POPPER campaign requires that all member runs have adversarial checks. The `add_run_to_campaign()` function enforces this when the campaign is in sequential mode.

Option B is stricter and connects to the POPPER paper's falsification-first mandate, but it adds gate complexity and may create friction for exploratory POPPER campaigns that start before adversarial checks are designed.

### 4.9 Sprint audit integration

`sprint_audit.py` currently has no signal for sequential test abuse. Candidate signals:

- **evalue_product_at_conclusion**: for concluded POPPER campaigns, what was the final `E_n`? Low values (below threshold at conclusion) would flag premature stopping.
- **early_stopping_rate**: campaigns concluded before reaching the pre-specified threshold. Should be flagged as potentially opportunistic.
- **post_hoc_threshold_change**: if the threshold was changed after runs were added (requires versioning the threshold, or at minimum recording it at campaign creation time).
- **evalue_sequence_monotonicity**: whether `E_n` was monotonically increasing (consistent with H1) or fluctuating (suggesting H1 was mis-specified).

The most important signal is probably **premature stopping** — a campaign concluded with `E_n < stopping_threshold`. This is the sequential equivalent of p-hacking: stopping as soon as any trend is visible rather than as soon as the pre-specified threshold is crossed.

---

## 5. Key Decisions

The following decisions need to be made before this can be specced for implementation:

1. **Null model placement**: campaign field vs. sidecar `[popper]` block vs. derived from priors. This determines whether the null model is per-campaign or per-script-hypothesis.

2. **E-value formula**: label-based weights declared in sidecar vs. likelihood ratio from null/alternative rates vs. betting score. The choice affects both expressiveness and the cognitive burden on the researcher.

3. **Error-outcome treatment**: hard rule (e=1), configurable weight, or excluded-from-sequence. Should be a hard rule for statistical soundness, but the exact mechanism (especially for gate-denied runs) needs definition.

4. **Storage for per-run e-values**: `campaign_runs` table extension vs. separate `evalue_log` table vs. cool-tier run record. The `campaign_runs` extension is simplest; the `evalue_log` is most auditable.

5. **Sidecar surface**: new `SidecarKind.POPPER` vs. optional `[popper]` sub-section within `[experiment]`. The sub-section approach is more backward-compatible.

6. **Campaign mode surface**: new `mode="sequential"` vs. `protocol="popper"` field vs. new `EvalueCampaign` class.

7. **Adversarial check enforcement in POPPER campaigns**: unchanged per-run behavior vs. campaign-level enforcement that all member runs include adversarial checks.

8. **Sprint audit signals for sequential abuse**: which signals to add, and whether premature stopping should be a hard anomaly vs. a calibration-target warning (consistent with the existing threshold rationale ADR).

---

## 6. Open Questions

These are questions that cannot be resolved through design discussion alone — they require empirical investigation, literature review, or user input:

**Q1: What is a sensible default null base rate for a computational science experiment?**
The choice of `p_0 = 0.5` is conventional but arbitrary. For a typical bathos experiment (testing a hypothesis about a physical simulation or ML model), the natural null might be "no better than a baseline method" — which may have a very different base rate. Is there a principled default, or should `null_pass_rate` be required (no default)?

**Q2: How does the POPPER protocol interact with multi-script campaigns?**
The current campaign primitive supports adding runs from different scripts. In a POPPER campaign, each script may have its own hypothesis and null model. Does the e-value product aggregate across all scripts (treating the campaign as a single test), or is the e-value computed per-script and the campaign concludes when all per-script products cross their thresholds? This determines whether the "campaign" is the unit of inference or a container for per-script sequential tests.

**Q3: Is the POPPER protocol appropriate for experiments with continuous result metrics?**
The design above assumes label-based outcomes (pass/fail/marginal). The POPPER paper uses e-values derived from continuous test statistics (likelihood ratios from distributions). For experiments where `result_schema` contains continuous metrics (e.g., `temp_std`, `ns_per_day`), could the e-value be derived directly from the metric distribution rather than the label? This would be more statistically powerful but requires specifying a parametric model for the metric under H0 and H1.

**Q4: What UI surface is appropriate for declaring a POPPER campaign?**
`bth campaign create` currently takes `--mode exploration|confirmation --question X --hypothesis Y`. For a POPPER campaign, additional parameters are needed (`--null-pass-rate`, `--alt-pass-rate`, `--stopping-threshold`). Is this the right surface, or should POPPER campaigns be declared entirely via the sidecar `[popper]` block with `bth campaign create` remaining unchanged?

**Q5: Should the stopping threshold be immutable after campaign creation?**
If the threshold can be changed after runs are added, the sequential test loses its validity guarantee. The design should likely record the threshold at campaign creation time and lock it (refusing updates once runs are added). But this raises UX questions: what if the researcher realizes the threshold is wrong after the first run? Requiring a new campaign to be created (with `parent_campaign_id` linking to the abandoned one) is auditable but inconvenient.

**Q6: How should POPPER campaigns appear in `bth view` and `bth export --html`?**
The existing `render_campaign_table` in `rich_fmt.py` and HTML templates show only descriptive statistics. A POPPER campaign needs a sequential accumulation plot (E_n vs. run sequence number) and a clear visual indicator of the stopping threshold. This is a non-trivial viz addition that may belong in a separate design pass.

**Q7: What is the correct behavior when a run is removed from a POPPER campaign?**
The current `campaign_runs` table is insert-only (no delete). If a run is removed (e.g., discovered to be invalid), the e-value product must be recomputed. Should runs ever be removable from POPPER campaigns? If not, invalid runs should be excluded via `outcome="error"` (which contributes e=1) rather than deletion.

**Q8: Should e-value accumulation happen at `add_run_to_campaign()` time or lazily on query?**
Eager accumulation (updating `evalue_product` in the campaigns table on each `add_run_to_campaign()` call) is simple but requires transactional consistency with the join table. Lazy accumulation (computing the product on query from `campaign_runs`) is more flexible (allows recomputation if null model changes) but adds query-time cost. For large campaigns (100+ runs), both are fast, so the question is more about consistency guarantees.

---

## 7. Relationship to Existing Features

| Existing feature | Interaction |
|---|---|
| `bth campaign create/conclude` | Needs new flags for sequential mode; conclude logic needs threshold check |
| `campaign_runs` join table | Needs `evalue` column or new `evalue_log` table |
| `sidecar.py:evaluate_outcome()` | Returns label; needs companion function returning e-value scalar |
| `prereg.py:gate_check()` | Per-run gate unaffected; campaign-level enforcement is new |
| `sprint_audit.py` | New signals needed for sequential test validity |
| `compact.py` DDL | New columns in campaigns or new evalue_log table require DDL changes + migration |
| `migrate.py` | Schema version bump needed; migration path for existing campaigns to `evalue_product=NULL` |
| `rich_fmt.py` | `render_campaign_review()` needs sequential plot for POPPER campaigns |
| MCP tools (`mcp__bathos__campaign_*`) | `campaign_review` return value needs e-value fields |
| `validate.py` | New `[popper]` section validation needed |

---

## 8. Scope Boundary

This feature is marked **exploratory** in the backlog. It is not a prerequisite for any other v0.8 item. The design document is intended to surface the decision surface for the user to review.

Before implementation can begin, the user needs to resolve at minimum: Q1 (null model defaults), Q2 (multi-script campaign semantics), and the Key Decisions in section 5 (null model placement, e-value formula, storage structure). The remaining open questions (Q3-Q8) can be deferred to the implementation spec.

---

## 9. References

- POPPER: Automated Hypothesis Validation with Agentic Sequential Falsifications — arXiv 2502.09858
- Structural Enforcement of Statistical Rigor in AI-Driven Discovery (Research monad) — arXiv 2511.06701
- Sound Agentic Science Requires Adversarial Experiments — arXiv 2604.22080
- Agentic Science Research Synthesis — `.praxia/docs/research/260526_agentic-science-nlm-synthesis.md`, section 2.6
- ADR for sprint-audit threshold rationale — `.praxia/docs/decisions/260601_sprint-audit-threshold-rationale.md`
