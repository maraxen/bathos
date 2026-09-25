---
title: Project-local append-only run log with a disposable index
task_id: 260925_bathos-project-local-log
date: 260925
status: draft
revision: v2 (after adversarial cycle 2)
brainstorm_session: false
invest_overrides: []
---

# Project-local append-only run log with a disposable index

## Problem

- **Cool tier** (`~/.bth/catalog/runs/<slug>/run_<uuid>.parquet`): one Parquet file per run.
  Sampled 2026-09-25: 4,323 fragments, every one a single row, median 15 KB; the same record
  as a JSON line is a median 2.2 KB. *(Untracked inspection figures; re-measure under a
  pre-registered sidecar before citing, AC-12.)*
- **Warm tier** (`~/.bth/catalog/bathos.db`): meant to be a derived index, but it is the only
  copy of many rows and in-place updates (see "Authoritative writes"). `--force-rebuild` deletes
  it; the catalog carries `bathos.db.pre-campaign-restore-2608{14,16}`, `...pre-rebuild`,
  `...pre-groqseq-recovery-260827`. `compact` also rewrites run rows from files it finds under
  the *current directory's* workspace (`compact.py:794`, `:1012`), so the index depends on
  where compact happened to run.
- **Locking:** reads open `bathos.db` read-write (`query.py:429` for `run_sql`), and DuckDB
  refuses even a `read_only` open while another process holds the file read-write (verified
  2026-09-25, duckdb 1.5.2: `IOException: Could not set lock on file`). Every CLI read failed
  this way on 2026-09-25.
- A finished run is invisible until `bth compact`; a project's history lives outside the project.

## Goals

- G1. Every authoritative bathos record is appended as an event to a JSONL log inside the
  project and never edited in place.
- G2. `bathos.db` is a pure, disposable index: a deterministic function of the events alone
  (no filesystem or cwd reads at ingest).
- G3. Reads never block on a lock and never lag: a record is queryable once its line is written.
- G4. Works from any git worktree, the MCP server, and SLURM jobs.
- G5. Cross-project queries keep working.

## Non-goals (this epic)

- DuckDB 2.0/Quack, DuckLake, a cold archive, committing logs to git (opt-in tracking), and
  segment sealing. All are deferred; none is needed for G1–G5.
- Changing cisternal: bathos keeps calling `capture_git_state` and `pin_run` as today.

## Decisions

- **D1. Location: the project's main checkout, `<main root>/.bth/log/`.** *(Pending user
  approval: the request was "inside each project directory ... in the worktree it's run
  from". This keeps the log inside the project directory but writes it in the main checkout
  rather than the linked worktree, because linked worktrees are routinely deleted, often
  automatically, and an append-only record must outlive the directory that produced it. The
  worktree a run executed in is recorded in every event. If the user prefers the worktree
  location, D1 changes to "worktree root" and AC-5 becomes a recovery-from-mirror test.)*
  `.bth/` is reused, since projects already keep bathos state there.
- **D2. Format: JSONL, one file per writer process.** No cross-process locking; safe on
  network filesystems (unlike SQLite).
- **D3. The log is gitignored.** Ignored paths are excluded from `git status --porcelain`
  (cisternal's dirty check, `capture.py:196`), so appends never mark the tree dirty or force a
  provenance snapshot, and checkout/merge/branch switch never touch them. `bth` checks
  `git check-ignore -q .bth/log/x`; if not ignored it adds `/.bth/log/` to `.gitignore`
  (`bth init`) or fails `bth run` with a structured error.
- **D4. Provenance comes only from cisternal.** `run.started` embeds `GitState` (hash, branch,
  dirty, dirty_content_id, provenance_source) and the `PinResult` (pinned SHA,
  `refs/bathos/runs/<run_id>` or `refs/bathos/wip/<run_id>` for a dirty tree, manifest entry),
  computed where `runner.py` computes them today (`:546`, before the subprocess). bathos adds no
  git logic.
- **D5. No Parquet written one record at a time.** The run fragments and the existing
  per-record ledger fragments (`blast_radius.py:116`, trust ledger, anchors, reap, archived
  items) all move to the log. Parquet remains only for the existing `bth archive` export.
- **D6. Recording is not best-effort.** If `run.started` cannot be appended to the project log
  or the fallback, the script is not launched. If `run.finished` lands only in the fallback,
  the command exits with the script's own status plus a warning (so SLURM `afterok` still
  works); it exits non-zero only if both the project log and the fallback fail.
