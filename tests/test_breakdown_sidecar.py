"""Distributional breakdown, T1 (spec 261026 D1/D2): `[breakdown]` parse and validation, prefix
expansion, `uncovered_aggregates`, the run-time advisory channel, and the emission check.

Scripts live under `scripts/explore/`, which is not an enforced directory, so the stderr advisory
is asserted on a run the prereg gate never sees.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from bathos.catalog import init_catalog, read_runs
from bathos.mcp import run_tool
from bathos.runner import run_script
from bathos.sidecar import (
    SidecarError,
    effective_label,
    expand_breakdown_aggregates,
    label_names_nonresidual_outcome,
    parse_sidecar,
    uncovered_aggregates,
)
from bathos.validate import validate_sidecar

_HEAD = '[experiment]\nhypothesis = "h"\n'


def _sidecar(
    tmp_path: Path,
    columns: dict[str, str],
    extra: str = "",
    body: str | None = None,
    gate: list[str] | None = None,
):
    """Parse a legacy-class sidecar whose outcome condition names `gate` (default: every plain
    float column, never `__` summary columns or strings)."""
    names = gate or [c for c, t in columns.items() if t == "float" and "__" not in c]
    cond = " AND ".join(f"{c} < 1" for c in names) or "1 = 1"
    schema = "\n".join(f'{c} = "{t}"' for c, t in columns.items())
    outcomes = body or textwrap.dedent(f"""
        [outcomes.pass]
        condition = "{cond}"
        decision = "go"
        reasoning = "r"
        [outcomes.fail]
        condition = "NOT ({cond})"
        decision = "stop"
        reasoning = "r"
        is_residual = true
    """)
    p = tmp_path / "s.bth.toml"
    p.write_text(_HEAD + textwrap.dedent(extra) + outcomes + f"[result_schema]\n{schema}\n")
    return parse_sidecar(p), p


def _uncovered(tmp_path, columns, extra=""):
    sc, _ = _sidecar(tmp_path, columns, extra)
    return {u.column for u in uncovered_aggregates(sc)}


# ── detection: real lathex names ─────────────────────────────────────────────────────


@pytest.mark.parametrize("col", ["e_max_placement", "p_max_iptm", "max_abs_delta", "min_dist"])
def test_lathex_aggregate_names_hit(tmp_path, col):
    assert _uncovered(tmp_path, {col: "float"}) == {col}


@pytest.mark.parametrize(
    "col",
    ["n_max", "is_inf", "min_samples", "kendall_tau", "e_max_placement__n_over"],
)
def test_lathex_non_aggregate_names_miss(tmp_path, col):
    assert _uncovered(tmp_path, {col: "float"}) == set()


@pytest.mark.parametrize("col", ["worst_rmsd", "peak_force", "rmsd_worst", "force_peak_ns"])
def test_worst_and_peak_names_hit(tmp_path, col):
    assert _uncovered(tmp_path, {col: "float"}) == {col}


@pytest.mark.parametrize("col", ["max_loss_steps", "max_hit_count", "min_idx", "peak_flag"])
def test_count_step_index_and_flag_suffixes_miss_even_as_float_columns(tmp_path, col):
    """FLOAT columns, so the type check cannot be what rejects them: the suffix rules must."""
    assert _uncovered(tmp_path, {col: "float"}) == set()


def test_int_columns_miss_even_with_aggregate_like_names(tmp_path):
    assert _uncovered(tmp_path, {"max_steps_taken": "int", "peak_count": "int"}) == set()


def test_meta_opt_in_overrides_type_and_name_rules(tmp_path):
    extra = """
        [result_schema_meta.kendall_tau]
        aggregate = true
        [result_schema_meta.worst_rank]
        aggregate = true
    """
    sc, _ = _sidecar(
        tmp_path,
        {"kendall_tau": "float", "worst_rank": "int"},
        extra,
        gate=["kendall_tau", "worst_rank"],
    )
    found = {u.column: u.reason for u in uncovered_aggregates(sc)}
    assert found == {"kendall_tau": "result_schema_meta", "worst_rank": "result_schema_meta"}


def test_observe_sanity_expressions_are_scanned(tmp_path):
    body = textwrap.dedent("""
        [observe]
        looking_for = "x"
        sanity = ["max_abs_delta < 3"]
    """)
    p = tmp_path / "o.bth.toml"
    p.write_text(
        '[experiment]\nhypothesis = "h"\nstage_name = "exploration"\n'
        + body
        + '[result_schema]\nmax_abs_delta = "float"\n'
    )
    found = uncovered_aggregates(parse_sidecar(p))
    assert [(u.column, u.sources) for u in found] == [("max_abs_delta", ("observe.sanity",))]


def test_covered_by_explicit_aggregate_prefix_or_waive(tmp_path):
    cols = {
        "e_max_placement": "float",
        "e_max_placement__worst_unit": "str",
        "e_max_placement__second": "float",
        "e_max_placement__median": "float",
        "e_max_placement__n_units": "int",
    }
    explicit = """
        [breakdown]
        unit = "binder"
        [breakdown.aggregates.e_max_placement]
        direction = "max"
    """
    assert _uncovered(tmp_path, cols, explicit) == set()
    prefix = explicit.replace("e_max_placement]", '"e_max_*"]')  # `*` needs a quoted TOML key
    assert _uncovered(tmp_path, cols, prefix) == set()
    waived = """
        [breakdown]
        waive = "internal calibration; nothing is built on the maximum"
    """
    assert _uncovered(tmp_path, {"e_max_placement": "float"}, waived) == set()


# ── parse and validate ───────────────────────────────────────────────────────────────

_FAMILY = {
    "e_max_a": "float",
    "e_max_b": "float",
    "e_max_a__worst_unit": "str",
    "e_max_a__second": "float",
    "e_max_a__median": "float",
    "e_max_a__n_units": "int",
    "e_max_b__worst_unit": "str",
    "e_max_b__second": "float",
    "e_max_b__median": "float",
    "e_max_b__n_units": "int",
}
_PREFIX = """
    [breakdown]
    unit = "binder"
    [breakdown.aggregates."e_max_*"]
    direction = "max"
