+++
id = "HYG-002"
title = "Stage names outside the canonical set fragment every stage-tiered gate"
severity = "warning"
source_check = "check_canonical_stage_names"
tags = ["hygiene", "stages", "gates"]
see_also = ["DSGN-002"]
+++

`stage_name` drives stage-tiered enforcement: which runs need a `[reproduction]` block, which
submissions are gated, which campaigns downgrade. Those gates match on the canonical set —
`exploration`, `calibration`, `validation`, `ablation`, `production`.

A non-canonical name does not error. It silently falls outside every tiered gate, so the run
is neither exploratory-lenient nor validation-strict; it is simply unmatched.

**What to do.** Use a canonical stage. If none fits, that is worth raising rather than working
around — the set is meant to be extended from real usage, and a value that recurs is evidence
for extending it.

**Advisory only — but WARNING severity.** The check emits `IssueSeverity.WARNING` for both of
its cases; it never blocks a run, but it is not an INFO-level note either.