- **D7. Every line is written twice: project log + mirror.** The writer appends the same bytes
  to `<main root>/.bth/log/<segment>` and `~/.bth/log-mirror/<project-id>/<segment>`. The mirror
  protects against `git clean -fdX`, which deletes ignored files and would otherwise erase the
  project's history. A mirror append failure is a warning, not a run failure.

## Authoritative writes (everything the index must not own)

AC-2 enforces that this table stays complete.

| Current write | Location | Event (natural key) |
|---|---|---|
| run row, start and finish | `runner.py:524,623,847` | `run.started`, `run.finished` (`run_id`) |
| `runs.output_metadata` (drift baseline, `checker.py:132`) | recomputed at compact, `compact.py:959-976` | `run.outputs_hashed` (`run_id`, `seq`): at run end, and on explicit `bth check --rebaseline`. Behaviour change: compact no longer silently refreshes the baseline when outputs change |
| outcome + `postmortem_*` override | `compact.py:1012` (reads files via cwd) | `run.postmortem_applied` (`run_id`, postmortem sha256), emitted by postmortem validate/register |
| `claim_discriminates`, `claim_isolates`, `parity_run_type` COALESCE | `compact.py:978` | carried in `run.started.data` from the sidecar |
| `runs.metadata` rewrite by reaper | `reap.py:361` | `run.reaped` (`run_id`) |
| campaign insert/upsert | `campaigns.py`, `campaigns.py:1194`, `claim.py:649` | `campaign.created` (`campaign_id`) |
| `claim_path`, `claim_sha256` (Union Gate tamper anchor) | `claim.py:684`, `claim.py:1062` (incl. attest_parity rollback) | `campaign.claim_bound` (`campaign_id`, sha256); a rollback is a new event, not a deletion |
| `stopping_threshold` | `campaigns.py:340,508` | `campaign.threshold_set` (`campaign_id`, `seq`) |
| `evalue`, `seq_position` | `campaigns.py:517` | `campaign_run.evalue_updated` (`campaign_id`, `run_id`, `seq`) |
| `claim_mode='bypassed'` | `campaigns.py:1043` | `campaign.claim_bypassed` (`campaign_id`) |
| other campaign updates | `campaigns.py:1222` | `campaign.updated` (`campaign_id`, `seq`) |
| `campaign_runs` insert | `campaigns.py` | `campaign.run_added` (`campaign_id`, `run_id`) |
| campaign conclusion | `campaigns.py` | `campaign.concluded` (`campaign_id`) |
| `campaign_edges`, `run_edges` | `campaign_edges.py` | `edge.added` (`src`, `dst`, `type`) |
| `blast_radius_ledger` | `blast_radius.py:231`, `compact.py:620` | `blast_radius.recorded` (existing ledger id) |
| `sidecar_anchors` insert/update | `anchor.py:211` | `anchor.recorded` (`path`, `sha256`) |
| trust ledger, reap ledger, archived items | `trust_ledger.py`, `reap.py`, `archived_items.py` | `ledger.*`, `reap.*`, `archive.*` (existing ids) |
| `amendments` | created at `compact.py:430`; no writer exists today | none until a writer is added (AC-2 then requires one) |

## Design

### Log directory resolution

Built on the existing `resolve_workspace()` ladder (`workspace.py`), not a new one:
1. `fs_root` from `resolve_workspace()`: `BTH_WORKSPACE_ROOT` → git toplevel → the `.bth.toml`
   recorded `root` → cwd.
2. If that root is a *linked* worktree, map it to the main worktree: the first `worktree`
   entry of `git worktree list --porcelain` (absolute path). If that entry is `bare`, keep the
   linked worktree's own root and warn. A submodule is its own repository: its root is the
   submodule working tree (`git worktree list` run inside it reports that path).
3. Log dir = `<root>/.bth/log/`. If there is no git repo and no `.bth.toml`, use
   `~/.bth/log/unaffiliated/`, with `"project": null` on every event.

**Fallback (D6):** `~/.bth/log/fallback/<slug or _unaffiliated>/`. Ingest scans it like any
project log, so no reconcile step is needed.

**Event destination:** each event goes to the log of the project that owns its entity: run
events to the run's project, campaign events to the campaign's project. Cross-project links
refer to other entities by id. MCP tools that receive only `catalog_dir` resolve the owner
from the entity; if none can be resolved, the event goes to `unaffiliated/`.

