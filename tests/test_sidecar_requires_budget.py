"""Parsing and validation of the optional [requires] and [budget] sidecar tables (spec 261016 B1).

Parsing only: enforcement of these tables is a separate gate and is not tested here.
"""

from __future__ import annotations

import dataclasses
import json
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
    with pytest.raises(SidecarError, match=r"waiver\]\.reason must be a non-empty string"):
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


@pytest.mark.parametrize("value", ["0", "1.0", "1", "10.5", "-1"])
def test_budget_tolerance_range(tmp_path, value):
    with pytest.raises(SidecarError, match=r"tolerance must be finite and in \(0, 1\)"):
        parse_sidecar(make(tmp_path, f"[budget]\ntolerance = {value}\n"))


@pytest.mark.parametrize("value", ["true", "nan", "inf", "-inf"])
def test_budget_tolerance_rejects_bool_nan_inf(tmp_path, value):
    with pytest.raises(SidecarError, match=r"tolerance"):
        parse_sidecar(make(tmp_path, f"[budget]\ntolerance = {value}\n"))


def test_budget_tolerance_just_below_one_is_accepted(tmp_path):
    sc = parse_sidecar(make(tmp_path, "[budget]\nest_wall_s = 5\ntolerance = 0.99\n"))
    assert sc.budget is not None
    assert sc.budget.tolerance == 0.99


def test_budget_tolerance_default_is_in_range(tmp_path):
    sc = parse_sidecar(make(tmp_path, "[budget]\nest_wall_s = 5\n"))
    assert sc.budget is not None
    assert 0 < sc.budget.tolerance < 1


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


# ── degenerate values (review 261016 B1) ─────────────────────────────────────


def make_top(tmp_path: Path, top: str) -> Path:
    """Write a file with a top-level line (must precede any table header)."""
    p = tmp_path / "s.bth.toml"
    p.write_text(
        top + "\n" + '[experiment]\nhypothesis = "h"\n' + BASE_OUTCOMES,
        encoding="utf-8",
    )
    return p


@pytest.mark.parametrize("value", ['""', '"   "'])
def test_requires_artifact_blank_is_error(tmp_path, value):
    with pytest.raises(SidecarError, match=r"\[requires\]\.artifact must be a non-empty string"):
        parse_sidecar(make(tmp_path, f"[requires]\nartifact = {value}\n"))


@pytest.mark.parametrize("value", ['""', '"  "'])
def test_requires_json_key_blank_is_error(tmp_path, value):
    with pytest.raises(SidecarError, match=r"\[requires\]\.json_key must be a non-empty string"):
        parse_sidecar(make(tmp_path, f'[requires]\nartifact = "x.json"\njson_key = {value}\n'))


@pytest.mark.parametrize("value", ['""', '"\\t "'])
def test_requires_must_equal_blank_is_error(tmp_path, value):
    with pytest.raises(SidecarError, match=r"\[requires\]\.must_equal must be a non-empty string"):
        parse_sidecar(
            make(
                tmp_path, f'[requires]\nartifact = "x.json"\njson_key = "a"\nmust_equal = {value}\n'
            )
        )


@pytest.mark.parametrize("value", ["a..b", ".a", "a.", "."])
def test_requires_json_key_empty_segment_is_error(tmp_path, value):
    with pytest.raises(SidecarError, match=r"json_key must be a dotted key path with no empty"):
        parse_sidecar(make(tmp_path, f'[requires]\nartifact = "x.json"\njson_key = "{value}"\n'))


@pytest.mark.parametrize("value", ["/etc/x.json", "../x.json", "out/../../x.json", "a/.."])
def test_requires_artifact_must_stay_inside_sidecar_dir(tmp_path, value):
    with pytest.raises(SidecarError, match=r"artifact must be a glob relative to the project root"):
        parse_sidecar(make(tmp_path, f'[requires]\nartifact = "{value}"\n'))


def test_requires_artifact_relative_glob_is_accepted(tmp_path):
    sc = parse_sidecar(make(tmp_path, '[requires]\nartifact = "out/*.json"\n'))
    assert sc.requires is not None
    assert sc.requires.artifact == "out/*.json"


def test_requires_reject_suffixes_empty_entry_is_error(tmp_path):
    with pytest.raises(SidecarError, match=r"reject_suffixes must not contain an empty string"):
        parse_sidecar(make(tmp_path, '[requires]\nreject_suffixes = ["-dirty", ""]\n'))


def test_requires_reject_suffixes_empty_list_is_legal(tmp_path):
    sc = parse_sidecar(make(tmp_path, "[requires]\nreject_suffixes = []\n"))
    assert sc.requires is not None
    assert sc.requires.reject_suffixes == []


@pytest.mark.parametrize("value", ["true", "nan", "inf", "-inf", "0", "1", "1.0", "10.5", "-1"])
def test_budget_tolerance_rejected_values(tmp_path, value):
    with pytest.raises(SidecarError, match=r"tolerance"):
        parse_sidecar(make(tmp_path, f"[budget]\ntolerance = {value}\n"))


def test_budget_overflowing_est_wall_s_is_error(tmp_path):
    big = "1" + "0" * 400
    with pytest.raises(SidecarError, match=r"\[budget\]\.est_wall_s is too large"):
        parse_sidecar(make(tmp_path, f"[budget]\nest_wall_s = {big}\n"))


def test_budget_overflowing_per_unit_s_is_error(tmp_path):
    big = "1" + "0" * 400
    with pytest.raises(SidecarError, match=r"\[budget\]\.per_unit_s is too large"):
        parse_sidecar(make(tmp_path, f"[budget]\nper_unit_s = {big}\n"))


