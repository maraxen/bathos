"""Migration step 0's foreign-catalog refusal (debt #1998, spec v39):
`bth migrate --to-log` refuses -- unconditionally, before the dry-run branch
-- while any registered root's `.bth.toml` explicitly sets `[project]
catalog_dir` to somewhere other than the catalog being migrated.

Uses the same isolation as `test_migrate_to_log.py` (`isolated_home`,
autouse from `tests/runlog/conftest.py`) -- HOME lives under `tmp_path` for
every test here, never the real `~/.bth/`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bathos.runlog.migrate import explicit_catalog_dir, roots_with_foreign_catalog
from bathos.runlog.project_id import assign_project_id, register_main_root

from .conftest import make_git_repo
from .test_migrate_to_log import _commit_all


@pytest.fixture(autouse=True)
def _squeue_available_and_empty(monkeypatch):
    """Same rationale as `test_migrate_to_log.py`'s own fixture of the same
    name: this sandbox has no real `squeue`, and `my_squeue_job_ids()` fails
    CLOSED on any query failure."""
    monkeypatch.setattr("bathos.runlog.migrate.my_squeue_job_ids", lambda: [])


def _write_bth_toml(
    root: Path,
    slug: str,
    *,
    catalog_dir: Path | None = None,
    nested_catalog_dir: Path | None = None,
) -> None:
    lines = ["[project]", f'slug = "{slug}"', f'root = "{root}"']
    if catalog_dir is not None:
        lines.append(f'catalog_dir = "{catalog_dir}"')
    if nested_catalog_dir is not None:
        lines.append("")
        lines.append("[project.catalog]")
        lines.append(f'catalog_dir = "{nested_catalog_dir}"')
    (root / ".bth.toml").write_text("\n".join(lines) + "\n")


def _registered_root(
    tmp_path: Path,
    name: str,
    *,
    catalog_dir: Path | None = None,
    nested_catalog_dir: Path | None = None,
) -> Path:
    root = tmp_path / name
    make_git_repo(root)
    _write_bth_toml(root, name, catalog_dir=catalog_dir, nested_catalog_dir=nested_catalog_dir)
    assign_project_id(root)
    _commit_all(root, "assign id")
    register_main_root(root)
    return root


# --------------------------------------------------------------------------
# explicit_catalog_dir / roots_with_foreign_catalog (unit level)
# --------------------------------------------------------------------------


def test_explicit_catalog_dir_none_when_key_absent(tmp_path: Path):
    root = _registered_root(tmp_path, "proj")
    assert explicit_catalog_dir(root) is None


def test_explicit_catalog_dir_reads_flat_key(tmp_path: Path):
    foreign = tmp_path / "foreign-cat"
    root = _registered_root(tmp_path, "proj", catalog_dir=foreign)
    assert explicit_catalog_dir(root) == foreign


def test_explicit_catalog_dir_ignores_nested_table_form(tmp_path: Path):
    """`[project.catalog] catalog_dir` is not read by `config.py`'s
    `load_project_config`, and must not be read here either."""
    foreign = tmp_path / "foreign-cat"
    root = _registered_root(tmp_path, "proj", nested_catalog_dir=foreign)
    assert explicit_catalog_dir(root) is None


# --------------------------------------------------------------------------
# migrate_to_log's step-0 refusal
# --------------------------------------------------------------------------


def test_foreign_catalog_dir_refuses_to_log(tmp_path: Path):
    from bathos.runlog.migrate import migrate_to_log

    cd = tmp_path / "cat"
    foreign = tmp_path / "foreign-cat"
    root = _registered_root(tmp_path, "proj", catalog_dir=foreign)

    result = migrate_to_log(cd)

    assert result.status == "foreign_catalogs"
    assert result.foreign_catalogs == [{"root": str(root), "catalog_dir": str(foreign)}]
    assert not (cd / "cutover.json").exists()


def test_foreign_catalog_dir_refuses_on_dry_run_too(tmp_path: Path):
    """Spec: checked before the dry-run branch, so `--dry-run` reports it too."""
    from bathos.runlog.migrate import migrate_to_log

    cd = tmp_path / "cat"
    foreign = tmp_path / "foreign-cat"
    root = _registered_root(tmp_path, "proj", catalog_dir=foreign)

    result = migrate_to_log(cd, dry_run=True)

    assert result.status == "foreign_catalogs"
    assert result.foreign_catalogs == [{"root": str(root), "catalog_dir": str(foreign)}]


def test_foreign_catalog_dir_refuses_even_with_force(tmp_path: Path):
    """Unconditional -- unlike the squeue conflict, there is no `--force`
    override for a foreign catalog_dir."""
    from bathos.runlog.migrate import migrate_to_log

    cd = tmp_path / "cat"
    foreign = tmp_path / "foreign-cat"
    _registered_root(tmp_path, "proj", catalog_dir=foreign)

    result = migrate_to_log(cd, force=True)

    assert result.status == "foreign_catalogs"


# --------------------------------------------------------------------------
# Negative controls: must NOT be refused
# --------------------------------------------------------------------------


def test_no_catalog_dir_key_is_not_refused(tmp_path: Path):
    from bathos.runlog.migrate import migrate_to_log

    cd = tmp_path / "cat"
    _registered_root(tmp_path, "proj")

    assert roots_with_foreign_catalog(cd) == []
    result = migrate_to_log(cd)
    assert result.status != "foreign_catalogs"


def test_catalog_dir_equal_to_dest_is_not_refused(tmp_path: Path):
    from bathos.runlog.migrate import migrate_to_log

    cd = tmp_path / "cat"
    _registered_root(tmp_path, "proj", catalog_dir=cd)

    assert roots_with_foreign_catalog(cd) == []
    result = migrate_to_log(cd)
    assert result.status != "foreign_catalogs"


def test_nested_project_catalog_table_form_is_not_refused(tmp_path: Path):
    """The nested `[project.catalog] catalog_dir` form is never read by
    `config.py`, so a project using only that form is -- as far as bathos is
    concerned -- on the default catalog, and must not trigger the refusal."""
    from bathos.runlog.migrate import migrate_to_log

    cd = tmp_path / "cat"
    foreign = tmp_path / "foreign-cat"
    _registered_root(tmp_path, "proj", nested_catalog_dir=foreign)

    assert roots_with_foreign_catalog(cd) == []
    result = migrate_to_log(cd)
    assert result.status != "foreign_catalogs"
