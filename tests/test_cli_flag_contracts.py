"""Contract tests for CLI flags that are advertised in --help.

These exist because three flags (`bth lineage --depth`, `bth classify
--no-content`, `bth postmortem validate --strict`) shipped declared-but-unwired
and survived for months. Nothing caught them: every test for those commands
called the underlying library function directly, so a flag that never reached
the library was structurally invisible.

Two contracts are pinned here:
  1. `--depth` actually changes the result (it is wired end to end).
  2. The removed flags stay removed — passing them is an error, not a silent
     no-op that someone could mistake for working.
"""

from __future__ import annotations

import inspect
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bathos.catalog import init_catalog, write_run
from bathos.cli_cyclopts import app
from bathos.compact import compact
from bathos.schema import Run
from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_run(n: int, base: datetime, parent_run_id: str = "") -> Run:
    return Run(
        project_slug="test",
        command=f"python test{n}.py",
        argv=["python", f"test{n}.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=base + timedelta(seconds=n),
        status="completed",
        exit_code=0,
        parent_run_id=parent_run_id,
    )


@pytest.fixture
def lineage_chain(tmp_path: Path) -> tuple[Path, list[Run]]:
    """A 4-deep ancestor chain (run1 <- run2 <- run3 <- run4) in a warm catalog."""
    init_catalog(tmp_path)
    base = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)

    runs: list[Run] = []
    parent = ""
    for n in range(1, 5):
        r = _make_run(n, base, parent)
        write_run(r, tmp_path)
        runs.append(r)
        parent = r.id

    compact(tmp_path)
    return tmp_path, runs


# --- 1. --depth is wired end to end -----------------------------------------


def test_lineage_depth_limits_ancestor_hops(lineage_chain):
    """depth=N returns the run plus at most N ancestor hops."""
    from bathos.query import lineage

    catalog_dir, runs = lineage_chain
    leaf = runs[-1]

    # depth 0 -> just the leaf; each extra hop adds one ancestor.
    assert len(lineage(leaf.id, catalog_dir, depth=0)) == 1
    assert len(lineage(leaf.id, catalog_dir, depth=1)) == 2
    assert len(lineage(leaf.id, catalog_dir, depth=2)) == 3
    assert len(lineage(leaf.id, catalog_dir, depth=3)) == 4


def test_lineage_default_depth_returns_full_chain(lineage_chain):
    """The default (50) preserves the historical hardcoded cycle guard."""
    from bathos.query import lineage

    catalog_dir, runs = lineage_chain
    leaf = runs[-1]

    assert len(lineage(leaf.id, catalog_dir)) == len(runs)
    assert lineage(leaf.id, catalog_dir) == lineage(leaf.id, catalog_dir, depth=50)


def test_lineage_truncates_from_the_oldest_end(lineage_chain):
    """Truncation drops distant ancestors, never the run being asked about."""
    from bathos.query import lineage

    catalog_dir, runs = lineage_chain
    leaf = runs[-1]

    got = lineage(leaf.id, catalog_dir, depth=1)
    assert [r.id for r in got] == [runs[-2].id, leaf.id]


def test_lineage_negative_depth_raises(lineage_chain):
    from bathos.query import lineage

    catalog_dir, runs = lineage_chain
    with pytest.raises(ValueError, match="depth must be >= 0"):
        lineage(runs[-1].id, catalog_dir, depth=-1)


def test_cli_lineage_depth_changes_output(lineage_chain, monkeypatch):
    """The regression guard: --depth must reach the query layer.

    Before the fix this passed --depth and got identical output either way.
    """
    catalog_dir, runs = lineage_chain
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
    leaf = runs[-1]

    shallow = runner.invoke(app, ["lineage", leaf.id, "--depth", "1"])
    deep = runner.invoke(app, ["lineage", leaf.id, "--depth", "50"])

    assert shallow.exit_code == 0, shallow.output
    assert deep.exit_code == 0, deep.output

    # W3C PROV-JSON output (registry-driven cyclopts command, not the retired
    # Typer command's human-readable "outcome=" lines) -- one ancestor entity
    # vs three, over the same chain.
    shallow_entities = json.loads(shallow.output)["prov"]["entity"]
    deep_entities = json.loads(deep.output)["prov"]["entity"]
    assert len(shallow_entities) == 2
    assert len(deep_entities) == 4
    assert shallow.output != deep.output


