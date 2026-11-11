---
title: 'Bearings slice 3 and merge f2e8723d: two-surface review'
description: Cursor (correctness, merge) and Antigravity (measurement) review of slice 3 shadow gates and the main merge; verified findings and verdicts
status: active
task_id: 261104_multiscale-goal-awareness
date: '261111'
verdict: needs_work
base_sha: f2e8723d
---
# Bearings slice 3 and merge f2e8723d: two-surface review

PR #161, branch `wt/261104_bearings-ac7` at f2e8723d. Two reviewers ran through `surface_dispatch`, each in a
read-only worktree at that commit:

| Reviewer | Dispatch | Lens | Record |
|---|---|---|---|
| Cursor (headless) | `0105b094` | correctness and isolation of slice 3 (2512be3b..47bfb1eb), merge resolutions | admitted, `agent_sandbox`, no files changed |
| Antigravity (headless) | `29dce6b4` | measurement validity of the six gates | admitted, `agent_sandbox`, no files changed, verdict `needs_work` |

Both reviewers were told to leave the plan's open items alone. Neither re-filed them.

**Overall verdict: needs work before any shadow-check result is read as a promotion decision.** The merge itself
is clean. The slice-3 gates, as they stand, cannot permit a promotion for the agreement metric whatever the
mapper does, and they can score packets that AC-9's own scorer would call invalid.

## Merge f2e8723d: no defects found

Cursor checked each resolution and found none wrong:
- `CharterError` and `BearingsError` subclasses still reach their specific codes through main's
  `except BathosError` MRO lookup.
- The charter and Bearings views are created in both log-mode branches of `connect_read`, before validity wiring.
- The charter refolds run beside the acknowledgment and supersession refolds.

One finding touches `mcp.py` (V6 below), but it predates the merge.

## Verified findings

"Verified" means I checked the claim myself, by probe or by reading the cited lines at f2e8723d. "Plausible"
means the reading supports it but I did not reproduce it.

### V1 (critical, both reviewers, CONFIRMED by probe): the agreement gate can never pass

`derive()` emits agreement verdicts only for the targets a mapper mapped, always with `verdict=True`
(`src/bathos/bearings/shadow.py:661`). So tn = fn = 0, and cisternal's base rate
`(tp+fn)/n_labeled` (`cisternal/shadow/scoring.py:458`) equals precision. `evaluate_gate` blocks whenever the
precision lower bound is ≤ the base rate (`cisternal/shadow/mode_state.py:436`), and a Wilson lower bound is
always below its point estimate.

Probe (throwaway; synthetic verdict and label files through `check_mode_state` with the committed
`bearings.readers.agreement.toml`): 10/10 correct gives base rate 1.0 and lower bound 0.7225; 40/40 gives 1.0 and
0.9124. Both are blocked with `precision_lower_bound_below_base_rate`. The other reasons the probe reported
(`no_isolation_evidence`, `enforcer_unimportable`) came from the probe's setup.

The same rule also bites off-charter and silent whenever positives are most of the items (Cursor's example:
eight off-charter purposes and one real one, recall 1, base rate 8/9). PLAUSIBLE by the same algebra; not probed.

The two reviewers proposed different fixes, and the choice is a design decision:
- Emit a real negative class. Antigravity: a verdict for every (reading purpose, charter id) pair, so tn and fn
  exist and the base rate is the true prior.
- Or treat the cisternal base-rate check as not applicable to these gates. Cursor.

In both cases the missing control is a positive control: an oracle mapper with isolation evidence must reach
`promotion_permitted = true` on all six gates. No such test exists today.

### V2 (critical, Antigravity, CONFIRMED by reading): the shadow path skips AC-9's validity rules

`comparison_payload` (`shadow.py:428-457`) keeps only `present`, `silent`, `revived`, `indeterminate` and
`budget_limited` from the answer key. It drops `n_withheld_runs_unresolved` and the withheld and ambiguous counts.
`derive()` never calls `ac9.invalid_reasons` (`ac9.py:463`, review round 4: an unresolved withheld run makes
silent ground truth unknown) or `ac9.bound_violations`. So a packet that AC-9 would mark invalid is scored in
shadow.

