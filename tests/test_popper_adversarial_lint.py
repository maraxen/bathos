"""check_popper_adversarial is wired into `bth lint` and the MCP lint mirror.

Before task 261016_debt-sprint-cutover (backlog #4234) the check was defined in
linter.py and documented in CHANGELOG 0.8.0 as a shipped Tier-2 advisory, but no
caller ever invoked it. These tests pin: the direct contract, the scope (only
scripts/experiments is walked), and that both the CLI and the MCP tool surface the
advisory through the shared `lint_tool` aggregator.
"""

import asyncio
import json
from pathlib import Path

from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()

POPPER_HEAD = (
    "[experiment]\n"
    "hypothesis = 'h'\n"
    "[result_schema]\n"
    "metric = 'float'\n"
    "[popper]\n"
    "null_pass_rate = 0.3\n"
    "alt_pass_rate = 0.75\n"
    "stopping_threshold = 20.0\n"
)

# POPPER sidecar with no adversarial_check in any outcome -> advisory expected.
FLAGGED = POPPER_HEAD + (
    "[outcomes.pass]\n"
    "condition = 'metric < 5.0'\n"
    "decision = 'proceed'\n"
    "[outcomes.fail]\n"
    "condition = 'metric >= 5.0'\n"
    "decision = 'debug'\n"
)

# POPPER sidecar whose pass branch declares adversarial_check -> no advisory.
CLEAN = POPPER_HEAD + (
    "[outcomes.pass]\n"
    "condition = 'metric < 5.0'\n"
    "adversarial_check = 'n_samples >= 100'\n"
    "decision = 'proceed'\n"
    "[outcomes.fail]\n"
    "condition = 'metric >= 5.0'\n"
    "decision = 'debug'\n"
)

# Non-POPPER sidecar: no [popper] block, so the check must ignore it.
PLAIN = (
    "[experiment]\n"
    "hypothesis = 'h'\n"
    "[result_schema]\n"
    "metric = 'float'\n"
    "[outcomes.pass]\n"
    "condition = 'metric < 5.0'\n"
    "decision = 'proceed'\n"
)


def _build_project(root):
    exp = root / "scripts" / "experiments"
    exp.mkdir(parents=True)
    (exp / "run_flagged.bth.toml").write_text(FLAGGED)
    (exp / "run_clean.bth.toml").write_text(CLEAN)
    (exp / "run_plain.bth.toml").write_text(PLAIN)
    return exp


def _popper_issues(issues):
    return [i for i in issues if i.issue == "popper_missing_adversarial_check"]


def test_check_flags_only_popper_sidecar_without_adversarial_check(tmp_path):
    from bathos.linter import IssueSeverity, check_popper_adversarial

    _build_project(tmp_path)

    issues = check_popper_adversarial(tmp_path)

    assert [i.path.name for i in issues] == ["run_flagged.bth.toml"]
    assert issues[0].severity == IssueSeverity.WARNING


def test_check_ignores_sidecars_outside_scripts_experiments(tmp_path):
    """Scope guard: the check walks scripts/experiments only, not the whole project."""
    from bathos.linter import check_popper_adversarial

    bench = tmp_path / "scripts" / "benchmarks"
    bench.mkdir(parents=True)
    (bench / "bench_popper.bth.toml").write_text(FLAGGED)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "stray.bth.toml").write_text(FLAGGED)

    assert check_popper_adversarial(tmp_path) == []


def test_lint_project_root_without_scripts_is_quiet(tmp_path):
    from bathos.linter import check_popper_adversarial

    assert check_popper_adversarial(tmp_path) == []


def test_bth_lint_cli_surfaces_popper_advisory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _build_project(tmp_path)
    from bathos.cli_cyclopts import app

    result = runner.invoke(app, ["lint", "--project-root", str(tmp_path)])

    payload = json.loads(result.output)
    popper = [i for i in payload["issues"] if i["issue"] == "popper_missing_adversarial_check"]
    assert [Path(i["path"]).name for i in popper] == ["run_flagged.bth.toml"], result.output
    assert popper[0]["severity"] == "warning"


def test_mcp_lint_tool_includes_popper_advisory(tmp_path):
    from bathos.mcp import mcp_lint_tool

    _build_project(tmp_path)

    result = asyncio.run(mcp_lint_tool(project_root=str(tmp_path)))

    popper = [i for i in result["issues"] if i["issue"] == "popper_missing_adversarial_check"]
    assert len(popper) == 1, result["issues"]
    assert popper[0]["severity"] == "warning"
    assert popper[0]["path"].endswith("run_flagged.bth.toml")
    assert result["warning_count"] >= 1
