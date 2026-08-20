+++
id = "HYG-004"
title = "A stale script that is still visible in the working tree is still a landmine"
severity = "warning"
source_check = "check_archival_candidates"
tags = ["hygiene", "staleness", "archival"]
see_also = ["PREREG-005"]
+++

`[status] stale = true` (`PREREG-005`) stops `bth run` from executing a known-wrong script, but
the file is still sitting in `scripts/` — still readable, still greppable, still liable to be
copied from by a future agent or researcher who never runs it through `bth run` at all.

`bth archive-artifact` closes that gap: it replaces the script's (and its sidecar's, and any
tracked outputs') content with a small self-describing stub, committed and backed by `git
notes`, a durable ledger, and (for untracked outputs) a `git bundle` — full provenance
preserved, working-tree visibility removed. `bth restore <item-id>` recovers the original bytes
exactly, including from a fresh clone with no local catalog.

This check only ever *proposes* a candidate — a stale sidecar with no covering
`archived_items` record. It never archives anything itself; only an explicit
`bth archive-artifact` call mutates the tree.

**What to do.** Run `bth archive-artifact <script> --verdict ... --reason ...` to migrate it
out of working-tree visibility, or fix the script and clear `[status] stale`.
