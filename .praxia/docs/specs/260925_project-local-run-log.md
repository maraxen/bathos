---
title: Project-local append-only run log with a disposable index
task_id: 260925_bathos-project-local-log
date: 260925
status: draft
revision: v21 (after adversarial cycle 21)
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
  (`bth init`) or fails `bth run` with a structured error. With no git repository (a
  `.bth.toml`-only project) there is nothing to dirty and the check is skipped.
- **D4. Provenance comes only from cisternal.** `run.started` embeds `GitState` (hash, branch,
  dirty, dirty_content_id, provenance_source) and the `PinResult` (pinned SHA,
  `refs/bathos/runs/<run_id>` or `refs/bathos/wip/<run_id>` for a dirty tree, manifest entry),
  computed where `runner.py` computes them today (`capture_git_state` at `:415`, `pin_run` at
  `:546`, both before the subprocess). bathos adds no
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
  Forks or copies share it deliberately; segment names never collide between writers (a
  `cp -r` copy duplicates existing files, which eid dedup absorbs), and `bth verify` flags
  two registered roots with the same id (AC-24). The mirror
  protects against `git clean -fdX` and loss of the main checkout. **Ingest reads both copies
  and deduplicates by `eid`**, so neither is "authoritative on divergence": a line that reached
  only one of them is still ingested. Events with `project_id: null` (unaffiliated, or a SLURM job without an id) mirror to
  `~/.bth/log-mirror/_null/<slug or _unaffiliated>/`. A mirror append failure is a warning, not a run failure.
  Fallback appends (D6) are mirrored the same way. `bth log restore` copies back into the
  project log every mirror line that the project log lacks and whose `main_root` is not another
  *live* root with the same `project_id`, where live means listed in `projects.toml`, the path
  exists, and its `.bth.toml` carries this id. So a moved project (old path gone, pruned or not)
  recovers its history, and a fork (other root still live) never receives the other copy's
  events; lines that reached neither copy are reported by `bth verify`, not invented.

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
| `runs.metadata` rewrite by reaper, cool-fragment rewrite and reap ledger JSON | `reap.py:292-304`, `:361`; `catalog/reaped/<slug>/<run_id>.json` | `run.reaped` (`run_id`; carries `prior_status`) |
| reap revert (ledger JSON moved to `reverted/`) | `reap.py:203-217` | `run.reap_reverted` (`run_id`) |
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
| trust ledger fragment | `trust_ledger.py:120` (`ledger_<id>.parquet`) | `trust_ledger.recorded` (record `id`) |
| archived-item fragment | `archived_items.py:108` (`archived_<record_id>.parquet`) | `archived_item.recorded` (`record_id`; `data.event` is `archived` or `restored`, `data.id` is the item) |
| `amendments` | created at `compact.py:430`; no writer exists today | none until a writer is added (AC-2 then requires one) |

**Import kinds** (legacy importer only, `origin: "migration"`), one per legacy entity, same entity
key as the live kind:

| Import kind | Entity key | Built from |
|---|---|---|
| `run.imported` | `run_id` | warm `runs` row, cool fragment, reap ledger JSON |
| `run_reap.imported` | `run_id` | reverted reap ledger JSON (`reaped/<slug>/reverted/`) |
| `submit.imported` | `submit_id` | submit-provenance Parquet |
| `campaign.imported` | `campaign_id` | warm `campaigns` row, campaign JSON |
| `campaign_run.imported` | `(campaign_id, run_id)` | warm `campaign_runs` row (with stored `evalue`) |
| `edge.imported` | `(src, dst, type)` | warm `campaign_edges`, `run_edges` |
| `blast_radius.imported` | ledger id | warm `blast_radius_ledger`, fragment |
| `anchor.imported` | `anchor_id` | warm `sidecar_anchors` |
| `trust_ledger.imported` | record `id` | warm `trust_ledger`, fragment |
| `archived_item.imported` | `record_id` | warm `archived_items`, fragment |
| `legacy_source.unreadable` | `source_locator` | any legacy file that cannot be parsed (manifest bookkeeping only) |

## Design

### Mode (the flag and cut-over)

- **Cut-over marker:** `~/.bth/catalog/cutover.json` (`{"at": <RFC3339>, "bathos": <version>}`),
  written atomically (`os.replace`) by Migration step 4 (see its action order). "Before
  cut-over" and "after cut-over" everywhere in this spec mean "marker absent" and "marker
  present" in the local catalog.
