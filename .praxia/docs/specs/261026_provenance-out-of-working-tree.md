---
title: Keep bth run provenance out of the working tree
description: 'Spec for debt #3049: move the per-run sidecar lock into the git object store, untrack legacy manifest/lock files, and warn when bookkeeping would dirty the tree.'
status: draft
task_id: 261026_distributional-breakdown-debt
date: '261026'
backlog_ids: ''
adversarial_review: 'cycles 1-2 challenger+defender done; v3 revision'
---
# Keep bth run provenance out of the working tree

**v3** (after adversarial cycles 1-2 and two user decisions; see §6). Debt #3049. User report
(261026): every bathos run leaves provenance files in the tree (a sidecar lock file and a
manifest update), and they block the next fixer dispatch until committed.

## 0. Problem

1. **Per-run lock in the tree.** `_write_manifest` (`runner.py:531-578`) writes
   `<sidecar_dir>/<sidecar_stem>.<run_id>.bth.lock.toml` on every run with a sidecar: a new,
   timestamped, untracked file each run. Nothing ignores it.
2. **Legacy tracked bookkeeping.** Repos that committed `.bth/refs/manifest.jsonl` before
   debt #1943 see a modification on every run; ignore rules do not apply to tracked paths.
3. **Projects without the #1943 exclude** (old pinned bathos), e.g. biosensors. `.bth/log/`
   is ignored only by `bth init` (`init.py:229`).
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

**Committed bookkeeping is also a deliberate evidence channel.**
- praxia G19 (`scripts/lint/check_run_reference.py:212-235`) reads the lock *as committed at a
  rev*, beside the sidecar, and parses `run_id`, `sidecar_sha256`, `git_sha`, `sidecar_path`.
- praxia G31 (`check_run_code_binding.py:8-15,254`) requires the lock at `--head`, **and reads
  `<root>/.bth/refs/manifest.jsonl` from disk in every worktree root** (`:65` hardcodes
  `MANIFEST_REL`, `:181-189,293-300`) for clean-run proof.
- xtrax (`controller/bathos_library_wrappers.py:199`) calls `verify_run_manifest` and needs only
  a readable body whose sha256 equals `run.manifest_sha256`.

The design must keep all three working.

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
| B1 | Consumers: `prereg.verify_run_manifest` (live caller in xtrax); praxia G19 (committed lock at rev); praxia G31 (committed lock at `--head` **and** on-disk `manifest.jsonl` per worktree root); `blast_radius` (path text); `cite.py:25,36,50` and `index.py:145` (path text) | verified | as cited; G31 `:65,181-189` read by orchestrator |
| B2 | cisternal `update_ref` verifies `ref^{commit}` and returns False for a non-commit, after writing the ref | verified | `durable.py:238-241,249` |
| B3 | Every lock-body field except `written_at` is already in the run record | verified | `runner.py:557-568` |
| B4 | `ensure_manifest_ignored` returns early when the manifest is already ignored, before writing any other line; `MANIFEST_GITIGNORE_LINES` is shared with `init.py:39` | verified | `git_pin.py:161-162` |
| B5 | Git state is captured from the script's checkout (`capture_run_git_state`, `git.py:66-78`), but `pin_run` and `ensure_manifest_ignored` use `cwd` | verified | `runner.py:895` vs `1156,1166` |
| B6 | Existing `bth repair` is catalog-scoped (`catalog_dir`, `tier`, `dry_run`, write token) | verified | `mcp.py:4667-4748`, `repair.py` |
| B7 | `bth sync` syncs only `runs/` and `campaigns/` | verified | `sync.py:87-100` |
| B8 | A ref to a commit whose parent is `git_hash` survives `git gc --prune=now` and is fetchable with an explicit refspec | **unverified**, standard git semantics; T0 probes |
| B9 | praxia's dispatch dirty gate counts untracked files | **unverified** (user report); the design does not depend on it |
| B10 | `resolve_flag` (`config.py:60-85`) reads env then `.bth.toml [section] key` and **swallows** a malformed config to False | verified (orchestrator read) | so `lock_location` needs its own resolver (P1) |
| B11 | MCP error envelopes use `BathosErrorCode` (`errors.py:15`) with `RESOLUTION_HINTS` (`errors.py:70`), completeness enforced by `test_errors.py:67-70`; `GateErrorCode` (`prereg.py:28`) is pinned to an exact set by `test_gate_error_taxonomy.py:15-33` | verified | as cited |
| B12 | Whether `bth run` refuses when `.bth/log/` is unignored | **unverified** (`errors.py:64` shows a gate exists); P2 ignores it anyway, so the design does not depend on it |

