---
title: 'Output tiering: warm, cold and mirror copies of run outputs, recorded and verified by bathos'
description: Standard for where a run's output files live (warm local path, checksummed cold archive, mirror), how bathos records and verifies each copy, and how `bth warm` restores them; reuses myxcel profiles and transport, folds in naurmalade's coldify wrapper, and notes the already-fixed reap log-mode bug
status: draft
task_id: 261023_bathos-output-tiering
date: '261023'
backlog_ids: ''
adversarial_review: audits/261023_output-tiering-argon-review.md
---
# Output tiering: warm, cold and mirror copies of run outputs

**Status:** draft, not reviewed. Supersedes the denxity-worktree draft
`denxity/.claude/worktrees/qb-r6/.praxia/docs/specs/261023_bathos-output-tiering.md` (uncommitted, treated as input,
not as a decision). **task_id:** 261023_bathos-output-tiering. Decisions are in §11 (all decided). Revised after an adversarial review (§13).

## 1. Problem

Run outputs are untracked by git by design (denxity `.gitignore`: "tracked via bathos catalog, not git"), so they
exist only on the machine that ran the job. Nothing in bathos records where an output's authoritative copy is, or
whether a copy still matches the run. The trigger case is the denxity r6/r7 `qb_mi_coverage` set: 846 MB, 888 files
under `denxity/.claude/worktrees/qb-r6/outputs/validation/`. The worktree cannot be removed without losing them.
They were cold-copied by hand on 2026-10-23: three zstd tars plus `SHA256SUMS`, about 196 MB, on the Engaging so3
pool and on titanix.

The probes in §2 found a second, deeper problem. **The run records anchor 8 of the 888 files.** The other 880,
including the per-cell resume directories that hold most of the bytes, are not in any run's `output_paths`. So
"archive one run's `output_paths`" would, as things stand, archive almost nothing. The standard has to start by
making a run declare and hash its whole output set.

## 2. Assumptions: verified / unverified

Tags: **V** = probed or read at file:line in code today. **U** = not verified. **S** = verified by a throwaway
spike whose number must not be cited until re-run under a sidecar (plan step 0).

| # | Assumption | Tag | Evidence |
|---|---|---|---|
| A1 | Run records store per-output hashes | **V, partly** | `output_metadata` JSON list `{path,status,size_bytes,mtime_unix,sha256}` (`schema.py:94`). Written at run end in log mode as a `run.outputs_hashed` event (`runner.py:1636-1646`). |
| A2 | Those hashes cover every output file | **V, false** | `_collect_output_metadata` (`compact.py:151-189`) sets `sha256=None` for files ≥ 100 MiB (`compact.py:171`). A directory in `output_paths` gets no hash: `open()` on a directory raises, the exception is swallowed (`compact.py:177`), and the code never walks the directory. |
| A3 | The r6/r7 run records cite the r6/r7 outputs | **V, false for 880/888 files** | 4 runs: `1e4596c6` (r6, outcome `a5_not_calibrated`), `989c3174` (r7, `a5_calibrated_replicas`), `e3176770` / `14a9e161` (review tables, `observed`). `output_paths` lists 1 + 1 + 3 + 3 = 8 files. The `*_cells/` dirs (392 + 486 files) and the two `*_review_page.html` are cited by no run (`bth sql` over `output_paths LIKE '%_cells%'`). The cells are the script's `--resume-dir` (`qb_mi_coverage_r6.py:31-34`). |
| A4 | The recorded hashes match the archived files | **S** | All 8 recorded sha256s equal both the copy extracted from `review_pages_and_top_level.tar.zst` and the live worktree file. The 3 staged archives pass `sha256sum -c SHA256SUMS`, and hold 392 + 486 + 10 = 888 files, matching the 888 on disk. |
| A5 | The other 880 files have some integrity anchor | **S, partial** | Only the hand-made round-trip diff and the archive-level `SHA256SUMS`. The r6/r7 result JSON lists `_cells/` 368 and 457 times among ~800 and ~980 hex digests. That is a script-specific, incomplete transitive anchor, not a bathos contract. |
| A6 | The recorded hash is immutable after the run | **V, false** | The log-mode fold takes the **latest** `run.outputs_hashed` event (`runlog/fold_runs.py:474-477`). `bth check --rebaseline` appends a new one (`checker.py:116-167`). Legacy compact rewrites the hash whenever mtime changes (`compact.py:1204-1235`). The run-end event survives in the log, but nothing reads it back as "the hash at run end". |
| A7 | `bth archive-artifact` can carry large untracked outputs | **V, technically yes, unsuitably** | Untracked paths are packed into a throwaway-repo **git bundle** (`artifact_archive.py:277-307`, `_build_untracked_bundle` at `:379`). It does not compress with zstd, keeps no per-file manifest bathos can check against `output_metadata`, has no remote/cold root, no mirror and no verify-from-fresh-read. It also stubs in place and commits, so it is git-native by design. Wrong tool for 800 MB of binary output. |
| A8 | `myxcel coldify` can relocate a local source | **V, no** | `_resolve_source` (`myxcel/coldify.py:415-589`) builds only remote paths. The worker (`scripts/coldify_remote.py`) runs on the cluster as a SLURM job on `mit_data_transfer` (`coldify.py:95-148`) and copies remote→remote. |
| A9 | myxcel's pieces are reusable | **V** | `load_profile()` (`myxcel/config.py:82`, `cold_root` parsed at `:125`, optional). Cache-cold sha256 is fsync + `POSIX_FADV_DONTNEED` + fresh fd (`coldify_remote.py:38-56`), with a three-state `VerifyResult` (`coldify.py:919-922`). ssh is bare `ssh <host>` riding the user's ControlMaster. No `config show --json` exists. |
| A10 | myxcel writes `.partial` and renames | **V, no** | No `.partial` anywhere in `src/` or `scripts/`. The worker copies straight to the final cold path and refuses if it exists (`coldify_remote.py:139-145`). A failed attempt therefore blocks its own retry. |
| A11 | myxcel `_tree_hash` equals a plain sha256 | **V, no** | Single file → `sha256("" + ":" + filehash)` (`coldify_remote.py:30-32,59-67`). Any reuse must compute the same form or add a file mode. |
| A12 | titanix has a `cold_root` | **V, yes since D4** | Originally only `engaging.toml:54` set one. The owner added `titanix.toml:40` (`/home/solab/marielle`) on 2026-10-23. `~/.config/bathos/` does not exist. |
| A13 | naurmalade already systematizes cold storage | **V, partly** | `naurmalade/campaign/coldify.py:82-227` wraps `myxcel coldify` per campaign trajectory and appends a `trajectory_segments` row (`content_sha256`, `size_bytes`, `is_external`, `catalog/tables.py:71-73`). No per-file manifest, no zstd, no mirror, no restore, no bathos link. |
| A14 | naurmalade's recorded `content_sha256` is populated | **V, false (bug)** | `parse_sha256(verify_text)` (`coldify.py:42-44,173`) regex-searches `myxcel coldify verify` stdout, which prints only `verified` (`myxcel/cli.py:2688-2689`). The result is always `None`. The real hash is in myxcel's `.myxcel/coldified.jsonl`. |
| A15 | The isochore resolver is the reference pattern | **V** | `isochore traj_cache.cache_root_source` (`traj_cache.py:156-186`): explicit > `$TRAJ_CACHE_ROOT` (`none`/empty disables) > nearest `[tool.traj_cache]` > `${XDG_CONFIG_HOME:-~/.config}/traj_cache/config.toml` > unset. Returns `(value, layer)` and raises `ValueError` naming a malformed file. |
| A16 | bathos already reads `[tool.bathos]` | **V** | `dependency_provenance.resolve_tracked_packages` reads `[tool.bathos] track_packages` from the nearest pyproject (`dependency_provenance.py:100-120`). A new `[tool.bathos.outputs]` sub-table does not collide. |
| A17 | `bth reap --apply` fails in log mode from a shared log/index cause | **V, fixed on main, not deployed** | `reconcile_warm_tier` called `compact(force_rebuild=True)` (`reap.py:457`), which log mode rejects (`compact.py:900-905`) **after** the `run.reaped` events were written. Fixed by `fce4b803` + `c99271f0` (PR #127, merged into `main` 164a6b7d today). The fix is **not** in v0.13.0a7 and **not** in the installed `bth` (`~/.local/share/uv/tools/bathos/.../reap.py` lacks the guard). Cause: a legacy post-step not gated on log mode. It is not shared with output tiering, apart from the general rule in §6.3. |
| A18 | Remote cache-cold verify on a login node is allowed | **U** | myxcel moved its worker to a SLURM job, citing a login-node ban (myxcel design deviation (a)). Whether hashing ~200 MB over ssh on a login node breaks Engaging policy is not checked. See D3. |
| A19 | The so3 pool's setgid group and quota accept bathos-cold writes | **V for the one manual copy, U in general** | The manual copy landed group `orcd_rg_hstor012_pi_so3`, setgid (denxity draft). Quota headroom and file-count cap are not probed. Archives are few, large files, so the file count is low by construction. |
| A20 | A new event kind folds without touching existing folds | **V, false (corrected after review)** | `fold_roots_into` (`runlog/ingest.py:1164-1190`) dispatches per kind family with an explicit `if/elif` and advances the watermark past unknown kinds. `connect_read` (`index.py:440-456`) lists every view by hand. A new kind needs wiring in both, plus the `connect_read` guard for an index that predates the table (§5.3; review cycle 2 found there is no `INDEX_SCHEMA_VERSION`). |
| A21 | `mv -n` fails when the target exists | **V, version-dependent** | GNU coreutils 9.4 (this box): exit 1, source kept. Older coreutils exit 0 without moving. Engaging/titanix versions: U. Replaced by `ln && rm` (§6.1). |
| A22 | Existing `output_metadata` paths are project-relative | **V, false** | `BTH_OUTPUT_DIR` discovery records absolute paths (`runner.py:1302,1496`). `--output-paths` stores the raw string. `_collect_output_metadata` resolves against the process cwd. |
| A23 | Rebaseline events are distinguishable from run-end events | **V, false today** | `rebaseline_run_outputs` (`checker.py:116-167`) and the run-end emit (`runner.py:1636-1646`) both emit `run.outputs_hashed` with no `phase`. Migrated runs carry `output_metadata` in `run.imported` (`importer.py:197-209`). |
| A24 | Event writes and index refresh are reliable | **V, with gaps** | `emit_event` defaults to `hard_fail=False` (`runlog/emit.py:167`). `ingest_after_write` is skipped under `SLURM_JOB_ID` / `BTH_NO_INGEST=1` (`ingest.py:1006-1017`). `uuid7` is ms + random, so same-ms events order randomly (`runlog/envelope.py:29-40`). The envelope `ts` has µs resolution (`envelope.py:84-88`), so ties are rarer than the ms `eid` suggests but still possible; the fold tie-breaks on `kind_rank`. |
| A25 | CLI layer | **V** | No `cli.py`. Commands are `@cisternal.tool` in `mcp.py`, wired by `cli_cyclopts.py`. `render_or_exit` always exits 1 on error (`cli_render.py:35-50`). |
| A26 | bathos has no user-level config file | **V, false** | `~/.bth/config.toml` `[defaults]` is read at `runner.py:875`. It conflicts with the global XDG rule. See D9. |
| A27 | POSIX hard links work under the cold roots (`ln` is the commit primitive, §6.1) | **U** (review cycle 2, m5) | so3 `/orcd/data/so3/001` and titanix `/home/solab` filesystems not probed. Step 5a adds a live owner-gated probe on both hosts; if `ln` is refused, commit falls back to `renameat2(RENAME_NOREPLACE)` via a remote helper, and a filesystem with neither refuses `commit_unsupported`. |
| A28 | `bth cold` can run where a cluster run's files live | **U, likely false** (review cycle 2, M4) | A cluster run's `worktree_root` comes from `BTH_WORKSPACE_ROOT` (`workspace.py:96`), so its files are on the cluster. myxcel profiles are laptop ssh aliases (myxcel `config.py:101`), and no Engaging-side profile is known. v1 tiers only runs whose `worktree_root` exists on the host running `bth cold` (`worktree_missing` otherwise). Cluster-resident runs are a non-goal (§10). |

## 3. The standard (what bathos enforces)

1. **A run declares its whole output set.** A sidecar may declare `[outputs] roots = [...]`, files or directories,
   relative to the run's **live worktree root**: `resolve_workspace(cwd).fs_root` (`workspace.py:24-30`), not the
   recorded `identity_root`. At run end bathos expands every root to regular files, with no size cap and no directory
   skip (one shared hasher, `output_tiers/reference.py::hash_file`, replaces the capped one in
   `compact.py:151-189`, and `checker.py`'s re-hash uses the same function, so a ≥100 MiB file is compared rather
   than reported `UNREADABLE` (`checker.py:277-285`), which `checker.py:347` counts as drift and `claim.py:1304`
   turns into a parity downgrade (review cycle 2, F-05)), and emits `run.outputs_hashed` with `"phase": "run_end"`. Each entry keeps today's keys (`path`, `status`,
   `size_bytes`, `mtime_unix`, `sha256`) so `checker.py:223`, `query.py:64`, `readback.py` and `mcp.py` keep working,
   and adds `relpath` (POSIX, relative to `fs_root`) and `phase`. The run's `worktree_root` is already on the event
   envelope. `--output-paths` and auto-discovered `BTH_OUTPUT_DIR` files (absolute today, `runner.py:1302,1496`) are
   normalised to `relpath` the same way.
