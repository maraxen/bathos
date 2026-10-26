+++
id = "STAT-005"
title = "A worst-case aggregate gates the verdict with no per-unit breakdown"
severity = "warning"
applies_when = "has_uncovered_aggregate"
source_check = "check_aggregate_without_breakdown"
tags = ["statistics", "aggregate", "breakdown"]
see_also = ["STAT-003", "STAT-004"]
+++

An outcome condition (or an `[observe].sanity` check) reads a maximum, minimum or peak taken
over many units, and the sidecar declares no `[breakdown]`. The verdict then rests on one
number that says nothing about how many units produced it.

**The incident (lathex bth 78db9e04).** An exploration run gated on `e_max_placement` = 0.521 A
against a 0.5 A bound. "Marginal" was the human reading. A per-binder breakdown afterwards
(bth 797b791e) showed the maximum came from 1 binder of 24; the median binder maximum was
0.004 A. The deciding statistic was `n_binders_over`, 1-2 versus 3 or more, and nothing in the
sidecar had asked for it.

**What to do.** Declare `[breakdown]` with a `unit` and
`[breakdown.aggregates.<column>]` (`direction`, optional `bound` and `margin`). The run then
emits `<column>__worst_unit`, `__second`, `__median`, `__n_units` and, with a `bound`,
`__n_over`. Read `n_over`, `second` and `median` before the maximum. If the maximum is
deliberately the whole question (a calibration, an internal check nothing is built on), say so
with `[breakdown] waive = "reason"`. The waiver is read from the run-time sidecar only, so a
later waiver does not cover a run that already happened.

**Known misses.** Detection is by name (max/min/worst/peak) over float columns plus an explicit
opt-in, `[result_schema_meta.<column>] aggregate = true`. A derived ratio, or a boolean computed
from a maximum, is not recognised without the opt-in.