## 3. Design

### P0. Repo selection

The **lock writer** and the **ignore check** use the script's checkout root (`git.toplevel`
from `capture_run_git_state`), never `cwd` (B5). With no repo, an empty git hash, or the literal
`unknown`, the lock goes to the P1 fallback.

`pin_run` and the manifest stay on `cwd` (unchanged), so `bth provenance show/diff`
(`cli_cyclopts.py:405`) keeps finding entries. When the script root and `cwd` root differ,
`ensure_bookkeeping_ignored` runs on both.

### P1. Lock as a per-run commit ref (`lock_location = "git"`, default)

`_write_manifest` gains a `repo_root` parameter and builds the same TOML body. Then, under
`clean_git_env` (`cisternal capture.py:72`):

1. If `git cat-file -e <git_hash>^{commit}` succeeds, the parent is `git_hash`. Otherwise
   (shallow clone, hash absent) the commit is parentless and the `run.lock_written` event says
   so. Nothing today checks descent from `git_hash`.
2. With a temporary per-run `GIT_INDEX_FILE`: `read-tree <git_hash>` (empty tree when
   parentless); `hash-object -w` the body;
   `update-index --add --cacheinfo 100644,<blob>,<G19 lock relpath>`; `write-tree`;
   `commit-tree [-p <git_hash>]` with the bathos identity. This commit is a **lock carrier**:
   HEAD plus the lock, not a WIP snapshot.
3. `update_ref("refs/bathos/locks/<run_id>", commit)` via cisternal, unmodified (B2 satisfied).
4. Read back with `git show <ref>:<relpath>` and compare sha256 to the body. A mismatch is a
   write failure.
5. `run.manifest_path = "git:<common-dir>#refs/bathos/locks/<run_id>:<relpath>"` where
   `<common-dir>` comes from `rev-parse --path-format=absolute --git-common-dir`.
   `manifest_sha256` is unchanged.

**URI grammar.** Parse by the fixed marker `#refs/bathos/locks/<uuid>:`: everything before it,
minus the `git:` prefix, is the common dir (it may contain `#` or `:`); everything after is the
relpath. A test covers a common dir containing `#`.

The G19 relpath is the same name tree mode uses, `<sidecar_dir>/<stem>.<run_id>.bth.lock.toml`,
so a materialized copy is byte-identical and lands where G19 looks.

**Failure behaviour.** If the ref write or read-back fails, `bth run` writes the fallback and
records `ref_error` in `run.lock_written`. It does not fail the run, except under
`--agent-mode autonomous` with no fallback available, where it raises `RuntimeError` as today
(`runner.py:576`). `LOCK_REF_WRITE_FAILED` appears only in `bth lock *` envelopes.

**Fallback** (no repo, or the ref write fails): write
`<lock_dir>/<project_slug>/<run_id>.bth.lock.toml` atomically. Never fall back into the sidecar
directory.

**`lock_location = "tree"`** (opt-out): today's behaviour, unchanged.

**Configuration.** A dedicated resolver (not `resolve_flag`, which swallows malformed files,
B10) resolves `lock_location`, `lock_dir` and `ignore_outputs` in this order:

1. explicit argument;
2. env `BTH_LOCK_LOCATION` / `BTH_LOCK_DIR` / `BTH_IGNORE_OUTPUTS`. For `BTH_LOCK_DIR`,
   `none`/empty disables the fallback, so a ref failure becomes a warning, or fatal under
   autonomous mode. `BTH_LOCK_LOCATION` accepts only `git`/`tree`; anything else fails loudly.
