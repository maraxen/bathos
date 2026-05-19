import textwrap
from pathlib import Path
import pytest


def _write_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "run_test.bth.toml"
    p.write_text(textwrap.dedent(content))
    return p


def test_parse_experiment_sidecar(tmp_path):
    from bathos.sidecar import parse_sidecar, SidecarKind
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "NVT maintains ±5K over 50ps"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "proceed"
        [outcomes.fail]
        condition = "temp_std >= 5"
        decision = "debug"
        [result_schema]
        temp_std = "float"
    """)
    s = parse_sidecar(path)
    assert s.kind == SidecarKind.EXPERIMENT
    assert s.hypothesis == "NVT maintains ±5K over 50ps"
    assert "pass" in s.outcomes
    assert s.outcomes["pass"].condition == "temp_std < 5"
    assert s.result_schema == {"temp_std": "float"}


def test_parse_benchmark_sidecar(tmp_path):
    from bathos.sidecar import parse_sidecar, SidecarKind
    path = _write_toml(tmp_path, """
        [benchmark]
        baseline_ref = "run_abc123"
        metric = "ns_per_day"
        regression_threshold = 0.05
        target = "> 50 ns/day"
        [result_schema]
        ns_per_day = "float"
    """)
    s = parse_sidecar(path)
    assert s.kind == SidecarKind.BENCHMARK
    assert s.baseline_ref == "run_abc123"
    assert s.regression_threshold == 0.05


def test_parse_sidecar_invalid_toml(tmp_path):
    from bathos.sidecar import SidecarError
    path = tmp_path / "run_test.bth.toml"
    path.write_text("not valid toml ][[[")
    with pytest.raises(SidecarError, match="Failed to parse"):
        from bathos.sidecar import parse_sidecar
        parse_sidecar(path)


def test_find_sidecar_found(tmp_path):
    from bathos.sidecar import find_sidecar
    script = tmp_path / "run_nvt.py"
    script.touch()
    sidecar = tmp_path / "run_nvt.bth.toml"
    sidecar.write_text("[experiment]\nhypothesis='h'\n[result_schema]\n")
    assert find_sidecar(script) == sidecar


def test_find_sidecar_missing(tmp_path):
    from bathos.sidecar import find_sidecar
    script = tmp_path / "run_nvt.py"
    script.touch()
    assert find_sidecar(script) is None


def test_is_in_enforced_dir_true(tmp_path):
    from bathos.sidecar import is_in_enforced_dir
    script = tmp_path / "scripts" / "experiments" / "run_nvt.py"
    script.parent.mkdir(parents=True)
    script.touch()
    assert is_in_enforced_dir(script) is True


def test_is_in_enforced_dir_false(tmp_path):
    from bathos.sidecar import is_in_enforced_dir
    script = tmp_path / "scripts" / "scratch" / "explore_data.py"
    script.parent.mkdir(parents=True)
    script.touch()
    assert is_in_enforced_dir(script) is False


def test_evaluate_outcome_pass(tmp_path):
    from bathos.sidecar import parse_sidecar, evaluate_outcome
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "h"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "proceed"
        [outcomes.fail]
        condition = "temp_std >= 5"
        decision = "debug"
        [result_schema]
        temp_std = "float"
    """)
    s = parse_sidecar(path)
    label = evaluate_outcome(s, {"temp_std": 2.1})
    assert label == "pass"


def test_evaluate_outcome_no_match(tmp_path):
    from bathos.sidecar import parse_sidecar, evaluate_outcome
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "h"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "proceed"
        [result_schema]
        temp_std = "float"
    """)
    s = parse_sidecar(path)
    # value that matches no condition (shouldn't happen in well-formed sidecars, but be safe)
    label = evaluate_outcome(s, {})
    assert label == "unknown"


def test_evaluate_outcome_bool_result(tmp_path):
    from bathos.sidecar import parse_sidecar, evaluate_outcome
    path = _write_toml(tmp_path, """
        [debug]
        symptom = "NaN forces"
        suspected_cause = "PME grid"
        verification = "compare box sizes"
        [outcomes.reproduced]
        condition = "reproduced = TRUE"
        decision = "confirmed bug"
        [outcomes.not_reproduced]
        condition = "reproduced = FALSE"
        decision = "environment issue"
        [verdict_schema]
        reproduced = "bool"
    """)
    s = parse_sidecar(path)
    label = evaluate_outcome(s, {"reproduced": True})
    assert label == "reproduced"
