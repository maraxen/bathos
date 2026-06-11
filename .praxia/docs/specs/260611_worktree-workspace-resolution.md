# Spec: Worktree-aware workspace resolution for `bth`

- **task_id:** `260611_worktree-workspace-resolution`
- **contemplex session:** `a5491650` (open-creative; winner I4+I11+I10+I1-ladder)
- **status:** REVISED (post spec-challenger/defender adversarial cycle)
- **adversarial review:** challenger raised O1–O11; defender adjudicated. Design-forcing: **O2** (MCP param precedence inverted in code), **O5** (ratify git-above-config + scope AC-10). Wording: O1/O3/O4/O6. Hardening: O7–O11. All folded in below.
- **schema impact:** none (no catalog/schema migration)
- **date:** 2026-06-11

---

## 1. Problem

`bth` is increasingly invoked with `cwd` inside a **git worktree** (e.g. `<repo>/.claude/worktrees/<name>/`, an isolated linked checkout). Git provenance capture already works per-worktree (`git.py:18-39` shells out with `cwd`, and git auto-resolves the `.git`-file pointer, so `git_branch`/`git_hash`/`git_dirty` are already correct). The defect is **filesystem-root resolution**: bathos derives a "workspace root" from the absolute `[project] root` field recorded in `.bth.toml` (`config.py:37-48`, `load_project_config`), which points at the **main checkout**, not the live worktree.

**Concrete failure:** postmortem asset-link validation and the postmortem scan resolve asset paths against `workspace_root = project_config.root` (the main checkout) even when run from a worktree, so they validate the wrong files — silently. The pattern `workspace_root = project_config.root` (with a `Path.cwd()` fallback) is the `fs_root` defect, duplicated at **7 call sites** (verified by fresh grep, O1):

| # | Site | Role |
|---|------|------|
| 1 | `compact.py:410` | postmortem `rglob` scan root (`:412` cwd fallback) |
| 2 | `cli.py:1535` | postmortem cite (`:1537` cwd fallback) |
| 3 | `cli.py:1591` | postmortem scan (`:1593` cwd fallback) |
| 4 | `cli.py:1677` | postmortem validate |
| 5 | `mcp.py:969` | MCP postmortem mirror — at `:966` a `workspace_root` param seeds cwd, but `:969` **unconditionally overrides it** with recorded root. The explicit param does NOT win today; migration must invert this (see §5.5, AC-11) |
| 6 | `mcp.py:1028` | MCP postmortem mirror (same `:1025` seed → `:1028` override pattern) |
| 7 | `mcp.py:1051` | MCP postmortem mirror (same `:1048` seed → `:1051` override pattern) |

**DO-NOT-MIGRATE — catalog-identity sites (must stay path-independent; routing these through `fs_root` would cause RISK-1 leak):**
- `_catalog_dir` (`cli.py:32-41`) and `_require_project_slug` (`cli.py:44-54`) — read slug/catalog from the **TOML field**, not the path. Correct today.
- `list_outputs_tool` (`mcp.py:1091-1097`) and `outputs_summary_tool` (`mcp.py:1151-1157`) — take a `workspace_root` param but use it to resolve **`catalog_dir`** (identity), NOT a filesystem root. A mechanical `workspace_root` sweep would wrongly migrate these (O3).

**Considered-and-excluded:** `bth check` / `check_runs` (`checker.py:53`, fed `project_root=Path.cwd()` at `cli.py:458` and `mcp.py:332`) derives git state from **cwd already**, so it is intentionally untouched — git auto-resolves the worktree, making it already worktree-correct (O3).

## 2. Goal / Non-goals

**Goal.** When `bth` runs from a git worktree, all **workspace-relative filesystem** operations (postmortem asset validation/scan, and any future sidecar/sync/export consumer) resolve against the **live worktree checkout**, while **catalog identity** (project slug, catalog directory) stays **stable** across all worktrees of one repo. Behave gracefully (no crash, sane fallback) when not in a git tree (SLURM spool, extracted tarball).

