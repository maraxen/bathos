"""A typed client for ``myxcel cold ...`` (spec 261023_output-tiering section 6.2) and the capability probe.

Every function builds a list argv (``myxcel cold <sub> <profile> ... --json``), runs it through an injectable
``runner`` (default: :func:`subprocess.run`, no shell), parses the JSON and maps the ``status`` exactly as
section 6.2 pins it. Nothing here emits events or touches the catalog; Step 6 wires it in.

Rules that hold for every command:

* Remote paths are RELATIVE to the profile's ``cold_root``. A path that is empty, absolute or has a ``..``
  segment is refused locally, before the runner is invoked (myxcel refuses them too; this is the first line).
* Only a status that the contract names AND that agrees with the exit code is believed. Non-JSON output, a
  missing binary, a timeout, an unknown status, or a success status on a non-zero exit is never success:
  ``verify_file`` then returns ``unverifiable`` (never ``failed`` and never ``verified``, spec section 4), and
  every other command returns ``status == "error"`` (``lock``: never ``acquired``).
* ``verified`` is also checked against what was asked: a response whose ``sha256_observed`` or
  ``size_observed`` is missing or differs from the expected values is ``unverifiable``, not ``verified``.

The capability probe (section 6.1 step 1, section 9 row 5b) is :func:`probe_cold_capability`. Its form is
UNVERIFIED against a real myxcel (``myxcel cold put --help``, exit 0 and ``--json`` in the output); it lives in
:func:`_probe_once` so it can be adjusted when myxcel 5a lands.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, NoReturn

from bathos.cluster import MyxcelCapabilityGapError
from bathos.output_tiers.config import OutputTiersConfigError

__all__ = [
    "CommitResult",
    "GetResult",
    "LockResult",
    "MyxcelCapabilityGapError",
    "PutResult",
    "RunResult",
    "UnlockResult",
    "VerifyResult",
    "commit",
    "get",
    "lock",
    "probe_cold_capability",
    "put",
    "reset_capability_cache",
    "unlock",
    "verify_file",
]

MYXCEL = "myxcel"
_SHA256 = re.compile(r"[0-9a-f]{64}")

# Seconds. put/get move whole archives; verify-file may be a slurm job (D3) and is waited on by myxcel.
PROBE_TIMEOUT = 30.0
PUT_TIMEOUT = 3600.0
GET_TIMEOUT = 3600.0
COMMIT_TIMEOUT = 300.0
VERIFY_TIMEOUT = 7200.0
LOCK_TIMEOUT = 120.0


@dataclass(frozen=True)
class RunResult:
    """What a runner returns: the process exit code and its decoded output."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], float], RunResult]
"""``runner(argv, timeout_seconds) -> RunResult``. May raise ``FileNotFoundError`` (binary missing),
``subprocess.TimeoutExpired`` or ``OSError``; the client turns each into a typed non-success."""


def default_runner(argv: list[str], timeout: float) -> RunResult:
    """Run ``argv`` with no shell. Raises FileNotFoundError / TimeoutExpired / OSError for the client to type."""
    proc = subprocess.run(  # noqa: S603 - list argv, shell=False
        argv, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
    )
    return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")


# --------------------------------------------------------------------------------------- capability probe

_probe_ok = False


def reset_capability_cache() -> None:
    """Forget a successful probe (tests; a long-lived process after a myxcel upgrade)."""
    global _probe_ok
    _probe_ok = False


def _probe_once(runner: Runner, timeout: float) -> None:
    """UNVERIFIED form (spec section 9, 5b): ``myxcel cold put --help`` must exit 0 and mention ``--json``.
    Adjust here when myxcel 5a lands and the real behaviour of an unknown subcommand is known."""
    argv = [MYXCEL, "cold", "put", "--help"]
    try:
        res = runner(argv, timeout)
    except FileNotFoundError as e:
        raise MyxcelCapabilityGapError(f"myxcel is not installed or not on PATH ({e})") from e
    except subprocess.TimeoutExpired as e:
        raise MyxcelCapabilityGapError(f"`{' '.join(argv)}` timed out after {timeout:g}s") from e
    except OSError as e:
        raise MyxcelCapabilityGapError(f"cannot run `{' '.join(argv)}`: {e}") from e
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip().splitlines()[:1]
        raise MyxcelCapabilityGapError(
            f"the installed myxcel has no `cold` primitives (`{' '.join(argv)}` exited "
            f"{res.returncode}{': ' + detail[0] if detail else ''}); upgrade myxcel"
        )
    if "--json" not in f"{res.stdout}\n{res.stderr}":
        raise MyxcelCapabilityGapError(
            "the installed myxcel answers `myxcel cold put --help` but does not advertise --json; "
            "upgrade myxcel"
        )


