"""Distributional breakdown, T7 (spec 261026 D6): `near_bound`, the default-ON `near_bound`
obligation, the fork checks on discharge, and the scaffold. The fork-record gate at conclude and
the discharge wiring are T7a (tests/test_obligation_discharge_wiring.py)."""

from __future__ import annotations

import math
import sys
import textwrap
from pathlib import Path

import pytest

from bathos.obligations import (
    DEFAULT_ON,
    TRIGGERS,
    WIRED_TRIGGERS,
    list_obligations,
    trigger_enabled,
)
from bathos.postmortem import (
    parse_postmortem,
    scaffold_campaign_postmortem_template,
    scaffold_postmortem_template,
    validate_postmortem,
)
from bathos.sidecar import near_bound, parse_sidecar

_CONDITION = textwrap.dedent("""
    [outcomes.pass]
    condition = "e_max_placement <= 0.5"
    decision = "go"
    reasoning = "r"
    [outcomes.fail]
    condition = "e_max_placement > 0.5"
    decision = "stop"
    reasoning = "r"
    is_residual = true
""")
_SCHEMA = textwrap.dedent("""
    [result_schema]
    e_max_placement = "float"
    e_max_placement__worst_unit = "str"
    e_max_placement__second = "float"
    e_max_placement__median = "float"
    e_max_placement__n_units = "int"
    e_max_placement__n_over = "int"
""")


def _breakdown(agg: str) -> str:
    return (
        '[breakdown]\nunit = "binder"\n[breakdown.aggregates.e_max_placement]\n'
        'direction = "max"\n' + agg
    )


def _sidecar(tmp_path: Path, agg: str = "bound = 0.5\n", stage: str = "exploration"):
    head = '[experiment]\nhypothesis = "h"\n'
    if stage == "exploration":
        head += 'stage_name = "exploration"\n[observe]\nlooking_for = "worst case"\n'
    p = tmp_path / "s.bth.toml"
    p.write_text(_breakdown(agg) + head + _CONDITION + _SCHEMA)
    return parse_sidecar(p)


def _results(value=0.521, n_over=1, n_units=24):
    return {
        "e_max_placement": value,
        "e_max_placement__n_over": n_over,
        "e_max_placement__n_units": n_units,
    }


# ── the pure function ────────────────────────────────────────────────────────────────


def test_78db9e04_fires_single_unit(tmp_path):
    hits = near_bound(_sidecar(tmp_path), _results())
    assert {h.kind for h in hits} == {"single_unit", "margin"}
    assert all(h.column == "e_max_placement" for h in hits)


def test_one_unit_over_with_one_unit_total_is_not_single_unit(tmp_path):
    # far from the bound, so the margin trigger stays out of it
    hits = near_bound(_sidecar(tmp_path), _results(value=0.9, n_over=1, n_units=1))
    assert hits == []


def test_two_units_over_is_not_single_unit(tmp_path):
    assert near_bound(_sidecar(tmp_path), _results(value=0.9, n_over=2)) == []


def test_margin_trigger_is_symmetric_and_defaults_to_ten_percent(tmp_path):
    sc = _sidecar(tmp_path)  # default band is 0.10 * 0.5 = 0.05
    just_under = near_bound(sc, _results(value=0.46, n_over=0))
    assert [h.kind for h in just_under] == ["margin"]
    assert near_bound(sc, _results(value=0.40, n_over=0)) == []
    assert near_bound(sc, _results(value=0.56, n_over=0)) == []


def test_explicit_margin_overrides_the_default(tmp_path):
    sc = _sidecar(tmp_path, "bound = 0.5\nmargin = 0.2\n")
    assert [h.kind for h in near_bound(sc, _results(value=0.35, n_over=0))] == ["margin"]


@pytest.mark.parametrize("bad", [None, float("nan"), "0.52", True, math.inf])
def test_missing_nan_or_non_numeric_values_are_unevaluated(tmp_path, bad):
    sc = _sidecar(tmp_path)
    assert near_bound(sc, {"e_max_placement": bad}) == []
    assert near_bound(sc, {}) == []


def test_bound_zero_without_margin_is_never_near(tmp_path):
    sc = _sidecar(tmp_path, "bound = 0\n")
    assert near_bound(sc, _results(value=0.0, n_over=0)) == []


def test_aggregates_without_a_bound_are_ignored(tmp_path):
    assert near_bound(_sidecar(tmp_path, ""), _results()) == []


# ── default ON, opt-outs ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for trig in TRIGGERS:
        monkeypatch.delenv(f"BTH_OBLIGATION_{trig.upper()}", raising=False)


def _config(tmp_path: Path, body: str) -> Path:
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "p"\nroot = "{tmp_path}"\n{body}')
    return tmp_path