### Segments and writers

- Name: `<host>.<pid>.<start_ns>.jsonl`, with `.slurm-<job>-<task>-<restart>` appended as
  information only (uniqueness comes from host, pid and start_ns).
- One writer per `(process, log dir)`; the MCP server holds one per project it touches.
  Threads share a writer behind a lock. A segment rotates to a new file at 8 MB. There is no
  sealing: every file is read the same way.
- Each line is one `write()` of the full line plus `\n`, then `flush()` + `fsync()`, to the
  project log and then the mirror (D7).
- Before each append the writer compares `fstat(fd)` with `stat(path)`; if the file was deleted
  or replaced, it starts a new segment whose first line is `writer.resumed` naming the old one.

### Line envelope

```json
{"v": 1, "kind": "run.finished", "key": ["<run_id>"], "ts": "<RFC3339 UTC>",
 "project": "<slug or null>", "writer": "<segment stem>", "seq": 17,
 "worktree_root": "<abs path>", "origin": "live|migration", "data": { ... }}
```

- `(writer, seq)` identifies a physical line (dedups re-reads of the same file).
- `(kind, key)` is the **natural key** (from the table). Create-type events (`run.started`,
  `campaign.created`, `edge.added`, ...) are idempotent on it, so the same record arriving from
  dual-write and from migration folds to one. Update-type events include a `seq` in the key and
  are applied in fold order.
- `data` is validated against a per-kind JSON Schema at ingest; invalid lines go to a
  `quarantine` table with the reason.

### Fold rules

- Events for one entity are applied in `(ts, writer, seq)` order, which is deterministic for a
  fixed multiset of events. The only fields that can depend on cross-host clock order are the
  last-writer-wins updates (`campaign.threshold_set`, `campaign.updated`,
  `campaign.claim_bypassed`); AC-1 names them.
- `run.started` without `run.finished` folds to `status='incomplete'` until `run.reaped` or a
  later `run.finished`.

### Index ingest (generation swap)

- Ingest takes an exclusive `flock` on `~/.bth/catalog/ingest.lock` (a separate file, never
  the database), copies `bathos.db` to `bathos.db.<gen>.tmp`, ingests into the copy, closes it,
  and `os.replace()`s it onto `bathos.db`. Readers therefore never meet a read-write lock on
  `bathos.db`: they either open the old generation or the new one.
- Watermark per file: `(project, relative path, size, byte_offset)`. If a file is smaller than
  its watermark or has vanished, it is re-read from the mirror (D7) and `bth verify` reports it;
  idempotency by `(writer, seq)` makes re-reading safe. Inodes are not used.
- Ingest runs on `bth compact`, and opportunistically at the end of any read that found more
  than 500 unindexed events (non-blocking `flock`; skipped if held).
- Ingest reads nothing but events: no filesystem, cwd or sidecar reads.
- Unreachable project roots (unmounted, slow) are skipped with a warning naming them; their
  already-ingested data stays queryable.

### Reads (G3)

- Every read uses an in-memory DuckDB connection that `ATTACH`es `bathos.db` read-only as
  `idx`, then creates in-memory tables with the index's names and columns
  (`runs`, `campaigns`, ...) as `idx.<table>` with **all** unindexed events folded in: bytes
  past each file's watermark, from every segment. Folded rows *replace* rows with the same
  natural key (anti-join), never duplicate them. Unqualified names in user SQL (`bth sql`, MCP
  `run_sql`, `query.py:416`) resolve to these in-memory tables.
- `run_sql` therefore cannot modify the index: DML hits only the in-memory copy.
- If `bathos.db` does not exist yet, the in-memory tables are built from the events alone.

### Cluster

- SLURM jobs set `BTH_WORKSPACE_ROOT` (existing mechanism) to the cluster checkout, so they log
  to its `.bth/log/`. `bth sync --pull` delegates to `myxcel pull` for that directory into the
  local main checkout's `.bth/log/remote/<remote>/` (branches on either side are irrelevant,
  since the log is ignored). The mirror is written locally when pulled segments are ingested.
  The old catalog rsync in `sync.py:54-119` is retired at cut-over.
- Refs created on the cluster clone stay there. For dirty cluster runs the pin's exported
  bundle is pulled with the segments; `bth recover --import-bundles` imports it. Clean-tree
  runs cite a commit that must exist on the remote branch; `bth check` reports it if not.

### Discovery

