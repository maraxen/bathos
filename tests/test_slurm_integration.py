"""Tests for SLURM integration: _bth_env.sh template, catalog-dir override, slurm-job filter."""

from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest

from bathos.catalog import write_run, init_catalog
from bathos.init import init_project, _load_env_sh_template
from bathos.query import find_runs
from bathos.schema import Run


def test_bth_env_sh_exports_catalog_dir(tmp_path: Path):
    """_bth_env.sh template exports BTH_CATALOG_DIR with placeholder."""
    template = _load_env_sh_template()
    assert "BTH_CATALOG_DIR" in template
    assert "{catalog_dir}" in template


def test_init_generates_bth_env_sh_with_catalog_dir(tmp_path: Path):
    """bth init writes _bth_env.sh with BTH_CATALOG_DIR export."""
    init_project(tmp_path, slug="myproj", catalog_dir=tmp_path / ".bth" / "catalog")
    env_sh = (tmp_path / "scripts" / "slurm" / "_bth_env.sh").read_text()
    assert "BTH_CATALOG_DIR" in env_sh
    assert ".bth/catalog" in env_sh


def test_init_catalog_dir_override(tmp_path: Path):
    """bth init --catalog-dir flag overrides catalog_dir in _bth_env.sh."""
    custom_catalog = tmp_path / "custom" / "catalog"
    init_project(tmp_path, slug="myproj", catalog_dir=custom_catalog)
    env_sh = (tmp_path / "scripts" / "slurm" / "_bth_env.sh").read_text()
    # Verify the custom path is in the exported BTH_CATALOG_DIR
    assert str(custom_catalog) in env_sh or "custom/catalog" in env_sh


def test_check_filter_by_slurm_job(tmp_catalog: Path):
    """find_runs with slurm_job_id filter returns only matching runs."""
    init_catalog(tmp_catalog)
    base = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)

    # Create runs with and without slurm_job_id
    r1 = Run(
        project_slug="prolix",
        command="python test1.py",
        argv=["python", "test1.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=base,
        status="completed",
        exit_code=0,
        slurm_job_id="12345",  # Array element 0
    )
    write_run(r1, tmp_catalog)

    r2 = Run(
        project_slug="prolix",
        command="python test2.py",
        argv=["python", "test2.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=base + timedelta(hours=1),
        status="completed",
        exit_code=0,
        slurm_job_id="12345",  # Same job, array element 1
    )
    write_run(r2, tmp_catalog)

    r3 = Run(
        project_slug="prolix",
        command="python test3.py",
        argv=["python", "test3.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=base + timedelta(hours=2),
        status="completed",
        exit_code=0,
        slurm_job_id="67890",  # Different job
    )
    write_run(r3, tmp_catalog)

    # Filter by slurm_job_id
    runs = find_runs(tmp_catalog, slurm_job_id="12345")
    assert len(runs) == 2
    assert all(r.slurm_job_id == "12345" for r in runs)


def test_find_runs_slurm_job_filter_with_other_filters(tmp_catalog: Path):
    """find_runs with slurm_job_id AND project filter works correctly."""
    init_catalog(tmp_catalog)
    base = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)

    r1 = Run(
        project_slug="prolix",
        command="python test1.py",
        argv=["python", "test1.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=base,
        status="completed",
        exit_code=0,
        slurm_job_id="12345",
    )
    write_run(r1, tmp_catalog)

    r2 = Run(
        project_slug="espaloma",
        command="python test2.py",
        argv=["python", "test2.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=base + timedelta(hours=1),
        status="completed",
        exit_code=0,
        slurm_job_id="12345",
    )
    write_run(r2, tmp_catalog)

    # Filter by both slurm_job_id and project
    runs = find_runs(tmp_catalog, project="prolix", slurm_job_id="12345")
    assert len(runs) == 1
    assert runs[0].project_slug == "prolix"
    assert runs[0].slurm_job_id == "12345"
