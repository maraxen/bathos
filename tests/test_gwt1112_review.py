"""Tests for GWT-11/12: warm-loss gate and --acknowledge-warm-loss flag.

GWT-11: Warm-loss gate fires when bathos.db has postmortem annotations or output_metadata.
GWT-12: Warm-loss gate skips confirmation when warm-only data count is zero (safe rebuild).

Spec criteria:
- repair() with rebuild_warm action and acknowledge_warm_loss=False should:
  - Query bathos.db for postmortem_status != 'unassigned' count
  - Query bathos.db for output_metadata IS NOT NULL AND != '[]' count
  - Only raise SystemExit(1) if either count > 0
  - When both counts == 0, proceed silently with info log message
  - When counts > 0, print concrete counts and require --acknowledge-warm-loss

This spec prevents over-conservative warnings when there is no warm-only data to lose.
"""

from pathlib import Path

import pytest

from bathos.catalog import init_catalog, write_run
from bathos.repair import repair
from bathos.schema import Run


def _create_sample_run(catalog_dir: Path) -> Run:
    """Create and write a minimal run to cool tier."""
    run = Run(
        project_slug="test_project",
        command="echo test",
        argv=["echo", "test"],
        git_hash="abc123def456",
        git_branch="main",
        git_dirty=False,
    )
    init_catalog(catalog_dir)
    write_run(run, catalog_dir)
    return run


def _mock_rebuild_warm_action():
    """Manually inject a rebuild_warm action by mocking the scan logic."""
    from unittest.mock import patch

    from bathos.repair import RepairAction

    def mock_scan(cd, _tier, from_warm=False):
        # Return a fake rebuild_warm action
        return [
            RepairAction(
                action="rebuild_warm",
                path=str(cd / "bathos.db"),
                detail="Mocked rebuild_warm for testing",
            )
        ], []

    return patch("bathos.repair.scan", side_effect=mock_scan)


