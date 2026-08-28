#!/usr/bin/env python3
"""Generate a review-first CLI->MCP mapping for the typer->cyclopts migration.

Backlog #4702 Milestone 1: cli.py's 78 hand-written typer commands need to be
re-expressed as `@cisternal.tool(registry="bathos-cli", cli_group=..., cli_name=...)`
registrations on the same inner functions mcp.py's MCP tools already call, so
one function yields both an MCP tool and a CLI command via cisternal's
`wire()`. The `campaign` group (7 commands) was migrated by hand as the
Milestone 1 pilot, proving the pattern; this script exists to make migrating
the remaining 11 groups (and 27 top-level commands) mostly mechanical instead
of another 11 rounds of manual cross-referencing.

WHAT THIS SCRIPT DOES AND DOES NOT DO
--------------------------------------
Does: parse cli.py's Typer command surface via `ast`, introspect bathos.mcp's
live "bathos" cisternal registry for the ground-truth MCP tool list, fuzzy-join
the two (seeded with the ~20 rename pairs already confirmed by hand during
Milestone 1's research), and emit a reviewable TOML mapping table.

Does NOT: automatically edit mcp.py. A command whose row is marked "verified"
in a human-reviewed map can have its decorator kwargs generated to stdout via
`--emit-decorators`, for a human (or a fixer agent) to paste in — this script
never mutates source files itself. It also does NOT generate the CLI-rendering
wrapper bodies (dict-error -> stderr+exit(1) glue, see `bathos.cli_render`) —
those are bespoke per command and were hand-written for the campaign pilot;
templating them is explicitly deferred until 2+ groups have proven what's
reusable there.

IMPORTANT CAVEAT -- `inner_fn` is a starting point, not the final answer.
The mapping's `inner_fn` is read from the "bathos" (MCP) registry, i.e. it is
the name of the `async def mcp_x_tool(...)` wrapper `@traced_tool` decorates
-- almost never the plain, sync, business-logic function CLI wiring actually
needs (`wire()`'s CLI path calls the target directly, not via `asyncio.run`,
so an async function there silently returns an unawaited coroutine instead of
running). The campaign pilot's 7 commands split into two cases when
resolving this by hand: 6 already delegated to a separately-named plain sync
function (e.g. `mcp_campaign_add_tool` -> `campaign_add_tool`) that the new
`registry="bathos-cli"` decorator was added to directly; 1
(`claim_attest_parity`) had no such delegate and needed one extracted
(`campaign_attest_parity_tool`, see `bathos/mcp.py`). Expect the same mix
across the remaining 11 groups -- `--emit-decorators`' output paste-target
comment names the MCP-registry function as a lead, not a guarantee; verify
by reading it before pasting.

Usage::

    uv run python scripts/analysis/migrate_cli_to_cyclopts.py
    uv run python scripts/analysis/migrate_cli_to_cyclopts.py --map-out scripts/analysis/cli_migration_map.toml
    uv run python scripts/analysis/migrate_cli_to_cyclopts.py --emit-decorators --map-in scripts/analysis/cli_migration_map.toml
"""

from __future__ import annotations

import argparse
import ast
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("migrate_cli_to_cyclopts")

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PY = REPO_ROOT / "src" / "bathos" / "cli.py"
DEFAULT_MAP_PATH = REPO_ROOT / "scripts" / "analysis" / "cli_migration_map.toml"

# Rename pairs confirmed by hand during Milestone 1 research + the campaign
# pilot (Explore-agent findings + the pilot's own verified rows). Keyed by
# (cli_group_or_None, cli_name) -> mcp_name. Consulted before falling back to
# the normalize-and-compare heuristic below.
SEEDED_PAIRS: dict[tuple[str | None, str], str] = {
    (None, "ls"): "list_runs",
    (None, "find"): "find_runs",
    (None, "show"): "get_run",
    (None, "cite"): "cite_run",
    (None, "lineage"): "lineage_prov",
    (None, "sql"): "run_sql",
    (None, "archive-artifact"): "archive_artifact",
    (None, "new-experiment"): "new_experiment",
    (None, "validate-sidecar"): "validate_sidecar",
    ("campaign", "ls"): "campaign_list",
    ("campaign", "create"): "campaign_create",
    ("campaign", "add"): "campaign_add",
    ("campaign", "conclude"): "campaign_conclude",
    ("campaign", "attest-parity"): "claim_attest_parity",
    ("campaign", "show"): "campaign_show",
    ("campaign", "review"): "campaign_review",
    ("postmortem", "show"): "postmortem_get",
    ("query", "trust-state"): "get_trust_state",
    ("query", "figures"): "figure_lookup",
    ("query", "candidates"): "list_candidates",
    ("query", "blast-status"): "get_blast_radius_status",
    ("anchor", "figure-register"): "figure_entry_register",
    ("outputs", "list"): "list_outputs",
    ("outputs", "summary"): "outputs_summary",
    ("ref", "show"): "reference_get",
    ("ref", "list"): "reference_list",
    ("ref", "search"): "reference_search",
    ("ref", "applicable"): "reference_applicable",
    ("blast-radius", "assess"): "blast_radius_assess",
    ("blast-radius", "clear"): "blast_radius_clear",
}

