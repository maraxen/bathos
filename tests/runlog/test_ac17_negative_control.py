"""AC-17 negative control: prove the differential test's new claim-tier
coverage (`register_claim`/`attest_parity`, delivery item 3) actually bites.

A differential test that never fails is not evidence of parity -- it may
simply never be exercising the comparison it claims to. This monkeypatches
the NEW fold's own `campaign.claim_bound` handling to a no-op (dropping
`register_claim`'s effect from the new side only, leaving the legacy
reference untouched) and asserts `test_ac17_differential`'s own
`_run_differential` helper -- the exact machinery `test_ac17_differential_
fold` runs -- reports a mismatch, on a seed chosen because `ac17_gen.
generate_ops(2)` is known (as of this delivery) to produce a `register_claim`
op and no `attest_parity` op. (A seed covering both -- e.g. 4 -- was tried
first and does prove the same coverage, just one step earlier and via a
different exception: with the fold broken, `attest_parity`'s own AC-25
precondition check ("Campaign ... has no registered claim") raises
`RuntimeError` before the ops sequence even finishes executing, since the
new backend's `register_claim` effect never fully took hold. That is
consistent with AC-17 catching the same underlying regression, but this
file wants the specific `AssertionError`-mismatch-reporting path the task
asks for, so it isolates `register_claim` alone.)

Target: `bathos.runlog.fold_campaigns._GENERAL_CAMPAIGN_KINDS`, the frozenset
`_general_merge` iterates to decide which campaign-entity event kinds it
folds as ordinary general fields (`claim_path`/`claim_sha256`/`claim_mode`
all arrive exclusively via `campaign.claim_bound` events, per
`bathos.claim.register_claim`/`attest_parity`). Removing that one kind from
the set is the smallest possible "drop the effect" edit: every OTHER
campaign field (name, status, started_at, ...) still folds normally, so a
failure here is specifically attributable to claim-tier coverage, not a
blunt "break everything" patch that would pass for the wrong reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bathos.runlog import fold_campaigns

from .ac17_gen import generate_ops
from .test_ac17_differential import _run_differential

# generate_ops(2) is known (as of this delivery) to produce exactly one
# register_claim op and NO attest_parity op -- see the module docstring for
# why this file deliberately avoids a seed that covers both.
_SEED = 2


def test_seed_precondition_covers_register_claim() -> None:
    """Guard the precondition the negative control below relies on: if
    `ac17_gen.py` ever changes its randomization such that seed 2 stops
    covering `register_claim` (or starts also covering `attest_parity`,
    which would reintroduce the RuntimeError-before-comparison path the
    module docstring explains), this fails loudly (a clear signal to pick a
    new seed) rather than the negative control below silently passing for an
    unrelated reason (or silently testing nothing)."""
    ops = generate_ops(_SEED)
    kinds = {op["kind"] for op in ops}
    assert "register_claim" in kinds, (
        f"seed {_SEED} no longer generates register_claim; pick a new seed for "
        "the negative control below"
    )
    assert "attest_parity" not in kinds, (
        f"seed {_SEED} now also generates attest_parity, which raises RuntimeError "
        "before the comparison runs (see module docstring); pick a register_claim-only "
        "seed for the negative control below"
    )


def test_negative_control_broken_claim_bound_fold_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken_kinds = frozenset(fold_campaigns._GENERAL_CAMPAIGN_KINDS - {"campaign.claim_bound"})
    monkeypatch.setattr(fold_campaigns, "_GENERAL_CAMPAIGN_KINDS", broken_kinds)

    with pytest.raises(AssertionError) as excinfo:
        _run_differential(tmp_path, monkeypatch, _SEED)

    message = str(excinfo.value)
    assert "claim_path" in message or "claim_sha256" in message, (
        f"the differential DID fail, but not for the expected claim-tier reason -- got:\n{message}"
    )
