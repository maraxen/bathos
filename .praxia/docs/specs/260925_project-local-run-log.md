---
title: Project-local append-only run log with a disposable index
task_id: 260925_bathos-project-local-log
date: 260925
status: draft
revision: v1 (after adversarial cycle 1)
brainstorm_session: false
invest_overrides: []
---

# Project-local append-only run log with a disposable index

## Problem

bathos's storage has two tiers whose roles have drifted:

- **Cool tier** (`~/.bth/catalog/runs/<slug>/run_<uuid>.parquet`): one Parquet file per run.
  Sampled 2026-09-25: 4,323 fragments, every one a single row, median 15 KB; the same record
  as a JSON line is a median 2.2 KB. Parquet is a format for large immutable batches; one-row
  files are mostly metadata overhead. *(Untracked inspection figures: re-measure under a
  pre-registered sidecar before citing; AC-12.)*
- **Warm tier** (`~/.bth/catalog/bathos.db`): meant to be a derived index, but it is the only
  copy of several tables and several in-place updates (full list in "Authoritative writes").
  `bth compact --force-rebuild` deletes `bathos.db`, so it destroys them; the catalog carries
  `bathos.db.pre-campaign-restore-2608{14,16}`, `...pre-rebuild`, `...pre-groqseq-recovery-260827`.

Symptoms: finished runs are invisible until `bth compact`; read paths open `bathos.db`
read-write so any other holder makes every CLI read fail with `Could not set lock on file`
(observed throughout 2026-09-25); rebuilding the index can lose data; a project's history lives
outside the project.

## Goals

- G1. Every authoritative bathos record is appended as an event to a JSONL log inside the
  project, and is never edited in place.
- G2. `bathos.db` is a pure, disposable index: deleting it and re-ingesting the logs and the
  cold archive reproduces it (AC-1 defines "reproduces").
- G3. Readers never block and never lag: a record is queryable as soon as its line is written.
- G4. Works from any git worktree, from the MCP server, and from SLURM jobs.
- G5. Cross-project queries (`bth ls` across projects, `bth sprint-audit`) keep working.

## Non-goals

- DuckDB 2.0 / Quack or DuckLake (see Future work; the log format is independent of both).
- Changing sidecar semantics, outcome evaluation, or claim/campaign rules.
- Changing cisternal. This spec only calls `cisternal.provenance.channels.capture_git_state`
  and `cisternal.provenance.durable.pin_run` as bathos does today. Because the log is ignored
  (D3), it cannot perturb their dirty computation, so no cisternal change is needed.

## Decisions

- **D1. Location: the project's MAIN checkout, `<main root>/.bth/log/`.** `<main root>` is the
  parent of `git rev-parse --git-common-dir` (the main worktree), so a run executed in a linked
  worktree still appends to the main checkout's log. The worktree it ran in is recorded in the
  event (`worktree_root`, `branch` from `GitState`). Rationale: linked worktrees are routinely
  deleted (often automatically); an append-only record must outlive the directory it was
  produced in. `.bth/` is reused rather than a new `.bathos/`, since projects already keep
  bathos state there (claims, `refs/manifest.jsonl`, attestations, postmortems, hooks).
- **D2. Format: JSONL, one file per writer.** No locking or interleaving across processes;
  works on network filesystems, where SQLite's locking is unsafe.
- **D3. The log is gitignored by default.** An ignored path is excluded from
  `git status --porcelain`, so appends never mark the tree dirty, never force a provenance
  snapshot, and are never touched by checkout, merge or branch switch. `bth` verifies this with
  `git check-ignore -q .bth/log/`; if the path is NOT ignored, `bth init`/`bth run` add
  `/.bth/log/` to `.gitignore` or fail with a structured error. Committing sealed segments is an
  opt-in per project (D7).
- **D4. Provenance comes only from cisternal.** Each `run.started` embeds `GitState`
  (`hash`, `branch`, `dirty`, `dirty_content_id`, `provenance_source`) and the `PinResult`
  (`pinned_sha`, the ref it created under `refs/bathos/runs/<run_id>` or, for a dirty tree,
  `refs/bathos/wip/<run_id>`, and the manifest entry), exactly as `runner.py` computes them
  today (pin before the subprocess starts, `runner.py:546`). bathos adds no git logic.
