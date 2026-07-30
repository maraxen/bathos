+++
id = "HYG-003"
title = "A large pile of unvalidated runs means outcomes are not being evaluated"
severity = "warning"
source_check = "check_run_concentration"
tags = ["hygiene", "outcomes", "process"]
see_also = ["STAT-002", "HYG-001"]
+++

More than **20** runs in one campaign, or against one script, with no outcome recorded —
outcome null, empty, `unknown` or `none` — means runs are accumulating without anyone
evaluating what they showed.

Unvalidated runs are not neutral. They are the raw material for post-hoc selection: the larger
the pile, the easier it is to find a subset that supports whatever is later claimed, and the
harder it is to argue that subset was not chosen after the fact.

**What to do.** Evaluate or discard. If outcome conditions cannot be evaluated because the
result schema drifted, fix the schema — the runs are not informative until they carry a label.

**Threshold.** 20 is the lint's default (`threshold: int = 20`) and is conventional. It is also
CLI-configurable via `--concentration-threshold`.
