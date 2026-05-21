from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bathos.catalog import init_catalog, write_run
from bathos.query import _resolve_backend, find_runs, get_run, list_runs, run_sql
from bathos.schema import Run


@pytest.fixture
def populated_catalog(tmp_catalog: Path) -> Path:
    init_catalog(tmp_catalog)
    base = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    for i, (proj, status) in enumerate(
        [
            ("prolix", "completed"),
            ("prolix", "failed"),
            ("espaloma", "completed"),
        ]
    ):
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
    since = datetime(2026, 5, 10, 13, 30, 0, tzinfo=UTC)
    runs = find_runs(populated_catalog, since=since)
    assert len(runs) == 1
    assert runs[0].project_slug == "espaloma"


def test_run_sql_returns_rows(populated_catalog: Path):
    glob = str(populated_catalog / "runs" / "**" / "run_*.parquet")
    rows = run_sql(
        f"SELECT project_slug, count(*) as n FROM read_parquet('{glob}') GROUP BY 1 ORDER BY 1"
    )
    assert len(rows) == 2
    projects = {row[0] for row in rows}
    assert "prolix" in projects
    assert "espaloma" in projects


def test_backend_resolution_returns_cool_when_no_warm_db(populated_catalog: Path):
    """When bathos.db does not exist, _resolve_backend returns 'cool'."""
    backend = _resolve_backend(populated_catalog)
    assert backend == "cool"


def test_backend_resolution_returns_warm_when_db_exists(populated_catalog: Path):
    """When bathos.db exists, _resolve_backend returns 'warm'."""
    # Create a dummy bathos.db file
    (populated_catalog / "bathos.db").touch()
    backend = _resolve_backend(populated_catalog)
    assert backend == "warm"


def test_list_runs_uses_cool_when_no_warm_db(populated_catalog: Path):
    """When warm DB doesn't exist, list_runs uses cool path (PyArrow)."""
    # No bathos.db exists, should use cool path
    runs = list_runs(populated_catalog)
    assert len(runs) == 3
    assert all(isinstance(r, Run) for r in runs)


def test_run_sql_errors_clearly_without_warm_db(tmp_catalog: Path):
    """When warm DB doesn't exist but query needs it (runs table), run_sql raises clear error."""
    init_catalog(tmp_catalog)
    with pytest.raises(RuntimeError, match="No warm catalog.*bth compact"):
        run_sql("SELECT COUNT(*) FROM runs", catalog_dir=tmp_catalog)


