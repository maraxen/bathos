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
| 4078 | 3 asset-dependent export tests | standard | **done** — `7f4b493` (not as scoped — see D4) |
| 4080 | ~160 ruff errors (debt #1077) | extended | next |

**Full suite on titanix after 4077 + 4078: 0 failures** (was 4 at the start of the night).

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

### D4 — #4078's premise was wrong; xfail the tests and file the real bug upstream

**This is the decision most worth your attention in the morning.**

#4078 assumed missing assets and offered two options: commit them, or skipif. **Both are wrong.**
The assets exist and are tracked at the repo root (`agent_assets/skills/using-bathos/SKILL.md`
etc.). What is actually broken is `bth export --surface`, and the cause is a path-resolution
disagreement between the two consumers of `.praxia/manifest.toml`:

- **cisternal** resolves declared paths against the **manifest's own directory** —
  `cisternal/assets/manifest.py:25`, `self._root = self._manifest_path.parent` — so
  `agent_assets/skills/...` becomes `.praxia/agent_assets/skills/...`, which does not exist.
- **praxia** resolves the **same manifest** against the **repo root** (parent-of-parent),
  deliberately and with a comment saying so: `praxia-workflows/src/plugin_cli.rs:400-405`.

bathos's manifest is correct for praxia and wrong for cisternal, and **no single path string
satisfies both** — rewriting to `../agent_assets/...` would fix cisternal and break praxia.

**Chose `xfail(strict=True)` over `skipif`.** These tests are correctly detecting a live defect;
skipping would hide it. `strict=True` means they fail loudly if cisternal ever changes its
resolution, which is exactly the signal wanted. Filed the root cause as **debt #1189** with the
upstream fix written out (parent-of-parent for `.praxia/manifest.toml`, or an `--asset-root`
flag — `cisternal assets export` currently exposes neither).

**Backtrack:** if you disagree that xfail is acceptable in CI, the alternative is leaving the
three red and accepting that main's CI can never be green until cisternal is fixed. I judged a
recorded, strict, self-revoking xfail better than a permanently red build — but that is a
judgement call about your CI policy, not a fact, and it is one line each to revert.

**Scope note:** I did not touch `.praxia/manifest.toml`. Changing it would have made the tests
pass while breaking `praxia plugin install/export` for bathos — a silent, worse failure.

## Notes worth keeping

- The backlog dedup probe reported debt `#1077` as **absent** when adding #4080, because it only
  checks the backlog table. Same bare-`#NNN` cross-table ambiguity documented in
  `260805_deferred-item-triage-reconcile-the-handoff-backlog.md`, now reproduced in a second
  tool.
- The pre-existing loop state (`.praxia/loop_state.toml`, tombstoned 2026-06-23) points at
  `EXECUTE` for `260623_lit-parity-w4`, whose items #2222/#2223 are completed. It is stale, not
  resumable, and was not resumed.
