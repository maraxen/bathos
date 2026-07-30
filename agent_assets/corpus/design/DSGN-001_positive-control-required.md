+++
id = "DSGN-001"
title = "Without a positive control, 'no effect' and 'broken pipeline' are indistinguishable"
severity = "warning"
applies_when = "has_null_capable_outcome = true AND has_controls = false AND has_differential = false"
source_check = "check_positive_control_missing"
tags = ["controls", "design", "validity"]
see_also = ["DSGN-003", "DSGN-004"]
+++

An outcome that can legitimately fire when the measured effect is genuinely absent — a `fail`
label, or any `is_residual = true` catch-all — needs something that proves the measurement
could have detected an effect had one been present.

Without it the two most important explanations for a null are indistinguishable:

1. the effect is genuinely absent, and
2. the pipeline is broken or insensitive.

**What to do.** Declare a `[controls].positive_outcome`, or a `[differential]` block. The
control should be something you are confident *will* fire; its failing is then a pipeline
alarm rather than a finding.

**Provenance.** Filed as debt #1071 after 13 experiments in which a broken, insensitive
pipeline read silently as a negative result.

**Scope of `applies_when`.** Fires only when a null-capable outcome exists *and* neither a
`[controls].positive_outcome` nor a `[differential]` block is declared — matching
`check_positive_control_missing`, which skips sidecars that are not null-capable and accepts
either block as satisfying the requirement.
