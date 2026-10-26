+++
id = "HYG-006"
title = "A resume or output directory outside every declared root has no reference hashes"
severity = "warning"
source_check = "check_undeclared_resume_dirs"
tags = ["hygiene", "outputs", "reproducibility"]
see_also = ["HYG-001"]
+++

A run passed `--resume-dir` or `--out-dir` a directory that lies under none of its sidecar's
`[outputs] roots` and that `bth outputs adopt` never recorded. Nothing in the catalog holds
reference hashes for the files written there, so `bth cold` cannot copy them and the worktree
cannot be removed without losing them.

The failure is silent until cleanup: the per-cell files a resumable run depends on look like
ordinary scratch, and they disappear with the worktree.

**What to do.** Declare the directory, or a parent of it, under `[outputs] roots` in the sidecar
before the next run. For a run that has already finished, record its files with
`bth outputs adopt <run_id> <dir>`.
