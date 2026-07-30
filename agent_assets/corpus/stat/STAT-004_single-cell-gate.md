+++
id = "STAT-004"
title = "Confirmatory runs sharing identical metadata do not cover the claim's regime"
severity = "warning"
source_check = "check_single_cell_gate"
tags = ["statistics", "claim", "coverage"]
see_also = ["STAT-001", "STAT-003"]
+++

If every run in a confirmatory campaign carries identical values for all shared metadata
keys, the campaign explored one cell of the parameter space. A claim registered over it holds
for that cell — not for the regime the claim states.

This is the quiet version of overclaiming: the runs are real, the outcomes are real, and the
verdict is arithmetically correct. What is missing is variation, and nothing in a pass/fail
label records its absence.

**What to do.** Vary the dimensions the claim's `regime` names, or narrow the regime to what
was actually covered. Narrowing is not a defeat — it is the honest version of the same result.

**Reachability note.** As of 260730 this check is defined and unit-tested but is not invoked
by `bth lint` or the MCP `lint` tool, so it does not currently fire in normal use.
