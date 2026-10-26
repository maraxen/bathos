# ruff: noqa: F811
"""Remediation group 1 (spec 261026 D3 "Visibility"): `bth campaign review` lists each member's
run-time `[breakdown] waive`, read from the sidecar that was in force when the run happened."""

from __future__ import annotations

import io
from types import SimpleNamespace

from rich.console import Console

from bathos.campaigns import review_campaign
from bathos.rich_fmt import render_campaign_review
from tests.test_breakdown_gate import (  # noqa: F401
    _COVERED,
    _SUMMARY,
    _WAIVED,
    _bound_campaign,
    _db,
    _record_run,
    _write_sidecar,
    cat,
)

_REASON = "calibration; nothing is built on the maximum"


def _review(cat, tmp_path):
    campaign = _bound_campaign(cat, tmp_path)
    waived = _write_sidecar(tmp_path, _WAIVED, "waived.bth.toml")
    r_waived = _record_run(cat, campaign, waived, sidecar_text=_WAIVED, stamp_campaign=True)
    covered = _write_sidecar(tmp_path, _COVERED, "covered.bth.toml")
    r_covered = _record_run(
        cat,
        campaign,
        covered,
        sidecar_text=_COVERED,
        metadata={"e_max_placement": 0.2, **_SUMMARY},
        stamp_campaign=True,
    )
    db = _db(cat)
    try:
        review = review_campaign(db, campaign.id, cat, workspace_root=tmp_path)
    finally:
        db.close()
    return review, r_waived, r_covered


def test_a_waived_member_shows_its_reason_and_an_unwaived_one_nothing(cat, tmp_path):
    review, r_waived, r_covered = _review(cat, tmp_path)
    assert review["waivers"] == [{"run_id": r_waived.id, "reason": _REASON}]
    assert all(w["run_id"] != r_covered.id for w in review["waivers"])


def test_a_post_hoc_waiver_on_the_live_file_is_not_listed(cat, tmp_path):
    campaign = _bound_campaign(cat, tmp_path)
    sidecar = _write_sidecar(tmp_path, _COVERED, "late.bth.toml")
    _record_run(
        cat,
        campaign,
        sidecar,
        sidecar_text=_COVERED,
        metadata={"e_max_placement": 0.2, **_SUMMARY},
        stamp_campaign=True,
    )
    sidecar.write_text(_WAIVED)  # edited after the run: the run-time sidecar had no waiver
    db = _db(cat)
    try:
        review = review_campaign(db, campaign.id, cat, workspace_root=tmp_path)
    finally:
        db.close()
    assert review["waivers"] == []


def test_without_a_workspace_the_key_is_empty(cat, tmp_path):
    campaign = _bound_campaign(cat, tmp_path)
    sidecar = _write_sidecar(tmp_path, _WAIVED, "w.bth.toml")
    _record_run(cat, campaign, sidecar, sidecar_text=_WAIVED, stamp_campaign=True)
    db = _db(cat)
    try:
        assert review_campaign(db, campaign.id, cat)["waivers"] == []
    finally:
        db.close()


def test_the_renderer_shows_waivers(cat, tmp_path):
    review, r_waived, _ = _review(cat, tmp_path)
    out = io.StringIO()
    render_campaign_review(
        SimpleNamespace(name="rev", mode="confirmation"),
        review,
        console=Console(file=out, width=140),
    )
    text = out.getvalue()
    assert "waivers" in text and r_waived.id[:8] in text and "calibration" in text
