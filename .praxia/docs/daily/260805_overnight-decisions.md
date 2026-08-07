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
| 4080 | ~160 ruff errors (debt #1077) | extended | **done** — `13442f5` (agy, audited — D5/D6) |

**Full suite on titanix after all three: 0 failures** (was 4 at the start of the night).

**CI is still not green, and the three items above were not the whole story.** Clearing lint let
the `pyright` step execute for the first time ever, and it is a fourth blocker nobody could have
seen — see **D7**. Backlog **#4085**, and it needs your decision, not more overnight work.

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

### D5 — agy escaped its worktree and wrote to your main checkout; work rejected, NOT reverted

**Read this one first in the morning. Your main checkout is dirty and I deliberately left it
that way.**

Dispatched `agy` (antigravity CLI, `gemini-3.1-pro-high`, `--dangerously-skip-permissions`) on
the ruff cleanup, in a worktree at `.claude/worktrees/agy-ruff`. Two containment failures:

1. **It wrote to `/home/marielle/projects/bathos` instead of the worktree.** 110 modified files
   there, **0** in the worktree. The worktree sat *inside* the repo tree, so agy resolved its
   workspace to the git root and walked up.
2. **That checkout is on `fix/3717-claim-discriminates-propagation` @ `abcfc0a`** — one of your
   branches, and **not an ancestor of `origin/main`** (main is 14 commits ahead on a divergent
   line). So the lint was fixed against a codebase that isn't in main's history. The patch does
   not apply to `fix/ci-green-overnight`; it conflicts across many files.

**I did NOT revert your main checkout.** Reverting is *recoverable* (I saved the full diff), but
I cannot distinguish agy's edits from any uncommitted work of your own that may have been there
before it ran, and it is your active branch. Leaving it dirty is non-destructive; reverting is
destructive-but-recoverable. I chose the former.

**To clean it yourself:** `git -C /home/marielle/projects/bathos checkout -- src tests uv.lock`
(note it also modified `uv.lock`, which was out of scope). The full diff is saved at
`/tmp/claude/agy_ruff.patch` — 539KB, 110 files — but `/tmp` is not durable, and the main
checkout is currently the only other copy, so grab the patch first if you want to keep it.

**The work itself was good, which is the frustrating part.** Audited before rejecting:

- 0 blanket `# noqa`, 0 file-level `# ruff: noqa`, `pyproject.toml` untouched — no ignore-list cheat
- every suppression carries a specific code and a reason: 57 ARG002, 36 ARG001, 5 UP042, 3 F401
- UP042 correctly *suppressed*, not converted — classes remain `(str, Enum)`; the `StrEnum`
  strings in the diff are comment text in the reasons
- found **3** real F821 bugs (one more than I flagged) and fixed all three with real imports:
  `Path` in campaigns.py, `duckdb` in linter.py, and a missing `logger` in sync.py
- deleted 5 test functions — **verified legitimate**: each had 2 byte-identical copies at HEAD
  and now has 1. The −6 assert delta is exactly the 6 asserts in those duplicate blocks, so no
  test lost an assertion.

**Re-dispatched** at a worktree *outside* the repo tree (`/home/marielle/wt-agy-ruff`, branch
`agy/ruff-v2`, cut from `fix/ci-green-overnight`), with a hard `pwd` guard in the brief and a
shell-level check that refuses to start if cwd is wrong.

**Lesson worth keeping:** a git worktree is not a sandbox. It constrains *git*, not the agent —
a tool that resolves its own workspace root will happily walk up out of one. Isolation has to be
enforced by path placement outside the parent tree, or by an actual sandbox.

### D6 — agy run 2 accepted after audit; ported to the branch as `13442f5`

Re-dispatched into `/home/marielle/wt-agy-ruff` (outside the repo tree). **Containment held**:
41 files dirty in the correct worktree, 0 new files in your checkout.

Audited before accepting, because "ruff check passes" is weak evidence — the cheap ways to fake
it are blanket suppression, `pyproject.toml` ignore entries, and deleting the unused parameters
that are 64% of the batch. None were used:

| check | result |
| --- | --- |
| bare `# noqa` / file-level `# ruff: noqa` | 0 / 0 |
| `pyproject.toml` modified | no |
| suppressions, all coded + reasoned | 68 ARG002, 38 ARG001, 5 UP042, 1 F401 |
| UP042 converted to `StrEnum` | 0 — classes stay `(str, Enum)` |
| test defs removed / added | 65 / 60 → net −5 |
| asserts removed / added | 18 / 12 → net −6 |
| my earlier work intact | 3 xfail marks + `CorpusError` registration all present |

The −5/−6 deltas are the same verified-duplicate block from D5: 5 test functions that existed as
2 byte-identical copies, now 1 each, carrying exactly 6 asserts between them. Nothing lost.

**Two real bugs fixed, not suppressed:** `Path` in `campaigns.py` and `duckdb` in `linter.py`
were both used-but-never-imported — latent `NameError`s. Run 1 found a third (`logger` in
`sync.py`); run 2 correctly found only two because that one was already fixed on main by
`ac07c97`, which is a good sign the difference is real rather than a miss.

**One genuine shadowing bug:** `cli.py` defined `show` twice at module scope —
`@postmortem_app.command()`'s version was overwriting the top-level `@app.command()` one.
Renamed the function to `postmortem_show` while keeping `@postmortem_app.command("show")`, so
`bth postmortem show` is unchanged.

**Behaviour gate:** full suite on titanix, **0 failures**, matching tonight's pre-change baseline
exactly.

**Backtrack:** the whole cleanup is one commit (`13442f5`), so `git revert 13442f5` undoes it
wholesale. The suppressions are the part most worth a skim — 106 ARG noqas is a lot of "this
parameter is intentionally unused", and while each carries a reason, a human should spot-check a
sample rather than take that on faith.

### D7 — a fourth blocker: pyright. Hang fixed; the 2624 errors are YOUR call, not mine

**I told you earlier tonight that all three CI blockers were closed and CI should go green. That
was wrong.** It was true of the three tracked items, but clearing lint made a previously-invisible
fourth blocker execute for the first time.

**What happened:** the Lint step had always failed first and short-circuited the job, so
`Type check with pyright` had literally never run on this repo. Once lint passed, CI sat on that
step for 30+ minutes without completing. A local run behaved identically.

**Cause — fixed in `e61624b`:** `[tool.pyright]` set `typeCheckingMode` and `exclude` but never
set `include`. `exclude` alone does not bound analysis; without `include`, pyright walks the whole
tree from cwd — so it was type-checking `.venv/` (duckdb, pyarrow, fastmcp) and the nested
worktrees under `.claude/`. Adding `include = ["src"]` makes it terminate.

**Correction to my own diagnosis:** I first reported "pyright has no configuration at all", from a
grep that only surfaced the dependency lines. There *is* a `[tool.pyright]` section; it was
incomplete, not absent. I nearly committed a duplicate section and a `typeCheckingMode` change on
that false premise — the TOML parser's duplicate-key error caught it. **I did not touch
`typeCheckingMode`; strict was set deliberately and reversing it is not mine to do.**

**What remains, and why I stopped:** scoped strict pyright reports **2624 errors on `src` alone**.

| count | rule |
| --- | --- |
| 903 | reportUnknownVariableType |
| 636 | reportUnknownMemberType |
| 496 | reportUnknownArgumentType |
| 227 | reportUnknownParameterType |
| 196 | reportMissingTypeArgument |
| ~166 | everything else, incl. 24 reportArgumentType, 7 possibly-unbound, 6 optional-member-access |

**2458 of 2624 (94%) are `reportUnknown*` / `reportMissingTypeArgument`** — they fire because
duckdb, pyarrow and fastmcp ship little or no type information, so every value flowing out of them
is untyped. No amount of local code quality removes them. Only ~166 describe real problems.

This is a **typing-policy decision** — relax to `standard`, ignore the `reportUnknown*` family,
write stubs, or drop pyright from CI — and each is a reversal of a deliberate earlier choice.
Picking one silently at 4am is exactly the kind of call L5 should not make. Filed as backlog
**#4085** with the four options written out.

**Net:** lint ✓, full test suite ✓ (0 failures), pyright ✗ by policy. PR #38 is one decision away
from green, and that decision is yours.

---

## 2026-08-06 — resolution: CI is green

You answered D7 in one line: **"We do not use pyright in this project."** That closes #4085 by
dissolving it rather than deciding it — the four typing-policy options were all answers to a
question this project never asked. Removing the tool revealed two more never-executed steps
behind it, in the same pattern as D7 itself.

### D8 — pyright removed (`439544b`, closes #4085)

Evidence it was never used, not merely never green: pyright entered at v0.1.0 scaffolding
(`73e40a6` for the dep + config, `691a90e` for the CI step) straight from the v0.1 release plan's
template, and no run has ever reached it — ruff failed first, every time, for the repo's whole
history. There are no `# pyright:` pragmas anywhere in `src/` or `tests/`, which is what you'd
expect from a strict-mode checker nobody has ever run.

Removed the CI step, the dep from both dev lists, the `[tool.pyright]` block (including the
`include` scoping from `e61624b`, now moot), and the contributing-guide references. **This leaves
CI with no type-check gate** — adding one (astral's `ty`, say) is a separate decision I did not
make.

### D9 — pytest-cov was missing (`3562ceb`)

With pyright gone, `Run tests with coverage` executed for the first time and failed in under a
second: `pytest: error: unrecognized arguments: --cov=bathos --cov-report=term-missing`. The
workflow has always passed `--cov`; `pytest-cov` was never a dependency. Added it rather than
stripping the flags, because the contributing guide documents `pytest --cov=bathos` and names
">80% overall coverage" as a review criterion — the intent was there, the wiring wasn't. No
`--cov-fail-under` is set, so coverage reports without gating.

### D10 — CI installed no extras (`81b6fc8`)

Then the suite ran end-to-end for the first time: **15 failed, 1597 passed, 7 skipped, 3 xfailed.**
All 15 were one cause — `uv sync --dev` installs no optional extras, so scipy was absent: 10 raised
`ScipyUnavailableError` outright, and 5 asserted against a verdict that silently degrades to
`'underpowered'` without it. `tests/test_stats_gates.py` imports the scipy-backed gates unguarded,
so it assumes an `--all-extras` venv — true locally, false in CI.

Switched to `uv sync --all-extras --dev`, which also closes a quieter gap the same run exposed:
the **viz tests guard with `importorskip`, so instead of failing they had been silently skipping in
CI since they were written.** They pass once the extra is installed. Chose `--all-extras` over
`--extra stats` deliberately, so a future extra cannot repeat either failure mode; the cost is
sphinx in the test job.

### Result

`31109722314` on `81b6fc8` — every step green: **1628 passed, 3 xfailed, 79% coverage, 0 failures.**
This is the first passing CI run in the repository's history.

Also confirmed by that run: the 3 strict xfails on the cisternal path bug (#4078) held on a clean
runner — they failed as expected rather than XPASSing, so debt **#1189 reproduces outside this
machine** and stays open, correctly.

**The pattern across D7–D10 is worth naming.** Four blockers hid behind each other, each invisible
until the one before it was cleared, because a failing early step short-circuits every later one.
Steps that have never executed are not passing steps — and a CI file can look complete while most
of it has never run. The lint gate had been masking three untested steps for the repo's entire
history.

**Backtrack:** `git revert 81b6fc8 3562ceb 439544b` restores pyright and the previous install line
exactly, and re-breaks CI in the way it was broken before.

## Notes worth keeping

- The backlog dedup probe reported debt `#1077` as **absent** when adding #4080, because it only
  checks the backlog table. Same bare-`#NNN` cross-table ambiguity documented in
  `260805_deferred-item-triage-reconcile-the-handoff-backlog.md`, now reproduced in a second
  tool.
- The pre-existing loop state (`.praxia/loop_state.toml`, tombstoned 2026-06-23) points at
  `EXECUTE` for `260623_lit-parity-w4`, whose items #2222/#2223 are completed. It is stale, not
  resumable, and was not resumed.
