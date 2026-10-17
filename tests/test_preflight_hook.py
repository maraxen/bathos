"""bth-preflight-hook: PreToolUse backstop for job submissions that bypass `bth submit` (spec 261016 P1)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bathos.cli_cyclopts import app
from bathos.hooks import preflight_hook as hook
from tests._cyclopts_runner import CyclopticRunner
from tests.test_requires_gate_cli import MISSING, _project, _script, _sidecar

runner = CyclopticRunner()
_REAL_WHICH = shutil.which  # captured before the autouse fixture replaces it


class _Recorder:
    """Stands in for subprocess.run; records calls and returns a canned CompletedProcess."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "", raise_exc=None):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raise_exc = raise_exc

    def __call__(self, args, **_kw):
        self.calls.append(list(args))
        if self.raise_exc is not None:
            raise self.raise_exc
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)


@pytest.fixture(autouse=True)
def fake_bth(monkeypatch):
    """Every test sees a `bth` on PATH; tests that need it missing override this."""
    monkeypatch.setattr(hook.shutil, "which", lambda _name: "/fake/bin/bth")


def _bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp"}


def _run_main(monkeypatch, capsys, payload: dict | str) -> tuple[str, str]:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(text))
    hook.main()
    out = capsys.readouterr()
    return out.out, out.err


# ── detection: non-submissions never invoke the preflight ─────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "git status",
        "bth submit scripts/experiments/run.py --preset gpu",
        "echo sbatch",
        "grep -r sbatch docs/",
        "myxcel status",
    ],
)
def test_non_submission_bash_is_silent_and_never_calls_bth(monkeypatch, command):
    rec = _Recorder()
    monkeypatch.setattr(hook.subprocess, "run", rec)
    assert hook.run(_bash(command)) is None
    assert rec.calls == []


# ── detection: submissions are found ───────────────────────────────────────────


def test_myxcel_submit_job_command_is_extracted():
    jobs = hook.submission_commands(
        "Bash",
        {
            "command": 'myxcel submit-job --preset gpu --command "uv run python scripts/experiments/x.py"'
        },
    )
    assert jobs == ["uv run python scripts/experiments/x.py"]


def test_sbatch_is_detected():
    jobs = hook.submission_commands("Bash", {"command": "sbatch job.sh"})
    assert len(jobs) == 1 and "job.sh" in jobs[0]


def test_chained_sbatch_after_cd_is_detected():
    jobs = hook.submission_commands("Bash", {"command": "cd x && sbatch job.sh"})
    assert len(jobs) == 1 and "job.sh" in jobs[0]


def test_mcp_submit_job_is_detected():
    jobs = hook.submission_commands(
        "mcp__plugin_myxcel_myxcel__submit_job",
        {"command": "uv run python scripts/experiments/x.py", "preset": "gpu"},
    )
    assert jobs == ["uv run python scripts/experiments/x.py"]


def test_mcp_other_tools_are_ignored():
    assert hook.submission_commands("mcp__plugin_myxcel_myxcel__job_status", {"command": "x"}) == []


def test_unparseable_quotes_fall_back_to_whitespace_split():
    jobs = hook.submission_commands("Bash", {"command": 'sbatch job.sh "unterminated'})
    assert len(jobs) == 1 and "job.sh" in jobs[0]


# ── mapping of preflight results to hook output ───────────────────────────────


def test_block_exit_1_denies_with_hint(monkeypatch):
    body = json.dumps(
        {
            "status": "block",
            "error_code": "requires_unmet",
            "errors": ["artifact missing.json not found"],
            "hint": "produce the artifact",
            "waiver_reason": None,
        }
    )
    monkeypatch.setattr(hook.subprocess, "run", _Recorder(returncode=1, stdout=body))
    out = hook.run(_bash("sbatch job.sh"))
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    assert spec["permissionDecision"] == "deny"
    assert "produce the artifact" in spec["permissionDecisionReason"]
    assert "missing.json" in spec["permissionDecisionReason"]


def test_warn_exit_0_gives_system_message_not_deny(monkeypatch):
    body = json.dumps(
        {
            "status": "warn",
            "error_code": "requires_unmet",
            "errors": ["x missing"],
            "hint": "",
            "waiver_reason": None,
        }
    )
    monkeypatch.setattr(hook.subprocess, "run", _Recorder(returncode=0, stdout=body))
    out = hook.run(_bash("sbatch job.sh"))
    assert set(out) == {"systemMessage"}
    assert "bathos preflight warning" in out["systemMessage"]


def test_pass_exit_0_is_silent(monkeypatch, capsys):
    body = json.dumps(
        {"status": "pass", "error_code": None, "errors": [], "hint": "", "waiver_reason": None}
    )
    monkeypatch.setattr(hook.subprocess, "run", _Recorder(returncode=0, stdout=body))
    assert hook.run(_bash("sbatch job.sh")) is None
    out, err = _run_main(monkeypatch, capsys, _bash("sbatch job.sh"))
    assert out == "" and err == ""


