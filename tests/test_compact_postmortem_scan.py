"""Tests for compact()'s workspace postmortem scan.

Two distinct defects are pinned here, both in the same loop in compact.py:

1. Backlog #4233 -- the scan used a bare `workspace_root.rglob()` with no
   repo-boundary awareness, so postmortems inside vendored submodules and nested
   `.claude/worktrees/` checkouts were collected. Unlike the linter's version of
   this bug (fixed in #45) which merely duplicated warnings, this one builds a
   map keyed by run_id, so a foreign or stale copy silently overwrites the live
   entry.

2. A `rel_path` scoping bug -- `rel_path` was assigned inside the
   `if pm.status != "draft"` guard but consumed outside it, so a draft
   postmortem carrying a run_id either raised NameError (swallowed by the
   surrounding `except Exception`) or, once any non-draft file had been seen,
   silently reused *that* file's path. `scaffold_postmortem_template` writes
   `run_id` and `status = "draft"` together, so every scaffolded postmortem hits
   this. The consequence is not cosmetic: postmortem_map feeds verdict_override
   into the run's recorded outcome (compact.py, `outcome = ...` branch), so an
   unfinished postmortem could overwrite a run's result.
"""

from pathlib import Path

import duckdb
import pytest

from bathos.catalog import init_catalog, write_run
from bathos.compact import compact
from bathos.schema import Run

DRAFT_PM = """run_id = "{run_id}"

[postmortem]
hypothesis_status = "refuted"
summary = "half-written"
verdict_override = "{override}"
author = ""
status = "draft"
"""

SUBMITTED_PM = """run_id = "{run_id}"

[postmortem]
hypothesis_status = "held"
summary = "done"
verdict_override = "{override}"
author = "me"
status = "submitted"
"""


def _run(slug: str = "testproj") -> Run:
    return Run(
        project_slug=slug,
        command="python scripts/experiments/run.py",
        argv=["python", "scripts/experiments/run.py"],
        git_hash="deadbeef",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        duration_s=1.0,
        output_paths=[],
        tags=[],
        hostname="test-host",
    )


def _outcome(catalog: Path, run_id: str) -> str:
    con = duckdb.connect(str(catalog / "bathos.db"), read_only=True)
    try:
        row = con.execute("SELECT outcome FROM runs WHERE id = ?", [run_id]).fetchone()
        return (row[0] if row else "") or ""
    finally:
        con.close()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    (ws / "scripts" / "experiments").mkdir(parents=True)
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(ws))
    return ws


def test_draft_postmortem_does_not_override_run_outcome(tmp_catalog, workspace):
    """A draft postmortem must not reach postmortem_map at all.

    Pinned with a submitted postmortem for a *different* run present, because the
    pre-fix bug only surfaces once `rel_path` has been bound by an earlier
    iteration -- a lone draft raised NameError instead and was swallowed.
    """
    init_catalog(tmp_catalog)
    submitted_run, draft_run = _run(), _run()
    write_run(submitted_run, tmp_catalog)
    write_run(draft_run, tmp_catalog)

    exp = workspace / "scripts" / "experiments"
    # Names chosen so the submitted file sorts first and is processed first.
    (exp / f"a_done.py.{submitted_run.id}.bth.postmortem.toml").write_text(
        SUBMITTED_PM.format(run_id=submitted_run.id, override="pass")
    )
    (exp / f"b_wip.py.{draft_run.id}.bth.postmortem.toml").write_text(
        DRAFT_PM.format(run_id=draft_run.id, override="fail")
    )

    compact(tmp_catalog)

    assert _outcome(tmp_catalog, submitted_run.id) == "pass"
    assert _outcome(tmp_catalog, draft_run.id) != "fail", (
        "draft postmortem's verdict_override leaked into the run's outcome"
    )