def probe_cold_capability(runner: Runner | None = None, timeout: float = PROBE_TIMEOUT) -> None:
    """Return None if the installed myxcel has the ``cold`` primitives; raise
    :class:`~bathos.cluster.MyxcelCapabilityGapError` (``error_code == "myxcel_capability_gap"``) if the
    binary is missing or the probe fails. A success is cached for the life of the process
    (:func:`reset_capability_cache`); a failure is not, so a fixed install is picked up."""
    global _probe_ok
    if _probe_ok:
        return
    _probe_once(runner or default_runner, timeout)
    _probe_ok = True


# --------------------------------------------------------------------------------------- argument checks


def _refuse(message: str) -> NoReturn:
    raise OutputTiersConfigError(message, "invalid_param")


def _check_positional(label: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _refuse(f"{label} is empty")
    if "\x00" in value or "\n" in value:
        _refuse(f"{label} {value!r} holds a control character")
    if value.startswith("-"):
        _refuse(f"{label} {value!r} starts with '-' and would be read as an option")
    return value


def check_remote_path(path: str, label: str = "remote path") -> str:
    """Refuse a remote path that is empty, absolute, or has a ``..`` segment (relative to ``cold_root``)."""
    _check_positional(label, path)
    if path.startswith("/") or PurePosixPath(path).is_absolute():
        _refuse(f"{label} {path!r} is absolute; myxcel takes paths relative to cold_root")
    if ".." in path.split("/"):
        _refuse(f"{label} {path!r} contains a '..' segment")
    return path


def _argv(sub: str, profile: str, *args: str) -> list[str]:
    return [MYXCEL, "cold", sub, _check_positional("profile", profile), *args, "--json"]


# --------------------------------------------------------------------------------------- response parsing


@dataclass(frozen=True)
class _Reply:
    """A run that either produced a JSON object (``data``) or did not (``problem``)."""

    returncode: int
    data: dict[str, Any] | None
    problem: str | None


def _call(argv: list[str], runner: Runner | None, timeout: float) -> _Reply:
    run = runner or default_runner
    shown = " ".join(argv[:3])
    try:
        res = run(argv, timeout)
    except FileNotFoundError as e:
        return _Reply(-1, None, f"myxcel is not installed or not on PATH ({e})")
    except subprocess.TimeoutExpired:
        return _Reply(-1, None, f"`{shown}` timed out after {timeout:g}s")
    except OSError as e:
        return _Reply(-1, None, f"cannot run `{shown}`: {e}")
    try:
        data = json.loads(res.stdout)
    except (json.JSONDecodeError, ValueError):
        tail = (res.stderr or res.stdout).strip().splitlines()[-1:]
        return _Reply(
            res.returncode,
            None,
            f"`{shown}` exited {res.returncode} without a JSON object on stdout"
            f"{': ' + tail[0][:200] if tail else ''}",
        )
    if not isinstance(data, dict):
        return _Reply(res.returncode, None, f"`{shown}` printed JSON that is not an object")
    return _Reply(res.returncode, data, None)


def _str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _message(data: dict[str, Any] | None, fallback: str) -> str:
    if data is not None:
        reported = _str(data, "message")
        if reported:
            return reported
    return fallback


def _unknown_status(sub: str, data: dict[str, Any]) -> str:
    return f"`myxcel cold {sub}` returned unknown status {data.get('status')!r}"


def _exit_disagrees(sub: str, status: str, code: int) -> str:
    return f"`myxcel cold {sub}` reported {status!r} but exited {code}"


# --------------------------------------------------------------------------------------- results


@dataclass(frozen=True)
class PutResult:
    """``status``: ``ok`` or ``error`` (any local failure is ``error``)."""

    status: str
    remote_path: str | None = None
    bytes: int | None = None
    message: str = ""


@dataclass(frozen=True)
class CommitResult:
    """``status``: ``ok``, ``exists`` (the final name was taken; NOT a success) or ``error``."""

    status: str
    message: str = ""


@dataclass(frozen=True)
class VerifyResult:
    """``status``: ``verified``, ``failed`` or ``unverifiable``. ``method`` is ``login_ssh`` / ``slurm_job``
    as myxcel reports it (``None`` if it did not)."""

    status: str
    sha256_observed: str | None = None
    size_observed: int | None = None
    method: str | None = None
    message: str = ""


@dataclass(frozen=True)
class GetResult:
    """``status``: ``ok`` or ``error``."""

    status: str
    local_path: str | None = None
    bytes: int | None = None
    message: str = ""


@dataclass(frozen=True)
class LockResult:
    """``status``: ``acquired``, ``held`` (``holder`` is myxcel's holder record) or ``error``."""

    status: str
    holder: Any = None
    message: str = ""


@dataclass(frozen=True)
class UnlockResult:
    """``status``: ``released``, ``held`` (the lock is someone else's; ``holder``) or ``error``.

    Section 6.2 gives ``acquired|held`` for both ``lock`` and ``unlock`` without naming unlock's success word;
    ``released`` and ``ok`` are accepted for it. ``acquired`` is not (INTERPRETATION, to be pinned in the spec)."""

    status: str
    holder: Any = None
    message: str = ""


# --------------------------------------------------------------------------------------- commands


def put(
    profile: str,
    local_file: str,
    remote_rel_name: str,
    *,
    runner: Runner | None = None,
    timeout: float = PUT_TIMEOUT,
) -> PutResult:
    """``myxcel cold put <profile> <local_file> <remote_rel_name> --json``: upload to exactly that name."""
    check_remote_path(remote_rel_name, "remote_rel_name")
    argv = _argv("put", profile, _check_positional("local_file", str(local_file)), remote_rel_name)
    reply = _call(argv, runner, timeout)
    if reply.data is None:
        return PutResult("error", message=reply.problem or "")
    data = reply.data
    status = data.get("status")
    msg = _message(data, "")
    if status == "ok":
        if reply.returncode != 0:
            return PutResult("error", message=_exit_disagrees("put", "ok", reply.returncode))
        return PutResult("ok", _str(data, "remote_path"), _int(data, "bytes"), msg)
    if status == "error":
        return PutResult(
            "error", _str(data, "remote_path"), _int(data, "bytes"), msg or "myxcel reported error"
        )
    return PutResult("error", message=_unknown_status("put", data))


def commit(
    profile: str,
    remote_partial: str,
    remote_final: str,
    *,
    runner: Runner | None = None,
    timeout: float = COMMIT_TIMEOUT,
) -> CommitResult:
    """``myxcel cold commit <profile> <remote_partial> <remote_final> --json`` (``ln && rm``, never ``mv``)."""
    check_remote_path(remote_partial, "remote_partial")
    check_remote_path(remote_final, "remote_final")
    reply = _call(_argv("commit", profile, remote_partial, remote_final), runner, timeout)
    if reply.data is None:
        return CommitResult("error", reply.problem or "")
    status = reply.data.get("status")
    msg = _message(reply.data, "")
    if status == "ok":
        if reply.returncode != 0:
            return CommitResult("error", _exit_disagrees("commit", "ok", reply.returncode))
        return CommitResult("ok", msg)
    if status == "exists":
        return CommitResult("exists", msg or "final name already exists")
    if status == "error":
        return CommitResult("error", msg or "myxcel reported error")
    return CommitResult("error", _unknown_status("commit", reply.data))


def verify_file(
    profile: str,
    remote_path: str,
    sha256: str,
    size: int,
    *,
    runner: Runner | None = None,
    timeout: float = VERIFY_TIMEOUT,
) -> VerifyResult:
    """``myxcel cold verify-file <profile> <remote_path> <sha256> <size> --json``: three states, never a bool.

    Anything short of a clear, self-consistent answer is ``unverifiable`` (spec section 4)."""
    check_remote_path(remote_path)
    expected = sha256.lower() if isinstance(sha256, str) else ""
    if not _SHA256.fullmatch(expected):
        _refuse(f"sha256 {sha256!r} is not 64 lowercase hex digits")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        _refuse(f"size {size!r} is not a non-negative integer")
    reply = _call(_argv("verify-file", profile, remote_path, expected, str(size)), runner, timeout)
    if reply.data is None:
        return VerifyResult("unverifiable", message=reply.problem or "")
    data = reply.data
    status = data.get("status")
    observed_sha = _str(data, "sha256_observed")
    observed_size = _int(data, "size_observed")
    method = _str(data, "method")
    msg = _message(data, "")

    def make(state: str, message: str) -> VerifyResult:
        return VerifyResult(state, observed_sha, observed_size, method, message)

    if status == "verified":
        if reply.returncode != 0:
            return make(
                "unverifiable", _exit_disagrees("verify-file", "verified", reply.returncode)
            )
        if observed_sha is None or observed_size is None:
            return make(
                "unverifiable",
                "verify-file said verified without sha256_observed and size_observed",
            )
        if observed_sha.lower() != expected or observed_size != size:
            return make(
                "unverifiable",
                "verify-file said verified but its observed sha256/size differ from the expected values",
            )
        return make("verified", msg)
    if status == "failed":
        return make("failed", msg or "myxcel reported a mismatch")
    if status == "unverifiable":
        return make("unverifiable", msg or "myxcel could not verify")
    return make("unverifiable", _unknown_status("verify-file", data))


def get(
    profile: str,
    remote_path: str,
    local_dir: str,
    *,
    runner: Runner | None = None,
    timeout: float = GET_TIMEOUT,
) -> GetResult:
    """``myxcel cold get <profile> <remote_path> <local_dir> --json``: fetch one file into ``local_dir``."""
    check_remote_path(remote_path)
    argv = _argv("get", profile, remote_path, _check_positional("local_dir", str(local_dir)))
    reply = _call(argv, runner, timeout)
    if reply.data is None:
        return GetResult("error", message=reply.problem or "")
    data = reply.data
    status = data.get("status")
    msg = _message(data, "")
    if status == "ok":
        if reply.returncode != 0:
            return GetResult("error", message=_exit_disagrees("get", "ok", reply.returncode))
        local_path = _str(data, "local_path")
        if local_path is None:
            return GetResult("error", message="get said ok without a local_path")
        return GetResult("ok", local_path, _int(data, "bytes"), msg)
    if status == "error":
        return GetResult(
            "error", _str(data, "local_path"), _int(data, "bytes"), msg or "myxcel reported error"
        )
    return GetResult("error", message=_unknown_status("get", data))


def lock(
    profile: str,
    remote_dir: str,
    *,
    runner: Runner | None = None,
    timeout: float = LOCK_TIMEOUT,
) -> LockResult:
    """``myxcel cold lock <profile> <remote_dir> --json``: atomic ``mkdir <remote_dir>/.bth-lock``.

    ``held`` carries the holder. ``acquired`` is believed only on exit 0; every doubt is ``error``."""
    check_remote_path(remote_dir, "remote_dir")
    reply = _call(_argv("lock", profile, remote_dir), runner, timeout)
    if reply.data is None:
        return LockResult("error", message=reply.problem or "")
    data = reply.data
    status = data.get("status")
    msg = _message(data, "")
    if status == "acquired":
        if reply.returncode != 0:
            return LockResult(
                "error", message=_exit_disagrees("lock", "acquired", reply.returncode)
            )
        return LockResult("acquired", data.get("holder"), msg)
    if status == "held":
        return LockResult("held", data.get("holder"), msg)
    if status == "error":
        return LockResult("error", message=msg or "myxcel reported error")
    return LockResult("error", message=_unknown_status("lock", data))


def unlock(
    profile: str,
    remote_dir: str,
    *,
    runner: Runner | None = None,
    timeout: float = LOCK_TIMEOUT,
) -> UnlockResult:
    """``myxcel cold unlock <profile> <remote_dir> --json``: remove only the lock directory."""
    check_remote_path(remote_dir, "remote_dir")
    reply = _call(_argv("unlock", profile, remote_dir), runner, timeout)
    if reply.data is None:
        return UnlockResult("error", message=reply.problem or "")
    data = reply.data
    status = data.get("status")
    msg = _message(data, "")
    if status in {"released", "ok"}:
        if reply.returncode != 0:
            return UnlockResult(
                "error", message=_exit_disagrees("unlock", str(status), reply.returncode)
            )
        return UnlockResult("released", data.get("holder"), msg)
    if status == "held":
        return UnlockResult("held", data.get("holder"), msg)
    if status == "error":
        return UnlockResult("error", message=msg or "myxcel reported error")
    return UnlockResult("error", message=_unknown_status("unlock", data))