**Non-goals (explicitly deferred):**
- **N1.** Schema-tagged worktree provenance (idea I5) — deferred; branch already identifies a worktree (see §8, ASM-3).
- **N2.** Catalog concurrency (per-worktree namespacing I7 / shared-catalog hardening I8) — out of scope; tracked as a downstream dependency (see §9, RISK-3).
- **N3.** A first-class `workspace` entity / lifecycle (I9).
- **N4.** Changing the `.bth.toml` format or the meaning of `[project] root` (it remains a valid fallback).

## 3. Decision (winner)

Introduce a single resolution seam: **`src/bathos/workspace.py`** exposing `resolve_workspace(cwd) -> WorkspaceContext`.

```python
@dataclass(frozen=True)
class WorkspaceContext:
    slug: str | None           # catalog IDENTITY — from .bth.toml field (or BTH_PROJECT_SLUG)
    identity_root: Path | None # recorded [project] root (stable; used for identity/display only)
    fs_root: Path              # LIVE filesystem root for workspace-relative file ops
    is_worktree: bool          # cwd is inside a linked git worktree
    worktree_name: str | None  # basename of the worktree checkout when is_worktree
    source: str                # which rung resolved fs_root: "env" | "git" | "config" | "cwd"
```

**`fs_root` precedence ladder (highest wins):**
1. **`BTH_WORKSPACE_ROOT`** env var, if set and non-empty → `Path(...).expanduser().resolve()` (idea I10; value SHOULD be absolute — `.resolve()` guards a relative value, O10). The deterministic escape hatch.
2. **Git toplevel** (idea I11): a single `git rev-parse --show-toplevel --git-common-dir --git-dir` from `cwd`. `--show-toplevel` → `fs_root`; `is_worktree = (realpath(--git-dir) != realpath(--git-common-dir))`. (`--git-common-dir`/`--git-dir` may be **relative** on some git versions → resolve against `cwd` before comparing.)
3. **Recorded `project_config.root`** (idea I1 ladder), if a `.bth.toml` is found.
4. **`Path.cwd()`** — last resort.

**Rung ordering is deliberate — git (rung 2) ABOVE recorded root (rung 3) — and was ratified against O5.** Do NOT invert globally: a worktree's recorded `[project] root` *is* the main checkout, so putting config above git would defeat the entire fix (worktree invocations would resolve to the main checkout again). The consequence is that when recorded root and git toplevel **legitimately diverge** (recorded root is a monorepo subdir, or a symlinked checkout), git toplevel wins by design — this is a deliberate, documented behavior change scoped by AC-10, listed in §6, and flagged for follow-up in TBD-6.

**Identity is unchanged:** `slug` and `catalog_dir` continue to come from `.bth.toml` / `BTH_PROJECT_SLUG` / `BTH_CATALOG_DIR`. `resolve_workspace` MUST NOT derive identity from `fs_root`. This is the load-bearing contract (see RISK-1).

**Migration of call sites:** the 8 sites in §1 switch from `project_config.root` (with `cwd` fallback) to `resolve_workspace(cwd).fs_root`. `mcp.py` sites keep honoring their explicit `workspace_root` param when a caller passes one (it maps to precedence rung 1-equivalent).

**Runner-up (recorded, not chosen):** I1 pure surgical inline fix. Steelman: smallest correct change, highest reversibility, no premature abstraction. Rejected as standalone because 8 duplicated edits drift with no central test seam; the Rule-of-Three extraction trigger is already met at 8 sites. I1's ladder is preserved *inside* `resolve_workspace`, so descoping to I1 later is a no-migration refactor.

## 4. Acceptance criteria (Given-When-Then, atomic, testable)

