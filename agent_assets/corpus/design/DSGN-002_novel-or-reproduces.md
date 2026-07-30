+++
id = "DSGN-002"
title = "A validation or production experiment must say whether it is novel or reproducing"
severity = "error"
applies_when = "sidecar_kind = 'experiment' AND stage_name IN ('validation','production') AND has_reproduction = false AND declares_novel = false"
source_check = "check_novel_or_reproduces_declared"
tags = ["design", "literature", "provenance"]
see_also = ["DSGN-005"]
+++

At the `validation` and `production` stages an experiment must declare either a
`[reproduction]` block — naming `reproduces_paper` or `reproduces_run` — or `novel = true`.

The point is not bookkeeping. These two cases have different evidentiary burdens: a
reproduction inherits a prior result's context and can be checked against it, while a novel
claim carries its own weight entirely. A sidecar that declares neither leaves the reader
unable to tell which standard to apply.

**What to do.** Add `[reproduction]` with the DOI or prior run id, or set `novel = true`.
Declaring `novel` is not a lesser option — it is a claim, and stating it makes it reviewable.

**Enforcement.** This is the file's one genuinely blocking check (ERROR severity, AC-7).