- **D5. Parquet is for sealed history only.** Sealed segments are compacted into partitioned
  Parquet in the cold archive; nothing writes Parquet one record at a time.
- **D6. Recording is not best-effort.** Provenance capture must never fail a run; appending the
  run's own events must. If `run.started` cannot be appended, the script is not launched. If
  `run.finished` cannot be appended, the command exits non-zero with a structured error naming
  the log path, after a retry to the fallback path (below).
- **D7. Opt-in tracking.** A project can set `[log] tracked = true` in `.bth.toml`; then
  `.bth/log/sealed/` (and only sealed/) is un-ignored and committed by the user like any file.
  Consequences stated in `bth init`: command lines, argv and env subsets enter git history
  permanently; default stays untracked (public repos, privacy).

## Authoritative writes (everything the index must not own)

Every current write to `bathos.db` that is not reproducible from another source becomes an
event. Implementation must add to this table any write found later (AC-2 enforces it).

| Current write | Location | Event |
|---|---|---|
| run row (start/finish) | `runner.py:524,623,847`, cool fragment | `run.started`, `run.finished` |
| `runs.output_metadata` (output hashes; drift-check baseline, `checker.py:132`) | recomputed at compact, `compact.py:959-976` | `run.outputs_hashed`, emitted once at run end; compact never recomputes it |
| `runs.metadata` rewrite by the reaper | `reap.py:361` | `run.reaped` |
| `campaigns` insert / upsert | `campaigns.py` | `campaign.created` |
| `stopping_threshold` update | `campaigns.py:340,508` | `campaign.threshold_set` |
| `evalue`, `seq_position` update | `campaigns.py:517` | `campaign_run.evalue_updated` |
| `claim_mode='bypassed'` | `campaigns.py:1043` | `campaign.claim_bypassed` |
| `campaign_runs` insert | `campaigns.py` | `campaign.run_added` |
| campaign conclusion | `campaigns.py` | `campaign.concluded` |
| `campaign_edges`, `run_edges` | `campaign_edges.py` | `edge.added` |
| `amendments` | — | `amendment.recorded` |
| trust ledger, anchors, reap ledger, archived items (already durable Parquet) | `trust_ledger.py`, `anchor.py`, `reap.py`, `archived_items.py` | `ledger.*`, `anchor.*`, `reap.*`, `archive.*` |

## Design

### Log directory resolution

In order:
1. `BTH_LOG_DIR` if set (SLURM and tests).
2. `<main root>/.bth/log/` when the run's `project_root`/cwd is inside a git repo (D1).
3. `<dir containing .bth.toml>/.bth/log/` when not in a git repo but a `.bth.toml` is found
   walking up (bounded, as `resolve_workspace` does).
4. Otherwise `~/.bth/log/unaffiliated/`, and every event carries `"project": null`;
   `bth ls` shows these as unaffiliated. A run is never unrecorded because it has no project.

**Fallback path (D6):** if the resolved directory is unwritable, append to
`~/.bth/log/fallback/<project-slug>/` and emit a warning; `bth log reconcile` later moves those
events into the project log (idempotent by `(writer, seq)`).

### Segment naming and writers

`<host>.<pid>.<start_ns>[.slurm-<job>-<task>-<restart>].jsonl`, where `<restart>` is
`SLURM_RESTART_COUNT` (default 0). This is unique per process even with several `bth run`
processes in one SLURM task and across requeues.

- Writers are keyed by `(process, log dir)`: the long-lived MCP server, which runs scripts for
  arbitrary `project_root`s (`mcp.py:1717`), holds one writer per project log it touches.
  Threads share a writer behind a lock.
- **Sealing:** a segment is sealed (renamed into `sealed/`, then never modified) at 8 MB, when
  the process exits cleanly, or after 1 h idle for long-lived processes.