def test_cli_lineage_rejects_negative_depth(lineage_chain, monkeypatch):
    catalog_dir, runs = lineage_chain
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

    result = runner.invoke(app, ["lineage", runs[-1].id, "--depth", "-1"])
    assert result.exit_code != 0
    assert "depth must be >= 0" in result.output


def test_mcp_lineage_prov_honours_depth(lineage_chain):
    """The MCP surface publishes `depth` in its schema, so it must work too."""
    import asyncio

    from bathos.mcp import mcp_lineage_prov_tool

    catalog_dir, runs = lineage_chain
    leaf = runs[-1]

    shallow = asyncio.run(
        mcp_lineage_prov_tool(run_id=leaf.id, catalog_dir=str(catalog_dir), depth=1)
    )
    deep = asyncio.run(
        mcp_lineage_prov_tool(run_id=leaf.id, catalog_dir=str(catalog_dir), depth=50)
    )

    assert len(shallow["prov"]["entity"]) < len(deep["prov"]["entity"])


# --- 2. Removed flags stay removed ------------------------------------------


def test_classify_no_content_flag_is_gone(tmp_path, monkeypatch):
    """--no-content named a feature rejected by design in 239fa20.

    It must error rather than be silently accepted, so nobody can believe it
    is doing something.
    """
    (tmp_path / "scripts").mkdir()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["classify", "--no-content"])
    assert result.exit_code != 0
    assert "unknown option" in result.output.lower()


def test_classify_still_runs_without_the_removed_flag(tmp_path, monkeypatch):
    """Guard against the removal having broken the command itself."""
    (tmp_path / "scripts").mkdir()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["classify"])
    assert result.exit_code == 0, result.output


def test_postmortem_validate_strict_flag_is_gone(tmp_path):
    """--strict's stated job is already done unconditionally by parse_postmortem."""
    pm = tmp_path / "x.bth.postmortem.toml"
    pm.write_text("")

    result = runner.invoke(app, ["postmortem", "validate", str(pm), "--strict"])
    assert result.exit_code != 0
    assert "unknown option" in result.output.lower()


def test_postmortem_validate_strict_files_still_accepted(tmp_path):
    """--strict-files is genuinely wired and must survive the removal of --strict."""
    pm = tmp_path / "x.bth.postmortem.toml"
    pm.write_text("")

    result = runner.invoke(app, ["postmortem", "validate", str(pm), "--strict-files"])
    # Parse fails on an empty file, but the option itself must be recognised.
    assert "unknown option" not in result.output.lower()


# --- 3. Doc surfaces don't drift from the real flag names -------------------
#
# praxia debt #1660: README/skill/docs examples said `bth run --out/--tag
# --campaign`, but those flags are actually `--output-paths/--tags
# --campaign-id` (cyclopts derives CLI flag names from the underlying
# `@cisternal.tool` function's parameter names, kebab-cased -- see
# src/bathos/mcp.py's `run_cli_tool`). Copy-pasting a documented invocation
# then failed with "Unknown option".
#
# This guard re-derives each command's real flag set from its live cyclopts
# signature (never a hardcoded list, so it can't itself go stale the same
# way) and checks every single-line `bth <command> ...` invocation in the
# doc surfaces that were fixed for #1660. It intentionally does NOT try to
# parse multi-line shell continuations beyond a simple backslash-join, and it
# stops each invocation at a bare `--` token -- everything after that is
# forwarded to the user's own script/process, not parsed by bth itself, so
# flags there (e.g. `bth run -- uv run python train.py --out foo.json`) are
# correctly out of scope.

# command name (as it appears right after "bth ") -> cyclopts app path
COMMAND_APP_PATHS: dict[str, tuple[str, ...]] = {
    "run": ("run",),
    "submit": ("submit",),
    "find": ("find",),
    "campaign add": ("campaign", "add"),
}

