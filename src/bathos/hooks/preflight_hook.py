"""PreToolUse backstop: run the [requires]/[budget] preflight before a job submission that bypasses ``bth submit``.

Registered in ``.praxia/manifest.toml`` as ``[[plugin.hook_specs]]`` (event ``PreToolUse``, matcher
``Bash|mcp__.*__submit_job``) and installed as the console script ``bth-preflight-hook``. It is a
backstop, not the gate: ``bth submit`` runs the same check in-process, and ``bth submit`` itself is
skipped here.

What it looks at:

* ``Bash``: each command segment (split on ``; && || | &`` and newlines) whose command word is
  ``sbatch``, or ``myxcel submit-job`` / ``myxcel submit_job``. For myxcel the job command is the
  value of ``--command``; for ``sbatch`` the segment itself is the job command.
* ``mcp__*__submit_job`` (the myxcel MCP tool): ``tool_input.command`` is the job command.

The job command is passed to ``bth preflight --command <cmd> --json``, which uses the same sidecar
discovery as ``bth submit``. Subprocess cwd is the payload's ``cwd``.

Output contract (Claude Code hook JSON on stdout, exit 0 always):

* preflight block (exit 1), or sidecar not found (exit 2): a deny decision with the reason
* preflight warn (exit 0): ``{"systemMessage": "..."}``, shown to the user, not a deny
* pass, or no submission in the call: no stdout
* any internal failure (no ``bth`` on PATH, timeout, unparseable preflight output, bad payload):
  one stderr line ``bth-preflight-hook: internal error: ...``, no stdout, exit 0 (allow)

Exit code 2 is never used: a deny is a JSON decision.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from typing import Any

PREFLIGHT_TIMEOUT_S = 30
BASH_TOOL = "Bash"
MCP_SUBMIT = re.compile(r"^mcp__.*__submit_job$")
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
WRAPPERS = {"env", "nohup", "time", "exec", "nice", "sudo", "timeout", "command", "builtin"}
CONTROL = {";", "&&", "||", "|", "&", "(", ")", ";;", "|&"}
MYXCEL_SUBMIT = {"submit-job", "submit_job"}


class HookInternalError(Exception):
    """Any failure of the hook itself. Reported on stderr, and the call is allowed."""


def _segment_tokens(text: str) -> list[list[str]]:
    """Split a shell command text into per-command token lists, dropping control operators."""
    segments: list[list[str]] = []
    for line in text.split("\n"):
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            tokens = line.split()
        current: list[str] = []
        for tok in tokens:
            if tok in CONTROL or set(tok) <= set(";&|()"):
                if current:
                    segments.append(current)
                current = []
            else:
                current.append(tok)
        if current:
            segments.append(current)
    return segments


def _command_word_index(tokens: list[str]) -> int | None:
    """Index of the command word in a segment, skipping env assignments and plain wrappers."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        base = os.path.basename(tok)
        if ENV_ASSIGN.match(tok) or base in WRAPPERS:
            i += 1
            continue
        if base == "uv" and i + 1 < len(tokens) and tokens[i + 1] == "run":
            i += 2
            continue
        return i
    return None


def _job_command_for_segment(tokens: list[str]) -> str | None:
    """The job command string for a submission segment, or None when the segment is not one."""
    idx = _command_word_index(tokens)
    if idx is None:
        return None
    word = os.path.basename(tokens[idx])
    if word == "sbatch":
        return " ".join(shlex.quote(t) for t in tokens[idx:])
    if word == "myxcel" and idx + 1 < len(tokens) and tokens[idx + 1] in MYXCEL_SUBMIT:
        rest = tokens[idx + 2 :]
        for j, tok in enumerate(rest):
            if tok == "--command" and j + 1 < len(rest):
                return rest[j + 1]
            if tok.startswith("--command="):
                return tok.split("=", 1)[1]
        return " ".join(shlex.quote(t) for t in rest)
    return None


