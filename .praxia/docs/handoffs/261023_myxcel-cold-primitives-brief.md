---
title: Brief for a myxcel session to implement myxcel cold primitives (output tiering step 5a)
description: Paste-ready brief for implementing myxcel cold put/commit/verify-file/get/lock/unlock with JSON contracts pinned by the bathos output-tiering spec, plus the owner-gated hard-link probe on so3 and titanix
status: active
task_id: 261023_bathos-output-tiering
date: '261023'
backlog_ids: ''
adversarial_review: ''
---
task_id: 261023_bathos-output-tiering (Step 5a: myxcel `cold` primitives)

Run this from a session in a **myxcel** worktree, on a new branch off myxcel `main` (for example
`feat/cold-primitives`). Commit on the branch and do not push without asking.

**Contract.** bathos PR #129, spec `.praxia/docs/specs/261023_output-tiering.md` on branch
`spec/261023-output-tiering`. Read §6.2 (commands and JSON outputs), §6.1 (how bathos sequences them: lock, resume
check, put to `.partial-<attempt8>`, verify partial, commit manifest then archive, verify finals, unlock), §5.1
(paths are relative to the profile's `cold_root`), D3, A21 and A27. bathos already mocks this contract in
`src/bathos/output_tiers/myxcel.py` (Step 5b), so every field name and status string must match §6.2 exactly.

**Commands**, a new `cold` sub-app. Use `cisternal.cli_group` as `cli.py:261-269` does, with `--json` on every
command:

| Command | Behaviour |
|---|---|
| `myxcel cold put <profile> <local_file> <remote_rel_name>` | Uploads to exactly that name. Creates missing parent directories (`mkdir -p` on the parent). Never overwrites an existing final name; a partial name is the caller's choice. |
| `myxcel cold commit <profile> <remote_partial> <remote_final>` | Runs `ln <partial> <final> && rm <partial>` on the remote. POSIX `ln` without `-f` fails if `final` exists, which gives `{"status":"exists"}`. Never `mv`, never `-f`. |
| `myxcel cold verify-file <profile> <remote_path> <sha256> <size>` | Plain sha256 (not `_tree_hash`). Re-reads cache-cold the way coldify verify does (fsync + `posix_fadvise(DONTNEED)`, see `coldify.py`). Uses login ssh at or below the size threshold (D3: a config key, default 2 GiB) and a `mit_data_transfer` job above it (reuse `_submit_coldify_job`, `coldify.py:95`). Returns `verified` / `failed` / `unverifiable`, plus `sha256_observed`, `size_observed`, `method` (`login_ssh` / `slurm_job`) and `message`. Unreachable host or job failure is `unverifiable`, never `failed`. |
| `myxcel cold get <profile> <remote_path> <local_dir>` | `{"status","local_path","bytes"}`. Refuses if the local target exists. |
| `myxcel cold lock` / `unlock <profile> <remote_dir>` | `mkdir -p <parent>` then an atomic `mkdir <remote_dir>/.bth-lock`, holding a small JSON file with host, pid, attempt and start time. Returns `acquired`, or `held` plus `holder`. `unlock` removes only the lock directory. |
| `myxcel cold ls <profile> <remote_dir>` | One directory, not recursive. `{"status":"ok"\|"missing"\|"error","entries":[{"name","size_bytes","mtime"}],"message"}`, regular files only, plus the `.bth-lock` directory as `{"name":".bth-lock","lock":true,"holder"}`. `missing` if the directory does not exist. (Added after bathos Step 6, owner decision 2026-10-23.) |
| `myxcel cold rm-partial <profile> <remote_path>` | Refuses, **before any ssh call**, a path whose last segment does not match `*.partial-[0-9a-f]{8}`. Plain `rm` of one regular file: never `-r`, never a directory, never `-f` on a glob. `{"status":"removed"\|"absent"\|"refused"\|"error","message"}`. (Added with `ls`.) |

Every command refuses an absolute remote path, any `..` segment, or an empty path, **before** any ssh call.
Remote paths are joined under the profile's `cold_root`. A profile without `cold_root` refuses.

**Tests**, fake ssh in the style of `tests/test_coldify_remote.py`. Never the whole suite; run only the new test
file with threads capped. Each test must fail on a broken implementation:
- `commit` onto an existing final returns `exists`, and the final is unchanged.
- Each attempt uses its own partial name.
- A `..`, absolute or empty path refuses without invoking ssh.
- `verify-file` returns all three states: a flipped byte gives `failed`, an ssh failure gives `unverifiable`.
- `lock` twice gives `held` with the holder.
- `put` into a missing parent creates it.
- `rm-partial` on a final name (`archive.tar.zst`), on `.bth-lock`, or on `x.partial-zz` is refused with no ssh call; on a real partial it removes exactly that file and leaves its sibling final untouched.
- `ls` on a missing directory returns `missing`; on a directory with a held lock it lists the lock with its holder.
- Every command's `--json` output parses and matches §6.2 exactly.

**Live hard-link probe (A27). It is owner-gated, so ask before running.** The sandbox blocks SSH, so this needs the
permission gate with the sandbox off, one command at a time. Under `<cold_root>/bathos-cold/.probe/` on so3
(`/orcd/data/so3/001/marielle`) and titanix (`/home/solab/marielle`):
1. `touch a`
2. `ln a b`, which must succeed
3. `ln a b` again, which must fail with a non-zero exit
4. `rm a b`, then remove only the `.probe` directory

Report the exit codes. If `ln` is refused on either filesystem, stop and report it: the spec's fallback
(`renameat2(RENAME_NOREPLACE)` through a remote helper, or `commit_unsupported`) is a design change for the owner.

**Report back to the bathos session:** branch and commit sha, test commands with pass/fail counts, the probe's exit
codes per host, and anything in §6.2 that did not fit myxcel. Do not change the contract to fit.
