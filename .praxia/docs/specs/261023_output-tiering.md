---
title: 'Output tiering: warm, cold and mirror copies of run outputs, recorded and verified by bathos'
description: Standard for where a run's output files live (warm local path, checksummed cold archive, mirror), how bathos records and verifies each copy, and how `bth warm` restores them; reuses myxcel profiles and transport, folds in naurmalade's coldify wrapper, and notes the already-fixed reap log-mode bug
status: draft
task_id: 261023_bathos-output-tiering
date: '261023'
backlog_ids: ''
adversarial_review: ''
---
# Output tiering: warm, cold and mirror copies of run outputs

**Status:** draft, not reviewed. Supersedes the denxity-worktree draft
`denxity/.claude/worktrees/qb-r6/.praxia/docs/specs/261023_bathos-output-tiering.md` (uncommitted, treated as input,
not as a decision). **task_id:** 261023_bathos-output-tiering. Decisions are recorded in §11 (all decided).

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
| A20 | A new event kind folds without touching the run fold | **U** | `_RUN_KINDS` is an allow-list (`runlog/ingest.py:97-107`), but affected entities are derived from `entity`. A new `output_copy.*` family needs its own fold module and an `index.db` table. Not prototyped. |

## 3. The standard (what bathos enforces)

1. **A run declares its whole output set.** A sidecar may declare `[outputs] roots = [...]`, files or directories,
   relative to the project root. At run end bathos expands every root to regular files and records per-file
   `{relpath, size_bytes, sha256}`, with no size cap and no directory skip, in a **frozen** `run.outputs_hashed`
   event marked `"phase": "run_end"`. `--output-paths` keeps working and is treated as extra roots.
   `bth lint` warns when a script passes a `--resume-dir`/`--out-dir`-like argument that is not under a declared root
   (heuristic, Tier-2).
2. **The run-end hash is the reference.** Archive, warm and verify compare against the `run_end` event, never against
   the latest rebaseline. If a later rebaseline differs from `run_end`, tiering refuses with `outputs_drifted` until
   the owner chooses (D5).
3. **Three tiers per run:** `warm` (a local directory, path recorded), `cold` (one zstd tar + manifest on the cold
   profile's root) and `mirror` (the same bytes on a second profile's root). Each copy is a row in `output_copies`
   (§5.3). The run record stays the source of truth for file hashes. Copy rows say only where bytes are and when
   they last verified.
4. **Copies are written two-phase and never overwritten.** Write `<name>.partial`, verify from a fresh cache-cold
   read, then rename. A final name that already exists with a different sha256 is a hard error.
5. **bathos never deletes warm files.** Freeing space is a separate, explicit command (out of scope here, §10). It
   requires `cold` and `mirror` both `verified` within a window.
6. **No defaults for locations.** If no cold profile resolves, every tiering command refuses and names the layers
   it checked.

## 4. Command surface

All commands print JSON with `--json`, exit non-zero on anything but full success, and have MCP mirrors (`cli.py`
and `mcp.py` stay thin layers over a new `bathos/output_tiers.py`).

| Command | Does | Refuses when |
|---|---|---|
| `bth outputs hash <run_id>` | Recomputes per-file hashes for the run's roots and compares with `run_end`. Read-only: reports `match` / `drift` / `missing`. | No `run_end` event exists (legacy run). Points to `adopt`. |
| `bth outputs adopt <run_id> --root <path>...` | For runs that finished before this standard (r6/r7). Hashes the given roots **now** and emits `run.outputs_hashed` with `"phase": "adopted"`, `adopted_at` and the operator's reason. Adopted hashes are weaker than `run_end`, and every report shows the phase. | The run is not terminal. A root lies outside the project. A `run_end` already exists for that path with a different hash. |
| `bth cold <run_id> [--profile P] [--mirror M]` | Builds `run-<id8>.tar.zst` + `run-<id8>.manifest.json` in a local staging dir, checks every member against the reference hashes, then pushes to cold (and mirror) two-phase. Emits `output_copy.recorded` per copy. | No reference hashes. Any file differs from its reference. Profile has no `cold_root`. Final archive exists with a different sha256. |
| `bth warm <run_id> --to <dir>` | Fetches the archive from the first `verified` copy, checks its sha256, extracts into `<dir>/.bth-warm-<id8>.partial/`, hashes every file against the manifest **and** the reference hashes, then renames to `<dir>/<layout>` and emits `output_copy.recorded tier=warm`. | Any mismatch, extra or missing file. Target exists and is non-empty (no merge, no overwrite). |
| `bth verify --tier outputs [--run R] [--profile P]` | For each cold/mirror copy: cache-cold re-hash of the archive on its host, compared with the recorded archive sha256, then cold vs mirror agreement. Emits `output_copy.verified`. Status per copy: `verified` / `failed` / `unverifiable`. | Nothing. A non-zero exit means any copy is `failed`. `unverifiable` exits non-zero only with `--strict`. |
| `bth outputs where <run_id>` | Lists copies: tier, profile, host, path, archive sha256, last verified state and timestamp, and the config layer that chose the profile. | — |

