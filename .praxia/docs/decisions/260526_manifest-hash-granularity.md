# ADR: Pre-Execution Manifest Hash Granularity

**Date:** 2026-05-26
**Status:** Accepted
**Decision tag:** D2 (in spec `260526_agentic-science-v06-evolution-spec.md`)
**task_id:** 260526_agentic-science-evolution

---

## Context

bathos v0.6 introduces a pre-execution manifest file (`<script-stem>.<run_id>.bth.lock.toml`) written between `runner.py:203` (`write_run`) and `runner.py:215` (`subprocess.run`). The manifest contains a cryptographic hash that proves the experiment's declared intent existed before execution began.

The question: **what does the hash cover?**

The synthesis identifies two integrity threats that this manifest addresses:
- **Constraint drop** (synthesis §1.3): the agent or researcher modifies experimental constraints mid-trajectory and presents the final result as if the original constraints had been met.
- **Implementation drift** (synthesis §1.3): outcome conditions are tuned after observing partial results, allowing post-hoc selection bias.

These threats apply to different parts of the sidecar:
- The `[experiment].hypothesis` string is the canonical statement of intent.
- The `[outcomes.*]` conditions are the canonical pass/fail criteria.
- The `[result_schema]` declares what the experiment will measure.
- The `adversarial_check` (new in v0.6) declares the falsification target.

## Decision

**The manifest hash covers the full sidecar**: hypothesis + outcomes + result_schema + adversarial_check. The hash is sha256 of the canonical sidecar TOML bytes.

Refining outcome conditions after the manifest is written becomes a **tracked REVISION** via `bth run --derived-from <original_run_id>`, producing a new run record with explicit lineage. The original manifest remains the integrity-of-record for the original execution.

## Consequences

**Positive:**
- Constraint drop and outcome tuning are equally guarded — the hash invariant detects both.
- The audit chain `claim → sidecar hash → manifest → run record → result` is unbroken; an auditor checking a downstream result can verify it against the original commitment in one hash comparison.
- `adversarial_check` (the synthesis §1.4 load-bearing field) is included in the hash, so it cannot be silently weakened or removed between commitment and execution.

**Negative:**
- Legitimate iterative refinement of outcome conditions during exploratory work requires explicit REVISION tracking rather than silent edits. For solo researcher use this adds friction but the friction is the point — the audit trail is the integrity guarantee.
- The sidecar bytes are canonicalized before hashing (key order normalized). This requires `sidecar.canonical_toml()` to be implementation-stable; any change to canonical serialization breaks hash compatibility with prior manifests.

## Alternatives considered

**Hypothesis-string-only hash** — would allow outcome tuning without REVISION events.
*Rejected:* synthesis §1.3 identifies constraint drop on outcome conditions specifically as a documented failure mode of AI Scientist v1/v2. A hash that misses this misses half the threat surface. Q-1 note in `.praxia/nlm.jsonl` (line 99) names the second commitment mechanism as covering "an execution plan," not just the hypothesis.

**Two-tier hash (hypothesis hash + sidecar hash, both stored)** — would let reviewers see hypothesis stability separately from outcome-condition stability.
*Rejected as redundant:* the same information is recoverable from the sidecar canonical form + lineage records. Adding two hashes to every manifest doubles the audit complexity without strictly more guarantee. Reconsider for v0.7+ if reviewer workflows surface a clear need.

## References

- Synthesis §1.3 (constraint drop) and §1.4 (adversarial design)
- Brainstorm Q1 resolution (this task_id)
- Related ADR: `260526_adversarial-check-policy.md` (D3, justifies why `adversarial_check` is hashed)
- Spec `260526_agentic-science-v06-evolution-spec.md` §4 Item 2 (manifest implementation)
- Synthesis open question §6 Q1 (now resolved)
