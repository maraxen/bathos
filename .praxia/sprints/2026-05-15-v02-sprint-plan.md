# bathos v0.2 Sprint Plan

**Date:** 2026-05-15  
**Backlog items:** #130, #131, #138, #139, #140  
**Prerequisite:** v0.1 complete and merged to main ✅

---

## Architecture constraints (from advisor review)

These are hard rules that every task must respect:

- `catalog.py` stays cool-tier only — no compaction or DuckDB logic bleeds in; it must never import `compact`
- `compact.py` owns all cool→warm logic: DuckDB connection, migrations, schema version dispatch
- `query.py` gets a `Backend` abstraction (cool vs. warm dispatch) *before* `compact.py` lands — prevents parallel filter implementations
- Cool tier uses `COOL_SCHEMA` (no `metadata` column); warm tier uses `WARM_SCHEMA` (adds `metadata` JSON, `outcome`, warm-only enrichment fields)
- `metadata` is **omitted** from cool Parquet entirely — not stored as `"{}"`; `Run.metadata` is Python-only until compact time
- Auto-compact-on-query is **not** in v0.2 — explicit `bth compact` command only; `bth ls` shows a banner warning when fragment count > 50
- `slurm_job_id` is a **cool-tier field** (needed for triage of running jobs before compact)
- Two independent migration axes: cool fragment `schema_version` vs. warm DuckDB schema version — tracked separately, never conflated
- `from_arrow_row` stays strict (fails on missing fields) — migration lives in `compact.py` only
- `run_sql` (query.py) must point at warm DuckDB and error clearly when warm doesn't exist — currently a silent no-op

---

## Sprint v0.2a — Data pipeline

**Goal:** Get the warm tier right. After this sprint: `bth compact` ingests cool fragments into DuckDB, `bth sql "SELECT * FROM runs"` queries the full catalog, the schema is versioned and split correctly.

### Task A1: Schema split — COOL_SCHEMA / WARM_SCHEMA + schema_version + slurm_job_id

**Files:**
- Modify: `src/bathos/schema.py`
- Modify: `src/bathos/catalog.py` (write uses COOL_SCHEMA, read handles missing fields gracefully for backward compat)
- Modify: `tests/test_schema.py`, `tests/test_catalog.py`

**Changes:**
- Rename `RUN_SCHEMA` → `COOL_SCHEMA`; export `WARM_SCHEMA` (adds `metadata pa.string()`, `outcome pa.string()`)
- Add `pa.field("schema_version", pa.string())` to `COOL_SCHEMA` (default `"1"`)
- Add `pa.field("slurm_job_id", pa.string())` to `COOL_SCHEMA` (default `""`, populated from `SLURM_JOB_ID` env var at run time)
- Add `metadata: str = "{}"` to `Run` dataclass (Python string, not dict — serialized JSON; not written to cool Parquet Arrow, only present in warm)
- Add `slurm_job_id: str = ""` to `Run` dataclass
- `Run.to_arrow()` uses `COOL_SCHEMA` only — does not include `metadata`
- `Run.from_arrow_row()` strict: raises on missing required fields; `schema_version` and `slurm_job_id` default-filled for v0 fragments
- Update `runner.py`: populate `slurm_job_id` from `os.environ.get("SLURM_JOB_ID", "")`
- Update all tests for new schema

**Tests:** 
- `test_schema_version_in_cool_parquet` — written fragment contains `schema_version="1"`
- `test_slurm_job_id_captured_from_env` — env var → Run → Parquet round-trip
- `test_metadata_not_in_cool_parquet` — written fragment has no `metadata` column
- `test_warm_schema_has_metadata_column` — WARM_SCHEMA contains `metadata`

---

### Task A2: Backend abstraction in query.py

**Files:**
- Modify: `src/bathos/query.py`
- Modify: `tests/test_query.py`

**Changes:**
- Add `_resolve_backend(catalog_dir: Path) -> Literal["cool", "warm"]` — returns `"warm"` if `catalog_dir/bathos.db` exists, else `"cool"`
- Refactor `list_runs`, `find_runs`, `get_run` to dispatch to either:
  - `_cool_list_runs(...)` — current PyArrow concat + Python filter (preserved unchanged)
  - `_warm_list_runs(...)` — DuckDB query against `bathos.db`
- Fix `run_sql`: if warm DB exists, open `catalog_dir/bathos.db`; if not, raise `RuntimeError("No warm catalog. Run `bth compact` first.")`
- No behavior change from user perspective when warm DB doesn't exist — cool path is identical to v0.1

**Tests:**
- `test_list_runs_uses_cool_when_no_warm_db` — no bathos.db → cool path used
- `test_run_sql_errors_clearly_without_warm_db` — clean error message
- `test_backend_resolution` — bathos.db present → warm; absent → cool

---

### Task A3: compact.py — cool→warm ingestion

