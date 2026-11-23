+++
id = "PREREG-006"
title = "Calibration threshold sources must point to usable calibrating runs and cover SESOI"
severity = "error"
source_check = "check_calibration_sources"
tags = ["prereg", "calibration", "thresholds", "sesoi"]
see_also = ["PREREG-001", "HYG-005"]
+++

When an outcome branch cites a run-derived threshold (`source = "run:<id>:<field>"`), that
reference must resolve to a completed `epistemic_class = 'calibrating'` run (exit code 0,
neither `error` nor `invalid_measurement`) that actually emitted `<field>`. Exploratory
observation source runs cannot be laundered into calibration thresholds.

Additionally, if the sidecar also declares a smallest effect size of interest
(`sesoi:<value>:<why>`) and the calibration run reports a minimum detectable effect (`mde`)
or noise floor larger than that SESOI, the experiment is underpowered by construction.

**What to do.** Source `run:<id>:<field>` only from a dedicated, passing `[calibrate]` run
that measures the baseline noise floor, and verify that any declared SESOI exceeds the
calibration MDE.
