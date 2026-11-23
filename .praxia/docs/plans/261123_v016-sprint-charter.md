---
title: 'v0.16.0 Sprint Charter: Workspace Rebase, Ready Branches, Silent Integrity Gates & C2 Catalog Hygiene'
description: Locked sprint scope, sequencing, deferrals, and mechanized process controls derived from 2026-11-23 praxia backlog/debt/lessons audit and ground-truth repo verification.
status: completed
task_id: 261123_v016-sprint-charter
date: '261123'
sprint: 'v0.16.0'
backlog_ids: '6687,6688,4379'
---
# v0.16.0 Sprint Charter: Workspace Rebase, Ready Branches, Silent Integrity Gates & C2 Catalog Hygiene

**Date:** 2026-11-23 · **Workspace:** `bathos` (`ws_87da9d89-a4c1-4c0d-b749-caf6ad7a5e13`) · **Target:** `v0.16.0`

## 1. Observed Baseline (2026-11-23)

- **Repository Ground Truth:**
  - Local `main` (`893634e1`, `v0.13.0a9`, 2026-10-30) is **192 commits / 188 files behind** `origin/main` (`ddcb3456`, `v0.15.0`, 2026-11-13), which already shipped `v0.13.1`–`v0.13.3` (non-git project binding & strict unaffiliated refusal), `v0.14.0` (Result Validity, Supersession & Archive), and `v0.15.0` (Bearings v1, `cisternal>=0.1.7`).
  - Local `main` carries 79 modified/untracked paths (100% under `.praxia/`) and **42 registered git worktrees** (several locked with uncommitted edits per `.praxia/docs/audits/261022_locked-worktrees-uncommitted.md`, plus uncommitted edits to `CHANGELOG.md` and `agent_assets/skills/*` in `bathos-validity`).
  - Unmerged branches ahead of `origin/main`: `feat/6687-stoichiometry-and-citations` (`9bd918f2`, 1 commit ahead — implements `#6687` and `#6688`) and `wt/261109_progress-report-skill` (`34ded18f`, 3 commits ahead).
  - `AGENTS.md` is stale (`v0.12.0 — 918 tests` vs 4,490 collected tests across 266 files on local `main` and 284 top-level `tests/test_*.py` files on `origin/main`).
- **Praxia Tracker State:**
  - **Backlog (Postgres):** 118 total (106 completed, 7 open, 3 cancelled, 2 archived) + 5 cross-workspace open items targeting bathos + 4 staging items (`#244`, `#245`, `#137`, `#95`) + 2 ideas (`idea-001`, `idea-002`). Most "open" items (`#6527`, `#5607`, `#5606`, `#6456`, `#2829`, staging `#244`/`#245`/`#95`, `idea-001`) are already shipped or superseded on `origin/main`.
  - **Tech Debt (Postgres + `.praxia/deferred_resolutions.yaml`):** 172 total (**62 open**, 110 resolved, 0 in_progress; 2 P1, 33 P2, 16 P3, **11 unprioritized**; median age 37.7d), plus **11 active `kept_live`/`kept_conditional` deferred entries** with `promoted_to: null`. `#3210`, `#3211`, and `#2945` are already shipped; `#4122` (`ty` diagnostics) grew from 51 to **81 diagnostics** (76 warnings + 5 optional-extra `unresolved-import` errors).
- **Telemetry & Lessons:**
  - `telemetry.jsonl`: **88.2%** (879/997) `subagent_stop` rows `unparsed`; **47.8%** (75/176) record-expected dispatches filed.
  - `admission_log.jsonl` & `dispatches.jsonl`: **46.1%** (95/206) admission divergence (`mode='warn'`); **19.4%** (40/206) `cli_status='timeout'`; **26/206 unjoined mints** (**19/26 `praxia:fixer`**).
  - `agent_results.jsonl`: **12.2%** (12/98) rejected on schema constraints (`summary <= 400`, relative claim paths, repo-registered worktrees). `dispatch_runs.jsonl`: 4/19 failed/timed out (including `agy` exiting `0` despite denied tools).
  - `handoffs.jsonl` and `daily.jsonl` stopped updating after `2026-08-28`; `.praxia/pending_kb_ingest.jsonl` holds 6 `lesson_id: "unknown"` entries.

