"""Distributional breakdown, T5 (spec 261026 D5): `format = 2` postmortems, `ValidationResult.warnings`
and `run_lookup`. Format-1 files must behave exactly as before, drift included."""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from bathos.postmortem import (
    Postmortem,
    ValidationResult,
    parse_postmortem,
    validate_postmortem,
)

_BASE = textwrap.dedent("""
    run_id = "abcdef12"
    {fmt}
    [postmortem]
    hypothesis_status = "inconclusive"
    verdict_override = "{override}"
    summary = "s"
    {extra}
""")


def _write(tmp_path: Path, fmt: str = "", override: str = "none", extra: str = "") -> Path:
    p = tmp_path / "pm.bth.postmortem.toml"
    p.write_text(_BASE.format(fmt=fmt, override=override, extra=extra))
    return p


_AMENDMENT = textwrap.dedent("""
    [[amendments]]
    analysis_run_id = "797b791e"
    kind = "distributional_outlier"
    outlier_units = ["binder_17"]
    effect = "verdict_contextualized"
    note = "1 of 24 binders over"
""")
_FORK = textwrap.dedent("""
    [fork]
    choice = "stop"
    reason = "internal validation only"
    cited = false
    follow_up_run_ids = []
""")


def _append(path: Path, text: str) -> Path:
    path.write_text(path.read_text() + text)
    return path


# ── parsing ──────────────────────────────────────────────────────────────────────────


def test_a_file_without_format_parses_as_format_1_with_no_fork_or_amendments(tmp_path):
    pm = parse_postmortem(_write(tmp_path))
    assert pm.format == 1
    assert pm.fork == {}
    assert pm.amendments == []


def test_format_2_parses_fork_and_amendments(tmp_path):
    path = _append(_write(tmp_path, fmt="format = 2"), _FORK + _AMENDMENT)
    pm = parse_postmortem(path)
    assert pm.format == 2
    assert pm.fork["choice"] == "stop" and pm.fork["cited"] is False
    (amendment,) = pm.amendments
    assert amendment.analysis_run_id == "797b791e"
    assert amendment.amends_run_id == ""
    assert amendment.kind == "distributional_outlier"
    assert amendment.outlier_units == ["binder_17"]
    assert amendment.effect == "verdict_contextualized"
    assert amendment.note == "1 of 24 binders over"


@pytest.mark.parametrize("bad", ['format = "two"', "format = 3", "format = true", "format = 0"])
def test_an_unsupported_format_is_a_parse_error(tmp_path, bad):
    with pytest.raises(ValueError, match="format"):
        parse_postmortem(_write(tmp_path, fmt=bad))


def test_a_malformed_amendments_shape_is_a_parse_error(tmp_path):
    path = _write(tmp_path, fmt="format = 2", extra="")
    _append(path, '[amendments]\nanalysis_run_id = "x"\n')  # a table, not an array of tables
    with pytest.raises(ValueError, match="amendments"):
        parse_postmortem(path)


# ── validation: warnings channel, format-1 unchanged ─────────────────────────────────


def test_validation_result_has_a_warnings_channel_defaulting_empty():
    assert ValidationResult(ok=True).warnings == []


def _drifted_run():
    return SimpleNamespace(git_dirty=True, git_hash="old")


def test_format_1_is_unchanged_under_drift_and_never_calls_run_lookup(tmp_path):
    pm = parse_postmortem(_write(tmp_path))
    calls: list[str] = []
    with_lookup = validate_postmortem(
        pm, workspace_root=tmp_path, run=_drifted_run(), run_lookup=lambda rid: calls.append(rid)
    )
    without = validate_postmortem(pm, workspace_root=tmp_path, run=_drifted_run())
    assert [e.message for e in with_lookup.errors] == [e.message for e in without.errors]
    assert any("git_dirty" in e.message for e in without.errors)  # drift still errors in format 1
    assert with_lookup.warnings == without.warnings == []
    assert calls == []


def test_format_1_with_fork_or_amendments_warns_that_it_is_not_enforced(tmp_path):
    path = _append(_write(tmp_path), _FORK + _AMENDMENT)
    result = validate_postmortem(parse_postmortem(path), workspace_root=tmp_path)
    assert result.ok
    assert any("not enforced" in w and "format = 2" in w for w in result.warnings)


def test_format_2_drift_block_does_not_newly_fire_without_a_run(tmp_path):
    """`run_lookup` feeds format-2 checks only; it does not make the drift block fire."""
    pm = parse_postmortem(_write(tmp_path, fmt="format = 2"))
    result = validate_postmortem(pm, workspace_root=tmp_path, run_lookup=lambda _rid: None)
    assert result.ok and result.errors == []


def test_postmortem_dataclass_defaults_are_format_1():
    pm = Postmortem(run_id="r")
    assert (pm.format, pm.fork, pm.amendments) == (1, {}, [])


def test_a_fork_alone_without_amendments_is_inert_in_format_2(tmp_path):
    """[fork] with nothing discharged is only checked when a near_bound id is named."""
    path = _append(_write(tmp_path, fmt="format = 2"), _FORK)
    pm = parse_postmortem(path)
    assert pm.fork["choice"] == "stop" and pm.amendments == []
    result = validate_postmortem(pm, workspace_root=tmp_path)
    assert result.ok and result.warnings == []


def test_a_fork_alone_in_format_1_warns_not_enforced(tmp_path):
    path = _append(_write(tmp_path), _FORK)
    result = validate_postmortem(parse_postmortem(path), workspace_root=tmp_path)
    assert result.ok
    assert any("not enforced" in w for w in result.warnings)


def test_a_non_table_fork_is_a_parse_error(tmp_path):
    path = _write(tmp_path, fmt="format = 2", extra="")
    text = path.read_text().replace("[postmortem]", 'fork = "stop"\n[postmortem]', 1)
    path.write_text(text)  # a top-level `fork = "stop"`, not a [fork] table
    with pytest.raises(ValueError, match="fork must be a table"):
        parse_postmortem(path)
