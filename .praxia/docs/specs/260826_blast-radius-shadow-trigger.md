---
title: Blast-radius shadow-mode git-hook trigger
task_id: 260826_blast-radius-shadow-trigger
date: 260826
status: draft
brainstorm_session: true
invest_overrides: []
---

# Blast-radius shadow-mode git-hook trigger

## Problem

Backlog #4555 (split from #4552, Phase 2b): an event/git-hook-based auto-suggestion that
logs what blast-radius assessment WOULD have flagged, without writing a durable affecting
record — calibrating hook-trigger reliability before ever letting it act (spec Decision
Log #6, `260826_blast-radius-assessment-skill.md`). This is the first OS-level integration
bathos has ever done (confirmed: no existing hook-installation code anywhere in the
codebase, `init_project` only writes `.bth.toml` and a cluster env script), so it needed
its own design pass rather than being folded into Phase 2a's pure-Python scope.

## Decision Log

| # | Question | Decision | Rationale |
|---|---|---|---|
| 1 | Trigger event | `post-commit`, filtering on that commit's own message against a fix-like keyword pattern | `commit-msg` runs before the commit object exists (no SHA yet); `post-commit` has full access to both the finalized SHA and the message via `git log -1 --pretty=%B HEAD` |
| 2 | Keyword pattern | Hardcoded default regex (`fix`, `bug`, `regression`, `hotfix`, etc., case-insensitive), not configurable via `.bth.toml` yet | User: prove the trigger before building configurability |
| 3 | Install mechanism | `git config core.hooksPath` pointed at a bathos-managed directory (`.bth/hooks/`), NOT direct writes to `.git/hooks/post-commit` | Avoids clobbering an existing `post-commit` script outright; `core.hooksPath` is the git-native mechanism other tools (husky, pre-commit framework) also use |
| 4 | Preserving existing hooks | The managed directory **wraps, never replaces**: every hook name other than `post-commit` present in the previously-active hooks location is symlinked through unchanged; `post-commit` specifically chains to the previously-active `post-commit` (if any) before running bathos's own logic. The previous `core.hooksPath` value (or "was unset, defaulted to `.git/hooks`") is recorded so `uninstall-hook` can cleanly restore it | User: "can we somehow symlink or wrap any existing config path so if it's set by another tool it's preserved" — explicit requirement, not a nice-to-have |
| 5 | Performance / blocking | The installed hook script does the keyword check inline (cheap) but spawns the actual assessment work as a **detached background process** — `git commit` must return immediately regardless of catalog size | User's own pre-mortem pick: the most likely real failure is "slows down every fix-labeled commit" (over "broke another tool's hooks" or "log nobody reviews"), so latency was treated as the primary design constraint, not an afterthought |
| 6 | Shadow-firing storage | Reuse `blast_radius_ledger` (same table from Phase 1/2a): new `entity_type="shadow_trigger"`, `entity_id=<commit_sha>`, a 4th `to_state="shadow_only"` value distinct from affected/unverifiable/cleared | Steelmanned alternative (a separate lightweight JSONL log) loses on avoiding a *third* near-identical append-only-ledger implementation on top of the duplication code-review already flagged (debt #1474); composite-key design from Phase 1 already supports new entity_type buckets with zero collision risk against `entity_type="run"` |
| 7 | Never durable, never gating | The shadow-trigger path NEVER calls `flag_blast_radius`/`propagate_to_campaigns`/`propagate_to_claims` — it only ever writes `shadow_trigger` records, which are inert to `fold_blast_radius_state(catalog_dir, "run", ...)` and all other real-entity reads (different entity_type namespace, different to_state) | Same "shadow means shadow" discipline as AC-10's auto-clear verdict — the whole point is calibrating trigger reliability without any risk of a false positive polluting real ledger state |

## Acceptance Criteria

- SAC-1: Given a repo with no `core.hooksPath` set, when the hook is installed, then `core.hooksPath` is set to the bathos-managed directory, every pre-existing hook file in `.git/hooks/` other than `post-commit` is reachable unchanged (symlinked) from the managed directory, and a pre-existing `.git/hooks/post-commit` (if any) is chained-to by the new managed `post-commit` script.
- SAC-2: Given a repo with `core.hooksPath` already set to some other directory, when the hook is installed, then that directory's hooks remain reachable (same wrap/chain treatment as SAC-1, sourced from the previous `core.hooksPath` location instead of `.git/hooks/`) and `core.hooksPath` is repointed to the bathos-managed directory only after that reconciliation succeeds.
- SAC-3: Given the hook is installed, when `bth blast-radius uninstall-hook` runs, then `core.hooksPath` is restored to its exact pre-install value (or unset, if it was unset before), and the bathos-managed hooks directory is removed.
- SAC-4: Given a commit whose message matches the keyword pattern, when the installed `post-commit` hook fires, then it spawns the shadow-assessment work as a detached background process and the hook script itself exits immediately (no synchronous `assess_blast_radius` call in the hook script's own process).
- SAC-5: Given a commit whose message does NOT match the keyword pattern, when the hook fires, then no shadow assessment is attempted at all (not even in the background) and no ledger record is written.
- SAC-6: Given the background shadow-assessment process completes, when it finds affected/unverifiable runs for that commit (treated as a `commit` anchor against its own parent, same semantics as `assess_blast_radius(commit=...)`), then a single `blast_radius_ledger` record is appended with `entity_type="shadow_trigger"`, `entity_id=<commit_sha>`, `to_state="shadow_only"`, and the would-be affected/unverifiable run IDs recorded in `matched_files`/`match_reason` (repurposed fields — see TBD below on whether a dedicated field is worth adding).
- SAC-7: Given any number of shadow-trigger records exist, when `fold_blast_radius_state(catalog_dir, "run", <any run id>)` or the campaign/claim equivalents are read, then shadow-trigger records never appear in or influence that result (different entity_type namespace).
- SAC-8: Given shadow-trigger records exist, when a review command lists them, then the output is ordered and shows commit SHA, matched keyword, and the would-be affected/unverifiable run counts — a real, usable calibration surface, not just inert rows in a table nobody queries.

## Assumptions

- `git config core.hooksPath` (repo-local, not `--global`) is respected by the git version bathos already assumes elsewhere (no new minimum-git-version constraint identified, but not independently re-verified here).
- A `post-commit` hook can never block or fail a commit (the commit object already exists by the time it runs) — this is why performance is the correct primary risk to design against, not correctness-of-blocking.
- `git log -1 --pretty=%B HEAD` reliably returns the just-made commit's full message from within a `post-commit` hook's working directory context.

## TBDs (deferred to implementation)

- Exact background-spawn mechanism (`nohup ... &` inside the hook's POSIX shell script vs. a Python-level `subprocess.Popen` with `start_new_session=True`) — decide during implementation, whichever is more portable/testable.
- Whether `matched_files`/`match_reason` (repurposed from the run/campaign/claim shape) are sufficient for SAC-6, or a shadow-trigger-specific field is worth adding (e.g. `shadow_verdict` already exists on the table and could hold the structured would-be-report instead of overloading `match_reason`).
- Exact CLI surface for install/uninstall/review (`bth blast-radius install-hook` / `uninstall-hook` / `shadow-log`, naming TBD at implementation time).
- Windows/non-POSIX-shell support is out of scope for this round (bathos's existing hook-adjacent scripts, e.g. `_bth_env.sh`, are already POSIX-shell-only).

## Pre-mortem Record

**Named risk (user-selected as most likely): performance.** `assess_blast_radius` running
synchronously inside the hook on a large catalog would add a noticeable pause after every
fix-labeled commit. Two alternatives (breaking another tool's hooks; an unreviewed shadow
log) were named and set aside as less likely, though the wrap/chain design (Decision #4)
already defends against the first regardless.

**Mitigation baked into scope:** SAC-4 makes backgrounding a hard acceptance criterion, not
an optimization to consider later — the hook script's own process must exit immediately.

**Residual risk accepted:** a detached background process's own errors (e.g. a corrupt
catalog) are invisible to the user at commit time by design (that's the point of not
blocking) — this trades "no noise on every commit" for "a broken shadow trigger fails
silently until someone checks the review surface." Acceptable for a feature whose whole
purpose is calibration before being trusted, not correctness-critical from day one.