def test_exit_2_denies_not_found(monkeypatch):
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        _Recorder(returncode=2, stderr="preflight: sidecar not found for x (enforced directory)"),
    )
    out = hook.run(_bash("myxcel submit-job --command 'uv run python scripts/experiments/x.py'"))
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    assert "not found" in spec["permissionDecisionReason"]


def test_the_preflight_subprocess_gets_the_payload_cwd(monkeypatch, tmp_path):
    rec = _Recorder(returncode=0, stdout=json.dumps({"status": "pass", "errors": []}))
    monkeypatch.setattr(hook.subprocess, "run", rec)
    hook.run(
        {"tool_name": "Bash", "tool_input": {"command": "sbatch job.sh"}, "cwd": str(tmp_path)}
    )
    assert rec.calls[0][:3] == ["/fake/bin/bth", "preflight", "--command"]
    assert rec.calls[0][-1] == "--json"


# ── failure policy: allow, one stderr line, no stdout ─────────────────────────


def test_bth_missing_allows_with_stderr(monkeypatch, capsys):
    monkeypatch.setattr(hook.shutil, "which", lambda _name: None)
    out, err = _run_main(monkeypatch, capsys, _bash("sbatch job.sh"))
    assert out == ""
    assert err.startswith("bth-preflight-hook: internal error:")
    assert err.count("\n") == 1


def test_timeout_allows(monkeypatch, capsys):
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        _Recorder(raise_exc=subprocess.TimeoutExpired(cmd="bth", timeout=30)),
    )
    out, err = _run_main(monkeypatch, capsys, _bash("sbatch job.sh"))
    assert out == ""
    assert "internal error" in err and "timed out" in err


def test_garbage_json_allows(monkeypatch, capsys):
    monkeypatch.setattr(hook.subprocess, "run", _Recorder(returncode=0, stdout="not json {"))
    out, err = _run_main(monkeypatch, capsys, _bash("sbatch job.sh"))
    assert out == ""
    assert err.startswith("bth-preflight-hook: internal error:")


def test_unexpected_exit_code_allows(monkeypatch, capsys):
    monkeypatch.setattr(hook.subprocess, "run", _Recorder(returncode=7, stderr="boom"))
    out, err = _run_main(monkeypatch, capsys, _bash("sbatch job.sh"))
    assert out == "" and "internal error" in err


def test_unparseable_stdin_allows(monkeypatch, capsys):
    out, err = _run_main(monkeypatch, capsys, "{not json")
    assert out == "" and err.startswith("bth-preflight-hook: internal error:")


def test_main_never_exits_2(monkeypatch):
    monkeypatch.setattr(hook.subprocess, "run", _Recorder(returncode=2, stderr="nope"))
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(_bash("sbatch job.sh"))))
    hook.main()  # must return normally, not SystemExit(2)


# ── `bth preflight --command` uses the submit discovery ───────────────────────


def test_preflight_command_blocks_unmet_requires_for_every_py_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path)
    _sidecar(tmp_path, "validation", MISSING)
    _script(tmp_path)
    result = runner.invoke(
        app, ["preflight", "--command", "python helper.py scripts/experiments/run_nvt.py", "--json"]
    )
    assert result.exit_code == 1, result.output


def test_preflight_command_passes_without_enforced_script(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path, cutoff=None)
    result = runner.invoke(app, ["preflight", "--command", "sbatch job.sh", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output.strip().splitlines()[-1])["status"] == "pass"


def test_preflight_command_no_sidecar_blocks_when_enforcement_active(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path)  # cutoff in the past
    result = runner.invoke(app, ["preflight", "--command", "sbatch job.sh", "--json"])
    assert result.exit_code == 1, result.output
    assert "no_sidecar" in result.output


def test_preflight_rejects_script_and_command_together(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path)
    result = runner.invoke(app, ["preflight", str(_script(tmp_path)), "--command", "sbatch x.sh"])
    assert result.exit_code == 2


# ── end to end through the real `bth` on a tmp project ───────────────────────


def test_end_to_end_real_bth_denies_unmet_validation_sidecar(tmp_path, monkeypatch):
    _project(tmp_path)  # requires_enforce_after in the past
    _sidecar(tmp_path, "validation", MISSING)
    _script(tmp_path)
    venv_bin = str(Path(sys.executable).parent)
    monkeypatch.setenv("PATH", venv_bin + ":" + __import__("os").environ.get("PATH", ""))
    monkeypatch.setattr(shutil, "which", _REAL_WHICH)
    assert shutil.which("bth"), "bth console script must be on the venv PATH"
    payload = {
        "tool_name": "mcp__plugin_myxcel_myxcel__submit_job",
        "tool_input": {"command": "uv run python scripts/experiments/run_nvt.py"},
        "cwd": str(tmp_path),
    }
    out = hook.run(payload)
    assert out is not None
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    assert (
        "REQUIRES_UNMET" in spec["permissionDecisionReason"]
        or "artifact" in spec["permissionDecisionReason"]
    )
