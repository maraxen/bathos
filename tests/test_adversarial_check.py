"""`adversarial_check` evaluation, its polarity, and the lint heuristics that guard it.

**Polarity, settled from the source material:** the check is a *stricter conjunct* the outcome
must also clear, so it FIRES when it evaluates FALSE. Evidence — the D3 ADR defines it as a
condition that "would flip the outcome if the hypothesis were wrong"; the v0.6 spec's example
pairs `condition = "temp_std < 5"` with
`adversarial_check = "temp_std < 5 AND n_steps >= 10000 AND dt_fs <= 0.5"`, restating the pass
condition and adding to it; and the spec's distinct-column lint heuristic only coheres if the
check is an additional bar rather than a negation.

The rival reading (a refuter that fires when TRUE) cannot account for the spec's example, and
adopting it would invert every verdict this module produces — hence the explicit polarity
tests rather than trusting the implementation to be self-evidently right.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bathos.linter import check_adversarial_checks
from bathos.sidecar import (
    OutcomeSpec,
    Sidecar,
    SidecarKind,
    evaluate_adversarial_check,
)


def _sc(check: str | None, condition: str = "metric < 5.0") -> Sidecar:
    return Sidecar(
        kind=SidecarKind.EXPERIMENT,
        result_schema={"metric": "float", "n_samples": "int"},
        outcomes={
            "pass": OutcomeSpec(condition=condition, decision="go", adversarial_check=check),
            "fail": OutcomeSpec(condition="metric >= 5.0", decision="stop", is_residual=True),
        },
    )


# ── polarity ────────────────────────────────────────────────────────────────


def test_a_satisfied_stricter_bar_passes():
    """The pass cleared the extra requirement, so nothing is caught."""
    sc = _sc("metric < 5.0 AND n_samples >= 100")
    assert evaluate_adversarial_check(sc, "pass", {"metric": 1.0, "n_samples": 500}) == "passed"


def test_an_unsatisfied_stricter_bar_fires():
    """Same passing metric, but the run was too small to believe — this is the whole point."""
    sc = _sc("metric < 5.0 AND n_samples >= 100")
    assert evaluate_adversarial_check(sc, "pass", {"metric": 1.0, "n_samples": 3}) == "fired"


def test_polarity_is_not_inverted():
    """Guard against the rival 'refuter fires when true' reading. Under that inversion both
    assertions above would flip, silently turning every strong run into an obligation."""
    sc = _sc("n_samples >= 100")
    strong = evaluate_adversarial_check(sc, "pass", {"metric": 1.0, "n_samples": 500})
    weak = evaluate_adversarial_check(sc, "pass", {"metric": 1.0, "n_samples": 3})
    assert (strong, weak) == ("passed", "fired")


def test_the_spec_example_shape_behaves_as_documented():
    """The v0.6 spec's own example, evaluated."""
    sc = Sidecar(
        kind=SidecarKind.EXPERIMENT,
        result_schema={"temp_std": "float", "n_steps": "int", "dt_fs": "float"},
        outcomes={
            "pass": OutcomeSpec(
                condition="temp_std < 5",
                decision="proceed to NPT validation",
                adversarial_check="temp_std < 5 AND n_steps >= 10000 AND dt_fs <= 0.5",
            )
        },
    )
    good = {"temp_std": 2.0, "n_steps": 50000, "dt_fs": 0.5}
    cheap = {"temp_std": 2.0, "n_steps": 10, "dt_fs": 2.0}
    assert evaluate_adversarial_check(sc, "pass", good) == "passed"
    assert evaluate_adversarial_check(sc, "pass", cheap) == "fired"


# ── absent stays distinguishable from cleared ──────────────────────────────


def test_no_declared_check_is_none_not_passed():
    """Absent must never read as cleared — otherwise every undeclared branch looks verified."""
    assert evaluate_adversarial_check(_sc(None), "pass", {"metric": 1.0}) is None


@pytest.mark.parametrize("label", ["", "unknown", "error"])
def test_unevaluated_outcomes_have_no_result(label):
    assert evaluate_adversarial_check(_sc("n_samples >= 1"), label, {"metric": 1.0}) is None


def test_no_result_data_is_none():
    assert evaluate_adversarial_check(_sc("n_samples >= 1"), "pass", {}) is None


def test_a_malformed_check_does_not_raise():
    """Advisory signal: a bad SQL fragment must not fail a run whose outcome already
    evaluated successfully."""
    assert evaluate_adversarial_check(_sc("this is not sql ((("), "pass", {"metric": 1.0}) is None


def test_the_check_is_read_from_the_selected_branch_only():
    sc = Sidecar(
        kind=SidecarKind.EXPERIMENT,
        result_schema={"metric": "float"},
        outcomes={
            "pass": OutcomeSpec(condition="metric < 5", decision="go"),
            "fail": OutcomeSpec(
                condition="metric >= 5",
                decision="stop",
                is_residual=True,
                adversarial_check="metric > 1000",
            ),
        },
    )
    # 'pass' declares no check, so the 'fail' branch's must not leak into it.
    assert evaluate_adversarial_check(sc, "pass", {"metric": 1.0}) is None
    assert evaluate_adversarial_check(sc, "fail", {"metric": 10.0}) == "fired"


# ── lint heuristics (spec Item 6 a/b) ──────────────────────────────────────