- **AC-1 (worktree fs_root).** **Given** a repo with `.bth.toml` recording `[project] root` = the main checkout, and a git worktree at `<repo>/.claude/worktrees/wt1`, **When** `resolve_workspace(<repo>/.claude/worktrees/wt1)` is called, **Then** `fs_root == <repo>/.claude/worktrees/wt1`, `is_worktree is True`, `worktree_name == "wt1"`, `source == "git"`.
- **AC-2 (main checkout unchanged).** **Given** the same repo, **When** `resolve_workspace(<repo>)` is called from the main checkout, **Then** `fs_root == <repo>`, `is_worktree is False`, `source == "git"`.
- **AC-3 (identity stable across nested worktrees).** **Given** two worktrees `wt1`, `wt2` **nested under the repo** (so upward `.bth.toml` discovery from each reaches the main checkout — the `<repo>/.claude/worktrees/<name>/` convention guarantees this), **When** `resolve_workspace` is called in each, **Then** `slug` and the resolved catalog directory are **identical** across both and equal to the main-checkout values (no path-derived identity). *Precondition noted per O6: a worktree created OUTSIDE the repo tree (`git worktree add /tmp/wt2`) whose upward walk finds no `.bth.toml` would fall to the default catalog — that case is out of scope here and tracked in TBD-5.*
- **AC-4 (env override wins).** **Given** `BTH_WORKSPACE_ROOT=/tmp/ws` exported, **When** `resolve_workspace(cwd)` is called from anywhere (including inside a git repo), **Then** `fs_root == /tmp/ws` and `source == "env"`.
- **AC-5 (not in a git tree → recorded root).** **Given** a directory that is **not** under any git repo but where a `.bth.toml` is found by upward walk, and `BTH_WORKSPACE_ROOT` unset, **When** `resolve_workspace(cwd)` is called, **Then** `fs_root == project_config.root`, `is_worktree is False`, `source == "config"`, and **no exception is raised**.
- **AC-6 (no git, no config → cwd).** **Given** a directory with no git repo and no discoverable `.bth.toml`, and no env override, **When** `resolve_workspace(cwd)` is called, **Then** `fs_root == cwd`, `source == "cwd"`, no exception.
- **AC-7 (relative --git-common-dir handled).** **Given** a git version where `--git-common-dir` returns a path relative to `cwd`, **When** detection runs in a worktree, **Then** `is_worktree` is still computed correctly (paths resolved to absolute before comparison).
- **AC-8 (postmortem validates worktree files).** **Given** a postmortem whose `asset_link` carries a `sha256` **OR** validation is run with `--strict-files` (the gate at `postmortem.py:160-161` — a missing asset is silently tolerated otherwise, O4), **and** the asset present in worktree `wt1` but **absent** from the main checkout, **and** a `.bth.toml` discoverable from `wt1` (nesting, so config resolution stays inside the worktree), **When** `bth postmortem validate <file>` runs with `cwd` inside `wt1`, **Then** validation resolves the relative `asset_link` against `wt1` (`postmortem.py:148,159`) and passes; **And** the same validation run from the main checkout fails (proving the fix is load-bearing). *Without the sha256/`--strict-files` precondition the bug does not reproduce and the test cannot go red-before-green.*
- **AC-9 (detached HEAD).** **Given** a worktree with a detached HEAD, **When** `resolve_workspace` runs, **Then** `fs_root` is the worktree toplevel and no exception is raised (provenance/branch handling unchanged).
- **AC-10 (no regression for single-checkout users — scoped, O5).** **Given** an existing single-checkout project where `realpath(git_toplevel) == realpath(recorded [project] root)` (the common case), **When** any migrated command runs, **Then** observable behavior is identical to pre-change (`fs_root` == that shared path via the git rung). *The guarantee is explicitly scoped to `toplevel == recorded root`; when they diverge see AC-12.*
- **AC-11 (MCP explicit param wins — O2).** **Given** an MCP postmortem mirror called with an explicit `workspace_root=X`, **When** the (migrated) tool resolves its filesystem root, **Then** `fs_root == X` regardless of any discoverable `.bth.toml` (i.e. the recorded-root override at `mcp.py:969/1028/1051` is removed). *This is a deliberate behavior change from current code, which lets recorded root clobber the param.*
- **AC-12 (documented divergence behavior — O5).** **Given** a project whose recorded `[project] root` is a **subdir** of the git toplevel (monorepo) or a **symlinked** checkout, so `git_toplevel != recorded root`, and no `BTH_WORKSPACE_ROOT` set, **When** a migrated postmortem scan runs, **Then** `fs_root` is the **git toplevel** by design (rung 2 > rung 3) — which **widens** the `rglob` scan scope vs pre-change. This is the deliberate, documented trade-off of O5's rung ratification; the alternative (recorded-root override) is rejected because it would re-break the worktree case. A one-time stderr warning on this divergence is offered in TBD-1.

