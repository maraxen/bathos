# ADR: Nonrepudiation Ambition — Self-Signed Manifest Only for v0.6

**Date:** 2026-05-26
**Status:** Accepted
**Decision tag:** D5 (in spec `260526_agentic-science-v06-evolution-spec.md`)
**task_id:** 260526_agentic-science-evolution

---

## Context

K-Veritas (arXiv 2605.08586, synthesis §1.1 mechanism 4) defines true nonrepudiation as a tamper-evident record signed by a key the author does not control. This is the strongest mechanism in the synthesis's four-tier ranking of hypothesis-commitment integrity guarantees.

For a solo researcher (this project's consumer), there is no institutional independent signer by default. The synthesis open question §6 Q4 explicitly raises this gap.

Available implementation paths:
1. **Self-signed manifest only** — content-hash bound to git commit. Tamper-evident WITHIN the author's history but not author-key-separated.
2. **Optional RFC 3161 timestamp authority (TSA) hook** — manifest optionally timestamped by a public TSA (FreeTSA, DigiCert). Provides author-key separation when enabled.
3. **Required external TSA for `--agent-mode`** — strongest; agentic runs cannot complete without TSA stamp.
4. **OSF pre-registration API integration** — submits manifest to Open Science Framework; OSF holds the timestamp.

Path 2 and Path 4 are described in DR-2 raw note (`.praxia/nlm.jsonl` line 62) as legitimate paths for solo researchers; the synthesis correctly identifies them but does not commit to either.

## Decision

**v0.6 ships with self-signed manifest only.** No external TSA, no OSF integration, no third-party signing dependency.

The manifest contains:
- `sidecar_sha256` (full-sidecar hash per ADR `260526_manifest-hash-granularity.md` D2)
- `git_sha` (current HEAD)
- `script_sha256` (script content hash, already computed at `runner.py:117`)
- `agent_id` (nullable; populated under `--agent-mode`)
- `run_id` (UUID)
- `written_at` (ISO 8601 UTC)

"Self-signed" in this ADR explicitly means: **content-hash + git-commit-bound, with no separate signing key**. The git commit itself is the author identity anchor; the sha256 hashes are the tamper-evidence. There is no field in the manifest dedicated to a cryptographic signature.

External TSA / OSF integration is **deferred to v0.7+**. Reopen when at least one of:
- Multi-author collaboration enters scope, OR
- Submission to a venue that requires nonrepudiation (e.g., a conference following K-Veritas recommendations), OR
- The researcher decides the self-signed-only ceiling is materially limiting reproducibility audits.

## Consequences

**Positive:**
- No new network dependency in `bth run`. Offline cluster jobs (Engaging compute nodes per `~/.claude/rules/CLUSTER.md`) work unchanged — compute nodes typically have no outbound internet, so any TSA hook would have failed there.
- No service account or API key management overhead.
- The manifest still provides strong intra-author integrity: any post-hoc edit to the sidecar will fail hash comparison; any rewrite of the script will fail script_sha256 comparison; any rebase that loses the manifest commit will fail git_sha lookup.

**Negative:**
- The synthesis's strongest tier (mechanism 4: nonrepudiable execution records) is **not achieved** in v0.6. The integrity guarantee is "tamper-evident under good-faith authorship" not "tamper-evident under adversarial authorship."
- An adversarial researcher who controls their own git history can in principle backdate manifests by rewriting commits. The same researcher who would do this would not be using bathos in good faith anyway.
- If a future v0.7+ adds external TSA, the manifest schema will need to grow a `tsa_stamp` field. Forward-compat is straightforward (nullable field) but worth noting.

## Alternatives considered

**Optional TSA hook in v0.6** — manifest can optionally be stamped by a public TSA; no-op when not configured.
*Rejected for v0.6:* adds optional-feature complexity without a concrete use case yet. The cost of the optional code path is real (network handling, TSA failure mode, retry logic) while the population that would enable it is the empty set. Revisit in v0.7+ when a user need surfaces.

**Required TSA for `--agent-mode`** — strongest; agents cannot run without TSA stamp.
*Rejected for v0.6:* breaks offline cluster jobs unless TSA responses are cached, which adds substantial complexity. Also creates a network dependency for the integrity gate, which inverts the gate's purpose (the gate should be more reliable than the experiment, not less).

**OSF pre-registration API integration** — submits manifest to OSF; OSF holds nonrepudiation.
*Rejected for v0.6:* OSF account creation and API key management is meaningful setup overhead. OSF's API is designed for human-driven pre-registration with rich metadata; auto-submitting a TOML at every `bth run` is not the API's design center. Defer to v0.7+ as a `bth publish` adjacent feature.

## References

- Synthesis §1.1 mechanism 4 (K-Veritas / nonrepudiable execution records)
- Synthesis §3 (maraxiom retraction — formerly housed nonrepudiation discussion)
- DR-2 raw note: `.praxia/nlm.jsonl` line 62 (alternatives for solo researcher)
- Brainstorm Q4 resolution (this task_id)
- Prior design: `.praxia/docs/preregistration/260520_prereg-nonrepudiation-design.md`
- Spec `260526_agentic-science-v06-evolution-spec.md` §4 Item 2 (manifest implementation)
- Related ADR: `260526_manifest-hash-granularity.md` (D2 — defines what the hash covers)
- Synthesis open question §6 Q4 (now resolved for v0.6 only; reopens in v0.7+ if conditions above are met)
- arXiv 2605.08586 (K-Veritas)
- Cluster constraint: `~/.claude/rules/CLUSTER.md` (offline compute nodes)
