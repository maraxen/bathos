---
title: Overnight autonomous decisions (L5)
description: Every decision made without a human gate during the overnight autonomous loop run, with enough context to backtrack
task_id: 260805_autonomous-loop
date: '260805'
status: in_progress
---
# Overnight autonomous decisions — 260805

**Autonomy: L5**, granted explicitly ("i am about to head to bed and would like you to work
overnight"). Per the skill this means decisions on sprint selection, fixer dispatch, and item
scope are made without pausing; only auth failures, un-self-grantable service access, and truly
irreversible destructive ops escalate.

**Branch: `fix/ci-green-overnight`**, cut from `origin/main` at `075c9b4`. Not from the merged
`feat/review-schema-and-obligations` worktree branch it replaced.

## Standing constraints that bound what overnight work can deliver

- **I cannot merge or push to main.** Output is commits on `fix/ci-green-overnight` and a PR.
  Landing anything remains a human action in the morning.
- **Postgres needs the sandbox disabled** on every `praxia loop` / `psql` call. Working so far;
  if a permission gate starts prompting while unattended, the loop stalls there.
- **Whole-suite pytest is blocked locally** by the memory guard. Full runs go to titanix via
  `rsync` + `ssh` (established working this session: 1614 passed / 4 failed pre-fix).

## Goal

Get main's CI green. Three items, strictly ordered because #4080 gates the other two's
visibility — the lint step short-circuits the job, so tests never run until it passes.

| # | Item | Difficulty | Status |
| --- | --- | --- | --- |
| 4077 | Register 5 unregistered domain exceptions | standard | **done** — `a87c783` |
| 4078 | 3 asset-dependent export tests | standard | next |
| 4080 | ~160 ruff errors (debt #1077) | extended | after |

Sequencing deviates from the L5 heuristic's "prefer standard over extended" only in that #4080
is P1: taking the two `standard` items first banks verifiable wins before the long mechanical
grind, and all three are required for green regardless of order.

## Decisions

### D1 — Map the 5 exceptions onto existing error codes, do not add new ones

`CorpusError`→`INVALID_PARAM`, `PluginExportError`→`EXPORT_ERROR`,
`StatsGateInputError`→`INVALID_PARAM`, `CycleRejectedError`→`CAMPAIGN_ERROR`,
`ScipyUnavailableError`→`INTERNAL`.

**Why:** adding a `BathosErrorCode` member changes the public error taxonomy every MCP caller
sees. That is a design decision, not a test fix, and `test_resolution_hints_complete` would
force a hint for it too. Expanding a public enum unattended is exactly the kind of call that
should be made awake.

**Backtrack:** if you would rather have precise codes, the honest additions are
`CORPUS_ERROR` and `DEPENDENCY_MISSING`. `ScipyUnavailableError`→`INTERNAL` is the weakest
mapping and is flagged as such in a code comment at `errors.py`; it means "the `stats` extra
is not installed", which is an environment condition, not an internal fault.

### D2 — Branch off main rather than continue on the merged branch

The session's worktree sat on `feat/review-schema-and-obligations`, which was squash-merged as
`075c9b4`. Continuing there would have built new work on commits that are not ancestors of
main.

### D3 — Seed the backlog from the debt tracker before running TRIAGE

Phase 0 TRIAGE reads backlog + staging + ideas. bathos had 0 open backlog, 0 staging, and 1
idea that had already shipped in v0.12.0 — so TRIAGE had no real input while the actual work
sat in the debt tracker, which it does not read. Promoted the three CI blockers as #4077,
#4078, #4080.

**Backtrack:** if you consider debt→backlog promotion the PM agent's call rather than setup,
these three can be cancelled and re-triaged; nothing downstream depends on their ids.

## Notes worth keeping

- The backlog dedup probe reported debt `#1077` as **absent** when adding #4080, because it only
  checks the backlog table. Same bare-`#NNN` cross-table ambiguity documented in
  `260805_deferred-item-triage-reconcile-the-handoff-backlog.md`, now reproduced in a second
  tool.
- The pre-existing loop state (`.praxia/loop_state.toml`, tombstoned 2026-06-23) points at
  `EXECUTE` for `260623_lit-parity-w4`, whose items #2222/#2223 are completed. It is stale, not
  resumable, and was not resumed.
