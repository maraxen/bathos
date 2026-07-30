+++
id = "DSGN-003"
title = "A pass condition with no adversarial check has nothing trying to falsify it"
severity = "warning"
applies_when = "has_pass_branch = true AND n_adversarial_checks = 0"
source_check = "check_adversarial_checks"
tags = ["design", "falsification", "outcomes"]
see_also = ["DSGN-004", "DSGN-001"]
+++

An `outcomes.pass` branch says what success looks like. An `adversarial_check` on it says what
would have to be true for that success to be spurious — and asserts it is not.

Pre-registration without one is only half the discipline: it fixes the success criterion in
advance, which prevents moving the goalposts, but it does nothing to catch a criterion that
was too easy to begin with.

**What to do.** Add a condition designed to *falsify* the hypothesis, not to confirm it.
Useful shapes: a sanity invariant that must hold, a control arm that must not fire, a
magnitude bound that a trivial artifact would exceed.

**Honest limit.** The lint only checks the field is present. Presence is a syntactic proxy;
whether the check actually strengthens the claim is a judgement no lint can make.

**Scope of `applies_when`.** Requires an outcome labelled exactly `pass`, because
`check_adversarial_checks` inspects only that branch. A sidecar with no `pass` branch produces
no finding from the underlying lint, so the card must not fire either.
