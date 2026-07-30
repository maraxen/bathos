+++
id = "STAT-002"
title = "A high residual-outcome rate means the pre-registered branches do not cover reality"
severity = "warning"
source_check = "check_residual_rates"
tags = ["statistics", "outcomes", "campaign"]
see_also = ["STAT-003"]
+++

Every sidecar declares outcome branches ahead of the run. A *residual* outcome is the
catch-all that fires when none of the declared branches matched. A campaign where residuals
exceed **10%** of runs is evidence the declared branches did not anticipate what the
experiment actually does.

That matters beyond tidiness: an outcome that lands in the residual bucket was, by
construction, not pre-registered — so any claim resting on it is post-hoc.

**What to do.** Read the residual runs, find the pattern the branches missed, and add a
branch for it. Do not widen an existing branch to swallow them; that converts a visible gap
into an invisible one.

**Threshold.** The 10% figure is the lint's default (`threshold: float = 0.10`) and is
conventional, not derived. Treat it as a prompt to look, not a calibrated boundary.
