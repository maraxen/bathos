+++
id = "HYG-001"
title = "Outputs written under /tmp are lost on reboot"
severity = "warning"
source_check = "check_ephemeral_output_paths"
tags = ["hygiene", "outputs", "reproducibility"]
see_also = ["HYG-003"]
+++

A run that registers an output path under `/tmp`, `/var/tmp` or the system temp dir has
recorded provenance pointing at a file that will not survive a reboot. The catalog entry
outlives the artifact it references.

The failure is delayed and total: everything looks correct until someone tries to re-read the
output, by which time the run's own context is gone too.

**What to do.** Write outputs under a persistent project path. If a run genuinely produces
nothing worth keeping, register no output rather than a temporary one.
