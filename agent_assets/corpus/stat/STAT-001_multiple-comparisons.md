+++
id = "STAT-001"
title = "OR-ing several significance tests inflates the family-wise false-positive rate"
severity = "warning"
applies_when = "n_pass_branches >= 3"
source_check = "check_multiple_comparisons"
tags = ["statistics", "outcomes", "false-positives"]
see_also = ["STAT-004", "PREREG-001"]
+++

An outcome condition that fires when *any* of N significance tests passes is a union, and a
union of N tests at level alpha has a family-wise false-positive rate of roughly
`1 - (1 - alpha)^N`. At alpha = 0.05 and N = 10 that is about 40%, not 5%.

AND-joined comparisons are **not** the anti-pattern and are deliberately excluded: requiring
every test to fire is conservative, not permissive. Only OR-chains inflate the rate.

**What to do.** Apply a correction — `bathos.stats_gates.holm_correction` — or, if the union
is intentional and defensible, declare `multiple_comparisons_correction` on the outcome to
record that the choice was made rather than overlooked.

**Provenance.** Motivated by a real incident (debt #1071): a gate running 90 tests that
initially fired on a single uncorrected p = 0.0497.

**Note on `applies_when`.** The lint itself parses outcome conditions for OR-chained p-value
terms. The context row exposes no such parse, so this card fires on a coarser proxy — three
or more pass-direction branches. Treat a hit as "worth checking", not as a detection.
