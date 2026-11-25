---
title: 'Cross-host run sync, verifiable run bundles, and remote output binding'
description: 'Closes the cross-host runlog gap exposed by remote run e88aa486 on titanix: defines canonical verifiable run bundles (bathos.bundle.v1), durable import receipts, selective pull (bth sync --pull --run), offline export/import (bth bundle export/import, bth import), local worktree/sidecar resolution, equivocation detection, remote reap protection, launch-time remote execution (bth run --on), and runtime output binding.'
status: draft
task_id: 261125_cross-host-run-sync-and-import
date: '261125'
---
# Cross-host run sync, verifiable run bundles, and remote output binding

**Status:** draft (revised after Round 1 adversarial reviews `#272` and `#273`; awaiting Round 2 sign-off and owner decisions in §10).  
**Branch:** `feat/cross-host-run-sync`.  
**Originating handoff:** `tev_design/.claude/worktrees/261116-wave1-order1-design-dag/.praxia/docs/handoffs/261125_bathos-remote-run-catalog-gap.md` (`bth` run `e88aa486-3a4c-4977-826b-2f5d511ca29d` executed on `titanix`).

---

## 1. Executive Diagnosis

The `runlog` cutover (`260925_project-local-run-log.md`, `261012_retire-compact-runlog-cutover.md`) replaced cool-tier Parquet fragments with append-only JSONL event logs (`src/bathos/runlog/envelope.py`) and generation-swap folding into `index.db` (`src/bathos/runlog/ingest.py`). Although the envelope schema (`eid`, `writer`, `ts`, `project_id`, `main_root`, `worktree_root`) and `discover_roots()` remote subtrees (`remote-log`, `remote-fallback`, `remote-mirror`) were designed to be host-agnostic, **seven concrete gaps** prevent a run executed on a remote host (such as `e88aa486` launched via SSH inside a git-bundle clone on `titanix`) from being visible, reviewable, or output-verifiable on the local host:

1. **Ad-hoc remote runs stay stranded on the executing host.** Launches outside `bth submit` (e.g., `ssh titanix` + git-bundle clone + `bth run`) write events only to `<bundle_clone>/.bth/log/` and remote `~/.bth/log-mirror/<project_id>/` (or `_null/<slug>/`), or to `~/.bth/catalog/runs/` if the remote has not cut over to log mode. No push-back or registration path brings those records to the local catalog.
2. **`pull_cluster_log` (`src/bathos/sync.py:441-520`) misses git-bundle clones and external checkouts.** `pull_cluster_log` hardcodes `{remote_root}/.bth/log/`, `~/.bth/log/fallback/{slug}/`, and `~/.bth/log-mirror/{project_id or _null/slug}/`. Runs executed inside an ad-hoc git-bundle clone outside `remote_root` never appear in `{remote_root}/.bth/log/`, and `myxcel.rsync.validate_pull_path` rejects arbitrary bundle-clone paths outside `~/.bth/`, `profile.workspace`, and configured `scratch_root(s)`. (Remote `~/.bth/log-mirror/<project_id>/` *is* allowlisted by `myxcel pull-path`, but only captures log-mode runs whose `project_id` resolved and is only pulled when `bth sync --pull` is invoked.)
3. **`bth sync --pull` never runs `run_ingest()`, and `connect_read()` does not re-fold unindexed deltas.** `sync_tool()` (`src/bathos/mcp.py:2761-2840`) calls `pull_cluster_log()` when `is_log_mode(cat_dir)` is true, but never calls `run_ingest(cat_dir)`. Meanwhile `connect_read()` (`src/bathos/index.py:717`) attaches `index.db` directly without re-folding unindexed log segments (G3 delta views are not implemented). As a result, pulled segments remain invisible to `bth show`/`sql`/`cite` and `mrx review build` until `bth compact` or another write happens to trigger `run_ingest()`.
4. **`bth submit --wait --then-sync` bypasses `pull_cluster_log`, fails in linked worktrees, and skips `run_ingest`.** `submit()` (`src/bathos/cli_cyclopts.py:1345-1350`) calls `sync_catalog(cluster.remote, config, cat_dir, pull=True)` directly, skipping both `pull_cluster_log()` and `run_ingest()`. Moreover, `submit()` sets its project path from `cfg_path.parent` (`:1223, 1274`) rather than `resolve_log_root(Path.cwd()).main_root`, so pulling into `cfg_path.parent` from a linked worktree would land logs where `discover_roots()` (`ingest.py:351-371`) never scans.
5. **`bth sync --pull` lacks selective filtering, and no single-run export/import exists.** `sync_tool()` exposes only `--remote-name`, `--pull/--no-pull`, and `--catalog-dir`, forcing an all-or-nothing pull of every run and project in the remote log/mirror. Meanwhile `bth export` (`src/bathos/cli_cyclopts.py:1806`, backed by `src/bathos/export.py`) exports only agent skills, MCP config, plugins, and HTML reports; there is no `bth import` or verifiable run-bundle command.
6. **`bth reap` prematurely reaps remote non-SLURM runs.** `_is_live_process_on_host()` (`src/bathos/reap.py:267-279`) returns `False` immediately whenever `hostname != socket.gethostname()`. For non-SLURM runs (`slurm_job_id == ""`), `_classify_reap_candidates()` (`src/bathos/reap.py:481-489`) under default `bth reap` (`slurm_only=False`) classifies any remote `running` run older than `older_than_h` (min 24h) as `orphan_window_exceeded`, marking live remote runs `abandoned` if their `run.finished` event has not yet synced locally. (Note: stale local run `e20c0a4b` has `hostname == local_host` and is legitimately reapable.)
7. **Remote-executed runs fail local `bth outputs`, `bth check`, `bth claim`, and `maraxiom` review/audit checks.**
   - `output_tiers/run_context.py:_resolve_root()` (`src/bathos/output_tiers/run_context.py:75-101`) allows `--worktree-root` overrides **only** when `kind == "run.imported"`. For any live `run.started` run brought from another host, passing `--worktree-root` raises `INVALID_PARAM` (`:95-100`), while omitting it fails with `WORKTREE_MISSING` (`:158-159`) because the remote `worktree_root` does not exist locally.
   - `output_tiers/adopt.py:_root_relpath()` (`src/bathos/output_tiers/adopt.py:212-226`) rejects paths outside the recorded `worktree_root` with `ROOT_OUTSIDE_WORKTREE`.
   - `checker.py:199-289` (`check_output_sha_drift`) and `claim.py:1620` (`output_metadata_has_sha_drift`) read `entry["path"]` instead of resolving `entry["relpath"]` against the local worktree, marking imported runs `MISSING` and downgrading `"controlled"` parity to `"uncontrolled"`.
   - `maraxiom` (`bathos_bridge.py:1499-1514`, `review_build.py:316-338, 757-769`) requires `sidecar_path` to satisfy `p.is_absolute() and p.is_file()` on the local host and checks `panel.table in record["output_paths"]` under `repo` (see §4.3 for exact `bathos` vs companion `maraxiom` scope).

---

## 2. Architecture Overview (Unified A + B + D + E)

```
  Remote host (titanix / engaging / Colab / airgapped)
     │
     ├─ Option B (offline): bth bundle export <run_id> -o run.bthbundle
     ├─ Option A (online):  bth sync --pull --run <id> [--since ...] [--project ...]
     └─ Option D (launch):  bth run --on <remote>  ──(push-back on exit)──┐
                                                                           ▼
                                                        Canonical `bathos.bundle.v1`
                                                                           │
                                          ┌────────────────────────────────┴──────────────────────┐
                                          ▼                                                       ▼
                         Verify canonical JSON SHA-256s,                        Fail closed on tampering,
                         archive safety, & equivocation                         zip-slip/symlinks, or
                                          │                                     conflicting genesis/terminal
                                          ▼
                         Write immutable `.jsonl` + `.receipt.json` under
                         `.bth/log/imported/<host>/` + local SHA-256 resolution
                         + `run_ingest(catalog_dir)`
                                          │
                                          ▼
                   Local `bathos` readers (`bth show/cite/outputs/check/claim`)
                   work offline with original `run_id` & `ts` (+ companion `mrx` hook)
```

