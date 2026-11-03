"""Project binding for non-git checkouts (praxia debt #3201).

A project copied to a host with no git (an Engaging worktree of a project whose
`.bth.toml` records a laptop path) must bind to its project, not `unaffiliated`;
and a run that carries a slug must fail loudly rather than fall back silently.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bathos.query import run_sql
from bathos.runlog.project_id import assign_project_id, read_project_id
from bathos.runlog.resolve import (
    UnaffiliatedProjectError,
    require_affiliated,
    resolve_log_root,
)
from bathos.runner import run_script

from .test_ac18_flag_on_reads import enable_log_mode


def _nongit_project(tmp_path: Path, slug: str = "demo") -> tuple[Path, Path]:
    """A non-git copy whose recorded [project] root does not exist here."""
    work = tmp_path / "engaging" / slug
    work.mkdir(parents=True)
    stale = tmp_path / "laptop" / "projects" / slug  # never created
    (work / ".bth.toml").write_text(f'[project]\nslug = "{slug}"\nroot = "{stale}"\n')
    assign_project_id(work)
    return work, stale


def _script(root: Path) -> Path:
    script = root / "scripts" / "analysis" / "hello.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('hi')\n")
    return script


def test_nongit_stale_recorded_root_binds_to_project(tmp_path: Path):
    work, stale = _nongit_project(tmp_path)
    assert not stale.exists()
    res = resolve_log_root(work)
    assert not res.unaffiliated
    assert res.main_root == work.resolve()
    assert require_affiliated("demo", work).main_root == work.resolve()


def test_nongit_run_records_project_id_and_is_queryable(tmp_path: Path, monkeypatch):
    work, _stale = _nongit_project(tmp_path)
    catalog = tmp_path / "catalog"
    enable_log_mode(catalog)
    monkeypatch.setenv("BTH_LOG_MODE", "1")
    pid = read_project_id(work / ".bth.toml")
    assert pid

    code = run_script(
        argv=[sys.executable, str(_script(work))],
        project_slug="demo",
        catalog_dir=catalog,
        output_paths=[],
        tags=[],
        cwd=work,
    )
    assert code == 0
    runs = run_sql("SELECT project_slug FROM runs WHERE project_slug = 'demo'", catalog_dir=catalog)
    assert len(runs) == 1
    # project_id is carried on the event envelope (index `events` table)
    ev = run_sql(
        "SELECT project, project_id FROM events WHERE kind = 'run.started'", catalog_dir=catalog
    )
    assert ev == [("demo", pid)]
    # filed under the project's own log, not ~/.bth/log/unaffiliated/
    assert (work / ".bth" / "log").is_dir()


def test_slug_without_bth_toml_fails_loudly(tmp_path: Path, monkeypatch, capsys):
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setenv("BTH_PROJECT_SLUG", "demo")
    catalog = tmp_path / "catalog"
    enable_log_mode(catalog)
    monkeypatch.setenv("BTH_LOG_MODE", "1")

    with pytest.raises(UnaffiliatedProjectError) as exc:
        require_affiliated("demo", bare)
    msg = str(exc.value)
    assert "'demo'" in msg
    assert str(bare.resolve()) in msg
    assert "recorded [project] root=None" in msg

    code = run_script(
        argv=[sys.executable, str(_script(bare))],
        project_slug="demo",
        catalog_dir=catalog,
        output_paths=[],
        tags=[],
        cwd=bare,
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "'demo'" in err and str(bare.resolve()) in err and "recorded [project] root" in err
    # nothing was filed under the unaffiliated log
    assert not (Path.home() / ".bth" / "log" / "unaffiliated").exists()


def test_unaffiliated_without_slug_unchanged(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    res = resolve_log_root(plain)
    assert res.unaffiliated
    assert res.main_root == plain.resolve()


def test_error_names_stale_recorded_root_when_toml_is_unreachable(tmp_path: Path, monkeypatch):
    # BTH_WORKSPACE_ROOT pointing at a directory without .bth.toml, no git: the
    # slug-bearing run must fail and the message must carry the recorded root.
    work, stale = _nongit_project(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(empty))
    with pytest.raises(UnaffiliatedProjectError) as exc:
        require_affiliated("demo", work)
    assert str(stale) in str(exc.value)
    assert str(work.resolve()) in str(exc.value)
