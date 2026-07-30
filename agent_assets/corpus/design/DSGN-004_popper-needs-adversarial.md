+++
id = "DSGN-004"
title = "A POPPER sidecar without any adversarial check is running a sequential test on unchallenged evidence"
severity = "warning"
applies_when = "has_popper = true AND n_adversarial_checks = 0"
source_check = "check_popper_adversarial"
tags = ["design", "falsification", "popper"]
see_also = ["DSGN-003"]
+++

A `[popper]` block turns runs into a sequential e-value test, accumulating evidence toward a
stopping threshold. That machinery is only as good as the per-run outcome it consumes: if no
branch carries an `adversarial_check`, the accumulating evidence has never been challenged,
and the sequential test will converge confidently on whatever the outcomes happened to say.

**What to do.** Put an `adversarial_check` on at least one branch — ideally the pass branch,
whose e-value contribution is what drives the test toward its threshold.

**Reachability note.** As of 260730 this check is defined but is not called from `lint_project`,
the CLI, or the MCP tool — it is dead code. The rule stands on its own; the automated
enforcement does not currently exist.
