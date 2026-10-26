---
title: Keep bth run provenance out of the working tree
description: 'Spec for debt #3049: move the per-run sidecar lock into the git object store, untrack legacy manifest/lock files, and warn when bookkeeping would dirty the tree.'
status: draft
task_id: 261026_distributional-breakdown-debt
date: '261026'
backlog_ids: ''
adversarial_review: 'cycle 1 challenger+defender done; v2 revision'
---
# Keep bth run provenance out of the working tree

**v2** (after adversarial cycle 1 and two user decisions; see §6). Debt #3049. User report
(261026): every bathos run leaves provenance files in the tree (a sidecar lock file and a
manifest update), and they block the next fixer dispatch until committed.

## 0. Problem

1. **Per-run lock in the tree.** `_write_manifest` (`runner.py:531-578`) writes
   `<sidecar_dir>/<sidecar_stem>.<run_id>.bth.lock.toml` on every run with a sidecar: a new,
   timestamped, untracked file each run. Nothing ignores it.
2. **Legacy tracked bookkeeping.** Repos that committed `.bth/refs/manifest.jsonl` before
   debt #1943 see a modification on every run; ignore rules do not apply to tracked paths.
3. **Projects without the #1943 exclude** (old pinned bathos), e.g. biosensors.
4. **Other in-tree writers:** SLURM / `BTH_FORCE_PROVENANCE_EXPORT` writes
   `outputs/provenance/<run>.bundle|.json` (`git_pin.py:266-276`, `durable.py:413-424`);
   scripts write under `outputs/<run8>/` (`runner.py:1302`).

**Why it also matters for provenance accuracy.** The live dirty check is
`git status --porcelain` (`cisternal/telemetry/git_state.py:224`, reached via
`provenance/channels.py:555,561`), and untracked files count. Run N's git state is captured at
`runner.py:895`, before run N writes anything, so the invariant is **cross-run**: whatever run
N leaves must be outside the tree or ignored before run N+1 captures. Otherwise run N+1
records `git_dirty = true`, and its WIP snapshot (`durable.py:190`, `git add -A`) sweeps the
unignored files in. On myxcel/SLURM runs the env/sidecar channel can supply git state instead
(`channels.py:546-575`); the defect is on the live path.

**Committed locks are also a deliberate evidence channel.** praxia G19
(`scripts/lint/check_run_reference.py:212-235`) reads the lock *as committed at a rev*,
beside the sidecar, and parses `run_id`, `sidecar_sha256`, `git_sha` and `sidecar_path`. G31
(`check_run_code_binding.py:8-15,254`) requires it at `--head`. praxia has 63 committed locks.
xtrax (`controller/bathos_library_wrappers.py:199`) calls `verify_run_manifest` and needs only
a readable body whose sha256 equals `run.manifest_sha256`. The design must keep both working.

### Survey (261026, read-only; two rows spot-checked)

| Pattern | Repos |
|---|---|
| `manifest.jsonl` tracked | asr, plegadx, praxia, protamer, tev_design |
| lock files tracked | adaptyv 123, protamer 68, praxia 63 (deliberate, G19), rickettsia_sca1 24, maraxiom 21, alynxr 14, ecas711 5, asr 4, aminx 3, xtrax 3, nanobody 1 |
| untracked locks piling up | biosensors 35, plus an untracked, unignored manifest |
| clean | lathex (own `.gitignore`), colliculix, denxity, mpnn_ext, prolix |

## 1. User decisions (261026)

- **Lock home:** default `lock_location = "git"` (a commit under `refs/bathos/locks/<run_id>`).
  `bth lock materialize` writes the identical file at the G19 path when the owner wants it
  committed. `"tree"` remains as an opt-out.
- **outputs/:** new `bth init` adds `outputs/` to `.gitignore`. Existing projects are
  untouched unless they set `ignore_outputs = true`, which writes to `info/exclude`.

## 2. Assumptions

| # | Assumption | Tag | Evidence |
|---|---|---|---|
| B1 | Lock consumers: `prereg.verify_run_manifest` (live caller in xtrax), praxia G19/G31 (committed file at rev), `blast_radius` (path text), `cite.py:25,36,50` and `index.py:145` (path text) | verified (challenger+defender) | as cited |
| B2 | cisternal `update_ref` verifies `ref^{commit}` and returns False for a non-commit, after writing the ref | verified | `durable.py:238-241,249` |
| B3 | Every lock-body field except `written_at` is already in the run record | verified | `runner.py:557-568` |
| B4 | `ensure_manifest_ignored` returns early when the manifest is already ignored, before writing any other line; `MANIFEST_GITIGNORE_LINES` is shared with `init.py:39` | verified | `git_pin.py:161-162` |
| B5 | Git state is captured from the script's checkout (`capture_run_git_state`, `git.py:66-78`), but `pin_run` and `ensure_manifest_ignored` use `cwd` | verified (defender N1) | `runner.py:895` vs `1156,1166` |
| B6 | Existing `bth repair` is catalog-scoped (`catalog_dir`, `tier`, `dry_run`, write token) | verified | `mcp.py:4667-4748`, `repair.py` |
| B7 | `bth sync` syncs only `runs/` and `campaigns/` | verified | `sync.py:87-100` |
| B8 | A ref to a commit whose parent is `git_hash` survives `git gc --prune=now` and is fetchable with an explicit refspec | **unverified**, standard git semantics; T0 probes |
| B9 | praxia's dispatch dirty gate counts untracked files | **unverified** (user report); the design does not depend on it |

