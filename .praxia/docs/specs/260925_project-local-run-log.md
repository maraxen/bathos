---
title: Project-local append-only run log with a disposable index
task_id: 260925_bathos-project-local-log
date: 260925
status: draft
revision: v5 (after adversarial cycle 5)
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
- G2. The index (`~/.bth/catalog/index.db`, replacing `bathos.db` at cut-over) is a pure,
  disposable index: a deterministic function of the events alone (no filesystem or cwd reads
  at ingest).
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
  to `<main root>/.bth/log/<segment>` and `~/.bth/log-mirror/<project_id>/<segment>`, where
  `project_id` is a UUID stored in the project's tracked `.bth.toml` (`[project] id`). It is
  minted **only** by `bth init` (or `bth init --assign-id` for existing projects), which the
  user commits; a run never writes `.bth.toml`. The id is read from the resolved root's
  `.bth.toml` (the main checkout, per D1). A local `bth run` with no id fails with a structured
  error naming that command. SLURM jobs use `BTH_PROJECT_ID`, which `bth submit` exports from
  the local id; a job with neither writes `project_id: null` and ingest maps its `project` slug
  through `projects.toml`, with a warning. The id is stable across moves, clones and worktrees.
  Forks or copies share it deliberately; segment names never collide, `bth log restore` only
  restores events whose `worktree_root` lies under the restoring root, and `bth verify` flags
  two registered roots with the same id. The mirror
  protects against `git clean -fdX` and loss of the main checkout. **Ingest reads both copies
  and deduplicates by `eid`**, so neither is "authoritative on divergence": a line that reached
  only one of them is still ingested. A mirror append failure is a warning, not a run failure.
  `bth log restore` copies back into the project log every mirror line (for this root) that the
  project log lacks; lines that reached neither copy are reported by `bth verify`, not invented.

## Authoritative writes (everything the index must not own)

AC-2 enforces that this table stays complete. The key column is the **entity key** used by the
fold; event identity is the `eid` (see Line envelope).

| Current write | Location | Event (natural key) |
|---|---|---|
| run row, start and finish | `runner.py:524,623,847` | `run.started` (`run_id`; `data` includes the parsed sidecar declaration and its sha256, so e-value inputs travel with the run), `run.finished` (`run_id`) |
| submit provenance, one Parquet per record | `catalog.py:109` (`write_submit_provenance`; read by `sprint_audit.py:226`) | `submit.recorded` (`submit_id`; includes `slurm_job_id`) |
| `runs.output_metadata` (drift baseline, `checker.py:132`) | recomputed at compact, `compact.py:959-976` | `run.outputs_hashed` (`run_id`): at run end, and on explicit `bth check --rebaseline`. Behaviour change: compact no longer silently refreshes the baseline when outputs change |
| outcome + `postmortem_*` override | `compact.py:1012` (reads files via cwd) | `run.postmortem_applied` (`run_id`, postmortem sha256), emitted by postmortem validate/register |
| `claim_discriminates`, `claim_isolates`, `parity_run_type` COALESCE | `compact.py:978` | carried in `run.started.data` from the sidecar |
| `runs.metadata` rewrite by reaper | `reap.py:361` | `run.reaped` (`run_id`) |
| campaign insert/upsert | `campaigns.py`, `campaigns.py:1194`, `claim.py:649` | `campaign.created` (`campaign_id`) |
| `claim_path`, `claim_sha256` (Union Gate tamper anchor) | `claim.py:684`, `claim.py:1062` (incl. attest_parity rollback) | `campaign.claim_bound` (`campaign_id`, sha256); a rollback is a new event, not a deletion |
| `stopping_threshold` set explicitly by a command | `campaigns.py:340` | `campaign.threshold_set` (`campaign_id`) |
| `evalue`, `seq_position`, threshold lock | `campaigns.py:316-351`, `:414-521`, `:508` (recomputed from sidecar files at compact) | **no event**: derived by the campaign fold (see Fold rules) |
| `claim_mode='bypassed'` | `campaigns.py:1043` | `campaign.claim_bypassed` (`campaign_id`) |
| other campaign updates | `campaigns.py:1222` | `campaign.updated` (`campaign_id`) |
| `campaign_runs` insert | `campaigns.py` | `campaign.run_added` (`campaign_id`, `run_id`); membership is ALSO implied by `run.started.data.campaign_id` (`campaigns.py:414`) |
| campaign conclusion | `campaigns.py` | `campaign.concluded` (`campaign_id`) |
| `campaign_edges`, `run_edges` | `campaign_edges.py` | `edge.added` (`src`, `dst`, `type`) |
| `blast_radius_ledger` | `blast_radius.py:231`, `compact.py:620` | `blast_radius.recorded` (existing ledger id) |
| `sidecar_anchors` insert | `anchor.py:211` | `anchor.recorded` (`anchor_id`) |
| `sidecar_anchors` update (`campaign_id`, `anchored_at`, kind/label/hash) | `compact.py:728-733` | `anchor.updated` (`anchor_id`) |
| cool campaign JSON, rewritten in place | `campaigns.py:48-53` (`write_campaign_cool`) | replaced by the `campaign.*` events above; the JSON tier is retired at cut-over |
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
- After any failed or partial write the writer abandons that segment and starts a new one, so a
  torn line is never followed by more lines from the same writer. A segment deleted mid-run is
  covered by the mirror (D7), not by detection in the writer.

