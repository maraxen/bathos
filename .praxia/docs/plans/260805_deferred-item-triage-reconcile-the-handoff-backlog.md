---
title: 'Deferred-item triage: reconcile the handoff backlog against the tracker'
description: Scope for a one-time sweep of 42 deferred handoff entries plus a session-close reconciliation step, after Postgres evidence showed the defect is stale handoffs rather than untracked work
status: draft
task_id: 260805_deferred-triage
date: '260805'
sprint: ''
backlog_ids: ''
---
# Deferred-item triage: reconcile the handoff backlog against the tracker

> **Revised 2026-08-05 after querying Postgres directly.** The first draft of this document
> claimed deferred work was never tracked, on the evidence that `.praxia/backlog.jsonl` was
> 0 bytes and referenced ids did not resolve. **Both claims were wrong** and are corrected
> below. The corrected diagnosis is materially different, and so is the fix.

## Why

Deferred work accumulates in handoff YAML `deferred:` blocks and is carried session-to-session
by re-description. 42 deferred entries across 13 bathos handoffs (~36 distinct by 50-char
prefix), some restated verbatim across sessions.

### What was wrong in the first draft

**`.praxia/backlog.jsonl` being 0 bytes means nothing.** The backlog is **Postgres-backed**,
not JSONL-backed. bathos's workspace (`ws_87da9d89-a4c1-4c0d-b749-caf6ad7a5e13`) holds **90
backlog rows**: 85 `completed`, 3 `cancelled`, 2 `archived`. The MCP call returning `[]` for
`status:"open"` was *correct* — there are genuinely zero open items, because the work got
done. The backlog surface is heavily used and closed out properly.

**The referenced ids are not dangling.** They resolve. Of the 12 distinct ids cited across
deferred entries, 10 point at closed work:

| id | table | status | note |
| --- | --- | --- | --- |
| 137 | backlog | cancelled | global instruction portability |
| 791 | backlog | completed | results management |
| 792 | backlog | completed | POPPER e-value campaign primitive |
| 793 | backlog | completed | structured gate error taxonomy |
| 1684 | backlog | archived | CI grep guard |
| 1774 | backlog | archived | submit-provenance pruning |
| 2222 | backlog | completed | lit-parity T8 attest-parity |
| 2223 | backlog | completed | lit-parity T9 Signal 13 |
| 1075 | tech_debt | **open** | PR #9 rebase — correctly tracked, live |
| 1077 | tech_debt | **open** | ruff cleanup — correctly tracked, live |

### The corrected diagnosis

The defect is **not** that work goes untracked. It is that **handoffs are write-once snapshots
that nothing reconciles.** An item is deferred with a valid tracker reference; the tracker row
is later completed; the handoff still says `deferred`, and the next session reads it as live
work and carries it forward. The tracker is the fresh source of truth and the handoff is a
stale mirror of it.

This is why ~6 items were independently confirmed as already-shipped by cross-referencing code
(the 20260730 trigger wiring and its seam test both shipped 20260805; AC-20; W4; the v0.11.0
bump). Those are the same phenomenon showing up without an id to check.

### A second, sharper hazard: bare `#NNN` is ambiguous and sometimes wrong

The same integer exists in **both** `backlog` and `tech_debt`, across **different workspaces**,
with unrelated content. Resolving a bare `#NNN` without naming the table and workspace retrieves
confident nonsense:

- `#143` in a deferred entry means *"threshold epistemic hygiene — sidecar lint for unjustified
  numeric cutoffs."* There is no bathos backlog 143. `tech_debt` 143 exists but belongs to
  another workspace and reads *"Session commits violate main branch protection."* The real item
  is bathos backlog **760**, `completed`.
- `#142` means *"results management — output convention, file-count utilities."* No bathos
  backlog 142. That work is backlog **791**, `completed`.
- One entry labels ids "Praxia backlog items #793-796" — they are **bathos** backlog rows.

So two entries carry ids that resolve to nothing in their own workspace while the work they
describe is completed under a different id, and one entry attributes its ids to the wrong
project. Any sweep that trusts bare ids will mis-resolve.

## What the corpus contains

