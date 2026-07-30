---
task_id: 260616_worktree-prune
session_id: f4c42a66-27a1-4866-80e3-65ffcd33570a
workspace: bathos
status: complete
phase: Maintenance / post-Phase-2 cleanup
created: 260616
---

# Handoff — Worktree Prune & Backlog Assessment

> Written to file because the praxia MCP server's working directory was the
> active worktree (`wt-20260616-193211`) deleted during this session; the
> `mcp__praxia__handoff` write failed with ENOENT and the server disconnected.
> Re-handshake praxia to `/home/marielle/projects/bathos` next session and
> (optionally) replay this into the handoff store.

## Goal

Consolidate the bathos repo to a single `main` worktree after claim-tier
Phase 2 shipped, and assess what bathos work remains open.

## Summary

Claim-tier Phase 2 (AC-04/05/06/12/13/15) was **already shipped and merged to
main** before this session (merge `755d456`, HEAD `ead8c3a`, 825 tests passing /
4 skipped). This session was maintenance:

- **Pruned the full worktree sprawl** — removed ~44 merged worktrees + branches
  across `~/worktrees/bathos` (35 stale per-backlog sprint tracks) and
  `.claude/worktrees` (`compact-migration-fix`, `controls-spec`,
  `wt-20260613-151553` + nested child, `wt-20260616-183738`).
- **Merged-and-pruned the active session tree** `wt-20260616-193211` and its 4
  nested children (`-214053`, `-214053-1`, `-214147`, `-215100`). All five were
  already ancestors of main — **no real merge was needed**, only pruning.
- **Cleaned orphan remnants** — `claude-md-update` (stale `settings.local.json`)
  and `wt-20260527-184825` (a superseded *Draft* of `260527_telemetry-design.md`;
  main already holds the *Approved* version). No unique content lost.
- **Result:** repo is now a single `main` worktree and a single `main` branch.

## Backlog assessment

Open backlog had 5 items; the bathos feature backlog is **effectively drained**:

| ID | Title | Pri | Owner |
|---|---|---|---|
| 137 | Global instruction portability | P2 | praxia (deferred) |
| 794 | PostToolUse NLM hook coverage | P3 | praxia |
| 796 | Loop step budget enforcement | P3 | praxia |
| 1684 | CI grep guard `project_config.root` | P3 | praxia |
| **1774** | **submit-provenance pruning/compaction** | **P3** | **bathos** |

Claim-tier (Phase 1+2), controls discipline (#1708-1717), repair MVP
(#1330-1337), and the #793 structured-gate-error-taxonomy epic (#1810-1820) are
all merged. **No P1/P2 bathos work remains.**

## Next steps

1. Only **#1774** (submit-provenance pruning/compaction path, P3) remains
   genuinely bathos-side — pick up if/when prioritized.
2. **#137** (global instruction portability, P2) is deferred to praxia;
   **#794 / #796 / #1684** are praxia infra, not bathos.
3. Optional: regenerate or discard the pre-existing modified `report.html` in
   the main checkout (regenerable via `bth export --html`; it was dirty before
   this session and is not my change).
4. Re-handshake praxia MCP to `/home/marielle/projects/bathos` at session start
   (it disconnected this session after its worktree cwd was deleted).

## Immediately relevant

- `CLAUDE.md` (lines ~13-22) — Current Status block: v0.11.0, 825 tests,
  claim-tier shipped; no stale Open Backlog section.
- `src/bathos/claim.py` (lines ~182-290) — AC-13 `reference_parity` three-state
  dispatch + AC-04/05 advisory warnings (warnings do NOT set `ok=False`).

## Deferred

- **#1774** submit-provenance pruning / compaction path — P3, not on the
  critical path; bathos backlog otherwise drained. Recommended: future bathos
  sprint.

## Open questions

None.
