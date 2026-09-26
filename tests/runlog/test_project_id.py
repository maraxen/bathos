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
        '[project]\nslug = "proj"\nroot = "%s"\n\n[slurm]\npartition = "pi_so3"\n' % root
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
