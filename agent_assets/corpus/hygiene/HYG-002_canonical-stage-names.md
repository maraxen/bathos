+++
id = "HYG-002"
title = "Stage names outside the canonical set fragment every stage-tiered gate"
severity = "warning"
source_check = "check_canonical_stage_names"
tags = ["hygiene", "stages", "gates"]
see_also = ["DSGN-002", "HYG-005"]
+++

`stage_name` drives stage-tiered enforcement: which runs need a `[reproduction]` block, which
submissions are gated, which campaigns downgrade. Those gates match on the canonical set —
`exploration`, `calibration`, `validation`, `ablation`, `production`.

This check scans *past runs* in the catalog, so it only warns. For runs recorded before
schema v17 a non-canonical name fell outside every tiered gate, neither exploratory-lenient nor
validation-strict. From v17 on, a non-canonical name on a sidecar resolves the run's
`epistemic_class` to `confirmatory` (fail closed), and the sidecar-side check (HYG-005) reports it
as an error before the run.

**What to do.** Use a canonical stage. If none fits, that is worth raising rather than working
around — the set is meant to be extended from real usage, and a value that recurs is evidence
for extending it.

**Advisory only — but WARNING severity.** The check emits `IssueSeverity.WARNING` for both of
its cases; it never blocks a run, but it is not an INFO-level note either.
