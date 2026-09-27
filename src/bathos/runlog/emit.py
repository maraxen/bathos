"""Shared emission layer for runlog write sites (delivery step 2b, wave i).

See `.praxia/docs/specs/260925_project-local-run-log.md`, "Authoritative
writes" and "Mode (the flag and cut-over)". Every write site listed in that
table goes through `unit_of_work()` + `emit_or_legacy()` (or the lower-level
`emit_event()`) so the flag/legacy-vs-event branching is written once, not
once per call site.

## Unit-of-work mechanism

The spec: "Mode is fixed once per unit of work: read at the start of each CLI
process and each MCP tool invocation ... and never re-read within it."

This codebase's CLI and MCP surfaces do not share one universal function
call -- `cisternal.wire()` dispatches a `bth` CLI command straight to the
same plain (sync) "core" function (`registry="bathos-cli"` in `bathos.mcp`,
e.g. `reap_tool`, `check_tool`, `run_cli_tool`) that FastMCP's `registry=
"bathos"` async tools (wrapped in `@traced_tool`) call into internally as a
bridge. So there is no single frame that is *always* "the CLI entry point or
the MCP wrapper" -- for a CLI invocation the core function IS the entry
point; for an MCP invocation it is one frame *inside* `traced_tool`'s
wrapper.

`unit_of_work()` is therefore a re-entrant context manager keyed off a
`contextvar`, entered directly at the top of each write-site's own core
function (`run_script`, `write_submit_provenance`, `reap_runs`, the
postmortem validate/get tools, the check/rebaseline path). The first entry
in a call tree resolves the mode (after taking the writers lock) and caches
it in the contextvar; every nested entry (e.g. MCP's `traced_tool` wrapper
calling into the same core function, or one core function calling another)
sees the contextvar already set and is a no-op, sharing the outer
resolution. This satisfies "read once, never re-read" regardless of which
frame is the outermost one for a given call path, without requiring every
call path to funnel through one literal function.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from bathos.runlog.mode import is_log_mode, writers_lock
from bathos.runlog.resolve import LogRootResolution, resolve_log_root
from bathos.runlog.writer import AppendOutcome, append_event, resolve_write_project_id

logger = logging.getLogger(__name__)

_mode_cv: ContextVar[bool | None] = ContextVar("bth_runlog_mode", default=None)


class RunNotLaunchedError(RuntimeError):
    """D6: `run.started` could not be appended to the project log or the
    fallback. Per D6 the script must not be launched; callers must catch
    this (or let it propagate) before ever spawning the subprocess."""


@contextmanager
def unit_of_work(catalog_dir: Path | None = None) -> Iterator[bool]:
    """Fix the log-mode flag for one CLI process or one MCP tool invocation.

    Re-entrant: if a unit of work is already active (this is a nested call),
    this is a no-op that yields the SAME mode already resolved by the
    outermost entry -- the flag is read exactly once per call tree. Only the
    outermost entry takes the writers lock (shared, blocking; SLURM jobs
    skip it, see `bathos.runlog.mode.writers_lock`) and holds it for the
    context's duration, matching "every local command that writes ... first
    takes [the] lock, then reads the mode, and holds the lock for its
    duration."
    """
    if _mode_cv.get() is not None:
        yield _mode_cv.get()  # type: ignore[misc]
        return
    with writers_lock(catalog_dir):
        mode = is_log_mode(catalog_dir)
        token = _mode_cv.set(mode)
        try:
            yield mode
        finally:
            _mode_cv.reset(token)


def current_mode() -> bool:
    """The mode fixed by the enclosing `unit_of_work()`.

    Raises RuntimeError if called outside one: every write site must enter
    `unit_of_work()` before checking the flag, so hitting this is a bug in
    the write site, not a runtime condition to handle gracefully.
    """
    mode = _mode_cv.get()
    if mode is None:
        raise RuntimeError(
            "bathos.runlog.emit.current_mode() called outside unit_of_work() -- "
            "the caller must enter unit_of_work() before checking the flag."
        )
    return mode


def in_unit_of_work() -> bool:
    """True if a `unit_of_work()` is currently active on this task/thread."""
    return _mode_cv.get() is not None


def _project_slug_for(log_root: LogRootResolution) -> str | None:
    if log_root.unaffiliated:
        return None
    cfg_path = log_root.main_root / ".bth.toml"
    if not cfg_path.is_file():
        return None
    from bathos.config import load_project_config

    try:
        return load_project_config(cfg_path).slug
    except Exception:
        return None


def emit_event(
    *,
    kind: str,
    entity: list[str],
    data: dict[str, Any],
    cwd: Path | None = None,
    hard_fail: bool = False,
) -> AppendOutcome:
    """Append one event for a write site, resolving the log root and project
    id fresh on every call (only the MODE is cached per unit of work; the
    root/project resolution is cheap and must reflect the caller's actual
    cwd, which can differ between calls even within one long-lived MCP
    server process).

    D6: if `hard_fail` and the event landed in neither the project log nor
    the fallback, raises `RunNotLaunchedError` -- used for `run.started`,
    where the script must not be launched. Any other failure (mirror-only,
    or project+fallback failure on a `hard_fail=False` site) is logged as a
    warning only, per "other callers may choose to warn only".
    """
    log_root = resolve_log_root(cwd)
    project = _project_slug_for(log_root)
    project_id = resolve_write_project_id(log_root)
    outcome = append_event(
        kind=kind,
        entity=entity,
        data=data,
        log_root=log_root,
        project=project,
        project_id=project_id,
    )
    if not outcome.ok:
        if hard_fail:
            raise RunNotLaunchedError(
                f"{kind}: could not be appended to the project log or the fallback "
                f"(project_error={outcome.project_error!r}, "
                f"fallback_error={outcome.fallback_error!r}); per D6 the run is not launched."
            )
        logger.warning(
            "runlog: %s could not be recorded (project log and fallback both failed): "
            "project_error=%s fallback_error=%s",
            kind,
            outcome.project_error,
            outcome.fallback_error,
        )
    return outcome


def emit_or_legacy(
    *,
    kind: str,
    entity: list[str],
    data: dict[str, Any],
    legacy_write: Any,
    cwd: Path | None = None,
    hard_fail: bool = False,
) -> AppendOutcome | None:
    """The single flag branch every write site uses:

    - Flag ON: emit the event (per `emit_event`) and SKIP `legacy_write`
      entirely -- it is never called.
    - Flag OFF: call `legacy_write()` and return None; behavior is byte-for-
      byte the pre-2b legacy path.

    Must be called from inside an active `unit_of_work()` (asserts via
    `current_mode()`).
    """
    if current_mode():
        return emit_event(kind=kind, entity=entity, data=data, cwd=cwd, hard_fail=hard_fail)
    legacy_write()
    return None


def apply_run_finished_exit_semantics(script_exit_code: int, outcome: AppendOutcome) -> int:
    """D6's `run.finished` exit-code rule.

    "If `run.finished` lands only in the fallback, the command exits with
    the script's own status plus a warning ...; it exits non-zero only if
    both the project log and the fallback fail."

    Only called when the flag is ON (legacy `run.finished` writes, i.e.
    Parquet, are not best-effort in this sense and are unaffected).
    """
    if outcome.target == "none":
        logger.error(
            "runlog: run.finished could not be recorded in the project log or the "
            "fallback; exiting non-zero regardless of the script's own exit code (%r) "
            "per D6.",
            script_exit_code,
        )
        return script_exit_code if script_exit_code != 0 else 1
    if outcome.target == "fallback":
        logger.warning(
            "runlog: run.finished landed only in the fallback log (project log append "
            "failed); the script's own exit code is preserved so SLURM afterok still "
            "works."
        )
    return script_exit_code


def run_event_data(run: Any) -> dict[str, Any]:
    """JSON-safe dict of every field on a `bathos.schema.Run` row, for
    `run.finished.data` (the spec: "every field the runner sets at finish
    plus parity_run_type" -- parity_run_type is itself one of these fields,
    so the full row is a superset)."""
    d = dataclasses.asdict(run)
    ts = d.get("timestamp")
    if hasattr(ts, "isoformat"):
        d["timestamp"] = ts.isoformat()
    return d


def sidecar_declaration_for_event(sidecar: Any) -> dict[str, Any]:
    """Best-effort JSON-safe serialization of a parsed `Sidecar` dataclass for
    `run.started.data` (the spec's "parsed sidecar declaration"). Never
    raises -- an unserializable field must not block `run.started`, which is
    D6 not-best-effort for a different reason (the append itself, not this
    formatting step)."""
    if sidecar is None:
        return {}
    try:
        return dataclasses.asdict(sidecar)
    except Exception:  # pragma: no cover - defensive; must never break run.started
        with contextlib.suppress(Exception):
            return {"repr": repr(sidecar)}
        return {}


__all__ = [
    "RunNotLaunchedError",
    "apply_run_finished_exit_semantics",
    "current_mode",
    "emit_event",
    "emit_or_legacy",
    "in_unit_of_work",
    "run_event_data",
    "sidecar_declaration_for_event",
    "unit_of_work",
]
