"""Blast-radius campaign/claim propagation tests (AC-7, AC-8, Phase 2a, #4552)."""

from __future__ import annotations

import pytest

from bathos.blast_radius import (
    BlastRadiusMatch,
    BlastRadiusReport,
    fold_blast_radius_state,
    propagate_to_campaigns,
)
from bathos.campaigns import add_run_to_campaign, connect_catalog_db, create_campaign
from bathos.catalog import init_catalog, write_run
from bathos.schema import Run


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    init_catalog(cat)
    return cat


def _report(affected=(), unverifiable=()):
    return BlastRadiusReport(
        anchor_kind="commit",
        anchor_value="deadbeef",
        changed_files=["src/foo.py"],
        affected=list(affected),
        unverifiable=list(unverifiable),
        unaffected_run_ids=[],
    )


class TestCampaignPropagation:
    def test_campaign_with_affected_member_gets_flagged(self, catalog_dir):
        run = Run(
            project_slug="p",
            command="foo.py",
            argv=["foo.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
        )
        write_run(run, catalog_dir)
        from bathos.compact import compact as compact_catalog

        compact_catalog(catalog_dir)
        db = connect_catalog_db(catalog_dir, read_only=False)
        campaign = create_campaign(db, "camp-1", "p", "exploration", catalog_dir=catalog_dir)
        add_run_to_campaign(db, campaign.id, run.id, catalog_dir=catalog_dir)
        db.close()

        match = BlastRadiusMatch(
            run_id=run.id,
            git_hash="abc",
            command="foo.py",
            campaign_id=campaign.id,
            matched_files=["src/foo.py"],
            reason="r",
        )
        records = propagate_to_campaigns(_report(affected=[match]), catalog_dir)

        assert len(records) == 1
        assert records[0].entity_type == "campaign"
        assert records[0].entity_id == campaign.id
        assert records[0].to_state == "affected"
        assert fold_blast_radius_state(catalog_dir, "campaign", campaign.id) == "affected"

    def test_campaign_with_only_unverifiable_members_gets_unverifiable_state(self, catalog_dir):
        match = BlastRadiusMatch(
            run_id="run-x",
            git_hash="abc",
            command="foo.py",
            campaign_id="camp-2",
            matched_files=["x"],
            reason="r",
        )
        records = propagate_to_campaigns(_report(unverifiable=[match]), catalog_dir)
        assert records[0].to_state == "unverifiable"

    def test_mixed_affected_and_unverifiable_takes_affected(self, catalog_dir):
        a = BlastRadiusMatch(
            run_id="run-a", git_hash="abc", command="a", campaign_id="camp-3",
            matched_files=["a"], reason="r",
        )
        u = BlastRadiusMatch(
            run_id="run-u", git_hash="abc", command="u", campaign_id="camp-3",
            matched_files=["u"], reason="r",
        )
        records = propagate_to_campaigns(_report(affected=[a], unverifiable=[u]), catalog_dir)
        assert len(records) == 1  # one campaign, one record
        assert records[0].to_state == "affected"  # more severe wins

    def test_matches_with_no_campaign_id_are_ignored(self, catalog_dir):
        match = BlastRadiusMatch(
            run_id="run-y", git_hash="abc", command="y", campaign_id="",
            matched_files=["y"], reason="r",
        )
        records = propagate_to_campaigns(_report(affected=[match]), catalog_dir)
        assert records == []