- **The flag** (log mode) is on iff the local marker exists, or `BTH_LOG_MODE=1` is set in a
  context where it is honoured: a SLURM job (`SLURM_JOB_ID` set) or a test
  (`PYTEST_CURRENT_TEST` set, with a non-default `BTH_CATALOG_DIR`). Anywhere else
  `BTH_LOG_MODE=1` without the marker is refused with a structured error, so the real local
  catalog can never be half-switched.
- **Cluster jobs:** a compute node has no marker (its catalog is the remote
  `<remote_root>/.bth/catalog`, `cluster_catalog.py:21-26`). After cut-over, `bth submit`
  exports `BTH_LOG_MODE=1` into the job environment, so the job appends events to the cluster
  checkout's `.bth/log/` (Cluster). A job submitted with plain `sbatch`, bypassing `bth submit`,
  stays in legacy mode; its pulled fragments are reported by `bth verify` as legacy writes
  (Migration step 5).
- **Mode is fixed once per unit of work:** read at the start of each CLI process and each MCP
  tool invocation (for a writer, only after it holds the writers lock, below), and never
  re-read within it, so one command never mixes a legacy write with
  an event. A long-lived MCP server therefore switches at cut-over without a restart, from its
  next tool call.
- **Writers lock:** every local command that writes (legacy or events) first takes a shared,
  blocking `flock` on `~/.bth/catalog/writers.lock` (local disk), then reads the mode, and holds
  the lock for its duration; `bth migrate --to-log` takes it exclusively (Migration step 1). So
  a writer that waited through the switch starts in the new mode, and no local writer spans it.
  SLURM jobs (`SLURM_JOB_ID` set) skip the lock: their catalog is remote, they only append logs,
  and the switch concerns local writers.
- Flag on: events only, `connect_read` builds views. Flag off: legacy writes only, the
  pass-through in "Reads".

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
- After any failed or partial write to either copy, the writer abandons that segment name in
  both copies and starts a new segment, so a torn line is never followed by more lines from the
  same writer in either copy. A segment deleted mid-run is
  covered by the mirror (D7), not by detection in the writer.

### Line envelope

```json
{"v": 1, "eid": "<uuidv7>", "kind": "campaign.threshold_set", "entity": ["<campaign_id>"],
 "ts": "<RFC3339 UTC>", "project": "<slug or null>", "project_id": "<uuid or null>",
 "writer": "<segment stem>", "seq": 17, "main_root": "<abs path, per D1>",
 "worktree_root": "<abs path>",
 "origin": "live|migration", "data": { ... }}
```

- `eid` (UUIDv7, generated by the writer) is the **event identity**: every event, create or
  update, is ingested at most once per `eid`, whichever copy (project log, mirror, pulled
  remote) it arrives from. `(writer, seq)` remains for gap/duplicate diagnostics only.
- `entity` is the **entity key** from the table; the fold groups events by it.
- `origin` is `live` (written by a command) or `migration` (written by the legacy importer);
  there is no third value.
- `data` is validated against a per-kind JSON Schema (shipped in `bathos.index.schemas`, one
  file per kind) at ingest; invalid or torn lines anywhere in a file go to a `quarantine` table
  with the reason and file offset.
- **Only `\n`-terminated lines are read.** Bytes after a file's last `\n` are never ingested
  or quarantined; the watermark stops at the last `\n`. `bth verify` (not ingest) reports an
  unterminated tail as torn once its writer has a later segment or the file is unchanged for
  7 days. A torn line in the middle of a file (followed by a `\n`) is quarantined at ingest.
- An `eid` already in `events` is skipped if its `(kind, entity, data)` is identical (envelope
  fields such as `writer`, `seq`, `worktree_root` may differ between copies or re-imports); if
  that content differs, the new line is quarantined with reason `eid_conflict` and `bth verify`
  reports it.
- The `quarantine` table is diagnostic state outside the fold; AC-1 and AC-20 exclude it.

### Fold rules

- The index stores every accepted event verbatim in an `events` table keyed by `eid`. Derived
  tables are a fold over it.
- One fold function (`bathos.index.fold`) is used by both ingest and the read path, and it
  always **re-folds an affected entity from its complete event history** (indexed events plus
  new ones, sorted), never by applying new events on top of the stored row. So the result is
  independent of arrival order.
- **An entity folds in two stages:** (1) its `*.imported` events merge into a base state by the
  import merge rule below; (2) its live events apply on top in `(ts, eid)` order, except for run
  status (see "Run status"), which is ranked across both stages. UUIDv7 (and uuid5 for imports)
  makes the tie-break total and deterministic for a fixed event set.