def test_budget_overflowing_tolerance_is_error(tmp_path):
    big = "1" + "0" * 400
    with pytest.raises(SidecarError, match=r"\[budget\]\.tolerance is too large"):
        parse_sidecar(make(tmp_path, f"[budget]\ntolerance = {big}\n"))


def test_budget_overflowing_n_units_product_is_error(tmp_path):
    big = "1" + "0" * 400
    with pytest.raises(SidecarError, match=r"too large to check est_wall_s"):
        parse_sidecar(
            make(tmp_path, f"[budget]\nest_wall_s = 5\nper_unit_s = 1.0\nn_units = {big}\n")
        )


def test_budget_product_float_overflow_is_error(tmp_path):
    # Finite operands whose product overflows float to inf: rejected, not compared against.
    with pytest.raises(SidecarError, match=r"too large to check est_wall_s"):
        parse_sidecar(
            make(
                tmp_path,
                "[budget]\nest_wall_s = 5\nper_unit_s = 1e308\nn_units = 100000000000\n",
            )
        )


def test_requires_as_top_level_scalar_is_error(tmp_path):
    with pytest.raises(SidecarError, match=r"\[requires\] must be a table"):
        parse_sidecar(make_top(tmp_path, "requires = 3"))


def test_budget_as_top_level_scalar_is_error(tmp_path):
    with pytest.raises(SidecarError, match=r"\[budget\] must be a table"):
        parse_sidecar(make_top(tmp_path, "budget = 3"))


def test_requires_waiver_inline_non_table_is_error(tmp_path):
    with pytest.raises(SidecarError, match=r"\[requires\.waiver\] must be a table"):
        parse_sidecar(make(tmp_path, '[requires]\nwaiver = "x"\n'))


def test_requires_waiver_unknown_key_warns(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="bathos.sidecar"):
        sc = parse_sidecar(make(tmp_path, '[requires.waiver]\nreason = "r"\nbogus = 1\n'))
    assert sc.requires is not None
    assert sc.requires.waiver == RequiresWaiver(reason="r")
    assert any("bogus" in r.message for r in caplog.records)


def test_applies_if_warns_not_evaluated(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="bathos.sidecar"):
        sc = parse_sidecar(make(tmp_path, '[requires]\napplies_if = "gpu == true"\n'))
    assert sc.requires is not None
    assert sc.requires.applies_if == "gpu == true"
    assert any(
        "applies_if is not evaluated yet; the requirement always applies" in r.message
        for r in caplog.records
    )


@pytest.mark.parametrize("kind", ["benchmark", "debug"])
def test_requires_and_budget_on_other_kinds_are_errors(tmp_path, kind):
    with pytest.raises(SidecarError, match=r"only allowed on \[experiment\]"):
        parse_sidecar(
            make(tmp_path, '[requires]\nworkload = "jax"\n[budget]\nest_wall_s = 5\n', kind=kind)
        )


def test_both_blocks_serialize_to_json(tmp_path):
    sc = parse_sidecar(
        make(
            tmp_path,
            FULL_REQUIRES
            + "\n[budget]\nest_wall_s = 90\nper_unit_s = 2.0\nn_units = 50\ntolerance = 0.25\n",
        )
    )
    assert sc.requires is not None and sc.budget is not None
    dumped = json.dumps(dataclasses.asdict(sc))
    assert '"json_key": "meta.git.sha"' in dumped
    assert '"n_units": 50' in dumped


# ── B2 additions: [requires].hint and [budget].record_per_unit_key ───────────


def test_requires_hint_is_parsed_verbatim(tmp_path):
    sc = parse_sidecar(
        make(tmp_path, '[requires]\nartifact = "a.json"\nhint = "Run make_probe.py first"\n')
    )
    assert sc.requires is not None
    assert sc.requires.hint == "Run make_probe.py first"


def test_requires_hint_defaults_to_none(tmp_path):
    sc = parse_sidecar(make(tmp_path, '[requires]\nartifact = "a.json"\n'))
    assert sc.requires.hint is None


@pytest.mark.parametrize("bad", ['""', '"   "', "7"])
def test_requires_hint_must_be_nonblank_string(tmp_path, bad):
    with pytest.raises(SidecarError, match=r"\[requires\]\.hint"):
        parse_sidecar(make(tmp_path, f'[requires]\nartifact = "a.json"\nhint = {bad}\n'))


def test_budget_record_per_unit_key_parsed(tmp_path):
    sc = parse_sidecar(
        make(
            tmp_path,
            '[budget]\nper_unit_s = 2.0\nprobe_record = "probe.json"\n'
            'record_per_unit_key = "timing.per_unit_s"\n',
        )
    )
    assert sc.budget.record_per_unit_key == "timing.per_unit_s"


def test_budget_record_per_unit_key_requires_probe_record(tmp_path):
    with pytest.raises(SidecarError, match=r"record_per_unit_key is set but .*probe_record is not"):
        parse_sidecar(make(tmp_path, '[budget]\nper_unit_s = 2.0\nrecord_per_unit_key = "a.b"\n'))


@pytest.mark.parametrize("bad", ['"a..b"', '".a"', '""', "3"])
def test_budget_record_per_unit_key_rejects_bad_paths(tmp_path, bad):
    with pytest.raises(SidecarError, match=r"record_per_unit_key"):
        parse_sidecar(
            make(
                tmp_path,
                f'[budget]\nprobe_record = "p.json"\nrecord_per_unit_key = {bad}\n',
            )
        )


def test_budget_record_per_unit_key_is_not_an_unknown_key(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        parse_sidecar(
            make(
                tmp_path,
                '[budget]\nprobe_record = "p.json"\nrecord_per_unit_key = "a.b"\n',
            )
        )
    assert not any("Unknown key in [budget]" in r.message for r in caplog.records)
