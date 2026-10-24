"""CLI-presentation rendering for registry-driven cyclopts commands.

bathos's `@cisternal.tool`-registered inner functions (`campaign_create_tool`,
`campaign_list_tool`, etc.) signal validation/business-logic failures by
returning ``{"error": "..."}`` rather than raising — a convention their
`@traced_tool`-wrapped MCP async wrappers rely on (see `traced_tool`'s
docstring in `bathos.mcp`: an "ok" key is synthesized from the presence of
"error" in the return value). `cisternal.wire()`'s CLI error contract (F1)
only catches *raised* exceptions into a clean stderr+exit(1); a dict-shaped
error instead falls through to cyclopts' own default result handling, which
prints the raw ``{'error': '...'}`` dict repr via rich and exits 0 — no
"Error:" framing, no stderr, no nonzero exit code.

`render_or_exit` closes that gap. Rather than wrapping each of the 7
campaign commands individually, it's installed once as a cyclopts App's
`result_action` callable (see `cli_cyclopts.py`), so every wired command's
return value is routed through it automatically: an ``{"error": ...}``
result gets a clean stderr message + `sys.exit(1)`, matching wire()'s own
CLI error contract; anything else renders as pretty-printed JSON (or, when
present, a plain `message` field) — the pilot's goal is a working, provable
pattern, not byte-for-byte reproduction of each command's legacy
`typer.echo` phrasing.

Kept separate from `bathos.mcp` (which stays FastMCP-anchored) — mirrors
cisternal's own separation of `cisternal/cli.py` from its MCP-facing code.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def render_or_exit(result: dict, success_template: str | None = None) -> None:
    """Render *result* to stdout, or a stderr message + exit(1) on error.

    Args:
        result: The dict returned by a `@cisternal.tool`-registered inner
            function. An ``"error"`` key (regardless of whether an "ok" key
            is also present) is treated as failure.
        success_template: Optional literal string to print on success
            instead of the default rendering (result's own `message` field
            if present, else pretty-printed JSON).
    """
    error = result.get("error")
    if error:
        report = result.get("outputs_report")
        if isinstance(report, dict):
            # `bth verify --tier outputs`: a failing run still owes the per-copy JSON on stdout.
            print(json.dumps(report, indent=2, default=str))
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)

    if _is_blast_radius_report(result) and result.get("warnings"):
        _print_warning_block(result["warnings"])

    if success_template is not None:
        print(success_template)
    elif "message" in result:
        print(result["message"])
    else:
        print(json.dumps(result, indent=2, default=str))


def _is_blast_radius_report(result: dict) -> bool:
    """True for a `BlastRadiusReport` dict (bathos.blast_radius.assess_blast_radius).

    Matched on shape, not command name, so no other command's output changes.
    """
    return "anchor_kind" in result and "unaffected_run_ids" in result


def _print_warning_block(warnings: list[str]) -> None:
    """Coverage warnings go to stderr before the JSON on stdout, so stdout stays
    parseable and the caveats are read before any bucket counts."""
    print(
        "WARNING: this blast-radius assessment has unverified coverage. "
        "Zero matches below are not evidence of a clean result.",
        file=sys.stderr,
    )
    for w in warnings:
        print(f"  WARNING: {w}", file=sys.stderr)
    print(file=sys.stderr)


def cyclopts_result_action(result: Any) -> None:
    """A cyclopts `result_action` callable: routes dict results through
    `render_or_exit`, leaves anything else untouched (defensive — every
    command in an app configured with this handler is expected to return a
    dict, but a stray non-dict return shouldn't crash rendering)."""
    if isinstance(result, dict):
        render_or_exit(result)
