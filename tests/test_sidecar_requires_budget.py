"""Parsing and validation of the optional [requires] and [budget] sidecar tables (spec 261016 B1).

Parsing only: enforcement of these tables is a separate gate and is not tested here.
"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest

from bathos.sidecar import (
    BudgetBlock,
    RequiresBlock,
    RequiresWaiver,
    SidecarError,
    parse_sidecar,
)

BASE_OUTCOMES = """
[outcomes.pass]
condition = "x < 5"
decision = "go"
reasoning = "r"

[outcomes.fail]
condition = "x >= 5"
decision = "stop"
reasoning = "r"
is_residual = true

[result_schema]
x = "float"
"""


def make(tmp_path: Path, extra: str, kind: str = "experiment") -> Path:
    p = tmp_path / "s.bth.toml"
    if kind == "experiment":
        head = '[experiment]\nhypothesis = "h"\n'
    else:
        head = f'[{kind}]\nproperty = "p"\n'
    p.write_text(
        textwrap.dedent(head) + extra + BASE_OUTCOMES,
        encoding="utf-8",
    )
    return p


# ── absent tables ────────────────────────────────────────────────────────────


def test_absent_tables_are_none(tmp_path):
    sc = parse_sidecar(make(tmp_path, ""))
    assert sc.requires is None
    assert sc.budget is None


# ── [requires] ───────────────────────────────────────────────────────────────

FULL_REQUIRES = """
[requires]
workload = "jax"
artifact = "out/*.json"
json_key = "meta.git.sha"
must_equal = "HEAD"
reject_suffixes = ["-dirty"]
applies_if = "gpu == true"

[requires.waiver]
reason = "pinned by reviewer on 261016"
"""


def test_requires_full_table_round_trips(tmp_path):
    sc = parse_sidecar(make(tmp_path, FULL_REQUIRES))
    assert sc.requires == RequiresBlock(
        workload="jax",
        artifact="out/*.json",
        json_key="meta.git.sha",
        must_equal="HEAD",
        reject_suffixes=["-dirty"],
        applies_if="gpu == true",
        waiver=RequiresWaiver(reason="pinned by reviewer on 261016"),
    )


def test_requires_reject_suffixes_default(tmp_path):
    sc = parse_sidecar(make(tmp_path, '[requires]\nworkload = "jax"\n'))
    assert sc.requires is not None
    assert sc.requires.reject_suffixes == ["-dirty", "-unverified"]
    assert sc.requires.waiver is None
    assert sc.requires.must_equal is None


def test_empty_requires_table_is_present_with_defaults(tmp_path):
    sc = parse_sidecar(make(tmp_path, "[requires]\n"))
    assert sc.requires == RequiresBlock()


def test_requires_waiver_empty_reason_is_error(tmp_path):
    with pytest.raises(SidecarError, match=r"requires\.waiver\]\.reason"):
        parse_sidecar(make(tmp_path, '[requires.waiver]\nreason = "   "\n'))


def test_requires_waiver_missing_reason_is_error(tmp_path):
    with pytest.raises(SidecarError, match=r"reason"):
        parse_sidecar(make(tmp_path, "[requires.waiver]\n"))


def test_json_key_without_artifact_is_error(tmp_path):
    with pytest.raises(SidecarError, match=r"json_key.*artifact"):
        parse_sidecar(make(tmp_path, '[requires]\njson_key = "a.b"\n'))


def test_must_equal_without_json_key_is_error(tmp_path):
    with pytest.raises(SidecarError, match=r"must_equal.*json_key"):
        parse_sidecar(make(tmp_path, '[requires]\nartifact = "x.json"\nmust_equal = "HEAD"\n'))


def test_requires_wrong_type_is_error(tmp_path):
    with pytest.raises(SidecarError, match=r"\[requires\]\.workload must be a string"):
        parse_sidecar(make(tmp_path, "[requires]\nworkload = 3\n"))


def test_requires_reject_suffixes_wrong_type_is_error(tmp_path):
    with pytest.raises(SidecarError, match=r"reject_suffixes"):
        parse_sidecar(make(tmp_path, '[requires]\nreject_suffixes = "-dirty"\n'))


def test_requires_unknown_key_warns_not_errors(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="bathos.sidecar"):
        sc = parse_sidecar(make(tmp_path, '[requires]\nworkload = "jax"\nbogus = 1\n'))
    assert sc.requires is not None
    assert any("bogus" in r.message for r in caplog.records)


# ── [budget] ─────────────────────────────────────────────────────────────────


def test_budget_full_table_round_trips(tmp_path):
    sc = parse_sidecar(
        make(
            tmp_path,
            """
