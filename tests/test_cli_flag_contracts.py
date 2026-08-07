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

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bathos.catalog import init_catalog, write_run
from bathos.cli import app
from bathos.compact import compact
from bathos.schema import Run

runner = CliRunner()


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

    # One ancestor line vs three, over the same chain.
    assert shallow.output.count("outcome=") == 2
    assert deep.output.count("outcome=") == 4
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
    assert "no such option" in result.output.lower()


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
    assert "no such option" in result.output.lower()


def test_postmortem_validate_strict_files_still_accepted(tmp_path):
    """--strict-files is genuinely wired and must survive the removal of --strict."""
    pm = tmp_path / "x.bth.postmortem.toml"
    pm.write_text("")

    result = runner.invoke(app, ["postmortem", "validate", str(pm), "--strict-files"])
    # Parse fails on an empty file, but the option itself must be recognised.
    assert "no such option" not in result.output.lower()