def test_near_bound_is_a_wired_default_on_trigger():
    assert "near_bound" in TRIGGERS and "near_bound" in WIRED_TRIGGERS
    assert frozenset({"near_bound"}) == DEFAULT_ON
    assert WIRED_TRIGGERS == TRIGGERS


def test_on_with_empty_config(tmp_path):
    assert trigger_enabled("near_bound", _config(tmp_path, "")) is True
    assert trigger_enabled("near_bound", tmp_path / "nowhere") is True


def test_off_with_config_false(tmp_path):
    root = _config(tmp_path, "[obligations]\nnear_bound = false\n")
    assert trigger_enabled("near_bound", root) is False


def test_off_with_env_zero_and_env_beats_config_true(monkeypatch, tmp_path):
    monkeypatch.setenv("BTH_OBLIGATION_NEAR_BOUND", "0")
    assert trigger_enabled("near_bound", _config(tmp_path, "")) is False
    root = _config(tmp_path, "[obligations]\nnear_bound = true\n")
    assert trigger_enabled("near_bound", root) is False


def test_other_triggers_stay_off_by_default(tmp_path):
    assert [t for t in TRIGGERS - DEFAULT_ON if trigger_enabled(t, tmp_path)] == []


def test_resolve_flag_default_applies_to_all_three_non_env_returns(tmp_path):
    from bathos.config import resolve_flag

    kw = {"default": True}
    assert resolve_flag("BTH_NOPE", "obligations", "k", tmp_path, **kw) is True  # no config file
    root = _config(tmp_path, "[obligations]\nother = true\n")
    assert resolve_flag("BTH_NOPE", "obligations", "k", root, **kw) is True  # key absent
    assert resolve_flag("BTH_NOPE", "obligations", "k", root) is False  # legacy default
    root.joinpath(".bth.toml").write_text("not [valid toml")
    assert resolve_flag("BTH_NOPE", "obligations", "k", root, **kw) is True  # malformed


# ── run end: opens the obligation without blocking, exploratory runs included ────────

_SCRIPT = textwrap.dedent("""
    import json, os, sys
    p = os.environ.get("BTH_RESULTS_PATH")
    if p:
        json.dump(json.loads(sys.argv[1]), open(p, "w"))
""")


