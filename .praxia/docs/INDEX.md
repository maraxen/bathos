# bathos Internal Docs

## Daily

## Plans
- [260515_v01-pypi-rtd-plan](plans/260515_v01-pypi-rtd-plan.md)
- [260515_v02-sprint-plan](plans/260515_v02-sprint-plan.md)
- [260526_v06-sprint-composition](plans/260526_v06-sprint-composition.md) — Sprint composition: 12-item v0.6 dispatch DAG with workflow assignments, parallelization batches, exit criteria; flags Items 4+6 prereg.py conflict and sidecar canonical_toml() determinism risk
- [260601_v061-sprint-composition](plans/260601_v061-sprint-composition.md) — Sprint composition: 4-item v0.6.1 dispatch DAG (validate.py guard commit, GateErrorCode taxonomy cleanup, sprint-audit threshold ADR + boundary tests, NLM hook wiring); defers POPPER e-value and 11b-11d gate wiring
- [260611_worktree-workspace-resolution-dag](plans/260611_worktree-workspace-resolution-dag.md) — backlog DAG (T1→T3/T3b→T4) for worktree-aware workspace resolution; praxia backlog #1676–#1684

## Handoffs

## Specs
- [260611_worktree-workspace-resolution](specs/260611_worktree-workspace-resolution.md) — `resolve_workspace()` seam so `bth` resolves live-worktree fs_root while keeping catalog identity stable; post adversarial review (challenger/defender); implemented
- [260515_bathos-design](specs/260515_bathos-design.md)
- [260515_bathos-v01-plan](specs/260515_bathos-v01-plan.md)
- [260518_bathos-migration-v1-revised](specs/260518_bathos-migration-v1-revised.md)
- [260520_agentic-science-design](specs/260520_agentic-science-design.md)
- [260526_agentic-science-v06-evolution-spec](specs/260526_agentic-science-v06-evolution-spec.md) — Implementation spec: v0.6 evolution (12 items, 5 phases) — exception-swallow remediation, pre-execution manifest, outcome=error, adversarial_check, sprint-audit signals, bth cite/lineage --format prov, praxia NLM hook + Rust gate work
- [260527_telemetry-design](specs/260527_telemetry-design.md) — Telemetry/audit layer: stdlib logging + QueueHandler → per-process JSONL under <catalog_dir>/logs/ (rides bth sync); covers runner/sidecar/prereg/postmortem/campaign/lineage/mcp/sync/catalog; rejects aiologger
- [260601_bathos-backup-recovery-spec](specs/260601_bathos-backup-recovery-spec.md) — Implementation spec: v0.7 backup/recovery hardening (6 fixes) — compact.py transaction safety, DuckDB integrity check on connect, pre-migration .bak backup, archive SHA256 checksums, bth verify command, sync truncation detection
- [260602_item-760-threshold-lint](specs/260602_item-760-threshold-lint.md) — Implementation spec: backlog item #760 threshold epistemic hygiene — OutcomeSpec.source field, Sidecar.regression_threshold_basis field, check_threshold_basis Tier-2 linter, 9 test cases
- [260602_item-137-global-instruction-portability](specs/260602_item-137-global-instruction-portability.md) — Design doc: backlog item #137 global instruction portability — explores multi-surface snippet export, rules vs. skill distinction, scope semantics, manifest awareness, and composition model; identifies 10 open questions before implementation
- [260602_item-792-popper-evalue](specs/260602_item-792-popper-evalue.md) — Implementable spec: POPPER e-value multi-run campaign primitive — mode="sequential", [popper] sidecar block, likelihood-ratio e-values, per-script E_n products, threshold lock, premature_stopping_rate sprint-audit signal; 10 fixer tasks

## Audits

## Research
- [260526_agentic-science-nlm-research-plan](research/260526_agentic-science-nlm-research-plan.md) — NLM research prompts (3 Deep Research + 6 Queries) for agentic science sources beyond May-2026 synthesis
- [260526_agentic-science-nlm-synthesis](research/260526_agentic-science-nlm-synthesis.md) — Oracle-approved synthesis: agentic science rigor findings + bathos v0.6+ / maraxiom / praxia implications
- [260616_bathos-long-horizon-rigor](research/260616_bathos-long-horizon-rigor.md) — **SEED for claim-level rigor systematization** (from asr worked example): claim-tier pre-registration above the sidecar (Objective-Drift fix); Claim Ledger / Discriminability Map / Confound Register / Union Gate; enforcement ladder L0→L5 + rigor-item→mechanism map; §10 baseline/literature-reimpl fidelity + signal-discrimination probes. Input for research → brainstorm → spec → coherence → backlog DAG.

## Decisions
- [260526_manifest-hash-granularity](decisions/260526_manifest-hash-granularity.md) — D2: pre-execution manifest hashes full sidecar (hypothesis + outcomes + adversarial_check + schema); outcome refinement becomes tracked REVISION
- [260526_adversarial-check-policy](decisions/260526_adversarial-check-policy.md) — D3: `adversarial_check` required for --agent-mode, warn-only for human runs; rationale for asymmetric enforcement
- [260526_bypass-rate-split](decisions/260526_bypass-rate-split.md) — D4: bypass_rate reported as two metrics (bypass_explicit, bypass_in_agent_mode); rationale for not conflating populations
- [260526_nonrepudiation-v06](decisions/260526_nonrepudiation-v06.md)
- [260601_sprint-audit-threshold-rationale](decisions/260601_sprint-audit-threshold-rationale.md) — Documents domain rationale for all 7 sprint-audit signal thresholds; resolves schema_overflow_rate semantics (any-key check was a bug; fix: cross-reference against result_schema declared keys) — D5: v0.6 ships self-signed manifest only (content-hash + git-commit-bound); external TSA / OSF deferred to v0.7+

## Reference

## Roadmaps

## Preregistration
- [260520_prereg-nonrepudiation-design](preregistration/260520_prereg-nonrepudiation-design.md)

## Archive

## Misc
- [260515_mission](misc/260515_mission.md)

## Superpowers
> Skill outputs live in `.praxia/docs/superpowers/plans/` and `.praxia/docs/superpowers/specs/`.
- [plans](superpowers/plans/) — brainstorming + writing-plans outputs
- [specs](superpowers/specs/) — specification outputs