def test_warm_list_runs_works_with_real_db(tmp_catalog: Path):
    """When warm DB exists and is valid, list_runs uses warm path (DuckDB)."""
    # Use the compact flow to create a valid warm DB
    from bathos.compact import compact

    init_catalog(tmp_catalog)

    # Create and write runs to cool tier
    base = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    for i in range(2):
        r = Run(
            project_slug="testproj",
            command=f"python run_{i}.py",
            argv=["python", f"run_{i}.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            timestamp=base + timedelta(hours=i),
            status="completed",
            exit_code=0,
        )
        write_run(r, tmp_catalog)

    # Compact to create valid warm DB
    compact(tmp_catalog)

    # Now list_runs should use warm path and return results
    runs = list_runs(tmp_catalog)
    assert len(runs) == 2
    assert all(isinstance(r, Run) for r in runs)


def test_find_runs_filter_by_project(populated_catalog: Path):
    """find_runs with project filter returns only runs from that project."""
    runs = find_runs(populated_catalog, project="espaloma")
    assert len(runs) == 1
    assert runs[0].project_slug == "espaloma"


def test_filter_runs_by_output_file_pattern():
    """Verify output file glob pattern filtering works."""
    from bathos.query import _filter_runs_by_output_file

    run1 = Run(
        project_slug="test",
        command="test1",
        argv=["test1"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        output_paths=["/tmp/result.json"],
    )
    run2 = Run(
        project_slug="test",
        command="test2",
        argv=["test2"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        output_paths=["/tmp/result.csv"],
    )

    runs = [run1, run2]
    filtered = _filter_runs_by_output_file(runs, pattern="*.json")

    assert len(filtered) == 1
    assert filtered[0].id == run1.id


def test_filter_runs_no_pattern_returns_all():
    """Verify no pattern returns all runs."""
    from bathos.query import _filter_runs_by_output_file

    run1 = Run(
        project_slug="test",
        command="test1",
        argv=["test1"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        output_paths=["/tmp/a.json"],
    )
    run2 = Run(
        project_slug="test",
        command="test2",
        argv=["test2"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        output_paths=["/tmp/b.csv"],
    )

    filtered = _filter_runs_by_output_file([run1, run2], pattern=None)
    assert len(filtered) == 2


def test_filter_runs_with_warm_metadata():
    """Verify filtering works with warm-tier metadata."""
    import json

    from bathos.query import _filter_runs_by_output_file

    run = Run(
        project_slug="test",
        command="test",
        argv=["test"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
    )
    run.metadata = json.dumps(
        {"output_files": [{"path": "/results/analysis.json", "status": "present"}]}
    )

    filtered = _filter_runs_by_output_file([run], pattern="*.json")
    assert len(filtered) == 1


def test_filter_ignores_missing_output_files():
    """Verify missing output files are not matched."""
    import json

    from bathos.query import _filter_runs_by_output_file

    run = Run(
        project_slug="test",
        command="test",
        argv=["test"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
    )
    run.metadata = json.dumps(
        {"output_files": [{"path": "/results/missing.json", "status": "missing"}]}
    )

    filtered = _filter_runs_by_output_file([run], pattern="*.json")
    assert len(filtered) == 0


def test_list_runs_includes_outcome(tmp_path):
    """Runs compacted into warm DuckDB expose outcome field via list_runs."""
    import duckdb
    from bathos.compact import compact
    from bathos.query import list_runs
    from bathos.schema import Run
    from bathos.catalog import write_run

    r = Run(project_slug="proj", command="echo hi", argv=["echo", "hi"],
            git_hash="abc", git_branch="main", git_dirty=False,
            status="completed", exit_code=0)
    write_run(r, tmp_path)
    compact(tmp_path)

    # Manually set outcome in DuckDB
    db_path = tmp_path / "bathos.db"
    con = duckdb.connect(str(db_path))
    con.execute(f"UPDATE runs SET outcome = 'pass' WHERE id = '{r.id}'")
    con.close()

    runs = list_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0].outcome == "pass"


def test_lineage_returns_ancestor_chain(tmp_path):
    """Test that lineage() returns ancestor chain following parent_run_id."""
    from bathos.compact import compact
    from bathos.query import lineage

    init_catalog(tmp_path)

    # Create 3 runs where run3.parent_run_id=run2.id, run2.parent_run_id=run1.id
    base = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)

    run1 = Run(
        project_slug="test",
        command="python test1.py",
        argv=["python", "test1.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=base,
        status="completed",
        exit_code=0,
    )
    write_run(run1, tmp_path)

    run2 = Run(
        project_slug="test",
        command="python test2.py",
        argv=["python", "test2.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=base + timedelta(seconds=1),
        status="completed",
        exit_code=0,
        parent_run_id=run1.id,
    )
    write_run(run2, tmp_path)

    run3 = Run(
        project_slug="test",
        command="python test3.py",
        argv=["python", "test3.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=base + timedelta(seconds=2),
        status="completed",
        exit_code=0,
        parent_run_id=run2.id,
    )
    write_run(run3, tmp_path)

    # Compact to warm DB
    compact(tmp_path)

    # Query lineage of run3
    ancestors = lineage(run3.id, tmp_path)

    # Should return [run1, run2, run3] in chronological order
    assert len(ancestors) == 3
    assert ancestors[0].id == run1.id
    assert ancestors[1].id == run2.id
    assert ancestors[2].id == run3.id
    assert ancestors[0].timestamp < ancestors[1].timestamp < ancestors[2].timestamp