def _run(tmp_path: Path, sidecar_text: str, result: dict, monkeypatch) -> tuple[int, str]:
    from bathos.catalog import read_runs
    from bathos.runner import run_script

    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(tmp_path))
    import json

    catalog = tmp_path / "catalog"
    catalog.mkdir(exist_ok=True)
    scripts = tmp_path / "scripts" / "explore"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "probe.py").write_text(_SCRIPT)
    (scripts / "probe.bth.toml").write_text(sidecar_text)
    rc = run_script(
        argv=[sys.executable, str(scripts / "probe.py"), json.dumps(result)],
        project_slug="proj",
        catalog_dir=catalog,
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    return rc, read_runs(catalog)[0].id


def _sidecar_text(agg: str = "bound = 0.5\n") -> str:
    return (
        _breakdown(agg)
        + '[experiment]\nhypothesis = "h"\nstage_name = "exploration"\n'
        + '[observe]\nlooking_for = "worst case"\n'
        + _CONDITION
        + _SCHEMA
    )


_FULL_RESULT = {
    **_results(),
    "e_max_placement__worst_unit": "b17",
    "e_max_placement__second": 0.1,
    "e_max_placement__median": 0.004,
}


def test_78db9e04_exploration_run_opens_near_bound_without_blocking(tmp_path, monkeypatch, capsys):
    rc, run_id = _run(tmp_path, _sidecar_text(), _FULL_RESULT, monkeypatch)
    assert rc == 0
    obs = list_obligations(tmp_path)
    assert [(o.obligation_id, o.trigger) for o in obs] == [
        (f"run_{run_id}_near_bound", "near_bound")
    ]
    assert "near_bound" in capsys.readouterr().err


def test_a_far_from_bound_run_opens_nothing(tmp_path, monkeypatch, capsys):
    far = {**_FULL_RESULT, "e_max_placement": 0.1, "e_max_placement__n_over": 0}
    rc, _ = _run(tmp_path, _sidecar_text(), far, monkeypatch)
    assert rc == 0 and list_obligations(tmp_path) == []
    assert "near_bound" not in capsys.readouterr().err


def test_opt_outs_silence_the_run_time_advisory_and_open(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BTH_OBLIGATION_NEAR_BOUND", "0")
    rc, _ = _run(tmp_path, _sidecar_text(), _FULL_RESULT, monkeypatch)
    assert rc == 0 and list_obligations(tmp_path) == []
    assert "near_bound" not in capsys.readouterr().err


def test_config_opt_out_is_honoured_at_run_time(tmp_path, monkeypatch):
    _config(tmp_path, "[obligations]\nnear_bound = false\n")
    rc, _ = _run(tmp_path, _sidecar_text(), _FULL_RESULT, monkeypatch)
    assert rc == 0 and list_obligations(tmp_path) == []


def test_a_sidecar_with_no_breakdown_bound_never_opens_one(tmp_path, monkeypatch):
    rc, _ = _run(tmp_path, _sidecar_text(agg=""), _FULL_RESULT, monkeypatch)
    assert rc == 0 and list_obligations(tmp_path) == []


# ── fork checks on discharge (run-independent) ───────────────────────────────────────

_PM = textwrap.dedent("""
    run_id = "abcdef12"
    format = {fmt}
    [postmortem]
    hypothesis_status = "inconclusive"
    summary = "s"
    discharges = ["run_abcdef12-0000_near_bound"]
    {fork}
""")


def _validate(tmp_path: Path, fork: str = "", fmt: int = 2):
    path = tmp_path / "pm.bth.postmortem.toml"
    path.write_text(_PM.format(fmt=fmt, fork=fork))
    return validate_postmortem(parse_postmortem(path), workspace_root=tmp_path)


def _fork(**over) -> str:
    fields = {"choice": '"stop"', "reason": '"internal validation only"', "cited": "false"}
    fields.update(over)
    return "[fork]\n" + "\n".join(f"{k} = {v}" for k, v in fields.items() if v is not None)


def test_a_valid_fork_discharges_cleanly(tmp_path):
    result = _validate(tmp_path, _fork())
    assert result.ok, [e.message for e in result.errors]


def test_missing_fork_or_empty_reason_is_an_error(tmp_path):
    assert any("[fork]" in e.message for e in _validate(tmp_path, "").errors)
    assert any("reason" in e.message for e in _validate(tmp_path, _fork(reason='""')).errors)


def test_stop_with_cited_true_is_an_error(tmp_path):
    result = _validate(tmp_path, _fork(cited="true"))
    assert any("stop" in e.message and "cited" in e.message for e in result.errors)
    assert _validate(tmp_path, _fork(choice='"extend"', cited="true")).ok


def test_unknown_choice_is_an_error(tmp_path):
    assert any("choice" in e.message for e in _validate(tmp_path, _fork(choice='"shrug"')).errors)


def test_format_1_cannot_discharge_near_bound(tmp_path):
    result = _validate(tmp_path, _fork(), fmt=1)
    assert any("format = 2" in e.message for e in result.errors)


def test_fork_checks_do_not_touch_postmortems_that_discharge_nothing_near_bound(tmp_path):
    path = tmp_path / "pm.bth.postmortem.toml"
    path.write_text(
        'run_id = "abcdef12"\nformat = 2\n[postmortem]\nhypothesis_status = "held"\n'
        'discharges = ["run_abcdef12_outcome_failed"]\n'
    )
    assert validate_postmortem(parse_postmortem(path), workspace_root=tmp_path).ok


# ── scaffold ─────────────────────────────────────────────────────────────────────────


def test_run_scaffold_is_format_2_with_a_fork_and_a_commented_full_id_checklist(tmp_path):
    full = "abcdef12-0000-0000-0000-000000000001"
    path = scaffold_postmortem_template(
        "python scripts/experiments/probe.py",
        full,
        tmp_path,
        open_obligation_ids=[f"run_{full}_near_bound"],
    )
    text = path.read_text()
    assert "format = 2" in text and "[fork]" in text
    assert f"#   run_{full}_near_bound" in text
    pm = parse_postmortem(path)
    assert pm.format == 2 and pm.discharges == []
    assert pm.fork == {"choice": "", "reason": "", "cited": False, "follow_up_run_ids": []}


def test_campaign_scaffold_is_format_2_and_never_fills_discharges(tmp_path):
    path = scaffold_campaign_postmortem_template("camp-1", tmp_path, ["run_x_near_bound"])
    pm = parse_postmortem(path)
    assert pm.format == 2 and pm.discharges == [] and "[fork]" in path.read_text()
    assert "#   run_x_near_bound" in path.read_text()


def test_the_untouched_scaffold_does_not_validate_as_a_discharge(tmp_path):
    """An empty [fork] cannot discharge anything: the author must fill it and name the id."""
    path = scaffold_postmortem_template("python x.py", "abcdef12", tmp_path)
    pm = parse_postmortem(path)
    pm.discharges = ["run_abcdef12_near_bound"]
    assert not validate_postmortem(pm, workspace_root=tmp_path).ok