**Three-state rule** (aligned with myxcel `VerifyResult`, `coldify.py:919-922`). `verified` means the bytes were
re-read and matched. `failed` means the bytes were read and did not match, or a manifest member does not match its
reference. `unverifiable` means the bytes could not be read (host down, path missing, ssh error, no recorded
sha256). `unverifiable` never upgrades to `verified` and never downgrades to `failed`.

## 5. Data formats

### 5.1 Config (`OutputTierConfig`)

Resolution follows isochore's order, applied to **profile names**, not paths. bathos never stores a cold path, so it
cannot disagree with myxcel:

1. explicit `--profile/--mirror`;
2. `$BTH_COLD_PROFILE` / `$BTH_MIRROR_PROFILE` (`none` or empty disables);
3. nearest `pyproject.toml` `[tool.bathos.outputs] cold_profile = "engaging"`, `mirror_profile = "titanix"`;
4. `${XDG_CONFIG_HOME:-~/.config}/bathos/config.toml` `[outputs]` with the same keys;
5. off.

The profile name resolves to `host` and `cold_root` through `myxcel.config.load_profile` if importable. Otherwise
bathos reads `~/.config/myxcel/<name>.toml` with `tomllib`, D2. A profile without `cold_root` is a refusal that names
the profile file. Each resolution returns `(value, layer_label)`, which every command shows and which goes into the
copy row. A malformed TOML at any layer raises, naming the file. Archives land at
`<cold_root>/bathos-cold/<project_slug>/<run_id>/`.

### 5.2 Archive and manifest

- `run-<id8>.tar.zst`: a POSIX tar of the reference file set, members relative to the project root (the paths
  `output_metadata` already uses), sorted, `mtime` kept, uid/gid zeroed. Compressed with zstd level 19,
  long-distance mode, through `zstandard` (new dependency, D6) or the `zstd` binary. Byte-reproducibility is **not**
  promised. Identity is the manifest, not the tar bytes.
- `run-<id8>.manifest.json`:

```jsonc
{
  "manifest_version": 1,
  "run_id": "1e4596c6-7a55-4595-9083-2091b1719a0b",
  "project_slug": "denxity",
  "reference_phase": "run_end",            // or "adopted"
  "reference_event_eid": "<eid>",
  "created_at": "2026-10-23T18:00:00Z",
  "bathos_version": "0.14.0",
  "archive": {"name": "run-1e4596c6.tar.zst", "sha256": "…", "size_bytes": 0, "codec": "zstd-19-long"},
  "files": [{"relpath": "outputs/validation/qb_mi_coverage_r6.json", "size_bytes": 1658975, "sha256": "df15d2…"}]
}
```

Before upload, bathos asserts that `files[]` equals the reference hash set exactly. Neither a superset nor a subset
is accepted.

### 5.3 Catalog record: `output_copy.*` events → `index.db.output_copies`

Event kinds: `output_copy.recorded` and `output_copy.verified`. The entity is `[run_id, copy_id]`. `copy_id` is
`sha256(run_id|tier|profile|path)[:16]`.

| column | type | note |
|---|---|---|
| `run_id`, `copy_id` | str | PK `(copy_id)` |
| `tier` | `warm` \| `cold` \| `mirror` | |
| `profile` / `host` / `path` | str | warm: host = local hostname, path = extracted dir |
| `config_layer` | str | which layer chose the profile |
| `archive_sha256`, `manifest_sha256` | str | null for warm |
| `reference_phase`, `reference_event_eid` | str | pins which hash set the copy was checked against |
| `state` | `verified` \| `failed` \| `unverifiable` | latest `output_copy.verified` |
| `verified_at`, `recorded_at` | ts | |

Folded by a new `runlog/fold_output_copies.py`, by latest `(ts, eid)` per `copy_id`, as `fold_runs` does for
`output_metadata`. Legacy (non-log) catalogs: tiering commands refuse with `requires_log_mode`. No legacy write path
is added (D7).

## 6. Mechanics

### 6.1 Cold write (per profile, sequential: cold, then mirror)

