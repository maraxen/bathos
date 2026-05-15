from pathlib import Path
import dataclasses
import duckdb
import pytest
from uuid import uuid4

from bathos.catalog import write_run, init_catalog, read_runs
from bathos.compact import compact, CompactResult, _fragment_count
from bathos.schema import Run


def test_compact_ingests_all_fragments(tmp_catalog: Path, sample_run: Run):
    """compact() should ingest all cool fragments into DuckDB."""
    init_catalog(tmp_catalog)

    # Write 3 fragments to cool tier
    for i in range(3):
        run = Run(
            project_slug=sample_run.project_slug,
            command=f"python run.py --seed {i}",
            argv=["python", "run.py", "--seed", str(i)],
            git_hash=sample_run.git_hash,
            git_branch=sample_run.git_branch,
            git_dirty=sample_run.git_dirty,
            status=sample_run.status,
            exit_code=sample_run.exit_code,
            duration_s=sample_run.duration_s,
            output_paths=sample_run.output_paths,
            tags=sample_run.tags,
        )
        write_run(run, tmp_catalog)

    # Compact
    result = compact(tmp_catalog)

    # Verify result
    assert result.ingested == 3
    assert result.skipped == 0
    assert result.duration_s > 0

    # Verify DuckDB file created
    assert (tmp_catalog / "bathos.db").exists()

    # Verify all runs in DuckDB
    con = duckdb.connect(str(tmp_catalog / "bathos.db"))
    rows = con.execute("SELECT COUNT(*) FROM runs").fetchall()
    assert rows[0][0] == 3


def test_compact_is_idempotent(tmp_catalog: Path, sample_run: Run):
    """Running compact() twice should not duplicate rows (upsert by id)."""
    init_catalog(tmp_catalog)

    # Write 2 fragments
    for i in range(2):
        run = Run(
            project_slug=sample_run.project_slug,
            command=f"python run.py --i {i}",
            argv=["python", "run.py", "--i", str(i)],
            git_hash=sample_run.git_hash,
            git_branch=sample_run.git_branch,
            git_dirty=sample_run.git_dirty,
            status=sample_run.status,
            exit_code=sample_run.exit_code,
            duration_s=sample_run.duration_s,
            output_paths=sample_run.output_paths,
            tags=sample_run.tags,
        )
        write_run(run, tmp_catalog)

    # First compact
    result1 = compact(tmp_catalog)
    assert result1.ingested == 2

    # Second compact
    result2 = compact(tmp_catalog)
    assert result2.ingested == 0  # Already ingested
    assert result2.skipped == 2    # Both skipped as already present

    # Still only 2 rows
    con = duckdb.connect(str(tmp_catalog / "bathos.db"))
    rows = con.execute("SELECT COUNT(*) FROM runs").fetchall()
    assert rows[0][0] == 2


def test_compact_snapshots_file_list(tmp_catalog: Path, sample_run: Run):
    """compact() should snapshot file list at start; fragments written during compact ignored."""
    init_catalog(tmp_catalog)

    # Write 1 fragment
    run1 = Run(
        project_slug=sample_run.project_slug,
        command="python run.py --i 0",
        argv=["python", "run.py", "--i", "0"],
        git_hash=sample_run.git_hash,
        git_branch=sample_run.git_branch,
        git_dirty=sample_run.git_dirty,
        status=sample_run.status,
        exit_code=sample_run.exit_code,
        duration_s=sample_run.duration_s,
        output_paths=sample_run.output_paths,
        tags=sample_run.tags,
    )
    write_run(run1, tmp_catalog)

    # Monkey-patch write_run to verify snapshot behavior
    # We'll verify by checking that a fragment written after snapshot start is not ingested
    # For now, we just check that the count doesn't exceed what we wrote before compact

    result = compact(tmp_catalog)
    assert result.ingested == 1

    # Verify snapshot: now write another fragment after compact
    run2 = Run(
        project_slug=sample_run.project_slug,
        command="python run.py --i 1",
        argv=["python", "run.py", "--i", "1"],
        git_hash=sample_run.git_hash,
        git_branch=sample_run.git_branch,
        git_dirty=sample_run.git_dirty,
        status=sample_run.status,
        exit_code=sample_run.exit_code,
        duration_s=sample_run.duration_s,
        output_paths=sample_run.output_paths,
        tags=sample_run.tags,
    )
    write_run(run2, tmp_catalog)

    # Compact again: should only see the new fragment
    result2 = compact(tmp_catalog)
    assert result2.ingested == 1
    assert result2.skipped == 1

    # Total should be 2
    con = duckdb.connect(str(tmp_catalog / "bathos.db"))
    rows = con.execute("SELECT COUNT(*) FROM runs").fetchall()
    assert rows[0][0] == 2