### Line envelope

```json
{"v": 1, "eid": "<uuidv7>", "kind": "campaign.threshold_set", "entity": ["<campaign_id>"],
 "ts": "<RFC3339 UTC>", "project": "<slug or null>", "project_id": "<uuid or null>",
 "writer": "<segment stem>", "seq": 17, "worktree_root": "<abs path>",
 "origin": "live|migration", "data": { ... }}
```

- `eid` (UUIDv7, generated by the writer) is the **event identity**: every event, create or
  update, is ingested at most once per `eid`, whichever copy (project log, mirror, pulled
  remote) it arrives from. `(writer, seq)` remains for gap/duplicate diagnostics only.
- `entity` is the **entity key** from the table; the fold groups events by it.
- `data` is validated against a per-kind JSON Schema at ingest; invalid or torn lines anywhere
  in a file go to a `quarantine` table with the reason and file offset.

### Fold rules

- The index stores every accepted event verbatim in an `events` table keyed by `eid`. Derived
  tables are a fold over it.
- One fold function (`bathos.index.fold`) is used by both ingest and the read path, and it
  always **re-folds an affected entity from its complete event history** (indexed events plus
  new ones, sorted), never by applying new events on top of the stored row. So the result is
  independent of arrival order.
- For each entity, events are applied in `(ts, eid)` order; UUIDv7 makes the tie-break total
  and deterministic for a fixed event set.
- **Merge policy per kind reproduces today's semantics exactly**, verified by a differential
  test that feeds the same operation sequences to the old code and the new fold (AC-17). From
  `campaigns.py:124-140`: `status='concluded'` and `concluded_at` are sticky once set; `name`,
  `question`, `hypothesis`, `started_at` take the latest non-empty value; claim fields and
  `stopping_threshold` follow what the current upsert/update code does, not a new rule. Any
  divergence AC-17 finds is a spec bug to fix here, not a behaviour change.
- **Imported history:** the legacy importer emits exactly one `<kind>.imported` full-state
  event per entity, built by merging that entity's legacy sources in precedence order
  **warm row > cool fragment > campaign JSON** (field by field: the first source with a
  non-empty value wins). Its `ts` is the source record's own time (run: end time if finished,
  else start; campaign: `concluded_at` or `started_at`; ledger rows: their own timestamp). Its
  eid is `uuid5(NAMESPACE_BATHOS, f"import:{kind}:{entity}:{sha256(merged state)}")`, so
  re-importing unchanged sources is a no-op and a changed source (e.g. a straggler fragment
  that now says `finished`) yields a new event. Several imported events for one entity fold by
  status rank (`running` < terminal) and then `ts`. Imported and live events never describe the
  same fact: after cut-over, every new run and campaign is created only by live events.
- **Campaign-derived values:** a campaign's members are the union of `campaign.run_added` and
  runs whose `run.started.data.campaign_id` names it. `seq_position`, `evalue` and the threshold
  lock are **computed by the campaign fold** over all members sorted by `(run ts, run_id)`,
  using each member's sidecar declaration from `run.started.data` and its folded outcome. That
  reproduces `campaigns.py:414-521` without reading files. Any event on a member run marks its
  campaign(s) as affected, at ingest and in the read views. A member with `ts` before
  `concluded_at` that arrives after conclusion is included, and `bth verify` reports
  `evalue_changed_after_conclusion` rather than silently altering what a stopping decision saw.
- `run.started` without `run.finished` folds to `status='incomplete'` until `run.reaped` or a
  later `run.finished`.
- Cross-host clock skew can change which of two conflicting last-writer-wins updates wins; it
  cannot make the result depend on arrival order. AC-1 names the affected fields.

### Index ingest (generation swap)

- **Separate file until cut-over:** the new index is `~/.bth/catalog/index.db`. `bathos.db` is
  never swapped or written by the new path; legacy writers keep using it until cut-over, after
  which it is frozen read-only as a fallback.
- Ingest inserts new events into `idx.events` (ignoring known eids) and re-folds every affected
  entity from its full history.