def test_lone_draft_postmortem_is_skipped_without_error(tmp_catalog, workspace, caplog):
    """A draft postmortem is an expected state, not a parse failure."""
    init_catalog(tmp_catalog)
    run = _run()
    write_run(run, tmp_catalog)

    exp = workspace / "scripts" / "experiments"
    (exp / f"wip.py.{run.id}.bth.postmortem.toml").write_text(
        DRAFT_PM.format(run_id=run.id, override="fail")
    )

    with caplog.at_level("WARNING"):
        compact(tmp_catalog)

    assert "Skipping postmortem parse" not in caplog.text, (
        "a draft postmortem was reported as a parse failure (NameError on rel_path)"
    )
    assert _outcome(tmp_catalog, run.id) != "fail"


@pytest.fixture
def parsed_paths(monkeypatch):
    """Record every path compact() hands to parse_postmortem.

    Asserting on *which files were visited* rather than on the winning map entry
    is deliberate: with two same-run_id postmortems the map's final value depends
    on filesystem walk order, so an outcome-based assertion passes or fails by
    luck. This observes the boundary decision directly.
    """
    import bathos.postmortem as pm_mod

    seen: list[Path] = []
    real = pm_mod.parse_postmortem

    def spy(path, *a, **kw):
        seen.append(Path(path))
        return real(path, *a, **kw)

    monkeypatch.setattr(pm_mod, "parse_postmortem", spy)
    return seen


def test_postmortem_inside_nested_git_boundary_is_ignored(tmp_catalog, workspace, parsed_paths):
    """Backlog #4233: a nested worktree checkout's postmortem must not be collected.

    A stale copy carrying the same run_id would otherwise overwrite the live entry
    in postmortem_map -- silently, because the map is keyed by run_id.
    """
    init_catalog(tmp_catalog)
    run = _run()
    write_run(run, tmp_catalog)

    exp = workspace / "scripts" / "experiments"
    live = exp / f"live.py.{run.id}.bth.postmortem.toml"
    live.write_text(SUBMITTED_PM.format(run_id=run.id, override="pass"))

    wt = workspace / ".claude" / "worktrees" / "wt-old"
    stale_dir = wt / "scripts" / "experiments"
    stale_dir.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt-old\n")
    stale = stale_dir / f"live.py.{run.id}.bth.postmortem.toml"
    stale.write_text(SUBMITTED_PM.format(run_id=run.id, override="fail"))

    compact(tmp_catalog)

    assert live in parsed_paths, "the live postmortem was not scanned at all"
    assert stale not in parsed_paths, (
        "a postmortem inside a nested worktree checkout was scanned; keyed by run_id "
        "it can silently overwrite the live entry"
    )


def test_postmortem_inside_submodule_boundary_is_ignored(tmp_catalog, workspace, parsed_paths):
    """Same as above for a vendored submodule (`.git` as a directory)."""
    init_catalog(tmp_catalog)
    run = _run()
    write_run(run, tmp_catalog)

    exp = workspace / "scripts" / "experiments"
    live = exp / f"live.py.{run.id}.bth.postmortem.toml"
    live.write_text(SUBMITTED_PM.format(run_id=run.id, override="pass"))

    vendored = workspace / "external" / "dep"
    (vendored / ".git").mkdir(parents=True)
    stale = vendored / f"live.py.{run.id}.bth.postmortem.toml"
    stale.write_text(SUBMITTED_PM.format(run_id=run.id, override="fail"))

    compact(tmp_catalog)

    assert live in parsed_paths, "the live postmortem was not scanned at all"
    assert stale not in parsed_paths, "a postmortem inside a vendored submodule was scanned"


def test_find_postmortem_ignores_nested_git_boundary(tmp_path):
    """postmortem.py's own lookup has the same bug and needs the same fix.

    `find_postmortem` is the read path behind `bth postmortem show` and the
    postmortem_get MCP tool, so a stale nested copy can be returned as the answer.
    """
    from bathos.postmortem import find_postmortem

    ws = tmp_path / "ws"
    (ws / "scripts").mkdir(parents=True)
    rid = "11111111-2222-3333-4444-555555555555"

    wt = ws / ".claude" / "worktrees" / "wt-old"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt-old\n")
    (wt / f"stale.py.{rid}.bth.postmortem.toml").write_text(
        SUBMITTED_PM.format(run_id=rid, override="fail")
    )

    assert find_postmortem(ws, run_id=rid) is None, (
        "a postmortem from a nested checkout was returned as this workspace's answer"
    )