3. `.bth.toml [provenance]` (the project config SLURM jobs read, `config.py:71-74`;
   `_bth_env.sh` exports none of these, so the cluster relies on this layer);
4. `[tool.bathos.provenance]` in the nearest `pyproject.toml`;
5. `~/.bth/config.toml [provenance]`, bathos's existing user config. This replaces the XDG
   home to avoid a second user-config file, a documented deviation from the global
   derived-data rule;
6. defaults: `"git"`, `<catalog_dir>/locks`, `false`.

The deciding layer is recorded in `run.lock_written`. A malformed file at any layer raises.

### P2. Bookkeeping ignores (per path)

Replace the early-returning `ensure_manifest_ignored` with `ensure_bookkeeping_ignored(repo_root)`.
It covers each of the following:
- `/.bth/refs/manifest.jsonl`;
- `/.bth/refs/.manifest.lock`;
- `/.bth/log/`;
- `/outputs/provenance/`.

For each, it runs `git check-ignore -q --no-index` on a representative path and appends only
the missing lines to `info/exclude`. With `ignore_outputs = true` it also covers `/outputs/`.

Lock files are deliberately **not** ignored, so a materialized lock can be committed without
`add -f`. A *tracked* path is not made clean by any of this; P3 `untrack` covers that.

`bth init` adds `outputs/` to `_GITIGNORE_ENTRIES` for new projects (user decision).

### P3. `bth lock` command group (separate from catalog `repair`)

**CLI conventions.** `--project-root` defaults to the git toplevel of cwd. Commands are
dry-run by default, with `--apply` to act. They refuse a dirty index when they would stage, and
they are idempotent.

**MCP mirror.** `project_root` is required (no cwd default over MCP), with `apply: bool = False`.
It is registered under bathos and bathos-cli with `cli_group="lock"`. A write token is required
for push, fetch, materialize, and for untrack and relocate under `apply`.

- `bth lock materialize --run-id X [--apply]`: writes the ref's blob to its relpath. The owner
  commits it; G19/G31 then pass unchanged.
- `bth lock relocate [--apply]`: classifies each in-tree lock with
  `git ls-files --error-unmatch`.
  - **Tracked** locks are reported as "keep (committed evidence)" and are never deletion
    candidates. This covers praxia's 63, adaptyv's 123 and protamer's 68.
  - **Untracked** locks whose sha256 matches a catalog run's `manifest_sha256` are reported as
    "safe to delete".
  - For both kinds it ensures `refs/bathos/locks/<run_id>` holds that exact blob at that relpath.
  - It never deletes.
- `bth lock untrack [--apply]`: `git rm --cached` for tracked `manifest.jsonl` /
  `.manifest.lock` only. The on-disk file is never touched, and locks are excluded. The
  result is a staged deletion for the owner to commit.
  - Dry-run and apply both print the consequences:
    - **G31:** after the deletion is committed, fresh checkouts and worktrees start with an
      empty manifest. Runs whose manifest lines exist only in older checkouts then fail
      clean-run proof (`SNAPSHOT_UNPROVEN`).
    - **Merging:** other checkouts that modified the file hit a modify/delete conflict on
      merge. Recipe: `cp .bth/refs/manifest.jsonl /tmp/keep && git checkout -- .bth/refs/manifest.jsonl && git merge … && mv /tmp/keep .bth/refs/manifest.jsonl`.
      Whether `skip-worktree` is a cleaner alternative is unverified; T0 probes it.
- `bth lock push|fetch [--remote origin]`: explicit refspec `refs/bathos/locks/*`, so locks can
  travel to clones (B8).

**Error codes** (B11): `LOCK_REF_WRITE_FAILED`, `LOCK_NOT_FOUND`, `INDEX_DIRTY` are added to
`BathosErrorCode` with `RESOLUTION_HINTS` entries. `GateErrorCode` is not touched. Any new
raised exception class gets an `EXCEPTION_TO_CODE` entry (`test_errors.py:56-64` AST scan). The
`MANIFEST_WRITE_FAILED` hints at `errors.py:76` and `prereg.py:87` stop saying "script
directory". The errors.py module docstring count is updated.

