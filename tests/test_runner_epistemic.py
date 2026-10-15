"""Runner wiring for the epistemic class (spec 261015 §3.1, §3.2).

Scripts live under `scripts/explore/`, which is not an enforced directory, so these runs bypass
the pre-registration gate and exercise only the classification and outcome precedence.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

from bathos.catalog import init_catalog, read_runs
from bathos.obligations import list_obligations
from bathos.runner import run_script

_RESULT_SCRIPT = textwrap.dedent("""
    import os, json
    p = os.environ.get("BTH_RESULTS_PATH")
    if p:
        json.dump({{"x": {x}}}, open(p, "w"))
""")

_EXPLORATORY_SIDECAR = textwrap.dedent("""
    [experiment]
    hypothesis = "h"
    stage_name = "exploration"
    [observe]
    looking_for = "sign of x"
    [outcomes.pass]
    condition = "x > 0"
    decision = "look closer"
    reasoning = "positive"
    [outcomes.fail]
    condition = "x <= 0"
    decision = "look elsewhere"
    reasoning = "non-positive"
    is_residual = true
    [result_schema]
    x = "float"
""")

_SANITY_SIDECAR = _EXPLORATORY_SIDECAR.replace(
    'looking_for = "sign of x"\n',
    'looking_for = "sign of x"\nsanity = ["x > 0"]\n',
)
assert "sanity = " in _SANITY_SIDECAR

_CALIBRATING_SIDECAR = textwrap.dedent("""
    [experiment]
    hypothesis = "noise floor"
    stage_name = "calibration"
    [calibrate]
    quantity = "noise floor"
    [result_schema]
    x = "float"
""")

_LEGACY_SIDECAR = textwrap.dedent("""
    [experiment]
    hypothesis = "h"
    [outcomes.pass]
    condition = "x > 0"
    decision = "go"
    reasoning = "r"
    [outcomes.fail]
    condition = "x <= 0"
    decision = "stop"
    reasoning = "r"
    is_residual = true
    [result_schema]
    x = "float"
""")


def _run_with(tmp_path: Path, tmp_catalog: Path, sidecar: str, x: float, name: str = "run_x"):
    """Run a scratch script with a sidecar and return the single stored run."""
    init_catalog(tmp_catalog)
    scripts = tmp_path / "scripts" / "explore"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / f"{name}.py").write_text(_RESULT_SCRIPT.format(x=x))
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


def test_exploratory_run_stores_observed_and_tentative_label(tmp_path, tmp_catalog, monkeypatch):
    monkeypatch.delenv("BTH_OBLIGATION_OUTCOME_FAILED", raising=False)
    run = _run_with(tmp_path, tmp_catalog, _EXPLORATORY_SIDECAR, x=1.0)
    assert run.outcome == "observed"
    assert run.tentative_outcome == "pass"
    assert run.epistemic_class == "exploratory"


def test_exploratory_sanity_failure_is_invalid_measurement(tmp_path, tmp_catalog):
    run = _run_with(tmp_path, tmp_catalog, _SANITY_SIDECAR, x=-1.0)
    assert run.outcome == "invalid_measurement"
    assert "sanity:x > 0" in run.outcome_error_reason
    assert run.tentative_outcome is None
    metadata = json.loads(run.metadata)
    assert metadata["sanity_failed"] == "sanity:x > 0"


def test_exploratory_sanity_passing_still_observed(tmp_path, tmp_catalog):
    run = _run_with(tmp_path, tmp_catalog, _SANITY_SIDECAR, x=2.0)
    assert run.outcome == "observed"
    assert run.tentative_outcome == "pass"


def test_calibrating_run_stores_observed(tmp_path, tmp_catalog):
    run = _run_with(tmp_path, tmp_catalog, _CALIBRATING_SIDECAR, x=0.3)
    assert run.outcome == "observed"
    assert run.epistemic_class == "calibrating"
    assert run.tentative_outcome is None


def test_legacy_sidecar_keeps_todays_outcome(tmp_path, tmp_catalog):
    run = _run_with(tmp_path, tmp_catalog, _LEGACY_SIDECAR, x=1.0)
    assert run.outcome == "pass"
    assert run.epistemic_class == "legacy"
    assert run.tentative_outcome is None


def test_exploratory_fail_label_opens_no_obligation(tmp_path, tmp_catalog, monkeypatch):
    """Obligation trigger 1 is enabled, the tentative label is a failure, and still nothing opens."""
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("BTH_OBLIGATION_OUTCOME_FAILED", "1")
    run = _run_with(tmp_path, tmp_catalog, _EXPLORATORY_SIDECAR, x=-1.0)
    assert run.outcome == "observed"
    assert run.tentative_outcome == "fail"
    assert list_obligations(tmp_path) == []


def test_exploratory_invalid_measurement_opens_no_obligation(tmp_path, tmp_catalog, monkeypatch):
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("BTH_OBLIGATION_OUTCOME_FAILED", "1")
    run = _run_with(tmp_path, tmp_catalog, _SANITY_SIDECAR, x=-1.0)
    assert run.outcome == "invalid_measurement"
    assert list_obligations(tmp_path) == []


def test_agent_session_prefers_claude_code_session_id(tmp_path, tmp_catalog, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-cc")
    monkeypatch.setenv("BTH_AGENT_SESSION", "sess-bth")
    run = _run_with(tmp_path, tmp_catalog, _EXPLORATORY_SIDECAR, x=1.0)
    assert run.agent_session == "sess-cc"


def test_agent_session_falls_back_to_bth_agent_session(tmp_path, tmp_catalog, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("BTH_AGENT_SESSION", "sess-bth")
    run = _run_with(tmp_path, tmp_catalog, _EXPLORATORY_SIDECAR, x=1.0)
    assert run.agent_session == "sess-bth"


def test_agent_session_none_when_both_unset(tmp_path, tmp_catalog, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("BTH_AGENT_SESSION", raising=False)
    run = _run_with(tmp_path, tmp_catalog, _EXPLORATORY_SIDECAR, x=1.0)
    assert run.agent_session is None


def test_no_sidecar_leaves_epistemic_class_none(tmp_path, tmp_catalog):
    init_catalog(tmp_catalog)
    rc = run_script(
        argv=[sys.executable, "-c", "pass"],
        project_slug="testproj",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    assert rc == 0
    assert read_runs(tmp_catalog)[0].epistemic_class is None


def test_stored_class_is_fixed_at_run_start(tmp_path, tmp_catalog):
    """Editing the sidecar's stage_name after a run must not rewrite that run's class."""
    run = _run_with(tmp_path, tmp_catalog, _EXPLORATORY_SIDECAR, x=1.0)
    assert run.epistemic_class == "exploratory"

    sidecar = tmp_path / "scripts" / "explore" / "run_x.bth.toml"
    sidecar.write_text(_EXPLORATORY_SIDECAR.replace('"exploration"', '"validation"'))

    stored = read_runs(tmp_catalog)[0]
    assert stored.epistemic_class == "exploratory"


_CRASH_AFTER_FAIL_SCRIPT = textwrap.dedent("""
    import os, json, sys
    p = os.environ.get("BTH_RESULTS_PATH")
    if p:
        json.dump({"x": -1.0}, open(p, "w"))
    sys.exit(1)
