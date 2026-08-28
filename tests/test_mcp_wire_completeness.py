"""The wire() `expected=` guard must be exhaustive in BOTH directions.

``cisternal.wire(..., expected=[...])`` raises ``CisternalWireError`` when a name in
*expected* is missing from the registry snapshot. It does **not** check the converse:
a tool that is registered but absent from *expected* is wired silently and is covered
by no guard at all.

That asymmetry is not hypothetical. When this test was written, five tools --
``archive_artifact``, ``restore``, ``blast_radius_assess``, ``blast_radius_clear`` and
``get_blast_radius_status`` -- were registered on the server but missing from
``expected``, so the import-time guard that exists specifically to catch a
missing/misspelled registration was blind to nearly a tenth of the tool surface.

This test closes the other direction: the two sets must be equal.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

MCP_SOURCE = Path(__file__).resolve().parents[1] / "src" / "bathos" / "mcp.py"


def _tool_name(decorator: ast.expr, func_name: str) -> str | None:
    """Return the exposed name for a ``@cisternal.tool`` decorator, else None.

    The exposed name is the ``name=`` keyword when present -- tools are frequently
    declared as ``mcp_<x>_tool`` wrappers with ``name="<x>"`` -- and otherwise the
    Python function name.
    """
    if not isinstance(decorator, ast.Call):
        return None
    target = decorator.func
    if not (
        isinstance(target, ast.Attribute)
        and target.attr == "tool"
        and isinstance(target.value, ast.Name)
        and target.value.id == "cisternal"
    ):
        return None
    for kw in decorator.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    return func_name


def _parse_mcp_module() -> tuple[set[str], set[str]]:
    """Return (registered tool names, names listed in wire's `expected=`)."""
    tree = ast.parse(MCP_SOURCE.read_text(), filename=str(MCP_SOURCE))

    registered: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            name = _tool_name(dec, node.name)
            if name is not None:
                registered.add(name)
                break

    expected: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_wire = (isinstance(func, ast.Attribute) and func.attr == "wire") or (
            isinstance(func, ast.Name) and func.id == "wire"
        )
        if not is_wire:
            continue
        for kw in node.keywords:
            if kw.arg == "expected" and isinstance(kw.value, ast.List):
                for elt in kw.value.elts:
                    if isinstance(elt, ast.Constant):
                        expected.add(str(elt.value))

    return registered, expected


def test_module_parses_and_finds_both_sets():
    """Guard the guard: if this AST walk stops finding tools, the test below is vacuous."""
    registered, expected = _parse_mcp_module()
    assert len(registered) > 40, f"only found {len(registered)} registered tools -- walk is broken"
    assert len(expected) > 40, f"only found {len(expected)} expected names -- walk is broken"


def test_every_registered_tool_is_listed_in_expected():
    """The direction cisternal.wire() does NOT check."""
    registered, expected = _parse_mcp_module()
    unlisted = sorted(registered - expected)
    assert not unlisted, (
        "these tools are registered with @cisternal.tool but missing from wire(expected=[...]), "
        "so no import-time guard covers them:\n  " + "\n  ".join(unlisted)
    )


def test_every_expected_name_is_registered():
    """The direction cisternal.wire() does check -- asserted here so it fails fast in CI."""
    registered, expected = _parse_mcp_module()
    missing = sorted(expected - registered)
    assert not missing, (
        "these names are in wire(expected=[...]) but no @cisternal.tool registers them:\n  "
        + "\n  ".join(missing)
    )


def test_server_module_imports_without_wire_error():
    """wire() runs at import time; a mismatch raises CisternalWireError there."""
    pytest.importorskip("fastmcp")
    import bathos.mcp  # noqa: F401
