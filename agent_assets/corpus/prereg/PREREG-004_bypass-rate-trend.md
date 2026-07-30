+++
id = "PREREG-004"
title = "A rising sidecar-bypass rate means the discipline is being routed around"
severity = "warning"
source_check = "check_bypass_trend"
tags = ["prereg", "bypass", "process"]
see_also = ["PREREG-002"]
+++

`--no-sidecar` exists so the gate never blocks genuinely exploratory work. A bypass rate that
rises week over week is a different signal: the gate is being avoided rather than used.

Worth reading as a design symptom before a discipline one. A gate that fires spuriously, or
demands ceremony disproportionate to the run, will be bypassed by any reasonable person — and
the bypass rate is the only place that shows up.

**What to do.** Look at what is being bypassed before asking anyone to stop. If a whole class
of run is routinely bypassed, either that class needs a lighter sidecar or the gate needs to
not apply to it.