""")


def _crash_run_with(tmp_path: Path, tmp_catalog: Path, sidecar: str, name: str = "run_crash"):
    """Run a script that writes a complete fail-branch payload and then exits 1."""
    init_catalog(tmp_catalog)
    scripts = tmp_path / "scripts" / "explore"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / f"{name}.py").write_text(_CRASH_AFTER_FAIL_SCRIPT)
    (scripts / f"{name}.bth.toml").write_text(sidecar)
    rc = run_script(
        argv=[sys.executable, str(scripts / f"{name}.py")],
        project_slug="testproj",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    assert rc != 0
    runs = read_runs(tmp_catalog)
    assert len(runs) == 1
    return runs[0]


def test_exploratory_nonzero_exit_stays_error_with_tentative_label(
    tmp_path, tmp_catalog, monkeypatch
):
    """Spec §3.2: a crash never becomes a verdict; the fail label survives only as tentative."""
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("BTH_OBLIGATION_OUTCOME_FAILED", "1")
    run = _crash_run_with(tmp_path, tmp_catalog, _EXPLORATORY_SIDECAR)
    assert run.epistemic_class == "exploratory"
    assert run.outcome == "error"
    assert run.outcome_error_reason == "exit_code=1"
    assert run.tentative_outcome == "fail"
    assert list_obligations(tmp_path) == []


def test_legacy_nonzero_exit_still_stores_fail_outcome(tmp_path, tmp_catalog):
    """Legacy twin: the non-zero exit guard keeps storing the evaluated fail label as outcome."""
    run = _crash_run_with(tmp_path, tmp_catalog, _LEGACY_SIDECAR)
    assert run.epistemic_class == "legacy"
    assert run.outcome == "fail"
    assert run.outcome_error_reason == "exit_code=1"
    assert run.tentative_outcome is None


def test_exploratory_nonzero_exit_keeps_no_pass_label(tmp_path, tmp_catalog, monkeypatch):
    """A crash keeps no pass-direction label, not even a tentative one (mirrors the legacy guard)."""
    monkeypatch.setattr(
        sys.modules[__name__],
        "_CRASH_AFTER_FAIL_SCRIPT",
        _CRASH_AFTER_FAIL_SCRIPT.replace('{"x": -1.0}', '{"x": 1.0}'),
    )
    run = _crash_run_with(tmp_path, tmp_catalog, _EXPLORATORY_SIDECAR, name="run_crash_pass")
    assert run.epistemic_class == "exploratory"
    assert run.outcome == "error"
    assert run.tentative_outcome is None


def test_exploratory_tentative_pass_does_not_satisfy_requires_pass_stem(tmp_path, tmp_catalog):
    """Spec §8 negative control: a tentative exploratory pass is not a pass for any gate.

    The legacy twin on another stem proves the prerequisite check does fire on a real pass.
    """
    from bathos.compact import compact
    from bathos.prereg import check_reproduction_prerequisite

    explored = _run_with(tmp_path, tmp_catalog, _EXPLORATORY_SIDECAR, x=1.0, name="explored_stem")
    assert explored.outcome == "observed"
    assert explored.tentative_outcome == "pass"
    compact(tmp_catalog)
    assert check_reproduction_prerequisite("explored_stem", tmp_catalog) is False

    legacy_catalog = tmp_path / "legacy_catalog"
    legacy = _run_with(tmp_path, legacy_catalog, _LEGACY_SIDECAR, x=1.0, name="legacy_stem")
    assert legacy.outcome == "pass"
    compact(legacy_catalog)
    assert check_reproduction_prerequisite("legacy_stem", legacy_catalog) is True