- **Stale active segments:** a segment is presumed dead when its host matches the current host
  and its PID is not alive, or (other host) its mtime is older than 24 h. The next `bth`
  command seals it. Readers never need this to be correct: they read active and sealed
  segments alike (idempotency below), so a wrong liveness guess costs only an early seal.
- **Unlink/rename detection:** before each append the writer `fstat`s its fd and `stat`s the
  path; if the inode differs or the path is gone, it opens a new segment and records a
  `writer.resumed` event naming the lost file, instead of writing into an unlinked inode.
- Each line: one `write()` of the full line with a trailing `\n`, then `flush()` + `fsync()`.

### Line envelope

```json
{"v": 1, "kind": "run.finished", "id": "<entity uuid>", "ts": "<RFC3339 UTC>",
 "project": "<slug or null>", "writer": "<segment stem>", "seq": 17,
 "worktree_root": "<abs path>", "data": { ... }}
```

- `(writer, seq)` is globally unique and is the **idempotency key**; `seq` starts at 0 and
  increments per line within a writer.
- `data` is validated against a per-kind JSON Schema at ingest. Invalid lines go to an
  index-side `quarantine` table with the reason; they are never dropped silently.

### Fold rules (current state from events)

- Events for one entity are ordered by `(ts, writer, seq)`. This order is deterministic for a
  fixed multiset of events. Cross-writer order depends on host clocks; the only fields whose
  value can depend on cross-writer order are last-writer-wins attributes
  (`campaign.threshold_set`, `campaign.claim_bypassed`), and these are listed in AC-1 as
  skew-sensitive.
- `run.started` with no `run.finished` folds to `status = 'incomplete'` until a `run.reaped` or
  a later `run.finished` arrives.
- Duplicates (same `(writer, seq)`) are ingested once.

### Index (`bathos.db`)

- **Ingest** scans every registered project's `.bth/log/` (active + sealed) and the fallback
  and unaffiliated dirs. Per file it stores `(project, relative path, byte_offset,
  sha256 of bytes [0, byte_offset))`. If the stored prefix hash no longer matches (file
  replaced), it re-reads from 0; idempotency by `(writer, seq)` prevents duplicate rows.
  Inodes are not used (checkout, clone and rsync change them).
- **Reads** open `bathos.db` with `read_only=True` for the duration of one query and retry on
  lock with bounded backoff. To satisfy G3, a read first scans each active segment's bytes past
  its watermark into memory in Python (DuckDB `read_json` cannot start at a byte offset),
  validates them, and exposes them as a temporary relation unioned with the indexed tables.
- Only ingest opens `bathos.db` read-write. `bth compact` means "ingest now";
  `--force-rebuild` deletes and re-ingests, which cannot lose data because the index owns none.

### Worktrees

- Runs in a linked worktree append to the main checkout's log (D1); deleting the worktree loses
  nothing. The run's code state is protected independently by the existing pin
  (`refs/bathos/runs|wip/<run_id>`).
- A project whose main checkout is missing (bare repo with only linked worktrees) falls back to
  the first worktree listed by `git worktree list` and emits a warning.

### Cluster

- SLURM jobs set `BTH_LOG_DIR` to the cluster checkout's `.bth/log/` (resolution rule 1).
  `bth sync --pull` delegates to `myxcel pull` (per the cluster rules) for that directory and
  places the segments in the local main checkout's `.bth/log/remote/<remote-name>/`,
  regardless of which branch either side has checked out. The segment name's host and SLURM
  ids keep them distinct; idempotency makes repeated pulls safe.
- Refs created on the cluster clone stay there. The pin's `bundle_path` (created for dirty
  runs) is pulled with the segments; `bth recover --import-bundles` imports them locally.
  Clean-tree cluster runs cite a commit that must already exist on the remote branch; if it
  does not resolve locally after sync, `bth check` reports it, as it does today.

### Discovery (cross-project)

- `~/.bth/projects.toml` lists project main roots. Writing the first event to a project log
  registers that root automatically (idempotent), so a run is never written but unindexed.
  `bth projects prune` removes roots that no longer exist.
- **Prerequisite (ships first):** the test suite must never touch the real `~/.bth`. Today
  `~/.bth/projects.toml` contains pytest temp directories. All tests isolate `HOME` and
  `BTH_*` (AC-11).