- `~/.bth/projects.toml` lists main roots; the first event written to a project log registers
  it (idempotent). `bth projects prune` removes vanished roots.
- **Prerequisite:** today the test suite writes pytest temp dirs into the real
  `~/.bth/projects.toml`. It must be isolated first (AC-11).

### Migration and dual-write

1. **Dual-write:** the runner and every "Authoritative writes" site emit events while still
   writing the old tiers. The old tiers remain the only read source; log-append failures are
   warnings during this phase (D6 applies from cut-over).
2. **Export:** every cool fragment and every warm row becomes events with `origin: migration`,
   written to the owning project's log (unresolvable ones to `unaffiliated/`). Natural-key
   idempotency collapses records also produced by dual-write.
3. **Build and diff:** build an index from events alone and diff it against the current
   `bathos.db` table by table. Every difference must fall in an allow-listed class (data
   already lost to earlier force-rebuilds; corrupt fragments skipped by `compact.py:787`;
   `output_metadata` whose files changed since; postmortem overrides whose files are only in a
   deleted worktree; unresolvable project) and goes into a signed-off residual report.
   Unclassified differences block cut-over.
4. **Cut-over:** reads switch to the new path; the old catalog stays read-only for one release.

## Acceptance criteria

- AC-1. For any fixed multiset of events, delete + re-ingest yields an identical index,
  regardless of ingest order, cwd, or which files exist on disk (property test). The
  last-writer-wins fields in "Fold rules" are excluded and tested separately with fixed `ts`.
- AC-2. A test enumerates every SQL write to `bathos.db` in `src/bathos/` and fails unless it is
  the ingest path; every entry in "Authoritative writes" has an event with a schema.
- AC-3. A finished run is visible to `bth show/ls/sql` and the MCP equivalents immediately, with
  no compact, including when it sits in a rotated segment.
- AC-4. Two concurrent `bth run` processes, one ingest, and one reader never raise a lock error;
  both runs are recorded.
- AC-5. Deleting a linked worktree after a run changes nothing in the index or the logs.
- AC-6. A SLURM array (including a requeued task and a task running two `bth run`s) records
  every run exactly once after two pulls.
- AC-7. A truncated final line is ignored by reads and reported by `bth verify`; an invalid line
  is quarantined with a reason.
- AC-8. With the project log unwritable: `run.started` goes to the fallback and the run proceeds;
  with both unwritable the script is not launched; a `run.finished` in the fallback exits with
  the script's status plus a warning.
- AC-9. Deleting an active segment mid-run yields `writer.resumed` and later lines are recorded.
- AC-10. With `.bth/log/` not ignored, `bth run` fails with a structured error (or `bth init`
  adds the rule). Two consecutive runs on a clean tree both record `dirty=false`.
- AC-11. An autouse fixture points `HOME` and every `BTH_*` path at `tmp_path`, and a test
  asserts every bathos path accessor resolves under it.
- AC-12. The size and latency figures here are re-measured under a pre-registered sidecar.
- AC-13. Migration step 3 produces a residual report in which every difference is classified.
- AC-14. After `git clean -fdX` in a project, `bth verify` reports the loss and
  `bth log restore` rebuilds the project log from the mirror byte-for-byte.
- AC-15. The in-memory read result equals the result after a full ingest, for the same events.
- AC-16. Root resolution tests: main checkout, linked worktree, bare main, submodule, relative
  `--git-common-dir`, `BTH_WORKSPACE_ROOT` set, no git repo.

## Order of delivery

1. AC-11 (test isolation).
2. Writer, resolution, D3 check, mirror (AC-7..10, AC-14, AC-16).
3. Dual-write at every authoritative site (AC-2).
4. Ingest with generation swap, in-memory read path (AC-1, AC-3, AC-4, AC-15).
5. Migration + residual report (AC-13), then cut-over.
6. Cluster pull (AC-6).

## Risks

| Risk | Mitigation |
|---|---|
| A write site missed | AC-2 fails the build |
| `git clean -fdX` / project dir loss | Mirror (D7) + `bth log restore` (AC-14) |
| Main checkout itself deleted | Mirror holds every line |
| Clock skew | Deterministic tie-break; skew-sensitive fields listed (AC-1) |
| Copy-then-swap cost as the index grows | 11 MB today; revisit if ingest exceeds a few seconds (measure under AC-12) |

## Future work

Cold archive of old segments, opt-in git tracking of logs, DuckLake as the index backend, Quack.