# CLI commands with no MCP-tool equivalent at all -- confirmed by the
# Milestone 1 research pass. Rows for these are emitted as
# status="no_mcp_equivalent" rather than left to fall through to
# "needs_review", so a human doesn't waste time re-deriving this.
KNOWN_CLI_ONLY: set[tuple[str | None, str]] = {
    (None, "migrate"),
    (None, "migrate-to-project-subdirs"),
    (None, "classify"),
    (None, "sprint-audit"),
    (None, "catalog-version"),
    (None, "view"),
    (None, "export"),
    (None, "submit"),
    ("remote", "add"),
    ("remote", "list"),
    ("remote", "remove"),
    ("remote", "test"),
    ("report", "emit"),
    ("report", "show"),
    ("report", "show-manifest"),
    ("provenance", "show"),
    ("provenance", "diff"),
    ("provenance", "import"),
    ("blast-radius", "shadow-check"),
    ("blast-radius", "install-hook"),
    ("blast-radius", "uninstall-hook"),
    ("query", "shadow-log"),
}


@dataclass
class CliCommand:
    cli_group: str | None
    cli_name: str
    fn_name: str
    params: list[str] = field(default_factory=list)


@dataclass
class MappingRow:
    cli_group: str | None
    cli_name: str
    mcp_name: str | None
    inner_fn: str
    status: str  # "verified" | "needs_review" | "no_mcp_equivalent"


def _typer_command_name(decorator: ast.expr, fn_name: str) -> str:
    """Extract the CLI command name from a `@sub.command("name")` /
    `@sub.command()` decorator call, falling back to the kebab-cased function
    name (typer's own default) when no explicit name is given."""
    if isinstance(decorator, ast.Call):
        for arg in decorator.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return arg.value
        for kw in decorator.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                return str(kw.value.value)
    return fn_name.replace("_", "-")


def parse_cli_commands(cli_py: Path) -> list[CliCommand]:
    """Walk cli.py's AST for every `@app.command`/`@<subapp>.command`-decorated
    function, and every `app.add_typer(<subapp>, name="...")` call mapping a
    sub-Typer variable to its CLI group name."""
    tree = ast.parse(cli_py.read_text(), filename=str(cli_py))

    subapp_group_names: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "add_typer":
            continue
        if not node.args or not isinstance(node.args[0], ast.Name):
            continue
        subapp_var = node.args[0].id
        for kw in node.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                subapp_group_names[subapp_var] = str(kw.value.value)

    commands: list[CliCommand] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            call = dec if isinstance(dec, ast.Call) else None
            func_expr = call.func if call is not None else dec
            if not isinstance(func_expr, ast.Attribute) or func_expr.attr != "command":
                continue
            if not isinstance(func_expr.value, ast.Name):
                continue
            owner = func_expr.value.id
            cli_group = subapp_group_names.get(owner) if owner != "app" else None
            cli_name = _typer_command_name(dec, node.name)
            params = [a.arg for a in node.args.args if a.arg != "self"]
            commands.append(
                CliCommand(cli_group=cli_group, cli_name=cli_name, fn_name=node.name, params=params)
            )
    return commands


def load_mcp_registry_names() -> dict[str, str]:
    """Import bathos.mcp (executing its @cisternal.tool decorations) and
    return {mcp_name: inner_fn_name} for the "bathos" registry partition."""
    from cisternal.registration.registry import snapshot

    import bathos.mcp  # noqa: F401 -- decoration side effect

    snap = snapshot("bathos")
    return {name: entry.fn.__name__ for name, entry in snap.items()}


def _normalize(s: str) -> str:
    return s.replace("-", "").replace("_", "").lower()


