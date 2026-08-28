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

    Mirrors the subset of typer's `CliRunner` `Result` that bathos's
    existing tests assert on: `exit_code` and the combined stdout+stderr
    text (typer's `CliRunner` default is `mix_stderr=True`, so `.output`
    and `.stdout` are aliases of the same captured stream here too).
    """

    exit_code: int
    output: str
    stdout: str
    exception: BaseException | None = None


class CyclopticRunner:
    """Invoke a cyclopts App in-process, capturing output and exit code."""

    def invoke(self, app: cyclopts.App, args: list[str]) -> InvokeResult:
        buf = io.StringIO()
        exit_code = 0
        exception: BaseException | None = None

        old_stderr = sys.stderr
        try:
            with contextlib.redirect_stdout(buf):
                sys.stderr = buf
                try:
                    app(args)
                except SystemExit as e:
                    exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
                except Exception as e:  # noqa: BLE001 — defensive fallback, see module docstring
                    exit_code = 1
                    exception = e
        finally:
            sys.stderr = old_stderr

        text = buf.getvalue()
        return InvokeResult(exit_code=exit_code, output=text, stdout=text, exception=exception)
