"""Remediation group 2 (spec 261026 D5 rendering): `amendments_by_run` keys a campaign-scoped
postmortem on each amendment's `amends_run_id` and matches a prefix-named run against full member
ids, never matching on an empty prefix."""

from __future__ import annotations

from pathlib import Path

from bathos.postmortem import amendments_by_run

_FULL_A = "a1b2c3d4-0000-0000-0000-000000000001"
_FULL_B = "b2c3d4e5-0000-0000-0000-000000000002"

_AMEND = (
    '[[amendments]]\nanalysis_run_id = "797b791e"\nkind = "reanalysis"\n'
    'effect = "verdict_unchanged"\n{extra}'
)


def _write(tmp_path: Path, name: str, head: str, amendment: str) -> None:
    (tmp_path / f"{name}.bth.postmortem.toml").write_text(
        head + '[postmortem]\nhypothesis_status = "inconclusive"\n' + amendment
    )


def test_a_campaign_scoped_postmortem_is_keyed_on_each_amends_run_id(tmp_path):
    _write(
        tmp_path,
        "camp",
        'campaign_id = "camp-1"\nformat = 2\n',
        _AMEND.format(extra='amends_run_id = "a1b2c3d4"\n')
        + _AMEND.format(extra=f'amends_run_id = "{_FULL_B}"\n'),
    )
    found = amendments_by_run(tmp_path, [_FULL_A, _FULL_B])
    assert sorted(a["run_id"] for a in found) == [_FULL_A, _FULL_B]


def test_a_campaign_amendment_for_a_non_member_is_dropped(tmp_path):
    _write(
        tmp_path,
        "camp",
        'campaign_id = "camp-1"\nformat = 2\n',
        _AMEND.format(extra='amends_run_id = "zzzzzzzz"\n'),
    )
    assert amendments_by_run(tmp_path, [_FULL_A, _FULL_B]) == []


def test_a_prefix_named_run_postmortem_matches_the_full_member_id(tmp_path):
    _write(tmp_path, "run", 'run_id = "a1b2c3d4"\nformat = 2\n', _AMEND.format(extra=""))
    (found,) = amendments_by_run(tmp_path, [_FULL_A, _FULL_B])
    assert found["run_id"] == _FULL_A and found["analysis_run_id"] == "797b791e"


def test_an_ambiguous_prefix_matches_nothing(tmp_path):
    other = "a1b2c3d4-9999-0000-0000-000000000003"
    _write(tmp_path, "run", 'run_id = "a1b2c3d4"\nformat = 2\n', _AMEND.format(extra=""))
    assert amendments_by_run(tmp_path, [_FULL_A, other]) == []


def test_an_empty_amends_run_id_never_matches_every_member(tmp_path):
    _write(
        tmp_path,
        "camp",
        'campaign_id = "camp-1"\nformat = 2\n',
        _AMEND.format(extra='amends_run_id = ""\n'),
    )
    assert amendments_by_run(tmp_path, [_FULL_A, _FULL_B]) == []
