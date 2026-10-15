+++
id = "HYG-005"
title = "A sidecar's stage must match its shape, or it is recorded as something it is not"
severity = "error"
source_check = "check_epistemic_sidecars"
tags = ["hygiene", "stages", "epistemic-class"]
see_also = ["HYG-002"]
+++

Every run stores an immutable `epistemic_class` resolved from its sidecar at start:
`exploratory`, `calibrating`, `confirmatory` or `legacy`. Exploratory and calibrating runs store
`outcome = 'observed'`, keep any `[outcomes]` label only as `tentative_outcome`, and never satisfy
a pass gate or move a campaign e-value. The class follows the sidecar's *shape*, not just its
`stage_name`:

- `stage_name = "exploration"` needs an `[observe]` block; `"calibration"` needs
  `[calibrate] quantity = "..."`.
- **ERROR — non-canonical `stage_name`.** A typo such as `"validaton"` fails closed: the run is
  recorded as `confirmatory`, held to full pre-registration. Fix the spelling.
- **WARNING — `restage`.** `exploration`/`calibration` without its block. On a stem with prior
  runs it keeps today's `legacy` behaviour; on a new stem the validator refuses it. Add the block,
  or choose a confirmatory stage if the cutoffs are real.
- **WARNING — `malformed_source`.** A `source` on a confirmatory or legacy branch that is not
  `lit:<ref>`, `run:<id>:<field>`, `sesoi:<value>:<why>`, `provisional:<why>` or `card:<id>`.

**What to do.** Decide what the run is. If you do not yet know the right quantity or cutoff, it
is exploration: declare `question` and `[observe]`, and drop or comment out thresholds. If you do,
it is confirmatory: name the stage and cite each cutoff's source.