# Flags valid for every command regardless of its own signature.
UNIVERSAL_FLAGS = {"--help"}


def _valid_flags_for(app_path: tuple[str, ...]) -> set[str]:
    """Derive the real `--flag` names for a cyclopts command from its live signature."""
    sub = app
    for part in app_path:
        sub = sub[part]
    fn = sub.default_command
    assert fn is not None, f"no default_command for app path {app_path!r}"

    flags = set()
    for name, param in inspect.signature(fn).parameters.items():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        flags.add("--" + name.replace("_", "-"))
    return flags


_FLAG_RE = re.compile(r"--[a-zA-Z][a-zA-Z0-9-]*")


def _bth_invocations(text: str, command: str) -> list[str]:
    """Extract single-line `bth <command> ...` invocations from doc text.

    Joins simple backslash line-continuations first, then returns each
    matching invocation truncated at a bare `--` token (the passthrough
    separator), so passthrough args are never mistaken for bth's own flags.

    `bth run` is special-cased: its `*args` parameter is annotated
    `allow_leading_hyphen=True` (mcp.py's `run_cli_tool`), so once a bare
    (non-`--`) SCRIPT_PATH-shaped token is consumed positionally, cyclopts
    hands every later `--foo`-looking token to the script instead of
    matching it against `run`'s own options -- verified empirically:
    `bth run --no-sidecar /tmp/t.py --out foo.json` exits 0 and forwards
    `--out foo.json` to the script, it does not raise "Unknown option" the
    way a leading `bth run --out foo.json -- ...` does. An invocation shaped
    `bth run <SCRIPT_PATH> --flag ...` is therefore genuinely ambiguous
    (untestable from text alone) and is skipped rather than flagged.
    """
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    prefix = f"bth {command} "
    prefix_len = len(prefix.split())
    invocations = []
    for line in joined.splitlines():
        line = line.strip().strip("`")
        if not line.startswith(prefix):
            continue
        tokens = line.split()
        if (
            command == "run"
            and len(tokens) > prefix_len
            and not tokens[prefix_len].startswith("-")
        ):
            continue
        cut = len(tokens)
        for i, tok in enumerate(tokens):
            if tok == "--":
                cut = i
                break
        invocations.append(" ".join(tokens[:cut]))
    return invocations


# Doc surfaces known to document `bth run` / `bth submit` / `bth find` /
# `bth campaign add` invocations -- the surfaces #1660 grepped. Historical
# planning docs under docs/superpowers/{plans,specs}/ are deliberately
# excluded: they are dated records of past design, not living usage docs.
DOC_SURFACES = [
    "README.md",
    "agent_assets/using_bathos/SKILL.md",
    "agent_assets/skills/using-bathos/SKILL.md",
    "agent_assets/snippets/rules.md",
    "agent_assets/skills/bathos-campaigns/SKILL.md",
    "agent_assets/skills/bathos-cluster/SKILL.md",
    "agent_assets/skills/bathos-trust-ledger/SKILL.md",
    "agent_assets/agents/experiment-runner.md",
    "docs/source/user-guide.rst",
    "docs/source/slurm-integration.rst",
]


@pytest.mark.parametrize("command", list(COMMAND_APP_PATHS))
def test_doc_invocations_use_real_flags(command):
    """Every documented `bth <command> ...` invocation uses a flag the CLI accepts."""
    valid = _valid_flags_for(COMMAND_APP_PATHS[command]) | UNIVERSAL_FLAGS
    violations: list[str] = []

    for rel_path in DOC_SURFACES:
        path = REPO_ROOT / rel_path
        if not path.exists():
            continue
        text = path.read_text()
        for invocation in _bth_invocations(text, command):
            used = set(_FLAG_RE.findall(invocation))
            unknown = used - valid
            if unknown:
                violations.append(f"{rel_path}: {invocation!r} uses unknown flag(s) {sorted(unknown)}")

    assert not violations, "stale/invalid flags in documented invocations:\n" + "\n".join(violations)
