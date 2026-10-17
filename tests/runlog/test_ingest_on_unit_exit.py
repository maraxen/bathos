"""Index refresh at the end of every log-mode unit of work (debt #2811).

Before this fix only `bth run` folded new log events into `index.db`. Campaign,
claim, blast-radius and MCP writers left the index stale until some other
command ingested. Each visibility test reads the index with no manual
compact/ingest. The `BTH_NO_INGEST=1` and `SLURM_JOB_ID` tests pin the
no-op guards that must still hold for the new trigger.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from bathos.blast_radius import BlastRadiusRecord, append_ledger_record
from bathos.campaigns import create_campaign
from bathos.query import run_sql
from bathos.runlog.emit import unit_of_work
from bathos.runner import run_script

from .test_ac18_flag_on_reads import enable_log_mode, setup_project


@pytest.fixture
def log_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo, _pid = setup_project(tmp_path)
    catalog = tmp_path / "catalog"
    enable_log_mode(catalog)
    return repo, catalog


@pytest.fixture
def ingest_calls(monkeypatch) -> list:
    """Count calls to `ingest_after_write` while still running the real one.

    Both the runner and the unit-of-work exit look the name up at call time,
    so patching the module attribute observes every trigger.
    """
    import bathos.runlog.ingest as ingest_mod

    calls: list = []
    real = ingest_mod.ingest_after_write

    def counting(catalog_dir=None, **kwargs):
        calls.append(catalog_dir)
        return real(catalog_dir, **kwargs)

    monkeypatch.setattr(ingest_mod, "ingest_after_write", counting)
    return calls


def _rows(sql: str, catalog: Path) -> list[tuple]:
    try:
        return run_sql(sql, catalog_dir=catalog)
    except Exception:
        return []


def _campaign(repo: Path, catalog: Path, name: str = "camp"):
    return create_campaign(
        None,
        name=name,
        project_slug="repo",
        mode="sequential",
        catalog_dir=catalog,
        cwd=repo,
    )


def _blast(repo: Path, catalog: Path, entity_id: str = "r1") -> None:
    append_ledger_record(
        BlastRadiusRecord(
            entity_type="run",
            entity_id=entity_id,
            to_state="affected",
            anchor_kind="commit",
            anchor_value="deadbeef",
        ),
        catalog,
        cwd=repo,
    )


def _script(repo: Path) -> Path:
    script = repo / "scripts" / "analysis" / "hello.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('hi')\n")
    return script


# (a) campaign writer -------------------------------------------------------


def test_campaign_create_visible_in_index_without_manual_ingest(log_repo):
    repo, catalog = log_repo
    campaign = _campaign(repo, catalog)
    rows = _rows("SELECT id FROM campaigns", catalog)
    assert [r[0] for r in rows] == [campaign.id]


def test_campaign_create_with_no_ingest_env_stays_invisible(log_repo, monkeypatch):
    """Negative control: the visibility above must come from the trigger."""
    repo, catalog = log_repo
    monkeypatch.setenv("BTH_NO_INGEST", "1")
    _campaign(repo, catalog)
    assert _rows("SELECT id FROM campaigns", catalog) == []


# (b) blast-radius writer ---------------------------------------------------


def test_blast_radius_record_visible_in_index_without_manual_ingest(log_repo):
    repo, catalog = log_repo
    _blast(repo, catalog, entity_id="r1")
    rows = _rows("SELECT entity_id FROM blast_radius_ledger", catalog)
    assert rows == [("r1",)]


# (c) MCP core path that emits an event ------------------------------------


def test_mcp_postmortem_validate_visible_in_index_without_manual_ingest(tmp_path, monkeypatch):
    from bathos import mcp
    from bathos.runlog.writer import reset_writers_for_test
    from tests.test_postmortem import _setup_postmortem_workspace

    workspace_root, catalog_dir, run = _setup_postmortem_workspace(tmp_path)
    enable_log_mode(catalog_dir)
    monkeypatch.setenv("BTH_LOG_MODE", "1")
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
    pm_path = workspace_root / "scripts" / f"run.py.{run.id}.bth.postmortem.toml"

    res = asyncio.run(
        mcp.postmortem_validate(
            path=str(pm_path),
            workspace_root=str(workspace_root),
            catalog_dir=str(catalog_dir),
        )
    )
    assert res["validation_ok"] is True
    rows = _rows(
        "SELECT entity FROM events WHERE kind = 'run.postmortem_applied'",
        catalog_dir,
    )
    assert len(rows) == 1
    reset_writers_for_test()


# (d) exactly one ingest per command -----------------------------------------


def test_bth_run_triggers_exactly_one_ingest(log_repo, ingest_calls):
    repo, catalog = log_repo
    script = _script(repo)
    code = run_script(
        argv=[sys.executable, str(script)],
        project_slug="repo",
        catalog_dir=catalog,
        output_paths=[],
        tags=[],
        cwd=repo,
    )
    assert code == 0
    assert len(ingest_calls) == 1
    assert len(_rows("SELECT id FROM runs", catalog)) == 1


def test_nested_units_in_one_command_ingest_once(log_repo, ingest_calls):
    repo, catalog = log_repo
    with unit_of_work(catalog):
        _campaign(repo, catalog, name="a")
        _campaign(repo, catalog, name="b")
    assert len(ingest_calls) == 1
    assert len(_rows("SELECT id FROM campaigns", catalog)) == 2


# (e) legacy (flag off) mode never ingests ----------------------------------


def test_legacy_mode_never_calls_ingest(tmp_path, ingest_calls):
    repo, _pid = setup_project(tmp_path)
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    script = _script(repo)
    code = run_script(
        argv=[sys.executable, str(script)],
        project_slug="repo",
        catalog_dir=catalog,
        output_paths=[],
        tags=[],
        cwd=repo,
    )
    assert code == 0
    assert ingest_calls == []
    assert not (catalog / "index.db").exists()


# Guards: the trigger must stay a no-op where the runner's guards apply -----


def test_slurm_job_unit_does_not_build_index(log_repo, monkeypatch):
    repo, catalog = log_repo
    monkeypatch.setenv("SLURM_JOB_ID", "4242")
    _blast(repo, catalog)
    assert not (catalog / "index.db").exists()


def test_bth_no_ingest_unit_does_not_build_index(log_repo, monkeypatch):
    repo, catalog = log_repo
    monkeypatch.setenv("BTH_NO_INGEST", "1")
    _blast(repo, catalog)
    assert not (catalog / "index.db").exists()


# Exit semantics: never mask the body, never raise out of the unit ----------


def test_body_exception_propagates_and_ingest_still_runs(log_repo, ingest_calls):
    repo, catalog = log_repo
    with pytest.raises(ValueError, match="boom"), unit_of_work(catalog):
        _campaign(repo, catalog)
        raise ValueError("boom")
    assert len(ingest_calls) == 1
    assert len(_rows("SELECT id FROM campaigns", catalog)) == 1


def test_ingest_failure_never_escapes_unit(log_repo, monkeypatch):
    import bathos.runlog.ingest as ingest_mod

    def boom(*_args, **_kwargs):
        raise RuntimeError("ingest exploded")

    monkeypatch.setattr(ingest_mod, "ingest_after_write", boom)
    repo, catalog = log_repo
    campaign = _campaign(repo, catalog)
    assert campaign.id
