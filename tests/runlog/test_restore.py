"""`bth log restore` -- AC-27 and the log half of AC-14."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from bathos.runlog.project_id import assign_project_id, read_project_id, register_main_root
from bathos.runlog.resolve import resolve_log_root
from bathos.runlog.restore import restore_from_mirror
from bathos.runlog.writer import append_event, reset_writers_for_test

from .conftest import make_git_repo, write_bth_toml


def _read_lines(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for f in sorted(path.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def test_ac14_restore_after_simulated_git_clean(tmp_path: Path):
    repo = make_git_repo(tmp_path / "repo")
    write_bth_toml(repo, slug="repo")
    assign_project_id(repo)
    pid = read_project_id(repo / ".bth.toml")
    res = resolve_log_root(repo)

    for i in range(3):
        append_event(
            kind="test.recorded",
            entity=[f"e{i}"],
            data={"n": i},
            log_root=res,
            project="repo",
            project_id=pid,
        )
    reset_writers_for_test()
    mirror_lines_before = _read_lines(Path.home() / ".bth" / "log-mirror" / pid)
    assert len(mirror_lines_before) == 3

    # Simulate `git clean -fdX`: .bth/log/ (gitignored) is wiped, but the
    # tracked .bth.toml and .gitignore survive.
    shutil.rmtree(repo / ".bth" / "log")
    assert not (repo / ".bth" / "log").exists()

    report = restore_from_mirror(repo, project_id=pid)
    assert report.restored == 3
    assert report.already_present == 0
    assert report.skipped_other_live_root == 0

    project_lines_after = _read_lines(repo / ".bth" / "log")
    assert {line["eid"] for line in project_lines_after} == {
        line["eid"] for line in mirror_lines_before
    }

    # Idempotent: running it again finds everything already present.
    report2 = restore_from_mirror(repo, project_id=pid)
    assert report2.restored == 0
    assert report2.already_present == 3


def test_ac27_fork_never_receives_the_other_roots_events(tmp_path: Path):
    shared_id = None

    root_a = make_git_repo(tmp_path / "a")
    write_bth_toml(root_a, slug="a")
    assign_project_id(root_a)
    shared_id = read_project_id(root_a / ".bth.toml")

    root_b = make_git_repo(tmp_path / "b")
    write_bth_toml(root_b, slug="b", project_id=shared_id)  # forked: same id

    register_main_root(root_a)
    register_main_root(root_b)

    res_a = resolve_log_root(root_a)
    res_b = resolve_log_root(root_b)

    append_event(
        kind="test.recorded",
        entity=["from-a"],
        data={},
        log_root=res_a,
        project="a",
        project_id=shared_id,
    )
    append_event(
        kind="test.recorded",
        entity=["from-b"],
        data={},
        log_root=res_b,
        project="b",
        project_id=shared_id,
    )
    reset_writers_for_test()

    # Both roots are alive and registered. Restoring in A must not pull in B's
    # own local event (B is a live root sharing the id).
    report_a = restore_from_mirror(root_a, project_id=shared_id)
    lines_a = _read_lines(root_a / ".bth" / "log")
    entities_a = {tuple(line["entity"]) for line in lines_a}
    assert ("from-a",) in entities_a
    assert ("from-b",) not in entities_a
    assert report_a.skipped_other_live_root >= 1

    report_b = restore_from_mirror(root_b, project_id=shared_id)
    lines_b = _read_lines(root_b / ".bth" / "log")
    entities_b = {tuple(line["entity"]) for line in lines_b}
    assert ("from-b",) in entities_b
    assert ("from-a",) not in entities_b
    assert report_b.skipped_other_live_root >= 1


def test_ac27_moved_project_recovers_events_written_at_old_path(tmp_path: Path):
    old_root = make_git_repo(tmp_path / "old")
    write_bth_toml(old_root, slug="moved")
    assign_project_id(old_root)
    pid = read_project_id(old_root / ".bth.toml")
    register_main_root(old_root)

    res_old = resolve_log_root(old_root)
    append_event(
        kind="test.recorded",
        entity=["old-run"],
        data={},
        log_root=res_old,
        project="moved",
        project_id=pid,
    )
    reset_writers_for_test()

    # "Move" the project: copy only the TRACKED content (.bth/log/ is
    # gitignored, so a real move via a fresh clone/rsync never brings it
    # along) to a new path, then delete the old root entirely. The old root
    # is NOT live any more (path gone), so its mirrored events must come back
    # at the new path.
    new_root = tmp_path / "new"
    shutil.copytree(old_root, new_root, ignore=shutil.ignore_patterns("log"))
    assert not (new_root / ".bth" / "log").exists()
    shutil.rmtree(old_root)
    assert not old_root.exists()

    report = restore_from_mirror(new_root, project_id=pid)
    assert report.restored == 1
    assert report.skipped_other_live_root == 0

    lines = _read_lines(new_root / ".bth" / "log")
    assert {tuple(line["entity"]) for line in lines} == {("old-run",)}
