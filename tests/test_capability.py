"""Tests for the B2-06 capability probe (#2181, AC-20)."""

from pathlib import Path

import duckdb
import pytest

from bathos.capability import (
    SEED_COLUMNS,
    check_seed_live,
    check_stats_battery_live,
    probe_capabilities,
)
from bathos.catalog import init_catalog, write_run
from bathos.compact import compact
from bathos.schema import Run


class TestCheckSeedLive:
    def test_no_warm_db_is_not_live(self, tmp_catalog: Path):
        live, missing = check_seed_live(tmp_catalog)
        assert live is False
        assert missing == SEED_COLUMNS

    def test_freshly_compacted_catalog_is_live(self, tmp_catalog: Path, sample_run: Run):
        init_catalog(tmp_catalog)
        write_run(sample_run, tmp_catalog)
        compact(tmp_catalog)

        live, missing = check_seed_live(tmp_catalog)
        assert live is True
        assert missing == ()

    def test_warm_db_missing_seed_columns_is_not_live(self, tmp_catalog: Path):
        # Simulate a pre-B2-02 warm DB: a runs table that exists but predates the seed
        # columns entirely (no ALTER TABLE has ever run against it).
        db_path = tmp_catalog / "bathos.db"
        con = duckdb.connect(str(db_path))
        try:
            con.execute("CREATE TABLE runs (id TEXT, project_slug TEXT)")
        finally:
            con.close()

        live, missing = check_seed_live(tmp_catalog)
        assert live is False
        assert set(missing) == set(SEED_COLUMNS)

    def test_partial_migration_reports_exactly_missing_columns(self, tmp_catalog: Path):
        # A hypothetical partially-migrated DB (seed added, baseline_hpo_* not yet) --
        # exercises that `missing` names exactly the absent columns, not all-or-nothing.
        db_path = tmp_catalog / "bathos.db"
        con = duckdb.connect(str(db_path))
        try:
            con.execute("CREATE TABLE runs (id TEXT, seed BIGINT)")
        finally:
            con.close()

        live, missing = check_seed_live(tmp_catalog)
        assert live is False
        assert set(missing) == {"baseline_hpo_trials", "baseline_hpo_compute_budget"}


class TestCheckStatsBatteryLive:
    def test_scipy_installed_is_live(self):
        pytest.importorskip("scipy")
        live, reason = check_stats_battery_live()
        assert live is True
        assert reason == ""


class TestProbeCapabilities:
    def test_full_report_on_compacted_catalog(self, tmp_catalog: Path, sample_run: Run):
        pytest.importorskip("scipy")
        init_catalog(tmp_catalog)
        write_run(sample_run, tmp_catalog)
        compact(tmp_catalog)

        report = probe_capabilities(tmp_catalog)
        assert report.seed_live is True
        assert report.missing_seed_columns == ()
        assert report.stats_battery_live is True
        assert report.stats_unavailable_reason == ""

    def test_report_on_empty_catalog(self, tmp_catalog: Path):
        report = probe_capabilities(tmp_catalog)
        assert report.seed_live is False
        assert report.missing_seed_columns == SEED_COLUMNS
