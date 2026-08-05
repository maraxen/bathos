---
title: 'Deferred-item triage: drain the handoff backlog and close the promotion leak'
description: Scope for a one-time sweep of 42 deferred handoff entries plus a session-close promotion step so the pile stops refilling
status: draft
task_id: 260805_deferred-triage
date: '260805'
sprint: ''
backlog_ids: ''
---
# Deferred-item triage: drain the handoff backlog and close the promotion leak

## Why

Deferred work accumulates in handoff YAML `deferred:` blocks and is carried session-to-session
by **re-description** rather than being promoted into a ranked queue. Nothing consumes it.

Measured 2026-08-05 against `.praxia/handoffs/bathos_*.yaml` (13 files):

| Surface | State |
| --- | --- |
| Deferred entries | **42** across 13 handoffs (~36 distinct by 50-char prefix) |
| `.praxia/backlog.jsonl` | **0 bytes**, dated May 15 — never written |
| Debt tracker | 4 open items |
| GitHub issues (`maraxen/bathos`) | 0 |

So ~36 distinct pieces of deliberately-deferred work exist in a store nothing ranks, dedupes,
or ages out.

**This is not a discipline failure at the authoring end.** The `deferred:` blocks are high
quality — every entry carries a `rationale` and a `recommended_phase`. The defect is that
`recommended_phase` is a triage signal with no consumer. The information needed to rank this
work has been captured all along and then dropped on the floor.

## What the corpus actually contains

Four categories fall out of the data, and they need different handling. Treating them
uniformly is the main risk to this work.

### 1. Already done, never closed (~6 of 36 — ~17%)

Confirmed by cross-referencing shipped state:

- `20260730` "Wiring the four §5 obligation triggers, the conclude-time obligation
  listing/downgrade, and the `bth submit` warning" → shipped 20260805
- `20260730` "Integration test for the `BTH_REVIEW_COVERAGE_ENFORCE` wiring seam" → shipped as
  `tests/test_review_coverage_seam.py`
- `20260730` "Build-order steps 2-5" → shipped
- `20260613` "CHANGELOG.md + CLAUDE.md version bump to v0.11.0" → CLAUDE.md now reads v0.12.0
- `20260619` "AC-20 — SHA-drift detection of parity verdict artifacts" → `check_output_sha_drift()` shipped
- `20260619` "W4 — T8 #2222 (attest-parity CLI + MCP) and T9 #2223 (Signal 13)" → both shipped

A triage that ranks before classifying puts finished work back in the queue. This category is
why premise verification is step one and not a polish pass.

### 2. Already tracked (3)

The `20260729` ruff items record `recommended_phase: tracked in debt #1077` / `#1075`. These
were promoted correctly.

**This is the most useful finding in the corpus**: the leak is not universal, and the sessions
that did it right already established the convention. Piece A copies an existing practice
rather than inventing one.

### 3. Trigger-conditioned, not priority-ranked

Several `recommended_phase` values are **conditions**, not schedule positions:

- "When a monorepo user appears" (monorepo `scan_root` override, ×2)
- "Revisit only on concrete query need" (schema-tagged worktree provenance)
- "Opportunistic" (CI grep guard for `project_config.root`)
- "After corpus v1 has been used against real sidecars" (statistics corpus batch)
- "After `outcome_failed` and `enforce` have produced real data" (`campaign_confounded` trigger)

Assigning these a priority is a category error. They are correctly deferred and will stay
correctly deferred until an event fires. Forced into a ranked queue they become permanent P3s
that inflate the backlog and train everyone to ignore it.

**They need a `condition` field and an owner event, not a rank.** How the debt tracker should
represent that is an open question below.

### 4. Genuinely live, rankable work

The remainder. Examples: the fragile script-stem key in `prereg.py:296-349` (restated **3×** —
`20260730`, `20260805`, `20260730`), the multi-worktree `bathos.db` compaction race (×2), and
the CHANGELOG backfill now logged as **debt #1173**.