### Design Principle: One Canonical Wire Unit (`bathos.bundle.v1`)

One canonical self-verifying bundle format (`bathos.bundle.v1`, 1 run per bundle) serves as the universal transport unit:

- **Option B (`bth bundle export` / `bth bundle import` / `bth import`)** writes and ingests `bathos.bundle.v1` archives directly.
- **Option A (`bth sync --pull --run ...`)** invokes remote `bth bundle export` into a remote `~/.bth/tmp/` staging file and pulls it via `bathos.cluster.pull_path()` (with a clearly-labeled unverified mirror fallback when remote `bth bundle` is absent), feeding it through the shared importer.
- **Option D (`bth run --on <remote>`)** registers a non-conflicting `run.remote_dispatched` launch stub locally, runs remotely with `BTH_RUN_ID=<run_id>` and Option E runtime output binding, and pulls back the resulting `bathos.bundle.v1` on completion (with Option A/B recovering seamlessly if SSH drops).
- **Option E (Runtime output binding & remote adopt)** ensures remote runs bind `[outputs]` at run end (or adopt external roots cleanly via relpath mapping and `external_root` resolution) before bundling.

### Why Option C (Remote-Aware Readers) Is Rejected as Primary

Option C (teaching `bth show/cite`, `mrx review build/audit`, `bth claim`, and `bth campaign` to query a remote host over SSH on demand) is **rejected as the primary architecture**:
1. **Violates offline auditability:** Review artifacts and claim gates must remain verifiable when the remote workstation or Duo-gated cluster is offline.
2. **Fragments reader invariants:** `maraxiom`, `bathos.claim`, `bathos.cite`, `bathos.validity`, and `bathos.output_tiers` all assume a unified local `index.db`.
3. **Weakens integrity guarantees:** Remote catalogs can mutate or be reaped independently; local verified import freezes a digest-checked `.receipt.json`.
4. **Optional read-only convenience only:** A lightweight `bth show --remote <name> <run_id>` may optionally be added later for pre-pull inspection, never as a dependency for review, audit, or claim gates.

---

## 3. Canonical Bundle Format (`bathos.bundle.v1`)

### 3.1 Container & Layout

A `bathos.bundle.v1` archive is a `.tar.zst` archive (default extension `.bthbundle` or `.tar.zst`; `.tar.gz` and uncompressed `.tar` accepted on import when `zstd` is unavailable) containing **only** the following top-level layout (unknown top-level members are rejected):

```
manifest.json                     # Canonical JSON manifest (format, digests, closure summary)
events.jsonl                      # Causal-closure event envelopes (one JSON line per event)
sidecar.bth.toml                  # Optional: exact sidecar bytes (required when sidecar_sha256 != "")
git.bundle                        # Optional: shallow git bundle covering git_hash (iff --include-git)
blobs/outputs/<relpath>           # Optional: regular output files (iff --include-outputs)
```

Using `blobs/outputs/<relpath>` (rather than `outputs/<relpath>`) guarantees zero path ambiguity when a run's `relpath` itself begins with `outputs/`.

### 3.2 Causal Closure for `run_id`

Exporting `run_id` collects every event forming the run's causal closure across all relevant entity families in `src/bathos/runlog/ingest.py` (`_RUN_KINDS` and sibling tables):

| # | Table / Event Kind(s) | Selection Rule | Notes |
|---|---|---|---|
| 1 | `runs` (`run.started`, `run.finished`, `run.reaped`, `run.reap_reverted`, `run.outputs_hashed`, `run.outputs_adopt_retracted`, `run.postmortem_applied`, `run.declaration_backfilled`, `run.imported`, `run_reap.imported`, `run.remote_dispatched`) | `entity_key == json.dumps([run_id])` | Full lifecycle of the run itself. |
| 2 | `sidecar_anchors` (`anchor.recorded`, `anchor.imported`) | `(path, sha256)` matching run's `(sidecar_path, sidecar_sha256)` | Preserves sidecar anchor provenance. |
| 3 | `output_copies` (`output_copy.*`) | `entity[0] == run_id` (`len(entity) == 2`) | Preserves cold/mirror copy records (`OUTPUT_COPY_KINDS`). |
| 4 | `submits` (`submit.recorded`, `submit.imported`) | `slurm_job_id == run.slurm_job_id` (when non-empty), else `(project_slug == run.project_slug AND sidecar_sha256 == run.sidecar_sha256 AND command == run.command)` | Matches actual `SUBMITS_DDL` (`src/bathos/index.py:326-338`, which has no `run_id` column). |
| 5 | `run_edges` (`edge.added`, `edge.imported`) | `entity` is `[child, parent, "run"]` where `child == run_id` or `parent == run_id` | Preserves run lineage without pulling unrelated runs. |
| 6 | `supersession_edges` (`supersession.declared`, `supersession.imported`) | `(entity_type == "run" AND entity_id == run_id)` OR `(superseded_by_type == "run" AND superseded_by_id == run_id)` | Preserves result supersession state. |
| 7 | `blast_radius_ledger` (`blast_radius.recorded`, `blast_radius.imported`) | `(entity_type == "run" AND entity_id == run_id)` OR `(entity_type == "sidecar" AND anchor_value == run.sidecar_sha256)` | Prevents laundering an `"affected"` run into `"clean"`. |
| 8 | `trust_ledger` (`trust_ledger.recorded`, `trust_ledger.imported`) | `data.run_id == run_id` | Preserves candidate/promoted trust state. |
| 9 | `archived_items` (`archived_item.recorded`, `archived_item.imported`) | `run_id` in `data.run_ids` | Preserves artifact archival state. |
| 10 | `observations` (`observation.*`) | `data.run_id == run_id` or `id == run.metadata["derived_from_observation"]` | Preserves linked exploratory observation provenance. |
| 11 | `acknowledgment_ledger` (`validity.acknowledged`, `validity.estimand_override_recorded`) | `run_id` in `data.run_ids` | Preserves gate acknowledgments. |

**Legacy (pre-log-mode) remote export support:** When `bth bundle export` executes on a host/catalog where `is_log_mode(cat_dir)` is `False` (legacy cool-tier Parquet / `bathos.db`), it reads the run via `get_run(..., allow_cool_fallback=True)` and deterministically synthesizes `run.imported` (plus `run.outputs_hashed` from `output_metadata` if present) with `origin="migration"` and deterministic `uuid5(NAMESPACE_BATHOS, ...)` `eid`s, recording `source_mode = "legacy"` in `manifest.json`. Thus `titanix` can export runs regardless of whether `bth migrate --to-log` has been run there.

**Sidecar bytes & `sidecar_sha256`:** If `run.sidecar_sha256` is non-empty, `bth bundle export` locates the raw TOML bytes via: (1) local `sidecar_path` (under `worktree_root`/`main_root`, or via SHA-256-matched relative suffix as in `claim.py:1685-1695`), (2) `refs/bathos/locks/<run_id>` (`lock_ref.locate_lock_bytes`), or (3) `<catalog_dir>/imported_sidecars/<run_id>/<sha256>.bth.toml` / `sidecars/snapshots/`. (Note: `sidecar_declaration_for_event()` in `emit.py:284` stores a parsed JSON dict, not raw TOML bytes, so it cannot substitute for `sidecar.bth.toml`.) If raw bytes matching `sidecar_sha256` cannot be found, export fails with `SidecarUnverifiableError` / `RunBundleInvalidError` unless `--allow-missing-sidecar` is explicitly passed (recording `sidecar.included = false` and `sidecar.missing_reason`). Additionally, to prevent future loss on ephemeral `/tmp` git-bundle clones, `runner.py` should archive a copy of the run-time sidecar bytes in `~/.bth/sidecar-cache/<sidecar_sha256>.bth.toml` alongside writing `log-mirror/`.

