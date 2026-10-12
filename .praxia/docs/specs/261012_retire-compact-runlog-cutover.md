---
title: Retire bth compact latency via the runlog cut-over, with an interim get_run stopgap
description: Do not build an incremental legacy compact; finish delivery step 5 of the project-local run log (260925) behind its blockers, and add a cool-fragment fallback to get_run meanwhile (debts 2344, 2110, 1685, 1647, 1993)
status: draft
task_id: 261012_incremental-compact
date: '261012'
backlog_ids: ''
adversarial_review: ''
---
# Retire `bth compact` latency via the runlog cut-over, with an interim `get_run` stopgap

## Problem

`bth compact` is slow, and the global workflow depends on running it often.

- **Readers never consult the cool tier once `bathos.db` exists.** `query.py:171`
  `_resolve_backend` returns `warm` whenever the file exists, so a just-finished run is invisible to
  `get_run`/`list_runs`/`find_runs` until someone compacts (debt #2110; false data-loss reports
  #843, #1816, #1926).
- **Each compact costs time proportional to total history, not to what's new.** `compact.py:782`:
  - `read_runs_report` (`catalog.py:68`) `pq.read_table`s every fragment, one file at a time. That
    was 5,868 fragments across every project on 2026-10-12.
  - It then does a per-row `SELECT` and `UPDATE`s for every existing run (`compact.py:959-1041`).
  - It re-stats every output path of every existing run (`compact.py:964-993`).
  - It walks the workspace for postmortems and runs about 30 `ALTER TABLE` statements.
  - It appends a `_schema_migrations` row on every call (`compact.py:1196`), so that table grows
    without bound.
  - Result: compact runs past the 120 s foreground limit (debt #2344).
- **Existing rows are frozen except for a few columns**, so rewritten cluster fragments never update
  `status`/`outcome` (#1647, #1685). Users compact again, and it doesn't help.

## Decision: do not build an incremental legacy compact

The obvious fix (an ingest manifest plus upsert of only changed fragments) is rejected:

1. **It is superseded.** The project-local run log spec
   (`.praxia/docs/specs/260925_project-local-run-log.md`, rev v41) replaces compact with an
   append-only log and a disposable folded index. Its read path, "Reads (G3)", re-folds only the
   delta past each log's watermark: "Cost scales with the unindexed delta, not the index size."
   That is exactly the property an incremental compact would add. Delivery steps 1–4 are built
   (`src/bathos/runlog/`, `migrate.py` ~2,480 lines, MCP `migrate_to_log`). Only step 5, the
   real-catalog cut-over, remains.
2. **It would move the migration's reference point.** Runlog AC-17 diffs the fold against the
   *canonical* legacy state (`compact(force_rebuild=True)` plus the reap-ledger merge). The spec
   says explicitly that an incremental legacy compact is not a usable reference
   (260925 §Fold rules). Changing the legacy compact's semantics now would add new residual
   classes to Migration step 3 for no lasting benefit.
3. Any work on the legacy write path is thrown away at cut-over (step 5 empties the AC-18
   allow-list).

The remaining work is therefore (A) a small read-side stopgap that survives until cut-over, and
(B) the cut-over itself.

## Assumptions

| # | Assumption | Tag |
|---|---|---|
| S1 | Runlog delivery steps 1–4 are built and tested against fixture catalogs | *partially verified*: modules and MCP tool exist (`src/bathos/runlog/`, `mcp.py`); test pass status on main not re-run here |
| S2 | No cut-over has happened on this machine | *verified*: `~/.bth/catalog/cutover.json` absent, 2026-10-12 |
| S3 | `bth migrate --to-log --dry-run` writes no data files and does not pull remotes | *verified from the spec* (260925 "Dry run", v38 correction); *unverified in code* |
| S4 | Cool fragment file names are `runs/<slug>/run_<id>.parquet`, written atomically via tmp+rename | *verified*: `catalog.py:38-47` |
| S5 | `get_run` takes exact ids only (no prefix match) | *verified*: `query.py:369` |
| S6 | Bathos's cluster run-log pull (`bth sync --pull`) has never worked | *reported* by debt #1993; *unverified* here. If true, it blocks B |
| S7 | Debt #2012 (writers.lock waits print nothing) was fixed by PR #84 (`5443be69`) | *unverified*: close or re-scope the debt after checking |
| S8 | Cut-over latency meets the runlog AC-12 budget on the real catalog | *unverified*: measured in B4 under a bathos sidecar |

## Phase A: interim `get_run` cool fallback (legacy path, small)

**Change:** in `query.py` `_warm_get_run`, when the warm query returns no row and `run_id`
contains no glob metacharacters, check `catalog_dir/runs/*/run_{run_id}.parquet`. If exactly one
file exists, read it with `pq.read_table`, apply `_apply_migrations`, and return the `Run` with
`metadata["_source"] = "cool_unindexed"`. Emit `catalog.query` with
`query_kind="get_cool_fallback"`.

- Reads one file. It needs no DuckDB connection and no lock, so it doesn't conflict with AC-18
  (which targets `duckdb.connect` calls).
- `bth show`/MCP `get_run` render a one-line notice when `_source == "cool_unindexed"`: "not yet
  compacted; fields computed at compact (output_metadata, postmortem overrides) are absent".
- Out of scope for A: `list_runs`/`find_runs` fallback (that's the whole-tree scan this spec avoids),
  and any change to `compact.py`.

**Acceptance (A):**

- A-1: with a populated warm DB, a fragment written after the last compact is returned by
  `get_run`, with `_source == "cool_unindexed"`. A run present in warm is returned from warm,
  unchanged.
- A-2 (negative control): an id with no fragment returns `None`. An id containing `*` or `/`
  returns `None` without globbing.
- A-3: two fragments with the same id in different slug dirs → return `None` and emit a warning
  event (ambiguous; never guess).
- A-4: the test runs under `BTH_CATALOG_DIR` pointed at a tmp dir (the real-home guard is enforced).

Partly resolves #2110 (single-run lookup only). Leave #2110 open, pointing at B.

## Phase B: runlog cut-over (delivery step 5 of 260925)

Ordered. Each step is a gate; stop on failure.

- **B0 — blockers.**
  - Resolve #1993 (cluster run-log pull) or record an explicit, user-signed decision to cut over
    without it, stating how cluster runs reach the local index in the meantime. With it broken,
    post-cut-over cluster runs would be invisible locally, which is worse than today's behavior.
  - Verify S7 and close or re-scope #2012.
  - Confirm the runlog test subset passes on main. Run it remotely or narrowly: no whole-suite local
    runs (local-compute rule).
- **B1 — project ids.** Migration step 0: run `bth init --assign-id` in every registered project.
  Resolve every `foreign_catalogs` refusal with `bth migrate --consolidate-catalog <root>`.
- **B2 — dry run.** Run `bth migrate --to-log --dry-run`. Review `residual_lines`; `unclassified`
  must be 0. `would_pull` lists remotes that a real run will pull, so expect the real report to
  differ by those runs. Do the review with the user; the user's sign-off is required.
- **B3 — real run.** Run `bth migrate --to-log`, then re-run with `--accept-residual <sha>` using
  the hash from *this* real run (never a dry run's). Quiesce first: no running `bth run`/`submit`
  locally, and no in-flight SLURM jobs from older bathos installs (the squeue check enforces this).
- **B4 — measure.** Measure read latency for `get_run`, `list_runs` and `bth sql` on a freshly
  finished run, and index ingest time (runlog AC-12), on the real catalog. Use a tracked script with
  a `.bth.toml` sidecar committed **before** the measurement. Pre-registered outcomes:
  - pass: a just-finished run is visible to every reader with no explicit compact, and p95 read
    latency is ≤ 2 s;
  - marginal: visible, but p95 is between 2 and 10 s;
  - fail: any reader misses a just-finished run, or p95 is > 10 s.
  - The 2 s/10 s thresholds are judgment calls (interactive feel vs. the 120 s tool limit), not
    derived; revise them before the run if AC-12 sets a different budget.
  - Negative control: with the index deliberately stale (watermarks rewound in a copy), the
    instrument must still report the run, since the delta re-fold should cover it. A broken
    re-fold must show up as a fail.
- **B5 — retire the ritual.** Once B4 passes:
  - change `bth compact` under log mode to print that it is a no-op/ingest trigger rather than
    silently doing nothing;
  - ask the user to strike the "`bth compact` before you conclude a run is missing" clause from
    `~/.claude/CLAUDE.md` (user-owned file; do not edit it from a session);
  - close #2344, #2110, #1685 and #1647 with links to the B4 run id.

**Rollback:** follow the runlog spec's Migration section. Before step 4(b), an abort deletes staging
and leaves `bathos.db` untouched. *Open question to verify before B3:* the spec's procedure for
reverting after the switch (`bathos.db.frozen` restore). If none is specified, write one down before
running B3.

## Out of scope

- Rust/pyo3 reimplementation of compact or the fold. Compact is being retired. Fold/ingest
  candidacy is tracked separately in the Rust-migration investigation debt and should wait until
  the post-cut-over code stops churning.
- The other compact debts that cut-over also changes (#2010 stale ART index, #2196 campaigns
  table): re-triage them after B. Most are moot in log mode, but confirm each one rather than
  closing them in bulk.

## Risks

| Risk | Mitigation |
|---|---|
| Cut-over with broken cluster pull hides cluster runs | B0 hard gate on #1993 |
| Residual report has a class nobody understood and it's signed off anyway | `unclassified == 0` required, plus user review of each class count in B2 |
| Phase A fallback returns a run missing compact-time fields, and someone reads it as final | `_source` marker plus a rendered notice; fields left absent, not defaulted |
| B4 thresholds chosen arbitrarily | Stated as judgment calls; pre-registered before measurement; aligned with AC-12 if it specifies a budget |