def build_mapping(cli_commands: list[CliCommand], mcp_names: dict[str, str]) -> list[MappingRow]:
    normalized_mcp = {_normalize(name): name for name in mcp_names}
    rows: list[MappingRow] = []

    for cmd in cli_commands:
        key = (cmd.cli_group, cmd.cli_name)

        if key in KNOWN_CLI_ONLY:
            rows.append(
                MappingRow(cmd.cli_group, cmd.cli_name, None, cmd.fn_name, "no_mcp_equivalent")
            )
            continue

        mcp_name = SEEDED_PAIRS.get(key)
        if mcp_name is None:
            # Heuristic 1: group_clicame concatenation (e.g. campaign+add -> campaign_add)
            candidate = _normalize(f"{cmd.cli_group}_{cmd.cli_name}" if cmd.cli_group else cmd.cli_name)
            mcp_name = normalized_mcp.get(candidate)
        if mcp_name is None:
            # Heuristic 2: bare cli_name match, ignoring group (catches short aliases
            # not in the seed table, e.g. a future group's own "ls"-style alias)
            mcp_name = normalized_mcp.get(_normalize(cmd.cli_name))

        if mcp_name is not None and mcp_name in mcp_names:
            rows.append(
                MappingRow(
                    cmd.cli_group, cmd.cli_name, mcp_name, mcp_names[mcp_name], "verified"
                )
            )
        else:
            rows.append(
                MappingRow(cmd.cli_group, cmd.cli_name, mcp_name, cmd.fn_name, "needs_review")
            )

    return rows


def write_toml(rows: list[MappingRow], out_path: Path) -> None:
    lines = [
        "# Generated by scripts/analysis/migrate_cli_to_cyclopts.py -- review before use.",
        "# status: verified (mcp_name/inner_fn confirmed) | needs_review (guessed or unmatched)",
        "#       | no_mcp_equivalent (stays a hand-written cyclopts command, never registry-driven)",
        "",
    ]
    for row in sorted(rows, key=lambda r: (r.cli_group or "", r.cli_name)):
        lines.append("[[mapping]]")
        # TOML has no null literal -- a None value is represented by omitting
        # the key entirely (consumers use dict.get(key), which returns None
        # for an absent key exactly as it would for an explicit null).
        if row.cli_group is not None:
            lines.append(f'cli_group = "{row.cli_group}"')
        lines.append(f'cli_name = "{row.cli_name}"')
        if row.mcp_name:
            lines.append(f'mcp_name = "{row.mcp_name}"')
        lines.append(f'inner_fn = "{row.inner_fn}"')
        lines.append(f'status = "{row.status}"')
        lines.append("")
    out_path.write_text("\n".join(lines))
    log.info("Wrote %d mapping rows to %s", len(rows), out_path)


def emit_decorators(map_path: Path) -> None:
    """Read a (human-reviewed) mapping TOML and print the
    `@cisternal.tool(registry="bathos-cli", ...)` decorator line for every
    "verified" row -- for a human/fixer to paste onto the named inner_fn in
    mcp.py. Never edits mcp.py itself."""
    import tomllib

    data = tomllib.loads(map_path.read_text())
    verified = [row for row in data.get("mapping", []) if row.get("status") == "verified"]
    if not verified:
        log.warning("No status=\"verified\" rows found in %s", map_path)
        return

    print(
        "# CAUTION: inner_fn below is the MCP async wrapper's name, not necessarily",
        "# the plain sync function to paste this onto -- see the module docstring's",
        "# 'IMPORTANT CAVEAT' section before pasting any of these.",
        sep="\n",
    )
    for row in verified:
        group = row.get("cli_group")
        group_kw = f', cli_group="{group}"' if group else ""
        print(
            f'@cisternal.tool(registry="bathos-cli", name="{row["mcp_name"]}"'
            f'{group_kw}, cli_name="{row["cli_name"]}")'
        )
        print(f"# ^ verify then paste above the real sync fn for: {row['inner_fn']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli-py", type=Path, default=CLI_PY)
    parser.add_argument("--map-out", type=Path, default=DEFAULT_MAP_PATH, help="Where to write the generated mapping TOML")
    parser.add_argument("--map-in", type=Path, default=DEFAULT_MAP_PATH, help="Mapping TOML to read for --emit-decorators")
    parser.add_argument(
        "--emit-decorators",
        action="store_true",
        help="Skip generation; print decorator lines for verified rows in --map-in",
    )
    args = parser.parse_args()

    if args.emit_decorators:
        emit_decorators(args.map_in)
        return 0

    cli_commands = parse_cli_commands(args.cli_py)
    log.info("Parsed %d CLI commands from %s", len(cli_commands), args.cli_py)

    mcp_names = load_mcp_registry_names()
    log.info("Loaded %d MCP tool names from the 'bathos' registry", len(mcp_names))

    rows = build_mapping(cli_commands, mcp_names)
    verified = sum(1 for r in rows if r.status == "verified")
    needs_review = sum(1 for r in rows if r.status == "needs_review")
    cli_only = sum(1 for r in rows if r.status == "no_mcp_equivalent")
    log.info(
        "Mapping: %d verified, %d needs_review, %d no_mcp_equivalent (total %d)",
        verified,
        needs_review,
        cli_only,
        len(rows),
    )

    write_toml(rows, args.map_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