**Files:**
- Create: `src/bathos/compact.py`
- Modify: `tests/test_catalog.py` (add compact fixtures)
- Create: `tests/test_compact.py`

**Changes:**
- `compact(catalog_dir: Path) -> CompactResult` — ingests all cool fragments into `bathos.db`
  - Snapshots file list at start (ignores fragments written after snapshot)
  - Upserts into DuckDB `runs` table (keyed on `id`)
  - Tracks warm-tier schema version in `_schema_meta` table (`{"warm_version": "1"}`)
  - Returns `CompactResult(ingested=N, skipped=N, duration_s=float)`
- Migration registry: `MIGRATIONS: dict[str, Callable[[dict], dict]]` — keyed by cool `schema_version`; v0 fragments (no `schema_version` field) treated as `"0"`, upgraded in-place during ingest
- Does NOT remove cool fragments after ingest (safe default; future `--prune` flag)
- `_fragment_count(catalog_dir) -> int` — used by `bth ls` banner warning

**Tests:**
- `test_compact_ingests_all_fragments` — N fragments → 1 DuckDB file, N rows
- `test_compact_is_idempotent` — run twice, still N rows (upsert by id)
- `test_compact_snapshots_file_list` — fragment written during compact not included
- `test_compact_upgrades_v0_fragments` — fragment missing schema_version → migrated to v1
- `test_compact_tracks_warm_schema_version` — `_schema_meta` table written
- `test_fragment_count_helper` — correct count returned

---

### Task A4: bth compact CLI command + bth ls banner

**Files:**
- Modify: `src/bathos/cli.py`
- Modify: `tests/test_cli.py`

**Changes:**
- Add `bth compact` command: calls `compact.compact(catalog_dir)`, prints summary (`Compacted N runs into bathos.db in X.Xs`)
- Modify `bth ls`: call `_fragment_count(catalog_dir)` after listing; if > 50 and no warm DB, print `"\n⚠  {n} uncompacted runs — run 'bth compact' to speed up queries"`
- Modify `bth sql`: surfaces the new `run_sql` error clearly

**Tests:**
- `test_compact_command_runs` — CLI invocation, exit 0, summary in output
- `test_ls_shows_compact_banner_at_threshold` — 51 fragments → banner shown
- `test_ls_no_banner_below_threshold` — 10 fragments → no banner
- `test_sql_error_without_warm_db` — clear error message

---

### Task A5: Integration test update

**Files:**
- Modify: `tests/test_integration.py`

**Changes:**
- Extend `test_full_workflow` to include compact step: after two runs, call `bth compact`, then verify `bth sql "SELECT count(*) FROM runs"` returns 2
- Verify `bth ls` banner appears before compact, disappears after (or if < threshold)

---

### v0.2a Exit criteria

- [ ] All 44 existing tests still pass
- [ ] `bth compact` ingests fragments into `bathos.db`
- [ ] `bth sql "SELECT count(*) FROM runs"` returns correct count after compact
- [ ] `bth ls` shows banner when > 50 uncompacted fragments
- [ ] `slurm_job_id` captured in cool Parquet when `SLURM_JOB_ID` env var set
- [ ] No `metadata` column in cool-tier Parquet files
- [ ] `schema_version="1"` in all new cool fragments

---

## Sprint v0.2b — Cluster workflow

**Goal:** Sync catalogs between laptop and cluster. Check run validity against git drift. SLURM integration hardened.  
**Prerequisite:** v0.2a merged — warm tier exists, `Backend` abstraction in place.

### Task B1: bth sync — rsync cool-tier catalog to/from cluster

**Files:**
- Create: `src/bathos/sync.py`
- Modify: `src/bathos/cli.py`
- Create: `tests/test_sync.py`

**Changes:**
- `sync_catalog(remote_name: str, config: ProjectConfig, catalog_dir: Path, pull: bool = False) -> SyncResult`
  - Reads `config.remotes[remote_name]` for `host` and `remote_root`
  - Constructs rsync command: `rsync -azP --ignore-existing <src>/runs/ <dst>/runs/`
  - `--ignore-existing` ensures POSIX atomic rename semantics preserved (no partial overwrites)
  - Pull: `rsync -azP --ignore-existing <host>:<remote_root>/.bth/catalog/runs/ <local>/runs/`
  - Push: opposite direction
  - Returns `SyncResult(transferred=N, duration_s=float, remote=str)`
- `bth sync <remote>` — push by default; `bth sync --pull <remote>` to pull
- Errors clearly if remote not in `.bth.toml`

**Tests:**
- `test_sync_constructs_correct_rsync_command` — mock subprocess, verify args
- `test_sync_pull_reverses_direction` — src/dst swapped
- `test_sync_errors_on_unknown_remote` — clean error
- `test_sync_uses_ignore_existing` — flag present in command

---

### Task B2: bth check — git-drift validity