**Safe `campaign_id` / `_refold_campaign()` / `reconcile_campaign_runs()` handling:**  
In `src/bathos/runlog/ingest.py:974-980, 1391-1396, 1561-1637`, emitting `campaign.run_added` without `campaign.created` triggers `_refold_campaign()` (which deletes `campaign_runs` before `reconcile_campaign_runs()` runs) AND causes `maraxiom` (`bathos_bridge.py:1488-1495`, `review_audit.py:202-209`) to see `campaign.run_added` with `claim_bound_at = None` and emit `BANNER_CAMPAIGN_UNREADABLE`. Conversely, dragging every sibling run of a campaign violates selective single-run import. Therefore:
1. **Default single-run mode (`with_campaign=False`):**
   - `events.jsonl` MUST NOT include `campaign.run_added` if `campaign.created` is omitted.
   - `manifest.json` records `campaign_context`: `{campaign_id, campaign_name, campaign_mode, claim_path, claim_sha256, seq_position, evalue, external_campaign_ref}`.
   - On import, `.receipt.json` persists `external_campaign_ref = campaign_id`. In `ingest.py` (`:944-948, 1391, 1441`), `_refold_run()` preserves `row["campaign_id"] = external_campaign_ref` (and `seq_position`/`evalue` from the receipt if present) when `campaign_id` is not in `known_campaign_ids()`, without adding `external_campaign_ref` to `affected_campaign_ids` (so `_refold_campaign()` is not triggered) and without `reconcile_campaign_runs()` (`bad_run_cids` at `:1583`) clearing or refolding it.
2. **Full-campaign mode (`--with-campaign`):**
   - Includes `campaign.*` events (`campaign.created`, `campaign.claim_bound`, `campaign.threshold_set`, `campaign.concluded`, `campaign.run_added`) for `campaign_id` (and optionally sibling member run IDs on `bth sync --pull --with-campaign`) so the campaign folds natively.

**Git metadata:** `manifest.json` records `git`: `{git_hash, git_branch, git_dirty, git_dirty_content_id, git_provenance_source, dependency_lock_sha256}`. With `--include-git`, `git.bundle` packages `git_hash` (`git bundle create ... <git_hash>`). On import, if `git_hash` is absent from local git history (e.g. a bundle-clone commit), import succeeds and records `git_commit_local = false` in `.receipt.json`.

### 3.3 Canonical JSON Hashing & Verification

To close the digest blind spots in `ingest.py:1373` (which hashes/compares only `(kind, entity, data)`) and `_row_to_event()` (`ingest.py:589-617`, which omits `"v"` and drops extra top-level keys such as `"actor"`):

1. **Canonical serializer:** `canonical_json_bytes(obj)` = `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")`. `NaN` / `Infinity` values MUST be rejected (`RunBundleInvalidError`) rather than serialized non-standardly.
2. **Full-envelope canonicalization:** Every event's `event_digests[eid]` is `sha256(canonical_json_bytes(normalized_envelope))` over **all** envelope fields (`v`, `eid`, `kind`, `entity`, `ts`, `project`, `project_id`, `writer`, `seq`, `main_root`, `worktree_root`, `origin` defaulting to `"live"`, `data`, plus any extra top-level keys present such as `actor`).
3. **`events` table canonical digest:** Add nullable `envelope_sha256 VARCHAR` (and `extra_keys VARCHAR`) to `EVENTS_DDL` (`src/bathos/index.py:49-65`) via `_add_missing_columns()`, and update `_load_existing_events()` / `_row_to_event()` to include `"v"` and compare `envelope_sha256` (falling back to canonical 13-key comparison for legacy `index.db` rows lacking `envelope_sha256`).

`manifest.json` schema (`format = "bathos.bundle.v1"`):

```json
{
  "format": "bathos.bundle.v1",
  "bundle_version": 1,
  "source_mode": "log",
  "run_id": "e88aa486-3a4c-4977-826b-2f5d511ca29d",
  "project_slug": "tev_design",
  "project_id": "019...",
  "origin_host": "titanix",
  "source_main_root": "/home/solab/marielle/...",
  "source_worktree_root": "/tmp/bundle-clone-...",
  "exported_at": "2026-11-25T22:10:00.000000Z",
  "exported_by_bth_version": "0.18.0",
  "include_outputs": false,
  "include_git": false,
  "events_count": 8,
  "events_sha256": "<sha256 of raw events.jsonl bytes>",
  "event_digests": {
    "<eid>": "<sha256 of canonical_json_bytes(envelope)>"
  },
  "sidecar": {
    "included": true,
    "recorded_path": "scripts/analysis/wave1_g2_readout_merged.bth.toml",
    "sha256": "<sidecar_sha256>",
    "missing_reason": null
  },
  "outputs": [
    {
      "relpath": "outputs/wave1_g2_merged.parquet",
      "sha256": "<sha256>",
      "size_bytes": 123456,
      "phase": "run_end",
      "blob_included": false
    }
  ],
  "campaign_context": {
    "campaign_id": null,
    "external_campaign_ref": null
  },
  "git": {
    "git_hash": "dcf6dcca2...",
    "git_branch": "main",
    "git_dirty": false,
    "git_bundle_included": false
  },
  "bundle_sha256": "<sha256 of canonical_json_bytes(manifest_without_bundle_sha256)>"
}
```

**Verification order on import (all in-memory before any disk write):**
1. Validate container safety (§3.5): reject unknown top-level members, traversal, symlinks/hardlinks/special files, member-count/byte-bomb limits.
2. Parse `manifest.json` (`allow_nan=False`); assert `format == "bathos.bundle.v1"` and `bundle_version == 1`.
3. Recompute `bundle_sha256` over `manifest.json` without `bundle_sha256`; mismatch -> `RunBundleTamperedError`.
4. Verify `sha256(events.jsonl) == manifest["events_sha256"]`, parse each line (`allow_nan=False`), validate envelope (`_validate_envelope`), assert line count == `manifest["events_count"]`, verify every `eid` matches `manifest["event_digests"][eid]`, and verify at least one run event for `manifest["run_id"]` is present.
5. **Sidecar verification:** Compute expected `sidecar_sha256` from the run's events.
   - If `sidecar.included` is `true`: verify `sha256(sidecar.bth.toml) == manifest["sidecar"]["sha256"] == run_sidecar_sha256` (`RunBundleTamperedError` on mismatch).
   - If `sidecar.included` is `false` and `run_sidecar_sha256 != ""`: check whether the local worktree (or `imported_sidecars/`) already holds a file whose SHA-256 equals `run_sidecar_sha256`; if not, **refuse** with `RunBundleInvalidError` unless `--allow-missing-sidecar` is explicitly passed on import.
6. **Outputs manifest & blob verification:** Recompute `expected_refs = reference_hashes(events)`.
   - Verify `manifest["outputs"]` matches every `(relpath, sha256, size_bytes, phase)` in `expected_refs` regardless of `include_outputs` (`RunBundleTamperedError` if altered/dropped).
   - If `include_outputs` is `true`: require **every** `expected_refs` entry to have a corresponding `blobs/outputs/<relpath>` member whose streamed `size_bytes` and `sha256` match (`RunBundleTamperedError` / `RunBundleInvalidError` on missing or mismatched blob).

### 3.4 Optional Output File Blobs (`--include-outputs`)

