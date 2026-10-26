---
title: Keep bth run provenance out of the working tree
description: 'Spec for debt #3049: move the per-run sidecar lock into the git object store, untrack legacy manifest/lock files, and warn when bookkeeping would dirty the tree.'
status: draft
task_id: 261026_distributional-breakdown-debt
date: '261026'
backlog_ids: ''
adversarial_review: ''
---
# Keep bth run provenance out of the working tree

Debt #3049. User report (261026): every bathos run leaves provenance files in the tree (a
sidecar lock file and a manifest update), and they block the next fixer dispatch until they
are committed.

## 0. Problem

Three causes, each confirmed:

1. **The per-run lock file lands in the tree, unignored.** `_write_manifest`
   (`src/bathos/runner.py:531-578`) writes `<sidecar_dir>/<sidecar_stem>.<run_id>.bth.lock.toml`
   on every run with a sidecar. The name is unique per run and the body carries `written_at`,
   so it is always a new untracked file. `bth init` (`init.py:39`, `_GITIGNORE_ENTRIES`) and
   `ensure_manifest_ignored` (`git_pin.py:128-188`) ignore only the refs manifest.
2. **Legacy tracked files cannot be ignored.** Repos that committed `.bth/refs/manifest.jsonl`
   or lock files before debt #1943 see a modification or new file on every run; ignore rules
   do not apply to tracked paths.
3. **Some projects never received the #1943 exclude** (old pinned bathos, e.g. tev_design pins
   0.4.1; biosensors has an untracked, unignored `manifest.jsonl`).

**A consequence beyond agility:** cisternal measures dirtiness as "`git status --porcelain`
printed anything" (`cisternal/provenance/capture.py:199-200`), untracked files included. The
lock is written *after* `pin_run` (`runner.py:1166` pin, `runner.py:1278-1290` lock), so run N's
lock makes run N+1 record `git_dirty = true` and take a WIP snapshot, on an otherwise clean
tree. The #1943 docstring (`runner.py:1150-1155`) names this failure mode for the manifest;
the lock file reintroduces it.

### Survey (261026, read-only; two rows spot-checked by hand)

| Pattern | Repos |
|---|---|
| `manifest.jsonl` tracked | asr, plegadx, praxia (verified), protamer, tev_design |
| lock files tracked | adaptyv 123, protamer 68, praxia 63, rickettsia_sca1 24, maraxiom 21, alynxr 14, ecas711 5, asr 4, aminx 3, xtrax 3, nanobody 1 |
| untracked lock files piling up | biosensors 35 (verified), plus manifest untracked and unignored |
| clean | lathex (its own `.gitignore` covers `*.bth.lock.toml`, `outputs/`, manifest), colliculix, denxity, mpnn_ext, prolix |

## 1. Assumptions

| # | Assumption | Tag | Evidence |
|---|---|---|---|
| B1 | The lock's only reader is `prereg.verify_run_manifest`, and that function has no live callers | verified (rg over `src/`: only `stats_gates.py:49` docstring mentions it); **unverified** for sibling repos (maraxiom, praxia) | `prereg.py:439-470` |
| B2 | `blast_radius` reads `run.manifest_path` as text for anchor matching | verified | `blast_radius.py:636-659` |
| B3 | Every field in the lock body is already in the run record (`sidecar_sha256`, `sidecar_path`, `git_hash`, `script_sha256`, `run_id`) except `written_at` | verified | `runner.py:557-568`, `schema.py:47` |
| B4 | bathos already writes refs under `refs/bathos/runs/<id>` and `refs/bathos/wip/<id>` through cisternal `update_ref` | verified | `git_pin.py:252-290`, `cisternal/provenance/durable.py:373-386` |
| B5 | A git ref may point at a blob, and `git cat-file blob <ref>` reads it back | verified (git semantics; tags-to-blobs are standard) — **unverified** that cisternal's `update_ref` accepts a non-commit sha |
| B6 | `ensure_manifest_ignored` writes `git rev-parse --git-path info/exclude`, which linked worktrees share | verified | `git_pin.py:128-188` |
| B7 | The run's `git_dirty` comes from cisternal `capture.py` porcelain on the live path | verified for `capture.py:199`; **unverified** that `bth run` takes the live path rather than the env/sidecar channel (`channels.py:501-511`) in every mode |
| B8 | praxia's dispatch refuses a dirty tree and counts untracked files | **unverified** (user report); the fix does not depend on it |

## 2. Design

### P1. The lock record moves into the git object store

`_write_manifest` stops writing into the working tree. It builds the same TOML body, then:

- inside a git repo: `git hash-object -w --stdin` and `update-ref refs/bathos/locks/<run_id> <blob>`.
  `run.manifest_path = "git:refs/bathos/locks/<run_id>"`; `run.manifest_sha256` stays the
  sha256 of the body, as now.
