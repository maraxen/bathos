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
in words.

**Enforcement, stated precisely.** A *blank or missing* label is an error in `validate_claim`
(`claim.py`). A *placeholder* label — `"TODO"`, `"hypothesis 1"` — is **only ever a lint
warning**: `is_placeholder_label` is called from `linter.py` alone, and neither `register_claim`
nor `conclude_campaign` calls `validate_claim` at all. So nothing downstream will catch a
placeholder for you. Fix it at authoring time because no gate will.
