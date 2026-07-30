+++
id = "PREREG-001"
title = "A numeric threshold with no stated basis is an arbitrary cutoff"
severity = "warning"
applies_when = "n_outcome_branches > n_threshold_sources"
source_check = "check_threshold_basis"
tags = ["prereg", "thresholds", "justification"]
see_also = ["STAT-001", "STAT-002"]
+++

An outcome condition containing a numeric literal — `temp_std < 5`, `rmsd <= 2.0` — encodes a
decision boundary. Where the number came from is not recoverable from the number itself.

Two legitimate answers, and the point is to record which one applies:

- It is derived: from a power calculation, an instrument's resolution, a cited prior result.
- It is conventional: chosen by convention or convenience, with no deeper claim.

Both are acceptable. What is not acceptable is leaving it unmarked, because an unmarked
threshold reads as derived to every later reader — including the author.

**What to do.** Set `source = "..."` on the outcome, or `regression_threshold_basis` on a
`[benchmark]` block. One sentence is enough; "conventional, no derivation" is a complete and
honest answer.
