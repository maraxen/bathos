"""Contract tests binding each MCP tool wrapper to the implementation it forwards to.

Why this file exists
--------------------
`mcp_archive_tool` declared `keep_cool: bool = False` and forwarded it to
`archive_tool(...)`, which has no such parameter. Every authenticated call therefore
raised ``TypeError: archive_tool() got an unexpected keyword argument 'keep_cool'``.
The tool was completely broken, and nothing noticed, for two compounding reasons:

1. ``@traced_tool`` never re-raises to the transport layer — it catches everything
   and shapes it into an ``ok=False`` envelope. A tool that cannot run at all is
   indistinguishable from one reporting an ordinary domain error.
2. The only test touching that tool, ``tests/test_mcp_auth.py``, exercises the
   ``@require_write_token`` rejection path, which short-circuits *before* the body.
   It passed against a function whose body could never execute.

`archive_tool`'s own docstring documented `keep_cool`, so reading the code did not
reveal it either. Only checking the two signatures against each other does.

The invariant
-------------
Wrappers follow the convention ``mcp_<name>_tool`` → ``<name>_tool``. Every parameter
a wrapper declares must be one its implementation accepts, except ``token``, which is
consumed by ``@require_write_token`` and deliberately never forwarded.

This is the same defect class as the three dead CLI flags in #39: a declared
parameter with nothing behind it. There the surface was ``--help``; here it is the
published MCP schema, which is worse — an agent reads the schema, passes the
argument, and gets a plausible-looking error back.
"""

from __future__ import annotations

import inspect

import pytest

import bathos.mcp as mcp_module

# Consumed by @require_write_token before the body runs; never forwarded.
_DECORATOR_CONSUMED = {"token"}


def _wrapper_impl_pairs() -> list[tuple[str, str]]:
    """Every ``mcp_<name>_tool`` that has a matching ``<name>_tool`` implementation."""
    pairs = []
    for wrapper_name in sorted(dir(mcp_module)):
        if not (wrapper_name.startswith("mcp_") and wrapper_name.endswith("_tool")):
            continue
        impl_name = wrapper_name.removeprefix("mcp_")
        if hasattr(mcp_module, impl_name):
            pairs.append((wrapper_name, impl_name))
    return pairs


def test_pairs_are_discovered() -> None:
    """Guard the guard: a rename that breaks the convention must not silently
    empty this suite into a no-op that passes."""
    pairs = _wrapper_impl_pairs()
    assert len(pairs) >= 30, f"expected the bulk of MCP tools to pair, found {len(pairs)}"
    assert ("mcp_archive_tool", "archive_tool") in pairs


@pytest.mark.parametrize(("wrapper_name", "impl_name"), _wrapper_impl_pairs())
def test_wrapper_params_are_accepted_by_impl(wrapper_name: str, impl_name: str) -> None:
    """No wrapper may declare a parameter its implementation cannot accept."""
    wrapper = getattr(mcp_module, wrapper_name)
    impl = getattr(mcp_module, impl_name)

    wrapper_params = set(inspect.signature(wrapper).parameters) - _DECORATOR_CONSUMED
    impl_params = set(inspect.signature(impl).parameters)

    undeliverable = wrapper_params - impl_params
    assert not undeliverable, (
        f"{wrapper_name} declares {sorted(undeliverable)}, which {impl_name} does not "
        f"accept. Forwarding it raises TypeError, and @traced_tool turns that into an "
        f"ok=False envelope instead of a crash — so the tool looks merely unhappy "
        f"rather than broken. Either add the parameter to {impl_name} and implement "
        f"it, or drop it from the wrapper and its docstring."
    )


def test_archive_tool_accepts_exactly_what_its_wrapper_forwards() -> None:
    """The specific regression: archive_tool must accept the wrapper's real call.

    Pinned separately from the parametrized sweep so the original bug has a named
    test, and so this still fails if the naming convention above is ever reworked.
    """
    impl_params = set(inspect.signature(mcp_module.archive_tool).parameters)
    assert "keep_cool" not in impl_params, (
        "archive_tool grew a keep_cool parameter — if that is intentional, "
        "bathos.archive.archive() must actually retain or remove cool-tier "
        "fragments, which it does not do today; it only exports warm→cold."
    )

    # Make the call the wrapper actually makes. Python validates kwargs before the
    # body runs, so a signature mismatch raises TypeError here without needing a
    # catalog; the empty-project guard returns first and keeps this off the disk.
    result = mcp_module.archive_tool(catalog_dir="", project="")
    assert result == {"error": "project parameter is required"}