In the same area: a packet with no labelled sidecars has `required_labels == {}`. The `bad` list is then empty and
the packet is used. Every live purpose in `key["silent"]` then becomes a `label=True` silent item derived from zero
at-work labels.

### V3 (high, Antigravity, CONFIRMED by reading): the default feature_version is chosen across all projects

At `shadow.py:564` the newest feature_version is chosen from `raw[arm]` before the project filter at line 571. The
default log is shared by every project on the machine (`<catalog_dir>/shadow`). After another project's newer
verdict, `shadow_check(project="A")` scores none of A's records.

### V4 (medium, Cursor, CONFIRMED by reading): the version choice drops subjects silently

Verdicts whose feature_version differs from the chosen one hit `continue` at `shadow.py:567` without an entry in
`skipped`. Re-emitting one packet under a new charter sha therefore replaces the corpus with no trace.

### V5 (high, Cursor, CONFIRMED by reading; depends on cluster layout): cluster jobs never record at-work labels

`cluster_catalog_export` (`src/bathos/cluster_catalog.py:29`) exports `BTH_CATALOG_DIR=${BTH_PROJECT_ROOT}/.bth/catalog`
when no remote root is set. The default shadow dir `<catalog>/shadow` is then inside the project tree, and
`ensure_shadow_pipeline` refuses it (`shadow.py:231`) whenever the cluster checkout is a git work tree. The at-work
hook swallows the error, so `bth run` succeeds and no label is written. Unverified: whether the cluster project
roots are git work trees in practice.

### V6 (medium, Cursor, CONFIRMED by reading, predates the merge): four Bearings errors map to `internal`

`PacketLeakError` (RuntimeError), `PurposeLabelError`, `Ac9LabelError` and `ShadowConfigError` (ValueError) have
entries in `EXCEPTION_TO_CODE` (`errors.py:502-505`) but are not `BathosError` subclasses. Their entries were
already unreachable before the merge: the handler at 47bfb1eb caught only `(CharterError, BearingsError)`. Fix:
derive them from `BearingsError`, and add a `traced_tool` test for each one.

### V7 (high, Antigravity, CONFIRMED by reading): the readers arm does not require three readers

The readers-arm silent intersection (`shadow.py:628`) runs over however many readings the packet has, including
one. `bth bearings compare` defaults `--shadow-arm` to `readers` (`cli_cyclopts.py:2606`), so an oracle reading
recorded without the flag lands in the readers arm. Unverified: whether the oracle reader id is refused anywhere
upstream.

## Plausible, not verified

- **F3 (Antigravity):** the gates are evaluated per metric, not as a conjunction per arm. `min_verdicts` and
  `min_labels` count all items, not the metric's denominator, so silent can pass with `n_present = 1`.
- **F5 (Antigravity):** at-work labels are keyed on the sidecar content alone. One post-confirmation run of an
  unchanged sidecar validates its pre-confirmation runs. This may be the intended meaning of a content-keyed label;
  it needs an owner reading.
- **F6 (Antigravity), independence:** three readers on one packet, and overlapping 28-day windows, multiply one
  sidecar label into many correlated items, so n=8 overstates the evidence. The Wilson arithmetic is right: 8/8
  gives a lower bound of 0.676, 12/13 gives 0.667, 32/40 gives 0.652.
- **F7 (Antigravity):** single-word charter ids skip the leak patterns (`purpose_label.py:107`), and padding a
  sidecar with several slugs inflates agreement.
- **F8 (Antigravity):** the comparison payload lacks audit provenance (reading_id, mapper_id, packet window,
  run_ids, raw subjects in `derived_from`). `_emit_shadow_comparison` does not pass `catalog_dir`.
- **Cursor, medium/low:**
  - `bth sync --pull` exits 23 when the remote has no `shadow/` directory, despite `--ignore-missing-args`.
  - A non-table `tool.bathos` node falls through to the default instead of raising.
  - A `git rev-parse` failure skips the work-tree check.

## Missing tests (union of both reviews)

