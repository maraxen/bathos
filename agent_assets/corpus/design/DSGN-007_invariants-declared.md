+++
id = "DSGN-007"
title = "Confirmatory and benchmark sidecars should declare fail-closed invariants"
severity = "warning"
source_check = "check_invariants_declared"
tags = ["design", "invariants", "validity"]
see_also = ["DSGN-003", "DSGN-008"]
+++

Confirmatory experiment sidecars in `scripts/experiments/` and benchmark sidecars in
`scripts/benchmarks/` (excluding `[observe]` and `[calibrate]` stages) should declare at
least one `[invariants]` assertion.

Invariants check structural or physical sanity properties on emitted results (such as finite
counts, non-empty sample sizes, or conservation/monotonicity bounds) so that silent
measurement bugs fail closed rather than producing a spurious `pass` or `fail`.

**What to do.** Add an `[invariants.<name>]` block with a DuckDB SQL `condition` and
`reasoning` that must hold for any valid measurement.