- **Merge policy per kind reproduces today's semantics exactly**, verified by a differential
  test that feeds the same operation sequences to the old code and the new fold (AC-17). From
  `campaigns.py:124-140`: `status='concluded'` and `concluded_at` are sticky once set; `name`,
  `question`, `hypothesis`, `started_at` take the latest non-empty value; claim fields and
  `stopping_threshold` follow what the current upsert/update code does, not a new rule. Any
  divergence AC-17 finds is a spec bug to fix here, not a behaviour change.
- **Imported history:** the legacy importer emits one `<kind>.imported` event **per (entity,
  legacy source)**, never a pre-merged one: a run with a warm row and a cool fragment gets two
  `run.imported` events; each legacy `campaign_runs` row gives a `campaign_run.imported` carrying
  its stored `evalue` (not `seq_position`, which the fold always recomputes). Each carries `data.source_class`, one of `warm`
  (`bathos.db.frozen`, or `bathos.db` before cut-over), `warm_recreated` (a `bathos.db` recreated
  after cut-over by an older install), `fragment`, `campaign_json`, `ledger_json` (live reap
  ledger), `ledger_reverted` (reverted reap ledger) or `submit_parquet`; precedence is that order.
  (`corrupt`, below, marks manifest-only chains and never enters a fold.)
  Each also carries `data.source_locator` and `data.source_sha256`: sha256 of the canonical
  source record, which is the record as a JSON object with sorted keys, no whitespace, UTF-8,
  timestamps as RFC3339 UTC and floats in shortest round-trip form. This form is versioned
  (`data.canon: 1`) and never changed in place. The locator is the source file's path relative to the catalog dir; for a warm
  row it is `warm:<table>` for both `bathos.db` and `bathos.db.frozen` (so the step-4 rename
  changes nothing), and `bathos.db:<table>` only for class `warm_recreated`. The importer never reads current sidecar or output
  files. `ts` is the source record's own time (run: end time if finished, else start; campaign:
  `concluded_at` or `started_at`; ledger rows: their own timestamp). eid = `uuid5(NAMESPACE_BATHOS,
  f"import:{kind}:{entity}:{source_class}:{source_locator}:{snapshot}:{source_sha256}")`,
  where `data.snapshot` is an ordinal per snapshot chain.
- **Snapshots:** a snapshot chain is one `(kind, entity, source_class, source_locator)`: one
  concrete source record. For each chain the importer compares the source's hash with the
  `source_sha256` of the newest existing snapshot (highest ordinal) of that chain. Equal: it appends nothing, so
  re-running on unchanged sources is a no-op. Different (including a source that changed back
  to an earlier content): it appends a new event with ordinal = newest + 1. Only the highest
  ordinal of each chain takes part in the fold; every chain does (so several reverted ledgers of
  one run all fold). The importer holds the ingest lock, so ordinals never race.
  "Existing" means, before cut-over, the current staging attempt and the destination logs; after
  cut-over, the index and the destination logs.
- **Unreadable sources** (e.g. corrupt fragments, skipped today at `compact.py:787`) have no
  record to import. The importer records each in `import_manifest` as a chain with
  `source_class=corrupt` and the sha256 of the file's bytes, via a `legacy_source.unreadable`
  event (entity: `source_locator`); it takes no part in any fold. `bth verify` applies the same
  byte-hash test, so a corrupt source is reported once as `corrupt_legacy_source` (matching the
  step-3 residual report), not as a legacy write, and is reported again only if its bytes change.
- **Reap ledgers:** each `catalog/reaped/<slug>/<run_id>.json` imports as an abandoned claim
  (`run.imported`, `source_class=ledger_json`, `ts` = its `reaped_at`). Each
  `reverted/<run_id>.<YYYYmmddHHMMSS>.json` (`reap.py:213-217`, UTC, one-second resolution)
  imports as `run_reap.imported` (`source_class=ledger_reverted`) carrying `reaped_at` and
  `reverted_at = max(reaped_at, filename time)`; the fold treats it as an abandoned claim at
  `reaped_at` followed by a revert at `reverted_at`, the revert always after its own claim. So a
  pre-cut-over revert is honoured whatever the warm row says.
- **Import merge (stage 1)**, field by field, never whole-state replacement:
  - *Campaigns:* `status='concluded'` is sticky: the base is concluded if any import says so,
    with `concluded_at` taken by source precedence, then earliest `ts`.
  - *Every field except run status* keeps the first non-empty value in order of source
    precedence, then `ts`, then eid. So a fragment never blanks a field (e.g. `metadata`,
    `output_metadata`, postmortem fields) that a warm import set.
