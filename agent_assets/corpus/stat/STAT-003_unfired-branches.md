+++
id = "STAT-003"
title = "A branch that always fires the same way has stopped discriminating"
severity = "warning"
source_check = "check_unfired_branches"
tags = ["statistics", "outcomes", "discriminability"]
see_also = ["STAT-002", "STAT-004"]
+++

When a script has accumulated **5 or more** runs and every one produced the *same* outcome
label, the declared branch set is no longer distinguishing anything. Two readings, both worth
acting on:

- The hypothesis is settled. Record that and stop re-running it.
- The other branches are unreachable — a condition that can never be true, or a measurement
  that cannot move. That is a broken instrument wearing the costume of a confirmed result.

The second reading is the dangerous one, and it looks identical to the first from the outcome
column alone. Distinguishing them requires checking that some input *could* have driven a
different branch.

**Threshold.** 5 runs is the lint's default (`min_runs: int = 5`), conventional rather than derived.