`bth run` prints one stderr warning per process when it finds tracked `manifest.jsonl`
(naming `bth lock untrack` and its G31 caveat) or untracked locks in the tree (naming
`bth lock relocate`).

### P4. Verification and text consumers

`verify_run_manifest(run)`:
1. `git:` URI: `git --git-dir <common-dir> show <ref>:<relpath>`, then hash-compare. If the
   common dir does not exist on this host (a record synced from another host), fall through to
   step 3, using the repo of the run's script path.
2. Plain path that exists: as today.
3. Plain path that is missing: find the repo from the nearest existing ancestor of the path
   (`rev-parse --path-format=absolute --git-common-dir`). Then try
   `refs/bathos/locks/<run.id>:<relpath of the old path>`. This makes records relocated by
   `bth lock relocate` verify after their files are deleted. **Limit:** it works only where
   that ancestor lies in the owning repo. A worktree outside the repo whose directory was
   removed is unverifiable and is reported as such; a test covers it.

`blast_radius` "manifest text", `cite.py` and `index.py` go through one
`read_lock_text(manifest_path)` resolver. Tests cover the `git:` form so no match source is
silently dropped.

### P5. Honest scope statement (goes into the CHANGELOG)

After this spec, `bth run` writes into the working tree only:
- script outputs under `outputs/<run8>/`, ignored for new projects and opt-in for old ones;
- the ignored bookkeeping listed in P2.

Locks no longer travel with commits by default. `bth lock push/fetch` and `materialize` restore
that when wanted. The default trades portability for a clean tree.

**Per-repo outcome on upgrade:**

| Repos | Outcome |
|---|---|
| praxia | New lock files stop appearing. The **tracked manifest keeps dirtying every run** until `untrack` plus commit, and untrack costs G31 history (P3). Full relief needs praxia-side work: G31 should accept clean-run proof from something other than a per-worktree file. That is filed as a praxia handoff (T7) and **blocks praxia's relief**. G19/G31 evidence runs still need `materialize` plus commit. |
| protamer, asr, plegadx, tev_design | New lock files stop appearing (where any were written). The tracked manifest dirties every run until `untrack` plus commit. No G31 dependency is known. |
| biosensors | Locks stop. The manifest, `.bth/log/` and `outputs/provenance/` are ignored by P2, so the tree is clean. The 35 stray locks: `relocate`, then delete. |
| clean repos | No change. |

Migration order: read the run-time warnings, then `lock relocate`, then `lock untrack`
(with its caveats), then `materialize` where G19 evidence is wanted.

## 4. Fixer tasks

