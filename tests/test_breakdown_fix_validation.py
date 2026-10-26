"""Remediation group 5: validation tightening (spec 261026 D1 waive, D5 outlier_units)."""

from __future__ import annotations

from bathos.postmortem import parse_postmortem, validate_postmortem
from bathos.sidecar import uncovered_aggregates
from bathos.validate import validate_sidecar
from tests.test_breakdown_sidecar import _sidecar


def test_a_whitespace_only_waive_covers_nothing_and_fails_validation(tmp_path):
    sc, path = _sidecar(tmp_path, {"e_max_placement": "float"}, '[breakdown]\nwaive = "   "\n')
    assert [u.column for u in uncovered_aggregates(sc)] == ["e_max_placement"]
    messages = [e.message for e in validate_sidecar(sc, path).errors]
    assert any("waive" in m and "blank" in m for m in messages), messages


def test_an_empty_waive_is_blank_too(tmp_path):
    sc, path = _sidecar(tmp_path, {"e_max_placement": "float"}, '[breakdown]\nwaive = ""\n')
    assert uncovered_aggregates(sc)
    assert any("waive" in e.message for e in validate_sidecar(sc, path).errors)


def test_a_real_waive_reason_still_covers(tmp_path):
    sc, path = _sidecar(tmp_path, {"e_max_placement": "float"}, '[breakdown]\nwaive = "why"\n')
    assert uncovered_aggregates(sc) == []
    assert validate_sidecar(sc, path).ok


def _pm(tmp_path, units: str):
    path = tmp_path / "pm.bth.postmortem.toml"
    path.write_text(
        'run_id = "abcdef12"\nformat = 2\n[postmortem]\nhypothesis_status = "inconclusive"\n'
        '[[amendments]]\nanalysis_run_id = "797b791e"\nkind = "distributional_outlier"\n'
        f'effect = "verdict_unchanged"\noutlier_units = {units}\n'
    )
    return parse_postmortem(path)


def test_outlier_units_as_a_bare_string_is_a_validation_error_not_characters(tmp_path):
    pm = _pm(tmp_path, '"binder_17"')
    result = validate_postmortem(pm, workspace_root=tmp_path)
    assert not result.ok
    assert any("outlier_units" in e.message and "list" in e.message for e in result.errors)
    assert pm.amendments[0].outlier_units == []  # never split into "b", "i", "n", ...


def test_outlier_units_with_non_string_items_is_an_error(tmp_path):
    result = validate_postmortem(_pm(tmp_path, "[17, 18]"), workspace_root=tmp_path)
    assert any("outlier_units" in e.message for e in result.errors)


def test_a_proper_outlier_units_list_validates(tmp_path):
    assert validate_postmortem(_pm(tmp_path, '["binder_17"]'), workspace_root=tmp_path).ok