- **Run status (both stages).** The status-dependent fields (`status`, end time, `exit_code`,
  `outcome` when not overridden) come together from the single highest-ranked *status claim*:
  - rank 2, terminal (`completed`, `failed`, `killed`; `runner.py:613,657,679,687`): a
    `run.finished`, or a `run.imported` with a terminal status;
  - rank 1, `abandoned` (`reap.py:288`): a `run.reaped` or a `run.imported` with status
    `abandoned`, unless a `run.reap_reverted` (live, or the revert inside a `run_reap.imported`)
    follows it in `(ts, is_revert, eid)` order (`is_revert` is 0 for a claim, 1 for a revert, so
    at equal `ts` a revert sorts after every abandoned claim); a revert cancels every earlier or
    same-`ts` abandoned claim, imported ones included;
  - rank 0, `running`: otherwise (a `run.started`, or a `run.imported` with status `running`).
    This is today's stored value for a started, unfinished run (`runner.py:456`), so the fold
    stays row-identical under AC-17 and the reaper keeps selecting `status == 'running'`
    (`reap.py:223`).
  Ties go by source precedence (live counts as highest), then later `ts`, then eid. So a real
  finish beats a reap whatever their `ts`, including a finish pulled from the cluster after the
  reap, and a stale warm `running` never hides a fragment's terminal status. After cut-over the
  reaper selects runs whose folded status is `running` and records that as `prior_status`; a
  revert with no terminal claim folds back to `running`.
- **Behaviour change (stated, not accidental):** an imported campaign member has no sidecar
  declaration in the log, so a postmortem applied to it after cut-over changes its folded
  `outcome` but not its stored `evalue`; today's compact would recompute it from the sidecar.
- **Legacy writes after cut-over** (an older bathos still writing fragments, submit Parquet or a
  fresh `bathos.db`) are not imported automatically. `bth verify` reports them with the writing
  host, and the user re-runs `bth migrate --import-legacy`, which appends only new eids.
- **Campaign-derived values:** a campaign's members are the union of `campaign.run_added`,
  `campaign_run.imported`, and runs whose `run.started.data.campaign_id` or
  `run.imported.data.campaign_id` names it. The campaign fold reproduces
  `campaigns.py:380-521`:
  - members are sorted by the folded run **start time** `runs.timestamp` (null sorts first, as
    `datetime.min` UTC), then `run_id` (`_run_sort_key`, `campaigns.py:406-412`). The event `ts`
    is never used for this order.
  - each member's outcome is its folded `runs.outcome` (postmortem override applied).
  - a member with a sidecar declaration in `run.started.data` gets `evalue` computed from it; a
    member without one (imported history) keeps its stored `evalue` from
    `campaign_run.imported`, mirroring `evalue = COALESCE(?, evalue)` (`campaigns.py:517`).
  - the threshold lock is computed as today; on a threshold mismatch the campaign is skipped as
    today (`campaigns.py:500-504`) and `bth verify` reports it.
  Any event on a member run marks its campaign(s) as affected, at ingest and in the read views.
  A member whose folded end time (`run.finished.ts`, or the imported end time) is later than the
  campaign's conclusion time (`campaign.concluded.ts`, or the imported `concluded_at`) is still
  included, and `bth verify` reports `evalue_changed_after_conclusion` for that campaign. This
  depends only on event contents, not arrival.
- **Clock-skew-sensitive results** (the single list; AC-1 refers here). These are decided by
  `(ts, eid)` order between events that may come from different hosts, so cross-host clock skew
  can change the winner, though never make it depend on arrival order:
  - campaign `name`, `question`, `hypothesis`, `started_at` (latest non-empty) and every field
    set by `campaign.updated`, `campaign.threshold_set`, `campaign.claim_bound`,
    `campaign.claim_bypassed`;
  - anchor fields set by `anchor.updated`;
  - `runs.output_metadata` (latest `run.outputs_hashed`) and postmortem fields (latest
    `run.postmortem_applied`);
  - whether a `run.reap_reverted` follows a given abandoned claim;
  - ties between two status claims of equal rank and source precedence;
  - `evalue_changed_after_conclusion` (compares `ts` values).

### Index ingest (generation swap)

- **Separate file until cut-over:** the new index is `~/.bth/catalog/index.db`. `bathos.db` is
  never swapped or written by the new path; legacy writers keep using it until cut-over, after
  which it is frozen read-only as a fallback.