- Ingest runs only on the local machine (never on cluster nodes, which only append logs). It
  takes an exclusive `flock` on `~/.bth/catalog/ingest.lock` (local disk), copies `index.db` to
  `index.db.<gen>.tmp`, ingests into the copy, runs `CHECKPOINT`, closes it, refuses to proceed
  if `index.db.<gen>.tmp.wal` still exists, then `os.replace()`s it onto `index.db`.
- Watermark per file: `(root kind, root id, path relative to that root, size, byte_offset)`,
  where root kind is `project` (root id = `project_id`), `mirror`, `fallback` or
  `unaffiliated`. A file smaller than its watermark, or vanished, is re-read from the other copy
  (D7) and reported by `bth verify`; `eid` dedup makes re-reading safe. Inodes are not used.
- **When ingest runs:** on `bth compact`, and non-blockingly (skipped if the lock is held) at the
  end of commands that write events (`bth run`, campaign and claim commands, MCP equivalents).
  Read commands, including `bth view`, never ingest.
- Ingest reads nothing but events: no filesystem, cwd or sidecar reads.
- Unreachable project roots are skipped with a warning; their mirror is still ingested.

### Reads (G3)

- **One read API.** `bathos.index.connect_read()` returns an in-memory DuckDB connection that
  `ATTACH`es `index.db` read-only for this call only (never held across calls, including by the
  long-lived MCP server, so it never pins an old generation).
- It reads each log file's bytes past its watermark, collects the affected entity keys, loads
  those entities' indexed events from `idx.events`, and re-folds just those entities into small
  in-memory tables. For each index table it then creates a **view** with the table's name and
  columns: `idx.<table>` rows whose key is not affected, `UNION ALL` the re-folded rows. Cost
  scales with the unindexed delta, not the index size. Unqualified names in user SQL
  (`bth sql`, MCP `run_sql`) resolve to these views, and views reject DML, so user SQL can
  never modify the index.
- All 25 modules that call `duckdb.connect(` today (65 call sites) move to this API or to the
  ingest path; AC-18 fails the build on any other `duckdb.connect` against a catalog path.
- If `index.db` does not exist yet, the views are built from the events alone.

### Cluster

- SLURM jobs set `BTH_WORKSPACE_ROOT` (existing mechanism) to the cluster checkout, so they log
  to its `.bth/log/`. `bth sync --pull` delegates to `myxcel pull` for that directory into the
  local main checkout's `.bth/log/remote/<remote>/` (branches on either side are irrelevant,
  since the log is ignored). The mirror is written locally when pulled segments are ingested.
  The old catalog rsync in `sync.py:54-119` narrows at cut-over to pulling legacy fragments
  only, and is removed by `bth migrate --retire-legacy` (Migration step 5).
- Refs created on the cluster clone stay there. For dirty cluster runs the pin's exported
  bundle is pulled with the segments; `bth recover --import-bundles` imports it. Clean-tree
  runs cite a commit that must exist on the remote branch; `bth check` reports it if not.

### Discovery

- `~/.bth/projects.toml` lists main roots; the first event written to a project log registers
  it (idempotent). `bth projects prune` removes vanished roots.
- **Prerequisite:** today the test suite writes pytest temp dirs into the real
  `~/.bth/projects.toml`. It must be isolated first (AC-11).

### Migration (quiesced cut-over, no dual-write)

There is no dual-write phase: the new path is built and tested against fixture catalogs, then
switched on in one step.

0. **Ids:** `bth migrate --to-log` refuses to start until every registered root's `.bth.toml`
   has a `[project] id` present in its committed `HEAD` (it lists the roots missing one and
   the `bth init --assign-id` command).
1. **Quiesce:** it runs `bth reap` first (reconciling stale `running` rows through `sacct`),
   then refuses while any job in the user's `squeue` has a job id found in submit records or
   in a `running` run's `slurm_job_id`. Other queued jobs are listed; proceeding past them
   needs `--force`.
2. **Import:** convert every cool fragment, submit record, campaign JSON, and every row of every
   table in "Authoritative writes" into `*.imported` events (Fold rules), written to the owning
   project's `.bth/log/` (unresolvable ones to `unaffiliated/`).
3. **Build and diff:** build `index.db` from events alone and diff it against `bathos.db` table
   by table. Every difference must fall in an allow-listed class (data already lost to earlier
   force-rebuilds; corrupt fragments skipped by `compact.py:787`; `output_metadata` whose files
   changed since; postmortem overrides whose files are only in a deleted worktree; e-values
   whose sidecar changed since compact last read it; unresolvable project) and goes into a
   signed-off residual report. Unclassified differences abort.
4. **Switch:** reads and writes move to the new path. `bathos.db` is renamed to
   `bathos.db.frozen` and kept read-only for one release.