## 3. Design

### P0. Repo selection

All provenance git operations (`pin_run`, the lock writer, ignore checks) use the **script's
checkout** root (`git.toplevel` from `capture_run_git_state`), never `cwd` (B5). With no repo,
an empty git hash, or the literal `unknown`, the lock goes to the P1 fallback.

### P1. Lock as a per-run commit ref (`lock_location = "git"`, default)

`_write_manifest` gains a `repo_root` parameter and builds the same TOML body. Then:

1. With a temporary `GIT_INDEX_FILE`: `read-tree <git_hash>`; `hash-object -w` the body;
   `update-index --add --cacheinfo 100644,<blob>,<G19 lock relpath>`; `write-tree`;
   `commit-tree -p <git_hash>` with the bathos identity.
2. `update_ref("refs/bathos/locks/<run_id>", commit)` via cisternal, unmodified (B2 satisfied).
3. Read back with `git show <ref>:<relpath>` and compare sha256 to the body. A mismatch is a
   write failure.
4. `run.manifest_path = "git:<abs git-common-dir>#refs/bathos/locks/<run_id>:<relpath>"`. The
   URI is self-contained (no cwd needed); `manifest_sha256` is unchanged.

The G19 relpath is the same name the tree mode uses, `<sidecar_dir>/<stem>.<run_id>.bth.lock.toml`,
so a materialized copy is byte-identical and lands where G19 looks.

**Fallback** (no repo, or the ref write fails): write
`<lock_dir>/<project_slug>/<run_id>.bth.lock.toml` atomically, where `lock_dir` resolves per the
derived-data rule: explicit arg > `BTH_LOCK_DIR` (`none`/empty disables, so the failure becomes
a warning, or fatal under `--agent-mode autonomous`) > `[tool.bathos] lock_dir` in the nearest
`pyproject.toml` > `${XDG_CONFIG_HOME:-~/.config}/bathos/config.toml` > `<catalog_dir>/locks`.
The deciding layer goes into the `run.lock_written` event. A malformed config fails loudly.
Never fall back into the sidecar directory.

**`lock_location = "tree"`** (opt-out): today's behaviour, unchanged.

`lock_location` resolves through the same chain (arg > `BTH_LOCK_LOCATION` > `[tool.bathos]` >
user config > `"git"`).

### P2. Bookkeeping ignores (per path)

Replace the early-returning `ensure_manifest_ignored` with `ensure_bookkeeping_ignored(repo_root)`.
For each of `/.bth/refs/manifest.jsonl`, `/.bth/refs/.manifest.lock` and `/outputs/provenance/`, it runs
`git check-ignore -q --no-index` on a representative path and appends **only the missing**
lines to `info/exclude`. With `ignore_outputs = true` it also covers `/outputs/`. Lock files
are deliberately **not** ignored: a materialized lock must be committable without `add -f`.
`bth init` adds `outputs/` to `_GITIGNORE_ENTRIES` for new projects (user decision).

### P3. `bth lock` command group (separate from catalog `repair`)

All subcommands take `--project-root` (default: the git toplevel of cwd), are dry-run by
default (`--apply` to act), refuse a dirty index when they would stage, and are idempotent.

- `bth lock materialize --run-id X [--apply]`: writes the ref's blob to its relpath. The owner
  commits it; G19/G31 then pass unchanged.
- `bth lock relocate [--apply]`: for each in-tree lock whose sha256 matches a catalog run's
  `manifest_sha256`, ensures `refs/bathos/locks/<run_id>` exists with that exact blob at that
  relpath, then lists the file as safe to delete. It does not delete.
- `bth lock untrack [--apply]`: `git rm --cached` for tracked `manifest.jsonl` / `.manifest.lock`
  only. Locks are excluded: in praxia they are intended evidence. The result is a staged
  deletion for the owner to commit.
- `bth lock push|fetch [--remote origin]`: explicit refspec `refs/bathos/locks/*`, so locks can
  travel to clones (B8).

New error codes: `LOCK_REF_WRITE_FAILED`, `LOCK_NOT_FOUND`, `INDEX_DIRTY`, each with a
resolution hint. `MANIFEST_WRITE_FAILED`'s hint stops saying "script directory".