---

## 2. Locked Scope & Sequencing

### Phase 0 — Workspace Sync, Worktree Cleanup & Praxia Hygiene
1. **Git Sync & Worktree Cleanup (`tech_debt #2817`):**
   - Preserve unique local `.praxia/` entries, fast-forward local `main` cleanly to `origin/main` (`ddcb3456`, `v0.15.0`), and reconcile local `.praxia/`.
   - Audit `bathos-validity` (`CHANGELOG.md`, `agent_assets/skills/*`) and the 8 locked worktrees from `.praxia/docs/audits/261022_locked-worktrees-uncommitted.md`; salvage any unique unmerged work and prune dead/merged worktrees.
2. **Praxia Hygiene & `AGENTS.md` Refresh:**
   - Close verified-shipped rows: backlog `#6527`, `#5607`, `#5606`, `#6456`, `#2829`; staging `#244`, `#245`, `#95`; `tech_debt #3210`, `#3211`, `#2945`; reconcile `idea-001`.
   - Prioritize the 11 unprioritized `tech_debt` rows in Postgres.
   - Reconcile the 11 untracked active rows in `.praxia/deferred_resolutions.yaml` using qualified `{table, workspace_id, id}` references.
   - Refresh `AGENTS.md` header to `v0.15.0` and accurate test count.

### Phase 1 — Land Ready Branches & Quick Integrity Wins
1. **Review, verify & merge `feat/6687-stoichiometry-and-citations` (`9bd918f2`):** Covers `backlog #6687` & `#6688`; clarify scope overlap with `tech_debt #2345` (coordinate/quote verification vs numeric metric verification).
2. **Review, verify & merge `wt/261109_progress-report-skill` (`34ded18f`):** `bathos-progress-report` skill (3 commits).
3. **Literature-parity skill smoke tests (`backlog #4379`):** Add `tests/test_lit_parity_skill.py` covering `agent_assets/skills/bathos-literature-parity/` and `plugin/skills/bathos-literature-parity/`.
4. **Silent `blast-status` false `'clean'` (`tech_debt #3563`):** Fix `src/bathos/blast_radius.py` & `src/bathos/mcp.py` returning `'clean'` on `'unverifiable'` rows / swallowed `duckdb.Error`.
5. **Stepback trigger fixes (`tech_debt #3236`):** Fix `src/bathos/stepback/triggers.py` `evaluate_entropy_collapse` firing false `"Unproductive rut"` on `observed`/exploratory windows and add a progress check to `evaluate_campaign_staleness` Rule 3.

### Phase 2 — Silent Gate Fixes & C2 Catalog/Sidecar Integrity
1. **Postmortem-overridden verdict flagging (`tech_debt #3494`):** Detect/flag decision docs citing bathos verdicts later corrected by a postmortem override (integrating with `v0.14.0` `postmortem.py`/`supersession.py`/`validity.py`/`doc_store.py`).
2. **Silent `outcome=unknown/error` & result-schema validation (`tech_debt #3326`, `#3327`):** Eliminate silent exit-0 on `outcome=unknown/error` in `src/bathos/runner.py` and validate emitted result JSON against `result_schema`/conditions in `src/bathos/validate.py`.
3. **C2 — Catalog & Sidecar Integrity:**
   - `tech_debt #3051`: Repair/prevent orphaned `index.db campaign_runs` rows referencing missing `campaigns` rows / 8-char prefix mismatches (`campaigns.py`, `runlog/`, `verify.py`).
   - `tech_debt #3118`: Freeze campaign member sidecars in catalog at registration so pruning `/tmp` worktrees does not lose them (`campaigns.py`, `prereg.py`, `sidecar.py`).
   - `tech_debt #2819` remainder: Address `runlog/ingest.py` `lock_timeout_s` / ingest-vs-migrate concurrency handling and resolve `src/bathos/repair.py:723` (`TODO: backup_warm`).

