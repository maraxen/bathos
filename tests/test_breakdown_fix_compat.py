# ruff: noqa: F811
"""Remediation group 8: pin the intended fail-closed behaviour for claim-bound members whose
run-time sidecar cannot be verified (old, imported or repaired runs): refused at conclude with
SIDECAR_UNVERIFIABLE, never waved through. Spec 261026 D3 (`Unresolvable` is a refusal)."""

from __future__ import annotations

import pytest

from bathos.campaigns import BreakdownRefused, conclude_campaign
from bathos.errors import BathosErrorCode
from tests.test_breakdown_gate import (  # noqa: F401
    _OFFENDING,
    _WAIVED,
    _bound_campaign,
    _db,
    _record_run,
    _write_sidecar,
    cat,
)


def _conclude(cat, tmp_path, campaign):
    db = _db(cat)
    try:
        conclude_campaign(db, campaign.id, "pass", "c", workspace_root=tmp_path, catalog_dir=cat)
    finally:
        db.close()


def test_a_member_with_a_sidecar_path_but_no_recorded_sha_is_refused_at_conclude(cat, tmp_path):
    sidecar = _write_sidecar(tmp_path, _WAIVED)  # even a perfectly waived sidecar
    campaign = _bound_campaign(cat, tmp_path)
    _record_run(cat, campaign, sidecar, sidecar_text=None, stamp_campaign=True)  # sha ""
    with pytest.raises(BreakdownRefused) as exc:
        _conclude(cat, tmp_path, campaign)
    assert exc.value.code is BathosErrorCode.SIDECAR_UNVERIFIABLE
    assert "no sidecar sha256" in str(exc.value)


def test_an_edited_live_sidecar_with_no_pin_is_refused_at_conclude(cat, tmp_path):
    sidecar = _write_sidecar(tmp_path, _OFFENDING)
    campaign = _bound_campaign(cat, tmp_path)
    _record_run(cat, campaign, sidecar, sidecar_text=_OFFENDING, stamp_campaign=True)
    sidecar.write_text(_WAIVED)  # repaired after the fact, never pinned
    with pytest.raises(BreakdownRefused) as exc:
        _conclude(cat, tmp_path, campaign)
    assert exc.value.code is BathosErrorCode.SIDECAR_UNVERIFIABLE
    assert "sha256 mismatch" in str(exc.value)


def test_the_refusal_leaves_the_campaign_open(cat, tmp_path):
    sidecar = _write_sidecar(tmp_path, _WAIVED)
    campaign = _bound_campaign(cat, tmp_path)
    _record_run(cat, campaign, sidecar, sidecar_text=None, stamp_campaign=True)
    with pytest.raises(BreakdownRefused):
        _conclude(cat, tmp_path, campaign)
    db = _db(cat)
    try:
        assert db.execute(
            "SELECT status FROM campaigns WHERE id = ?", [campaign.id]
        ).fetchone() == ("open",)
    finally:
        db.close()