- outside a git repo, or if the ref write fails: write to the catalog side,
  `<catalog_dir>/locks/<project_slug>/<run_id>.bth.lock.toml` (outside the tree; atomic
  write-then-rename like cool fragments). `manifest_path` is that absolute path.
- never fall back to the sidecar directory. In `--agent-mode autonomous` a failure of both
  is fatal, as today (`runner.py:574-575`).

`verify_run_manifest` accepts both forms: `git:<ref>` reads via `git cat-file blob`, and
anything else is read as a path. Old records whose lock still sits beside the sidecar keep
verifying unchanged.

Rationale: the lock is tamper evidence keyed to the run's commit. `.git` is where the run's
other pins already live (B4), and it is shared by every worktree, invisible to `git status`,
and not counted by the porcelain dirty check.

### P2. Ignore safety net

Add `*.bth.lock.toml` (unanchored) to the patterns `ensure_manifest_ignored` puts in
`info/exclude`, and to `_GITIGNORE_ENTRIES` for `bth init`. This hides legacy untracked lock
files and those written by older bathos versions on other hosts. It never edits a tracked
`.gitignore` during `bth run` (the #1943 rule).

### P3. Detect and repair tracked bookkeeping

- `bth run` checks once per process (`git ls-files -- .bth/refs/manifest.jsonl
  .bth/refs/.manifest.lock '*.bth.lock.toml'`). If any are tracked, it prints one stderr
  warning naming the count and the exact repair command, and emits
  `run.provenance_tracked_warning`. It never changes the index itself.
- New `bth repair --untrack-provenance` (dry run by default, `--apply` to act) runs
  `git rm --cached` on those paths, leaves the files on disk, and prints the commit the owner
  should make. It is an owner action: the result is a commit in the user's history.
- `bth repair --relocate-locks` (dry run by default) imports each in-tree lock file whose
  sha256 matches a catalog run's `manifest_sha256` into `refs/bathos/locks/<run_id>`. It then
  lists the files as safe to delete. It does not delete; deletion stays with the owner. A
  file with no matching run, or a mismatched hash, is reported and left alone.

### P4. Order of writes

With P1 the lock no longer touches the tree, so run N cannot dirty run N+1. Add a
regression test: two consecutive `bth run`s on a clean tree record `git_dirty = false` for
the second run.

### P5. Out of scope

- `outputs/<run8>/` (`runner.py:1302`). An empty directory is invisible to git; files the
  script writes there are the experiment's outputs, which the project chooses whether to track.
  `bth init` could offer `outputs/` as an ignore entry later; not here.
- praxia's dispatch gate. Fix the source, not the gate.
- Pushing `refs/bathos/*` to remotes. Same as the existing run refs.

## 3. Fixer tasks

| Task | Scope | Tests |
|---|---|---|
| T0 | Probe B5 (cisternal `update_ref` with a blob sha), B7, and B1 in maraxiom/praxia (`rg verify_run_manifest manifest_path`) | spike |
| T1 | P1 writer: git blob + ref, catalog fallback, no tree write; `verify_run_manifest` reads both forms | `tests/test_lock_out_of_tree.py`: no `*.bth.lock.toml` in tree after `bth run`; ref exists and its body hashes to `manifest_sha256`; non-git cwd falls back to catalog; legacy path record still verifies |
| T2 | P4 regression: two runs on a clean tree, second `git_dirty = false` | in the same file |
| T3 | P2 ignore patterns in `info/exclude` and `bth init` | extend `tests/test_git_pin*.py` / init tests |
| T4 | P3 tracked warning + `repair --untrack-provenance` + `--relocate-locks` (dry run default) | `tests/test_repair_provenance.py` |
| T5 | CHANGELOG; using-bathos note ("bathos writes no files into your tree on `bth run` except `outputs/<run>/`") | — |

Narrow pytest per task locally; full suite on CI.

## 4. Risks

| Risk | Mitigation |
|---|---|
| A ref to a blob is unusual; `git gc` prunes unreachable objects | A ref makes it reachable; T0 probes cisternal's helper; test `git gc --prune=now` keeps it |
| Clones and other hosts lack `refs/bathos/locks/*` | Same property as existing run refs; `verify_run_manifest` returns False (inadmissible, loud), unchanged semantics |
| SLURM jobs on a worktree on scratch | Ref write goes to the shared git dir; if it fails, the catalog fallback applies, never the tree |
| Old bathos versions on other hosts keep writing lock files | P2 hides them; P3 warning names the version skew when it sees a lock newer than this release |

## 5. Adversarial review log

(filled by the challenger → defender cycles)
