"""Tests for archive-tier repair (quarantine_archive action).

Tests cover:
1. scan() detects SHA256 mismatches and returns quarantine_archive actions
2. dry-run lists actions without moving files
3. repair() moves partition files to quarantine/<year>/<month>/ path
4. manifest.json is updated to mark entries as quarantined (tombstone preservation)
5. manifest.jsonl includes both expected_sha256 and actual_sha256 for forensic audit
6. Re-running repair on same partition is idempotent
7. Valid partitions are not touched
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bathos.archive import archive
from bathos.catalog import init_catalog, write_run
from bathos.compact import compact
from bathos.repair import RepairAction, _actions_from_archive_verify, repair, scan
from bathos.schema import Run
from bathos.verify import verify_archive


class TestArchiveQuarantine:
    """Test suite for archive partition quarantine repair."""

    def test_scan_returns_quarantine_archive_for_sha256_mismatch(
        self, tmp_path: Path
    ) -> None:
        """Verify scan() detects SHA256 mismatch and returns quarantine_archive action."""
        catalog_dir = tmp_path / "catalog"
        archive_root = tmp_path / "archive"
        catalog_dir.mkdir(parents=True)

        # Set up initial archive with good data
        init_catalog(catalog_dir)
        run = Run(
            project_slug="test",
            command="test",
            argv=["test"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
        )
        write_run(run, catalog_dir)
        compact(catalog_dir)
        archive(catalog_dir, archive_root=archive_root)

        # Verify archive is clean initially
        result = verify_archive(archive_root)
        assert result.ok, f"Archive should be clean initially: {result.errors}"

        # Manually update the manifest to have a wrong SHA256, simulating post-archive corruption
        # without actually corrupting the parquet file
        manifest_path = archive_root / "manifest.json"
        with open(manifest_path) as f:
            manifest = json.load(f)

        # Change the SHA256 in the manifest to a different value
        if manifest.get("entries"):
            manifest["entries"][0]["sha256"] = "0" * 64  # Fake SHA256
            with open(manifest_path, "w") as f:
                json.dump(manifest, f, indent=2)

        # Now verify_archive should detect the SHA256 mismatch
        result = verify_archive(archive_root)
        assert not result.ok, "Archive should fail verification after SHA256 mismatch in manifest"
        assert any("SHA256 mismatch" in error for error in result.errors), (
            f"Should detect SHA256 mismatch. Errors: {result.errors}"
        )

        # Run repair scan and check for quarantine_archive action
        actions, warnings = scan(catalog_dir, tier="archive", archive_root=archive_root)
        assert any(
            a.action == "quarantine_archive" for a in actions
        ), f"Should have quarantine_archive action. Got: {[a.action for a in actions]}"

    def test_dry_run_lists_action_no_move(self, tmp_path: Path) -> None:
        """Verify --dry-run lists actions without moving files."""
        catalog_dir = tmp_path / "catalog"
        archive_root = tmp_path / "archive"
        catalog_dir.mkdir(parents=True)

        # Set up archive
        init_catalog(catalog_dir)
        run = Run(
            project_slug="test",
            command="test",
            argv=["test"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
        )
        write_run(run, catalog_dir)
        compact(catalog_dir)
        archive(catalog_dir, archive_root=archive_root)

        # Corrupt the archive
        partition_path = archive_root / "project=test"
        parquet_files = list(partition_path.glob("year=*/month=*/runs.parquet"))
        bad_file = parquet_files[0]
        bad_file.write_bytes(b"corrupted")

        # Run repair with dry_run=True
        manifest = repair(
            catalog_dir,
            tier="archive",
            dry_run=True,
            archive_root=archive_root,
        )

        assert manifest.dry_run is True
        # In dry-run, the file should still exist at original location
        assert (
            bad_file.exists()
        ), "File should not be moved during dry-run"

    def test_repair_moves_to_quarantine_year_month_path(
        self, tmp_path: Path
    ) -> None:
        """Verify repair() moves partition files to correct subpath."""
        catalog_dir = tmp_path / "catalog"
        archive_root = tmp_path / "archive"
        catalog_dir.mkdir(parents=True)

        # Set up archive
        init_catalog(catalog_dir)
        run = Run(
            project_slug="myproj",
            command="test",
            argv=["test"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
        )
        write_run(run, catalog_dir)
        compact(catalog_dir)
        archive(catalog_dir, archive_root=archive_root)

        # Corrupt the archive
        partition_path = archive_root / "project=myproj"
        parquet_files = list(partition_path.glob("year=*/month=*/runs.parquet"))
        assert len(parquet_files) == 1
        bad_file = parquet_files[0]
        original_content = bad_file.read_bytes()
        bad_file.write_bytes(b"corrupted")

        # Run repair (not dry-run)
        manifest = repair(
            catalog_dir,
            tier="archive",
            dry_run=False,
            archive_root=archive_root,
        )

        # Verify file was moved
        assert not bad_file.exists(), "Original file should be moved"

        # Verify quarantine directory structure: .bth/quarantine/archive/<year>/<month>/
        quarantine_root = catalog_dir / "quarantine" / "archive"
        assert quarantine_root.exists(), "Quarantine archive directory should exist"

        # Find the quarantined file
        quarantined_files = list(
            quarantine_root.glob("*/*/*.parquet")
        )
        assert (
            len(quarantined_files) == 1
        ), f"Should have one quarantined file. Found: {quarantined_files}"

        quarantined_file = quarantined_files[0]
        # Verify the quarantined file exists and contains original (corrupted) data
        assert quarantined_file.exists(), "Quarantined file should exist"

    def test_manifest_json_tombstoned_not_deleted(
        self, tmp_path: Path
    ) -> None:
        """Verify manifest.json entry is marked quarantined, not deleted."""
        catalog_dir = tmp_path / "catalog"
        archive_root = tmp_path / "archive"
        catalog_dir.mkdir(parents=True)

        # Set up archive
        init_catalog(catalog_dir)
        run = Run(
            project_slug="test",
            command="test",
            argv=["test"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
        )
        write_run(run, catalog_dir)
        compact(catalog_dir)
        archive(catalog_dir, archive_root=archive_root)

        # Corrupt the archive
        partition_path = archive_root / "project=test"
        parquet_files = list(partition_path.glob("year=*/month=*/runs.parquet"))
        bad_file = parquet_files[0]
        bad_file.write_bytes(b"corrupted")

        # Get the partition name
        partition_name = str(bad_file.parent.parent.parent.name)
        for part in bad_file.parent.parent.parent.iterdir():
            partition_name += f"/{part.name}"

        # Run repair
        repair(
            catalog_dir,
            tier="archive",
            dry_run=False,
            archive_root=archive_root,
        )

        # Check manifest.json
        manifest_path = archive_root / "manifest.json"
        assert manifest_path.exists(), "Archive manifest.json should exist"

        with open(manifest_path) as f:
            manifest = json.load(f)

        # Find the quarantined entry
        quarantined_entry = None
        for entry in manifest.get("entries", []):
            if entry.get("quarantined") is True:
                quarantined_entry = entry
                break

        assert (
            quarantined_entry is not None
        ), "Manifest should have an entry marked quarantined"
        assert (
            quarantined_entry["partition"] is not None
        ), "Quarantined entry should still have partition field"

    def test_manifest_jsonl_has_sha256_fields(
        self, tmp_path: Path
    ) -> None:
        """Verify manifest.jsonl includes expected_sha256 and actual_sha256."""
        catalog_dir = tmp_path / "catalog"
        archive_root = tmp_path / "archive"
        catalog_dir.mkdir(parents=True)

        # Set up archive
        init_catalog(catalog_dir)
        run = Run(
            project_slug="test",
            command="test",
            argv=["test"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
        )
        write_run(run, catalog_dir)
        compact(catalog_dir)
        archive(catalog_dir, archive_root=archive_root)

        # Corrupt the archive
        partition_path = archive_root / "project=test"
        parquet_files = list(partition_path.glob("year=*/month=*/runs.parquet"))
        bad_file = parquet_files[0]
        bad_file.write_bytes(b"corrupted")

        # Run repair
        repair(
            catalog_dir,
            tier="archive",
            dry_run=False,
            archive_root=archive_root,
        )

        # Check quarantine manifest
        quarantine_manifest = catalog_dir / "quarantine" / "archive" / "manifest.jsonl"
        assert (
            quarantine_manifest.exists()
        ), "Quarantine manifest.jsonl should exist"

        entries = []
        with open(quarantine_manifest) as f:
            for line in f:
                entries.append(json.loads(line))

        assert len(entries) > 0, "Should have at least one quarantine entry"

        entry = entries[0]
        assert (
            "expected_sha256" in entry
        ), "Entry must have expected_sha256 field"
        assert (
            "actual_sha256" in entry
        ), "Entry must have actual_sha256 field"
        assert entry["expected_sha256"] != entry["actual_sha256"], (
            "SHA256 hashes should differ for corrupted file"
        )

    def test_idempotent_rerun_is_noop(self, tmp_path: Path) -> None:
        """Verify re-running repair on already-quarantined partition is idempotent."""
        catalog_dir = tmp_path / "catalog"
        archive_root = tmp_path / "archive"
        catalog_dir.mkdir(parents=True)

        # Set up archive
        init_catalog(catalog_dir)
        run = Run(
            project_slug="test",
            command="test",
            argv=["test"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
        )
        write_run(run, catalog_dir)
        compact(catalog_dir)
        archive(catalog_dir, archive_root=archive_root)

        # Corrupt the archive
        partition_path = archive_root / "project=test"
        parquet_files = list(partition_path.glob("year=*/month=*/runs.parquet"))
        bad_file = parquet_files[0]
        bad_file.write_bytes(b"corrupted")

        # Run repair first time
        manifest1 = repair(
            catalog_dir,
            tier="archive",
            dry_run=False,
            archive_root=archive_root,
        )
        count1 = len([a for a in manifest1.actions if a.action == "quarantine_archive"])

        # Run repair second time
        manifest2 = repair(
            catalog_dir,
            tier="archive",
            dry_run=False,
            archive_root=archive_root,
        )

        # Second run should find no actions (partition already quarantined)
        count2 = len([a for a in manifest2.actions if a.action == "quarantine_archive"])
        assert (
            count2 == 0
        ), f"Second repair run should be idempotent; expected 0 actions, got {count2}"

    def test_valid_partition_untouched(self, tmp_path: Path) -> None:
        """Verify valid partitions are not affected by repair."""
        catalog_dir = tmp_path / "catalog"
        archive_root = tmp_path / "archive"
        catalog_dir.mkdir(parents=True)

        # Set up archive with a valid partition
        init_catalog(catalog_dir)
        run = Run(
            project_slug="valid",
            command="test",
            argv=["test"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
        )
        write_run(run, catalog_dir)
        compact(catalog_dir)
        archive(catalog_dir, archive_root=archive_root)

        # Verify the partition exists and is valid
        partition_path = archive_root / "project=valid"
        parquet_files = list(partition_path.glob("year=*/month=*/runs.parquet"))
        assert len(parquet_files) == 1

        original_content = parquet_files[0].read_bytes()
        original_mtime = parquet_files[0].stat().st_mtime

        # Run repair (no corruption)
        repair(
            catalog_dir,
            tier="archive",
            dry_run=False,
            archive_root=archive_root,
        )

        # Verify the valid partition was not moved
        assert parquet_files[0].exists(), "Valid partition should not be moved"
        assert (
            parquet_files[0].read_bytes() == original_content
        ), "Valid partition content should not change"

    def test_archive_root_parameter_injected_in_tests(
        self, tmp_path: Path
    ) -> None:
        """Verify archive_root parameter is injectable for hermetic tests."""
        catalog_dir = tmp_path / "catalog"
        archive_root = tmp_path / "custom_archive"  # Not ~/.bth/archive
        catalog_dir.mkdir(parents=True)

        # This test should not touch the real ~/.bth/archive
        init_catalog(catalog_dir)
        run = Run(
            project_slug="test",
            command="test",
            argv=["test"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
        )
        write_run(run, catalog_dir)
        compact(catalog_dir)
        archive(catalog_dir, archive_root=archive_root)

        # Corrupt and run repair with custom archive_root
        partition_path = archive_root / "project=test"
        parquet_files = list(partition_path.glob("year=*/month=*/runs.parquet"))
        bad_file = parquet_files[0]
        bad_file.write_bytes(b"corrupted")

        # Repair should use the custom archive_root
        repair(
            catalog_dir,
            tier="archive",
            dry_run=False,
            archive_root=archive_root,
        )

        # Verify quarantine used catalog_dir, not ~/
        quarantine_dir = catalog_dir / "quarantine" / "archive"
        assert (
            quarantine_dir.exists()
        ), "Quarantine should be under catalog_dir, not home"
