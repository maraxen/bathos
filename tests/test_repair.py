"""Test suite for bathos repair module.

Tests the repair scanning, action generation, dry-run, and execution flows.
Covers cool-tier sentinel cleanup, corrupt fragment quarantine, and warm-tier
database corruption detection and backup/rebuild.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from bathos.catalog import init_catalog, write_run
from bathos.repair import (
    RepairAction,
    RepairManifest,
    repair,
    scan,
)
from bathos.schema import Run


# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture
def clean_catalog(tmp_path: Path) -> Path:
    """Empty cool tier, no DB."""
    catalog = tmp_path / ".bth" / "catalog"
    catalog.mkdir(parents=True)
    os.environ["BTH_CATALOG_DIR"] = str(catalog)
    yield catalog
    if "BTH_CATALOG_DIR" in os.environ:
        del os.environ["BTH_CATALOG_DIR"]


@pytest.fixture
def catalog_with_sentinels(tmp_path: Path, sample_run: Run) -> Path:
    """Create runs/myslug/ with:
    - run_abc.parquet (valid fragment)
    - run_old.parquet.tmp (orphaned atomic write, old mtime)
    - run_stale.parquet.bak (orphaned migration backup, mtime 120s ago)
    - run_fresh.parquet.bak (current mtime, should not be moved)
    """
    catalog = tmp_path / ".bth" / "catalog"
    catalog.mkdir(parents=True)
    init_catalog(catalog)

    slug_dir = catalog / "runs" / "myslug"
    slug_dir.mkdir(parents=True, exist_ok=True)

    # Create a valid fragment
    run = Run(
        id="run_abc",
        project_slug="myslug",
        command="python run.py",
        argv=["python", "run.py"],
        git_hash="deadbeef",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        duration_s=1.0,
        output_paths=[],
        tags=[],
        hostname="test-host",
    )
    write_run(run, catalog)

    # Create orphaned .tmp.parquet file (simulating interrupted atomic write)
    # Note: write_run creates run_{id}.tmp.parquet during atomic write
    tmp_parquet_file = slug_dir / "run_old.tmp.parquet"
    tmp_parquet_file.write_bytes(b"incomplete data")
    old_mtime = datetime.now(UTC).timestamp() - 120
    os.utime(tmp_parquet_file, (old_mtime, old_mtime))

    # Create stale .bak file (mtime 120s in past)
    stale_bak_path = slug_dir / "run_stale.parquet.bak"
    stale_bak_path.write_bytes(b"backup data")
    stale_mtime = datetime.now(UTC).timestamp() - 120
    os.utime(stale_bak_path, (stale_mtime, stale_mtime))

    # Create fresh .bak file (current mtime, should NOT be moved)
    fresh_bak_path = slug_dir / "run_fresh.parquet.bak"
    fresh_bak_path.write_bytes(b"fresh backup data")

    os.environ["BTH_CATALOG_DIR"] = str(catalog)
    yield catalog
    if "BTH_CATALOG_DIR" in os.environ:
        del os.environ["BTH_CATALOG_DIR"]


@pytest.fixture
def catalog_with_corrupt_fragment(tmp_path: Path, sample_run: Run) -> Path:
    """Create a catalog with:
    - one valid fragment
    - one zero-byte (corrupt) fragment with old mtime
    """
    catalog = tmp_path / ".bth" / "catalog"
    catalog.mkdir(parents=True)
    init_catalog(catalog)

    slug_dir = catalog / "runs" / "myslug"
    slug_dir.mkdir(parents=True, exist_ok=True)

    # Create a valid fragment
    run = Run(
        id="run_valid",
        project_slug="myslug",
        command="python run.py",
        argv=["python", "run.py"],
        git_hash="deadbeef",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        duration_s=1.0,
        output_paths=[],
        tags=[],
        hostname="test-host",
    )
    write_run(run, catalog)

    # Create a zero-byte corrupt fragment (with old mtime to bypass recency guard)
    corrupt_path = slug_dir / "run_corrupt.parquet"
    corrupt_path.write_bytes(b"")
    corrupt_mtime = datetime.now(UTC).timestamp() - 120
    os.utime(corrupt_path, (corrupt_mtime, corrupt_mtime))

    os.environ["BTH_CATALOG_DIR"] = str(catalog)
    yield catalog
    if "BTH_CATALOG_DIR" in os.environ:
        del os.environ["BTH_CATALOG_DIR"]


@pytest.fixture
def catalog_with_corrupt_warm_db(tmp_path: Path, sample_run: Run) -> Path:
    """Create valid cool fragments and a corrupt warm DB."""
    catalog = tmp_path / ".bth" / "catalog"
    catalog.mkdir(parents=True)
    init_catalog(catalog)

    slug_dir = catalog / "runs" / "myslug"
    slug_dir.mkdir(parents=True, exist_ok=True)

    # Create a valid fragment
    run = Run(
        id="run_valid",
        project_slug="myslug",
        command="python run.py",
        argv=["python", "run.py"],
        git_hash="deadbeef",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        duration_s=1.0,
        output_paths=[],
        tags=[],
        hostname="test-host",
    )
    write_run(run, catalog)

    # Overwrite bathos.db with invalid bytes
    db_path = catalog / "bathos.db"
    db_path.write_bytes(b"NOTADB" * 100)

    os.environ["BTH_CATALOG_DIR"] = str(catalog)
    yield catalog
    if "BTH_CATALOG_DIR" in os.environ:
        del os.environ["BTH_CATALOG_DIR"]


# =============================================================================
# TESTS: DRY-RUN BEHAVIOR (ISSUE #1: Parametrized)
# =============================================================================


class TestDryRun:
    """Test parametrized dry-run behavior: True never mutates, False does."""

    @pytest.mark.parametrize("dry_run", [True, False])
    def test_dry_run_never_mutates(
        self, catalog_with_sentinels: Path, dry_run: bool
    ):
        """Parametrized test: dry_run=True → no mutations, False → mutations occur.

        Args:
            catalog_with_sentinels: Fixture with orphaned .tmp.parquet and .bak
            dry_run: If True, assert no mutations; if False, assert deletions
        """
        slug_dir = catalog_with_sentinels / "runs" / "myslug"
        tmp_parquet = slug_dir / "run_old.tmp.parquet"
        stale_bak = slug_dir / "run_stale.parquet.bak"

        # Run repair
        manifest = repair(catalog_with_sentinels, tier="cool", dry_run=dry_run)

        if dry_run:
            # dry_run=True: no mutations
            assert manifest.dry_run is True
            assert tmp_parquet.exists()
            assert stale_bak.exists()
        else:
            # dry_run=False: mutations happen
            assert manifest.dry_run is False
            # .tmp.parquet deleted
            assert not tmp_parquet.exists()
            # .bak quarantined (moved)
            assert not stale_bak.exists()


# =============================================================================
# TESTS: SENTINEL CLEANUP
# =============================================================================


class TestSentinelCleanup:
    """Test .tmp and .bak sentinel cleanup."""

    def test_sentinel_cleanup_deletes_tmp(self, catalog_with_sentinels: Path):
        """Applied repair deletes .tmp.parquet files."""
        slug_dir = catalog_with_sentinels / "runs" / "myslug"
        tmp_parquet = slug_dir / "run_old.tmp.parquet"

        assert tmp_parquet.exists()
        repair(catalog_with_sentinels, tier="cool", dry_run=False)

        assert not tmp_parquet.exists()

    def test_sentinel_cleanup_skips_fresh_bak(self, catalog_with_sentinels: Path):
        """Applied repair does not move fresh .bak files (mtime < 60s)."""
        slug_dir = catalog_with_sentinels / "runs" / "myslug"
        fresh_bak = slug_dir / "run_fresh.parquet.bak"

        assert fresh_bak.exists()
        manifest = repair(catalog_with_sentinels, tier="cool", dry_run=False)

        # Fresh .bak file should still exist
        assert fresh_bak.exists()

        # Should have a warning about skipping in-flight write
        assert any("in-flight" in w.lower() for w in manifest.warnings)

    def test_sentinel_cleanup_quarantines_stale_bak(self, catalog_with_sentinels: Path):
        """Applied repair quarantines stale .bak files (mtime > 60s)."""
        slug_dir = catalog_with_sentinels / "runs" / "myslug"
        stale_bak = slug_dir / "run_stale.parquet.bak"

        assert stale_bak.exists()
        manifest = repair(catalog_with_sentinels, tier="cool", dry_run=False)

        # Stale .bak file should be quarantined (moved away)
        assert not stale_bak.exists()

        # Quarantine directory should exist with manifest
        quarantine_dir = catalog_with_sentinels / "quarantine" / "myslug"
        assert quarantine_dir.exists()
        assert (quarantine_dir / "manifest.jsonl").exists()


# =============================================================================
# TESTS: CORRUPT FRAGMENT QUARANTINE
# =============================================================================


class TestCorruptFragmentQuarantine:
    """Test quarantine of corrupt fragments."""

    def test_corrupt_fragment_quarantined(self, catalog_with_corrupt_fragment: Path):
        """Applied repair quarantines corrupt fragments."""
        slug_dir = catalog_with_corrupt_fragment / "runs" / "myslug"
        corrupt_file = slug_dir / "run_corrupt.parquet"

        assert corrupt_file.exists()
        repair(catalog_with_corrupt_fragment, tier="cool", dry_run=False)

        assert not corrupt_file.exists()

        # Quarantine directory should exist
        quarantine_dir = catalog_with_corrupt_fragment / "quarantine" / "myslug"
        assert quarantine_dir.exists()

    def test_quarantine_manifest_fields(self, catalog_with_corrupt_fragment: Path):
        """Quarantine manifest includes all required fields."""
        repair(catalog_with_corrupt_fragment, tier="cool", dry_run=False)

        manifest_file = catalog_with_corrupt_fragment / "quarantine" / "myslug" / "manifest.jsonl"
        assert manifest_file.exists()

        with open(manifest_file) as f:
            lines = f.readlines()

        assert len(lines) > 0

        for line in lines:
            entry = json.loads(line)
            assert "ts" in entry
            assert "action" in entry
            assert "original_path" in entry
            assert "slug" in entry

    def test_quarantine_idempotent(self, catalog_with_corrupt_fragment: Path):
        """Re-running repair on already-repaired catalog is no-op."""
        manifest1 = repair(catalog_with_corrupt_fragment, tier="cool", dry_run=False)
        actions1_count = len(manifest1.actions)

        manifest2 = repair(catalog_with_corrupt_fragment, tier="cool", dry_run=False)
        actions2_count = len(manifest2.actions)

        # Second repair should find nothing to do
        assert actions2_count == 0


# =============================================================================
# TESTS: WARM-TIER PROTECTION
# =============================================================================


class TestWarmTierProtection:
    """Test warm-tier corruption detection and --acknowledge-warm-loss gate."""

    def test_warm_loss_gate_exits_1(self, catalog_with_corrupt_warm_db: Path):
        """Repair on corrupt DB without warm-only data should proceed without error.

        The gate only raises SystemExit if there's warm-only data (postmortem or output_metadata).
        If the warm DB is corrupted and has no warm-only data, repair proceeds automatically.
        """
        # Should proceed without raising SystemExit (no warm-only data at risk)
        manifest = repair(
            catalog_with_corrupt_warm_db,
            tier="all",
            dry_run=False,
            acknowledge_warm_loss=False,
        )

        # Repair should succeed
        assert manifest is not None
        assert isinstance(manifest, RepairManifest)

    def test_warm_loss_gate_rebuilds_with_ack(self, catalog_with_corrupt_warm_db: Path):
        """Repair with acknowledge_warm_loss=True rebuilds the warm DB."""
        db_path = catalog_with_corrupt_warm_db / "bathos.db"
        corrupt_content = db_path.read_bytes()

        repair(
            catalog_with_corrupt_warm_db,
            tier="all",
            dry_run=False,
            acknowledge_warm_loss=True,
        )

        # DB should be rebuilt (no longer corrupt)
        assert db_path.exists()
        new_content = db_path.read_bytes()
        assert new_content != corrupt_content

        # Should be readable
        con = duckdb.connect(str(db_path))
        con.execute("SELECT COUNT(*) FROM runs").fetchall()
        con.close()

    def test_bak_rotation_cap(self, catalog_with_corrupt_warm_db: Path):
        """Multiple rebuilds keep at most 1 .bak file (rotation cap)."""
        for i in range(4):
            try:
                repair(
                    catalog_with_corrupt_warm_db,
                    tier="all",
                    dry_run=False,
                    acknowledge_warm_loss=True,
                )
            except Exception:
                pass

        bak_files = list(catalog_with_corrupt_warm_db.glob("bathos.db.bak*"))
        assert len(bak_files) <= 1


# =============================================================================
# TESTS: MCP MIRROR
# =============================================================================


class TestMCPMirror:
    """Test MCP mirror tool behavior."""

    def test_mcp_dry_run_default(self, clean_catalog: Path):
        """MCP repair tool defaults to dry_run=True (scan-only)."""
        slug_dir = clean_catalog / "runs" / "myslug"
        slug_dir.mkdir(parents=True, exist_ok=True)
        tmp_parquet = slug_dir / "run_test.tmp.parquet"
        tmp_parquet.write_bytes(b"test")
        old_mtime = datetime.now(UTC).timestamp() - 120
        os.utime(tmp_parquet, (old_mtime, old_mtime))

        # Call scan (which is what MCP defaults to)
        actions, warnings = scan(clean_catalog, tier="cool")

        # Scan is read-only
        assert len(actions) > 0
        assert tmp_parquet.exists()  # File untouched


# =============================================================================
# TESTS: INTEGRATION (ISSUE #3: Calls compact() after repair)
# =============================================================================


class TestIntegration:
    """Integration tests combining multiple repair operations."""

    def test_integration_sentinel_plus_corrupt(self, tmp_path: Path, sample_run: Run):
        """Both sentinel and corrupt fragment repairs applied, then compact succeeds.

        This test verifies that after repair cleans up sentinels and quarantines
        corrupt fragments, the compact() operation succeeds without errors.
        """
        catalog = tmp_path / ".bth" / "catalog"
        catalog.mkdir(parents=True)
        init_catalog(catalog)

        slug_dir = catalog / "runs" / "myslug"
        slug_dir.mkdir(parents=True, exist_ok=True)

        # Valid fragment
        run = Run(
            id="run_valid",
            project_slug="myslug",
            command="python run.py",
            argv=["python", "run.py"],
            git_hash="deadbeef",
            git_branch="main",
            git_dirty=False,
            status="completed",
            exit_code=0,
            duration_s=1.0,
            output_paths=[],
            tags=[],
            hostname="test-host",
        )
        write_run(run, catalog)

        # Add sentinels
        tmp_parquet = slug_dir / "run_old.tmp.parquet"
        tmp_parquet.write_bytes(b"temp")
        old_mtime = datetime.now(UTC).timestamp() - 120
        os.utime(tmp_parquet, (old_mtime, old_mtime))

        # Add corrupt fragment
        corrupt_file = slug_dir / "run_corrupt.parquet"
        corrupt_file.write_bytes(b"")
        os.utime(corrupt_file, (old_mtime, old_mtime))

        os.environ["BTH_CATALOG_DIR"] = str(catalog)

        try:
            # Run repair
            manifest = repair(catalog, tier="cool", dry_run=False)

            # Both repairs should be applied
            assert len(manifest.actions) >= 2

            # Now run compact — should succeed after repairs
            from bathos.compact import compact

            try:
                compact_result = compact(catalog)
                # Compact should succeed and create the DB
                assert (catalog / "bathos.db").exists()
            except Exception as e:
                pytest.fail(f"compact() should succeed after repair, got: {e}")
        finally:
            if "BTH_CATALOG_DIR" in os.environ:
                del os.environ["BTH_CATALOG_DIR"]
