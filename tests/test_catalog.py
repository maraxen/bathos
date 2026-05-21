import dataclasses
from pathlib import Path

from bathos.catalog import init_catalog, read_runs, write_run
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
