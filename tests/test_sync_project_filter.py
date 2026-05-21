"""Tests for per-project sync filtering and migration to project subdirectories."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bathos.catalog import write_run
from bathos.config import ProjectConfig
from bathos.migrate import migrate_to_project_subdirs
from bathos.schema import Run
from bathos.sync import SyncResult, sync_catalog


def _make_run(slug: str, suffix: str = "") -> Run:
    return Run(
        project_slug=slug,
        command=f"python {slug}{suffix}.py",
        argv=["python", f"{slug}{suffix}.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )


def _write_flat(runs_dir: Path, slug: str, suffix: str = "") -> Path:
    """Write a parquet with project_slug directly in the flat runs/ dir (pre-migration layout)."""
    run = _make_run(slug, suffix)
    tbl = run.to_arrow()
    path = runs_dir / f"run_{run.id}.parquet"
    pq.write_table(tbl, path)
    return path


def _config(tmp_path: Path, slug: str, sync_filter: str = "project_slug") -> ProjectConfig:
    return ProjectConfig(
        slug=slug,
        root=tmp_path,
        remotes={"engaging": {"host": "engaging", "remote_root": f"~/projects/{slug}"}},
        sync_filter=sync_filter,
    )


# ---------------------------------------------------------------------------
# 1. Sync filters by slug: only this project's subdir is pushed
# ---------------------------------------------------------------------------

def test_sync_filters_by_slug(tmp_path: Path):
    catalog = tmp_path / "catalog"
    config = _config(tmp_path, "asr")

    # Write runs for two projects into their respective subdirs via write_run
    write_run(_make_run("asr"), catalog)
    write_run(_make_run("asr", "_2"), catalog)
    write_run(_make_run("prolix"), catalog)

    with patch("bathos.sync.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "Number of regular files transferred: 2"

        result = sync_catalog("engaging", config, catalog, pull=False)

        cmd = mock_run.call_args[0][0]
        # Source should be the project-slug subdir
        assert any(f"runs/asr/" in str(a) for a in cmd), f"Expected runs/asr/ in cmd: {cmd}"
        # Should NOT reference prolix
        assert not any("prolix" in str(a) for a in cmd)
        # Filtered count: 1 prolix run
        assert result.filtered == 1


# ---------------------------------------------------------------------------
# 2. sync_filter="none" preserves current flat behavior
# ---------------------------------------------------------------------------

def test_sync_no_filter_mode_preserves_current_behavior(tmp_path: Path):
    catalog = tmp_path / "catalog"
    config = _config(tmp_path, "asr", sync_filter="none")

    write_run(_make_run("asr"), catalog)
    write_run(_make_run("prolix"), catalog)

    with patch("bathos.sync.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""

        result = sync_catalog("engaging", config, catalog, pull=False)

        cmd = mock_run.call_args[0][0]
        # Source should be the flat runs/ dir (no slug subdir)
        assert any(str(catalog / "runs") + "/" in str(a) for a in cmd), f"Expected flat runs/ in cmd: {cmd}"
        assert not any("asr" in str(a) and "runs/asr" in str(a) for a in cmd)
        assert result.filtered == 0


# ---------------------------------------------------------------------------
# 3. Pull uses project-slug subdir as destination
# ---------------------------------------------------------------------------

def test_sync_pull_targets_project_subdir(tmp_path: Path):
    catalog = tmp_path / "catalog"
    config = _config(tmp_path, "asr")

    with patch("bathos.sync.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""

        sync_catalog("engaging", config, catalog, pull=True)

        cmd = mock_run.call_args[0][0]
        # Remote source should include the slug subdir
        assert any("runs/asr/" in str(a) for a in cmd), f"Expected runs/asr/ in cmd: {cmd}"
        # Local destination should also include the slug subdir
        assert any(str(catalog / "runs" / "asr") in str(a) for a in cmd)


# ---------------------------------------------------------------------------
# 4. migrate_to_project_subdirs --dry-run reports counts but doesn't move
# ---------------------------------------------------------------------------

def test_migrate_to_project_subdirs_dry_run(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_flat(runs_dir, "asr")
    _write_flat(runs_dir, "prolix")

    result = migrate_to_project_subdirs(tmp_path, dry_run=True)

    assert result.moved == 2
    assert result.dry_run is True
    # Files should still be in the flat dir
    assert len(list(runs_dir.glob("run_*.parquet"))) == 2
    assert not (runs_dir / "asr").exists()
    assert not (runs_dir / "prolix").exists()


# ---------------------------------------------------------------------------
# 5. migrate_to_project_subdirs is idempotent
# ---------------------------------------------------------------------------

def test_migrate_to_project_subdirs_idempotent(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_flat(runs_dir, "asr")
    _write_flat(runs_dir, "prolix")

    result1 = migrate_to_project_subdirs(tmp_path, dry_run=False)
    assert result1.moved == 2

    # Second call: no flat files left → nothing to move
    result2 = migrate_to_project_subdirs(tmp_path, dry_run=False)
    assert result2.moved == 0

    # Files are in the right places
    assert len(list((runs_dir / "asr").glob("run_*.parquet"))) == 1
    assert len(list((runs_dir / "prolix").glob("run_*.parquet"))) == 1


# ---------------------------------------------------------------------------
# 6. Migration fallback for parquets without a project_slug
# ---------------------------------------------------------------------------

def test_migrate_fallback_for_runs_without_slug(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    # Write a parquet with no project_slug column
    tbl = pa.table({"id": ["abc-123"], "command": ["python test.py"]})
    path = runs_dir / "run_abc-123.parquet"
    pq.write_table(tbl, path)

    result = migrate_to_project_subdirs(tmp_path, dry_run=False)

    assert result.moved == 1
    assert result.by_slug == {"default": 1}
    assert (runs_dir / "default" / "run_abc-123.parquet").exists()


# ---------------------------------------------------------------------------
# 7. bth ls finds runs after migration (read_runs uses rglob)
# ---------------------------------------------------------------------------

def test_ls_finds_runs_in_subdirs(tmp_path: Path):
    from bathos.catalog import read_runs

    catalog = tmp_path / "catalog"
    run = _make_run("asr")
    write_run(run, catalog)  # now writes to catalog/runs/asr/

    runs = read_runs(catalog)
    assert len(runs) == 1
    assert runs[0].project_slug == "asr"
    assert runs[0].id == run.id


# ---------------------------------------------------------------------------
# 8. bth run writes new runs under runs/<slug>/
# ---------------------------------------------------------------------------

def test_run_writes_to_project_subdir(tmp_path: Path):
    from bathos.catalog import write_run

    catalog = tmp_path / "catalog"
    run = _make_run("myproject")
    write_run(run, catalog)

    # Should be under runs/myproject/, NOT in runs/ directly
    assert (catalog / "runs" / "myproject" / f"run_{run.id}.parquet").exists()
    assert not (catalog / "runs" / f"run_{run.id}.parquet").exists()
