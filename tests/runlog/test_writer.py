"""Segment writer + mirror (D2, D7), and the log-level halves of AC-5 and
AC-9: a linked worktree's deletion, or an active project-log segment's
deletion, must lose no event because the mirror holds every line."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from bathos.runlog.project_id import assign_project_id, read_project_id
from bathos.runlog.resolve import resolve_log_root
from bathos.runlog.writer import append_event, mirror_dir_for, reset_writers_for_test

from .conftest import make_git_repo, write_bth_toml


def _read_lines(path: Path) -> list[dict]:
    out = []
    for f in sorted(path.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def test_append_writes_project_log_and_mirror_identically(tmp_path: Path):
    repo = make_git_repo(tmp_path / "repo")
    write_bth_toml(repo, slug="repo")
    assign_project_id(repo)
    pid = read_project_id(repo / ".bth.toml")
    res = resolve_log_root(repo)

    outcome = append_event(
        kind="test.recorded",
        entity=["e1"],
        data={"n": 1},
        log_root=res,
        project="repo",
        project_id=pid,
    )
    assert outcome.target == "project"
    assert outcome.mirror_ok
    assert outcome.envelope["v"] == 1
    assert outcome.envelope["kind"] == "test.recorded"
    assert outcome.envelope["entity"] == ["e1"]
    assert outcome.envelope["project_id"] == pid
    assert outcome.envelope["main_root"] == str(repo.resolve())
    assert outcome.envelope["origin"] == "live"

    project_log_dir = repo / ".bth" / "log"
    mirror_dir = mirror_dir_for(pid, "repo")
    project_lines = _read_lines(project_log_dir)
    mirror_lines = _read_lines(mirror_dir)
    assert len(project_lines) == 1
    assert project_lines == mirror_lines  # same bytes, D7
    reset_writers_for_test()


def test_envelope_has_uuidv7_eid_and_sequential_seq(tmp_path: Path):
    repo = make_git_repo(tmp_path / "repo")
    write_bth_toml(repo, slug="repo")
    assign_project_id(repo)
    pid = read_project_id(repo / ".bth.toml")
    res = resolve_log_root(repo)

    envs = []
    for i in range(3):
        outcome = append_event(
            kind="test.recorded",
            entity=[f"e{i}"],
            data={"n": i},
            log_root=res,
            project="repo",
            project_id=pid,
        )
        envs.append(outcome.envelope)

    eids = [e["eid"] for e in envs]
    assert len(set(eids)) == 3
    for eid in eids:
        # UUIDv7: version nibble is 7
        assert eid[14] == "7"
    assert [e["seq"] for e in envs] == [1, 2, 3]
    assert len({e["writer"] for e in envs}) == 1  # same writer/segment
    reset_writers_for_test()


def test_ac5_deleting_linked_worktree_leaves_main_root_log_intact(tmp_path: Path):
    repo = make_git_repo(tmp_path / "repo")
    write_bth_toml(repo, slug="repo")
    assign_project_id(repo)
    pid = read_project_id(repo / ".bth.toml")

    worktree = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-b", "feature", str(worktree)], cwd=repo, check=True)

    res = resolve_log_root(worktree)
    assert res.main_root == repo.resolve()
    assert res.worktree_root == worktree.resolve()

    outcome = append_event(
        kind="test.recorded",
        entity=["wt-run"],
        data={},
        log_root=res,
        project="repo",
        project_id=pid,
    )
    assert outcome.target == "project"
    assert outcome.envelope["worktree_root"] == str(worktree.resolve())
    assert outcome.envelope["main_root"] == str(repo.resolve())
    reset_writers_for_test()

    # Delete the linked worktree entirely (simulating routine worktree cleanup).
    subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=repo, check=True)
    assert not worktree.exists()

    # The event recorded from inside it is unaffected: it lives under the
    # MAIN root's .bth/log/, which the worktree deletion never touched.
    project_lines = _read_lines(repo / ".bth" / "log")
    assert len(project_lines) == 1
    assert project_lines[0]["entity"] == ["wt-run"]


def test_ac9_deleting_active_segment_loses_no_event_because_of_mirror(tmp_path: Path):
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

    project_log_dir = repo / ".bth" / "log"
    mirror_dir = mirror_dir_for(pid, "repo")
    segment_files = list(project_log_dir.glob("*.jsonl"))
    assert len(segment_files) == 1
    mirror_before = _read_lines(mirror_dir)
    assert len(mirror_before) == 3

    reset_writers_for_test()  # close file handles before deleting the segment

    # Simulate the active segment vanishing mid-run.
    segment_files[0].unlink()
    assert not list(project_log_dir.glob("*.jsonl"))

    # Every line is still recoverable from the mirror -- nothing was lost.
    mirror_after = _read_lines(mirror_dir)
    assert mirror_after == mirror_before


def test_unaffiliated_root_writes_to_unaffiliated_log_dir(tmp_path: Path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    res = resolve_log_root(plain)
    assert res.unaffiliated

    outcome = append_event(
        kind="test.recorded",
        entity=["u1"],
        data={},
        log_root=res,
        project=None,
        project_id=None,
    )
    assert outcome.target == "project"
    assert outcome.envelope["project"] is None
    assert outcome.envelope["project_id"] is None

    unaffiliated_dir = Path.home() / ".bth" / "log" / "unaffiliated"
    lines = _read_lines(unaffiliated_dir)
    assert len(lines) == 1
    reset_writers_for_test()


def test_project_write_failure_falls_back(tmp_path: Path, monkeypatch):
    repo = make_git_repo(tmp_path / "repo")
    write_bth_toml(repo, slug="repo")
    assign_project_id(repo)
    pid = read_project_id(repo / ".bth.toml")
    res = resolve_log_root(repo)

    # Make the project log directory unwritable by pre-creating it as a file
    # (so mkdir(parents=True, exist_ok=True) -- and the subsequent open() --
    # both fail with a clean OSError, deterministically, cross-platform).
    bth_dir = repo / ".bth"
    bth_dir.mkdir(parents=True, exist_ok=True)
    (bth_dir / "log").write_text("not a directory")

    outcome = append_event(
        kind="test.recorded",
        entity=["fallback-run"],
        data={},
        log_root=res,
        project="repo",
        project_id=pid,
        allow_fallback=True,
    )
    assert outcome.target == "fallback"
    assert outcome.project_error is not None

    fallback_dir = Path.home() / ".bth" / "log" / "fallback" / "repo"
    lines = _read_lines(fallback_dir)
    assert len(lines) == 1
    assert lines[0]["entity"] == ["fallback-run"]
    reset_writers_for_test()
    shutil.rmtree(bth_dir, ignore_errors=True)


def test_both_targets_failing_reports_none(tmp_path: Path, monkeypatch):
    repo = make_git_repo(tmp_path / "repo")
    write_bth_toml(repo, slug="repo")
    assign_project_id(repo)
    pid = read_project_id(repo / ".bth.toml")
    res = resolve_log_root(repo)

    bth_dir = repo / ".bth"
    bth_dir.mkdir(parents=True, exist_ok=True)
    (bth_dir / "log").write_text("not a directory")

    fake_home = Path(str(tmp_path / "home2"))
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    # Make the fallback root itself unwritable too.
    fallback_parent = fake_home / ".bth" / "log"
    fallback_parent.mkdir(parents=True)
    (fallback_parent / "fallback").write_text("not a directory")

    outcome = append_event(
        kind="test.recorded",
        entity=["nowhere"],
        data={},
        log_root=res,
        project="repo",
        project_id=pid,
        allow_fallback=True,
    )
    assert outcome.target == "none"
    assert not outcome.ok
    assert outcome.project_error is not None
    assert outcome.fallback_error is not None
    reset_writers_for_test()
