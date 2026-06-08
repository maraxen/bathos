"""Tests for bathos repair module (sentinel cleanup, quarantine, dry-run guards).

Tests cover:
- GWT-3 scenario: .tmp.parquet orphan, stale .bak, fresh .bak
- Dry-run safety (no mutations, manifests returned)
- Fresh file skip behavior (< 60s mtime) in warnings
- Quarantine directory creation and manifest generation
- Warning list population for skipped files
"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bathos.repair import scan, repair, RepairAction, RepairManifest


class TestScanAndRepairBasics:
    """Test scan() and repair() basic structure."""

    def test_scan_returns_tuple(self, tmp_catalog: Path):
        """scan() returns (actions, warnings) tuple."""
        actions, warnings = scan(tmp_catalog, tier="cool")
        assert isinstance(actions, list)
        assert isinstance(warnings, list)

    def test_repair_manifest_structure(self, tmp_catalog: Path):
        """repair() returns RepairManifest with all required fields."""
        manifest = repair(tmp_catalog, tier="cool", dry_run=True)
        assert isinstance(manifest, RepairManifest)
        assert hasattr(manifest, "run_ts")
        assert hasattr(manifest, "catalog_dir")
        assert hasattr(manifest, "dry_run")
        assert hasattr(manifest, "tier")
        assert hasattr(manifest, "actions")
        assert hasattr(manifest, "warnings")
        assert manifest.dry_run is True

    def test_manifest_warnings_is_list(self, tmp_catalog: Path):
        """manifest.warnings is always a list."""
        manifest = repair(tmp_catalog, tier="cool", dry_run=True)
        assert isinstance(manifest.warnings, list)


class TestGWT3Scenario:
    """GWT-3 check 6: Fresh .bak files (mtime < 60s) skip with warning, not action."""

    def test_old_tmp_deleted_stale_bak_quarantined_fresh_bak_skipped(self, tmp_catalog: Path):
        """Core GWT-3 scenario: old .tmp → delete, stale .bak → quarantine, fresh .bak → skip with warning."""
        # Setup: one old .tmp.parquet, one stale .bak (mtime > 120s), one fresh .bak (mtime < 30s)
        runs_dir = tmp_catalog / "runs" / "test_slug"
        runs_dir.mkdir(parents=True, exist_ok=True)

        old_tmp = runs_dir / "run_old.tmp.parquet"
        old_tmp.write_text("dummy")
        # Manually set mtime to 150s ago
        old_mtime = (datetime.now(UTC).timestamp() - 150)
        old_tmp.touch()
        import os
        os.utime(old_tmp, (old_mtime, old_mtime))

        stale_bak = runs_dir / "run_stale.bak"
        stale_bak.write_text("dummy")
        # Manually set mtime to 120s ago
        stale_mtime = (datetime.now(UTC).timestamp() - 120)
        os.utime(stale_bak, (stale_mtime, stale_mtime))

        fresh_bak = runs_dir / "run_fresh.bak"
        fresh_bak.write_text("dummy")
        # Manually set mtime to 15s ago
        fresh_mtime = (datetime.now(UTC).timestamp() - 15)
        os.utime(fresh_bak, (fresh_mtime, fresh_mtime))

        # Scan in dry-run mode
        actions, scan_warnings = scan(tmp_catalog, tier="cool")

        # Filter actions by type
        delete_tmp_actions = [a for a in actions if a.action == "delete_tmp"]
        quarantine_bak_actions = [a for a in actions if a.action == "quarantine_bak"]

        # Assertions on actions
        assert len(delete_tmp_actions) == 1, f"Expected 1 delete_tmp action, got {len(delete_tmp_actions)}"
        assert len(quarantine_bak_actions) == 1, f"Expected 1 quarantine_bak action, got {len(quarantine_bak_actions)}"
        assert str(old_tmp) in delete_tmp_actions[0].path

        # Assertions on warnings
        assert len(scan_warnings) >= 1, f"Expected at least 1 warning (fresh .bak skip), got {len(scan_warnings)}"
        fresh_skip_warnings = [w for w in scan_warnings if "fresh" in w.lower() or "in-flight" in w.lower()]
        assert len(fresh_skip_warnings) >= 1, f"Expected fresh .bak skip in warnings, got: {scan_warnings}"

        # Verify no skip_bak_young action exists (it should be warning-only)
        skip_actions = [a for a in actions if a.action.startswith("skip_")]
        assert len(skip_actions) == 0, f"Expected no skip_* actions, but found {skip_actions}"

    def test_dry_run_true_returns_no_mutations(self, tmp_catalog: Path):
        """dry_run=True produces manifest but no filesystem mutations."""
        runs_dir = tmp_catalog / "runs" / "slug"
        runs_dir.mkdir(parents=True, exist_ok=True)

        tmp_file = runs_dir / "run.tmp.parquet"
        tmp_file.write_text("data")
        # Set old mtime
        import os
        old_mtime = datetime.now(UTC).timestamp() - 100
        os.utime(tmp_file, (old_mtime, old_mtime))

        # Run dry_run repair
        manifest = repair(tmp_catalog, tier="cool", dry_run=True)

        # Assertions
        assert manifest.dry_run is True
        assert tmp_file.exists(), ".tmp.parquet should still exist after dry_run"
        assert len(manifest.actions) >= 1, "manifest.actions should identify the .tmp file"

    def test_dry_run_false_actually_deletes(self, tmp_catalog: Path):
        """dry_run=False actually deletes orphaned .tmp files."""
        runs_dir = tmp_catalog / "runs" / "slug"
        runs_dir.mkdir(parents=True, exist_ok=True)

        tmp_file = runs_dir / "run.tmp.parquet"
        tmp_file.write_text("data")
        # Set old mtime
        import os
        old_mtime = datetime.now(UTC).timestamp() - 100
        os.utime(tmp_file, (old_mtime, old_mtime))

        # Run actual repair
        manifest = repair(tmp_catalog, tier="cool", dry_run=False)

        # Assertions
        assert manifest.dry_run is False
        assert not tmp_file.exists(), ".tmp.parquet should be deleted"
        assert len(manifest.actions) >= 1

    def test_fresh_tmp_skipped_with_warning(self, tmp_catalog: Path):
        """Fresh .tmp files (mtime < 60s) skip with warning, not action."""
        runs_dir = tmp_catalog / "runs" / "slug"
        runs_dir.mkdir(parents=True, exist_ok=True)

        fresh_tmp = runs_dir / "run.tmp.parquet"
        fresh_tmp.write_text("data")
        # Set fresh mtime (15s ago)
        import os
        fresh_mtime = datetime.now(UTC).timestamp() - 15
        os.utime(fresh_tmp, (fresh_mtime, fresh_mtime))

        # Scan
        actions, scan_warnings = scan(tmp_catalog, tier="cool")

        # Assertions
        tmp_actions = [a for a in actions if "tmp" in a.action.lower()]
        assert len(tmp_actions) == 0, f"Fresh .tmp should not create action, but got {tmp_actions}"

        tmp_warnings = [w for w in scan_warnings if "tmp" in w.lower()]
        assert len(tmp_warnings) >= 1, f"Fresh .tmp should appear in warnings, got {scan_warnings}"


class TestQuarantineManifest:
    """Test quarantine manifest generation."""

    def test_quarantine_manifest_created(self, tmp_catalog: Path):
        """Quarantine operation creates manifest.jsonl with correct fields."""
        runs_dir = tmp_catalog / "runs" / "slug"
        runs_dir.mkdir(parents=True, exist_ok=True)

        bak_file = runs_dir / "run.bak"
        bak_file.write_text("backup_data")
        # Set stale mtime
        import os
        stale_mtime = datetime.now(UTC).timestamp() - 120
        os.utime(bak_file, (stale_mtime, stale_mtime))

        # Execute repair
        manifest = repair(tmp_catalog, tier="cool", dry_run=False)

        # Check quarantine directory exists
        quarantine_dir = tmp_catalog / "quarantine" / "slug"
        assert quarantine_dir.exists(), "quarantine/<slug> directory should exist"

        # Check manifest.jsonl exists
        manifest_file = quarantine_dir / "manifest.jsonl"
        assert manifest_file.exists(), "manifest.jsonl should exist"

        # Read and validate manifest entries
        with open(manifest_file) as f:
            lines = f.readlines()
            assert len(lines) >= 1, "manifest.jsonl should have at least one entry"

            for line in lines:
                entry = json.loads(line.strip())
                # Verify all 8 required fields
                required_fields = {"ts", "tier", "action", "original_path", "moved_to", "mtime_s", "size_bytes", "slug"}
                assert set(entry.keys()) == required_fields, f"Missing or extra fields in manifest entry: {entry.keys()}"
                assert entry["tier"] == "cool"
                assert entry["action"] == "quarantine_bak"
                assert entry["slug"] == "slug"

    def test_quarantine_directory_timestamped(self, tmp_catalog: Path):
        """Quarantine files are timestamped: YYMMDD_HHMMSS_<basename>."""
        runs_dir = tmp_catalog / "runs" / "slug"
        runs_dir.mkdir(parents=True, exist_ok=True)

        bak_file = runs_dir / "run.bak"
        bak_file.write_text("data")
        # Set stale mtime
        import os
        stale_mtime = datetime.now(UTC).timestamp() - 120
        os.utime(bak_file, (stale_mtime, stale_mtime))

        # Execute repair
        repair(tmp_catalog, tier="cool", dry_run=False)

        # Check quarantine directory
        quarantine_dir = tmp_catalog / "quarantine" / "slug"
        quarantined_files = list(quarantine_dir.glob("*"))
        # Should have manifest.jsonl + quarantined .bak file
        quarantined_bak_files = [f for f in quarantined_files if f.name != "manifest.jsonl"]
        assert len(quarantined_bak_files) >= 1, "Quarantined .bak file should exist"

        # Verify filename format: YYMMDD_HHMMSS_run.bak
        quarantined_name = quarantined_bak_files[0].name
        parts = quarantined_name.split("_")
        assert len(parts) >= 3, f"Quarantined name should have format YYMMDD_HHMMSS_..., got {quarantined_name}"


class TestRepairActionFields:
    """Test RepairAction structure and dry_run field."""

    def test_repair_action_has_required_fields(self, tmp_catalog: Path):
        """RepairAction has action, path, detail, dry_run fields."""
        runs_dir = tmp_catalog / "runs" / "slug"
        runs_dir.mkdir(parents=True, exist_ok=True)

        tmp_file = runs_dir / "run.tmp.parquet"
        tmp_file.write_text("data")
        # Set old mtime
        import os
        old_mtime = datetime.now(UTC).timestamp() - 100
        os.utime(tmp_file, (old_mtime, old_mtime))

        # Scan
        actions, _ = scan(tmp_catalog, tier="cool")

        # All actions should have required fields
        for action in actions:
            assert hasattr(action, "action")
            assert hasattr(action, "path")
            assert hasattr(action, "detail")
            assert hasattr(action, "dry_run")
            assert action.dry_run is True  # scan() always sets dry_run=True

    def test_manifest_actions_all_have_dry_run_true_in_scan(self, tmp_catalog: Path):
        """All actions from scan() have dry_run=True."""
        runs_dir = tmp_catalog / "runs" / "slug"
        runs_dir.mkdir(parents=True, exist_ok=True)

        tmp_file = runs_dir / "run.tmp.parquet"
        tmp_file.write_text("data")
        # Set old mtime
        import os
        old_mtime = datetime.now(UTC).timestamp() - 100
        os.utime(tmp_file, (old_mtime, old_mtime))

        # Scan
        actions, _ = scan(tmp_catalog, tier="cool")

        # Verify all have dry_run=True
        for action in actions:
            assert action.dry_run is True, f"Action {action.action} should have dry_run=True"


class TestRepairTiers:
    """Test repair behavior for different tiers."""

    def test_repair_cool_tier_only(self, tmp_catalog: Path):
        """tier='cool' only processes cool-tier issues."""
        runs_dir = tmp_catalog / "runs" / "slug"
        runs_dir.mkdir(parents=True, exist_ok=True)

        tmp_file = runs_dir / "run.tmp.parquet"
        tmp_file.write_text("data")
        # Set old mtime
        import os
        old_mtime = datetime.now(UTC).timestamp() - 100
        os.utime(tmp_file, (old_mtime, old_mtime))

        # Repair with tier='cool'
        manifest = repair(tmp_catalog, tier="cool", dry_run=True)

        assert manifest.tier == "cool"
        # Should find the .tmp file
        tmp_actions = [a for a in manifest.actions if a.action == "delete_tmp"]
        assert len(tmp_actions) >= 1, "Should find old .tmp file in cool tier"

    def test_repair_all_tier(self, tmp_catalog: Path):
        """tier='all' processes all tiers."""
        runs_dir = tmp_catalog / "runs" / "slug"
        runs_dir.mkdir(parents=True, exist_ok=True)

        tmp_file = runs_dir / "run.tmp.parquet"
        tmp_file.write_text("data")
        # Set old mtime
        import os
        old_mtime = datetime.now(UTC).timestamp() - 100
        os.utime(tmp_file, (old_mtime, old_mtime))

        # Repair with tier='all'
        manifest = repair(tmp_catalog, tier="all", dry_run=True)

        assert manifest.tier == "all"
        # Should find the .tmp file
        tmp_actions = [a for a in manifest.actions if a.action == "delete_tmp"]
        assert len(tmp_actions) >= 1


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_empty_catalog_no_errors(self, tmp_catalog: Path):
        """Empty catalog produces no actions or errors."""
        actions, warnings = scan(tmp_catalog, tier="cool")

        assert isinstance(actions, list)
        assert isinstance(warnings, list)
        assert len(actions) == 0

    def test_missing_runs_dir_no_errors(self, tmp_catalog: Path):
        """Missing runs/ directory doesn't crash."""
        actions, warnings = scan(tmp_catalog, tier="cool")

        assert isinstance(actions, list)
        assert isinstance(warnings, list)

    def test_stale_bak_quarantined_once(self, tmp_catalog: Path):
        """Multiple stale .bak files each quarantined separately."""
        runs_dir = tmp_catalog / "runs" / "slug"
        runs_dir.mkdir(parents=True, exist_ok=True)

        bak1 = runs_dir / "run1.bak"
        bak1.write_text("data1")
        bak2 = runs_dir / "run2.bak"
        bak2.write_text("data2")

        # Set stale mtime for both
        import os
        stale_mtime = datetime.now(UTC).timestamp() - 120
        os.utime(bak1, (stale_mtime, stale_mtime))
        os.utime(bak2, (stale_mtime, stale_mtime))

        # Execute repair
        manifest = repair(tmp_catalog, tier="cool", dry_run=False)

        # Check manifest entries
        manifest_file = tmp_catalog / "quarantine" / "slug" / "manifest.jsonl"
        assert manifest_file.exists()

        with open(manifest_file) as f:
            entries = [json.loads(line) for line in f]
            assert len(entries) == 2, f"Expected 2 quarantine entries, got {len(entries)}"
