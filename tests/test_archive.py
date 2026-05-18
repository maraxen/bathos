import json
from pathlib import Path

import pytest

from bathos.archive import ArchiveResult, archive
from bathos.catalog import init_catalog, write_run
from bathos.compact import compact
from bathos.schema import Run


def test_archive_requires_warm_catalog(tmp_catalog: Path):
    """Verify archive errors clearly if warm DB missing."""
    with pytest.raises(RuntimeError, match="No warm catalog"):
        archive(tmp_catalog)


def test_archive_creates_partitions(tmp_catalog: Path, sample_run: Run):
    """Verify archive creates project=.../year=.../month=... structure."""
    init_catalog(tmp_catalog)

    # Write and compact a run
    run = Run(
        project_slug="prolix",
        command="test",
        argv=["test"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )
    write_run(run, tmp_catalog)
    compact(tmp_catalog)

    # Archive
    archive_root = tmp_catalog.parent / "archive"
    result = archive(tmp_catalog, archive_root=archive_root)

    # Verify partition structure
    assert result.runs_archived == 1
    assert result.partitions_created == 1
    assert result.archive_size_bytes > 0

    # Verify files exist
    prolix_dir = archive_root / "project=prolix"
    assert prolix_dir.exists()
    # Year/month will depend on current date
    parquet_files = list(prolix_dir.glob("year=*/month=*/runs.parquet"))
    assert len(parquet_files) == 1


def test_archive_manifest_created(tmp_catalog: Path, sample_run: Run):
    """Verify manifest.json is written with metadata."""
    init_catalog(tmp_catalog)

    run = Run(
        project_slug="test",
        command="test",
        argv=["test"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )
    write_run(run, tmp_catalog)
    compact(tmp_catalog)

    archive_root = tmp_catalog.parent / "archive"
    result = archive(tmp_catalog, archive_root=archive_root)

    assert result.manifest_path.exists()
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["runs_archived"] == 1
    assert manifest["partitions"] == 1
    assert len(manifest["entries"]) == 1


def test_archive_dry_run_no_writes(tmp_catalog: Path, sample_run: Run):
    """Verify --dry-run shows what would happen without writing."""
    init_catalog(tmp_catalog)

    run = Run(
        project_slug="test",
        command="test",
        argv=["test"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )
    write_run(run, tmp_catalog)
    compact(tmp_catalog)

    archive_root = tmp_catalog.parent / "archive"
    result = archive(tmp_catalog, archive_root=archive_root, dry_run=True)

    # Verify nothing written
    assert not archive_root.exists()
    # But counts are still returned
    assert result.runs_archived == 1
    assert result.partitions_created == 1


def test_archive_filters_by_project(tmp_catalog: Path, sample_run: Run):
    """Verify --project filter archives only matching project."""
    init_catalog(tmp_catalog)

    # Write runs from two projects
    run1 = Run(
        project_slug="proj1",
        command="test",
        argv=["test"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )
    run2 = Run(
        project_slug="proj2",
        command="test",
        argv=["test"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )
    write_run(run1, tmp_catalog)
    write_run(run2, tmp_catalog)
    compact(tmp_catalog)

    archive_root = tmp_catalog.parent / "archive"
    result = archive(tmp_catalog, archive_root=archive_root, project_slug="proj1")

    assert result.runs_archived == 1
    assert (archive_root / "project=proj1").exists()
    assert not (archive_root / "project=proj2").exists()


def test_archive_returns_result_object(tmp_catalog: Path, sample_run: Run):
    """Verify archive returns ArchiveResult with all fields populated."""
    init_catalog(tmp_catalog)

    run = Run(
        project_slug="test",
        command="test",
        argv=["test"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )
    write_run(run, tmp_catalog)
    compact(tmp_catalog)

    archive_root = tmp_catalog.parent / "archive"
    result = archive(tmp_catalog, archive_root=archive_root)

    # Verify ArchiveResult fields
    assert isinstance(result, ArchiveResult)
    assert isinstance(result.runs_archived, int)
    assert isinstance(result.partitions_created, int)
    assert isinstance(result.archive_size_bytes, int)
    assert isinstance(result.manifest_path, Path)
    assert isinstance(result.duration_s, float)
    assert result.duration_s >= 0


def test_archive_empty_catalog(tmp_catalog: Path):
    """Verify archive handles empty catalog gracefully."""
    init_catalog(tmp_catalog)

    # Create an empty database
    compact(tmp_catalog)

    archive_root = tmp_catalog.parent / "archive"
    result = archive(tmp_catalog, archive_root=archive_root)

    assert result.runs_archived == 0
    assert result.partitions_created == 0
    assert result.archive_size_bytes == 0
