+++
id = "HYG-008"
title = "Sweep active sidecars and specs whenever a methodological correction lands"
severity = "warning"
source_check = "check_correction_sweep"
tags = ["hygiene", "corrections", "staleness", "specs"]
see_also = ["HYG-004", "PREREG-005"]
+++

Accepting a methodological correction in conversation or a decision note does not remove
the superseded assumption from existing sidecars, claims, and specs. Without a mechanized
sweep, active scripts continue running under rules (`converged = true`, retired scoring
models, outdated PAE cutoffs) that were already rejected.

**What to do.** Register landed corrections in `.bth/corrections.toml` or
`.bth/corrections/*.toml` (with `old_assumptions` / `old_patterns` and `replacement`) and
run `bth lint` (or `bathos.correction_sweep.sweep_corrections`). Update every matching
active sidecar and spec, or mark intentionally retained historical references with
`[status] stale = true` / `correction_ack = ["<id>"]` / `bth:deliberate`.
