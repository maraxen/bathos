+++
id = "DSGN-005"
title = "A benchmark's baseline_ref must resolve to a real run"
severity = "warning"
source_check = "check_baseline_ref_exists"
tags = ["design", "benchmark", "provenance"]
see_also = ["DSGN-002"]
+++

A `[benchmark].baseline_ref` names the run the benchmark is measured against. If it does not
resolve in the warm catalog, the comparison has no anchor — the regression threshold is being
applied against nothing.

This fails quietly. A benchmark with an unresolvable baseline still runs, still produces
numbers, and still reports an outcome; what it cannot do is tell you whether those numbers
got worse.

**What to do.** Check the run id, and `bth compact` if the run exists in the cool tier but has
not reached the warm catalog yet.