- Ingest inserts new events into `idx.events` (ignoring known eids) and re-folds every affected
  entity from its full history.
- Ingest runs only on the local machine (never on cluster nodes, which only append logs): it is
  skipped whenever `SLURM_JOB_ID` is set or `BTH_NO_INGEST=1`. It
  takes an exclusive `flock` on `~/.bth/catalog/ingest.lock` (local disk), copies `index.db` to
  `index.db.<gen>.tmp`, ingests into the copy, runs `CHECKPOINT`, closes it, refuses to proceed
  if `index.db.<gen>.tmp.wal` still exists, then `os.replace()`s it onto `index.db`.
- Watermarks live in an `ingest_watermarks` table inside `index.db`, so they swap atomically
  with the data they describe. One row per file: `(root kind, root id, path relative to that
  root, size, byte_offset)`,
  where (root kind: root id) is one of `project`: `main_root` (the main root's `.bth/log/`,
  excluding `remote/`); `mirror`: `project_id` or `_null/<slug>`; `fallback`: slug;
  `unaffiliated`: constant; `remote-log`, `remote-fallback`, `remote-mirror`:
  `(main_root, remote)`; `staging`: the attempt id (migration step 3 only, never read by
  `connect_read`). Root ids use `main_root`, not `project_id`, wherever two roots can share
  an id, so every file belongs to exactly one root. A file smaller than its watermark, or vanished, is re-read from the other copy
  (D7) and reported by `bth verify`; `eid` dedup makes re-reading safe. Inodes are not used.
- **Flag gate:** ingest runs only with the flag on (see "Mode"), or from
  `bth migrate --to-log` step 4; with the flag off, `bth compact` and the end-of-command hook
  run only the legacy compaction. So `~/.bth/catalog/index.db` is never created before the
  switch.
- **When ingest runs:** on `bth compact`, and non-blockingly (skipped if the lock is held) at the
  end of commands that write events (`bth run`, campaign and claim commands, MCP equivalents).
  Read commands, including `bth view`, never ingest.
- Ingest reads nothing but events: no filesystem, cwd or sidecar reads.
- Unreachable project roots are skipped with a warning; their mirror is still ingested.

### Reads (G3)

- **One read API.** `bathos.index.connect_read()` returns an in-memory DuckDB connection that
  `ATTACH`es `index.db` read-only for this call only (never held across calls, including by the
  long-lived MCP server, so it never pins an old generation).
- On every call it enumerates the roots afresh: each main root in `projects.toml` (including its
  `.bth/log/remote/`), `~/.bth/log-mirror/`, `~/.bth/log/fallback/` and
  `~/.bth/log/unaffiliated/`. It then reads each log file's bytes past its watermark, collects the affected entity keys, loads
  those entities' indexed events from `idx.events`, and re-folds just those entities into small
  in-memory tables. For each index table it then creates a **view** with the table's name and
  columns: `idx.<table>` rows whose key is not affected, `UNION ALL` the re-folded rows. Cost
  scales with the unindexed delta, not the index size. Unqualified names in user SQL
  (`bth sql`, MCP `run_sql`) resolve to these views, and views reject DML, so user SQL can
  never modify the index.
- All 25 modules that call `duckdb.connect(` today (65 call sites) move to this API or to the
  ingest path; AC-18 fails the build on any other `duckdb.connect` against a catalog path.
- If `index.db` does not exist yet, the views are built from the events alone.
- **Before cut-over (flag off)** `connect_read(path, read_only=..., missing=...)` is a
  pass-through: it opens `bathos.db` with exactly the arguments that call site uses today
  (most sites open `read_only=True`, e.g. `linter.py:422`, `prereg.py:165`; `run_sql` opens
  read-write and falls back to `""` when the file is missing, `query.py:427-429`), creates no
  views and does not use `connect_legacy`. Legacy writers (e.g. `campaigns.py:45`,
  `anchor.py:198`, `compact.py:838`) keep their own opens on the AC-18 test's allow-list and
  never go through `connect_read`. So production behaviour, including unqualified table names,
  DML in `bth sql` and today's lock failure, is unchanged until Migration step 4 (the switch);
  the read-only views and the DML refusal are part of the switch. G3 holds only from cut-over.

### Cluster

