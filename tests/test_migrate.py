import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pathlib import Path
from typer.testing import CliRunner

from bathos.schema import COOL_SCHEMA, Run
from bathos.catalog import write_run

runner = CliRunner()


def _write_old_fragment(runs_dir: Path, stem: str) -> Path:
    """Write a fragment missing the 'outcome' column (pre-v0.2)."""
    old_schema = pa.schema([f for f in COOL_SCHEMA if f.name != "outcome"])
    r = Run(project_slug="p", command="c", argv=["c"], git_hash="abc",
            git_branch="main", git_dirty=False)
    full_tbl = r.to_arrow()
    old_tbl = full_tbl.select([f.name for f in old_schema])
    path = runs_dir / f"run_{stem}.parquet"
    pq.write_table(old_tbl, path)
    return path


def test_migrate_upgrades_old_fragment(tmp_path):
    from bathos.migrate import migrate_catalog
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_old_fragment(runs_dir, "aaa")

    result = migrate_catalog(tmp_path, dry_run=False)
    assert result.scanned == 1
    assert result.migrated == 1
    assert result.already_current == 0

    tbl = pq.read_table(runs_dir / "run_aaa.parquet")
    assert "outcome" in tbl.schema.names
    assert tbl.column("outcome")[0].as_py() == ""


def test_migrate_dry_run_does_not_write(tmp_path):
    from bathos.migrate import migrate_catalog
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_old_fragment(runs_dir, "bbb")

    result = migrate_catalog(tmp_path, dry_run=True)
    assert result.migrated == 1
    assert result.dry_run is True

    tbl = pq.read_table(runs_dir / "run_bbb.parquet")
    assert "outcome" not in tbl.schema.names


def test_migrate_skips_current_fragments(tmp_path):
    from bathos.migrate import migrate_catalog
    r = Run(project_slug="p", command="c", argv=["c"], git_hash="abc",
            git_branch="main", git_dirty=False)
    write_run(r, tmp_path)

    result = migrate_catalog(tmp_path)
    assert result.scanned == 1
    assert result.migrated == 0
    assert result.already_current == 1


def test_migrate_empty_catalog(tmp_path):
    from bathos.migrate import migrate_catalog
    result = migrate_catalog(tmp_path)
    assert result.scanned == 0
    assert result.migrated == 0


def test_migrate_multiple_fragments(tmp_path):
    from bathos.migrate import migrate_catalog
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_old_fragment(runs_dir, "old1")
    _write_old_fragment(runs_dir, "old2")
    r = Run(project_slug="p", command="c", argv=["c"], git_hash="abc",
            git_branch="main", git_dirty=False)
    write_run(r, tmp_path)

    result = migrate_catalog(tmp_path)
    assert result.scanned == 3
    assert result.migrated == 2
    assert result.already_current == 1


def test_cli_migrate_dry_run(tmp_path, monkeypatch):
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path))
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_old_fragment(runs_dir, "ccc")

    from bathos.cli import app
    result = runner.invoke(app, ["migrate", "--dry-run"])
    assert result.exit_code == 0
    assert "1" in result.output
    assert "Would migrate" in result.output

    tbl = pq.read_table(runs_dir / "run_ccc.parquet")
    assert "outcome" not in tbl.schema.names


def test_cli_migrate_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path))
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_old_fragment(runs_dir, "ddd")

    from bathos.cli import app
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0
    assert "Migrated 1" in result.output

    tbl = pq.read_table(runs_dir / "run_ddd.parquet")
    assert "outcome" in tbl.schema.names


def test_migrate_writes_schema_version_on_upgraded_fragments(tmp_path):
    """migrate_catalog sets schema_version = CURRENT_SCHEMA_VERSION on upgraded fragments."""
    from bathos.migrate import migrate_catalog
    from bathos.schema import CURRENT_SCHEMA_VERSION

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_old_fragment(runs_dir, "ver_test")

    migrate_catalog(tmp_path, dry_run=False)

    tbl = pq.read_table(runs_dir / "run_ver_test.parquet")
    versions = tbl.column("schema_version").to_pylist()
    assert all(v == CURRENT_SCHEMA_VERSION for v in versions)


def test_migrate_v6_to_v7_stage_name_null_backfill(tmp_path):
    """Test v6→v7 migration: stage_name missing from v6 parquet is backfilled with None (null), not empty string.

    This test exercises the parquet-level migration path (migrate_catalog), not the dict/Run level.
    Verifies:
    - schema_version is updated to CURRENT_SCHEMA_VERSION (7)
    - stage_name column is added
    - stage_name values are all None (null), not empty strings
    - row count is unchanged (no data loss)
    """
    from bathos.migrate import migrate_catalog
    from bathos.schema import COOL_SCHEMA, CURRENT_SCHEMA_VERSION

    # Create a v6-equivalent schema (COOL_SCHEMA minus stage_name)
    v6_schema = pa.schema([f for f in COOL_SCHEMA if f.name != "stage_name"])

    # Create a sample Run and export to v6 schema
    r = Run(
        project_slug="test-project",
        command="python test.py",
        argv=["python", "test.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )
    full_tbl = r.to_arrow()
    v6_tbl = full_tbl.select([f.name for f in v6_schema])

    # Write v6 parquet to temp catalog
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    fragment_path = runs_dir / "run_v6_test.parquet"
    pq.write_table(v6_tbl, fragment_path)

    # Verify precondition: stage_name does not exist
    tbl_before = pq.read_table(fragment_path)
    assert "stage_name" not in tbl_before.schema.names
    assert tbl_before.num_rows == 1

    # Run migrate_catalog
    result = migrate_catalog(tmp_path, dry_run=False)
    assert result.scanned == 1
    assert result.migrated == 1

    # Read back the migrated table
    tbl_after = pq.read_table(fragment_path)

    # Verify stage_name column was added
    assert "stage_name" in tbl_after.schema.names

    # Verify stage_name values are None (null), not empty strings
    stage_name_col = tbl_after.column("stage_name").to_pylist()
    assert all(v is None for v in stage_name_col), f"Expected all None, got {stage_name_col}"

    # Verify row count unchanged
    assert tbl_after.num_rows == 1

    # Verify schema_version is CURRENT_SCHEMA_VERSION
    schema_versions = tbl_after.column("schema_version").to_pylist()
    assert all(v == CURRENT_SCHEMA_VERSION for v in schema_versions)