## 5. Design detail

**5.1 Single git call.** `subprocess.run(["git","rev-parse","--show-toplevel","--git-common-dir","--git-dir"], cwd=cwd, ...)` yielding toplevel + the two git-dir variants. Reuse the exact exception idiom from `git.py:18-39`: catch `(subprocess.CalledProcessError, FileNotFoundError)` → fall through to the next rung. Parse stdout line-by-line in flag order. Resolve any relative `--git-common-dir`/`--git-dir` against `cwd` before equality test. The signature MUST be `def resolve_workspace(cwd: Path | None = None)` with `cwd = cwd or Path.cwd()` inside — do NOT copy `git.py:18`'s `cwd: Path = Path.cwd()` eval-at-import default (O8; the §5.1 reuse is scoped to the *exception* idiom only). The multi-flag call is **all-or-nothing**: if any requested flag fails (e.g. `--show-toplevel` in a bare repo) the whole `git rev-parse` exits non-zero and ALL outputs are discarded → clean fall-through to rung 3 (O7).

**5.2 Worktree detection.** `is_worktree = realpath(git_dir) != realpath(git_common_dir)`. In a primary checkout these are equal; in a linked worktree they differ. `worktree_name = fs_root.name` when `is_worktree`. **Known false-positives (O7, accepted):** a **submodule** checkout also satisfies `git_dir != git_common_dir`, so `is_worktree` may report True for a submodule — acceptable because `fs_root` stays the correct submodule toplevel; only the boolean is misleading. An externally-set `GIT_DIR`/`GIT_COMMON_DIR`/`GIT_WORK_TREE` can defeat the discriminator; `BTH_WORKSPACE_ROOT` overrides `fs_root` (rung 1) but there is no override for the `is_worktree` flag. A first-class submodule-vs-worktree distinction is a **non-goal**.

**5.3 Identity/fs split contract.** `resolve_workspace` returns BOTH `identity_root` (recorded, for display/identity) and `fs_root` (live). `_catalog_dir`/`_require_project_slug` continue to use TOML-sourced identity and MUST NOT be rewired to `fs_root`. A unit test asserts catalog dir + slug are byte-identical whether called from main checkout or a worktree (AC-3).

**5.4 Caching.** `resolve_workspace` may memoize within a single process invocation (one git call per `bth` run). No cross-process/global cache. Any memo **MUST key on `cwd`** (and relevant env state); the long-lived MCP server process handles many tool calls with differing `cwd`/`workspace_root`, so a process-global memo would serve a stale key (O11).

**5.5 mcp.py — precedence must be INVERTED (O2, design fix).** The three postmortem mirrors *appear* to accept an explicit `workspace_root` param, but the current code at `mcp.py:966-969` (and `:1025-1028`, `:1048-1051`) does:
```python
ws = Path(workspace_root) if workspace_root else Path.cwd()   # seed
config_path = find_project_config(ws)
if config_path:
    ws = load_project_config(config_path).root                # UNCONDITIONAL override
```
i.e. the explicit `workspace_root` is merely the **seed** for config discovery and is then **clobbered by recorded root** whenever a `.bth.toml` is found. So today the param does NOT win. The migration must **invert** this: an explicitly-passed `workspace_root` becomes the **top-precedence** source (rung-1-equivalent), and only when it is absent do we call `resolve_workspace(cwd).fs_root` (which itself runs the env→git→config→cwd ladder). This is a deliberate behavior change for any MCP caller that passed `workspace_root` expecting it to win — verified by AC-11.

## 6. Edge-case matrix