`bth run` prints one stderr warning per process when it finds tracked `manifest.jsonl`
(naming `bth lock untrack`) or untracked locks in the tree (naming `bth lock relocate`).

### P4. Verification and text consumers

`verify_run_manifest(run)`:
1. `git:` URI: `git --git-dir <common-dir> show <ref>:<relpath>`, then hash-compare.
2. Plain path that exists: as today.
3. Plain path that is missing: derive `<common-dir>` from the nearest existing ancestor of
   the path (`rev-parse --git-common-dir`), then try `refs/bathos/locks/<run.id>:<relpath of
   the old path>`. This makes records relocated by `bth lock relocate` verify after their
   files are deleted.

`blast_radius` "manifest text", `cite.py` and `index.py` go through one
`read_lock_text(manifest_path)` resolver. Tests cover the `git:` form so no match source is
silently dropped.

### P5. Honest scope statement

After this spec, `bth run` writes into the working tree only: script outputs under
`outputs/<run8>/` (ignored for new projects, opt-in for old), the ignored bookkeeping above,
and `.bth/log/` (ignored by init). Cross-host: locks no longer travel with commits by default.
`bth lock push/fetch` and `materialize` restore that when wanted; the default trades
portability for a clean tree, and the CHANGELOG says so.

## 4. Fixer tasks

| Task | Scope | Tests |
|---|---|---|
| T0 | Probe B8: commit-ref lock survives `gc --prune=now`; fetch with explicit refspec; `commit-tree` with a temp index in a linked worktree | spike |
| T1 | P0 repo selection; `lock_location`/`lock_dir` resolution with layer reporting | `tests/test_lock_config.py`: each layer, `none` disables, malformed file fails |
| T2 | P1 commit-ref writer + read-back + fallback; tree opt-out | `tests/test_lock_out_of_tree.py`: no lock in tree after `bth run`; ref body hash == `manifest_sha256`; G19 relpath; non-git and `unknown` hash → fallback; `"tree"` unchanged |
| T3 | P2 per-path ignores; `bth init` outputs entry; `ignore_outputs` | extend git_pin/init tests: each missing line added once; early-return bug covered |
| T4 | P4 verification (three forms) + `read_lock_text` used by blast_radius/cite/index | `tests/test_verify_run_manifest.py` |
| T5 | P3 `bth lock` group + MCP mirrors + error codes + run-time warnings | `tests/test_lock_cli.py`: dry-run default, dirty-index refusal, idempotence, materialize byte-identical to what G19 parses |
| T6 | Cross-run regression: committed sidecar + `.bth.toml`, script that writes nothing, two `bth run`s → second `git_dirty = false`; repeat with a script writing to `outputs/` under `ignore_outputs = true` | `tests/test_cross_run_clean.py` |
| T7 | CHANGELOG (portability trade-off), using-bathos note, praxia handoff note (materialize before commit) | — |

Narrow pytest per task locally; full suite on CI.

## 5. Risks

| Risk | Mitigation |
|---|---|
| Refs are not pushed by default; clones lose lock evidence | `bth lock push/fetch`; `materialize` for committed evidence; CHANGELOG |
| Ref deletion has no reflog | Same as existing run refs; `materialize` gives a committed copy where it matters |
| A temp-index `commit-tree` racing in SLURM arrays | Per-run temp index and per-run ref: no shared state; T0 probes worktrees |
| praxia forgets to materialize and G19 fails | G19's own LOCK_ABSENT error; the handoff note; praxia can set `lock_location = "tree"` |
| Old bathos on other hosts keeps writing in-tree locks | `bth run` warns, naming `bth lock relocate` (no version claim: the lock carries no version) |

## 6. Adversarial review log

**Cycle 1** (Sonnet challenger then defender; `.praxia/subagent_outputs/`
`toolu_01BMJpX3LrX2SwUJeFuM3Ukj.1.md`, `toolu_01PubjhsBP4eKPgaX9Hzu86e.1.md`). 12 findings (4
blocking, 4 major, 4 minor); 7 conceded, 1 partial (C6), 4 minors accepted. v2 changes:
- C1 committed locks are evidence (praxia G19/G31, xtrax) → `lock_location` with `materialize`; untrack excludes locks; user decision.
- C2 blob ref fails cisternal verify → per-run commit ref.
- C3 relocated records unverifiable → three-form verification.
- C4 early return; ignoring locks breaks materialize → per-path ignores, locks not ignored.
- C5 outputs/provenance and outputs/ → ignored; honest scope statement; user decision.
- C6 wrong file / channel nuance → evidence corrected; invariant stated as cross-run.
- C7 repair shape → separate `bth lock` group with its own conventions.
- C8 portability → push/fetch, documented trade-off.
- C9-C12 → repo_root param; URI resolver; version-skew claim dropped; T0 trimmed.
- Defender N1-N5 → P0 script-checkout repo; lock_dir config chain; unknown-hash fallback; committed fixtures; text consumers via resolver.
