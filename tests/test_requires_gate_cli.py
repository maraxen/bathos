"""`bth preflight` exit codes and `--json` shape, and `bth submit` blocking on [requires] (spec 261016 B2).

`bth submit` tests patch the cluster backend and assert it is never reached on a block.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from bathos.cli_cyclopts import app
from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()

PAST = "2026-10-01"

OUTCOMES = """
[outcomes.pass]
condition = "value > 0"
decision = "proceed"
reasoning = "r"
source = "provisional:fixture"

[outcomes.fallback]
condition = "TRUE"
decision = "review"
reasoning = "r"
is_residual = true
source = "provisional:fixture"

[result_schema]
value = "float"
"""

_SUBMIT_RESULT = {
    "slurm_job_id": "12345",
    "script_path": "/tmp/x.sh",
    "preset_used": {},
    "job_name": "x",
}


def _project(root: Path, cutoff: str | None = PAST) -> None:
    body = f'[project]\nslug = "proj"\nroot = "{root}"\n\n[slurm]\nremote = "engaging"\npreset = "gpu"\n'
    if cutoff is not None:
        body += f'\n[gate]\nrequires_enforce_after = "{cutoff}"\n'
    (root / ".bth.toml").write_text(body)


def _sidecar(root: Path, stage: str, requires: str) -> Path:
    d = root / "scripts" / "experiments"
    d.mkdir(parents=True, exist_ok=True)
    extra = '[observe]\noutputs = ["x.csv"]\n\n' if stage == "exploration" else ""
    p = d / "run_nvt.bth.toml"
    p.write_text(
        f'[experiment]\nhypothesis = "h"\nstage_name = "{stage}"\n' + extra + requires + OUTCOMES
    )
    return p


def _script(root: Path) -> Path:
    s = root / "scripts" / "experiments" / "run_nvt.py"
    s.parent.mkdir(parents=True, exist_ok=True)
    s.write_text("print(1)\n")
    return s


MISSING = '[requires]\nartifact = "missing.json"\n'


# ── bth preflight ────────────────────────────────────────────────────────────


def test_preflight_exit_0_on_pass(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path)
    _sidecar(tmp_path, "validation", "")
    result = runner.invoke(app, ["preflight", str(_script(tmp_path))])
    assert result.exit_code == 0, result.output


def test_preflight_exit_0_on_warn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path)
    _sidecar(tmp_path, "exploration", MISSING)
    result = runner.invoke(app, ["preflight", str(_script(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "WARNING REQUIRES_UNMET" in result.output


def test_preflight_exit_1_on_block(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path)
    _sidecar(tmp_path, "validation", MISSING)
    result = runner.invoke(app, ["preflight", str(_script(tmp_path))])
    assert result.exit_code == 1, result.output
    assert "BLOCKED REQUIRES_UNMET" in result.output


def test_preflight_exit_2_when_enforced_script_has_no_sidecar(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path)
    result = runner.invoke(app, ["preflight", str(_script(tmp_path))])
    assert result.exit_code == 2, result.output


def test_preflight_exit_0_for_ungated_script_without_sidecar(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path)
    s = tmp_path / "misc" / "tool.py"
    s.parent.mkdir()
    s.write_text("print(1)\n")
    result = runner.invoke(app, ["preflight", str(s)])
    assert result.exit_code == 0, result.output
    assert "no sidecar, not gated" in result.output


def test_preflight_json_shape_on_block(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path)
    _sidecar(tmp_path, "validation", MISSING)
    result = runner.invoke(app, ["preflight", str(_script(tmp_path)), "--json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {"status", "error_code", "errors", "hint", "waiver_reason"}
    assert payload["status"] == "block"
    assert payload["error_code"] == "requires_unmet"
    assert payload["errors"] and "missing.json" in payload["errors"][0]
    assert payload["hint"]
    assert payload["waiver_reason"] is None


def test_preflight_json_shape_on_pass_with_waiver(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path)
    _sidecar(
        tmp_path,
        "validation",
        MISSING + '\n[requires.waiver]\nreason = "pilot"\n',
    )
    result = runner.invoke(app, ["preflight", str(_script(tmp_path)), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "pass"
    assert payload["error_code"] is None
    assert payload["errors"] == []
    assert payload["waiver_reason"] == "pilot"


# ── bth submit ───────────────────────────────────────────────────────────────


def _submit_argv() -> list[str]:
    return ["submit", "--no-wait", "uv", "run", "python", "scripts/experiments/run_nvt.py"]


def test_submit_aborts_on_block_before_any_cluster_call(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    _project(tmp_path)
    _sidecar(tmp_path, "validation", MISSING)
    _script(tmp_path)
    with (
        patch("bathos.cluster.push_project") as mock_push,
        patch("bathos.cluster.submit_job", return_value=_SUBMIT_RESULT) as mock_submit,
    ):
        result = runner.invoke(app, _submit_argv())
    assert result.exit_code != 0, result.output
    assert "BLOCKED REQUIRES_UNMET" in result.stderr
    assert "hint:" in result.stderr
    mock_push.assert_not_called()
    mock_submit.assert_not_called()


def test_submit_warn_continues_to_cluster(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    _project(tmp_path)
    _sidecar(tmp_path, "exploration", MISSING)
    _script(tmp_path)
    with (
        patch("bathos.cluster.push_project"),
        patch("bathos.cluster.submit_job", return_value=_SUBMIT_RESULT) as mock_submit,
    ):
        result = runner.invoke(app, _submit_argv())
    assert result.exit_code == 0, result.output
    assert "WARNING REQUIRES_UNMET" in result.stderr
    mock_submit.assert_called_once()


def test_submit_fails_closed_on_malformed_gate_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    _project(tmp_path, cutoff=None)
    (tmp_path / ".bth.toml").write_text(
        (tmp_path / ".bth.toml").read_text() + '\n[gate]\nrequires_enforce_after = "soon"\n'
    )
    # A [requires] table is what makes the gate config matter; the requirement itself is unmet.
    _sidecar(tmp_path, "exploration", MISSING)
    _script(tmp_path)
    with (
        patch("bathos.cluster.push_project") as mock_push,
        patch("bathos.cluster.submit_job", return_value=_SUBMIT_RESULT) as mock_submit,
    ):
        result = runner.invoke(app, _submit_argv())
    assert result.exit_code != 0, result.output
    assert "requires preflight failed" in result.stderr
    mock_push.assert_not_called()
    mock_submit.assert_not_called()