- SLURM jobs set `BTH_WORKSPACE_ROOT` (existing mechanism) to the cluster checkout, so they log
  to its `.bth/log/`. `bth sync --pull` gains a log pull (new work: `sync.py:115-131` calls
  `rsync` directly today) that copies three remote directories into the local main checkout's
  `.bth/log/remote/<remote>/`: the checkout's `.bth/log/` to `log/`, the remote
  `~/.bth/log/fallback/<slug>/` to `fallback/`, and the remote `~/.bth/log-mirror/<project_id>/`
  to `mirror/`. Each is a root kind for enumeration and watermarks (`remote-log`,
  `remote-fallback`, `remote-mirror`); eid dedup makes the overlapping copies safe. So D6's
  fallback and D7's mirror protect cluster runs too. Branches on either side are irrelevant,
  since the log is ignored.
  The old catalog rsync in `sync.py:54-119` narrows at cut-over to pulling legacy fragments
  only, so late cluster writes by an older bathos still arrive for `bth verify` to report.
  Removing it is future work.
- Refs created on the cluster clone stay there, as today. Moving provenance refs between
  clones is out of scope (D4).

### Discovery

- `~/.bth/projects.toml` lists main roots; the first event written to a project log registers
  it (idempotent). `bth projects prune` removes vanished roots.
- **Prerequisite:** today the test suite writes pytest temp dirs into the real
  `~/.bth/projects.toml`. It must be isolated first (AC-11).

### Migration (quiesced cut-over, no dual-write)

There is no dual-write phase: the new path is built and tested against fixture catalogs, then
switched on in one step.

0. **Ids:** `bth migrate --to-log` refuses to start until every registered root's `.bth.toml`
   has a `[project] id` present in its committed `HEAD`, or, for a root with no git
   repository, present in the file (it lists the roots missing one and the
   `bth init --assign-id` command).
1. **Quiesce:** it first pulls the legacy catalog from every configured remote (the existing
   `sync.py` rsync), so finished cluster runs whose fragments were never pulled are imported; it
   then runs `bth reap` (reconciling stale `running` rows through `sacct`),
   then refuses while any job in the user's `squeue` has a job id found in submit records or
   in a `running` run's `slurm_job_id`. Other queued jobs are listed; proceeding past them
   needs `--force`. It then takes the writers lock exclusively (waiting for, and reporting, any
   running local `bth` writer) and holds it until step 4 completes.
2. **Import:** convert every cool fragment, submit record, campaign JSON, and every row of every
   table in "Authoritative writes" into `*.imported` events (Fold rules). Before cut-over the
   events go to a staging directory, `~/.bth/log/import-staging/<attempt>/`, never to project
   logs; an abort at step 3 deletes it (including its index, below). Standalone `bth migrate --import-legacy` refuses to run
   before cut-over. After cut-over it writes directly to the owning project's `.bth/log/`
   (unresolvable ones to `unaffiliated/`).
3. **Build and diff:** build `import-staging/<attempt>/index.db` from the staged events alone
   (a root of kind `staging`, used only here) and diff it against `bathos.db` table by table.
   `~/.bth/catalog/index.db` is not touched before step 4. On a clean diff it writes the
   `diff_passed` record into the attempt directory. Every difference must fall in an allow-listed class (data already lost to earlier
   force-rebuilds; corrupt fragments skipped by `compact.py:787`; `output_metadata` whose files
   changed since; postmortem overrides whose files are only in a deleted worktree; campaign
   members whose legacy e-value used the fragment outcome rather than the postmortem-overridden
   one; unresolvable project) and goes into a
   signed-off residual report. Unclassified differences abort.
4. **Switch**, in this order, each action idempotent: (a) copy the staged events into their
   owning project logs and mirrors as fixed-name segments `import-<attempt>-<n>.jsonl`, each
   written to a temp file and renamed into place, and skipped if it already exists (so a repeat
   writes nothing); (b) build
   `~/.bth/catalog/index.db` from those logs; (c) write `cutover.json`, after which reads and
   writes use the new path; (d) rename `bathos.db` to `bathos.db.frozen`; (e) delete the
   staging directory. `bth migrate --to-log` is resumable. Every re-run first repeats step 1
   (squeue check and the exclusive writers lock). Then, with the marker absent: if the attempt
   directory holds a `diff_passed` record (written by step 3 with the sha256 set of the legacy
   sources it imported) and those sources are unchanged, it continues from (a); otherwise it
   deletes the staging directory and restarts from step 2. With the marker present: from (d) if
   `bathos.db` exists without `bathos.db.frozen`, then (e) if staging remains; step 3's diff is
   never re-run once the marker exists. `import_manifest` is a derived table of the index
   with one row per snapshot chain `(kind, entity, source_class, source_locator)` holding the
   newest snapshot's `source_sha256`, so it is rebuilt by a delete + re-ingest like every other
   table and updated by every later re-import. `bathos.db.frozen` is kept read-only for one
   release.
