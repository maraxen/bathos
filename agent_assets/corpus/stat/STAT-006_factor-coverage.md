+++
id = "STAT-006"
title = "Declared factor levels and total unit counts require explicit coverage tracking"
severity = "warning"
source_check = "check_factor_coverage_lint"
tags = ["statistics", "factors", "coverage"]
see_also = ["STAT-004", "STAT-005"]
+++

When a sidecar declares `[factors.declared_levels]` or `[excluded_levels]`, every level
list must be non-empty and every exclusion must state a non-empty reason. Unless all
declared levels are explicitly excluded, `[result_schema]` should include a matching
coverage field (such as `analyzed_levels`, `covered_levels`, or `per_<factor>`) so dropped
arms or levels cannot go unnoticed.

Similarly, declaring `n_units` in `[result_schema]` without `n_units_informative` obscures
how many units were actually non-degenerate.

**What to do.** Track analyzed levels in `[result_schema]` (or document omissions in
`[excluded_levels]`), and pair `n_units` with `n_units_informative`.