5. **Stragglers and stale installs:** an older bathos (e.g. pinned in a project venv, or on the
   cluster) may keep writing cool fragments, submit Parquet, or a fresh `bathos.db`. The
   straggler importer converts all three on `bth compact` and `bth sync --pull`, and
   `bth verify` reports every legacy write after cut-over with the writing host. The legacy
   fragment pull from the cluster stays enabled until `bth migrate --retire-legacy`, which
   refuses while any unimported legacy data exists.

## Acceptance criteria

- AC-1. For any fixed multiset of events, delete + re-ingest yields an identical index,
  regardless of ingest order, cwd, or which files exist on disk (property test). The
  last-writer-wins fields in "Fold rules" are excluded and tested separately with fixed `ts`.
- AC-2. A test enumerates every SQL write to a catalog database and every file write under
  `~/.bth/catalog` in `src/bathos/`, and fails unless it is the ingest path or the log writer;
  every entry in "Authoritative writes" has an event with a schema.
- AC-3. A finished run is visible to `bth show/ls/sql` and the MCP equivalents immediately, with
  no compact, including when it sits in a rotated segment.
- AC-4. Two concurrent `bth run` processes, one ingest, and one reader never raise a lock error;
  both runs are recorded.
- AC-5. Deleting a linked worktree after a run changes nothing in the index or the logs.
- AC-6. A SLURM array (including a requeued task and a task running two `bth run`s) records
  every run exactly once after two pulls.
- AC-7. A torn line at the end of a segment or in the middle of a file (followed by good lines)
  is quarantined with its offset and reported by `bth verify`; all good lines are ingested.
- AC-8. With the project log unwritable: `run.started` goes to the fallback and the run proceeds;
  with both unwritable the script is not launched; a `run.finished` in the fallback exits with
  the script's status plus a warning.
- AC-9. Deleting an active segment mid-run loses no event: every line is ingested from the mirror.
- AC-10. With `.bth/log/` not ignored, `bth run` fails with a structured error (or `bth init`
  adds the rule). Two consecutive runs on a clean tree both record `dirty=false`.
- AC-11. An autouse fixture points `HOME` and every `BTH_*` path at `tmp_path`, and a test
  asserts every bathos path accessor resolves under it.
- AC-12. The size figures here, and read-path latency against today's `bth ls`/`bth sql`, are
  measured under a pre-registered sidecar. The latency budget is set in that sidecar, with a
  stated basis, before the measurement runs; this spec does not invent one.
- AC-13. Migration step 3 produces a residual report in which every difference is classified.
- AC-14. After `git clean -fdX` in a project, `bth verify` reports the loss, and after
  `bth log restore` every event present in the mirror for that root is back in the project log
  and the index is unchanged.
- AC-15. For the same events, the read views equal the tables produced by a full ingest.
- AC-17. Differential fold test: for randomized operation sequences on campaigns, anchors and
  runs, the old upsert/update code and the new fold produce identical rows.
- AC-18. No module other than `bathos.index` and the ingest path calls `duckdb.connect` on a
  catalog path (AST test).
- AC-19. Ingest refuses the swap when a `.wal` remains after close, and a reader that attached
  the previous generation completes its query correctly across the `os.replace`.
- AC-20. Arrival-order independence: delivering the same events in any order and in any
  batching (including a late event with an earlier `ts`) yields the same index and the same
  read results as one sorted full ingest.
- AC-21. Importing the same legacy catalog twice changes nothing the second time; importing a
  late fragment for an already-imported run changes that run exactly once (e.g. running to
  finished).
- AC-22. Campaign fold: for sequential campaigns, the fold's `seq_position`, `evalue` and
  threshold lock equal what `campaigns.py:414-521` computes for the same runs and sidecars, and
  do not depend on the order in which member-run events arrive.
- AC-23. After cut-over, a write by an older bathos (fragment, submit Parquet, or new
  `bathos.db`) is imported and reported by `bth verify`.
- AC-16. Root resolution tests: main checkout, linked worktree, bare main, submodule, relative
  `--git-common-dir`, `BTH_WORKSPACE_ROOT` set, no git repo.

## Order of delivery

1. AC-11 (test isolation).
2. Writer, resolution, D3 check, `project_id` via `bth init`/`--assign-id`, mirror (AC-7,
   AC-9, AC-10, AC-14, AC-16), behind a feature flag; production still uses the old tiers.
3. `index.db`, `events` table, fold (incl. the campaign fold), generation-swap ingest, read API;
   move all 25 modules to it, exercised against fixture catalogs (AC-1, AC-15, AC-17, AC-18,
   AC-19, AC-20, AC-22).
4. Legacy and straggler importer (AC-21, AC-23) and cluster log pull (AC-6).
5. Assign ids in every registered project (migration step 0), then cut-over via
   `bth migrate --to-log` (AC-13); then AC-2, AC-3, AC-4 and AC-8 hold in production.

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