def submission_commands(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    """Job command strings for every submission this tool call makes. Empty when there are none."""
    if tool_name == BASH_TOOL:
        command = tool_input.get("command")
        if not isinstance(command, str):
            return []
        found = []
        for tokens in _segment_tokens(command):
            job = _job_command_for_segment(tokens)
            if job is not None:
                found.append(job)
        return found
    if MCP_SUBMIT.match(tool_name):
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            return [command]
    return []


def _run_preflight(job_command: str, cwd: str) -> subprocess.CompletedProcess[str]:
    bth = shutil.which("bth")
    if bth is None:
        raise HookInternalError("bth is not on PATH; cannot run the preflight")
    try:
        return subprocess.run(
            [bth, "preflight", "--command", job_command, "--json"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=PREFLIGHT_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise HookInternalError(f"bth preflight timed out after {PREFLIGHT_TIMEOUT_S} s") from e


def _reason(payload: dict[str, Any]) -> str:
    errors = payload.get("errors") or []
    hint = payload.get("hint") or ""
    text = "bathos preflight blocked this job submission: " + (
        "; ".join(map(str, errors)) or "see bth preflight"
    )
    if hint:
        text += f" (hint: {hint})"
    return text + ". Run the job through `bth submit` after fixing the sidecar, or ask the user."


def decide(job_command: str, cwd: str) -> dict[str, Any] | None:
    """The hook JSON for one submission, or None to allow silently. Raises HookInternalError."""
    proc = _run_preflight(job_command, cwd)
    if proc.returncode == 2:
        detail = (proc.stderr or proc.stdout or "").strip() or "script or sidecar not found"
        return _deny(
            f"bathos preflight: {detail}. The job runs in an enforced directory and "
            "needs its sidecar beside the script; run it through `bth submit` or ask the user."
        )
    if proc.returncode not in (0, 1):
        raise HookInternalError(
            f"bth preflight exited {proc.returncode}: {proc.stderr.strip()[:300]}"
        )
    try:
        payload = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError) as e:
        raise HookInternalError(f"unparseable bth preflight output: {e}") from e
    if not isinstance(payload, dict) or "status" not in payload:
        raise HookInternalError("bth preflight output has no status field")
    status = payload["status"]
    if proc.returncode == 1 or status == "block":
        if status != "block":
            raise HookInternalError(f"bth preflight exit 1 with status {status!r}")
        return _deny(_reason(payload))
    if status == "warn":
        errors = "; ".join(map(str, payload.get("errors") or [])) or "requires table not met"
        return {"systemMessage": f"bathos preflight warning: {errors}"}
    return None


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def run(payload: Any) -> dict[str, Any] | None:
    """The hook output for a parsed stdin payload. Raises HookInternalError on any failure."""
    if not isinstance(payload, dict):
        raise HookInternalError("stdin payload is not a JSON object")
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        return None
    jobs = submission_commands(tool_name, tool_input)
    if not jobs:
        return None
    cwd = payload.get("cwd") or os.getcwd()
    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        raise HookInternalError(f"payload cwd is not a directory: {cwd!r}")
    outputs = [out for out in (decide(job, cwd) for job in jobs) if out is not None]
    denied = [o for o in outputs if "hookSpecificOutput" in o]
    if denied:
        return denied[0]
    warnings = [o["systemMessage"] for o in outputs if "systemMessage" in o]
    if warnings:
        return {"systemMessage": " | ".join(warnings)}
    return None


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
        out = run(payload)
    except HookInternalError as e:
        sys.stderr.write(f"bth-preflight-hook: internal error: {e}\n")
        return
    except Exception as e:  # noqa: BLE001 - backstop: never block on a hook bug
        sys.stderr.write(f"bth-preflight-hook: internal error: {type(e).__name__}: {e}\n")
        return
    if out is not None:
        sys.stdout.write(json.dumps(out) + "\n")


if __name__ == "__main__":
    main()
