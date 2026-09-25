---
title: Project-local append-only run log with a disposable index
task_id: 260925_bathos-project-local-log
date: 260925
status: draft
brainstorm_session: false
invest_overrides: []
---

# Project-local append-only run log with a disposable index

## Problem

bathos's storage has two tiers whose roles have drifted:

- **Cool tier** (`~/.bth/catalog/runs/<slug>/run_<uuid>.parquet`): one Parquet file per run.
  Sampled 2026-09-25: 4,323 fragments, every one a single row, median 15 KB. The same
  record serialized as a JSON line is a median 2.2 KB. Parquet is a columnar format for
  large, immutable batches; one-row files are almost entirely metadata overhead.
  *(Both figures are from an untracked inspection run and must be re-measured under a bathos
  sidecar before they are cited anywhere else; see AC-9.)*
- **Warm tier** (`~/.bth/catalog/bathos.db`): meant to be a derived index, but it is the
  ONLY copy of `campaigns`, `campaign_runs`, `campaign_edges`, `run_edges` and `amendments`.
  `bth compact --force-rebuild` deletes `bathos.db`, so it destroys them. The catalog
  directory carries the scars: `bathos.db.pre-campaign-restore-260814`, `...-260816`,
  `...pre-rebuild`, `...pre-groqseq-recovery-260827`.

Symptoms agents hit daily:

1. **Read lag.** A finished run is invisible to `bth ls/find/sql/show` until `bth compact`.
2. **Lock contention.** Read paths in `query.py` open `bathos.db` read-write, so any other
   process holding it makes every CLI read fail (`Could not set lock on file`), which
   happened throughout 2026-09-25.
3. **Unsafe rebuild.** Rebuilding the index can lose authoritative data.
4. **Global, not project-scoped.** A project's experiment history lives outside the project,
   so it does not travel with the repo, cannot be reviewed in a PR, and a fresh clone
   has none of it.

## Goals

- G1. Every authoritative bathos record is appended to a project-local JSONL log and is never
  edited in place.
- G2. `bathos.db` becomes a pure, disposable index: deleting it and rebuilding from the logs
  (plus cold archive) reproduces it exactly. `--force-rebuild` is always safe.
- G3. Readers never block and never lag: a run is queryable the moment its record is written.
- G4. The log travels with the project and works from any git worktree and from SLURM jobs.
- G5. Cross-project queries (`bth ls` across projects, `bth sprint-audit`) keep working.

## Non-goals

- Adopting DuckDB 2.0 / Quack (multi-writer server) or DuckLake. Revisit after this ships
  (see Future work); the log format is chosen to be independent of either.
- Changing sidecar semantics, outcome evaluation, or the claim/campaign rules themselves.
- Changing cisternal's provenance capture (`cisternal.provenance.channels`) or durable
  pinning (`cisternal.provenance.durable`); this spec consumes them as-is.

## Decisions

- **D1. Location: `<worktree root>/.bth/log/`, not a new `.bathos/`.** Projects already keep
  bathos state in `.bth/` (claims, `refs/manifest.jsonl`, attestations, postmortems, hooks;
  12+ projects as of 2026-09-25). A second directory would split project state for no gain.
- **D2. Format: JSONL, one file per writer.** No locking, no interleaving, safe on network
  filesystems (unlike SQLite, whose locking is unsafe over NFS, which the Engaging project
  and scratch filesystems are).
- **D3. The log is git-tracked.** It is committed with the work that produced it, reviewed in
  PRs, and merged with its branch. Per-writer file names make merge conflicts impossible
  (no two writers ever touch the same file).
- **D4. Provenance comes only from cisternal.** Each run record embeds the
  `GitState` from `cisternal.provenance.channels.capture_git_state` (hash, branch, dirty,
  dirty_content_id, provenance_source) and the `PinResult` from
  `cisternal.provenance.durable` (snapshot SHA, `refs/provenance/runs/<run_id>`, manifest
  entry). bathos adds no git logic of its own.
- **D5. Parquet is for sealed history only.** Sealed log segments may be compacted into
  partitioned Parquet in the global cold archive; nothing writes Parquet one record at a time.
- **D6. Recording is not best-effort.** Unlike provenance capture (which must never fail a
  run), failing to append the run record is a hard error reported to the caller: a tracker
  that silently loses runs violates bathos's central goal.

## Design

### Layout

```
<worktree>/.bth/
  log/
    <host>.<pid>.<start_ts>.jsonl      # active segment, one per writer process
    <host>.slurm-<jobid>-<taskid>.jsonl # SLURM writer (array-safe)
    sealed/                            # closed segments (immutable), still tracked
  refs/manifest.jsonl                  # existing: cisternal durable manifest
  claims/ attestations/ ...            # existing, unchanged
~/.bth/
  catalog/bathos.db                    # disposable index over all registered projects
  catalog/archive/                     # cold Parquet, partitioned project/year/month
  projects.toml                        # registry of project roots (see Discovery)
```

### Line envelope

Every line is one JSON object:

```json
{"v": 1, "kind": "run.finished", "id": "<uuid>", "ts": "<RFC3339 UTC>",
 "project": "<slug>", "writer": "<segment file name>", "seq": 17, "data": { ... }}
```

- `v`: envelope schema version. `kind`: event type (below). `id`: the entity the event is
  about. `seq`: monotonic per writer, so gaps and duplicates are detectable.
- `data` is validated against a per-kind schema at ingest; invalid lines are quarantined
  (moved to an index-side quarantine table with the reason), never dropped silently.