5. **Stale installs:** an older bathos (e.g. pinned in a project venv, or on the cluster) may
   keep writing cool fragments, submit Parquet, or a fresh `bathos.db`. `bth verify` reports
   each such write: a legacy source record whose chain is missing from `import_manifest` or
   whose hash differs from the chain's newest `source_sha256` (the same test the importer uses,
   so verify and importer never disagree; it catches late cluster pulls whatever their mtime,
   and rows of a `bathos.db` recreated beside `bathos.db.frozen`, which clear once imported) with the writing host; re-running
   `bth migrate --import-legacy` imports it (sources: `bathos.db.frozen`, any new `bathos.db`,
   fragments, submit Parquet, campaign JSON).

## Acceptance criteria

- AC-1. For any fixed multiset of events, delete + re-ingest yields an identical index,
  regardless of ingest order, cwd, or which files exist on disk (property test). The
  clock-skew-sensitive results listed in "Fold rules" are excluded and tested separately with
  fixed `ts`; the `quarantine` table is excluded.
- AC-2. A test enumerates every SQL write to a catalog database and every file write under
  `~/.bth/catalog` in `src/bathos/`, and fails unless it is the ingest path or the log writer;
  every entry in "Authoritative writes" has an event with a schema.
- AC-3. A finished run is visible to `bth show/ls/sql` and the MCP equivalents immediately, with
  no compact, including when it sits in a rotated segment.
- AC-4. Two concurrent `bth run` processes, one ingest, and one reader never raise a lock error;
  both runs are recorded.
- AC-5. Deleting a linked worktree after a run changes nothing in the index or the logs.
- AC-6. A SLURM array (including a requeued task and a task running two `bth run`s) records
  every run exactly once after two pulls, including when the first pull copies a segment
  mid-line (no quarantine entry; the line ingests once after the second pull).
- AC-7. A torn line in the middle of a file (followed by good lines) is quarantined with its
  offset and reported by `bth verify`; all good lines are ingested. An unterminated tail is never
  quarantined by ingest, and `bth verify` reports it once the writer has a later segment.
- AC-8. With the project log unwritable: `run.started` goes to the fallback and the run proceeds;
  with both unwritable the script is not launched; a `run.finished` in the fallback exits with
  the script's status plus a warning. Cluster variant: a SLURM `run.started` that lands in the
  remote fallback is in the local index after one `bth sync --pull` and ingest.
- AC-9. Deleting an active segment mid-run loses no event: every line is ingested from the mirror,
  locally and (via the pulled remote mirror) for a cluster run.
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
- AC-16. Root resolution tests: main checkout, linked worktree, bare main, submodule, relative
  `--git-common-dir`, `BTH_WORKSPACE_ROOT` set, no git repo.
- AC-17. Differential fold test: for randomized operation sequences on campaigns, anchors and
  runs, the old upsert/update code and the new fold produce identical rows.
- AC-18. No module other than `bathos.index` and the ingest path calls `duckdb.connect` on a
  catalog path (AST test). The importer and `bth verify` open legacy databases (`bathos.db`,
  `bathos.db.frozen`) only through `bathos.index.connect_legacy(path)`, which opens read-only
  and, if the file is locked by another process (e.g. an older install), returns a structured
  `legacy_db_locked` result instead of raising; `bth migrate --to-log` aborts on it, a
  post-cut-over `--import-legacy` skips that source and reports it. Before cut-over the legacy
  write sites are on the AC-18 test's allow-list (delivery step 3).
- AC-19. Ingest refuses the swap when a `.wal` remains after close.
- AC-20. Arrival-order independence: delivering the same events in any order and in any
  batching (including a late event with an earlier `ts`) yields the same index and the same
  read results as one sorted full ingest.
- AC-21. Importing the same legacy catalog twice appends nothing and quarantines nothing the
  second time; importing a rewritten fragment for an already-imported run whose warm row says
  `running` advances its status exactly once (to terminal) and leaves every warm-only field
  (`metadata`, `output_metadata`, postmortem fields) unchanged.
- AC-22. Campaign fold: for sequential campaigns, the fold's `seq_position`, `evalue`,
  threshold lock and threshold-mismatch skip equal what `campaigns.py:380-521` computes for the
  same runs, outcomes and sidecars; members without a declaration keep their stored `evalue`;
  the result does not depend on the order in which member-run events arrive.
