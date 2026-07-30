+++
id = "PREREG-002"
title = "A TODO left in a scaffolded sidecar means the pre-registration never happened"
severity = "warning"
source_check = "check_todo_strings_in_scaffold"
tags = ["prereg", "scaffold", "hygiene"]
see_also = ["PREREG-003"]
+++

`bth new-experiment` scaffolds a sidecar with `TODO` markers in the hypothesis and in each
outcome's `decision`. A run against a sidecar still carrying them is pre-registered in form
only — the file exists and validates, but nothing was actually committed to in advance.

This defeats the entire mechanism rather than degrading it. The gate checks that a sidecar is
present and parses; it cannot check that the author meant it.

**What to do.** Replace every `TODO` before the first real run. If the hypothesis is not yet
clear enough to state, the experiment is exploratory — say so with `stage_name = "exploration"`
rather than leaving a placeholder in a validation-stage sidecar.