"""


def test_prefix_expansion_skips_summary_columns(tmp_path):
    sc, _ = _sidecar(tmp_path, _FAMILY, _PREFIX)
    expanded, unmatched = expand_breakdown_aggregates(sc)
    assert sorted(expanded) == ["e_max_a", "e_max_b"]  # not e_max_a__second etc.
    assert unmatched == []


def test_prefix_with_summary_columns_declared_validates(tmp_path):
    sc, path = _sidecar(tmp_path, _FAMILY, _PREFIX)
    result = validate_sidecar(sc, path)
    assert result.ok, result.errors


def test_prefix_expands_before_summary_columns_are_checked(tmp_path):
    cols = {k: v for k, v in _FAMILY.items() if not k.startswith("e_max_b__")}
    sc, path = _sidecar(tmp_path, cols, _PREFIX)
    messages = [e.message for e in validate_sidecar(sc, path).errors]
    assert any("e_max_b__worst_unit" in m for m in messages)
    assert not any("e_max_a__" in m for m in messages)


def test_unmatched_prefix_and_dunder_key_are_errors(tmp_path):
    sc, path = _sidecar(tmp_path, {"e_max_a": "float"}, _PREFIX.replace("e_max_*", "zzz_*"))
    assert any("matches no float" in e.message for e in validate_sidecar(sc, path).errors)
    dunder = _PREFIX.replace('"e_max_*"', "e_max_a__second")
    sc, path = _sidecar(tmp_path, _FAMILY, dunder)
    assert any("'__'" in e.message for e in validate_sidecar(sc, path).errors)


@pytest.mark.parametrize(
    ("block", "needle"),
    [
        ('[breakdown]\nunit = "u"\n', "declare [breakdown.aggregates"),
        ('[breakdown]\n[breakdown.aggregates.e_max_a]\ndirection = "max"\n', "required when"),
        ('[breakdown]\nunit = "u"\n[breakdown.aggregates.e_max_a]\ndirection = "up"\n', "one of"),
        (
            '[breakdown]\nunit = "u"\n[breakdown.aggregates.e_max_a]\ndirection = "max"\nmargin = 1\n',
            "requires a `bound`",
        ),
        (
            '[breakdown]\nunit = "u"\n[breakdown.aggregates.e_max_a]\n'
            'direction = "max"\nbound = 0\n',
            "explicit `margin`",
        ),
        (
            '[breakdown]\nunit = "u"\n[breakdown.aggregates.e_max_a]\n'
            'direction = "max"\nwaive = "late"\n',
            "belongs directly under [breakdown]",
        ),
        (
            '[breakdown]\nunit = "u"\n[breakdown.aggregates.nope]\ndirection = "max"\n',
            "not in [result_schema]",
        ),
    ],
)
def test_breakdown_validation_errors(tmp_path, block, needle):
    sc, path = _sidecar(tmp_path, _FAMILY, block)
    messages = [e.message for e in validate_sidecar(sc, path).errors]
    assert any(needle in m for m in messages), messages


def test_bound_without_n_over_column_is_an_error_and_bound_literal_warns(tmp_path):
    cols = {k: v for k, v in _FAMILY.items() if k.startswith("e_max_a")}
    block = (
        '[breakdown]\nunit = "u"\n[breakdown.aggregates.e_max_a]\ndirection = "max"\nbound = 0.5\n'
    )
    sc, path = _sidecar(tmp_path, cols, block)
    result = validate_sidecar(sc, path)
    assert any("e_max_a__n_over" in e.message for e in result.errors)
    # the outcome condition is `e_max_a < 1`, so 0.5 is not a literal of any condition
    assert any("bound 0.5 does not appear" in w for w in result.warnings)


def test_bound_literal_present_does_not_warn(tmp_path):
    cols = {k: v for k, v in _FAMILY.items() if k.startswith("e_max_a")}
    cols["e_max_a__n_over"] = "int"
    block = (
        '[breakdown]\nunit = "u"\n[breakdown.aggregates.e_max_a]\ndirection = "max"\nbound = 1\n'
    )
    sc, path = _sidecar(tmp_path, cols, block)
    result = validate_sidecar(sc, path)
    assert result.ok, result.errors
    assert not any("does not appear" in w for w in result.warnings)


def test_result_schema_meta_validation(tmp_path):
    extra = """
        [result_schema_meta.ghost]
        aggregate = true
        [result_schema_meta.kendall_tau]
        aggregate = "yes"
        colour = "red"
    """
    sc, path = _sidecar(tmp_path, {"kendall_tau": "float"}, extra)
    messages = [e.message for e in validate_sidecar(sc, path).errors]
    assert any("'ghost' is not a [result_schema] column" in m for m in messages)
    assert any("must be a boolean" in m for m in messages)
    assert any("unknown key 'colour'" in m for m in messages)


def test_malformed_breakdown_shapes_raise(tmp_path):
    p = tmp_path / "bad.bth.toml"
    p.write_text('breakdown = "yes"\n' + _HEAD)
    with pytest.raises(SidecarError):
        parse_sidecar(p)
    sc_text = _HEAD + '[breakdown.aggregates.x]\ndirection = "max"\nbound = "high"\n'
    p.write_text(sc_text)
    with pytest.raises(SidecarError):
        parse_sidecar(p)


def test_validate_warns_on_uncovered_aggregate(tmp_path):
    sc, path = _sidecar(tmp_path, {"e_max_placement": "float"})
    result = validate_sidecar(sc, path)
    assert result.ok
    assert any("e_max_placement" in w and "no [breakdown]" in w for w in result.warnings)


# ── effective label ──────────────────────────────────────────────────────────────────


def test_effective_label_defers_to_tentative_for_observed():
    assert effective_label("observed", "pass") == "pass"
    assert effective_label("observed", None) == "observed"
    assert effective_label("pass", "fail") == "pass"
    assert effective_label(None, None) is None


def test_label_exemptions(tmp_path):
    sc, _ = _sidecar(tmp_path, {"e_max_a": "float"})
    assert label_names_nonresidual_outcome(sc, "pass")
    assert not label_names_nonresidual_outcome(sc, "fail")  # residual
    assert not label_names_nonresidual_outcome(sc, "error")
    assert not label_names_nonresidual_outcome(sc, "invalid_measurement")
    assert not label_names_nonresidual_outcome(sc, "unknown")  # not declared
    assert not label_names_nonresidual_outcome(sc, None)


# ── runner: stderr, emission check, completeness ─────────────────────────────────────

_RESULT_SCRIPT = textwrap.dedent("""
    import json, os, sys
    p = os.environ.get("BTH_RESULTS_PATH")
    if p:
        json.dump(json.loads(sys.argv[1]), open(p, "w"))
    sys.exit(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
""")

_BREAKDOWN_TAIL = """
    [breakdown]
    unit = "binder"
    [breakdown.aggregates.e_max]
    direction = "max"
"""
_SUMMARY_SCHEMA = """
    e_max__worst_unit = "str"
    e_max__second = "float"
    e_max__median = "float"
    e_max__n_units = "int"
"""
_SUMMARY_RESULT = {
    "e_max__worst_unit": "b17",
    "e_max__second": 0.1,
    "e_max__median": 0.004,
    "e_max__n_units": 24,
}


def _sidecar_text(*, stage: str, breakdown: bool, summary_schema: bool = True) -> str:
    head = '[experiment]\nhypothesis = "h"\n'
    if stage == "exploration":
        head += 'stage_name = "exploration"\n[observe]\nlooking_for = "worst case"\n'
    text = head + textwrap.dedent("""
        [outcomes.pass]
        condition = "e_max <= 0.5"
        decision = "go"
        reasoning = "r"
        [outcomes.fail]
        condition = "e_max > 0.5"
        decision = "stop"
        reasoning = "r"
        is_residual = true
        [result_schema]
        e_max = "float"
    """)
    if breakdown:
        text += textwrap.dedent(_BREAKDOWN_TAIL)
        if summary_schema:
            text += textwrap.dedent(_SUMMARY_SCHEMA)
    return text


def _run(tmp_path, tmp_catalog, sidecar_text, result, *, exit_code=0):
    init_catalog(tmp_catalog)
    scripts = tmp_path / "scripts" / "explore"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "probe.py").write_text(_RESULT_SCRIPT)
    (scripts / "probe.bth.toml").write_text(sidecar_text)
    rc = run_script(
        argv=[sys.executable, str(scripts / "probe.py"), json.dumps(result), str(exit_code)],
        project_slug="testproj",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    return rc, read_runs(tmp_catalog)


def test_uncovered_aggregate_warns_on_stderr_in_non_enforced_dir(tmp_path, tmp_catalog, capsys):
    rc, runs = _run(
        tmp_path, tmp_catalog, _sidecar_text(stage="legacy", breakdown=False), {"e_max": 0.2}
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "'e_max' gates a verdict" in err
    assert "[breakdown]" in err
    assert runs[0].outcome == "pass"  # advisory only


def test_covered_aggregate_is_silent(tmp_path, tmp_catalog, capsys):
    _run(
        tmp_path,
        tmp_catalog,
        _sidecar_text(stage="legacy", breakdown=True),
        {"e_max": 0.2, **_SUMMARY_RESULT},
    )
    assert "WARNING" not in capsys.readouterr().err


def test_missing_summary_columns_warn_but_keep_the_outcome(tmp_path, tmp_catalog, capsys):
    rc, runs = _run(
        tmp_path, tmp_catalog, _sidecar_text(stage="legacy", breakdown=True), {"e_max": 0.2}
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "did not emit its [breakdown] summary columns" in err
    assert "e_max__worst_unit" in err
    assert runs[0].outcome == "pass"


def test_exploratory_run_uses_tentative_outcome_for_the_emission_check(
    tmp_path, tmp_catalog, capsys
):
    rc, runs = _run(
        tmp_path, tmp_catalog, _sidecar_text(stage="exploration", breakdown=True), {"e_max": 0.2}
    )
    assert rc == 0
    assert runs[0].outcome == "observed"
    assert runs[0].tentative_outcome == "pass"
    err = capsys.readouterr().err
    assert "outcome 'pass' was reached" in err


def test_residual_label_is_exempt_from_the_emission_check(tmp_path, tmp_catalog, capsys):
    rc, runs = _run(
        tmp_path, tmp_catalog, _sidecar_text(stage="legacy", breakdown=True), {"e_max": 0.9}
    )
    assert rc == 0
    assert runs[0].outcome == "fail"  # residual
    assert "did not emit" not in capsys.readouterr().err


def test_exploratory_residual_tentative_label_is_exempt(tmp_path, tmp_catalog, capsys):
    _, runs = _run(
        tmp_path, tmp_catalog, _sidecar_text(stage="exploration", breakdown=True), {"e_max": 0.9}
    )
    assert runs[0].tentative_outcome == "fail"
    assert "did not emit" not in capsys.readouterr().err


def test_crashed_run_keeps_label_recovery_without_summary_columns(tmp_path, tmp_catalog):
    """Summary columns are excluded from `_result_schema_complete`, so a crash whose payload
    has every other declared field still evaluates its label (here: a recorded non-pass)."""
    _, runs = _run(
        tmp_path,
        tmp_catalog,
        _sidecar_text(stage="legacy", breakdown=True),
        {"e_max": 0.9},
        exit_code=3,
    )
    assert runs[0].outcome == "fail"
    assert "exit_code=3" in runs[0].outcome_error_reason


# ── MCP run tool ─────────────────────────────────────────────────────────────────────


def test_run_tool_returns_warnings(tmp_path, tmp_catalog, monkeypatch):
    from bathos import mcp as mcp_module
    from bathos.runner import run_script as real_run_script

    init_catalog(tmp_catalog)
    scripts = tmp_path / "scripts" / "explore"
    scripts.mkdir(parents=True)
    (scripts / "probe.py").write_text(_RESULT_SCRIPT)
    (scripts / "probe.bth.toml").write_text(_sidecar_text(stage="legacy", breakdown=False))
    monkeypatch.chdir(tmp_path)

    def in_tmp(**kwargs):
        return real_run_script(cwd=tmp_path, **kwargs)

    with patch.object(mcp_module, "run_script", side_effect=in_tmp):
        result = run_tool(
            script_path=str(scripts / "probe.py"),
            args=[json.dumps({"e_max": 0.2}), "0"],
            project_slug="testproj",
            catalog_dir=str(tmp_catalog),
        )
    assert result["success"] is True
    codes = [w["code"] for w in result["warnings"]]
    assert codes == ["aggregate_uncovered"]
    assert result["warnings"][0]["column"] == "e_max"


def test_run_tool_warnings_empty_and_not_leaked(tmp_path, tmp_catalog, monkeypatch):
    from bathos import mcp as mcp_module
    from bathos.runner import run_script as real_run_script
    from bathos.telemetry import run_advisories_var

    init_catalog(tmp_catalog)
    scripts = tmp_path / "scripts" / "explore"
    scripts.mkdir(parents=True)
    (scripts / "probe.py").write_text(_RESULT_SCRIPT)
    (scripts / "probe.bth.toml").write_text(_sidecar_text(stage="legacy", breakdown=True))
    monkeypatch.chdir(tmp_path)

    def in_tmp(**kwargs):
        return real_run_script(cwd=tmp_path, **kwargs)

    with patch.object(mcp_module, "run_script", side_effect=in_tmp):
        result = run_tool(
            script_path=str(scripts / "probe.py"),
            args=[json.dumps({"e_max": 0.2, **_SUMMARY_RESULT}), "0"],
            project_slug="testproj",
            catalog_dir=str(tmp_catalog),
        )
    assert result["warnings"] == []
    assert run_advisories_var.get(None) is None
