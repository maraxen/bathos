# ADR: Sprint-Audit Signal Threshold Rationale

**Date:** 2026-06-01
**Status:** Accepted
**task_id:** 260601_v061_S2A
**Sprint item:** S2-A — sprint-audit threshold documentation + schema_overflow_rate resolution

---

## Context

`bth sprint-audit` computes 7 rigor signals across a project's recent run history and emits `[WARN]` anomalies when signals cross threshold values. These thresholds were introduced in v0.6 as **calibration targets** (spec Item 5, `260526_agentic-science-v06-evolution-spec.md`) — meaning they are starting points derived from empirical studies, not validated project-specific gates. This ADR records the domain rationale for each threshold, the literature source it derives from, and the calibration status.

Additionally, the `schema_overflow_rate` signal had a semantic bug in its implementation (`sprint_audit.py:229–240`) that this ADR resolves.

### What the signals protect against

The synthesis (`260526_agentic-science-nlm-synthesis.md §1.5`) identifies the failure modes each signal is designed to detect:

- **Diversity compression / Hypothesis Hivemind** — an agent drives all outcomes to the same label, hiding genuine uncertainty.
- **Constraint drop / objective drift** — sidecar bypass removes the pre-registration anchor that distinguishes a genuine finding from a post-hoc selection.
- **Overexcitement / premature conclusions** — outcome branches the researcher declared but never observed suggest the experiment hasn't tested the full hypothesis space.
- **Metric substitution / outcome switching** — script emits results outside the declared schema, allowing outcome switching after the fact.
- **Post-hoc experiment culling** — worst outcomes concentrated in the early portion of a time-ordered sequence are a signature of retroactive experiment selection.

---

## Decision: `schema_overflow_rate` semantics

### The ambiguity

The previous implementation read:

```python
metadata = json.loads(run.get("metadata", "{}") or "{}")
if metadata:  # any non-empty metadata = overflow
    overflow_count += 1
```

This counted **any run with non-empty metadata as overflow**.

### Resolution: the previous behavior was a bug

**`metadata` is the script's primary output channel.** `runner.py:_read_result_emission()` reads the script's result JSON from `BTH_RESULTS_PATH` and stores it in `Run.metadata`. The `[result_schema]` section of the sidecar declares which keys the script is *expected* to emit.

Therefore:
- A run where `metadata = {"temp_std": 3.2}` and the sidecar declares `temp_std = "float"` — **no overflow** (declared key).
- A run where `metadata = {"temp_std": 3.2, "undeclared_debug": 42}` and the sidecar does not declare `undeclared_debug` — **overflow** (metric substitution risk).
- A run without `sidecar_path` — **skip** (no schema to compare against; tracked by `bypass_explicit`).

The old behavior flagged every well-behaved experiment that emitted declared results as overflow, making the signal permanently near-100% for any project using sidecars correctly.

### Fix applied

Added `_load_sidecar_schema_keys(sidecar_path) -> set[str]` and replaced the overflow check with an undeclared-key cross-reference. Denominator changed from `total_all` to `runs_with_sidecar`.

---

## Signal threshold table

| Signal | Threshold | Direction | Failure mode | Literature source | Calibration status |
|--------|-----------|-----------|--------------|-------------------|-------------------|
| `error_rate` | 0.10 | `> 0.10` flags | Systematic script failures | Internal (spec Item 5) | **Uncalibrated.** Domain reasoning: >10% indicates infrastructure problems. No empirical study. |
| `bypass_explicit` | 0.30 | `> 0.30` flags | Constraint drop / objective drift | arXiv 2509.08713 constraint violation rates (1.3%–71.4%) | **Loosely calibrated.** 30% is midpoint heuristic from LLM constraint-violation distribution. |
| `bypass_in_agent_mode` | 0.05 | `> 0.05` flags | Unexpected agentic bypass | Internal (ADR 260526_bypass-rate-split, spec D4) | **Uncalibrated.** Deliberately 6× tighter than `bypass_explicit`; agents have zero incremental cost to include a sidecar. |
| `outcome_entropy` | 0.5 nats | `< 0.5` flags | Diversity compression / Hypothesis Hivemind | arXiv 2501.10421 hivemind experiment | **Loosely calibrated.** 0.5 nats < ln(2)≈0.693 (balanced 2-outcome). |
| `unfired_branches` | 0.40 | `> 0.40` flags | Overexcitement / premature conclusions | arXiv 2509.08713 revision-agent unfired branches | **Loosely calibrated.** From observation that >40% of revision-agent decision branches were never triggered. |
| `schema_overflow_rate` | 0.20 | `> 0.20` flags | Metric substitution / outcome switching | arXiv 2510.21652 AstaBench metric misuse | **Loosely calibrated** (meaningful only after bug fix above). AstaBench §5: ~20% of manipulated-condition runs used out-of-schema metrics. |
| `post_hoc_bias_flag` | worst-label count in first third > 10% of total | Boolean | Post-hoc experiment culling | arXiv 2510.21652 AstaBench §4: chi2(4,200)=61.99, p<1e-10, Cramer's V=0.39 | **Best-calibrated.** 10% figure from AstaBench manipulated-vs-control comparison. |

### Calibration debt summary

Three signals have meaningful empirical grounding: `outcome_entropy`, `unfired_branches` (arXiv 2509.08713), `post_hoc_bias_flag` (AstaBench chi-squared).

Four signals are domain-reasoned or loosely analogized: `error_rate`, `bypass_in_agent_mode` (internal reasoning), `bypass_explicit` (LLM constraint-violation midpoint), `schema_overflow_rate` (AstaBench metric-misuse observation).

**v0.6 policy:** all signals emit `[WARN]` only. "Calibrate first, gate later."

---

## Consequences

**Positive:**
- Every threshold has a traceable literature source or explicit "uncalibrated" label.
- The `schema_overflow_rate` bug fix makes the signal meaningful: it drops to near-zero for well-behaved projects and fires only when scripts emit undeclared keys.
- Inline comments link threshold decisions to this ADR, making calibration debt visible at the code level.

**Negative:**
- `schema_overflow_rate` will drop dramatically for existing projects (from near-100% to near-0%) after the fix. This is correct — the old value was wrong.
- Calibration debt is documented but not resolved. Project-specific calibration requires real run-history analysis.

---

## References

- `260526_agentic-science-nlm-synthesis.md` §1.5 (signal calibration table with literature sources)
- `260526_agentic-science-v06-evolution-spec.md` §4 Item 5 (7-signal extension)
- `260526_bypass-rate-split.md` (D4 rationale for two-metric bypass)
- arXiv 2501.10421 — outcome entropy / hivemind experiment
- arXiv 2509.08713 — bypass rate / unfired branches / constraint violation data
- arXiv 2510.21652 — schema overflow / post-hoc bias AstaBench chi-squared
- `src/bathos/runner.py:_read_result_emission()` — confirms metadata = script output JSON