2. **Expansion refuses anything that is not a regular file inside `fs_root`.** A symlink (to a file or dangling),
   FIFO, socket or device under a root is a `symlink_in_outputs` / `special_file_in_outputs` refusal at run end. The
   run still finishes; its record carries the refusal and has no reference set. Paths that resolve outside `fs_root`
   are refused the same way.
3. **The reference set is fixed per path and never rewritten.** `reference_hashes(run_id)` = per-path union of:
   (a) entries from the event with `phase: "run_end"`; (b) for runs from before this standard, the `output_metadata`
   carried by `run.imported` (`importer.py:197-209`) or the **earliest** phase-less `run.outputs_hashed`, labelled
   `run_end_legacy`; (c) `adopted` entries, only for paths with no (a)/(b) entry.
   **Legacy precedence and gaps (review cycle 2, M2/M3):**
   - For (b), `run.imported` wins. Otherwise only a phase-less event **before the run's `run.finished`** counts: the
     run-end emit precedes it (`runner.py:1643` vs `:1650`), and a pre-Step-2 rebaseline (`checker.py:161`, also
     phase-less) can only come after it.
   - A legacy entry with `sha256: null` (a file of 100 MiB or more, or a directory) counts as **absent**, so `adopt`
     may cover it. A legacy entry with a sha is never overridden.
   - A legacy entry carries only `path`. Its relpath is that path minus the run's `worktree_root` if absolute, or the
     path itself if relative (resolved against `worktree_root`). If that file does not exist, tiering refuses with
     `legacy_path_unresolved` naming it, and `adopt` cannot cover it.
   - **Adopt is closed to standard runs.** From Step 2, `run.started` records `outputs_declared: true` when the
     sidecar declares `[outputs] roots`, and the run-end `run.outputs_hashed` is emitted with `hard_fail=True`. It
     also records a refusal (symlink or special file) as a `phase: "run_end"` event with `refused: <code>` and no
     entries, which is a tombstone. `adopt` on a run with `outputs_declared` refuses (`adopt_closed`), whether or not
     a run_end event exists. Otherwise a failed or refused run-end hash would let `adopt` vouch for files modified
     later. The fix is a new run. Rebaseline events
   (`checker.py:116-167`, which from Step 2 tag `"phase": "rebaseline"`) are never part of the reference.
   **Fold, per path (review cycle 2, F-04).** `fold_runs.py:474-477` today sets `output_metadata` from the latest
   `run.outputs_hashed`, whole list for whole list. From Step 2 it **skips `phase: "adopted"` events entirely** when
   setting `output_metadata`. An adopt event carries only the adopted paths, so letting it win would drop the run-end
   entries. Adopted entries fold into a separate `runs.adopted_outputs` column (latest non-retracted adopt per
   relpath), and `reference_hashes()` is computed as `{relpath: run_end entry if any, else run_end_legacy entry if any,
   else adopted entry}`. Since `output_metadata` never holds adopted entries, `readback.py:231-249` (which matches any
   `output_metadata` sha with no phase filter) can't make an adopted file a trust candidate, and AC-20
   (`claim.py:1300-1305`) never sees one. Rebaseline keeps its current latest-wins effect on `output_metadata` (owner
   bug §12.3) but is excluded from `reference_hashes()`.
   **Which `worktree_root` (F-12).** A relpath is resolved against the `worktree_root` of the event that recorded
   it: run-end entries against the run's own events. `adopt` refuses roots outside the run's recorded
   `worktree_root` and writes that root, never the adopter's cwd, onto its event. A `run.imported` run's
   `worktree_root` is untrusted, because the importer defaults it to `catalog_dir` (`importer.py:800-801`). Every
   tiering command on such a run refuses with `worktree_unknown` unless it gets `--worktree-root <path>`, which is then
   recorded on the event it writes.
