"""#2205: `bth submit` sidecar auto-discovery must not fail silently."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bathos.cli_cyclopts import app
from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()

_SUBMIT_RESULT = {
    "slurm_job_id": "12345",
    "script_path": "/tmp/x.sh",
    "preset_used": {},
    "job_name": "bth-submit",
}

_SIDECAR = """
[experiment]
hypothesis = "h"
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


def _setup(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "myproject"\nroot = "{tmp_path}"\n\n'
        '[slurm]\nremote = "engaging"\npreset = "gpu"\n'
    )


def _submit(command: list[str]):
    with (
        patch("bathos.cluster.push_project"),
        patch("bathos.cluster.submit_job", return_value=_SUBMIT_RESULT),
    ):
        return runner.invoke(app, ["submit", "--no-wait", *command])


def test_missing_sidecar_warns_with_looked_for_path(tmp_path: Path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    (tmp_path / "train.py").write_text("pass\n")

    result = _submit(["python", "train.py"])

    assert result.exit_code == 0, result.output
    assert "no sidecar found" in result.stderr
    assert "train.bth.toml" in result.stderr
    assert "reproduction/parity submit gates were skipped" in result.stderr


def test_nonexistent_script_warns_with_looked_for_script(tmp_path: Path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    result = _submit(["python", "scripts/gone.py"])

    assert result.exit_code == 0, result.output
    assert "scripts/gone.py" in result.stderr
    assert "does not exist" in result.stderr
    assert "gates were skipped" in result.stderr


def test_no_py_token_warns(tmp_path: Path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    result = _submit(["bash", "run.sh"])

    assert result.exit_code == 0, result.output
    assert "no .py script" in result.stderr
    assert "gates were skipped" in result.stderr


def test_found_sidecar_does_not_warn(tmp_path: Path, monkeypatch):
    """Negative control: a discovered sidecar must not trigger the warning."""
    _setup(tmp_path, monkeypatch)
    (tmp_path / "train.py").write_text("pass\n")
    (tmp_path / "train.bth.toml").write_text(_SIDECAR)

    result = _submit(["python", "train.py"])

    assert result.exit_code == 0, result.output
    assert "gates were skipped" not in result.stderr
    assert "no sidecar found" not in result.stderr


def test_explicit_sidecar_flag_does_not_warn(tmp_path: Path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    explicit = tmp_path / "explicit.bth.toml"
    explicit.write_text(_SIDECAR)

    result = _submit(["--sidecar", str(explicit), "python", "train.py"])

    assert result.exit_code == 0, result.output
    assert "gates were skipped" not in result.stderr
