"""D7 project-id assignment and Discovery root registration."""

from __future__ import annotations

from pathlib import Path

import pytest

from bathos.runlog.project_id import (
    ProjectIdMissingError,
    assign_project_id,
    list_registered_roots,
    prune_vanished_roots,
    read_project_id,
    register_main_root,
    require_project_id,
)

from .conftest import write_bth_toml


def test_assign_project_id_mints_when_absent(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    write_bth_toml(root, slug="proj")
    result = assign_project_id(root)
    assert result.minted is True
    assert read_project_id(root / ".bth.toml") == result.project_id


def test_assign_project_id_idempotent(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    write_bth_toml(root, slug="proj")
    first = assign_project_id(root)
    second = assign_project_id(root)
    assert second.minted is False
    assert second.project_id == first.project_id


def test_assign_project_id_preserves_other_toml_content(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".bth.toml").write_text(
        f'[project]\nslug = "proj"\nroot = "{root}"\n\n[slurm]\npartition = "pi_so3"\n'
    )
    assign_project_id(root)
    text = (root / ".bth.toml").read_text()
    assert 'partition = "pi_so3"' in text
    assert 'slug = "proj"' in text


def test_assign_project_id_missing_toml_raises(tmp_path: Path):
    root = tmp_path / "no-toml"
    root.mkdir()
    with pytest.raises(FileNotFoundError):
        assign_project_id(root)


def test_require_project_id_raises_structured_error_when_absent(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    write_bth_toml(root, slug="proj")  # no id
    with pytest.raises(ProjectIdMissingError):
        require_project_id(root)


def test_require_project_id_returns_id_when_present(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    write_bth_toml(root, slug="proj")
    result = assign_project_id(root)
    assert require_project_id(root) == result.project_id


def test_register_main_root_idempotent(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    write_bth_toml(root, slug="proj")
    assign_project_id(root)
    assert register_main_root(root) is True
    assert register_main_root(root) is False
    assert root.resolve() in list_registered_roots()


def test_prune_vanished_roots(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    write_bth_toml(root, slug="proj")
    register_main_root(root)
    import shutil

    shutil.rmtree(root)
    pruned = prune_vanished_roots()
    assert root.resolve() in pruned
    assert list_registered_roots() == []


def test_register_main_root_fills_in_id_assigned_later(tmp_path: Path):
    """A root registered before it had an id picks the id up on re-registration,
    even within the same process (the registration cache must not short-circuit it)."""
    import tomllib

    from bathos.runlog.project_id import projects_registry

    root = tmp_path / "proj"
    root.mkdir()
    write_bth_toml(root, slug="proj")  # no id yet
    assert register_main_root(root) is True
    result = assign_project_id(root)
    assert register_main_root(root) is False
    entries = tomllib.loads(projects_registry().read_text())["roots"]
    assert [e.get("project_id") for e in entries if e["root"] == str(root.resolve())] == [
        result.project_id
    ]


def test_init_assign_id_registers_root(tmp_path: Path):
    """`bth init --assign-id` must register the root: before cut-over nothing else
    does, and migrate --to-log routes legacy runs only to registered roots."""
    from bathos.mcp import init_tool

    root = tmp_path / "proj"
    root.mkdir()
    write_bth_toml(root, slug="proj")
    first = init_tool(project_root=str(root), assign_id=True)
    assert first["minted"] is True
    assert first["registered"] is True
    assert root.resolve() in list_registered_roots()

    again = init_tool(project_root=str(root), assign_id=True)
    assert again["minted"] is False
    assert again["registered"] is False
    assert again["project_id"] == first["project_id"]
    assert list_registered_roots().count(root.resolve()) == 1


def test_init_assign_id_negative_control_plain_assign_does_not_register(tmp_path: Path):
    """Negative control: the bare id retrofit (no init wiring) registers nothing, so
    test_init_assign_id_registers_root is not passing because of some other path."""
    root = tmp_path / "proj"
    root.mkdir()
    write_bth_toml(root, slug="proj")
    assign_project_id(root)
    assert list_registered_roots() == []


def test_register_main_root_updates_stale_project_id(tmp_path: Path, caplog):
    """#3508: re-registering after `.bth.toml`'s `[project] id` changes updates
    `~/.bth/projects.toml` even within the same process (in-process cache must not
    serve the stale id)."""
    import logging
    import tomllib

    from bathos.runlog.project_id import list_registered_entries, projects_registry

    root = tmp_path / "proj"
    root.mkdir()
    write_bth_toml(root, slug="proj", project_id="11111111-1111-1111-1111-111111111111")
    assert register_main_root(root) is True

    # Simulate `.bth.toml` id changing (e.g. `assign_project_id(root, force=True)`).
    second = assign_project_id(root, force=True)
    assert second.minted is True
    assert second.project_id != "11111111-1111-1111-1111-111111111111"

    with caplog.at_level(logging.WARNING, logger="bathos.runlog.project_id"):
        assert register_main_root(root) is False

    assert any("stale project_id" in r.getMessage().lower() for r in caplog.records)
    entries = tomllib.loads(projects_registry().read_text())["roots"]
    assert [e.get("project_id") for e in entries if e["root"] == str(root.resolve())] == [
        second.project_id
    ]
    assert list_registered_entries() == [(root.resolve(), second.project_id)]


def test_register_main_root_refreshes_externally_stale_registry(tmp_path: Path):
    """#3508: if `projects.toml` was overwritten externally with a stale `project_id`,
    a subsequent `register_main_root` in the same process detects the mtime change
    and restores `.bth.toml`'s `project_id`."""
    from bathos.runlog.project_id import list_registered_entries, projects_registry

    root = tmp_path / "proj"
    root.mkdir()
    real_id = "22222222-2222-2222-2222-222222222222"
    write_bth_toml(root, slug="proj", project_id=real_id)
    assert register_main_root(root) is True

    reg_path = projects_registry()
    reg_path.write_text(f'[[roots]]\nroot = "{root.resolve()}"\nproject_id = "stale-id"\n')
    assert register_main_root(root) is False
    assert list_registered_entries() == [(root.resolve(), real_id)]


def test_register_main_root_preserves_registry_id_and_warns_when_toml_loses_id(
    tmp_path: Path, caplog
):
    """#3508 negative control: if `.bth.toml` has no `[project] id`, `register_main_root`
    warns and leaves the registry entry intact so `verify_runlog` can flag it."""
    import logging

    from bathos.runlog.project_id import list_registered_entries

    root = tmp_path / "proj"
    root.mkdir()
    old_id = "33333333-3333-3333-3333-333333333333"
    write_bth_toml(root, slug="proj", project_id=old_id)
    assert register_main_root(root) is True

    write_bth_toml(root, slug="proj", project_id=None)
    with caplog.at_level(logging.WARNING, logger="bathos.runlog.project_id"):
        assert register_main_root(root) is False

    assert any("has no [project] id" in r.getMessage() for r in caplog.records)
    assert list_registered_entries() == [(root.resolve(), old_id)]