Positive control (oracle mapper, all six gates permitted); `BTH_CATALOG_DIR` inside the repo; `git rev-parse`
failure; non-table config node; pull without a remote `shadow/`; re-emit under a new charter sha; cross-project
feature_version; zero-label packet; unresolved withheld run; single-reader readers arm; `traced_tool` for each of the
four non-`BathosError` exceptions; mapper_only arm counts.

## What held

Cursor found these correct:
- The at-work hook never changes the `bth run` exit code when `git rev-parse` works.
- The exit flush is bounded at 2 s.
- Symlinks into the tree are refused.
- Each config layer reports its source.
- Labels from before the first confirmation do not count.
- Derived counts match `ac9.pooled_metrics`.
- A disabled log refuses rather than passes.
- Sync push semantics are right.

Antigravity found that deriving silent labels is not circular: the verdict is the readers' intersection, the label
is the sidecars' purposes. It also found that `fp_rate` equals `false_silent / n_present`.

## Fixes (261111, owner choices via AskUserQuestion: real negative class; fix V2, V3/V4, V6/V7, V5)

| Item | Commit | Fix |
|---|---|---|
| V6 | 5568c1a1 | The four errors also derive from `BearingsError`, keeping their builtin base; a `traced_tool` test pins each code. |
| V3, V4 | 5ab411fe | The version is picked after the project filter; dropped subjects appear in `skipped` as `other_feature_version:<v>`. |
| V7, F8 (forwarding) | 6b04f188 | The readers arm needs three blind readers per packet. An oracle reading is refused on the readers arm at emit and dropped at derive. The isolation enforcer records under `mapper_only`. The CLI forwards `--catalog-dir`. |
| V1, V2 | 2e0f85a6 | Agreement gains a negative class: unmapped live targets for reading purposes with charter-purpose gold. The mapped items are unchanged, so precision is still AC-9 gold agreement. The agreement mode-state version goes 0.1.0 → 0.2.0, thresholds unchanged. The payload freezes `live_ids` and `bound_violations`. derive skips invalid, unlabelled and pre-check payloads. `shadow_check` reports an arm-level conjunction. |
| V5, partial | 2e683b44, 6b0cdc27 | The work-tree guard fails closed when git times out or errors. A missing git binary counts as "no work tree". The remote `shadow/` is created, so a pull before the first label doesn't exit 23. **Not resolved: cluster runs still record no usable label** (below). |
| tests | 6b0cdc27 | The degenerate mappers run on the synthetic corpus: every arm is blocked by metric reasons, never by counts. `first_only` passes agreement alone (F3), and only the arm conjunction stops it. `used` counts only scored readings. |

**Positive control:** a synthetic perfect corpus, 3 packets × 3 readers, prevalence ≤ 0.5 per metric, with isolation
evidence. It passes all six gates and both arms.

**V5, corrected reading.** Cursor's symptom is real, but the cause differs from what the review said:
1. myxcel pushes without `.git` (the engaging, titanix and home-gpu profiles all exclude it). The default
   `<catalog>/shadow` on the cluster is therefore accepted by the guard; a test pins that.
2. But `_emit_at_work_label` finds the project root with `_git_toplevel(path)`, which on the cluster returns None.
   So it returns `skipped:not_in_git` and no label is written.
3. Even with the root fixed, the charter state comes from the catalog's `charter_confirmations`
   (`charter.py:237`), which the cluster catalog does not have. A cluster label would carry
   `after_first_confirmation=False`, and `derive` discards it.

Making cluster labels count is a design choice, so it went to the owner and was not guessed here.

**CI baseline:** the merge f2e8723d failed the same 15 tests as main 0.14.0 (identical list; the ty errors are in
main's code). No failure is specific to this branch.

**Still open, not fixed in this pass:**
- F3's `n_present` floor. Silent can rest on few present purposes; the agreement and recall floors follow from the
  Wilson bounds.
- Partially labelled packets. Unlabelled work in a packet can still read as silent; only zero-label packets are
  refused.
- F5 (content-keyed labels).
- F6's independence across packets and readers.
- F7 (single-word ids, multi-slug padding).
- The rest of F8's audit provenance.
- Cursor's non-table config node.