### Explicitly Deferred
- `#5701` (cross-workspace run-log cutover — deferred until `#3051`/`#2819` land).
- C1 (`#3323` `real_home_guard.py`, `#3052` `bth verify` perf).
- Spec `260707_scientific-validity-gates.md` (`#2202`–`#2199`, `[invariants]`/`[sanity]`) — candidate anchor for next sprint.
- `#4122` (81 `ty` diagnostics cleanup), `#6515` (`[figures]/[review]` stanza collision), `#2663` (Rust/PyO3), `idea-002` (blocked on metascience corpus), and conditional deferred items (`tev-design-review-content`, `campaign-confounded-enable`, `stats-corpus-batch`).

---

## 3. Sprint Process Controls

1. **Pre-Dispatch Sync & Capability Pre-Flight:** Branch only after `main == origin/main`; keep external-agent worktrees outside the repo; verify tool capabilities before launch; treat headless runs logging `agy_denied_action` as failed even when exiting `0`.
2. **Admission & Concurrency Discipline:** Do not flip admission waist from `mode='warn'` to hard-block until `cli_status='timeout'` (19.4%) is resolved; cap concurrent `praxia:fixer` dispatches at `<=2` per task; route multi-file/interlocking gate specs to Sonnet+.
3. **Structured Self-Report Briefs:** Include exact `agent_results` schema constraints (`summary <= 400 chars`, repo-relative claim paths, registered worktree path, valid verdict enum) in every worker brief.
4. **Mechanized Gate Verification:** Every new/fixed lint or gate rule (`#3563`, `#3236`, `#3494`, `#3326`, `#3327`) must include an untruncated call-site grep and a negative/mutation test proving the check fires (never `skipif` for live defects; use `xfail(strict=True)` if deferred).
5. **Two-Tier Test Cadence:** Workers run targeted `pytest` + `ruff` + `ty` on touched files; run one full `uv run --all-extras pytest` + full CI sweep at integration without short-circuit masking.
6. **Sprint Close-Out:** Emit qualified `{table, workspace_id, id}` entries to `.praxia/daily.jsonl` and `.praxia/handoffs.jsonl`, flush `.praxia/pending_kb_ingest.jsonl`, and update this charter with outcomes.

---

## 4. Entry & Exit Gates

- **Entry Gate (Phase 0 complete):** `main == origin/main` (`ddcb3456`), locked worktrees audited/pruned, shipped Praxia rows closed, 11 unprioritized `tech_debt` items prioritized, `.praxia/deferred_resolutions.yaml` reconciled.
- **Exit Gate (Sprint complete):** All Phase 1–2 commits merged cleanly on local `main`, full `uv run --all-extras pytest` + `ruff` + `ty` verified on touched modules, Praxia rows closed, `AGENTS.md` updated, and close-out `daily.jsonl` / `handoffs.jsonl` written (no `git push` unless explicitly requested).

---

## 5. Outcomes (Updated at Close-Out)

**Closed 2026-11-23** on local `main` at `8dc54821` (13 commits stacked on `ddcb3456`, `v0.15.0`).

### Landed Commits (`ddcb3456..8dc54821`)
- `4d9dc016`: `feat(bathos): add stoichiometry stage-graph, reagent amortization, and strict citation verification` — `backlog #6687`, `#6688`
- `fc775c00`, `f5907853`, `a3aec9fa`, `16ab37fa`: `wt/261109_progress-report-skill` (`bathos-progress-report` skill, manifest registration, bundle regeneration, smoke tests)
- `012b6f98`: `test(parity): add literature-parity skill smoke tests (#4379) and refresh AGENTS.md` — `backlog #4379`
- `e0cf855f` + `6574f8de`: `fix(blast-radius)` false `'clean'` fix + AC-18 `connect_read` fallback fix — `tech_debt #3563`
- `8c88cf3a`: `fix(stepback): suppress entropy_collapse on observed/exploratory windows and require progress in staleness rule 3 (#3236)` — `tech_debt #3236`
- `a42fe7ba`: `feat(doc_store): flag decision docs citing postmortem-overridden runs (#3494)` — `tech_debt #3494`
- `a33b23ba`: `fix(runner,validate): surface unknown/error outcomes and validate result JSON (#3326, #3327)` — `tech_debt #3326`, `#3327`
- `f6e0ddbc` + `8dc54821`: `fix(catalog)` reconcile orphan `campaign_runs`, freeze member sidecars, harden `runlog/ingest.py` locks + implement `repair.py` `backup_warm` (removed `TODO`), and register campaign row before `compact` in `tests/test_discriminability_ingest.py` — `tech_debt #3051`, `#3118`, `#2819`