1. Resolve the profile (`host`, `cold_root`, layer).
2. `ssh host mkdir -p <dest>`, then check whether `<dest>/run-<id8>.tar.zst` exists. If it exists with the same
   sha256 as the local archive, record the copy and verify it (idempotent resume). If it exists with a different
   sha256, refuse with `archive_conflict`.
3. `rsync --partial-dir=.bth-partial <archive> <manifest> host:<dest>/run-<id8>.tar.zst.partial` (and the same for
   the manifest).
4. Remote cache-cold sha256 of the `.partial` files, using myxcel's `_cache_cold_sha256` semantics (D3 decides
   whether this runs on the login node or as a job).
5. On match, `ssh host mv -n …partial final` (`mv -n` never overwrites), then re-stat the final file.
6. Emit `output_copy.recorded` + `output_copy.verified state=verified`. On mismatch, leave the `.partial` in place,
   emit `failed`, and exit non-zero. The next attempt overwrites only the `.partial`.

Each run is one unit with its own process and timeout. A batch (`bth cold --all-uncopied`) persists each unit's
`verified` event as it completes and skips units already `verified` with matching sha256. That is the preemption
rule.

### 6.2 Transport ownership (D1)

Recommended: **myxcel gains the primitive and bathos calls it**, as `cluster.py` already shells out to myxcel
(`cluster.py:104-255`). New myxcel commands: `myxcel cold put <profile> <local_file> <cold_rel> --json` (steps 2-5)
and `myxcel cold verify-file <profile> <cold_rel> <sha256> --json` (cache-cold, three-state, plain sha256, not
`_tree_hash`). This fixes A10 (`.partial`) inside myxcel for both tools, and gives naurmalade a JSON verify output
that ends the A14 bug. Alternative: bathos does rsync/ssh itself (§11 D1).

### 6.3 Log-mode guard (from the reap bug)

All new write paths go through `unit_of_work()` + `emit_event`, with no legacy branch. The reap bug came from a
legacy post-step reachable after cutover. The rule here: tiering commands check `is_log_mode(catalog_dir)` once at
entry and never call `compact`. No further reap change is proposed. PR #127 is the fix and needs only a release and a
`uv tool install` (owner).

## 7. Failure modes

| Failure | Detection | Behaviour |
|---|---|---|
| Output changed after run end | `outputs hash` vs `run_end` | `cold` refuses with `outputs_drifted`, listing the paths. No archive. |
| Reference set incomplete (legacy run, ≥100 MiB file or directory recorded with null sha) | null sha in reference | `cold` refuses with `reference_incomplete`, pointing to `adopt`. |
| Copy interrupted mid-transfer | `.partial` left, no `verified` event | Retry overwrites the `.partial` only. |
| Final archive exists, different bytes | step 6.1.2 | `archive_conflict`. Never overwrite. The owner investigates. |
| Cold and mirror disagree | `verify --tier outputs` | Both rows stay as measured. The run-level verdict is `failed`. |
| Host unreachable / Duo / sandbox blocks ssh | ssh exit | `unverifiable`. The message names the sandbox cause when stderr matches `Operation not permitted`. |
| Warm target non-empty | pre-check | Refuse. No merge. |
| Warm extraction mismatch | per-file hash | Delete only the `.partial` dir bathos created. Report `failed`. |
| myxcel profile missing `cold_root` | config | Refuse, naming the profile file and layer. |
| Malformed bathos/pyproject config | resolver | Raise, naming the file. Never fall through to "off". |
| Login node policy (A18) | — | D3. |
| Group quota / setgid on so3 | `rsync` error | Surface the stderr. Pre-flight `df`/`lfs quota` is optional (U). |

## 8. naurmalade alignment

naurmalade's `campaign coldify` relocates **trajectories in place on the cluster** (symlink swap). bathos tiering
**archives a run's declared outputs from wherever they were written**. These are different jobs, and both should
exist. Alignment:

- One checksum authority per copy. For naurmalade segments that is myxcel's `coldified.jsonl`. For bathos archives
  it is the manifest plus `output_copies`. bathos does not ingest `trajectory_segments`.
- Fix A14 by having naurmalade read the sha256 from `myxcel cold verify-file --json` (or from `coldified.jsonl`),
  not from stdout prose. This is a draft bug report (§12), not filed.
- When a naurmalade analysis run cites a trajectory, its `[outputs] roots` should **not** include cold-relocated
  trajectory paths. Those are inputs, already tiered by myxcel. Lint: a root that is a symlink into a `cold_root`
  gets a Tier-2 warning.
- Shared field names: `sha256`, `size_bytes`, `created_at` (not `content_sha256`). Noted as a naurmalade follow-up,
  not a requirement.

## 9. Implementation plan

