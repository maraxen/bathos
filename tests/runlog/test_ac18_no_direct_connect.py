"""AC-18: no module other than `bathos.index` and the ingest path calls
`duckdb.connect` on a catalog path.

Spec (260925_project-local-run-log.md, AC-18): "No module other than
`bathos.index` and the ingest path calls `duckdb.connect` on a catalog path
(AST test)... Before cut-over the legacy write sites (flag-off branch only)
sit on an explicit allow-list in the AC-18 test, which step 5 empties."

**"On a catalog path", precisely:** every `duckdb.connect(...)` call site in
`src/bathos/` that opens `bathos.db` (or, in the exempt modules, `index.db`)
passes at least one argument -- a path, `read_only=`, or both. The only calls
that are NOT on a catalog path are bare, argument-less in-memory scratch
connections (`duckdb.connect()`, used by `validate.py`'s outcome-condition SQL
parse check and `query.run_sql`'s no-`catalog_dir` branch) -- there is no
argument-less call anywhere in this codebase that targets a real file, so
"has at least one argument" is an exact, not just a heuristic, stand-in for
"targets a catalog path" as of this wave. A future call that legitimately
needs an argument without targeting a catalog (e.g. `duckdb.connect(":memory:")`)
is still exempted explicitly below.

This test walks every `.py` file under `src/bathos/`, finds `duckdb.connect(`
call sites via the AST (so it survives reformatting, unlike a grep), and
fails if any such call sits outside:

- the exempt modules (`bathos/index.py`, the AC-18-named home of
  `connect_read`/`connect_legacy`; `bathos/runlog/ingest.py`, the ingest
  path, which writes `index.db` directly), or
- an entry in `ALLOWLIST` below, keyed by `(module, enclosing function name)`.

Every allow-list entry is a **legacy write site**: call sites this wave
confirmed already flag-gate their actual writes internally (via
`bathos.runlog.emit.emit_or_legacy`/`unit_of_work`, or by construction --
`bth compact`/the reaper's warm-tier reconciliation only run when the flag is
off, per spec "Mode"). Moving them onto `connect_read` would be wrong: that
function is read-only in spirit (flag-on, it's a read-only ATTACH), and some
of these opens are shared across several call sites with genuinely mixed
read/write intent (`campaigns.connect_catalog_db`) -- splitting those is
follow-up work (step 4/5), not this wave's.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "bathos"

# Modules exempt in full -- AC-18's "bathos.index and the ingest path".
EXEMPT_MODULES = {
    "index.py",
    "runlog/ingest.py",
}

# (module relative to src/bathos, enclosing function/method name) -> reason.
# Every entry is a legacy WRITE site (flag-off branch only); see module
# docstring. `None` as the function name means module-level (no enclosing
# def) -- not currently needed, kept for completeness.
ALLOWLIST: dict[tuple[str, str], str] = {
    ("campaigns.py", "_open_db"): (
        "shared writable-connection opener; each call site flag-gates its own "
        "writes via emit_or_legacy (spec's own example of a kept-as-is legacy writer)"
    ),
    ("campaigns.py", "connect_catalog_db"): (
        "dual-mode (read_only param) opener shared by 8+ legacy call sites across "
        "mcp.py/cli_cyclopts.py/blast_radius.py mixing read and write intent; "
        "splitting per-caller is deferred -- too entangled for a safe wave-d move"
    ),
    ("campaigns.py", "prepare_catalog_for_conclude"): (
        "legacy write: ingests cool campaign JSON + links cool runs into warm "
        "tables ahead of conclude/threshold checks (flag-off only; its own "
        "read-only warm_ids check moved to connect_read this wave)"
    ),
    ("anchor.py", "_connect"): (
        "CatalogAnchorStore's writable connection; insert() flag-gates its "
        "write via emit_or_legacy (spec's own example of a kept-as-is legacy writer)"
    ),
    ("blast_radius.py", "_connect"): (
        "blast-radius ledger writable connection (CREATE/ALTER + inserts); "
        "writes flag-gated at call sites"
    ),
    ("compact.py", "_open_db"): (
        "legacy compaction's connection opener; bth compact only runs the "
        "legacy path when the flag is off (spec Mode)"
    ),
    ("compact.py", "compact"): (
        "legacy cool->warm compaction pipeline; its before/after row-count "
        "telemetry reads bookend the same write transaction and only run "
        "when the flag is off"
    ),
    ("reap.py", "reconcile_warm_tier"): (
        "warm-tier reap reconciliation: backs up + force-rebuilds bathos.db "
        "(spec names this function directly; migrate step 1 disables it via "
        "reconcile_warm=False)"
    ),
    ("trust_ledger.py", "_connect"): (
        "trust-ledger writable connection (DDL + inserts); writes flag-gated "
        "at call sites"
    ),
    ("archived_items.py", "_connect"): (
        "archived-items writable connection (DDL + inserts); writes flag-gated "
        "at call sites"
    ),
    ("checker.py", "_legacy_write"): (
        "output_metadata backfill UPDATE; already named for its flag-off-only role"
    ),
    ("mcp.py", "campaign_attest_parity_tool"): (
        "opens db for claim.attest_parity(), which flag-gates its warm UPDATE "
        "via emit_or_legacy"
    ),
    ("mcp.py", "_claim_register_sync"): (
        "opens db for claim.register_claim(), which flag-gates its warm writes "
        "via emit_or_legacy/unit_of_work"
    ),
    ("mcp.py", "campaign_add_tool"): (
        "opens db for campaigns.add_run_to_campaign(), which flag-gates its "
        "campaign_runs insert"
    ),
}


def _is_duckdb_connect_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "connect"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "duckdb"
    )


def _is_bare_in_memory(node: ast.Call) -> bool:
    """True for a call with no args, or a single `":memory:"` literal arg."""
    if not node.args and not node.keywords:
        return True
    return (
        len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == ":memory:"
    )


class _ConnectCallFinder(ast.NodeVisitor):
    """Collects every `duckdb.connect(...)` call "on a catalog path", tagged
    with the name of its innermost enclosing function/method (or `None` at
    module level)."""

    def __init__(self) -> None:
        self.violations: list[tuple[int, str | None]] = []
        self._func_stack: list[str] = []

    def _enclosing(self) -> str | None:
        return self._func_stack[-1] if self._func_stack else None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        if _is_duckdb_connect_call(node) and not _is_bare_in_memory(node):
            self.violations.append((node.lineno, self._enclosing()))
        self.generic_visit(node)


def _iter_source_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def test_ac18_no_direct_duckdb_connect_outside_index_and_ingest() -> None:
    unresolved: list[str] = []

    for path in _iter_source_files():
        rel = path.relative_to(SRC_ROOT).as_posix()
        if rel in EXEMPT_MODULES:
            continue

        tree = ast.parse(path.read_text(), filename=str(path))
        finder = _ConnectCallFinder()
        finder.visit(tree)

        for lineno, func_name in finder.violations:
            key = (rel, func_name or "<module>")
            if key in ALLOWLIST:
                continue
            unresolved.append(
                f"{rel}:{lineno} in {func_name or '<module scope>'}() -- "
                f"duckdb.connect() on a catalog path outside bathos.index/ingest "
                f"and not on the AC-18 allow-list"
            )

    assert not unresolved, "AC-18 violations:\n" + "\n".join(unresolved)


def test_ac18_allowlist_entries_still_exist() -> None:
    """Every allow-list entry should correspond to a real call site.

    A stale entry (the call site was migrated or deleted) silently widens the
    allow-list for nothing -- fail so entries get pruned as step 5 (spec:
    "which step 5 empties") removes them.
    """
    seen: set[tuple[str, str]] = set()

    for path in _iter_source_files():
        rel = path.relative_to(SRC_ROOT).as_posix()
        if rel in EXEMPT_MODULES:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        finder = _ConnectCallFinder()
        finder.visit(tree)
        for _lineno, func_name in finder.violations:
            seen.add((rel, func_name or "<module>"))

    stale = sorted(set(ALLOWLIST) - seen)
    assert not stale, f"Stale AC-18 allow-list entries (no matching call site found): {stale}"


@pytest.mark.parametrize("rel", sorted(EXEMPT_MODULES))
def test_ac18_exempt_modules_exist(rel: str) -> None:
    assert (SRC_ROOT / rel).exists(), f"exempt module {rel} does not exist under {SRC_ROOT}"
