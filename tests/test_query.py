import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import dataclasses
import pytest
from bathos.catalog import write_run, init_catalog
from bathos.query import list_runs, get_run, find_runs, run_sql
from bathos.schema import Run


@pytest.fixture
def populated_catalog(tmp_catalog: Path) -> Path:
    init_catalog(tmp_catalog)
    base = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    for i, (proj, status) in enumerate([
        ("prolix", "completed"),
        ("prolix", "failed"),
        ("espaloma", "completed"),
    ]):
        r = Run(
            project_slug=proj,
            command=f"python run_{i}.py",
            argv=["python", f"run_{i}.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            timestamp=base + timedelta(hours=i),
            status=status,
            exit_code=0 if status == "completed" else 1,
        )
        write_run(r, tmp_catalog)
    return tmp_catalog


def test_list_runs_returns_all(populated_catalog: Path):
    runs = list_runs(populated_catalog)
    assert len(runs) == 3


def test_list_runs_filter_by_project(populated_catalog: Path):
    runs = list_runs(populated_catalog, project="prolix")
    assert len(runs) == 2
    assert all(r.project_slug == "prolix" for r in runs)


def test_list_runs_filter_by_status(populated_catalog: Path):
    runs = list_runs(populated_catalog, status="failed")
    assert len(runs) == 1
    assert runs[0].status == "failed"


def test_get_run_returns_correct(populated_catalog: Path):
    all_runs = list_runs(populated_catalog)
    target = all_runs[0]
    found = get_run(target.id, populated_catalog)
    assert found is not None
    assert found.id == target.id


def test_get_run_returns_none_for_unknown(populated_catalog: Path):
    assert get_run("nonexistent-id", populated_catalog) is None


def test_find_runs_since(populated_catalog: Path):
    since = datetime(2026, 5, 10, 13, 30, 0, tzinfo=timezone.utc)
    runs = find_runs(populated_catalog, since=since)
    assert len(runs) == 1
    assert runs[0].project_slug == "espaloma"


def test_run_sql_returns_rows(populated_catalog: Path):
    glob = str(populated_catalog / "runs" / "run_*.parquet")
    rows = run_sql(f"SELECT project_slug, count(*) as n FROM read_parquet('{glob}') GROUP BY 1 ORDER BY 1")
    assert len(rows) == 2
    projects = {row[0] for row in rows}
    assert "prolix" in projects
    assert "espaloma" in projects