- **Default (`include_outputs=False`):** Metadata + cryptographic hashes only (`output_metadata`, `output_copies`, `manifest["outputs"]`), keeping bundles tiny.
- **`--include-outputs`:** Embeds every regular file in `reference_hashes(events)` under `blobs/outputs/<relpath>`. Enforces `--max-output-bytes` (default configurable per §10 #5) and refuses symlinks/special files.
- **Extraction (`--extract-outputs <dir>`):** Extracts verified blobs into `<dir>/<relpath>` only after walking every parent directory component of `<dir>/<relpath>` with `os.lstat` / `O_NOFOLLOW` to confirm no pre-existing symlink sits in any parent directory. If `<dir>/<relpath>` already exists:
  - Identical `sha256`: idempotent skip.
  - Differing `sha256`: fail closed (`RunBundleTamperedError` / `RestoreConflictError`) unless `--overwrite-outputs` is explicitly passed.

### 3.5 Container Security & Bomb Guards

All bundle reads MUST enforce:
- Strict member allowlist: `manifest.json`, `events.jsonl`, optional `sidecar.bth.toml`, optional `git.bundle`, and `blobs/outputs/<relpath>` only (reject any other top-level path or duplicate member name with `RunBundleUnsafePathError`).
- Strict `PurePosixPath` validation (tighter than `check_root_syntax()` at `reference.py:88-101`, which does not reject `\x00`, `.`, or `//`): reject `\x00`, `\`, `//`, leading `/`, `.` or `..` segments, and Windows drive prefixes.
- Reject non-regular archive members (`issym()`, `islnk()`, FIFOs, devices, sockets; enforce `tarfile` `filter="data"` + explicit member checks) -> `RunBundleUnsafePathError`.
- Archive bomb bounds: cap `manifest.json` (4 MiB), `events.jsonl` (64 MiB), `sidecar.bth.toml` (4 MiB), max member count (default 50,000), and max total uncompressed output bytes (overridable via `--max-output-bytes`).

---

## 4. Imported Provenance, Reader Compatibility, and Equivocation Rules

### 4.1 Preserving `run_id`, Event `ts`, and `envelope.py` Constraints

**Critical invariants (`maraxiom` `review_audit.py:57-214`, `envelope.py:63`, `ingest.py:440`):**
- `mrx review audit` checks `built_at` against `Run.timestamp` (`run.started.ts`) and campaign binding `ts` to detect post-hoc laundering.
- `envelope.py:63` and `ingest.py:440` quarantine any event with `origin not in ("live", "migration")` as `bad_origin`.

Therefore, **import MUST preserve `run_id`, all event `ts` values, and `origin` (`"live"` or `"migration"`) verbatim**.

### 4.2 Durable Receipts, `imported` Root, and `index.db` Schema

Import provenance is stored **durably on disk** alongside the imported log so deleting and rebuilding `index.db` (`repair.py:281-282`, `run_ingest()`) or refolding a single run (`_refold_run()` at `ingest.py:940-960`) **never** erases `import_receipts` or `runs.imported_*`:

1. **Dedicated `imported` log root + immutable `.receipt.json`:**
   - For project-affiliated runs (resolved local `main_root`):
     - Events: `<main_root>/.bth/log/imported/<origin_host>/<run_id>.<bundle_sha256[:12]>.jsonl`
     - Receipt: `<main_root>/.bth/log/imported/<origin_host>/<run_id>.<bundle_sha256[:12]>.receipt.json`
   - For unaffiliated runs: `~/.bth/log/imported/<slug>/<origin_host>/<run_id>.<bundle_sha256[:12]>.{jsonl,receipt.json}`.
   - `discover_roots()` (`src/bathos/runlog/ingest.py:331-397`) enumerates `.bth/log/imported/<origin_host>/` as `root_kind = "imported"`, `root_id = f"{resolved}::{origin_host}"` (parallel to `remote_root_id()`).
   - Verified sidecar bytes are cached read-only at `<catalog_dir>/imported_sidecars/<run_id>/<sidecar_sha256>.bth.toml`.

2. **Immutable `.receipt.json` schema (`v: 1`):**
   ```json
   {
     "v": 1,
     "run_id": "e88aa486-3a4c-4977-826b-2f5d511ca29d",
     "bundle_sha256": "<64-hex>",
     "origin_host": "titanix",
     "source_main_root": "/home/solab/marielle/...",
     "source_worktree_root": "/tmp/bundle-clone-...",
     "imported_at": "2026-11-25T22:15:00.000000Z",
     "import_method": "bundle_import",
     "remote_name": "titanix",
     "local_worktree_root": "/home/marielle/projects/tev_design",
     "local_sidecar_path": "/home/marielle/projects/tev_design/scripts/analysis/wave1_g2_readout_merged.bth.toml",
     "external_campaign_ref": null,
     "git_commit_local": true,
     "sidecar_missing_allowed": false
   }
   ```
   (`import_method` is `"bundle_import"`, `"sync_pull"`, `"sync_pull_mirror_unverified"`, or `"remote_launch"`.)

3. **`import_receipts` table & `runs`/`Run` additive columns:**
   - Add `IMPORT_RECEIPTS_DDL` to `src/bathos/index.py`:
     ```sql
     CREATE TABLE IF NOT EXISTS import_receipts (
         run_id VARCHAR,
         bundle_sha256 VARCHAR,
         origin_host VARCHAR,
         source_main_root VARCHAR,
         source_worktree_root VARCHAR,
         imported_at VARCHAR,
         import_method VARCHAR,
         remote_name VARCHAR,
         local_worktree_root VARCHAR,
         local_sidecar_path VARCHAR,
         external_campaign_ref VARCHAR,
         git_commit_local BOOLEAN,
         sidecar_missing_allowed BOOLEAN,
         PRIMARY KEY (run_id, bundle_sha256)
     )
     ```
   - In `connect_read()` (`src/bathos/index.py:860-875`), add `("import_receipts", IMPORT_RECEIPTS_DDL)` to the `duckdb_tables()` compatibility loop so pre-upgrade `index.db` files read as empty without `CatalogException`.
   - In `fold_roots_into()`, scan `*.receipt.json` inside every `root_kind == "imported"` directory, upsert into `import_receipts`, mark `affected_run_ids.add(receipt["run_id"])`, and inside `_refold_run()` overlay the latest receipt (`ORDER BY imported_at DESC, bundle_sha256 DESC`) onto `runs.imported_from_host`, `runs.imported_at`, `runs.import_method`, and `runs.bundle_sha256`.
   - Add those 4 nullable `VARCHAR` columns to `RUNS_DDL` (`src/bathos/index.py:94-165`) and optional fields (`= None`) to `Run` (`src/bathos/schema.py`). `_run_from_index_row()` (`src/bathos/query.py:203-248`) and `maraxiom`'s `bathos_bridge.py` ignore unknown columns cleanly.
   - **Display surfaces:** `render_run_detail()` (`src/bathos/rich_fmt.py:78-160`), `get_run_tool()` (`src/bathos/mcp.py:596-655`), and `format_citation()` (`src/bathos/cite.py:135-177`) render `"imported from <imported_from_host>"` (plus `import_method` and `imported_at` / `bundle_sha256[:12]`) whenever `imported_from_host` is non-empty.

### 4.3 Local Worktree, `sidecar_path`, and `bathos` / `maraxiom` Reader Seam

1. **Event fields stay immutable; local resolution is SHA-256-verified:**
   - At import time (using `--worktree-root` or `resolve_workspace(Path.cwd())`), bathos probes `<local_worktree_root>/<rel_sidecar>` (handling both relative `_portable_sidecar_path()` paths and remote-absolute suffix matching as in `claim.py:1685-1695`). If the file exists and `sha256(bytes) == run.sidecar_sha256`, `local_sidecar_path` is set to that local absolute path; otherwise it points to `<catalog_dir>/imported_sidecars/<run_id>/<sidecar_sha256>.bth.toml` (when `sidecar.included=true`).
   - Add `resolve_local_sidecar_path(run, catalog_dir, worktree_root=None) -> Path | None` in `bathos.query`, returning the SHA-256-verified local absolute path (checking `import_receipts.local_sidecar_path`, local worktree relative/suffix match, `imported_sidecars/`, and `sidecars/snapshots/`), and surface `resolved_sidecar_path` in `get_run_tool()`.
2. **`output_tiers/run_context.py` (`_resolve_root` at `:75-101`):**
   - Key `RunContext.imported` off **`kind == "run.imported"` OR an `import_receipts` row / `runs.imported_from_host IS NOT NULL`** (explicitly NOT raw `hostname != socket.gethostname()`, avoiding false misclassification on cluster compute nodes or VPN hostname changes).
   - When `ctx.imported` is `True`: accept `--worktree-root <dir>` (`root_source = "override"`), or fall back to `import_receipts.local_worktree_root` if present and `is_dir()` (`root_source = "local_resolved"`), else raise `WORKTREE_UNKNOWN` hinting `--worktree-root`.
3. **Fixing `bathos` internal readers for imported runs:**
   - `checker.py:199-289` (`check_output_sha_drift`) and `claim.py:1620` (`output_metadata_has_sha_drift`): when `entry["path"]` is absent locally (or non-local) and `entry.get("relpath")` is set, resolve `local_worktree_root / entry["relpath"]` (using `import_receipts.local_worktree_root` or current workspace root) before marking `MISSING` or downgrading parity.
   - `claim.py:1655-1696` (`_resolve_member_sidecar`): also check `imported_sidecars/<run_id>/<sidecar_sha256>.bth.toml`.
   - `prereg.py:784-807` (`verify_run_manifest_detail`): when `run.imported_from_host` is set and remote `common_dir` does not exist locally, resolve against local repo's `refs/bathos/locks/<run_id>` or `imported_sidecars/` before failing with `lock_unlocatable`.
4. **Companion `maraxiom` scope (explicit cross-repo boundary):**
   - `maraxiom/src/maraxiom/bathos_bridge.py:1499-1514` (`_popper_state`) and `review_build.py:316-338, 757-769` (`_locate_script`, `_resolve_table`, `_hypothesis`) read `run_obj.sidecar_path` directly and require `Path(sidecar_path).is_absolute() and is_file()`, and require `panel.table in record["output_paths"]` under `repo`.
   - This `bathos` PR makes all catalog records, `Run.timestamp`, `output_metadata`, `bth outputs hash`, `bth check`, `bth claim`, and `resolve_local_sidecar_path()` work locally. Full local `mrx review build --run e88aa486` (without `BANNER_SIDECAR_UNAVAILABLE`) additionally requires a small companion `maraxiom` PR calling `bathos.query.resolve_local_sidecar_path()` in `_popper_state` / `_hypothesis` / `_locate_script` and resolving `relpath` in `_resolve_table` when `output_paths` or `adopted_outputs` holds remote/external paths.

### 4.4 Equivocation & Conflict Rules (Fail Closed)

Every import and ingest pass distinguishes **idempotent replay**, **legitimate lifecycle append**, **Option D stub transition**, and **equivocation**:

| Case | Condition | Outcome |
|---|---|---|
| **Idempotent replay** | Incoming `eid` already exists in `events`, AND `canonical_json_bytes(envelope)` (all 13+ keys) matches `envelope_sha256`. | **No-op** (`skipped_existing`). Bundle import returns `ok=True, status="already_present", new_events=0`. |
| **Same-`eid` equivocation** | Incoming `eid` exists in `events`, AND **any** canonical envelope field differs (including `ts`, `writer`, `seq`, `project`, `project_id`, `main_root`, `worktree_root`, `origin`, `actor`, or `data`). | **Loud refusal (`RunImportConflictError`)** on import before writing any file; quarantined as `eid_conflict` during `run_ingest()`. |
| **Option D stub -> remote `run.started`** | Existing `run.remote_dispatched` stub for `run_id` + incoming remote `run.started` for the same `run_id` whose `sidecar_sha256` and `git_hash` match the stub. | **Allowed** (folded at rank 0; remote `run.started` supersedes the launch stub cleanly). Mismatched `sidecar_sha256`/`git_hash` -> **`RunImportConflictError`**. |
| **Distinct-`eid` genesis equivocation** | Incoming `run.started` has a different `eid` than an existing `run.started` for `run_id` and differs in `sidecar_sha256`, `command`, `argv`, `git_hash`, `timestamp`, or normalized `hostname`. | **Loud refusal (`RunImportConflictError`)**. |
| **Distinct-`eid` terminal / `run_end` equivocation** | Incoming `run.finished` or `run.outputs_hashed` (`phase="run_end"`) has a different `eid` than an existing one for `run_id` with differing `status`/`exit_code`/`outcome`/`output_paths`/`duration_s` or differing `output_metadata`/`refused`. | **Loud refusal (`RunImportConflictError`)** (prevents silent split-brain between `fold_runs.py` latest-wins and `reference_hashes()` earliest-wins). |
| **Incremental append** | New distinct `eid` for same `run_id` representing a later lifecycle event (e.g. `run.finished` after `run.started`, `run.outputs_hashed` (`phase="adopted"`) after `run.finished`, `run.postmortem_applied`, `run.reaped`). | **Accepted and merged**; new `.jsonl` + `.receipt.json` written atomically and `run_id` refolded. |

**Atomicity invariant:** Any verification or conflict failure aborts before writing `.jsonl`, `.receipt.json`, `imported_sidecars/`, or extracted outputs.

---

## 5. Option A: Selective Remote Pull (`bth sync --pull`)

### 5.1 CLI & Parameters (Aligned with `cisternal.wire()`)

Extend `sync_tool` (`src/bathos/mcp.py:2761`) and `mcp_sync_tool` (matching `tests/test_mcp_signature_contracts.py`), using post-`def` `__annotations__` `cyclopts.Parameter(name=[...])` overrides (per `mcp.py:500-512, 3197-3215`):
- `run_ids: list[str] | None = None` (`Parameter(name=["--run", "--run-id"])` — repeatable `--run <id>`, e.g. `--run e88aa486 --run abc123`; each selected run produces/pulls one `bathos.bundle.v1` bundle so `manifest.json` stays single-run).
- `project: str = ""` (`--project <slug>` — narrows prefix resolution and `--since` queries on the remote).
- `since: str = ""` (`--since <iso8601_or_duration>`, e.g. `24h`, `7d`, `2026-11-25T00:00:00Z`).
- `include_outputs: bool = False` (`Parameter(name=["--with-outputs", "--include-outputs"])`).
- `with_campaign: bool = False` (`--with-campaign`).
- `allow_missing_sidecar: bool = False` (`--allow-missing-sidecar`).
- `dry_run: bool = False` (`--dry-run` — resolves matching runs and checks conflicts with zero local writes).

### 5.2 Transport Mechanics (`ssh -o BatchMode=yes` + `bathos.cluster.pull_path`)

**Constraint (`#272 #1`):** `myxcel` is a separate `uv tool` CLI binary (`pyproject.toml` does not depend on `myxcel`). `bathos` MUST NOT `import myxcel` and MUST NOT stream raw binary `.tar.zst` bytes over SSH stdout (which MOTDs / `.bashrc` / `text=True` can corrupt). Instead, use `["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", ...]` (matching `cluster_catalog.py:172` and `remote.py:152`) plus `bathos.cluster.pull_path()` (`myxcel pull-path`, which allowlists `~/.bth/...`):

1. **Selective mode (`run_ids`, `since`, or `project` set):**
   - **Primary path:** Over a single `ssh -o BatchMode=yes` call, resolve matching run IDs on the remote (narrowed by `--project` before prefix lookup so cross-project prefixes don't collide) and invoke remote `bth bundle export <id> -o ~/.bth/tmp/<uuid>.bthbundle` (for each matched run, returning JSON metadata on stdout), pull the staged file(s) from `~/.bth/tmp/` into a local temp dir via `bathos.cluster.pull_path(host, "~/.bth/tmp/<uuid>.bthbundle", ...)` (or directory `~/.bth/tmp/<batch_uuid>/`), remove the remote temp dir via SSH, and ingest each bundle via `import_bundle(..., import_method="sync_pull", remote_name=remote_name)`.
   - **Fallback path (`sync_pull_mirror_unverified`, when remote `bth` lacks `bundle export`):**
     - Pull `~/.bth/log-mirror/<project_id>/` (which `myxcel pull-path` allowlists even for ad-hoc git-bundle clones outside `remote_root`) + `{remote_root}/.bth/log/` into a temporary staging dir.
     - Filter to the causal closure of the requested `run_ids`/`since`/`project`, run completeness checks (require `run.started`, warn if non-terminal, verify `sidecar_sha256` against local worktree or require `--allow-missing-sidecar`), write `.jsonl` + `.receipt.json` with `import_method="sync_pull_mirror_unverified"` (never masquerading as a remote-attested `bathos.bundle.v1`), discard all unrelated runs from staging, and invoke `run_ingest(cat_dir)`.
2. **Whole-remote mode (`bth sync --pull` with no selective filter):**
   - Run existing `pull_cluster_log()` in log mode, **immediately followed by `run_ingest(cat_dir)`**, returning `ingest_report` (`new_events`, `skipped_existing`, `quarantined`, `entities_refolded`).
3. **Fix `bth submit --wait --then-sync` (`src/bathos/cli_cyclopts.py:1345-1350`):**
   - Note explicitly that `--then-sync` runs only when `--wait` is also passed (`:1326`).
   - Resolve `main_root = resolve_log_root(Path.cwd()).main_root` (NOT `cfg_path.parent`, fixing linked-worktree pulls!) and `project_id = read_project_id(main_root / ".bth.toml")`.
   - When `is_log_mode(cat_dir)` is true, call `pull_cluster_log(cluster.remote, config, main_root, project_id)` + `run_ingest(cat_dir)` (alongside `sync_catalog` for legacy/shadow compat), reporting pull failures or quarantined events on stderr.

---

## 6. Option B: Offline Verifiable Run Bundles (`bth bundle` & `bth import`)

**Constraint (`AGENTS.md`):** `src/bathos/export.py` MUST NOT be modified, and `bth export` remains strictly for skill, MCP, plugin, and HTML export.

Add dedicated module `src/bathos/run_bundle.py`:
- **`bth bundle export <run_id> -o <path>`** (`bundle_export` MCP tool):
  - Parameter `output_path: str` annotated via post-`def` `__annotations__` with `cyclopts.Parameter(name=["--out", "-o", "--output-path"])`.
  - Runs `ingest_after_write(cat_dir)`, resolves unique `run_id` prefix (optional `--project` filter), builds causal closure (§3.2; supports both log-mode and legacy catalogs), checks sidecar & output hashes, writes atomically (`<path>.tmp` -> `os.replace`).
- **`bth bundle import <path>`** (`bundle_import` MCP tool) and **CLI-only top-level alias `bth import <path>`** (registered in `cli_cyclopts.py` like `finding` at `:165-167`, avoiding a duplicate MCP tool):
  - Flags: `--worktree-root <dir>`, `--extract-outputs [<dir>]`, `--overwrite-outputs`, `--allow-missing-sidecar`, `--max-output-bytes <n>`, `--catalog-dir <dir>`, `--dry-run`.
  - Runs full verification (§3.3–§3.5) and equivocation checks (§4.4), writes `.jsonl` + `.receipt.json` + cached sidecar atomically, runs `run_ingest(cat_dir)`, and returns `{ok, status, run_id, origin_host, bundle_sha256, new_events, skipped_existing, local_sidecar_path, local_worktree_root}`.

---

## 7. Option D: Remote Launch Registration & Push-Back (`bth run --bth-on-remote <remote>`)

*(Recommended for Part 2 per Decision §10 #3 so Part 1 unblocks `e88aa486` immediately.)*

1. **Launch-time pre-start stub (no genesis conflict):**
   - Before SSH launch, mint `run_id = str(uuid7())` locally and emit a local **`run.remote_dispatched`** event (added to `_RUN_KINDS` in `ingest.py`, folded at rank 0 in `fold_runs.py` like `run.started`, and included in `live_general` at `fold_runs.py:458` so `project_slug`/`command`/`hostname`/`sidecar_sha256`/`git_hash` populate while pending and are overwritten cleanly by the later remote `run.started`). Using `run.remote_dispatched` rather than a second `run.started` prevents Option D from triggering §4.4 genesis equivocation when the remote `run.started` arrives!
2. **Remote preflight & execution (`runner.py` `BTH_RUN_ID` support):**
   - Update `src/bathos/runner.py:1700` to honor validated `os.environ.get("BTH_RUN_ID")` when `BTH_REMOTE_LAUNCH=1`.
   - Remote preflight checks whether remote `~/.bth/catalog/cutover.json` exists (log mode) vs legacy mode — **never** blindly setting `BTH_LOG_MODE=1` on a non-SLURM workstation (`mode.py:48-80` rejects `BTH_LOG_MODE=1` without `SLURM_JOB_ID`).
   - Support `--remote-worktree <path>` (or `remote_root`) and `--push-bundle` (syncing local HEAD/unpushed commit to remote via `git bundle` / `myxcel push` when needed), plus explicit remote `PATH` discovery / `--remote-bth` so non-login SSH shells find `uv` and `bth`.
   - Enforce Option E runtime output binding (§8.1).
3. **Completion push-back & dropped-SSH recovery:**
   - On remote exit (any exit code), export `~/.bth/tmp/<uuid>.bthbundle` on `<remote>`, pull via `bathos.cluster.pull_path()`, and import with `import_method="remote_launch"`.
   - If SSH drops mid-run, the local `run.remote_dispatched` stub remains `status="running"`, protected from `bth reap` by §8.3; `bth sync --pull --run <run_id>` or `bth bundle import` recovers cleanly once the remote job finishes.

---

## 8. Option E: Runtime Output Binding, External Adopt, and Remote Reap Guard

### 8.1 Runtime Output Binding on Remote Launches

When `runner.py` runs in a remote context (`BTH_REMOTE_LAUNCH=1`, `--bth-on-remote <remote>`, or `BTH_REQUIRE_OUTPUTS=1`):
- Before executing the script, check that at least one of `sidecar.outputs.roots`, `--output-paths`, or `BTH_OUTPUT_DIR` is configured.
- If none is set, enforce per Decision §10 #1 (recommended: refuse with `RemoteOutputsUndeclaredError` unless `--bth-allow-unbound-outputs` is passed).
- At run end (`src/bathos/runner.py:2563-2587`), if remote strictness is active (without `--bth-allow-unbound-outputs`) and `outputs_payload.get("refused")` is set, fail the run (exit non-zero) rather than only printing a stderr warning.

### 8.2 External / Remote Root Handling in `bth outputs adopt`

Add `--allow-external-root` and `--relpath-base <dir>` to `adopt_outputs()` (`src/bathos/output_tiers/adopt.py`):
- Without `--allow-external-root`, paths outside `ctx.worktree_root` continue raising `ROOT_OUTSIDE_WORKTREE`.
- With `--allow-external-root`:
  - Require `--relpath-base <dir>` (an existing directory whose components are verified symlink-free) and verify every `--root` is lexically and physically inside `--relpath-base` (still enforcing `SYMLINK_IN_OUTPUTS` and `SPECIAL_FILE_IN_OUTPUTS`).
  - Prefix/normalize `relpath` cleanly (e.g. `<rel_under_base>` with `external_root = str(relpath_base)` recorded on each entry and on the `run.outputs_hashed` `phase="adopted"` event). Refuse collisions if a different `external_root` already adopted the same `relpath` (`INVALID_PARAM`).
  - Update `hash_outputs()` / `_live_state()` (`adopt.py:98, 184`) and `fold_adopted()` (`reference.py:365`) so an entry carrying `external_root` resolves against `Path(entry["external_root"]) / entry.relpath` instead of `ctx.worktree_root / entry.relpath` (fixing the bug where external adopted outputs would otherwise report `missing`).

### 8.3 Remote-Host Guard in `bth reap` (`src/bathos/reap.py`)

In `_classify_reap_candidates()` (`src/bathos/reap.py:425-490`):
- Add hostname normalization helper `_same_host(a, b)` comparing lowercased shortnames (`h.strip().split(".")[0].lower()`) so shortname vs FQDN (e.g. `titanix` vs `titanix.mit.edu`) is handled accurately.
- For non-SLURM runs (`not run.slurm_job_id`): if `run.imported_from_host` is set OR (`run.hostname` is non-empty and not `_same_host(run.hostname, socket.gethostname())`):
  - When `include_remote=False` (the default): skip with reason `"remote_host_skipped"` instead of `"orphan_window_exceeded"`.
  - When `include_remote=True` (`--include-remote`): allow reaping stale remote non-SLURM runs past `older_than_h`.
  - Local stale runs (`_same_host(run.hostname, socket.gethostname())`, such as `e20c0a4b`) remain reapable as before.

---

## 9. CLI / MCP Parity & Error Taxonomy

### 9.1 CLI & MCP Surface (`src/bathos/mcp.py` + `src/bathos/cli_cyclopts.py`)

Per `tests/test_mcp_wire_completeness.py` and `tests/test_mcp_signature_contracts.py`:
- New `@cisternal.tool(registry="bathos-cli", ...)` functions MUST also register `@cisternal.tool(registry="bathos", ...)` wrappers (`mcp_<name>_tool`) and appear in **both** `cli_cyclopts.py`'s `expected=[...]` (`:60-163`) and `mcp.py`'s `_WIRED = wire(app, registry="bathos", expected=[...])` (`:8528-8644`).
- Extended flags on existing tools (`sync`, `reap`, `outputs_adopt`, `run`) MUST be added to both `<name>_tool` and `mcp_<name>_tool` (plus `run_cli_tool` at `mcp.py:3139`, using `--bth-on-remote` / `--bth-allow-unbound-outputs` if needed so `cyclopts` never steals script flags).

| CLI Command | MCP Tool (`registry="bathos"`) | Key Parameters / `Parameter` Aliases | Core Module |
|---|---|---|---|
| `bth bundle export <run_id> -o <path>` | `bundle_export` | `run_id`, `output_path` (`--out`/`-o`/`--output-path`), `project`, `include_outputs`, `include_git`, `with_campaign`, `allow_missing_sidecar`, `max_output_bytes`, `catalog_dir` | `bathos.run_bundle` |
| `bth bundle import <path>` | `bundle_import` | `bundle_path`, `worktree_root`, `extract_outputs`, `overwrite_outputs`, `allow_missing_sidecar`, `max_output_bytes`, `dry_run`, `catalog_dir` | `bathos.run_bundle` |
| `bth import <path>` *(CLI alias only)* | *(uses `bundle_import`)* | Same as `bth bundle import` (wired in `cli_cyclopts.py` only, no duplicate MCP tool) | `bathos.run_bundle` |
| `bth sync` *(extended)* | `sync` | Existing + `run_ids` (`--run`/`--run-id`), `project`, `since`, `include_outputs` (`--with-outputs`/`--include-outputs`), `with_campaign`, `allow_missing_sidecar`, `dry_run` | `bathos.sync` + `bathos.run_bundle` |
| `bth reap` *(extended)* | `reap` | Existing + `include_remote` | `bathos.reap` |
| `bth outputs adopt` *(extended)* | `outputs_adopt` | Existing + `allow_external_root`, `relpath_base` | `bathos.output_tiers.adopt` |
| `bth run` *(extended, Part 2)* | `run` | Existing + `on_remote` (`--bth-on-remote` on CLI), `remote_worktree`, `push_bundle`, `allow_unbound_outputs` (`--bth-allow-unbound-outputs`) | `bathos.runner` + `bathos.remote_run` |

*(Note: `src/bathos/export.py` and `bth export` remain completely untouched.)*

### 9.2 New Error Codes & Exceptions (`src/bathos/errors.py`)

Add to `BathosErrorCode`, `RESOLUTION_HINTS`, `BathosError` subclasses, and `EXCEPTION_TO_CODE`:

| `BathosErrorCode` | Exception Class | Trigger |
|---|---|---|
| `RUN_BUNDLE_INVALID` (`"run_bundle_invalid"`) | `RunBundleInvalidError(BathosError)` | Malformed `manifest.json`, unsupported version, `NaN`/invalid JSON, missing sidecar without `--allow-missing-sidecar`, or missing `run_id` events. |
| `RUN_BUNDLE_TAMPERED` (`"run_bundle_tampered"`) | `RunBundleTamperedError(BathosError)` | `bundle_sha256`, `events_sha256`, `event_digests`, `sidecar_sha256`, `manifest.outputs`, or output blob SHA-256 mismatch. |
| `RUN_BUNDLE_UNSAFE_PATH` (`"run_bundle_unsafe_path"`) | `RunBundleUnsafePathError(BathosError)` | Unknown top-level archive member, traversal (`..`, `.`, `//`, `\x00`, `/`), symlink, hardlink, special file, or bomb limit exceeded. |
| `RUN_IMPORT_CONFLICT` (`"run_import_conflict"`) | `RunImportConflictError(BathosError)` | Same `eid` with differing envelope fields, or distinct `eid` with conflicting `run.started`, `run.finished`, or `run.outputs_hashed` (`phase="run_end"`). |
| `REMOTE_OUTPUTS_UNDECLARED` (`"remote_outputs_undeclared"`) | `RemoteOutputsUndeclaredError(BathosError)` | Remote launch started with no `[outputs] roots`, `--output-paths`, or `BTH_OUTPUT_DIR` (or `run_end_payload` refused under remote strictness). |
| `REMOTE_LAUNCH_FAILED` (`"remote_launch_failed"`) | `RemoteLaunchError(BathosError)` | Remote SSH launch, `BTH_RUN_ID` propagation, or remote preflight failed. |

---

## 10. Open Owner Decisions (Surface via `ask_question`)

1. **Option E runtime output enforcement on remote launches:**  
   - *(Recommended)* **Refuse** remote `bth run` (`BTH_REMOTE_LAUNCH=1` / `--bth-on-remote`) if neither `[outputs] roots`, `--output-paths`, nor `BTH_OUTPUT_DIR` is set (with `--bth-allow-unbound-outputs` opt-out; optionally warn-only for `--no-sidecar` exploratory runs).  
   - *Alternative:* Warn loudly on stderr only and allow the run to proceed unbound.
2. **CLI verb naming for offline bundles:**  
   - *(Recommended)* `bth bundle export <run_id> -o <path>` + `bth bundle import <path>` + CLI alias `bth import <path>`, leaving `src/bathos/export.py` and `bth export` strictly untouched.  
   - *Alternative:* Overload `bth export --run <run_id> -o <path>` in `cli_cyclopts.py`.
3. **PR phasing for Option D (`bth run --bth-on-remote <remote>`):**  
   - *(Recommended)* **Two-part rollout:** Part 1 ships `bathos.bundle.v1`, `.receipt.json` + `import_receipts`, Option B (`bth bundle`/`bth import`), Option A (`bth sync --pull --run` + `run_ingest` + `submit --wait --then-sync` fix), Option E (`run_context.py` / `adopt.py` / `checker.py` / `claim.py` / `reap.py` guard) to unblock `e88aa486` immediately; Part 2 ships Option D (`bth run --bth-on-remote <remote>`).  
   - *Alternative:* Ship Options A + B + D + E in one PR.
4. **Campaign behavior on single-run pull/import (`--run <id>`):**  
   - *(Recommended)* Preserve `runs.campaign_id` via `external_campaign_ref` without emitting dangling `campaign.run_added` or pulling sibling runs unless `--with-campaign` is passed.  
   - *Alternative:* Auto-pull the full campaign whenever a pulled run has `campaign_id` set.
5. **Output blob size guard for `--include-outputs`:**  
   - *(Recommended)* Metadata-only by default; `--include-outputs` enforces a 512 MiB default ceiling unless `--max-output-bytes <n>` (`0` for unlimited) is passed.  
   - *Alternative:* No default byte cap when `--include-outputs` is explicitly specified.

---

## 11. Testable Acceptance Criteria & Mandatory Negative Controls

### 11.1 Acceptance Criteria (`AC-1` .. `AC-12`)

- **AC-1 (Bundle round-trip & full causal closure):** `bth bundle export <run_id> -o out.bthbundle` + `bth import out.bthbundle` into a fresh catalog reproduces `runs`, `sidecar_anchors`, `output_copies`, `run_edges`, `supersession_edges`, `submits`, `blast_radius_ledger`, `trust_ledger`, `archived_items`, `observations`, and `acknowledgment_ledger` rows, preserving original `run_id`, `Run.timestamp`, `sidecar_sha256`, `outcome`, and `output_metadata` verbatim.
- **AC-2 (Durable receipts across `index.db` rebuild & `_refold_run`):** Deleting `index.db` and rebuilding via `run_ingest()` (and refolding an imported run via `_refold_run()`) deterministically repopulates `import_receipts` and `runs.imported_from_host` / `imported_at` / `import_method` / `bundle_sha256` from `.receipt.json`; `bth show` and `bth cite` display `"imported from <origin_host>"`.
- **AC-3 (Selective `bth sync --pull --run`):** Pulling `--run <id>` from a multi-project remote stages a `bathos.bundle.v1` in remote `~/.bth/tmp/`, fetches via `bathos.cluster.pull_path()`, writes only that run's `.jsonl` + `.receipt.json` locally, leaves unrelated runs absent, and runs `run_ingest()` automatically so `bth show`/`sql`/`cite` see the run immediately.
- **AC-4 (Filter flags `--project`, `--since`, `--dry-run`, legacy remote):** `bth sync --pull --project <slug> --since <ts>` imports only matching runs (narrowing prefix lookup by `--project`), works on both log-mode and legacy-mode remotes, and `--dry-run` performs zero local writes.
- **AC-5 (`bth submit --wait --then-sync` in linked worktrees):** Calls `pull_cluster_log()` using `resolve_log_root(Path.cwd()).main_root` (not `cfg_path.parent`) and runs `run_ingest(cat_dir)` automatically.
- **AC-6 (Idempotent replay & incremental extension):** Re-importing/re-pulling an identical bundle returns `status="already_present"`, `new_events=0`; importing a subsequent bundle with only new post-run events (e.g. `run.outputs_hashed` adopt `01a21455`) merges cleanly and refolds `run_id`.
- **AC-7 (Local worktree, `bth outputs hash`, `bth check`, `bth claim`):** Importing a remote run whose local checkout contains matching sidecar/outputs resolves `local_sidecar_path` & `local_worktree_root`, lets `bth outputs hash <run_id> [--worktree-root ...]` report `match`, and prevents `check_output_sha_drift()` / `output_metadata_has_sha_drift()` from false-downgrading parity to `uncontrolled`.
- **AC-8 (`maraxiom` audit timestamp preservation):** `Run.timestamp` and any campaign event `ts` match the executing host's RFC 3339 timestamps verbatim; single-run imports with `external_campaign_ref` emit no dangling `campaign.run_added` (unmasking a campaign-bound run in `maraxiom` requires `--with-campaign`, since `bathos_bridge.py` fails closed with `BANNER_CAMPAIGN_UNREADABLE` when the campaign row is absent locally).
- **AC-9 (Safe `campaign_id` preservation):** Single-run import with `campaign_id` set preserves `runs.campaign_id` across `_refold_run()` and `reconcile_campaign_runs()` without pulling sibling runs; `--with-campaign` imports full campaign events cleanly.
- **AC-10 (`bth reap` remote-host guard):** Default `bth reap --apply` skips non-SLURM `running` runs where `imported_from_host` is set or `hostname` differs (normalized shortname) with `"remote_host_skipped"`, while reaping stale local runs (`e20c0a4b`); `--include-remote` opts in explicitly.
- **AC-11 (Option E runtime binding & external adopt):** Remote launch enforces declared outputs per §10 #1 and treats `run_end_payload` `refused` as failure; `bth outputs adopt --allow-external-root --relpath-base <dir>` adopts regular external files and `bth outputs hash` verifies them against `external_root` as `match`.
- **AC-12 (Option D launch & dropped-SSH recovery):** `bth run --bth-on-remote <remote>` emits `run.remote_dispatched` (rank 0), passes `BTH_RUN_ID`, pushes back `bathos.bundle.v1` on exit without genesis conflict, and recovers via `bth sync --pull --run <run_id>` on dropped SSH.

### 11.2 Mandatory Negative Controls (`NC-1` .. `NC-10`)

- **NC-1 (Tampered envelope metadata or `NaN` refused):** Altering any non-`data` key (`ts`, `writer`, `seq`, `project_id`, `origin`, `actor`) or injecting `NaN` in `events.jsonl` raises `RunBundleTamperedError` / `RunBundleInvalidError` with zero disk writes.
- **NC-2 (Same-`eid` envelope conflict in `run_ingest`):** Duplicate `eid` with identical `(kind, entity, data)` but altered `ts`, `writer`, or `v` is quarantined as `eid_conflict` (not skipped).
- **NC-3 (Distinct-`eid` genesis/terminal/`run_end` equivocation refused):** Conflicting `run.started`, `run.finished`, or `run.outputs_hashed` (`phase="run_end"`) for the same `run_id` fails closed with `RunImportConflictError`.
- **NC-4 (Missing or tampered sidecar / output manifest / blob refused):** Tampered `sidecar.bth.toml`, altered `manifest["outputs"]` (even with `include_outputs=false`), missing `reference_hashes` blob when `include_outputs=true`, or `sidecar.included=false` without `--allow-missing-sidecar` fails closed.
- **NC-5 (Archive traversal, unknown member, symlink, pre-existing parent symlink, clobber refused):** Unknown top-level member, `../`, `./`, `//`, `\x00`, symlink/hardlink/FIFO, pre-existing symlink under `--extract-outputs <dir>`, or existing differing file (without `--overwrite-outputs`) raises `RunBundleUnsafePathError` / `RunBundleTamperedError` with zero writes.
- **NC-6 (Adopt path safety & collision guard preserved):** `bth outputs adopt --allow-external-root` still refuses symlinks (`SYMLINK_IN_OUTPUTS`), special files (`SPECIAL_FILE_IN_OUTPUTS`), `run_end_conflict`, and colliding relpaths across distinct `external_root`s.
- **NC-7 (`src/bathos/export.py` untouched & `bth export` regression-free):** `git diff src/bathos/export.py` is empty and all existing `bth export` tests pass.
- **NC-8 (No `bad_origin` quarantine & MCP contract tests pass):** Imported events keep `origin in ("live", "migration")` (never quarantined as `bad_origin`), and `tests/test_mcp_wire_completeness.py`, `tests/test_mcp_signature_contracts.py`, and `tests/test_cli_flag_contracts.py` pass.
- **NC-9 (Remote `running` run protected from default `bth reap`):** Remote non-SLURM `running` run (`hostname="titanix"` or `titanix.mit.edu`, age 48h) is NOT marked `abandoned` by default `bth reap --apply`.
- **NC-10 (No `import myxcel` & no raw binary SSH stdout):** Codebase has zero `import myxcel` statements; selective pull stages under remote `~/.bth/tmp/` and transfers via `bathos.cluster.pull_path()`.