- AC-23. After cut-over, a write by an older bathos (fragment, submit Parquet, or new
  `bathos.db`), including a fragment pulled late with an mtime older than the migration, is
  reported by `bth verify`, and re-running `bth migrate --import-legacy` imports
  it; a third run changes nothing.
- AC-24. On fixture catalogs, `bth verify` emits one structured finding for each of: threshold
  mismatch, `evalue_changed_after_conclusion`, two roots sharing a `project_id`, a log file
  shrunk below or vanished past its watermark, an `eid_conflict`, a quarantined line, a line
  present in neither the project log nor the mirror, a legacy write after cut-over, a
  `corrupt_legacy_source`, and a `legacy_db_locked`; and none on a clean fixture.
- AC-25. Every write site in "Authoritative writes" has a test that, with the flag on, emits its
  event (and nothing else) and, with the flag off, performs only the legacy write.
- AC-26. Run status: a `run.reaped` with a later `ts` than a `run.finished` still folds to the
  terminal status; a reap then revert with no finish folds to `running`; a run reaped before
  cut-over (imported `abandoned`) and reverted after it folds to `running`; a run reaped and
  reverted before cut-over folds to its restored status even if the warm row still says
  `abandoned`; the reaper after cut-over selects exactly the folded `running`
  runs past its window.
- AC-27. With two roots sharing a `project_id`, `bth log restore` in one never copies the
  other's events; after moving a project, restore still recovers events written at the old path.
- AC-28. Re-importing a legacy source that changed yields the fold of its newest snapshot only,
  including a source that changed and then changed back; a run reaped, reverted, reaped and
  reverted again imports every ledger and re-importing it appends nothing; an aborted migration leaves no imported
  event in any project log and no `~/.bth/catalog/index.db`; abort, retry, switch leaves every
  imported event in the logs.
- AC-29. `bth migrate --to-log` killed during step 2, during step 3, and after each of step 4's
  actions (a)-(e), then re-run, completes the switch with the same index and logs as an
  uninterrupted run (a kill before `diff_passed` restarts from step 2; a legacy write made while
  it was down is included); a `bth run`
  started before the switch is either wholly legacy or wholly events; a `bth submit` after
  cut-over produces a job that appends events, and `BTH_LOG_MODE=1` outside a SLURM job or test
  is refused.
## Order of delivery

1. AC-11 (test isolation).
2. Writer, resolution, D3 check, `project_id` via `bth init`/`--assign-id`, root registration
   in `projects.toml` (Discovery), mirror and `bth log restore` (AC-10, AC-16, AC-27, plus the log-level halves of AC-5, AC-9 and AC-14:
   the right lines exist in the project log and the mirror), behind the flag ("Mode"); production
   still uses the old tiers. Every write site in "Authoritative writes" gains its event emission behind the same
   flag (AC-25).
3. `index.db`, `events` table, fold (incl. the campaign fold), generation-swap ingest, read API
   (flag off: the pass-through in "Reads"); move all 25 modules to it, exercised against
   fixture catalogs (AC-1, AC-5, AC-7's ingest half, AC-9, AC-12, AC-15, AC-17, AC-18, AC-19,
   AC-20, AC-22). AC-18 is enforced
   from here; the legacy write sites (flag-off branch only) sit on an explicit allow-list in the
   AC-18 test, which step 5 empties.
4. Legacy importer, reaper on folded status, `bth verify` checks, and `bth migrate --to-log`
   (Migration steps 0-4: staging, diff, abort, switch) built and exercised against fixture
   catalogs (AC-7's verify half, AC-13, AC-14, AC-21, AC-23, AC-24, AC-26, AC-28, AC-29), and the new
   cluster log pull in
   `bth sync --pull` (log, fallback, mirror; AC-6 and the cluster variants of AC-8, AC-9).
5. Assign ids in every registered project (migration step 0), then run the step-4
   `bth migrate --to-log` on the real catalog (its AC-13 residual report is signed off); then
   AC-2, AC-3, AC-4 and AC-8 hold in production.

## Risks

| Risk | Mitigation |
|---|---|
| A write site missed | AC-2 fails the build |
| `git clean -fdX` / project dir loss | Mirror (D7) + `bth log restore` (AC-14) |
| Main checkout itself deleted | Mirror holds every line |
| Clock skew | Deterministic tie-break; skew-sensitive fields listed (AC-1) |
| Copy-then-swap cost as the index grows | Measured under AC-12; revisit if ingest exceeds the budget set there |

## Future work

Cold archive of old segments, opt-in git tracking of logs, DuckLake as the index backend, Quack.