[budget]
est_wall_s = 120.0
per_unit_s = 2.0
n_units = 50
probe_record = "probe/out.json"
tolerance = 0.25
""",
        )
    )
    assert sc.budget == BudgetBlock(
        est_wall_s=120.0,
        per_unit_s=2.0,
        n_units=50,
        probe_record="probe/out.json",
        tolerance=0.25,
    )


def test_budget_tolerance_default(tmp_path):
    sc = parse_sidecar(make(tmp_path, "[budget]\nest_wall_s = 10\n"))
    assert sc.budget is not None
    assert sc.budget.tolerance == 0.5
    assert sc.budget.est_wall_s == 10.0


@pytest.mark.parametrize("value", ["0", "-5", "nan", "inf"])
def test_budget_est_wall_s_must_be_finite_positive(tmp_path, value):
    with pytest.raises(SidecarError, match=r"est_wall_s"):
        parse_sidecar(make(tmp_path, f"[budget]\nest_wall_s = {value}\n"))


def test_budget_bool_n_units_is_error(tmp_path):
    with pytest.raises(SidecarError, match=r"n_units must be an integer"):
        parse_sidecar(make(tmp_path, "[budget]\nn_units = true\n"))


def test_budget_zero_n_units_is_error(tmp_path):
    with pytest.raises(SidecarError, match=r"n_units must be > 0"):
        parse_sidecar(make(tmp_path, "[budget]\nn_units = 0\n"))


def test_budget_bool_est_wall_s_is_error(tmp_path):
    with pytest.raises(SidecarError, match=r"est_wall_s must be a number"):
        parse_sidecar(make(tmp_path, "[budget]\nest_wall_s = true\n"))


@pytest.mark.parametrize("value", ["0", "10.5", "-1"])
def test_budget_tolerance_range(tmp_path, value):
    with pytest.raises(SidecarError, match=r"tolerance"):
        parse_sidecar(make(tmp_path, f"[budget]\ntolerance = {value}\n"))


def test_budget_contradictory_estimate_is_error(tmp_path):
    # 2.0 * 50 = 100 s, tolerance 0.5 -> floor 50 s; est 10 s contradicts its parts.
    with pytest.raises(SidecarError, match=r"contradicts"):
        parse_sidecar(make(tmp_path, "[budget]\nest_wall_s = 10\nper_unit_s = 2.0\nn_units = 50\n"))


def test_budget_consistent_estimate_is_accepted(tmp_path):
    sc = parse_sidecar(
        make(tmp_path, "[budget]\nest_wall_s = 60\nper_unit_s = 2.0\nn_units = 50\n")
    )
    assert sc.budget is not None
    assert sc.budget.n_units == 50


def test_budget_unknown_key_warns_not_errors(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="bathos.sidecar"):
        sc = parse_sidecar(make(tmp_path, "[budget]\nest_wall_s = 5\nbogus = 1\n"))
    assert sc.budget is not None
    assert any("bogus" in r.message for r in caplog.records)


# ── kind restriction ─────────────────────────────────────────────────────────


def test_requires_on_validation_sidecar_is_error(tmp_path):
    with pytest.raises(SidecarError, match=r"only allowed on \[experiment\]"):
        parse_sidecar(make(tmp_path, '[requires]\nworkload = "jax"\n', kind="validation"))


def test_budget_on_validation_sidecar_is_error(tmp_path):
    with pytest.raises(SidecarError, match=r"only allowed on \[experiment\]"):
        parse_sidecar(make(tmp_path, "[budget]\nest_wall_s = 5\n", kind="validation"))