| Case | Expected | Rung |
|------|----------|------|
| Inside worktree, `.bth.toml` tracked | worktree toplevel | git |
| Inside worktree, `.bth.toml` main-only/gitignored | worktree toplevel (git rung doesn't depend on `.bth.toml`) | git |
| Main checkout | main toplevel | git |
| `BTH_WORKSPACE_ROOT` set | the env path | env |
| SLURM spool, not under any repo | recorded root (if config found) else cwd | config/cwd |
| SLURM spool **under an unrelated repo** | ⚠ git returns a *wrong-but-valid* toplevel → see RISK-2 (mitigation: set `BTH_WORKSPACE_ROOT` in the SLURM env helper) | env (mitigated) |
| Extracted tarball, no `.git` | recorded root else cwd | config/cwd |
| Detached HEAD worktree | worktree toplevel | git |
| Nested worktree | nearest enclosing toplevel (documented behavior) | git |
| Submodule checkout | submodule toplevel (correct); ⚠ `is_worktree` may be True (O7, accepted) | git |
| Monorepo: recorded root is a SUBDIR of git toplevel | git toplevel (WIDER than recorded root) — deliberate per AC-12/O5 | git |
| Symlinked checkout (recorded root ≠ realpath) | git toplevel (realpath) — deliberate per AC-12 | git |
| `GIT_DIR`/`GIT_WORK_TREE` env set | discriminator may be defeated; set `BTH_WORKSPACE_ROOT` to force `fs_root` | env |

## 7. Test plan

- **Unit (`tests/test_workspace.py`, new):** AC-1..AC-7, AC-9 with `tmp_path` fixtures that `git init` a repo, `git worktree add` a linked checkout, and monkeypatch `BTH_WORKSPACE_ROOT`. Include a fake-`git`-on-PATH or version-shim test for AC-7 (relative `--git-common-dir`).
- **Integration (`tests/test_postmortem.py`, extend):** AC-8 — asset present only in the worktree; assert validation passes from the worktree and (pre-change) fails against main.
- **Regression:** AC-10 — existing config/git tests must stay green; add a single-checkout assertion that `fs_root == project_config.root` in the common case.
- **Sanity invariant (per BATHOS.md rule):** feed `resolve_workspace` a synthetic main-vs-worktree divergence and assert it does NOT return identity from `fs_root`.

## 8. Assumptions

| ID | Assumption | If false |
|----|-----------|----------|
| ASM-1 | `git` is on PATH in interactive/agent contexts; absence is handled by the fallback ladder. | Fallback to recorded root/cwd still yields no-crash behavior. |
| ASM-2 | `git rev-parse --show-toplevel` returns the **intended** workspace when inside a git tree. | False under SLURM-spool-under-unrelated-repo → RISK-2; mitigated by `BTH_WORKSPACE_ROOT`. |
| ASM-3 | Per-worktree identity is adequately captured by `git_branch`/`git_hash` already in `COOL_SCHEMA` (`schema.py:39-41`); no new column needed. | Revisit I5 if a first-class "runs by worktree" query need emerges. |
| ASM-4 | Each worktree is on its own branch (so branch ≈ worktree identity). | If two worktrees share a branch, they are catalog-indistinguishable except by path; acceptable for now. |
| ASM-5 | The 7 enumerated sites (§1) are the complete set of **fs_root** consumers — NOT all `workspace_root` consumers. `list_outputs_tool`/`outputs_summary_tool` use `workspace_root` for catalog identity and are explicit carve-outs (§1). | **Verified by challenger+defender fresh grep (O1/O3).** Re-confirm before T3 if code drifted. |

## 9. Risk table

| ID | Risk | Severity | Mitigation |
|----|------|----------|------------|
| RISK-1 | Identity split leaks: a migrated site derives **catalog identity** from `fs_root`, fragmenting one project's history across N catalogs. | **High** | Contract in §5.3; AC-3 + sanity invariant test; code review gate that `_catalog_dir`/`_require_project_slug` are NOT rewired. |
| RISK-2 | `git --show-toplevel` returns a **plausible-but-wrong** path (SLURM spool under an unrelated repo; `bth` run from a different repo's subdir), used silently. | Med-High | `BTH_WORKSPACE_ROOT` highest precedence; set it in the SLURM env helper `templates/_bth_env.sh`; optional sanity warning when git toplevel and recorded root disagree AND no env override. |
| RISK-3 | Enabling correct per-worktree roots makes concurrent multi-worktree workflows attractive, finally exercising the latent shared-`bathos.db` compaction race (`compact.py`). | Med | Out of scope here (N2); tracked as explicit downstream dependency (I7/I8). Document the limitation; do not advertise concurrent compaction as supported. |
| RISK-4 | New module not adopted everywhere → drift returns. | Low | Single seam + grep test in CI that `project_config.root` is not used for filesystem roots outside `workspace.py`. |

## 10. Pre-mortem record (AI)

Six months out, it failed because: **(1)** the identity/fs split leaked and runs from worktrees silently landed in a `cwd`-derived catalog, fragmenting history (→ RISK-1 controls); **(2)** SLURM jobs wrote garbage roots from spool dirs because nobody set `BTH_WORKSPACE_ROOT` in the cluster env helper (→ RISK-2 mitigation made mandatory in `templates/_bth_env.sh`); **(3)** per-worktree roots made simultaneous multi-worktree work attractive and the unaddressed shared-DB compaction race corrupted the catalog (→ RISK-3, tracked separately).

## 11. Open TBDs (for spec-challenger)

| ID | TBD |
|----|-----|
| TBD-1 | Should a git-toplevel-vs-recorded-root divergence (no env override, AC-12) emit a one-time stderr **warning**, or stay silent? (epistemic-hygiene vs noise.) Leaning: warn once. |
| TBD-2 | Exact minimal `git rev-parse` flag set + parsing order; confirm `--git-dir` is needed or `--show-toplevel` + `--git-common-dir` suffice for `is_worktree`. Validate the single-call all-or-nothing behavior (§5.1) on the project's target git versions. |
| TBD-3 | **RESOLVED (O3 grep):** no recorded-root filesystem consumers beyond the 7 `fs_root` sites; `list_outputs_tool`/`outputs_summary_tool` are catalog-identity carve-outs; `bth check` is cwd-derived already. Re-run grep before implementation only if code drifted. |
| TBD-4 | Whether `mcp.py`'s `workspace_root` param needs a doc/typing change now that it becomes top-precedence (AC-11). |
| TBD-5 | Should catalog IDENTITY be resolved via `--git-common-dir` (→ main `.git`, hence the main checkout) instead of cwd-upward-walk, to make identity stable even for worktrees created OUTSIDE the repo tree (O6)? Larger design move; **non-goal for first ship**, candidate for I9 territory. |
| TBD-6 | Behavior when recorded root legitimately diverges from git toplevel (monorepo subdir / symlink, AC-12): is "git toplevel wins" acceptable long-term, or do monorepo users need a `[project] scan_root` override? Defer until a real monorepo user appears. |

## 12. Backlog decomposition (feeds the DAG)

- **T1.** `workspace.py`: `WorkspaceContext` + `resolve_workspace(cwd: Path|None=None)` with the 4-rung ladder (env `.expanduser().resolve()` → single all-or-nothing git call → recorded root → cwd) and dual-anchor git detection (relative-path-safe, submodule note). (no deps)
- **T2.** `tests/test_workspace.py`: AC-1..AC-7, AC-9 + the §5.3 sanity invariant (identity not from `fs_root`). (deps: T1)
- **T3.** Migrate the **7** `fs_root` call sites (`compact.py:410`; `cli.py:1535/1591/1677`; `mcp.py:969/1028/1051`) to `resolve_workspace(cwd).fs_root`. **Do NOT touch** the carve-outs (`_catalog_dir`, `_require_project_slug`, `list_outputs_tool`@1091, `outputs_summary_tool`@1151, `bth check`). (deps: T1)
- **T3b.** Invert MCP postmortem-mirror precedence so an explicit `workspace_root` param wins over recorded root (remove the `mcp.py:969/1028/1051` override) — satisfies AC-11. (deps: T1; pairs with T3)
- **T4.** Extend `tests/test_postmortem.py` for AC-8 (sha256/`--strict-files` precondition, worktree-only asset); AC-11 (MCP param precedence); regression AC-10 + documented-divergence AC-12. (deps: T3, T3b)
- **T5.** `templates/_bth_env.sh`: export an **absolute** `BTH_WORKSPACE_ROOT` (RISK-2 / O10). (deps: T1)
- **T6.** Docs: `.bth.toml`/env reference (`BTH_WORKSPACE_ROOT` absolute) + CHANGELOG; note RISK-3 (shared-DB compaction) and AC-12 divergence limitations. (deps: T3)
- **T7 (optional).** CI grep guard: `project_config.root` not used as a filesystem root outside `workspace.py` (RISK-4). (deps: T3)
