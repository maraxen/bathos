---
title: Handoff to praxia - lock refs and G31 clean-run proof (bathos debt #3049)
description: What bathos changed (sidecar locks as commit refs, untrackable manifest) and the praxia-side work it needs; blocks praxia's relief from per-run tree dirtying. Tracking item praxia debt #3062.
status: draft
task_id: 261026_provenance-out-of-working-tree
date: '261026'
backlog_ids: ''
adversarial_review: ''
---
> **BLOCKING for praxia's relief.** praxia keeps a dirty tree after every `bth run` until G31 stops depending on a
> per-worktree `.bth/refs/manifest.jsonl`. Tracking item: **praxia debt #3062**. Source spec (bathos):
> `.praxia/docs/specs/261026_provenance-out-of-working-tree.md`, bathos debt #3049.

## What bathos now does

- A run's sidecar lock is no longer a new untracked file. By default it is a commit under
  `refs/bathos/locks/<run_id>` in the sidecar's repo; `run.manifest_path` is a `git:` URI. `lock_location = "tree"`
  (set in `.bth.toml [provenance]`) restores the old in-tree file.
- `bth lock untrack --apply` stages `git rm --cached` for a tracked `.bth/refs/manifest.jsonl`; the on-disk file stays.
- `bth lock materialize --run-id X --apply` writes the ref's lock byte-for-byte at the path G19 reads.

## What praxia must do

1. **Materialize before commit (G19/G31 evidence runs).** G19 (`scripts/lint/check_run_reference.py:212-235`) reads
   the lock *as committed at a rev*, beside the sidecar; G31 (`check_run_code_binding.py:8-15,254`) requires it at
   `--head`. With the default `git` mode no such file exists. For any run that is cited as evidence, run
   `bth lock materialize --run-id <id> --apply` and commit the file, or set `lock_location = "tree"` for praxia in
   `.bth.toml [provenance]` and accept in-tree locks (the status quo, 63 tracked today).
2. **G31 clean-run proof must not depend on a per-worktree manifest file.** G31 hardcodes
   `MANIFEST_REL` (`:65`) and reads `<root>/.bth/refs/manifest.jsonl` from disk in every worktree root
   (`:181-189,293-300`) as its clean-run proof. That file is why praxia's tracked manifest dirties every run, and
   untracking it (`bth lock untrack`) leaves fresh checkouts and worktrees with an empty manifest, so runs whose lines
   exist only in older checkouts fail with `SNAPSHOT_UNPROVEN`. G31 needs a proof source that lives in git or the
   catalog: for example the run record's `git_dirty`/`pinned_sha` and the `refs/bathos/runs/<id>` ref, or the lock
   ref's carrier commit (its parent is the run's `git_hash`).
3. **Until G31 changes**, praxia can either keep the manifest tracked and live with its per-run diff, or `untrack` and
   accept the G31 consequence above. Merging a branch that modified the manifest after another untracked it hits a
   modify/delete conflict; recipe: `cp .bth/refs/manifest.jsonl /tmp/keep && git checkout -- .bth/refs/manifest.jsonl &&
   git merge ... && mv /tmp/keep .bth/refs/manifest.jsonl`.
4. **xtrax** (`controller/bathos_library_wrappers.py:199`) calls `verify_run_manifest`, which now reads all three forms
   (git URI, plain path, relocated); no change needed.

## Portability

Locks in refs do not travel with commits, `bth sync` or rsync. Use `bth lock push` / `bth lock fetch` to move
`refs/bathos/locks/*`, or `materialize` for a committed copy. A run record synced from another host is unverifiable
(`verify_run_manifest_detail` reason `lock_unlocatable`) until a local clone has the ref and `run.sidecar_path` exists.
