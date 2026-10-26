"""Output tiering step 10: the lint for resume/output directories outside every declared root
(spec 261023_output-tiering.md section 9 row 10).

Built on the real log-mode catalog of `test_output_tiers_adopt.World`, so the lint reads folded `run.started`
events and `adopted_outputs` the way it does on a real index. Each test names the broken implementation it guards
against.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from bathos.linter import check_undeclared_resume_dirs
from tests.test_output_tiers_adopt import RID, T0, World

RID2 = "22222222-bbbb-4bbb-8bbb-222222222222"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    w = World(tmp_path)
    monkeypatch.chdir(w.elsewhere)
    return w


def _started(
    w: World, argv: list[str], *, roots: list[str] | None = None, run_id: str = RID
) -> None:
    data = {
        "id": run_id,
        "project_slug": "repo",
        "command": " ".join(argv),
        "argv": argv,
        "git_hash": "aaa",
        "git_branch": "main",
        "git_dirty": False,
        "timestamp": T0,
        "agent_mode": "manual",
        "sidecar": {"outputs": {"roots": roots}} if roots is not None else {"outputs": None},
    }
    if roots:
        data["outputs_declared"] = True
    w.add("run.started", data, T0, f"e-start-{run_id[9:13]}", run_id)


def _flagged(w: World) -> list[str]:
    return [
        i.detail
        for i in check_undeclared_resume_dirs(w.catalog)
        if i.issue == "undeclared_resume_dir"
    ]


def test_an_undeclared_resume_dir_is_flagged_even_though_it_does_not_exist(world: World):
    """Guards a lint that stats the directory: the record alone must be enough (the dir was never created)."""
    _started(world, ["python", "x.py", "--resume-dir", "out/cells"])
    world.finished()
    world.ingest()

    assert not (world.root / "out" / "cells").exists()
    flagged = _flagged(world)
    assert len(flagged) == 1
    assert "--resume-dir out/cells" in flagged[0] and RID[:8] in flagged[0]


def test_a_dir_under_a_declared_root_is_not_flagged_in_either_flag_form(world: World):
    """Guards a lint that ignores the sidecar's [outputs] roots, or parses only `--flag value`."""
    _started(
        world,
        ["python", "x.py", "--resume-dir", "out/cells", "--out-dir=out/tables"],
        roots=["out"],
    )
    world.finished()
    world.ingest()

    assert _flagged(world) == []


def test_a_sibling_with_a_shared_prefix_is_not_under_the_root(world: World):
    """Guards a plain string-prefix test: `outputs2/cells` is not under the root `outputs`."""
    _started(world, ["python", "x.py", "--out-dir", "outputs2/cells"], roots=["outputs"])
    world.finished()
    world.ingest()

    assert len(_flagged(world)) == 1


def test_adopting_the_directory_clears_the_warning(world: World):
    """Guards a lint that only reads declared roots: an adopted pre-standard dir has reference hashes."""
    world.write("out/cells/c1.npy", b"cell-1")
    _started(world, ["python", "x.py", "--resume-dir", "out/cells"])
    world.finished()
    world.ingest()
    assert len(_flagged(world)) == 1

    world.adopt("out/cells")
    assert _flagged(world) == []


def test_an_absolute_value_is_read_against_the_runs_worktree_root(world: World):
    """Guards comparing an absolute argv value with a relative declared root as strings."""
    inside = str(world.root / "out" / "cells")
    outside = str(world.elsewhere / "cells")
    _started(world, ["python", "x.py", "--resume-dir", inside], roots=["out"])
    world.finished()
    _started(world, ["python", "y.py", "--resume-dir", outside], roots=["out"], run_id=RID2)
    world.finished(RID2)
    world.ingest()

    flagged = _flagged(world)
    assert len(flagged) == 1 and RID2[:8] in flagged[0]


def test_project_slug_limits_the_check_to_that_projects_runs(world: World):
    """Guards a catalog-wide scan: on a shared catalog `bth lint` in one project must not list another's runs."""
    _started(world, ["python", "x.py", "--resume-dir", "out/cells"])
    world.finished()
    world.ingest()

    assert len(check_undeclared_resume_dirs(world.catalog, project_slug="repo")) == 1
    assert check_undeclared_resume_dirs(world.catalog, project_slug="other") == []


def test_a_run_without_the_flags_is_never_flagged(world: World):
    """Negative control: `--resume-dirs` and `--out` are not the flags this lint knows."""
    _started(world, ["python", "x.py", "--out", "out/a.json", "--resume-dirs", "out/cells"])
    world.finished()
    world.ingest()

    assert _flagged(world) == []


def test_a_legacy_warm_catalog_without_events_flags_and_does_not_crash(tmp_path: Path):
    """Guards a query that assumes the `events` table and the `adopted_outputs` column exist."""
    from bathos.catalog import init_catalog, write_run
    from bathos.compact import compact
    from bathos.schema import Run

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    init_catalog(catalog_dir)
    write_run(
        Run(
            project_slug="test",
            command="python train.py --resume-dir cells",
            argv=["python", "train.py", "--resume-dir", "cells"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            timestamp=datetime.now(UTC),
            status="completed",
            exit_code=0,
        ),
        catalog_dir,
    )
    compact(catalog_dir)

    issues = check_undeclared_resume_dirs(catalog_dir)
    assert [i.issue for i in issues] == ["undeclared_resume_dir"]