Each step is one PR off `main`, ending with narrow tests (`uv run pytest tests/<file> -k …`, threads capped, never
the whole suite locally).

| Step | Scope | Acceptance |
|---|---|---|
| **0. Pre-register the r6/r7 check** | `scripts/validation/check_output_copies_r6r7.py` + `.bth.toml` committed **before** it runs: re-derive A3-A5 (8 recorded hashes vs archive and live, archive `SHA256SUMS`, 888-file count). | Run record has `outcome` evaluated and `output_paths` resolving. Numbers in this spec are then cited from that run, not from the spike. |
| **1. Config resolver** | `bathos/output_tiers/config.py`: `resolve_output_profiles(explicit, start) -> (cold, mirror, layers)`, myxcel profile read. | Table tests for each layer, `none` disabling, malformed file raising with its path, a profile missing `cold_root` refusing. No default path anywhere (grep test). |
| **2. Full output hashing** | `[outputs] roots` in the sidecar schema and validator. Run-end expansion of directories, no size cap for declared roots, `phase: run_end`. Fold keeps the `run_end` event reachable (`reference_hashes(run_id)`). | A directory root yields one entry per file. A 150 MiB file gets a sha256. A rebaseline does not change `reference_hashes`. Legacy `output_metadata` consumers are unchanged (existing `checker` tests pass). |
| **3. `outputs hash` / `outputs adopt` / `outputs where`** | Read-only compare plus the adopt event. | Adopt on a non-terminal run refuses. Adopted entries carry `phase: adopted` in every report. |
| **4. Archive builder (local only)** | Tar + zstd + manifest into staging, members checked against reference. | Round-trip: build → extract → per-file hash equals reference. A tampered member makes the build refuse. |
| **5. Transport** (myxcel if D1 = myxcel) | `cold put` / `cold verify-file` with `.partial` + `mv -n`, cache-cold, JSON. | Fake-ssh tests: interrupted copy leaves only `.partial`. Existing final with a different sha → `archive_conflict`. Rerun is idempotent. |
| **6. `bth cold`** | Wires 1+4+5. `output_copy.*` events and fold. | `index.db.output_copies` rows after cold+mirror. Kill mid-mirror → rerun skips the verified cold copy. |
| **7. `bth warm`** | Fetch, verify, extract to `.partial`, rename. | Mismatch, extra or missing file each refuse and leave the target untouched. A non-empty target refuses. |
| **8. `bth verify --tier outputs`** | Three-state per copy, plus agreement. | Unreachable host → `unverifiable`. A flipped byte in a test archive → `failed`. |
| **9. r6/r7 migration (live, owner-gated)** | `adopt` the 4 runs with roots covering all 888 files, `cold` to engaging + titanix, `warm` into a scratch dir, `verify`. A sidecar-tracked run for the whole sequence. | The run record shows every one of the 888 files hashed. Both copies `verified`. Warm into scratch matches. Manual copies and worktree originals are untouched. |
| **10. Docs** | `/using-bathos` section, lint rule for undeclared resume dirs. | Skill text names the commands and the three states. |

## 10. Non-goals

Deleting warm files (a later `bth outputs release` gated on cold+mirror `verified`). Content-addressed dedup (myxcel
rejected it for the same low-duplication reason). Automatic tiering. Tiering naurmalade trajectories. Legacy-mode
catalogs.

## 11. Decisions (all decided 2026-10-23)

- **D1 transport owner: myxcel.** myxcel gains `cold put` / `cold verify-file --json`, and bathos shells out to them
  (§6.2). Step 5 is a myxcel PR.
- **D2 profile reading: `tomllib`** on `~/.config/myxcel/<name>.toml`. bathos does not import myxcel, since the two
  run on different Pythons.
- **D3 remote verify: ssh on the login node for archives ≤ 2 GiB, a `mit_data_transfer` job above that.** A18
  (login-node policy) stays unverified. The threshold is a config key, not a constant.
- **D4 titanix `cold_root`: done.** The owner added `cold_root = "/home/solab/marielle"` to
  `~/.config/myxcel/titanix.toml:40`. It parses with `tomllib`.
- **D5 drift policy: refuse.** When the latest rebaseline differs from `run_end`, tiering refuses with
  `outputs_drifted` until `bth outputs adopt` records a new reference.
- **D6 zstd: the `zstandard` Python dependency.**
- **D7 legacy catalogs: refuse** tiering with `requires_log_mode`. No legacy write path.
- **D8 manual r6/r7 copies: leave them as they are.** Step 9 writes bathos-format archives alongside them. The manual
  tars are never adopted, moved or deleted by bathos.

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
