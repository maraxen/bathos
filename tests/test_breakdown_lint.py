"""Distributional breakdown, T2 (spec 261026 D4): the `check_aggregate_without_breakdown` lint,
its `has_uncovered_aggregate` context column, and the STAT-005 rule card."""

from __future__ import annotations

import textwrap
from pathlib import Path

from bathos.corpus import (
    CONTEXT_COLUMNS,
    build_context,
    evaluate_applies_when,
    load_corpus,
)
from bathos.linter import IssueSeverity, check_aggregate_without_breakdown, lint_project

_SIDECAR = textwrap.dedent("""
    [experiment]
    hypothesis = "h"
    [outcomes.pass]
    condition = "e_max_placement <= 0.5"
    decision = "go"
    reasoning = "r"
    [outcomes.fail]
    condition = "e_max_placement > 0.5"
    decision = "stop"
    reasoning = "r"
    is_residual = true
    [result_schema]
    e_max_placement = "float"
""")

_COVERED_TAIL = textwrap.dedent("""
    e_max_placement__worst_unit = "str"
    e_max_placement__second = "float"
    e_max_placement__median = "float"
    e_max_placement__n_units = "int"
    [breakdown]
    unit = "binder"
    [breakdown.aggregates.e_max_placement]
    direction = "max"
""")


def _project(tmp_path: Path, sidecar: str, subdir: str = "experiments") -> Path:
    d = tmp_path / "scripts" / subdir
    d.mkdir(parents=True, exist_ok=True)
    (d / "probe_placement.py").write_text("# script")
    (d / "probe_placement.bth.toml").write_text(sidecar)
    return d / "probe_placement.py"


def test_uncovered_aggregate_is_flagged_as_a_warning(tmp_path):
    _project(tmp_path, _SIDECAR)
    issues = check_aggregate_without_breakdown(tmp_path)
    assert len(issues) == 1
    assert issues[0].issue == "AGGREGATE_WITHOUT_BREAKDOWN"
    assert issues[0].severity == IssueSeverity.WARNING
    assert "e_max_placement" in issues[0].detail
    assert "STAT-005" in issues[0].detail


def test_covered_waived_and_non_aggregate_sidecars_are_clean(tmp_path):
    _project(tmp_path, _SIDECAR + _COVERED_TAIL)
    assert check_aggregate_without_breakdown(tmp_path) == []
    waived = _SIDECAR.replace("[experiment]", '[breakdown]\nwaive = "calibration"\n[experiment]')
    _project(tmp_path, waived)
    assert check_aggregate_without_breakdown(tmp_path) == []
    _project(tmp_path, _SIDECAR.replace("e_max_placement", "x_value"))
    assert check_aggregate_without_breakdown(tmp_path) == []


def test_unparseable_sidecar_is_skipped(tmp_path):
    _project(tmp_path, "[experiment\n")
    assert check_aggregate_without_breakdown(tmp_path) == []


def test_lint_project_runs_the_check(tmp_path):
    _project(tmp_path, _SIDECAR)
    assert any(i.issue == "AGGREGATE_WITHOUT_BREAKDOWN" for i in lint_project(tmp_path))


def test_has_uncovered_aggregate_context_column(tmp_path):
    script = _project(tmp_path, _SIDECAR)
    assert "has_uncovered_aggregate" in CONTEXT_COLUMNS
    assert build_context(script)["has_uncovered_aggregate"] is True
    script = _project(tmp_path, _SIDECAR + _COVERED_TAIL)
    assert build_context(script)["has_uncovered_aggregate"] is False
    missing = tmp_path / "scripts" / "experiments" / "nothing_here.py"
    assert build_context(missing)["has_uncovered_aggregate"] is False


def test_stat_005_card_is_wired():
    card = {c.id: c for c in load_corpus().cards}["STAT-005"]
    assert card.source_check == "check_aggregate_without_breakdown"
    assert card.applies_when == "has_uncovered_aggregate"
    assert evaluate_applies_when(card.applies_when, {"has_uncovered_aggregate": True})
    assert not evaluate_applies_when(card.applies_when, {"has_uncovered_aggregate": False})
    assert "78db9e04" in card.body


def test_linter_shares_the_sidecar_column_matcher():
    from bathos import linter, sidecar

    assert linter._columns_referenced is sidecar.columns_referenced
