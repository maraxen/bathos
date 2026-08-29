"""In-process test-invocation shim for cyclopts Apps.

``typer.testing.CliRunner`` has no cyclopts equivalent, and bathos's ~350
existing CLI-level tests depend on its ``runner.invoke(app, argv) ->
result(exit_code, output)`` shape. This module provides a minimal
CliRunner-alike (``CyclopticRunner``) sized to exactly what those assertion
patterns need — not a full port of typer's ``CliRunner`` surface (no
``isolated_filesystem()``/``input=`` stdin simulation; bathos's CLI has no
interactive prompts, so neither is needed yet).

Once a second cisternal consumer needs the same shim, it should graduate
into a reusable `cisternal.testing`-style utility rather than being
duplicated a second time (see backlog #4702 Milestone 1 plan, deferred
scope). Built bathos-side for now since cisternal has no `testing`-surface
precedent today.
"""

from __future__ import annotations

import contextlib
import io
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import cyclopts


@dataclass
class InvokeResult:
    """Result of a `CyclopticRunner.invoke()` call.

    Mirrors the subset of click 8.2+'s `Result` (typer's `CliRunner` return
    type) that bathos's existing tests assert on: `exit_code`, `stdout` and
    `stderr` captured separately, and `output` as their concatenation.
    `output` is NOT a byte-for-byte chronological interleaving of the two
    streams the way click's real `Result.output` is -- it's `stdout +
    stderr`, since bathos's CLI commands each write to one stream or the
    other per invocation rather than both interleaved, and every existing
    test assertion against `.output` is a substring check that doesn't
    depend on cross-stream ordering.
    """

    exit_code: int
    output: str  # stdout + stderr concatenated, see class docstring
    stdout: str
    stderr: str
    exception: BaseException | None = None


class CyclopticRunner:
    """Invoke a cyclopts App in-process, capturing output and exit code."""

    def invoke(self, app: cyclopts.App, args: list[str]) -> InvokeResult:
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        exit_code = 0
        exception: BaseException | None = None

        old_stderr = sys.stderr
        try:
            with contextlib.redirect_stdout(stdout_buf):
                sys.stderr = stderr_buf
                try:
                    app(args)
                except SystemExit as e:
                    exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
                except Exception as e:  # noqa: BLE001 — defensive fallback, see module docstring
                    exit_code = 1
                    exception = e
        finally:
            sys.stderr = old_stderr

        stdout_text = stdout_buf.getvalue()
        stderr_text = stderr_buf.getvalue()
        return InvokeResult(
            exit_code=exit_code,
            output=stdout_text + stderr_text,
            stdout=stdout_text,
            stderr=stderr_text,
            exception=exception,
        )
