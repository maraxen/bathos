---
title: Blast-radius assessment for bathos-tracked experiments
task_id: 260826_blast-radius-assessment-skill
date: 260826
status: draft
brainstorm_session: true
invest_overrides:
  - dimension: Small
    verdict: FAIL
    note: >
      Intentionally scoped as a multi-phase capability (3 anchor types x 3 entity
      levels x 2 shadow-mode subsystems). Recommend the planner/spec-challenger
      stage split this into at least two backlog items: (1) ledger + run-level
      flagging + commit/file anchors + manual invocation, (2) campaign/claim
      propagation + dependency-version anchor + shadow-mode hooks. Proceeding
      with this spec as a single scoping document per user direction; phasing
      is a TBD for the implementation planner, not resolved here.
---

# Blast-radius assessment for bathos-tracked experiments

## Problem

bathos has rich *per-run* provenance — git-drift (`checker.check_runs`: STALE/DIRTY_RUN/UNKNOWN_CODE),
output SHA-drift (AC-20, `check_output_sha_drift`), dependency-lock drift
(`check_dependency_lock_drift`, feeding the claim-tier `[differential]` gate), and
lineage/PROV-JSON ancestry export (`provenance.py`) — but no mechanism answers the
inverse question: **"a bug was just found or fixed — which past runs, campaigns, and
claims does it implicate?"**

