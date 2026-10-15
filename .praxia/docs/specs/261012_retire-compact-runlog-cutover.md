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
| S6 | Bathos's cluster run-log pull (`bth sync --pull`) has never worked | *verified*: `cluster.pull_path` (`cluster.py:179`) is a stub that always raises; `pull_cluster_log` docstring confirms every sub-pull fails. myxcel `pull` is bound to project names (`rsync.py:326`). Fixed in B0 |
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
  - **Cluster run-log pull (#1993), folded into this sprint (user decision, 2026-10-12).** After
    cut-over, SLURM jobs honour `BTH_LOG_MODE` and write log lines, not fragments, so
    `sync_catalog`'s fragment pull no longer brings them home. Without this, every post-cut-over
    cluster run is missing from the local index (lagging, not lost: the lines stay in the remote
    `.bth/log/` and `~/.bth/log-mirror/`). The bathos consumer is already built
    (`sync.pull_cluster_log`, `sync.py:383`); only `cluster.pull_path` (`cluster.py:179`) is a stub
    that always raises `MyxcelCapabilityGapError`. Work, in order:
    1. **myxcel** (backlog #5880, promotes myxcel debt #2166): `rsync.pull_path(profile,
       remote_path, local_dest)`, CLI `myxcel pull-path`, MCP `pull_path`. It reuses
       `_rsync_pull_flags`/`stream_command` from `rsync.pull` (`rsync.py:326`) and refuses remote
       paths outside `$HOME/.bth/` and the registered workspace roots.
    2. **bathos:** replace the `cluster.pull_path` stub with a call to the myxcel primitive, plus a
       unit test with myxcel mocked out. Do not implement it as direct rsync in bathos:
       `sync_catalog` already does that, which is a precedent to remove, not to copy.
    3. **Cluster test (L3), before B2:** a SLURM job with `BTH_LOG_MODE=1` against a scratch
       catalog → `bth sync --pull` → ingest → the run is visible locally, and
       `ClusterLogPullResult.pulled` contains `"log"`. Negative control: a bad remote path must
       land in `failures`, not return an empty success. Confirm the installed myxcel actually has
       `pull-path` by grepping the installed package (`--version` is not evidence).
    - Done when: L3 passes and bathos #1993 and myxcel #2166 are resolved, linking to the L3 job
      and run id.
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

*Resolved 2026-10-15:* the runlog spec gives no post-switch revert; the procedure below is the one
used for B3. It rests on these code facts, read on main `ef24e9f6`:
- The switch (`migrate.py:_do_switch`) does these steps in order:
  - (a) builds the index;
  - (b) writes `~/.bth/catalog/cutover.json`, the single commit point;
  - (c) copies staged segments into each project's `.bth/log/` and into `~/.bth/log-mirror/<id or _null/...>/` as `import-<attempt>-<n>.jsonl`;
  - (d) renames `bathos.db` to `bathos.db.frozen` (`_step4d_rename_warm`);
  - (e) deletes staging.
- Log mode is "marker exists" (`mode.py:is_log_mode`). While it is on, every write site skips its
  legacy write (`emit.py:emit_or_legacy`): **post-switch runs, campaigns and ledgers exist only as
  log lines, never as cool fragments or warm rows.**

**Pre-B3 backup (required):**
1. Quiesce: no local `bth run`/`submit`/MCP writers, no running SLURM jobs.
2. `cp -a ~/.bth/catalog ~/.bth/catalog.pre-cutover-<YYMMDD>`. The catalog was 478 MB on
   2026-10-15. Check `df -h ~` first: the disk had 7.1 GB free that day.
3. Record the list of existing `import-*.jsonl` files under every registered root's `.bth/log/` and
   under `~/.bth/log-mirror/`. It should be empty before the first cut-over.

**Revert after the switch:**
1. Quiesce, as above.
2. Delete `~/.bth/catalog/cutover.json`. Log mode is now off, and writes go back to the legacy path.
3. If `bathos.db.frozen` exists and `bathos.db` does not, run
   `mv ~/.bth/catalog/bathos.db.frozen ~/.bth/catalog/bathos.db`. If both exist, keep `bathos.db`
   aside, restore from the step-2 backup, and do not guess.
4. Remove `import-<attempt>-*.jsonl` for this attempt from every project `.bth/log/` and from
   `~/.bth/log-mirror/`. The index is rebuildable from logs, so a stale `index.db` may be deleted.
   Leaving the import files in place is safe for a later retry, because step (c) skips existing
   names, but deleting them keeps one source of truth while the system is in legacy mode.
5. `bth compact`, then verify a known pre-cut-over run by record (`bth sql ... WHERE id LIKE`).

**Known loss on revert:** runs recorded between the switch and the revert have no cool fragment.
They stay in the project logs, so nothing is deleted, but legacy readers do not see them. Note
their run ids from the log lines (`run.started` events newer than `cutover.json`'s mtime) before
reverting, and re-run the cut-over to surface them again. Revert only if the index is wrong, not
merely slow.

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
| Cut-over with broken cluster pull hides cluster runs | #1993 fixed inside B0 (myxcel #5880 + bathos stub), gated by a real-cluster L3 test |
| The installed myxcel lacks `pull-path` even though the release is tagged | Grep the installed package before L3; `--version` does not distinguish commits |
| Residual report has a class nobody understood and it's signed off anyway | `unclassified == 0` required, plus user review of each class count in B2 |
| Phase A fallback returns a run missing compact-time fields, and someone reads it as final | `_source` marker plus a rendered notice; fields left absent, not defaulted |
| B4 thresholds chosen arbitrarily | Stated as judgment calls; pre-registered before measurement; aligned with AC-12 if it specifies a budget |
