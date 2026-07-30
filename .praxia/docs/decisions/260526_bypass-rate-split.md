# ADR: `bypass_rate` Reports Two Metrics Separately

**Date:** 2026-05-26
**Status:** Accepted
**Decision tag:** D4 (in spec `260526_agentic-science-v06-evolution-spec.md`)
**task_id:** 260526_agentic-science-evolution

---

## Context

`bth sprint-audit` already tracks a `bypass_rate` signal at `sprint_audit.py:96-127`. The signal counts runs that ran without sidecar enforcement. The synthesis §2.4 proposed extending and formalizing this signal with a calibrated threshold (>30%).

The question: **what counts as a "bypass" — and should distinct populations be aggregated or reported separately?**

Two distinct populations exist:
1. **Explicit bypass** — researcher passed `--no-sidecar` knowingly. Bypass intent is in the audit trail.
2. **Unexpected bypass** — run ran under `--agent-mode` but the agent did not provide a sidecar, falling into an unintended degraded-mode path.

These have different operational meanings:
- Explicit bypass is a researcher discipline signal: how often is the convention being skipped for legitimate reasons (e.g., exploratory scratch work)?
- Unexpected bypass is an agentic pipeline integrity failure: an agent that was supposed to operate under the gate fell through the gate.

## Decision

**Report two metrics separately**, not aggregated:

- `bypass_explicit` = `count(runs where --no-sidecar)` / `count(total runs)`
- `bypass_in_agent_mode` = `count(runs where --agent-mode AND no sidecar found)` / `count(--agent-mode runs)`

Both are surfaced in `bth sprint-audit` output and in the structured `signals` dict (Item 5 spec). Each gets its own threshold:

| Signal | Proposed v0.6 threshold | Calibrate against |
|---|---|---|
| `bypass_explicit` | >0.30 flagged (WARN) | Researcher's own historical baseline |
| `bypass_in_agent_mode` | >0.05 flagged (WARN) | Should always be near zero; 5% threshold avoids single-run noise. Any nonzero count is suspect — investigate even below threshold. |

Both thresholds are CALIBRATION TARGETS in v0.6, not hard gates; all signals emit `[WARN]` only (per spec Item 5). Thresholds may be tightened (or pre-calibrated against the researcher's own historical catalog) before any hard-gate decision in v0.7+.

## Consequences

**Positive:**
- The distinct integrity stories are preserved. An audit that sees "30% bypass rate" can no longer mask whether researcher discipline or agent gate-failure is the cause.
- `bypass_in_agent_mode` becomes a strict alarm signal — any nonzero value warrants investigation. This aligns with the asymmetric enforcement in ADR `260526_adversarial-check-policy.md` (D3).
- Threshold calibration is per-metric; researcher's exploratory discipline doesn't taint agent pipeline alerts and vice versa.

**Negative:**
- One more column in `sprint_audit` output. Researcher must learn to read two numbers instead of one.
- The `bypass_in_agent_mode` denominator (agent-mode runs only) requires `sprint_audit.py` to maintain agent-mode run counts; currently it does not. Implementation cost is modest (one additional aggregate in the DuckDB query) but it is a small addition to scope.

## Alternatives considered

**Single aggregate `bypass_rate`** — current behavior; simpler to read.
*Rejected:* the conflation is the bug. Synthesis §2.4 specifically asks for a calibrated threshold to detect agentic pipeline gate-failure; if explicit bypass dominates the metric numerically, the signal is buried.

**Only `bypass_explicit` (drop agent-mode tracking)** — covers documented user behavior; ignores agent-mode population.
*Rejected:* the entire point of `--agent-mode` is to enforce stricter discipline; not tracking when that discipline failed defeats the mode's purpose.

**Three-way split (explicit / agent-mode-fall-through / lint-skip)** — finer-grained.
*Rejected for v0.6:* the third category (lint-skip via `bth lint --skip`) is uncommon enough that it can be folded into `bypass_explicit` initially. Revisit if signal noise emerges.

## References

- Synthesis §2.4 (sprint anomaly signal formalization)
- Brainstorm Q3 resolution (this task_id)
- Spec `260526_agentic-science-v06-evolution-spec.md` §4 Item 5 (implementation extends `sprint_audit.py:96-127`)
- Existing: `sprint_audit.py:96-127` (current single `bypass_rate`)
- Related ADR: `260526_adversarial-check-policy.md` (D3) — `bypass_in_agent_mode` aligns with D3's strict agent-mode posture
- Synthesis open question §6 Q3 (now resolved)
