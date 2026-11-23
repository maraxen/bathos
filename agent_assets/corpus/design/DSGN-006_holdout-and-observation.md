+++
id = "DSGN-006"
title = "Confirmatory tests promoted from exploration require a verified holdout"
severity = "error"
source_check = "check_holdout_and_observation_lint"
tags = ["design", "holdout", "observation", "laundering"]
see_also = ["HYG-005", "PREREG-006"]
+++

Promoting an exploratory observation into a confirmatory hypothesis (`--from-observation` /
`[provenance].derived_from_observation`) or running a confirmatory sidecar that matches a
prior exploratory script or canonicalized `[outcomes]` signature requires an explicit
`[holdout]` specification to guard against testing a hypothesis on the same data that
generated it.

Confirmatory runs linked to an observation that also execute with the exact same `argv` as
the observation's source run are flagged for holdout verification.

**What to do.** Declare `[holdout]` (`kind`, `unit`, or `split_manifest`) on confirmatory
sidecars promoted from exploration, and ensure the confirmatory invocation evaluates on
held-out inputs rather than reusing the exploratory run's exact command arguments.