def _lint(tmp_path: Path, body: str) -> set[str]:
    (tmp_path / "x.bth.toml").write_text(body)
    return {i.issue for i in check_adversarial_checks(tmp_path)}


_BASE = """
[experiment]
hypothesis = "h"

[outcomes.pass]
condition = "metric < 5.0"
decision = "go"
{check}

[result_schema]
metric = "float"
n_samples = "int"
"""


def test_a_negation_of_condition_is_flagged(tmp_path):
    """The exact bug the shipped scaffold template carried: as a conjunct it is a
    contradiction, so it would fire on every single pass."""
    issues = _lint(tmp_path, _BASE.format(check='adversarial_check = "metric >= 5.0"'))
    assert "adversarial_check_same_column" in issues


def test_a_distinct_column_check_is_clean(tmp_path):
    issues = _lint(
        tmp_path, _BASE.format(check='adversarial_check = "metric < 5.0 AND n_samples >= 100"')
    )
    assert issues == set()


@pytest.mark.parametrize(
    "check",
    [
        "n_samples >= 100 AND 1=1",
        "n_samples >= 100 AND TRUE",
        "n_samples = n_samples",
    ],
)
def test_tautological_conjuncts_are_flagged(tmp_path, check):
    issues = _lint(tmp_path, _BASE.format(check=f'adversarial_check = "{check}"'))
    assert "adversarial_check_tautology" in issues


def test_a_legitimate_boolean_comparison_is_not_a_tautology(tmp_path):
    """`flag = true` is a real comparison and must not trip the bare-TRUE deny-list."""
    body = """
[experiment]
hypothesis = "h"

[outcomes.pass]
condition = "metric < 5.0"
decision = "go"
adversarial_check = "converged = true"

[result_schema]
metric = "float"
converged = "bool"
"""
    assert "adversarial_check_tautology" not in _lint(tmp_path, body)


def test_missing_check_still_flagged(tmp_path):
    issues = _lint(tmp_path, _BASE.format(check=""))
    assert "missing_adversarial_check" in issues


def test_the_shipped_scaffold_template_is_lint_clean(tmp_path):
    """Regression: the template shipped a same-column negation, and because heuristics (a)
    and (b) were never implemented, nothing caught it. Wiring trigger 3 with the correct
    polarity would then have opened an obligation on every scaffolded experiment's every
    passing run."""
    from bathos.new_experiment import _SIDECAR_TEMPLATE

    (tmp_path / "scaffolded.bth.toml").write_text(_SIDECAR_TEMPLATE.format(name="demo"))
    assert [i.issue for i in check_adversarial_checks(tmp_path)] == []


# ── end to end through `bth run` ───────────────────────────────────────────

_SCRIPT = """
import os, json
p = os.environ.get("BTH_RESULTS_PATH")
if p:
    json.dump({{"metric": 1.0, "n_samples": {n}}}, open(p, "w"))
"""

_RUN_SIDECAR = """
[experiment]
hypothesis = "h"

[outcomes.pass]
condition = "metric < 5.0"
decision = "go"
reasoning = "r"
adversarial_check = "metric < 5.0 AND n_samples >= 100"

[outcomes.fail]
condition = "metric >= 5.0"
decision = "stop"
reasoning = "r"
is_residual = true

[result_schema]
metric = "float"
n_samples = "int"
"""


def _run(tmp_path: Path, n_samples: int):
    from bathos.catalog import read_runs
    from bathos.runner import run_script

    catalog = tmp_path / "catalog"
    catalog.mkdir(exist_ok=True)
    enforced = tmp_path / "scripts" / "experiments"
    enforced.mkdir(parents=True, exist_ok=True)
    (enforced / "run_x.py").write_text(_SCRIPT.format(n=n_samples))
    (enforced / "run_x.bth.toml").write_text(_RUN_SIDECAR)

    rc = run_script(
        argv=[sys.executable, str(enforced / "run_x.py")],
        project_slug="proj",
        catalog_dir=catalog,
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    assert rc == 0
    return read_runs(catalog)[0]


def test_run_records_the_result_column(tmp_path, monkeypatch):
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("BTH_OBLIGATION_ADVERSARIAL_CHECK_FIRED", raising=False)

    strong = _run(tmp_path, 500)
    assert (strong.outcome, strong.adversarial_check_result) == ("pass", "passed")
    # The declaration fact and the measurement are separate columns.
    assert strong.adversarial_check_status == "present"


def test_a_fired_check_opens_an_obligation_when_enabled(tmp_path, monkeypatch):
    from bathos.obligations import list_obligations

    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("BTH_OBLIGATION_ADVERSARIAL_CHECK_FIRED", "1")

    weak = _run(tmp_path, 3)
    # It still PASSED -- that is precisely why the check matters.
    assert (weak.outcome, weak.adversarial_check_result) == ("pass", "fired")

    obs = list_obligations(tmp_path)
    assert [o.trigger for o in obs] == ["adversarial_check_fired"]
    assert "n_samples" in obs[0].detail


def test_a_fired_check_opens_nothing_while_disabled(tmp_path, monkeypatch):
    from bathos.obligations import list_obligations

    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("BTH_OBLIGATION_ADVERSARIAL_CHECK_FIRED", raising=False)

    weak = _run(tmp_path, 3)
    assert weak.adversarial_check_result == "fired"
    assert list_obligations(tmp_path) == []
