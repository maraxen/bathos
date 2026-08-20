+++
id = "PREREG-005"
title = "A [status] stale flag with no reason is as unjustified as an unmarked threshold"
severity = "warning"
source_check = "check_stale_scripts_without_reason"
tags = ["prereg", "staleness", "justification"]
see_also = ["PREREG-001", "HYG-004"]
+++

`[status] stale = true` tells `bth run`'s pre-registration gate to refuse the script (in both
collaborative and autonomous mode) unless `--allow-stale` is passed — a script marked stale is
treated as a *live danger*, not mere clutter, because a stale-but-unmarked default is worse than
no script at all: it still executes.

That refusal is only actionable if the deny message says why. `stale_reason` is that message —
the same justification discipline as `PREREG-001`'s unmarked-threshold case, applied to "is this
script safe to run" instead of "is this outcome boundary derived."

**What to do.** Set `stale_reason` to a short, concrete reason ("wrong default for
`--input`, replaced by `run_thing_v2`" beats "outdated"). Optionally set `superseded_by` to the
run id, claim id, or archive item id that replaced it. Once the script is fixed, remove the
`[status]` block (or set `stale = false`) rather than leaving a stale flag with a stale reason.
