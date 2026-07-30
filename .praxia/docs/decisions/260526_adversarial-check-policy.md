# ADR: `adversarial_check` Field — Required-for-Agent-Mode, Warn-for-Human

**Date:** 2026-05-26
**Status:** Accepted
**Decision tag:** D3 (in spec `260526_agentic-science-v06-evolution-spec.md`)
**task_id:** 260526_agentic-science-evolution

---

## Context

bathos v0.6 introduces an `adversarial_check` field on each `[outcomes.pass]` block in sidecar TOMLs. The field is a DuckDB SQL condition the researcher believes would flip the outcome if the hypothesis were wrong — the operational form of falsification design from synthesis §1.4.

The question: **when is `adversarial_check` required vs optional?**

Synthesis §1.4 ("Sound Agentic Science Requires Adversarial Experiments," arXiv 2604.22080) argues that pre-registration of pass conditions alone does not prove a hypothesis was load-bearing — an agent can perfectly pre-register a weak confirmatory test that never genuinely stresses the claim. Q-4 raw note (`.praxia/nlm.jsonl` line 102) is explicit: "pre-registration alone CANNOT substitute for adversarial design."

Three policy options were considered:
1. Required universally from v0.6 — breaks every existing sidecar
2. Warn-only for v0.6, required in v0.7 — gradual rollout
3. Asymmetric: required for `--agent-mode`, warn-only for human runs

## Decision

**Asymmetric enforcement: `adversarial_check` is required for `--agent-mode` runs and warn-only for human runs in v0.6.**

`prereg.gate_check` (at `prereg.py:103-160`) blocks `--agent-mode` runs if any `outcomes.pass` block lacks an `adversarial_check`. Human runs emit a lint WARNING via `linter.py` Tier-2 check but proceed.

Enforcement deepens as the field matures. v0.6 does not commit to a universal-required v0.7 schedule; that policy decision is deferred to v0.7 planning.

## Consequences

**Positive:**
- The strictest enforcement lands where the integrity stakes are highest: autonomous pipelines that can't be paused for researcher judgment.
- Human runs gradient room — a researcher running a quick smoke test isn't blocked by lint friction; they get a reminder and a chance to add the field.
- No flag-day migration. Existing personal-project sidecars continue to work without `adversarial_check`; the researcher retrofits at their own pace.

**Negative:**
- Mixed enforcement is harder to explain than "always required" or "always optional." Documentation must be explicit about the boundary.
- The lint check itself is a syntactic proxy (full logical implication is SQL-undecidable). Atom-6c in the spec includes a tautology deny-list (`AND 1=1`, `AND TRUE`, `AND col = col`) and a distinct-column-preference WARNING to harden against trivial circumvention, but a determined gamer can still satisfy the heuristic. The lint message surfaces this honestly to the researcher.
- `prereg.gate_check` in `--agent-mode` now has one more failure mode (manifest-write-failure-style); error payload uses `GateErrorCode.ADVERSARIAL_CHECK_MISSING` per ADR `260526_*` (D4 placeholder).

## Alternatives considered

**Required universally from v0.6** — strongest signal value.
*Rejected:* breaks every existing sidecar across user's ~10 projects on day one. Personal-tool sprint values composability with prior work; this would force a bulk migration before any v0.6 feature is usable.

**Warn-only across v0.6, required in v0.7** — gentlest rollout.
*Rejected:* defers the strictness where it most matters. Agent-mode pipelines that run unattended for hours benefit most from a hard gate; deferring the gate by a version cycle delays the integrity payoff for the use case it was designed to protect.

**Optional indefinitely (documentation-only)** — lowest friction.
*Rejected:* Q-4 evidence indicates pre-registration without adversarial design is the documented failure mode. A documentation-only field is bypassed by every agent that doesn't read documentation, which is essentially all of them.

## References

- Synthesis §1.4 (sound agentic science)
- Q-4 raw note: `.praxia/nlm.jsonl` line 102
- Brainstorm Q2 resolution (this task_id)
- Spec `260526_agentic-science-v06-evolution-spec.md` §4 Item 6 (implementation)
- Related ADR: `260526_manifest-hash-granularity.md` (D2 — adversarial_check is included in manifest hash)
- Spec atom-6c (lint hardening; tautology deny-list + distinct-column preference)
- Synthesis open question §6 Q2 (now resolved)