This gap was named as the highest-ROI schema extension in the original design doc
(`260520_agentic-science-design.md` §3: "which downstream analyses depend on run X?
(impact of invalidation)") but was never built as a query — only per-run ancestry
export shipped. The user has hit this gap 3+ times: a bug is found, fixed, and there
is no systematic way to know which earlier experiment outcomes are now suspect.

## Decision Log

| # | Question | Decision | Rationale |
|---|---|---|---|
| 1 | Anchor types the skill accepts | Commit/commit-range, file/symbol, or dependency-version — chosen per invocation, one unified interface, not separate tools | User: "same skill, different anchors" |
| 2 | Anchor→run matching precision (v1) | File-path heuristic: `git diff --name-only` on the fix commit/range, cross-referenced against each run's recorded `git_hash` and command/script path | Import/call-graph precision would need a code dependency graph bathos doesn't have; deferred as tech debt (see TBDs) rather than built now |
| 3 | Output action | Report + persistent catalog flag; **no gating** of downstream actions (`campaign conclude`, `claim validate` do not refuse) in v1 | User explicitly separated "flag" from "gate" |
| 4 | Flag granularity | Independent flags at run, campaign, AND claim level — not run-only, not content_hash-based | A campaign can be "uncertain" from partial member-run exposure; a claim's exposure depends on which union-gate clauses the affected runs backed |
| 5 | Flag lifecycle / clearing | Manual re-attestation is the only mechanism that durably clears a flag (mirrors trust-ledger's PASS-before-promote gate). An auto-clear heuristic (re-run reproduces outcome) computes and logs a verdict on every flagged record but **never applies it** — shadow mode only | User: "best of both worlds... live test any auto-clear feature before it actually would be used in production" |
| 6 | Invocation trigger | Manual invocation only acts on the ledger in v1. Event/git-hook-based auto-suggestion may run, but only in shadow mode — it logs what it would have flagged without writing a durable ledger record | Same shadow-mode principle applied to trigger reliability, not just to clearing |
| 7 | Runs with untrustworthy `git_hash` (DIRTY_RUN/UNKNOWN_CODE) | Separate "unverifiable" bucket, distinct from "affected" and "unaffected" — never silently merged into either | Neither "clean" nor "affected" is honestly supportable for these; forcing a third bucket keeps the report honest |
| 8 | Flag storage architecture | New parallel `blast_radius_ledger`, composite-keyed by `(entity_type: run\|campaign\|claim, entity_id)`, dual-write cool Parquet + warm DuckDB, fold-latest-wins by `amended_at` — same shape as `trust_ledger.py` | Steelmanned alternative (attach status fields to `campaigns.py`/`postmortem.py`/`runs` directly) loses on migration surface (3 existing v13+ schemas touched vs. 1 new isolated one) and repeats the exact status-conflation risk the trust-ledger docs already warn about. Additive ledger preserves reversibility while this query pattern is still unproven and iterating under shadow mode; colocation convenience is recoverable later via display-time joins in `bth ls`/`campaign show` |

## Acceptance Criteria

- AC-1: Given a commit-range anchor, when blast-radius assessment runs, then it computes the changed file set via `git diff --name-only` over that range.
- AC-2: Given a file/symbol anchor, when blast-radius assessment runs, then the changed file set is the user-supplied path(s) directly.
- AC-3: Given a dependency-version anchor, when blast-radius assessment runs, then it reuses `check_dependency_lock_drift`/`hash_dependency_lock` to find runs whose recorded `dependency_lock_sha256` predates the flagged version.
- AC-4: Given a run whose recorded `git_hash` is an ancestor of the fix commit, when that run's command/script path is in the changed file set, then the run is placed in the "affected" bucket.
- AC-5: Given a run classified DIRTY_RUN or UNKNOWN_CODE by `check_runs()`, when blast-radius assessment runs, then that run is placed in the "unverifiable" bucket.
- AC-6: Given a run in the "affected" or "unverifiable" bucket, when the assessment completes, then a record is appended to `blast_radius_ledger` keyed by `(entity_type="run", entity_id=run_id)`.
- AC-7: Given a campaign with ≥1 "affected" or "unverifiable" member run, when the assessment completes, then a distinct campaign-level ledger record is appended.
- AC-8: Given a claim whose union-gate clauses are backed by an affected run, when the assessment completes, then a claim-level ledger record is appended naming the implicated clause IDs.
- AC-9: Given an existing ledger record, when a manual re-attestation clears it, then a new record is appended with the cleared state (no in-place mutation, fold latest-wins by `amended_at`).
- AC-10: Given a shadow auto-clear heuristic evaluates a flagged run, when it produces a verdict, then that verdict is recorded as a shadow annotation on the ledger record and never changes the record's actual state.
- AC-11: Given any invocation, when the report is produced, then it is shown to the user before any ledger write is treated as final (report-then-flag ordering).
- AC-12: Given a completed assessment, when the entity is later queried via `bth ls`/`sprint-audit`/`claim validate`, then the flag is surfaced automatically without a separate blast-radius-specific query.
- AC-13: Given a file-path heuristic match, when the report is generated, then each entry records its match reason (matched file(s), matched clause(s)) for audit — this is the load-bearing mitigation for the named pre-mortem risk (see below).

## Assumptions

- `check_dependency_lock_drift`/`hash_dependency_lock` (from claim-tier debt #1071) is reusable as-is for the dependency-version anchor; no new hashing logic needed.
- `check_runs()`'s existing STALE/DIRTY_RUN/UNKNOWN_CODE classification is sufficient input for the "unverifiable" bucket; no new git-state detection needs building.
- Campaign membership and claim union-gate clause-to-run linkage are already queryable from existing `campaigns.py`/`claim.py` structures (`campaign_edges`, `claim_discriminates`) — the assessment reads these, it does not need new linkage tracking.
- Solo-researcher cadence means the ledger does not need concurrent-write conflict resolution beyond what `trust_ledger.py`'s dual-write pattern already handles.

## TBDs (deferred to implementation planning)

- Exact `blast_radius_ledger` record schema (field names/types) — mirrors `trust_ledger.py`'s shape but needs new fields: `matched_files`, `matched_clauses`, `shadow_verdict`.
- Which specific existing query surfaces get the AC-12 "surface automatically" integration in v1 vs. later (`bth ls`, `sprint-audit`, `claim validate`, `campaign show` are candidates, not commitments).
- CLI surface naming/flags (e.g. `bth blast-radius --commit <sha> | --file <path> | --dependency <lock-entry>`).
- Whether shadow-mode data (auto-clear verdicts, hook-trigger accuracy) needs its own reporting command or lives as inline ledger fields.
- The precision-improvement path deferred as tech debt: cherry-picking praxia's analyzer/graph tooling (`code_index_workspace`, `graph_build`/`graph_query`) toward a dedicated Rust import/call-graph tool, and what false-positive-rate threshold (measurable via AC-13's match-reason field) would justify building it.
- MCP tool surface naming, mirroring the `bathos-mcp` error envelope conventions.
- Phasing split (see `invest_overrides` above).

## Pre-mortem Record

**Named risk (user-selected as most likely): heuristic noise.** The file-path heuristic
over-flags — a run touches a changed file without ever executing the changed logic path
— producing a report noisy enough that it gets ignored, the same fate as an unread alert
channel. Two alternative failure modes (ledger drift from source tables; shadow-mode data
accumulating without ever being reviewed/promoted) were named and set aside as less likely.

**Mitigation baked into scope:** AC-13 requires every match to record its match reason,
making false positives auditable rather than opaque. This is deliberately the only v1
mitigation — no algorithmic precision improvement is attempted now. The match-reason data
this produces is also the intended empirical justification for eventually building the
deferred import/call-graph precision tool (TBD above), rather than that tool being built
speculatively.

**Residual risk accepted:** v1 does not reduce false-positive rate; it only makes false
positives auditable and collects the evidence needed to decide whether the precision
upgrade is worth building.