### Duplication

Restated across sessions: `prereg.py` script-stem ×3 · compaction race ×2 · monorepo
`scan_root` ×2 · submit-provenance pruning ×2 · statistics corpus batch ×2 (a third near-dupe
under different wording). Dedupe must be semantic, not string-equality — the ×2 statistics
pair differs by the word "hand-authored".

## Scope

Decided 2026-08-05: **both pieces**, destination **debt tracker** (in use, working dedup probe;
the backlog surface stays unused rather than being revived).

### Piece A — close the leak

Promote `deferred:` entries into the debt tracker at session close, mapping the existing
`rationale` and `recommended_phase` fields across.

- Follow the convention the `20260729` handoff already used: the deferred entry keeps a
  back-reference (`tracked in debt #NNNN`) so the handoff stays readable on its own.
- Category-3 items must be promotable **as conditions** or explicitly held back — decide before
  building, or Piece A becomes the mechanism that floods the tracker with permanent P3s.
- Hook point candidates: extend `pretool-handoff-validate.sh`, or add a post-create step to
  `handoff(action="create")`. Not yet investigated.

### Piece B — drain the pile

1. **Write the sweep as a tracked script**, not an inline heredoc. It produces a classification
   over 42 items that will be cited and re-run; per the repo's ephemeral-scripts rule that
   makes it a persisted script, not a throwaway. **Use `yaml.safe_load_all`, not
   `yaml.safe_load`** — handoffs are written with both a leading and a *trailing* `---`, so
   YAML reads a second, empty document and `safe_load` dies with
   `ComposerError: expected a single document in the stream`. The sizing pass hit this on the
   `20260730` file. No handoff carries two real documents, so filtering to `dict` instances
   after `safe_load_all` is sufficient.
2. **Premise-verify every item before ranking.** Items date to 2026-05-15; the code has moved
   under them. praxia ships `staleness_probe` / `backlog staleness-scan --llm-extract` for
   exactly this, and the orchestration skill flags that `pm_triage` "prioritizes items without
   verifying their premise still matches reality" (idea-351). Skipping this ranks dead work
   with confident-looking priorities.
3. **Classify** into the four categories above.
4. **Semantic dedupe** across restatements.
5. **Promote** category-4 survivors to debt; close category 1; link category 2; hold category 3
   pending the condition-representation decision.

## Open questions

1. **How does the debt tracker represent a trigger condition?** Category 3 has no home today.
   Options: a `condition` field, a `blocked-on-event` status, or keep them in handoffs
   deliberately and exclude them from Piece A. Blocks the clean handling of ~5 items and shapes
   Piece A's promotion rule.
2. **Do the dangling ids resolve anywhere?** Entries reference `#791`, `#792`, `#137`, `#142`,
   `#143`, `#793-796`, `#1684` ("still OPEN" per `20260612`), `#1774`, `#2222`, `#2223`. None
   are in the debt tracker reachable from this cwd (4 items) or GitHub (0 issues). The `debt`
   MCP tool **ignored the `workspace` argument** — identical results for
   `/home/marielle/projects/bathos` and `/home/marielle/projects/praxia` — consistent with the
   documented cwd-binding, so this session could not check other stores. Resolve before
   treating any of them as dangling.
3. **Does the sweep extend to `open_questions` and unfinished `next_steps`?** Both are
   handoff-local queues with the same structural problem. Scoped out for now; revisit after
   Piece B proves the approach on `deferred:`.

## Non-goals

- Cross-project sweep. This corpus is bathos-local (13 bathos handoffs + 1 cisternal); other
  repos' `.praxia` dirs are a separate, larger job.
- Reviving `.praxia/backlog.jsonl`.
- Re-litigating any deferral. A correctly-deferred item stays deferred — the goal is that it
  is *ranked and visible* while deferred, not that it gets done.