| Task | Scope | Tests |
|---|---|---|
| T0 | Probe B8: commit-ref lock survives `gc --prune=now`; fetch with explicit refspec; `commit-tree` with a temp index in a linked worktree; parentless carrier in a shallow clone; `skip-worktree` as an untrack alternative | spike |
| T1 | P0 repo selection (lock writer + ignore check only); dedicated resolver for `lock_location`/`lock_dir`/`ignore_outputs` with layer reporting | `tests/test_lock_config.py`: each layer incl. `.bth.toml [provenance]`; `BTH_LOCK_DIR=none` disables; invalid `BTH_LOCK_LOCATION` fails; malformed file fails; foreign-cwd run uses the script repo |
| T2 | P1 commit-ref writer + read-back + fallback + `ref_error` event; tree opt-out; URI grammar | `tests/test_lock_out_of_tree.py`: no lock in tree after `bth run`; ref body hash == `manifest_sha256`; G19 relpath; non-git and `unknown` hash → fallback; parentless when hash absent; common dir containing `#`; `"tree"` unchanged |
| T3 | P2 per-path ignores incl. `/.bth/log/`; `bth init` outputs entry; `ignore_outputs` | extend git_pin/init tests: each missing line added once; early-return bug covered; both roots when script root ≠ cwd root |
| T4 | P4 verification (three forms, missing-common-dir fallthrough, outside-repo limit) + `read_lock_text` used by blast_radius/cite/index | `tests/test_verify_run_manifest.py` |
| T5 | P3 `bth lock` group + MCP mirrors (required `project_root`, `apply`, write tokens) + `BathosErrorCode` entries + hints + run-time warnings | `tests/test_lock_cli.py`: dry-run default, dirty-index refusal, idempotence, materialize byte-identical to what G19 parses, relocate never lists a tracked lock, untrack leaves the on-disk file and prints the G31 caveat; `tests/test_errors.py` passes unchanged in shape |
| T6 | Cross-run regression, each its own case: (i) committed manifest → run 2 `git_dirty = true` as a **documented-limitation test** (named and commented so nobody "fixes" it), then `untrack --apply`, commit, run 3 `git_dirty = false`; (ii) tracked lock + default `git` → no new lock file; (iii) repo without a `.bth/log/` ignore → `git status --porcelain` empty after a run; (iv) script writing to `outputs/` under `ignore_outputs = true` → run 2 clean; (v) fresh repo, script writing nothing → run 2 clean | `tests/test_cross_run_clean.py` (committed sidecar + `.bth.toml` fixtures) |
| T7 | CHANGELOG (P5 table + portability trade-off), using-bathos note; praxia handoff (materialize before commit; G31 clean-run proof without a per-worktree manifest file; marked blocking for praxia's relief) | — |

Narrow pytest per task locally; full suite on CI.

## 5. Risks

| Risk | Mitigation |
|---|---|
| Refs are not pushed by default; clones lose lock evidence | `bth lock push/fetch`; `materialize` for committed evidence; CHANGELOG |
| Ref deletion has no reflog | Same as existing run refs; `materialize` gives a committed copy where it matters |
| A temp-index `commit-tree` racing in SLURM arrays | Per-run temp index and per-run ref: no shared state; T0 probes worktrees |
| praxia forgets to materialize and G19 fails | G19's own LOCK_ABSENT error; the handoff note; praxia can set `lock_location = "tree"` |
| `untrack` erodes G31 clean-run proof in praxia | Printed caveat; praxia handoff; praxia can simply not untrack and live with the manifest diff until G31 changes |
| Old bathos on other hosts keeps writing in-tree locks | `bth run` warns, naming `bth lock relocate` (no version claim: the lock carries no version) |
| Parentless carriers in shallow clones | Event says so; nothing reads descent today; T0 probes |

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

**Cycle 2** (Sonnet challenger `toolu_01FVE8X7MAMqsLUM7mpfgmvu.1.md`, then defender on its
findings). 0 blocking, 4 major, 5 minor; defender conceded 3 majors and partially conceded M1.
The orchestrator verified the G31 `MANIFEST_REL` read, the `resolve_flag` semantics and the
`BathosErrorCode` location. v3 changes:
- M1 untrack breaks merges and G31 history (G31 reads the on-disk manifest per worktree) → untrack never touches the on-disk file; printed G31 and merge caveats; praxia handoff for G31; B1 lists G31's manifest read.
- M2 relocate offered tracked evidence locks for deletion → tracked locks are "keep", never candidates.
- M3 symptom persists in tracked-manifest repos → per-repo outcome table; `/.bth/log/` ignored; T6 split into five cases including a documented-limitation test.
- M4 error-code registry → `BathosErrorCode` + hints + `EXCEPTION_TO_CODE`; `GateErrorCode` untouched; run-time ref failure falls back with `ref_error`.
- m5 URI grammar → fixed-marker parse, absolute common dir, missing-common-dir fallthrough.
- m6 form-3 limit stated and tested.
- m7 config chain → `.bth.toml [provenance]` layer, `~/.bth/config.toml` as the user layer, dedicated loud resolver, invalid env fails.
- m8 MCP → required `project_root`, `apply`, `cli_group="lock"`, write tokens.
- m9 P0 narrowed to lock writer and ignore check; `clean_git_env`; carrier is not a snapshot.
- Defender new issues → praxia handoff marked blocking; parentless carriers documented; T6(i) as a documented-limitation test.
