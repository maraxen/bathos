+++
id = "PREREG-003"
title = "An opaque hypothesis or confound id needs a descriptive label"
severity = "warning"
source_check = "check_claim_opaque_labels"
tags = ["prereg", "claim", "readability"]
see_also = ["PREREG-002"]
+++

Claim entries identified only as `H1`, `C2` and the like carry no meaning outside the head of
whoever wrote them. The Union Gate reports coverage in terms of these ids, so an uninterpretable
id produces an uninterpretable verdict — the gate says clause `H3` is uncovered and the reader
has no way to know what that means.

A placeholder label (`"TODO"`, `"hypothesis 1"`) is treated the same as a missing one, because
it conveys the same amount.

**What to do.** Give every opaque id a one-line `label` that states the hypothesis or confound
in words. This is checked at `bth claim validate` and escalates to an error at register and
conclude, so it is cheaper to fix at authoring time.
