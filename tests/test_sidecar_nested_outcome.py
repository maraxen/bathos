"""Regression for #5198: outcome conditions must see nested result fields, and no outcome
evaluation failure may be silent.

Before the fix, `evaluate_outcome` bound only top-level keys (nested dicts were JSON text), so a
condition such as `a.b = 3` raised a DuckDB binder error. The adversarial_check evaluator swallowed
the same class of error and returned None, so the run recorded nothing at all.
"""

import sys
import textwrap
from pathlib import Path

import pytest

from bathos.catalog import init_catalog, read_runs
from bathos.runner import run_script
from bathos.sidecar import SidecarError, evaluate_outcome, parse_sidecar

NESTED_SIDECAR = textwrap.dedent("""
    [experiment]
    hypothesis = "nested fields are addressable"
    [outcomes.pass]
    condition = "a.b = 3 AND n = 1"
    decision = "go"
    reasoning = "r"
    [outcomes.fail]
    condition = "a.b <> 3"
    decision = "stop"
    reasoning = "r"
    is_residual = true
    [result_schema]
    n = "int"
""")


def _sidecar(tmp_path: Path, body: str):
    path = tmp_path / "s.bth.toml"
    path.write_text(body)
    return parse_sidecar(path)


def test_nested_field_condition_evaluates_to_pass(tmp_path):
    s = _sidecar(tmp_path, NESTED_SIDECAR)
    assert evaluate_outcome(s, {"a": {"b": 3}, "n": 1}) == "pass"


def test_nested_field_condition_falls_through_to_fail(tmp_path):
    s = _sidecar(tmp_path, NESTED_SIDECAR)
    assert evaluate_outcome(s, {"a": {"b": 4}, "n": 1}) == "fail"


def test_nested_null_field_is_addressable(tmp_path):
    body = NESTED_SIDECAR.replace('condition = "a.b = 3 AND n = 1"', 'condition = "a.b IS NULL"')
    s = _sidecar(tmp_path, body)
    assert evaluate_outcome(s, {"a": {"b": None}, "n": 1}) == "pass"


def test_nested_string_with_apostrophe_is_addressable(tmp_path):
    body = NESTED_SIDECAR.replace(
        'condition = "a.b = 3 AND n = 1"', "condition = \"a.name = 'it''s'\""
    )
    s = _sidecar(tmp_path, body)
    assert evaluate_outcome(s, {"a": {"name": "it's"}, "n": 1}) == "pass"


def test_top_level_dict_used_as_text_is_unchanged(tmp_path):
    """Legacy behaviour: a dict-valued top-level key still reads as JSON text in a string condition."""
    body = textwrap.dedent("""
        [experiment]
        hypothesis = "h"
        [outcomes.pass]
        condition = "args LIKE '%accuracy%'"
        decision = "go"
        reasoning = "r"
        [outcomes.fail]
        condition = "TRUE"
        decision = "stop"
        reasoning = "r"
        is_residual = true
        [result_schema]
        args = "dict"
    """)
    s = _sidecar(tmp_path, body)
    assert evaluate_outcome(s, {"args": {"accuracy": 0.9}}) == "pass"


def test_genuine_sql_error_still_raises_with_nested_result(tmp_path):
    body = NESTED_SIDECAR.replace('condition = "a.b = 3 AND n = 1"', 'condition = "a.b = = 3"')
    s = _sidecar(tmp_path, body)
    with pytest.raises(SidecarError, match="Failed to evaluate outcome 'pass'"):
        evaluate_outcome(s, {"a": {"b": 3}, "n": 1})


# --- end-to-end through the runner: the reason must be recorded, never silent -----------------

_NESTED_RESULT_SCRIPT = textwrap.dedent("""
    import os, json
    p = os.environ.get("BTH_RESULTS_PATH")
    if p:
        json.dump({"a": {"b": 3}, "n": 1}, open(p, "w"))
""")

_ADVERSARIAL_SIDECAR = textwrap.dedent("""
    [experiment]
    hypothesis = "adversarial check references a missing column"
    [outcomes.pass]
    condition = "n = 1"
    adversarial_check = "no_such_column > 0"
    decision = "go"
    reasoning = "r"
    [outcomes.fail]
    condition = "n <> 1"
    decision = "stop"
    reasoning = "r"
    is_residual = true
    [result_schema]
    n = "int"
""")


def _run(tmp_path: Path, tmp_catalog: Path, sidecar: str, script: str, name: str):
    init_catalog(tmp_catalog)
    scripts = tmp_path / "scripts" / "explore"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / f"{name}.py").write_text(script)
    (scripts / f"{name}.bth.toml").write_text(sidecar)
    rc = run_script(
        argv=[sys.executable, str(scripts / f"{name}.py")],
        project_slug="testproj",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    assert rc == 0
    runs = read_runs(tmp_catalog)
    assert len(runs) == 1
    return runs[0]


def test_runner_nested_condition_records_pass(tmp_path, tmp_catalog):
    run = _run(tmp_path, tmp_catalog, NESTED_SIDECAR, _NESTED_RESULT_SCRIPT, "run_nested")
    assert run.outcome == "pass"
    assert run.outcome_error_reason == ""


def test_runner_adversarial_check_sql_error_is_recorded(tmp_path, tmp_catalog):
    script = textwrap.dedent("""
        import os, json
        p = os.environ.get("BTH_RESULTS_PATH")
        if p:
            json.dump({"n": 1}, open(p, "w"))
    """)
    run = _run(tmp_path, tmp_catalog, _ADVERSARIAL_SIDECAR, script, "run_adv")
    # The label is not changed by a broken advisory check, but the failure must be visible.
    assert run.outcome == "pass"
    assert "adversarial_check" in run.outcome_error_reason
    assert "no_such_column" in run.outcome_error_reason


def test_adversarial_check_detailed_reports_error_text(tmp_path):
    from bathos.sidecar import evaluate_adversarial_check_detailed

    s = _sidecar(tmp_path, _ADVERSARIAL_SIDECAR)
    status, error = evaluate_adversarial_check_detailed(s, "pass", {"n": 1})
    assert status is None
    assert "no_such_column" in error


def test_adversarial_check_detailed_clean_has_no_error(tmp_path):
    from bathos.sidecar import evaluate_adversarial_check_detailed

    body = _ADVERSARIAL_SIDECAR.replace("no_such_column > 0", "n > 0")
    s = _sidecar(tmp_path, body)
    assert evaluate_adversarial_check_detailed(s, "pass", {"n": 1}) == ("passed", "")