class TestGWT11WarmLossGate:
    """Test suite for warm-loss gate (GWT-11/12 checks)."""

    def test_rebuild_warm_with_zero_postmortem_and_metadata_proceeds_silently(
        self, tmp_catalog: Path, caplog
    ):
        """Check 1: When both counts are zero, repair proceeds without raising SystemExit."""
        import logging

        import duckdb

        caplog.set_level(logging.INFO)

        _create_sample_run(tmp_catalog)

        # Create an empty warm DB with the correct schema (no warm-only data)
        db_path = tmp_catalog / "bathos.db"
        con = duckdb.connect(str(db_path))
        try:
            con.execute(
                """
                CREATE TABLE runs (
                    id VARCHAR,
                    project_slug VARCHAR,
                    command VARCHAR,
                    status VARCHAR,
                    postmortem_status VARCHAR DEFAULT 'unassigned',
                    output_metadata TEXT
                )
                """
            )
            con.commit()
        finally:
            con.close()

        # Mock the scan to return rebuild_warm action
        with _mock_rebuild_warm_action():
            # repair() should NOT raise SystemExit because warm-only data counts are zero
            manifest = repair(
                tmp_catalog,
                tier="warm",
                dry_run=True,
                acknowledge_warm_loss=False,
            )

        # Should have planned the rebuild
        rebuild_actions = [a for a in manifest.actions if a.action == "rebuild_warm"]
        assert len(rebuild_actions) > 0, "rebuild_warm action should be present"

        # Should NOT raise SystemExit; info log should be present instead
        assert "will not lose any warm-only data" in caplog.text

    def test_rebuild_warm_with_postmortem_data_requires_acknowledgment(
        self, tmp_catalog: Path, capsys
    ):
        """Check 2: When postmortem_count > 0, repair raises SystemExit(1) unless --acknowledge."""
        import duckdb

        run = _create_sample_run(tmp_catalog)

        # Create a DuckDB with postmortem data
        db_path = tmp_catalog / "bathos.db"
        con = duckdb.connect(str(db_path))
        try:
            con.execute(
                """
                CREATE TABLE runs (
                    id VARCHAR,
                    project_slug VARCHAR,
                    command VARCHAR,
                    status VARCHAR,
                    postmortem_status VARCHAR DEFAULT 'unassigned',
                    output_metadata TEXT
                )
                """
            )
            # Insert a run with postmortem status != 'unassigned'
            con.execute(
                "INSERT INTO runs (id, project_slug, command, status, postmortem_status, output_metadata) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    run.id,
                    run.project_slug,
                    run.command,
                    run.status,
                    "assigned",
                    None,
                ],
            )
            con.commit()
        finally:
            con.close()

        # Mock the scan to return rebuild_warm action
        with _mock_rebuild_warm_action():
            # repair() should raise SystemExit because postmortem_count > 0
            with pytest.raises(SystemExit) as exc_info:
                repair(
                    tmp_catalog,
                    tier="warm",
                    dry_run=True,
                    acknowledge_warm_loss=False,
                )
            assert exc_info.value.code == 1

        # Check stderr for warning message
        captured = capsys.readouterr()
        assert "1 postmortem annotation(s)" in captured.err

    def test_rebuild_warm_with_output_metadata_requires_acknowledgment(
        self, tmp_catalog: Path, capsys
    ):
        """Check 3: When output_metadata_count > 0, repair raises SystemExit(1) unless --acknowledge."""
        import duckdb

        run = _create_sample_run(tmp_catalog)

        # Create a DuckDB with output_metadata
        db_path = tmp_catalog / "bathos.db"
        con = duckdb.connect(str(db_path))
        try:
            con.execute(
                """
                CREATE TABLE runs (
                    id VARCHAR,
                    project_slug VARCHAR,
                    command VARCHAR,
                    status VARCHAR,
                    postmortem_status VARCHAR DEFAULT 'unassigned',
                    output_metadata TEXT
                )
                """
            )
            # Insert a run with non-empty output_metadata
            con.execute(
                "INSERT INTO runs (id, project_slug, command, status, postmortem_status, output_metadata) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    run.id,
                    run.project_slug,
                    run.command,
                    run.status,
                    "unassigned",
                    '[{"path": "/tmp/out.txt", "size": 1024}]',
                ],
            )
            con.commit()
        finally:
            con.close()

        # Mock the scan to return rebuild_warm action
        with _mock_rebuild_warm_action():
            # repair() should raise SystemExit because output_metadata_count > 0
            with pytest.raises(SystemExit) as exc_info:
                repair(
                    tmp_catalog,
                    tier="warm",
                    dry_run=True,
                    acknowledge_warm_loss=False,
                )
            assert exc_info.value.code == 1

        # Check stderr for warning message
        captured = capsys.readouterr()
        assert "1 output_metadata entry(ies)" in captured.err

    def test_rebuild_warm_with_acknowledge_flag_proceeds(self, tmp_catalog: Path):
        """Check 4: When --acknowledge-warm-loss is True, repair proceeds even with warm-only data."""
        import duckdb

        run = _create_sample_run(tmp_catalog)

        # Create a DuckDB with postmortem data
        db_path = tmp_catalog / "bathos.db"
        con = duckdb.connect(str(db_path))
        try:
            con.execute(
                """
                CREATE TABLE runs (
                    id VARCHAR,
                    project_slug VARCHAR,
                    command VARCHAR,
                    status VARCHAR,
                    postmortem_status VARCHAR DEFAULT 'unassigned',
                    output_metadata TEXT
                )
                """
            )
            con.execute(
                "INSERT INTO runs (id, project_slug, command, status, postmortem_status, output_metadata) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    run.id,
                    run.project_slug,
                    run.command,
                    run.status,
                    "assigned",
                    None,
                ],
            )
            con.commit()
        finally:
            con.close()

        # Mock the scan to return rebuild_warm action
        with _mock_rebuild_warm_action():
            # With acknowledge_warm_loss=True, should NOT raise SystemExit
            manifest = repair(
                tmp_catalog,
                tier="warm",
                dry_run=True,
                acknowledge_warm_loss=True,
            )

        # Should have planned the rebuild
        rebuild_actions = [a for a in manifest.actions if a.action == "rebuild_warm"]
        assert len(rebuild_actions) > 0

    def test_rebuild_warm_prints_concrete_warning_with_counts(
        self, tmp_catalog: Path, capsys
    ):
        """Check 5: Warning message includes concrete counts of warm-only data at risk."""
        import duckdb

        run = _create_sample_run(tmp_catalog)

        # Create a DuckDB with postmortem data
        db_path = tmp_catalog / "bathos.db"
        con = duckdb.connect(str(db_path))
        try:
            con.execute(
                """
                CREATE TABLE runs (
                    id VARCHAR,
                    project_slug VARCHAR,
                    command VARCHAR,
                    status VARCHAR,
                    postmortem_status VARCHAR DEFAULT 'unassigned',
                    output_metadata TEXT
                )
                """
            )
            con.execute(
                "INSERT INTO runs (id, project_slug, command, status, postmortem_status, output_metadata) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    run.id,
                    run.project_slug,
                    run.command,
                    run.status,
                    "assigned",
                    None,
                ],
            )
            con.commit()
        finally:
            con.close()

        # Mock the scan to return rebuild_warm action
        with _mock_rebuild_warm_action(), pytest.raises(SystemExit):
            # Capture stderr
            repair(
                tmp_catalog,
                tier="warm",
                dry_run=True,
                acknowledge_warm_loss=False,
            )

        captured = capsys.readouterr()
        # Check that warning was printed to stderr with counts
        assert "1 postmortem annotation(s)" in captured.err
        assert "--acknowledge-warm-loss" in captured.err

    def test_rebuild_warm_with_both_postmortem_and_metadata(self, tmp_catalog: Path):
        """Check 6: When both postmortem and metadata are present, gate fires with total count."""
        import duckdb

        run = _create_sample_run(tmp_catalog)

        # Create a DuckDB with both postmortem and output_metadata
        db_path = tmp_catalog / "bathos.db"
        con = duckdb.connect(str(db_path))
        try:
            con.execute(
                """
                CREATE TABLE runs (
                    id VARCHAR,
                    project_slug VARCHAR,
                    command VARCHAR,
                    status VARCHAR,
                    postmortem_status VARCHAR DEFAULT 'unassigned',
                    output_metadata TEXT
                )
                """
            )
            con.execute(
                "INSERT INTO runs (id, project_slug, command, status, postmortem_status, output_metadata) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    run.id,
                    run.project_slug,
                    run.command,
                    run.status,
                    "assigned",
                    '[{"path": "/tmp/out.txt"}]',
                ],
            )
            con.commit()
        finally:
            con.close()

        # Mock the scan to return rebuild_warm action
        with _mock_rebuild_warm_action():
            # Should raise SystemExit because both counts > 0
            with pytest.raises(SystemExit) as exc_info:
                repair(
                    tmp_catalog,
                    tier="warm",
                    dry_run=True,
                    acknowledge_warm_loss=False,
                )
            assert exc_info.value.code == 1