**Files:**
- Create: `src/bathos/checker.py`
- Modify: `src/bathos/cli.py`
- Create: `tests/test_checker.py`

**Changes:**
- `check_runs(catalog_dir: Path, project_root: Path) -> list[CheckResult]`
  - Gets current `git_hash` via `capture_git_state(project_root)`
  - For each run in catalog (uses Backend — warm if available, cool otherwise):
    - `STALE` if run's `git_hash` != current HEAD and run's `git_dirty` was False
    - `DIRTY_RUN` if run's `git_dirty` was True (results may not be reproducible)
    - `UNKNOWN_CODE` if run's `git_hash` == "unknown"
    - `OK` otherwise
  - Returns list of `CheckResult(run_id, status, run_git_hash, current_hash)`
- `bth check` CLI — tabular output; `--status stale` filter; exit code 1 if any STALE runs

**Tests:**
- `test_check_flags_stale_run` — run hash ≠ HEAD → STALE
- `test_check_ok_for_current_run` — run hash == HEAD → OK
- `test_check_flags_dirty_run` — `git_dirty=True` → DIRTY_RUN
- `test_check_unknown_outside_git` — hash "unknown" → UNKNOWN_CODE
- `test_check_uses_warm_backend_when_available`

---

### Task B3: SLURM integration hardening

**Files:**
- Modify: `src/bathos/init.py` (update `_bth_env.sh` template)
- Modify: `src/bathos/templates/_bth_env.sh`
- Create: `tests/test_slurm_integration.py`

**Changes:**
- Update `_bth_env.sh` template to also export `BTH_CATALOG_DIR` derived from `root`:
  ```bash
  export BTH_PROJECT_SLUG="{slug}"
  export BTH_PROJECT_ROOT="{root}"
  export BTH_CATALOG_DIR="${{HOME}}/.bth/catalog"
  ```
- Document array job usage: `SLURM_JOB_ID` is auto-captured by `bth run` — no extra setup needed
- `bth init` gains `--catalog-dir` option to override the default in `_bth_env.sh`
- Add `bth check --slurm-job <job_id>` filter to narrow check to a specific array job's runs

**Tests:**
- `test_bth_env_sh_exports_catalog_dir` — generated file exports `BTH_CATALOG_DIR`
- `test_init_catalog_dir_override` — `--catalog-dir` flag sets correct path in template
- `test_check_filter_by_slurm_job` — `--slurm-job 12345` returns only matching runs

---

### Task B4: Integration test — full cluster workflow

**Files:**
- Create: `tests/test_cluster_workflow.py`

**Changes:**
- End-to-end test simulating cluster round-trip:
  1. `bth init` with remote configured
  2. Two `bth run` calls (simulated, no actual SLURM)
  3. `bth compact`
  4. `bth check` → OK (runs match HEAD)
  5. Mutate a file, `bth check` → STALE for those runs
  6. `bth sync` command constructed correctly (mock rsync)

---

### v0.2b Exit criteria

- [ ] `bth sync engaging` pushes cool fragments to remote via rsync
- [ ] `bth sync --pull engaging` pulls remote fragments locally
- [ ] `bth check` correctly identifies STALE, DIRTY_RUN, OK, UNKNOWN_CODE runs
- [ ] `_bth_env.sh` exports `BTH_CATALOG_DIR`
- [ ] `SLURM_JOB_ID` captured in cool fragments during array jobs
- [ ] Full suite passes

---

## Dependency order

```
A1 (schema split)
    └─▶ A2 (Backend abstraction)
            └─▶ A3 (compact.py)
                    └─▶ A4 (CLI compact + banner)
                            └─▶ A5 (integration update)
                                    └─▶ [merge v0.2a]
                                            ├─▶ B1 (bth sync)
                                            ├─▶ B2 (bth check)  ← parallel
                                            └─▶ B3 (SLURM)      ← parallel
                                                    └─▶ B4 (cluster integration test)
```

B1, B2, B3 can run in parallel after v0.2a merges — they touch separate files.

---

## Key decisions locked

| Decision | Choice |
|---|---|
| Auto-compact trigger | Explicit only in v0.2; banner warning in `bth ls` at >50 fragments |
| Cool schema | `COOL_SCHEMA`: 13 original fields + `schema_version` + `slurm_job_id` |
| Warm schema | `WARM_SCHEMA`: COOL_SCHEMA fields + `metadata` (JSON string) + `outcome` |
| `metadata` in cool Parquet | Omitted entirely — not stored as `"{}"` |
| Migration | v0 fragments (no `schema_version`) treated as `"0"` at compact time |
| Warm-tier version tracking | `_schema_meta` table in `bathos.db`, separate from cool `schema_version` |
| Results storage | Deferred to #142 — reference + hash model, `outputs/<run-id>/` convention |
| `bth archive` | Deferred to #141 (P3) |
