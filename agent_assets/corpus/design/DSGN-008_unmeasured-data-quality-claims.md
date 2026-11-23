+++
id = "DSGN-008"
title = "Prose data-quality or filtering claims require measured counts and invariants"
severity = "error"
source_check = "check_unmeasured_data_quality_claims"
tags = ["design", "data-quality", "invariants"]
see_also = ["DSGN-007", "PREREG-001"]
+++

When sidecar prose (`hypothesis`, `decision`, `reasoning`, etc.) claims that data were
filtered, cleaned, deduplicated, or met a quality/completeness standard, those claims
cannot remain unmeasured prose assertions.

The sidecar must declare a corresponding measured count or quality field in
`[result_schema]` (such as `n_filtered` or `<metric>_count`) and reference it inside a
fail-closed `[invariants.*]` (or `[sanity]` / `[observe].sanity`) condition.

**What to do.** Emit the underlying quality/filtering count in `[result_schema]` and bind it
to a fail-closed `[invariants.*]` assertion so broken filtering cannot pass silently.