4. **Drift is measured on the live files, not on history.** `outputs_drifted` means a live file under the run's
   `worktree_root` differs from its reference hash. A past rebaseline that later got restored is not drift (D5).
   A drifted `run_end` path can never be re-referenced. The fix is a new run, not `adopt`.
5. **Three tiers per run:** `warm` (a local directory), `cold` (one zstd tar + manifest on the cold profile's root)
   and `mirror` (the same files on a second profile). Each physical copy is one row in `output_copies` (§5.3). The run
   record stays the source of truth for file hashes.
6. **Copies are written two-phase, per attempt, and never overwritten** (§6.1). A final name that exists with
   different content is a hard error. Superseding is a rename-aside, never a delete (§6.4).
7. **bathos never deletes warm files** (§10).
8. **No defaults for locations.** If no cold profile resolves, every tiering command refuses and names the layers it
   checked.

## 4. Command surface

Commands live in a package `src/bathos/output_tiers/` (`config`, `reference`, `archive`, `cold`, `warm`, `verify`).
They are registered as `@cisternal.tool(registry="bathos-cli", ...)` in `mcp.py` and wired by `cli_cyclopts.py`.
There is no `cli.py`. Errors go through `render_or_exit` (`cli_render.py:35-50`), so every failure exits 1. Callers
branch on the JSON `error_code`, never on exit status. Every command accepts a unique run-id **prefix** and resolves
it to the full UUID before doing anything (0 or more than 1 match is a refusal). `adopt` and `cold` take `--dry-run`,
which prints the file list, byte total and destinations and writes nothing.

| Command | Does | Refuses when (`error_code`) |
|---|---|---|
| `bth outputs hash <run>` | Re-hashes the live files under the run's `worktree_root` against `reference_hashes`. Reports `match` / `drift` / `missing` per path. Read-only. | No reference set (`no_reference_hashes`). |
| `bth outputs adopt <run> --root <path>... --reason <text>` | Hashes the given roots **now** and emits `run.outputs_hashed` with `phase: "adopted"`, containing only paths with no `run_end`/`run_end_legacy` entry. `--retract <eid> --reason` emits `run.outputs_adopt_retracted`, and the fold drops those entries. | Run not terminal (`run_not_terminal`). Root outside the run's `worktree_root` (`root_outside_worktree`). A path already has a `run_end` entry with a different hash (`run_end_conflict`, meaning re-run instead). Symlink or special file (§3.2). |
| `bth cold <run> [--profile P] [--mirror M]` / `--all-uncopied` | Builds archive + manifest (§5.2), checks every member against `reference_hashes`, then writes cold, then mirror (§6.1). `--all-uncopied` covers the current project only: completed runs that have a reference set and no verified copy on the profile. It keeps going past per-run failures and exits 1 at the end if any run failed. | `no_reference_hashes`, `outputs_drifted`, `no_cold_root`, `archive_conflict`, `myxcel_capability_gap`. |
| `bth warm <run> --to <dir>` | `<dir>` must not exist. Fetches the archive and manifest from the first `verified` copy via `myxcel cold get`, checks both sha256s **before** opening the tar, checks that the member set equals the manifest exactly, then extracts with `tarfile` `filter="data"` into the sibling `<dir>.bth-warm-<attempt8>.partial/`. Every file is hashed against the manifest and `reference_hashes`, then the staging directory is renamed to `<dir>` with `renameat2(..., RENAME_NOREPLACE)` (via `ctypes`; a plain `os.rename` would replace an empty `<dir>` created after the existence check, review cycle 2 m3). `EEXIST` → `warm_target_exists`, and a filesystem without the flag → `warm_noreplace_unsupported`, with staging kept for the owner. Emits `output_copy.recorded tier=warm`. | `<dir>` exists (`warm_target_exists`). Any mismatch, extra or missing member (`warm_mismatch`), after which bathos deletes only its own staging directory. No verified copy (`no_verified_copy`). |
| `bth verify --tier outputs [--run R] [--profile P] [--strict]` | For each cold/mirror copy, calls `myxcel cold verify-file` on both archive and manifest, then checks cold and mirror agree. Emits `output_copy.verified`. Each copy is `verified` / `failed` / `unverifiable`. **`--tier all` does not include `outputs`**, because it needs the network. | Never refuses. Exits 1 if any copy is `failed`, or `unverifiable` with `--strict`. |
| `bth outputs where <run>` | Lists rows from `output_copies`, each with the config layer that chose it. | Ships in Step 6, together with the table. |

**Three-state rule** (aligned with myxcel `VerifyResult`). `verified`: the bytes were re-read and matched. `failed`:
the bytes were read and did not match. `unverifiable`: the bytes could not be read. `unverifiable` never turns into
either of the other two.

## 5. Data formats

### 5.1 Config

Resolution applies to **profile names**, not paths, in this order:

1. explicit `--profile` / `--mirror`;
2. `$BTH_COLD_PROFILE` / `$BTH_MIRROR_PROFILE`. At every layer, including the explicit argument and the config
   files, `none` or empty disables the leg and stops resolution, so a project can opt out over a user default (as
   built in Step 1, PR #130);
3. the nearest `pyproject.toml` `[tool.bathos.outputs]` `cold_profile`, `mirror_profile`;
4. `${XDG_CONFIG_HOME:-~/.config}/bathos/config.toml` `[outputs]` with the same keys (D9; `~/.bth/config.toml` is
   not read for this);
5. off.

bathos stores no paths, so a project setting cannot point the mirror leg at the cold host's path (the split-brain
risk in F-21 doesn't arise). Each profile name resolves through `tomllib` on `~/.config/myxcel/<name>.toml` (D2).
The **pinned key contract** is top-level `host` (str, the ssh alias) and `cold_root` (str, absolute on that host).
Any other key is ignored. A missing `cold_root`, or one that is not absolute (`~` would expand locally), refuses with
`no_cold_root`, naming the file. Every TOML parse error in a file the resolution **reaches** raises, naming the file.
An unknown key under `[tool.bathos.outputs]` / `[outputs]` raises too, so a typo can't fall through silently. Files
below the deciding layer are not read, so a broken user config does not block an explicit `--profile` (as in
isochore's `cache_root_source`). The `dependency_provenance._load_toml` swallow pattern (`dependency_provenance.py:86-97`) is
explicitly not reused. Each resolution returns `(value, layer_label)`, which every command shows and which goes into
the copy row. Archives land at `<cold_root>/bathos-cold/<project_slug>/<full_run_id>/`. bathos passes myxcel the path **relative to `cold_root`** (`bathos-cold/<slug>/<run_id>/…`, §6.2); the absolute form is for display only. myxcel `cold put` and `cold lock` create missing parent directories (`mkdir -p` on the parent, then the atomic `mkdir` for the lock; review cycle 2, m1).

### 5.2 Archive and manifest

- `run-<full_run_id>.tar.zst`: a POSIX tar of the reference set, member names = `relpath`, sorted, with normalised
  headers (`mtime` = 0, uid/gid 0, empty uname/gname, mode 0644; corrected 2026-10-24 to match the Step 4 decision
  and `archive.py`: an earlier draft said "the reference event's ts", which every archive writer must not follow,
  or archives of the same reference set stop being byte-identical). zstd level 19,
  single-threaded, through `zstandard` (D6; Python 3.13 has no stdlib zstd). Deterministic by construction, but resume
  does **not** depend on byte identity (§6.1 step 3).
- `run-<full_run_id>.manifest.json` has no wall-clock fields, so the same reference set always yields the same bytes:

```jsonc
{
  "manifest_version": 1,
  "run_id": "1e4596c6-7a55-4595-9083-2091b1719a0b",
  "project_slug": "denxity",
  "worktree_root": "/home/marielle/projects/denxity/.claude/worktrees/qb-r6",
  "archive": {"name": "run-1e4596c6-….tar.zst", "sha256": "…", "size_bytes": 0, "codec": "zstd-19-st"},
  "files": [{"relpath": "outputs/validation/qb_mi_coverage_r6.json", "size_bytes": 1658975,
             "sha256": "df15d2…", "phase": "run_end", "origin_eid": "<eid>"}]
}
```

`phase` and `origin_eid` are **per file**, so a mixed `run_end` + `adopted` set (the r6/r7 case) is representable.
Before upload, bathos asserts that `files[]` equals `reference_hashes(run_id)` exactly.

### 5.3 Catalog record: `output_copy.*` events → `index.db.output_copies`

Kinds: `output_copy.recorded` (written by `cold`/`warm`, carrying the full row including `state` and `verified_at` from
its own pre-commit verify), `output_copy.verified` (written by `verify`: `state`, `verified_at`, `detail`) and
`output_copy.superseded` (§6.4). Entity: `[run_id, copy_id]`.

`copy_id = sha256(json.dumps([run_id, host, profile or "", canonical_path]))[:16]`. `host` is included, so warm copies
on two machines never collide. `tier` is an attribute, not part of identity, so one remote file is one row whichever
leg wrote it.

| column | type | note |
|---|---|---|
| `copy_id` (PK), `run_id` | str | |
| `tier` | `warm` \| `cold` \| `mirror` | attribute |
| `profile`, `host`, `path` | str | warm: `profile` = "" |
| `config_layer` | str | |
| `archive_sha256`, `manifest_sha256`, `archive_size_bytes` | str/int | null for warm |
| `state`, `verified_at`, `recorded_at`, `superseded_at` | | |

**Fold (`runlog/fold_output_copies.py`), a merge rather than "latest wins":** the base fields come from the latest
`recorded`. `state`/`verified_at` come from the latest of {`recorded`, `verified`}, ordered by `(ts, kind_rank, eid)`
with `recorded` = 0 and `verified` = 1. `uuid7` is a millisecond stamp plus random bits (`envelope.py:29-40`), so
`eid` alone does not order same-millisecond events. `superseded` sets `superseded_at`, and the row is then excluded
from "verified copy" lookups.

**Supersede and rewrite share a `copy_id` (review cycle 2, B1).** A rewrite after supersede lands at the same final
path, so it has the same `copy_id`. `superseded_at` is therefore set only when the latest of {`recorded`,
`superseded`} for that `copy_id` (same `(ts, kind_rank, eid)` order, `superseded` = 2) is the `superseded` event; a
later `recorded` clears it. The `superseded` event carries `renamed_to`, and the fold also writes a row for the
renamed-aside file (its own `copy_id`, from `renamed_to`, state `superseded`), so the old archive stays findable.

**Index wiring (all required in the same step):**
- `OUTPUT_COPIES_DDL` (and the `runs.adopted_outputs` column) in `init_index_schema`.
- An `output_copies` view in `connect_read` (`index.py:440-456`, which lists every view explicitly).
- `_OUTPUT_COPY_KINDS` dispatched in `fold_roots_into` (`ingest.py:1164-1190`). That function uses an explicit
  `if/elif` per kind family and advances the watermark past kinds it doesn't know, so an unwired kind is silently
  never folded.
- `run.outputs_adopt_retracted` added to `_RUN_KINDS` (`ingest.py:97-108`), or retract never refolds.
- A fold-parity check in `verify_runlog`.

**Existing `index.db` files (review cycle 2, F-06).** There is no `INDEX_SCHEMA_VERSION`; the earlier text was wrong.
`init_index_schema` runs inside `fold_roots_into` (`ingest.py:1112`), so a new table self-creates on the next ingest.
`connect_read` attaches `index.db` read-only, so it must not `CREATE VIEW output_copies` over a table that isn't there
yet: every reader would raise `CatalogException`. It checks `duckdb_tables()` for `idx.output_copies` and otherwise
creates the view over an empty typed `SELECT … WHERE false`. The same applies to `runs.adopted_outputs`: readers
`COALESCE` it as absent until the first ingest. A new `ALTER TABLE … ADD COLUMN` for an existing `runs` table goes in
`init_index_schema`, guarded by a column-exists check.

Legacy (non-log) catalogs: refuse with `requires_log_mode` (D7).

## 6. Mechanics

### 6.1 Cold write (per profile, cold first, then mirror)

All remote steps are `myxcel cold …` calls (D1). Each attempt mints `attempt8` (random).

1. Resolve the profile (`host`, `cold_root`, layer). Probe `myxcel cold put --help` once (form unverified, Step 5b) and refuse with
   `myxcel_capability_gap` if the subcommands are missing, as `cluster.py` does for `pull-path`. `ls` and `rm-partial`
   shipped later (myxcel #42) than the other six (#41), so they are probed separately and lazily
   (`cold_capabilities()`, Step 6b). Without them bathos keeps the Step 6 behaviour (steps 3–5 below say what changes),
   and `--clean-partials` refuses with `myxcel_capability_gap` naming the missing primitive. A myxcel that passes the
   `put` probe is never assumed to have `ls`.
2. **Take the remote lock first (review cycle 2, F-20: the resume check must not race a live writer):**
   `mkdir <dest>/.bth-lock` (atomic; it fails if the lock is held). The lock directory records the holder's local host,
   pid, attempt and start time. Every exit path, including the step 6 mismatch exit, releases it. If it is held:
   `lock_held`, naming the holder and its start time. bathos does not guess whether a remote holder is alive.
   `--break-lock` removes the lock directory only (never a partial or a final) and records the broken holder in the
   event, so an interrupted upload is recovered by one explicit rerun with `--break-lock`.
3. **Resume check.** If the final manifest and archive both exist remotely: fetch the remote manifest. It must be
   byte-identical to the manifest built locally from `reference_hashes` (deterministic, §5.2; checking the manifest
   against its own hashes would be vacuous, review cycle 2 m2). `verify-file` must pass on the remote manifest against
   the local manifest's sha256, and on the archive against the `archive.sha256` that manifest names. If both hold, emit `output_copy.recorded` for that copy, release the lock and stop (no upload). If they exist but differ:
   `archive_conflict`. If exactly one exists: `archive_conflict` with detail `half_committed`, so the owner can
   supersede (§6.4).
   **How existence is known (corrected 2026-10-24, review finding 3; implemented on PR #136, `c18d6c2b`).** When `ls`
   answers, the destination listing decides. `missing`, or neither final name listed, means absent. Exactly one
   listed is `half_committed`. A final that is listed but whose `get` or `verify-file` comes back unreadable refuses
   with `copy_unverifiable` and uploads nothing, so a transient failure on an already-committed pair never reads as
   "absent" or as `half_committed`. When there is no listing (a myxcel before #42, or an `ls` `error`, which is a
   warning rather than a refusal so a flaky `ls` alone does not block a copy), existence is inferred from `get`
   (manifest) and `verify-file` (archive). That inference cannot tell absent from unreachable. It is still safe,
   because the commit's `ln` never overwrites, so a final that was really there surfaces as `archive_conflict` at
   commit, and an unreachable host fails at `put`.
4. Upload the manifest, then the archive, each as its own call to its own name:
   `<name>.partial-<attempt8>`. A fresh name per attempt means rsync can't skip a corrupt leftover. Leftovers from
   other attempts are never reused. With `ls` (Step 6b) they are listed in the output (`partials.other_attempts`) and
   removed only by `--clean-partials`. Without `ls` they are not listed.
5. `verify-file` on both partials (cache-cold, D3). If they are not `verified`, release the lock, emit nothing and exit 1.
   Before releasing, with `rm-partial` (Step 6b), delete **this attempt's** two partials. Without it, leave them and
   name them in the error. A failed commit (step 6) cleans up the same way.
6. **Commit, manifest first and archive last:** `ln <partial> <final> && rm <partial>`. POSIX `ln` without `-f` fails
   if `final` exists, whatever the coreutils version. Do not use `mv -n`: GNU coreutils 9.4 exits 1 when the target
   exists, but older releases exit 0 without moving (A21). The archive's final name existing is the single commit
   point.
7. `verify-file` on both **final** names (post-commit, so a pre-existing different file can never be certified).
   Release the lock. Emit one `output_copy.recorded` with `state=verified` (`emit_event(..., hard_fail=True)`).

**Crash windows (corrected, review cycle 2 B2):** a kill anywhere after step 2 leaves `.bth-lock` held, so the plain
rerun reports `lock_held` naming the dead holder. The owner reruns with `--break-lock`, which then runs step 3:
- after 6a (manifest committed, archive not), step 3 sees `half_committed`;
- after 6b with no event, step 3 finds both files matching and records the copy without uploading.

Either way nothing is lost or overwritten.

**Unit of work and preemption:** each run is its own unit, with its own process and timeout. All events go through
`with unit_of_work(cat) as log_mode:` (not a separate `is_log_mode` pre-check) with `hard_fail=True`.

**Under SLURM the index is not the authority (review cycle 2, F-14).** There is no `ingest_all`; the earlier text was
wrong. The skip lives in `run_ingest` itself (`ingest.py:1011`), so any ingest under `SLURM_JOB_ID` / `BTH_NO_INGEST=1`
is a no-op by design, and cluster nodes don't refresh `index.db`. Correctness therefore never depends on reading
`output_copies`:
- The remote state checked in step 3 decides whether a run is already copied. Under SLURM, `--all-uncopied` selects
  candidates from a possibly stale index, and step 3 turns each already-copied one into a record-only pass with no
  upload.
- Progress goes to stdout as one JSON line per run (`run_id`, `leg`, `state`, `error_code`) and into the event log.
  It becomes visible in `output_copies` at the next ingest off-cluster.

### 6.2 myxcel primitives (D1), with JSON contracts pinned here so bathos tests can mock them

| Command | Output (`--json`) |
|---|---|
| `myxcel cold put <profile> <local_file> <remote_rel_name>` | `{"status":"ok"\|"error","remote_path","bytes","message"}`. Uploads to the exact name given (bathos chooses the `.partial-<attempt8>` name). |
| `myxcel cold commit <profile> <remote_partial> <remote_final>` | `{"status":"ok"\|"exists"\|"error","message"}` via `ln && rm`. |
| `myxcel cold verify-file <profile> <remote_path> <sha256> <size>` | `{"status":"verified"\|"failed"\|"unverifiable","sha256_observed","size_observed","method":"login_ssh"\|"slurm_job","message"}`. Plain sha256, not `_tree_hash`. |
| `myxcel cold get <profile> <remote_path> <local_dir>` | `{"status","local_path","bytes","message"}`. `<local_dir>` must already exist. |
| `myxcel cold lock <profile> <remote_dir>` | `{"status":"acquired"\|"held"\|"error","holder","message"}` |
| `myxcel cold unlock <profile> <remote_dir>` | `{"status":"released"\|"held"\|"error","holder","message"}`. Unlocking a directory with no lock is `error`; bathos's `--break-lock` treats that as nothing to break and proceeds to `lock`. |
| `myxcel cold ls <profile> <remote_dir>` | `{"status":"ok"\|"missing"\|"error","entries":[{"name","size_bytes","mtime"}],"message"}`. One directory, not recursive. Regular files only; the `.bth-lock` directory is reported as `{"name":".bth-lock","lock":true,"holder"}` so step 3 and `--clean-partials` can name it. `missing` when the directory does not exist. |
| `myxcel cold rm-partial <profile> <remote_path>` | `{"status":"removed"\|"absent"\|"refused"\|"error","message"}`. Refuses, before any ssh call, any path whose last segment does not match `*.partial-[0-9a-f]{8}`. Plain `rm` of one regular file, never `-r`, never a directory. |

Paths are relative to the profile's `cold_root`. myxcel refuses absolute paths and any `..` segment, and accepts only
segments of `[A-Za-z0-9._@+=,%-]` that do not start with `-`; bathos mirrors that rule so a bad slug refuses before any
myxcel call. Every command prints its JSON on stdout, and exits 0 only on its success status. That is `ok` for `put`,
`commit`, `get` and `ls`; `verified` for `verify-file`; `acquired` for `lock`; `released` for `unlock`; and `removed`
for `rm-partial` (read from myxcel `cli.py` on 2026-10-24; `ls`/`rm-partial` per #42). Every other status exits 1,
including the expected, non-error answers `ls` `missing`, `rm-partial` `absent`, `commit` `exists`, `lock` `held`
and `verify-file` `failed`/`unverifiable`. bathos therefore branches on the JSON `status`, never on the exit code.

**Reconciled with merged myxcel on 2026-10-23** (myxcel PR #41, `72f69df`; read-only audit of `cold.py` and `cli.py`). The
six primitives match on argv, keys, types and safety. Changes to this table: `unlock` returns `released` rather than
`acquired`; `message` keys on `commit`, `get`, `lock` and `unlock`; an `error` status on `lock`; `get` needs an existing
`<local_dir>`. Hard-link probe A27 is reported passed on so3 and titanix in that PR. `ls` and `rm-partial` were not in
#41. They merged separately as myxcel #42 (`689730c`, 2026-10-24) with the shapes in the table above.

**`ls` and `rm-partial` were added on 2026-10-23 after Step 6** (owner decision). Step 6 found that §6.1 step 5 ("delete this
attempt's partials"), step 4 ("leftovers from other attempts are listed") and `--clean-partials` had no primitive to stand
on. Until they land, bathos refuses `--clean-partials` with `myxcel_capability_gap` and names the partials it left in the
error (PR #135). Follow-up Step 6b wires them in: step 5 removes only this attempt's partials, step 4 lists other
attempts' partials, and `--clean-partials` removes only `*.partial-<attempt8>` files in the run's own destination
directory, only while holding the lock. bathos never removes a final name or a lock via `rm-partial`.

### 6.3 Log-mode guard

There is no legacy branch, and nothing calls `compact`. The reap bug (`compact(force_rebuild=True)` from a legacy
post-step, `reap.py:457`) is fixed on main by PR #127. Deploying it is an owner release item.

### 6.4 Supersede (rollback for live mistakes)

`bth cold <run> --supersede --reason <text>` takes the remote lock (§6.1 step 2; review cycle 2, M1: without it,
supersede races a writer's commit and leaves a mixed pair). It then renames **whichever** of the final pair exists, so
`half_committed` is covered too, to `run-<id>.superseded-<utc>.{tar.zst,manifest.json}` via `myxcel cold commit` (so
nothing is deleted). It emits `output_copy.superseded` with `renamed_to`, releases the lock, and lets a fresh cold
write proceed. Combined with `adopt --retract`, every Step 9 mistake can
be recovered without deleting anything.

## 7. Failure modes

| Failure | Detection | Behaviour |
|---|---|---|
| Live file differs from reference | `outputs hash` | `cold` refuses `outputs_drifted`. A drifted `run_end` path needs a new run. |
| Legacy run, or ≥100 MiB / directory entry with null sha | null legacy sha counts as absent (§3.3) | `adopt` covers those paths. A legacy entry with a sha is never overridden; an unresolvable legacy path refuses `legacy_path_unresolved`. A standard run (`outputs_declared`) refuses `adopt_closed`. |
| Symlink / special file / path escaping `fs_root` under a root | expansion | `symlink_in_outputs` / `special_file_in_outputs` at run end. No reference for that run. |
| Run invoked from another worktree or the main checkout | reference uses `relpath` + the event's `worktree_root` | Files are looked up under the recorded `worktree_root`. If that is gone: `worktree_missing` (warm from cold instead). |
| Interrupted upload | partial and `.bth-lock` left, no commit | Next attempt refuses `lock_held` with the holder and start time; rerun with `--break-lock` (removes the lock only), which then uses a new partial name. Leftovers are reported, removed only by `--clean-partials`. |
| Concurrent writers on one run/profile | `.bth-lock` | Second writer refuses with `lock_held` and the holder. |
| Commit target already exists | `ln` fails | `archive_conflict`, never overwritten. |
| Half-committed pair | resume step 3 | `archive_conflict detail=half_committed`, fixed with `--supersede`. |
| Cold and mirror disagree | `verify --tier outputs` | Both rows as measured, run verdict `failed`. |
| Host unreachable / Duo / sandbox | myxcel `unverifiable` | Reported as such. |
| Old myxcel without `cold` subcommands | capability probe | `myxcel_capability_gap`. |
| Tar member set ≠ manifest, or traversal / link members | pre-extract check + `filter="data"` | `warm_mismatch`, nothing written outside staging. |
| Warm target exists | pre-check | `warm_target_exists`. |
| Event write fails | `hard_fail=True` | Command exits 1 even if the remote copy succeeded. Resume step 3 records it next time. |
| Ingest suppressed (SLURM, `BTH_NO_INGEST`) | `run_ingest` no-ops by design (`ingest.py:1011`) | Index may be stale on the node; step 3 (remote state) is the authority, progress is JSON lines on stdout, `output_copies` catches up at the next off-cluster ingest. |
| Malformed config at any layer | resolver | Raise naming the file. |
| Group quota / setgid on so3 | myxcel error | Surface stderr (pre-flight optional, U). |

## 8. naurmalade alignment

naurmalade's `campaign coldify` relocates **trajectories in place on the cluster** (symlink swap). bathos tiering
**archives a run's declared outputs from wherever they were written**. These are different jobs, and both should
exist. Alignment:

- One checksum authority per copy. For naurmalade segments that is myxcel's `coldified.jsonl`. For bathos archives
  it is the manifest plus `output_copies`. bathos does not ingest `trajectory_segments`.
- Fix A14 by having naurmalade read the sha256 from `myxcel cold verify-file --json` (or from `coldified.jsonl`),
  not from stdout prose. This is a draft bug report (§12), not filed.
- A cold-relocated trajectory is a symlink into a `cold_root`, so §3.2 already refuses it as an output root. Those
  trajectories are inputs, already tiered by myxcel.
- Shared field names: `sha256`, `size_bytes`, `created_at` (not `content_sha256`). Noted as a naurmalade follow-up.

## 9. Implementation plan

Each step is one PR off `main` with narrow tests (`uv run pytest tests/<file> -k …`, threads capped, never the whole
suite locally). Steps 5a and 5b are myxcel PRs. Every later bathos step mocks them against the §6.2 contracts until
they land.

| Step | Scope | Acceptance (each must fail on a broken implementation) |
|---|---|---|
| **0. Pre-register the r6/r7 check (denxity repo, `qb-r6` worktree)** | `scripts/validation/check_output_copies_r6r7.py` + `.bth.toml` committed before it runs. Inputs: the 4 run ids, the 3 staged archives, the worktree. Result fields: `n_recorded`, `n_match_archive`, `n_match_live`, `n_files_archive`, `n_files_disk`, `sha256sums_ok`, plus a built-in **negative control**: one archive member copied with a byte flipped must count as a mismatch. | `outcome` = `all_match` only if 8/8/8, 888/888, `sha256sums_ok` and `negative_control_detected`. A version with the comparison stubbed out lands on `fail` via the control. **Done 2026-10-23:** run `64d60d3b-2028-4346-867d-72fcb442bb0a` (denxity `2c58940`), outcome `all_match`: 8/8/8, 888/888/888, no extra or irregular members, sums ok, all three controls fired. The first run `bd456925` recorded `observed` because the sidecar used `stage_name = "exploration"`, which keeps the label in `tentative_outcome` only. A sidecar that gates a decision uses `validation`. |
| **1. Config resolver** (`output_tiers/config.py`) | §5.1 incl. D9 | Unit tests: each layer wins over the next; `none` disables; malformed TOML at each layer raises naming the file; profile without `cold_root` refuses; **no layers set → `off` with the layer list** (not a grep check). |
| **2. Reference hashing** (`sidecar.py`, `runner.py`, `checker.py`, `compact.py`, `fold_runs.py`, `index` DDL, `output_tiers/reference.py`) | §3.1-3.4: a new top-level `[outputs] roots` table parsed and validated in `sidecar.py` (distinct from the existing `[observe].outputs`, `sidecar.py:515`), expansion rules, the shared uncapped `hash_file`, `relpath` + `phase` alongside the legacy keys, rebaseline tagged, the per-path fold with `runs.adopted_outputs`, `reference_hashes()` | Fixture events of each shape (run_end, phase-less, `run.imported`, rebaseline, adopted). `check_output_sha_drift` and `bth find --output-file` return the same results on a new-shape record as on a legacy one (new test, not just the old ones passing). An adopt event after a run_end event leaves `output_metadata` holding the run_end entries, and `readback` finds no candidate for an adopted-only sha. A 150 MiB file gets a sha and `check_output_sha_drift` reports it `OK`, not `UNREADABLE`. A symlink root refuses. An unknown key in `[outputs]` refuses. Prefix resolution: 0 or 2 matches refuse. |
| **3. `outputs hash` / `adopt` (+ `--retract`, `--dry-run`)** | §4 rows, plus `run.outputs_adopt_retracted` in `_RUN_KINDS` (it ships here, not in Step 6, or a retract is stored but never refolded, `ingest.py:1166`; review cycle 2 m4) | Adopt over a `run_end` path → `run_end_conflict`. Adopt on a running run refuses. Retract removes the entries from `reference_hashes`. `--dry-run` writes no event (log byte count unchanged). Adopt on an `outputs_declared` run → `adopt_closed`, even when its run-end event is missing. Adopt covers a null-sha legacy path but not one with a sha. |
| **4. Archive builder** (`output_tiers/archive.py`) | §5.2 | Same reference set built twice → identical archive and manifest sha256s. Round-trip per-file hashes match. A tampered staging file refuses. |
| **5a. myxcel `cold put/commit/verify-file/get/lock`** | §6.2 | Fake-ssh tests: `commit` onto an existing final → `exists`. Per-attempt names. A `..` path refuses. Verify three-state. `put` and `lock` into a missing parent create it. Live owner-gated probe of `ln` on so3 and titanix (A27). |
| **5b. myxcel capability probe in bathos** | §6.1 step 1. The probe form is **unverified** (myxcel's CLI is cyclopts, `myxcel/cli.py:25`; how it reports an unknown subcommand has not been probed): the probe runs `myxcel cold put --help` and requires exit 0 and `--json` in the output. It is confirmed against the real myxcel once 5a lands. | An old-myxcel stub → `myxcel_capability_gap`. The real 5a myxcel passes the probe, and the 0.x myxcel installed today fails it. |
| **6. `bth cold` + `output_copies` + `bth outputs where`** | §5.3 wiring, §6.1, §6.4 | Kill after manifest commit → plain rerun reports `lock_held`; rerun with `--break-lock` reports `half_committed`. Kill after archive commit but before the event → `--break-lock` rerun records without uploading. `half_committed` → `--supersede` → fresh write ends with the new copy `verified` and not superseded, the old pair findable as `superseded`. `--supersede` while another writer holds the lock → `lock_held`. Two concurrent writers → one gets `lock_held`. An `index.db` built before this step: `where` and every other reader work (empty `output_copies`, no `CatalogException`), and the table fills on the next ingest. A retract refolds (`reference_hashes` loses the entries). A same-millisecond recorded+verified pair folds to `verified` in 1000 shuffled orderings. `--all-uncopied` continues past a failing run and exits 1. Under `SLURM_JOB_ID`, a second `--all-uncopied` pass uploads nothing for runs the first pass copied, and every run prints one JSON progress line. A stale lock refuses with `lock_held`; `--break-lock` recovers and records the broken holder. An imported run without `--worktree-root` refuses `worktree_unknown`. |
| **7. `bth warm`** | §4 row | Archive containing a `../` member, an extra member or a symlink member → `warm_mismatch` with nothing outside staging. Existing `<dir>` refuses. Success leaves no staging directory. An empty `<dir>` created between the check and the rename → `warm_target_exists`, and the empty dir is untouched. |
| **8. `bth verify --tier outputs`** | §4 row | Unreachable host → `unverifiable`. Flipped byte → `failed`. `bth verify` (default `--tier all`) makes **no** network call (asserted with a stubbed myxcel that fails if invoked). |
| **9. r6/r7 migration (live, owner-gated)** | `--dry-run` first and owner review, then `adopt` (the cells dirs, review HTML) on the 4 runs, `cold` engaging + titanix, `warm` into scratch, `verify`. A sidecar-tracked run records the whole sequence. | All 888 files in `reference_hashes` (8 `run_end_legacy` + 880 `adopted`). Both copies `verified`. Warm-into-scratch per-file match. Manual copies and originals untouched (sha256 of the manual tars unchanged before and after). |
| **10. Docs** | `/using-bathos` section, lint for undeclared resume dirs (flags `--resume-dir`/`--out-dir` argv values not under a declared root, using the run record's argv, so it works whether or not the directory exists). | Skill names the commands, the three states and the supersede path. |

## 10. Non-goals

Tiering cluster-resident runs (A28): v1 runs `bth cold` only where the run's `worktree_root` exists.
Deleting warm files (a later `bth outputs release` gated on cold+mirror `verified`). Content-addressed dedup (myxcel
rejected it for the same low-duplication reason). Automatic tiering. Tiering naurmalade trajectories. Legacy-mode
catalogs.

## 11. Decisions (all decided 2026-10-23)

- **D1 transport owner: myxcel.** myxcel gains `cold put` / `cold verify-file --json`, and bathos shells out to them
  (§6.2). Step 5 is a myxcel PR.
- **D2 profile reading: `tomllib`** on `~/.config/myxcel/<name>.toml`. bathos does not import myxcel, since the two
  run on different Pythons.
- **D3 remote verify: ssh on the login node for archives ≤ 2 GiB, a `mit_data_transfer` job above that.** A18
  (login-node policy) stays unverified. The threshold is a config key, not a constant: the per-profile myxcel key
  `cold_verify_ssh_max_bytes` (default 2 GiB, `myxcel/config.py:23`).
  **A profile on a host without SLURM must set it high (review finding 5, 2026-10-24).** titanix has no SLURM, so at
  the default every titanix archive over 2 GiB would take the `slurm_job` path and come back `unverifiable` forever.
  `verify --strict` and any release gate on cold+mirror `verified` could then never pass. Done for titanix:
  `cold_verify_ssh_max_bytes = 1099511627776` (1 TiB) in `~/.config/myxcel/titanix.toml`, checked to load through
  `myxcel.config.load_profile`. Any new non-SLURM mirror profile needs the same line.
- **D4 titanix `cold_root`: done.** The owner added `cold_root = "/home/solab/marielle"` to
  `~/.config/myxcel/titanix.toml:40`. It parses with `tomllib`.
- **D5 drift policy: refuse.** Tiering refuses with `outputs_drifted`. **Remedy changed after review (F-04/F-16):**
  drift is measured on live files, and a drifted `run_end` path is never re-referenced by `adopt`; it needs a new
  run. `adopt` only covers paths that have no `run_end` entry.
- **D6 zstd: the `zstandard` Python dependency.**
- **D7 legacy catalogs: refuse** tiering with `requires_log_mode`. No legacy write path.
- **D8 manual r6/r7 copies: leave them as they are.** Step 9 writes bathos-format archives alongside them. The manual
  tars are never adopted, moved or deleted by bathos.

- **D9 user config file: XDG, decided 2026-10-23 (raised by review F-21).** Layer 4 is
  `${XDG_CONFIG_HOME:-~/.config}/bathos/config.toml` `[outputs]`, per the global derived-data rule. `~/.bth/config.toml`
  (`runner.py:875`) keeps its existing `[defaults]` role and is not read for `[outputs]`.

## 12. Owner items (drafted, not filed)

1. **Feature:** this spec, as an epic with steps 0-10.
2. **Bug (bathos):** `_collect_output_metadata` silently records `sha256=None` for files ≥ 100 MiB and for directories
   (`compact.py:151-189`). Callers read null as "no hash", not "unverified".
3. **Bug (bathos):** the effective `output_metadata` is the latest `run.outputs_hashed`, so a rebaseline replaces the
   run-end reference in every reader (`fold_runs.py:474-477`).
4. **Release:** PR #127 (reap log-mode fix) is on `main` but not in 0.13.0a7 or the installed `bth`. Cut a release
   and run `uv tool install` to deploy it. Until then the workaround stands: delete `index.db`, then `bth compact`.
5. **Bug (naurmalade):** `campaign/coldify.py:173` parses a sha256 from `myxcel coldify verify` stdout, which never
   prints one, so `trajectory_segments.content_sha256` is always null.
6. **Gap (myxcel):** coldify writes no `.partial`, and a failed attempt blocks its own retry
   (`coldify_remote.py:139-145`). `coldify_verify` ignores `state = "pending"`.
7. **Global rule (candidate):** "Every directory a run writes is declared under `[outputs] roots`, and outputs leave
   a machine only through `bth cold`." Needs your edit of global rules.

## 13. Adversarial review dispositions (Gemini 4 Argon, 2026-10-23)

Reviewer: `gemini-4-argon-high` via praxia `surface_dispatch` (Antigravity headless, dispatch `c0daf6d7`), against
`2b4c68c4`. Raw output: `audits/261023_output-tiering-argon-review.md`. Every code claim was re-checked by reading the
code. The reviewer **misquoted the spec in places** (it said `--to` defaults to the project root, gave exit code `2`,
named `state="unverified"`, flag-style myxcel signatures, `.npz` cells, `261022_`-prefixed paths), and its §2 matrix
mislabels rows. Those parts were discarded.

| ID | Verdict | Disposition |
|---|---|---|
| F-01 `mv -n` | **Partly**: version-dependent (A21) | §6.1 step 6-7: `ln && rm`, then verify finals after commit |
| F-02 non-reproducible resume | **Confirmed** | §5.2 deterministic manifest/tar. §6.1 step 3 (formerly 2) resumes on manifest content, not bytes |
| F-03 two-file rsync / crash window | **Confirmed** | §6.1 step 4 one file per call. Manifest-first commit. `half_committed` |
| F-04 adopt contradiction + laundering | **Confirmed** | §3.3-3.4, §4 adopt row, D5 remedy changed |
| F-05 `path`→`relpath` breaks consumers; absolute paths | **Confirmed** (A22) | §3.1 keeps legacy keys and adds `relpath`. Step 2 acceptance |
| F-06 fold dispatch + views | **Confirmed** (A20 corrected) | §5.3 index wiring |
| F-07 phase-less / `run.imported` references | **Confirmed** (A23) | §3.3 (b) `run_end_legacy`. Per-file phase in the manifest |
| F-08 `where` before its table | **Confirmed** | `where` moved to Step 6 |
| F-09 no fetch primitive | **Confirmed** | `myxcel cold get` + capability probe (§6.2, Step 5b) |
| F-10 warm staging inside target | **Partly**: "default project root" was fabricated; the in-target staging was a real defect | Sibling staging, `<dir>` must not exist |
| F-11 fold clobber + same-ms order | **Confirmed** (A24) | Merge fold with `kind_rank`. One `recorded` event carries state |
| F-12 worktree vs identity root | **Confirmed** | §3.1 `fs_root` + recorded `worktree_root` |
| F-13 `copy_id` lacks host | **Confirmed** | JSON-encoded id incl. host. Tier is an attribute |
| F-14 `hard_fail`, ingest skip | **Confirmed** (A24) | §6.1 unit-of-work paragraph |
| F-15 no `cli.py`, exit codes | **Partly**: no exit-2 requirement existed; the `cli.py` reference was wrong | §4 preamble: cyclopts wiring, branch on `error_code` |
| F-16 rebaseline untagged; history-based drift | **Confirmed**, wrong citation (`mcp.py:2171` is campaign code; the real site is `checker.py:116-167`) | §3.3-3.4 |
| F-17 no rollback | **Confirmed** | `--dry-run`, `adopt --retract`, `--supersede` (§6.4) |
| F-18 vacuous checks | **Partly**: Step 4 lint claim fabricated; Step 0/1/8 points real | Step 0 control, Step 1 unit test, Step 8 `--tier all` no network |
| F-19 symlinks, unsafe extraction | **Confirmed** | §3.2, §4 warm row, Step 7 |
| F-20 shared partial, rsync skip | **Confirmed** | Per-attempt names, remote lock |
| F-21 config split-brain | **Partly**: per-layer `cold_root` split-brain is moot (no paths stored); `~/.bth/config.toml` and the swallow pattern are real (A26) | §5.1, new D9 |
| F-22 run-id prefixes | **Confirmed** | §4 preamble |
| F-23 module vs package | **Confirmed** | `output_tiers/` package |
| F-24 `--all-uncopied` scope | **Confirmed** | §4 cold row |
| F-25 external refs unverifiable | **By design** (they were verified from their own repos) | Pinned myxcel profile keys and JSON contracts in §5.1/§6.2 |

**Process notes for praxia (draft, not filed):** (1) The dispatch admission rejected this read-only review because
of praxia's own `.praxia/dispatch/<id>/envelope.json`, which is missing from the materialization allowlist; the
retry-precondition check has the same gap (`dispatch_run.rs:393`). (2) The `agy` executor returned the review in
`run.log` `response` but `structured_output` was `{}`. (3) The MCP `surface_dispatch run` action needs the full
19-field envelope; the `EnvelopeRequest` front door described in the skill is not reachable from MCP. (4) The installed
praxia binary embeds a registry whose antigravity `power` id (`gemini-3.1-pro-high`) no longer exists in agy 1.4.6.
Fixed on praxia `origin/main` (#261) and bridged locally with `~/.praxia/model_registry.toml`.


### 13.2 Review cycle 2: defender pass on the Argon dispositions (Claude Sonnet subagent, 2026-10-23)

The pass was run in-session instead of through `surface_dispatch` while the praxia allowlist fix is pending. The
report is at `.praxia/subagent_outputs/toolu_01KA2qWpMuN6xkxESFpSj2h6.1.md` in the main checkout. Of F-01..F-25,
19 dispositions were upheld and 6 fixes were incomplete. All 6 were re-verified against code by the orchestrator
before any edit.

| Finding | Gap found | Fix |
|---|---|---|
| F-14 | `ingest_all` does not exist. The SLURM skip is inside `run_ingest` (`ingest.py:1011`), so an "explicit ingest" no-ops on a node | §6.1: remote state (step 3) is the authority, JSON-line progress, index catches up off-cluster. §7 row, Step 6 test |
| F-06 | No `INDEX_SCHEMA_VERSION` exists. A `connect_read` view over a missing table would break every reader on an old `index.db`. `run.outputs_adopt_retracted` is not in `_RUN_KINDS` | §5.3: guarded view, column-exists `ALTER`, `_RUN_KINDS` entry. Step 6 test |
| F-04 | Latest-wins fold (`fold_runs.py:474-477`) lets an adopt event drop the run-end entries. `readback.py:231-249` matches any `output_metadata` sha | §3.3: adopt events skipped for `output_metadata`, separate `runs.adopted_outputs`, per-path `reference_hashes()`. Step 2 tests |
| F-05 | "No size cap" vs the capped re-hash (`compact.py:172`) means `UNREADABLE` (`checker.py:277-285`), counted as drift (`:347`), downgrading parity (`claim.py:1304`) | §3.1: one shared uncapped `hash_file` used by the checker. Step 2 test |
| F-20 | `--break-lock` (§6.1) contradicted §7 "next attempt uses a new name". The resume check ran before the lock | §6.1: lock first (step 2), then resume (step 3); every exit releases; `--break-lock` removes the lock only. §7 row |
| F-12 | Which event's `worktree_root` applies was undefined. Imported runs default it to `catalog_dir` (`importer.py:800-801`) | §3.3: per-event root; adopt writes the run's root; imported runs need `--worktree-root` or refuse `worktree_unknown` |
| minor | `sidecar.py` has no top-level `[outputs]` parser. The `myxcel cold --help` probe form is unverified (cyclopts) | Step 2 scope adds `sidecar.py`. Step 5b marks the probe unverified |

### 13.3 Review cycle 2: challenger on §§3-9 (Claude Opus subagent, 2026-10-23)

The report is at `.praxia/subagent_outputs/toolu_01FoNyehXRwnTXiTx3ivce6C.1.md` in the main checkout. Verdict
needs_work: 2 blockers, 4 major, 5 minor. The orchestrator checked every code citation (`runner.py:1636-1650`,
`checker.py:138-163`, `workspace.py:92-98`, `envelope.py:84-88`) and every spec line before folding. All 11 stand.

| Finding | Gap | Fix |
|---|---|---|
| B1 | A rewrite after supersede reuses the `copy_id`, so `superseded_at` hides the new copy forever | §5.3: `superseded_at` only while `superseded` is the latest event; `renamed_to` row for the old pair. Step 6 test |
| B2 | Lock-first means a crash leaves the lock held, so the crash-window tests could never pass | §6.1 crash windows via `--break-lock`. Step 6 tests |
| M1 | Supersede took no lock and ignored `half_committed` | §6.4: lock, rename whichever final exists. Step 6 test |
| M2 | Null-sha legacy paths could never be adopted; no relpath rule for legacy `path` | §3.3: null counts as absent; relpath rule; `legacy_path_unresolved`. §7 row |
| M3 | A failed or refused run-end hash left `adopt` able to vouch for modified files; legacy (b) precedence undefined, could pick a rebaseline | §3.3: `outputs_declared` + `adopt_closed`, `hard_fail` run-end emit, refusal tombstone, imported-then-pre-`run.finished` precedence. Step 3 test |
| M4 | Cluster runs' files are on the cluster; profiles are laptop aliases | A28 (U); non-goal in §10 |
| m1 | Lock `mkdir` without a parent; absolute vs relative remote paths | §5.1 relative-to-`cold_root` rule, parents created. Step 5a test. Step 1 `ColdProfile.destination` gets a relative form (PR #130) |
| m2 | Resume verified the manifest against its own hashes | §6.1 step 3: byte-compare against the locally built manifest |
| m3 | `os.rename` replaces an empty `<dir>` | §4 warm: `renameat2(RENAME_NOREPLACE)`. Step 7 test |
| m4 | Retract wiring scheduled after retract ships | Moved to Step 3 |
| m5 | Hard links unprobed; `ts` resolution misstated | A27 (U) with probe and fallback; A24 corrected |

### 13.4 Decisions made during implementation (Steps 1-4)

These are binding for later steps. Each is listed in its PR.

| Step / PR | Decision |
|---|---|
| 1 / #130 | `none`/empty disables a leg at every layer. Only reached config files are parsed (§5.1). `ColdProfile.relative_destination()` is what myxcel receives; `display_destination()` is display-only. |
| 2 / #131 | A declared `[outputs]` root that does not exist becomes a `status: "missing"` entry, not a refusal. A failed hard-fail run-end emit exits 1 but still writes `run.finished`, so the run isn't left `running`. Legacy phase-less events must be strictly earlier than `run.finished`. Legacy paths outside `worktree_root` are skipped by `reference_hashes()`, and `legacy_relpath()` reports them for `legacy_path_unresolved`. |
| 3 / #133 | Commands take `--run-id <prefix>`, not a positional. A relative `--root` resolves against the run's worktree root. An abandoned run refuses `run_not_terminal`. A legacy entry whose file no longer exists shows `missing` in `outputs hash` and cannot be adopted. Retraction is `run.outputs_adopt_retracted` with `data.retracts=<eid>` and `data.reason`. New codes: `root_missing`, `adopt_event_not_found`, `run_not_found`, `run_prefix_ambiguous`. |
| 4 / #132 | Tar header `mtime = 0`. §5.2's "event ts" conflicted with "no wall-clock", and 0 satisfies both. The manifest `worktree_root` is the run's recorded root, not the directory read from, so building from a different read dir gives an identical manifest. Commit inside staging is `os.link`, manifest first. `verify_archive` never raises on a bad archive; it returns a structured result. |
| 5a | myxcel brief: `handoffs/261023_myxcel-cold-primitives-brief.md`. |
| 6b | `ls` and `rm-partial` are optional capabilities, probed separately (`cold ls --help` / `cold rm-partial --help`: exit 0 and `--json`); absent means Step 6 behaviour. Cleanup after a failed attempt runs on any non-`verified` partial verify and on any failed commit (not on a failed `put`). `--clean-partials` runs after the resume check passes, removes only `*.partial-<8hex>` files listed in the run's own directory, and refuses `myxcel_capability_gap` naming whichever primitive is missing. A removal or `ls` failure is a warning in the leg result (`partials`, `warnings`), never a failed copy. bathos refuses a non-partial last segment itself, with no myxcel call. |