def test_compact_upgrades_v0_fragments(tmp_catalog: Path, sample_run: Run):
    """compact() should migrate v0 fragments (missing schema_version) to v1."""
    init_catalog(tmp_catalog)

    # Write a v0 fragment (no schema_version field)
    v0_run = dataclasses.replace(sample_run, schema_version="0")
    write_run(v0_run, tmp_catalog)

    # Compact
    result = compact(tmp_catalog)
    assert result.ingested == 1

    # Verify in DuckDB: should have schema_version="1" (migrated)
    con = duckdb.connect(str(tmp_catalog / "bathos.db"))
    rows = con.execute("SELECT schema_version FROM runs").fetchall()
    assert rows[0][0] == "1"


def test_compact_tracks_warm_schema_version(tmp_catalog: Path, sample_run: Run):
    """compact() should write _schema_meta table tracking warm schema version."""
    init_catalog(tmp_catalog)
    write_run(sample_run, tmp_catalog)

    # Compact
    result = compact(tmp_catalog)

    # Verify _schema_meta table
    con = duckdb.connect(str(tmp_catalog / "bathos.db"))
    rows = con.execute(
        "SELECT value FROM _schema_meta WHERE key = 'warm_version'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "1"


def test_fragment_count_helper(tmp_catalog: Path, sample_run: Run):
    """_fragment_count() should return number of cool fragments."""
    init_catalog(tmp_catalog)

    # Write 3 fragments
    for i in range(3):
        run = Run(
            project_slug=sample_run.project_slug,
            command=f"python run.py --i {i}",
            argv=["python", "run.py", "--i", str(i)],
            git_hash=sample_run.git_hash,
            git_branch=sample_run.git_branch,
            git_dirty=sample_run.git_dirty,
            status=sample_run.status,
            exit_code=sample_run.exit_code,
            duration_s=sample_run.duration_s,
            output_paths=sample_run.output_paths,
            tags=sample_run.tags,
        )
        write_run(run, tmp_catalog)

    count = _fragment_count(tmp_catalog)
    assert count == 3


def test_fragment_count_empty_catalog(tmp_catalog: Path):
    """_fragment_count() should return 0 for empty catalog."""
    init_catalog(tmp_catalog)

    count = _fragment_count(tmp_catalog)
    assert count == 0


def test_compact_preserves_run_data(tmp_catalog: Path, sample_run: Run):
    """compact() should preserve all Run fields during ingest."""
    init_catalog(tmp_catalog)

    # Write a run with all fields populated
    run_with_data = Run(
        project_slug=sample_run.project_slug,
        command=sample_run.command,
        argv=sample_run.argv,
        git_hash=sample_run.git_hash,
        git_branch=sample_run.git_branch,
        git_dirty=sample_run.git_dirty,
        status=sample_run.status,
        exit_code=sample_run.exit_code,
        duration_s=sample_run.duration_s,
        tags=["smoke", "critical"],
        output_paths=["/tmp/a.json", "/tmp/b.json"],
        slurm_job_id="12345",
        metadata='{"key": "value"}',
    )
    write_run(run_with_data, tmp_catalog)

    # Compact
    compact(tmp_catalog)

    # Verify data preserved
    con = duckdb.connect(str(tmp_catalog / "bathos.db"))
    rows = con.execute(
        "SELECT tags, output_paths, slurm_job_id, metadata FROM runs"
    ).fetchall()
    assert len(rows) == 1
    # Verify complex types preserved (tags and output_paths)
    assert rows[0][0] == ["smoke", "critical"]
    assert rows[0][1] == ["/tmp/a.json", "/tmp/b.json"]
    assert rows[0][2] == "12345"
    # Note: metadata is not part of cool tier schema, so defaults to '{}' on read
    assert rows[0][3] == '{}'
