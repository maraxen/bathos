import dataclasses
from pathlib import Path

import pyarrow.parquet as pq

from bathos.catalog import init_catalog, read_runs, write_run, write_submit_provenance
from bathos.schema import Run


def test_init_catalog_creates_dirs(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    assert (tmp_catalog / "runs").is_dir()


def test_write_and_read_single_run(tmp_catalog: Path, sample_run: Run):
    init_catalog(tmp_catalog)
    write_run(sample_run, tmp_catalog)
    runs = read_runs(tmp_catalog)
    assert len(runs) == 1
    assert runs[0].id == sample_run.id
    assert runs[0].status == "completed"
    assert runs[0].exit_code == 0
    assert runs[0].output_paths == ["/tmp/results.parquet"]


def test_write_creates_parquet_file(tmp_catalog: Path, sample_run: Run):
    init_catalog(tmp_catalog)
    write_run(sample_run, tmp_catalog)
    files = list((tmp_catalog / "runs").rglob("*.parquet"))
    assert len(files) == 1
    assert sample_run.id in files[0].name


def test_overwrite_deduplicates_by_id(tmp_catalog: Path, sample_run: Run):
    """Writing the same run id twice (status update) should yield one result."""
    init_catalog(tmp_catalog)
    write_run(sample_run, tmp_catalog)
    updated = dataclasses.replace(sample_run, status="completed", duration_s=5.0)
    write_run(updated, tmp_catalog)
    runs = read_runs(tmp_catalog)
    assert len(runs) == 1
    assert runs[0].duration_s == 5.0


def test_multiple_runs_all_returned(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    for i in range(3):
        r = Run(
            project_slug="proj",
            command=f"python run.py --i {i}",
            argv=["python", "run.py", "--i", str(i)],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
        )
        write_run(r, tmp_catalog)
    runs = read_runs(tmp_catalog)
    assert len(runs) == 3


def test_parallel_writes_do_not_collide(tmp_catalog: Path):
    """Simulate two concurrent SLURM jobs writing simultaneously."""
    init_catalog(tmp_catalog)
    r1 = Run(
        project_slug="p", command="a", argv=["a"], git_hash="x", git_branch="main", git_dirty=False
    )
    r2 = Run(
        project_slug="p", command="b", argv=["b"], git_hash="x", git_branch="main", git_dirty=False
    )
    # Write both without waiting (atomic rename ensures no collision)
    write_run(r1, tmp_catalog)
    write_run(r2, tmp_catalog)
    runs = read_runs(tmp_catalog)
    assert len(runs) == 2


def test_write_submit_provenance_creates_file(tmp_catalog: Path):
    """Test write_submit_provenance creates a Parquet file with correct schema."""
    init_catalog(tmp_catalog)
    write_submit_provenance(
        project_slug="myproject",
        command="scripts/experiments/foo.py",
        sidecar_sha256="abc123def456",
        myxcel_job_id="12345",
        stage_name="validation",
        catalog_dir=tmp_catalog,
    )

    # Check file was created
    submit_dir = tmp_catalog / "submits" / "myproject"
    assert submit_dir.is_dir()
    parquet_files = list(submit_dir.glob("*.parquet"))
    assert len(parquet_files) == 1

    # Check schema and data
    table = pq.read_table(parquet_files[0])
    assert table.num_rows == 1
    assert "project_slug" in table.column_names
    assert "command" in table.column_names
    assert "sidecar_sha256" in table.column_names
    assert "bth_submit_version" in table.column_names
    assert "submitted_at" in table.column_names
    assert "myxcel_job_id" in table.column_names
    assert "stage_name" in table.column_names

    # Check field values
    assert table.column("project_slug")[0].as_py() == "myproject"
    assert table.column("command")[0].as_py() == "scripts/experiments/foo.py"
    assert table.column("sidecar_sha256")[0].as_py() == "abc123def456"
    assert table.column("myxcel_job_id")[0].as_py() == "12345"
    assert table.column("stage_name")[0].as_py() == "validation"


def test_write_submit_provenance_atomic_rename(tmp_catalog: Path):
    """Test that write_submit_provenance uses atomic rename (no .tmp file left behind)."""
    init_catalog(tmp_catalog)
    write_submit_provenance(
        project_slug="proj",
        command="scripts/experiments/test.py",
        sidecar_sha256="hash",
        myxcel_job_id="999",
        stage_name="exploration",
        catalog_dir=tmp_catalog,
    )

    submit_dir = tmp_catalog / "submits" / "proj"
    tmp_files = list(submit_dir.glob("*.tmp.parquet"))
    final_files = list(submit_dir.glob("*.parquet"))

    assert len(tmp_files) == 0, "Temporary .tmp.parquet file should not exist after atomic rename"
    assert len(final_files) == 1, "Should have exactly one final Parquet file"
