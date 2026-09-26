"""The shared emission layer (`bathos.runlog.emit`): unit-of-work re-entrancy,
the emit_or_legacy flag branch, and D6's run.finished exit-code semantics."""

from __future__ import annotations

from pathlib import Path

from bathos.runlog.emit import (
    apply_run_finished_exit_semantics,
    current_mode,
    emit_or_legacy,
    in_unit_of_work,
    unit_of_work,
)
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.writer import AppendOutcome

from .conftest import make_git_repo, write_bth_toml


def test_mode_off_by_default(tmp_path: Path):
    assert not in_unit_of_work()
    with unit_of_work(tmp_path / "catalog") as mode:
        assert mode is False
        assert current_mode() is False
        assert in_unit_of_work()
    assert not in_unit_of_work()


def test_mode_on_once_marker_present(tmp_path: Path):
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir(parents=True)
    cutover_marker_path(catalog_dir).write_text("{}")
    with unit_of_work(catalog_dir) as mode:
        assert mode is True
        assert current_mode() is True


def test_current_mode_raises_outside_unit_of_work():
    import pytest

    with pytest.raises(RuntimeError):
        current_mode()


def test_nested_unit_of_work_is_a_noop_sharing_outer_mode(tmp_path: Path):
    """AC-25's 'mode is not re-read mid-unit': a nested entry (e.g. the MCP
    traced_tool wrapper entering it, then calling into a CLI-registry core
    function that also enters it) must see the SAME resolution as the outer
    one, even if the on-disk marker changes in between -- proving the inner
    call did not re-read it."""
    catalog_dir = tmp_path / "catalog"
    with unit_of_work(catalog_dir) as outer_mode:
        assert outer_mode is False
        # Flip what a fresh read would now resolve to.
        catalog_dir.mkdir(parents=True, exist_ok=True)
        cutover_marker_path(catalog_dir).write_text("{}")
        with unit_of_work(catalog_dir) as inner_mode:
            # Still False: the inner entry did not re-read the (now-True) marker.
            assert inner_mode is False
            assert current_mode() is False
    assert not in_unit_of_work()
    # Outside any unit of work, a fresh read now sees the flipped marker.
    with unit_of_work(catalog_dir) as fresh_mode:
        assert fresh_mode is True


def test_unit_of_work_resets_after_exception(tmp_path: Path):
    catalog_dir = tmp_path / "catalog"
    try:
        with unit_of_work(catalog_dir):
            raise ValueError("boom")
    except ValueError:
        pass
    assert not in_unit_of_work()


def test_emit_or_legacy_flag_off_calls_legacy_only(tmp_path: Path):
    repo = make_git_repo(tmp_path / "repo")
    write_bth_toml(repo, slug="repo")
    calls = []

    with unit_of_work(tmp_path / "catalog"):
        result = emit_or_legacy(
            kind="test.recorded",
            entity=["e1"],
            data={},
            legacy_write=lambda: calls.append("legacy"),
            cwd=repo,
        )

    assert result is None
    assert calls == ["legacy"]
    # No project log was created.
    assert not (repo / ".bth" / "log").exists()


def test_emit_or_legacy_flag_on_emits_event_and_skips_legacy(tmp_path: Path):
    from bathos.runlog.project_id import assign_project_id
    from bathos.runlog.writer import reset_writers_for_test

    repo = make_git_repo(tmp_path / "repo")
    write_bth_toml(repo, slug="repo")
    assign_project_id(repo)
    calls = []

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir(parents=True)
    cutover_marker_path(catalog_dir).write_text("{}")

    with unit_of_work(catalog_dir):
        result = emit_or_legacy(
            kind="test.recorded",
            entity=["e1"],
            data={"n": 1},
            legacy_write=lambda: calls.append("legacy"),
            cwd=repo,
        )

    assert calls == []  # legacy never called
    assert result is not None
    assert result.target == "project"
    assert result.envelope["kind"] == "test.recorded"
    assert (repo / ".bth" / "log").exists()
    reset_writers_for_test()


def test_apply_run_finished_exit_semantics_project_ok_preserves_exit_code():
    outcome = AppendOutcome(target="project", envelope={}, mirror_ok=True)
    assert apply_run_finished_exit_semantics(0, outcome) == 0
    assert apply_run_finished_exit_semantics(7, outcome) == 7


def test_apply_run_finished_exit_semantics_fallback_preserves_exit_code():
    outcome = AppendOutcome(target="fallback", envelope={}, mirror_ok=True)
    assert apply_run_finished_exit_semantics(0, outcome) == 0
    assert apply_run_finished_exit_semantics(3, outcome) == 3


def test_apply_run_finished_exit_semantics_none_forces_nonzero():
    outcome = AppendOutcome(target="none", envelope=None, mirror_ok=False)
    assert apply_run_finished_exit_semantics(0, outcome) == 1
    assert apply_run_finished_exit_semantics(5, outcome) == 5