| Category | Count | Handling |
| --- | --- | --- |
| Carries an id resolving to **closed** work | 13 of 15 id-bearing | Drop — mechanical |
| Carries an id resolving to **open** debt (#1075, #1077) | 2 | Keep, already correct |
| No id, confirmed shipped by code cross-reference | ~6 | Drop — needs verification |
| Trigger-conditioned, not priority-ranked | ~5 | Promote per decision below |
| Genuinely live, rankable | remainder | Promote to debt |

**Measured 2026-08-05 by running `scripts/analysis/classify_deferred_items.py`** — this
supersedes an earlier estimate here that "15 of 42 entries (36%) can be settled mechanically",
which was too optimistic. 15 entries carry an id, but resolving them splits three ways:

| bucket | n | action |
| --- | --- | --- |
| `closed_done_by_id` | 4 | drop — no judgement |
| `closed_abandoned_by_id` | 6 | drop *after confirming the abandonment was intentional* |
| `open_by_id` | 3 | keep — tracked and live |
| `id_unresolved_in_workspace` | 2 | investigate — id wrong or ambiguous |
| `needs_review` | 27 | premise-verify (15 of these flagged conditional) |

So **4 of 42 (10%) settle with zero judgement**, and 10 of 42 (24%) are drop-eligible once
abandonment is confirmed. The gap is `archived`/`cancelled`: those rows are terminal for
tracking but mean "stopped", not "done" — and one entry reads *"#1684 (still OPEN)"* while its
row is `archived`, so collapsing the two would drop an entry whose own text contradicts the
status.

Duplication (semantic, not string-equal): `prereg.py` script-stem ×3 · compaction race ×2 ·
monorepo `scan_root` ×2 · submit-provenance ×2 · statistics corpus batch ×2 (a third near-dupe
differs only by the word "hand-authored").

## Decisions

**Destination: the debt tracker** (`tech_debt`), not a revived backlog surface.

**Trigger-conditioned items use `category` + `frontmatter`** — decided 2026-08-05, options 2+1.

The schema constrains this. `status` is `CHECK (status IN ('open','in_progress','resolved'))`,
so there is no `deferred` state without a migration on a Postgres DB shared across every
project; and `priority` is `CHECK (priority IN ('P1','P2','P3'))`, so every promoted row gets a
rank whether or not ranking is meaningful. That combination is the flooding mechanism.

So:

- `category: "conditional"` — free text, already used unevenly (`docs-debt`, `lint-debt`,
  `rebase-stale-pr`), and `debt list` already returns `category`, making the filter a single
  client-side predicate.
- `frontmatter: {"kind":"conditional","trigger":"<verbatim recommended_phase>"}` — jsonb, no
  migration, preserves the condition exactly as written. Confirmed reachable: `debt.rs` exposes
  `frontmatter`, `impact`, `proposed_solution`, `related_task_id`, and `backlog_id` on the add
  request.
- Where the trigger is *itself tracked work* (e.g. "after corpus v1 has been used against real
  sidecars"), also set `backlog_id` so it reads as blocked-by rather than merely conditional.
  This does not generalise — "when a monorepo user appears" has no row to depend on.

Chosen for reversibility: strictly additive, no migration. If consumers keep forgetting the
filter and the queue floods anyway, that is cheap evidence that migrating `valid_debt_status`
is worth its blast radius. Spending the migration first would be paying before knowing.

## Scope

### Piece A — stop handoffs going stale

Revised from "promote deferred items" — most already have tracker rows, so promotion was the
wrong verb.

1. **At session close:** every `deferred:` entry must carry a resolvable reference —
   `{table, workspace_id, id}`, not a bare `#NNN`. Create a tracker row when none exists;
   otherwise record the existing one. The `20260729` handoff already did this informally
   (`recommended_phase: tracked in debt #1077`), so the convention exists and needs only to be
   made explicit and machine-readable.
2. **At session start:** resolve every carried-forward reference and **report** entries whose
   row is `completed`/`cancelled`/`archived`/`resolved`. This is the step that does not exist
   today and is the direct cause of the pile.

   **Advisory, not automatic** — decided 2026-08-05. It reports proposed drops; a human
   decides. The staged path to automatic dropping is tracked as **debt #1179**, gated on
   measured agreement rather than elapsed time.

   The reason is asymmetric failure modes. A false *keep* is benign — a stale entry survives
   one more session, which is exactly today's status quo. A false *drop* silently deletes live
   work from the only record carrying its rationale, and the handoff is what the next session
   trusts. The `#142`/`#143` hazard makes that concrete: a naive resolver mis-resolves with
   full confidence. So the gate is tuned against false-drop, not overall accuracy.

   This is also the pattern every gate in this repo already follows — opening is observation,
   downgrading is enforcement, and enforcement turns on behind its own flag once real data
   exists (`review_coverage_enforce`, the four obligation triggers, the `[obligations]` /
   `[claim]` config precedent).

### Piece B — drain the existing pile

1. **Write the sweep as a tracked script**, not an inline heredoc — it produces a
   classification over 42 items that will be cited and re-run. **Use `yaml.safe_load_all`, not
   `yaml.safe_load`**: handoffs carry both a leading and a trailing `---`, so YAML sees a
   second empty document and `safe_load` dies with `ComposerError: expected a single document
   in the stream` (hit on the `20260730` file). No handoff holds two real documents, so
   filtering to `dict` after `safe_load_all` suffices.
2. **Mechanical pass:** resolve the 15 id-bearing entries against Postgres, scoped by
   `workspace_id` **and** table. Drop the closed ones. No judgment required.
3. **Premise-verify the remainder.** Items date to 2026-05-15 and the code has moved under
   them. praxia ships `staleness_probe` / `backlog staleness-scan --llm-extract` for this, and
   the orchestration skill notes `pm_triage` "prioritizes items without verifying their premise
   still matches reality" (idea-351).
4. **Semantic dedupe** across restatements.
5. **Promote survivors** to `tech_debt`, applying the `conditional` convention above.

## Sequencing note: Piece B is Piece A's test fixture

Do **B before A's stage 2**. Piece B's one-time sweep classifies all 42 existing deferred
entries by hand — and that classification *is* the labelled ground-truth set for validating
automatic reconciliation. Run B first and the shadow-mode comparison in debt #1179 gets a real
test corpus for free; skip it and stage 2 has nothing to measure agreement against.

This is the cheapest evidence available for the rollout, and it is a by-product of work already
scoped.

## Open questions

1. **Do the other 27 non-id-bearing entries deserve retroactive ids?** Cheap for the ~6 already
   shipped (just close them), less obvious for genuinely live work.

## Non-goals

- Cross-project sweep. This corpus is bathos-local (13 bathos handoffs + 1 cisternal).
- Migrating `valid_debt_status`. Held in reserve pending evidence that 2+1 is insufficient.
- Re-litigating any deferral. A correctly-deferred item stays deferred; the goal is that it is
  ranked and visible while deferred, not that it gets done.