- Serialization: UTF-8, no pretty-printing, one `write()` + `flush()` + `fsync()` per line.

### Event kinds (initial set)

| kind | replaces |
|---|---|
| `run.started`, `run.finished` | cool-tier run fragment |
| `campaign.created`, `campaign.run_added`, `campaign.concluded` | warm-only `campaigns`, `campaign_runs` |
| `edge.added` (campaign or run edges) | warm-only `campaign_edges`, `run_edges` |
| `amendment.recorded` | warm-only `amendments` |
| `ledger.*`, `anchor.*`, `reap.*`, `archive.*` | the existing durable Parquet ledgers |

Current state is a fold over events in `(ts, writer, seq)` order. Corrections are new events.

### Writers

- One writer object per process; threads share it behind a lock. The segment name is fixed
  at first write.
- Rotation: a segment is sealed (moved to `sealed/`) at 8 MB or when the process exits
  cleanly. A segment left active by a crashed process is sealed by the next `bth` command
  that finds it older than 24 h with no live owner.
- Crash tolerance: a partial final line is ignored by readers and reported by `bth verify`.

### Index (`bathos.db`)

- Ingest reads each registered project's segments from a per-file watermark
  `(path, inode, byte_offset)`; sealed files are ingested once and marked complete.
- Reads open `bathos.db` read-only, briefly, and additionally read any active-segment bytes
  past the watermark (DuckDB `read_json` over the tail), so G3 holds without compaction.
  Only ingest opens it read-write, and ingest retries on lock with backoff.
- `bth compact` becomes "ingest now" (an optimization). `--force-rebuild` deletes and
  re-ingests; because the index holds no authoritative data, it cannot lose anything.

### Worktrees

- A run started in a worktree writes to that worktree's `.bth/log/`. Its records are
  committed on that worktree's branch and reach `main` when the branch merges.
- **Loss risk:** a worktree deleted before its log is committed loses those records.
  Mitigation: the run-end pin (cisternal durable snapshot) already commits the worktree's
  contents to `refs/provenance/runs/<run_id>`; the run's own record is written to the
  segment BEFORE the pin, so the snapshot contains it and the ref keeps it reachable after
  the worktree and branch are gone. `bth recover --from-refs` replays such records.
- Discovery indexes every worktree of a registered project (`git worktree list`), not only
  the main checkout, so unmerged runs remain queryable while the worktree exists.

### Cluster

- SLURM jobs write to the cluster checkout's `.bth/log/` with an array-safe segment name.
  `bth sync --pull` (via myxcel) brings those segments back; they are then committed like any
  other log file. No catalog rsync is needed for new runs.

### Discovery (cross-project)

- `~/.bth/projects.toml` lists project roots. `bth init` registers; `bth projects prune`
  removes entries whose root no longer exists.
- Prerequisite fix: tests currently write to the real `~/.bth/projects.toml` (it contains
  pytest temp directories). All tests must isolate `$HOME`/`BTH_*` so the registry is
  trustworthy.

### Cold archive

- `bth archive` compacts sealed segments older than a threshold into
  `~/.bth/catalog/archive/project=<slug>/year=<Y>/month=<M>/*.parquet` and, only after
  verifying row-for-row equality, may delete the sealed JSONL from the working tree
  (history remains in git). The index reads archive + log together.

### Migration

1. Export every cool-tier fragment and every warm-only table row to log events in the
   owning project's `.bth/log/migrated-<date>.jsonl` (sealed immediately).
2. Rebuild a fresh index from the logs alone.
3. Diff it against the current `bathos.db`, table by table; any difference blocks cut-over.
4. Keep the old catalog read-only for one release as a fallback.

## Acceptance criteria

- AC-1. Deleting `bathos.db` and running `bth compact` reproduces every table exactly
  (property test over randomly generated event histories).
- AC-2. A run is visible to `bth show`, `bth ls`, `bth sql` and the MCP equivalents
  immediately after it finishes, with no `bth compact` in between.
- AC-3. Two concurrent `bth run` processes plus one reader in the same project never raise a
  lock error, and both runs are recorded.
- AC-4. A SLURM array of N tasks produces N distinct segments and N runs after sync.
- AC-5. A worktree removed after a run (log uncommitted) can be recovered with
  `bth recover --from-refs`, yielding the identical run record.
- AC-6. A truncated final line is ignored by readers and reported by `bth verify`; an invalid
  line is quarantined with a reason, never dropped.
- AC-7. Failing to append a run record fails the command with a non-zero exit and a
  structured error (D6).
- AC-8. The migration diff (step 3) is empty on the real `~/.bth/catalog`.
- AC-9. The size and read-latency claims in this spec are re-measured under a pre-registered
  bathos sidecar before being cited.
- AC-10. The test suite never writes to the real `~/.bth`.

## Risks

| Risk | Mitigation |
|---|---|
| Repo growth from tracked logs | 2 KB/run; rotation + cold archive; measure per project (AC-9) |
| Record loss in deleted worktrees | Record-before-pin + `recover --from-refs` (AC-5) |
| Reads slow once many active segments exist | Tail-read only past the watermark; seal on exit |
| Clock skew across hosts breaks ordering | Order by `(ts, writer, seq)`; `seq` is authoritative within a writer |

## Future work

- DuckLake as the index/cold backend once this ships (log format is independent of it).
- Quack only if multi-process local writers to the index become necessary.