### Cold archive

- `bth archive` compacts sealed segments older than a threshold into
  `~/.bth/catalog/archive/project=<slug>/year=<Y>/month=<M>/*.parquet`, verifies the archive
  row-for-row against the segments (by `(writer, seq)` and content hash), and only then deletes
  the sealed JSONL. The archive is authoritative for what it holds; the index reads
  archive + logs. Nothing is deleted on the assumption that git has a copy.

### Migration

1. Export every cool-tier fragment and every row of every table in "Authoritative writes" to
   events in the owning project's `.bth/log/sealed/migrated-<date>.jsonl`. Runs whose project
   root cannot be resolved go to `~/.bth/log/unaffiliated/`.
2. Build a fresh index from the logs alone.
3. Diff it against the current `bathos.db` table by table. Every difference must fall in an
   allow-listed class (data already lost to earlier force-rebuilds; corrupt fragments skipped
   by `compact.py:787`; `output_metadata` whose files have since changed; unresolvable project)
   and is written to a signed-off residual report. An unclassified difference blocks cut-over.
4. Keep the old catalog read-only for one release as a fallback.

## Acceptance criteria

- AC-1. For any fixed multiset of events, delete + re-ingest produces an identical index
  (property test with randomized events, writers and ingest order). Skew-sensitive fields
  (last-writer-wins attributes named under "Fold rules") are excluded from the equality and
  covered by a separate test that fixes `ts`.
- AC-2. A lint/test enumerates every SQL write to `bathos.db` in `src/bathos/` and fails if
  any is not the ingest path; each authoritative write must be an event.
- AC-3. A run is visible to `bth show/ls/sql` and the MCP equivalents immediately after it
  finishes, with no `bth compact`.
- AC-4. Two concurrent `bth run` processes plus a reader in one project never raise a lock
  error; both runs are recorded.
- AC-5. Deleting a linked worktree after a run changes nothing in the index or the logs.
- AC-6. A SLURM array of N tasks, including a requeued task and a task running two
  `bth run`s, yields N+1 distinct segments with every run recorded once after two pulls.
- AC-7. A truncated final line is ignored by readers and reported by `bth verify`; an invalid
  line is quarantined with a reason.
- AC-8. With the log path made unwritable: `run.started` failure prevents launch; a
  `run.finished` failure writes to the fallback path and exits non-zero with a structured
  error; `bth log reconcile` then moves it idempotently.
- AC-9. Deleting or renaming an active segment mid-run produces a `writer.resumed` event and
  no lost lines.
- AC-10. With `.bth/log/` not ignored, `bth run` adds the ignore rule or fails; it never
  appends to a tracked-or-untracked-visible path. A run's recorded `git_dirty` is unaffected by
  its own log appends (checked by two consecutive runs on a clean tree both recording
  `dirty=false`).
- AC-11. The test suite never writes to the real `~/.bth` (enforced by a session fixture that
  fails if `~/.bth` mtime changes).
- AC-12. The size and read-latency figures in this spec are re-measured under a pre-registered
  bathos sidecar before they are cited.
- AC-13. Migration step 3 produces a residual report in which every difference is classified.

## Order of delivery

1. AC-11 (test isolation), then the event writer + resolution + D3 enforcement (AC-8/9/10).
2. Dual-write: runner and every "Authoritative writes" site emit events while still writing the
   old tiers.
3. Index ingest + read path (AC-1..4), then migration (AC-13) and cut-over.
4. Cluster pull, cold archive, opt-in tracking (D7).

## Risks

| Risk | Mitigation |
|---|---|
| A write site missed in the table | AC-2 lint fails the build |
| Main checkout deleted | It is the project; same exposure as the repo itself. Cold archive holds sealed history |
| Clock skew across hosts | Deterministic tie-break; skew-sensitive fields enumerated (AC-1) |
| Many active segments slow reads | Tail-read past watermark only; 1 h idle seal |

## Future work

- DuckLake as the index/cold backend (the log format is independent of it).
- Quack only if multi-process writers to the index itself become necessary.