### Verification Summary
- `ruff check src/ tests/` -> `All checks passed!` (exit `0`)
- `ty check src/` -> exit `0` (`101` pre-existing warnings, `0` errors)
- Full `--all-extras` pytest on `6574f8de` (5,152 collected tests across 289 files, 8 shards): `5,118 passed, 32 skipped` (`30` `BTH_AC17_SWEEP=1` opt-in, `2` `myxcel` not installed)
- Both remaining items from `6574f8de` verified passing:
  - `tests/test_discriminability_ingest.py` fixed in `8dc54821` (`45/45` passed)
  - `tests/test_top_level_cli_cyclopts.py::TestRunCyclopts::test_missing_script_propagates_subprocess_exit_code` verified passing in writable-`.git` `workspace:share` (sandbox failure was read-only `.git/` artifact)

### Praxia Tracker Updates (`ws_87da9d89-a4c1-4c0d-b749-caf6ad7a5e13`)
- **Phase 3 `backlog` -> `completed`:** `#6687` (`4d9dc016`), `#6688` (`4d9dc016`), `#4379` (`012b6f98`)
- **Phase 3 `tech_debt` -> `resolved`:** `#3563` (`e0cf855f`, `6574f8de`), `#3236` (`8c88cf3a`), `#3494` (`a42fe7ba`), `#3326` (`a33b23ba`), `#3327` (`a33b23ba`), `#3051` (`f6e0ddbc`, `8dc54821`), `#3118` (`f6e0ddbc`), `#2819` (`f6e0ddbc`)
- **Phase 3 `tech_debt` updated (kept `open`):** `#2345` — scope narrowed to record that `#6688` (`4d9dc016`) covers `.numbered.txt` literature blockquote + coordinate verification (`verify_citations --strict-coords`), while `#2345` retains numeric metric & code/data `file:line` verification
- **Phase 0 hygiene (prior close-out):** `backlog #6527`, `#5607`, `#5606` -> `completed`; `backlog #6456` (`ws_bfbf2e90-8642-4a2f-abf6-f2c26c61a883`) -> `cancelled`; `tech_debt #2829`, `#3210`, `#3211`, `#2945` -> `resolved`; 11 unprioritized `tech_debt` rows prioritized; `idea-001` -> `completed`; `.praxia/deferred_resolutions.yaml` reconciled (40 rows)
- **Kept `open`:** `tech_debt #2817` (`P3`, 38 worktrees audited safe-to-prune)

### Manual Follow-Ups
1. **`tech_debt #2817` disk worktree prune:** 38 dead/merged worktrees audited safe-to-prune during Phase 0; execute disk prune unsandboxed with user confirmation.
2. **Staging `#244` & `#245`:** Triage/purge via `praxia-mcp` (`praxia staging` CLI lacks triage/purge subcommands; `#95` already `triaged: true`).
3. **`git push`:** Left to user — local `main` is at `8dc54821` (13 commits ahead of `origin/main` `ddcb3456`).
4. **`.praxia/pending_kb_ingest.jsonl`:** Contains 6 legacy `lesson_id: "unknown"` rows (`2026-06-01` / `2026-07-30`); `praxia knowledge` has no queue-flush command (`ingest` requires missing `.praxia/docs/actuation_surfaces/surfaces.yaml`), so file was left intact for manual triage/cleanup.
