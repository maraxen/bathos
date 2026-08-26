"""Blast-radius campaign/claim propagation tests (AC-7, AC-8, Phase 2a, #4552)."""

from __future__ import annotations

import json

import pytest

from bathos.blast_radius import (
    BlastRadiusMatch,
    BlastRadiusReport,
    fold_blast_radius_state,
    propagate_to_campaigns,
    propagate_to_claims,
)
from bathos.campaigns import add_run_to_campaign, connect_catalog_db, create_campaign
from bathos.catalog import init_catalog, write_run
from bathos.claim import register_claim
from bathos.compact import compact as compact_catalog
from bathos.schema import Run

_MINIMAL_CLAIM_TOML = """[claim]
headline = "Test claim"
kill_condition = "Outcome != expected"
kill_condition_satisfiable_by_null = false
regime = "param=1.0..2.0"

[[hypotheses]]
id = "H_primary"
label = "Primary hypothesis"
predicted_signature = "metric=100"

[[hypotheses]]
id = "H_null"
label = "Null hypothesis"
predicted_signature = "metric=50"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "main"
predicted_outcome = "discriminates"

[claim.union_gate]
[[claim.union_gate.clauses]]
id = "clause-1"
description = "Main clause"
hypothesis_ids = ["H_primary"]

[[claim.union_gate.clauses]]
id = "clause-2"
description = "Secondary clause"
hypothesis_ids = ["H_null"]
"""


def _campaign_with_claim(tmp_path, catalog_dir, run):
    """Scaffold+register a minimal 2-clause claim against a fresh campaign
    containing `run`. Returns (campaign_id, db) -- caller must close db."""
    compact_catalog(catalog_dir)
    db = connect_catalog_db(catalog_dir, read_only=False)
    campaign = create_campaign(db, "camp-claim", "p", "exploration", catalog_dir=catalog_dir)
    add_run_to_campaign(db, campaign.id, run.id, catalog_dir=catalog_dir)

    claim_path = tmp_path / "test.claim.toml"
    claim_path.write_text(_MINIMAL_CLAIM_TOML)
    register_claim(claim_path, campaign.id, db, tmp_path, catalog_dir=catalog_dir)
    db.commit()
    return campaign.id, db


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


class TestClaimPropagation:
    def test_claim_gets_flagged_with_implicated_clause(self, catalog_dir, tmp_path):
        run = Run(
            project_slug="p", command="x", argv=["x"], git_hash="abc", git_branch="main",
            git_dirty=False, claim_discriminates=json.dumps(["H_primary"]),
        )
        write_run(run, catalog_dir)
        campaign_id, db = _campaign_with_claim(tmp_path, catalog_dir, run)
        db.close()

        match = BlastRadiusMatch(
            run_id=run.id, git_hash="abc", command="x", campaign_id=campaign_id,
            matched_files=["x"], reason="r",
        )
        records = propagate_to_claims(_report(affected=[match]), catalog_dir, workspace_root=tmp_path)

        assert len(records) == 1
        assert records[0].entity_type == "claim"
        assert records[0].entity_id == campaign_id
        assert json.loads(records[0].matched_clauses) == ["clause-1"]
        assert fold_blast_radius_state(catalog_dir, "claim", campaign_id) == "affected"

    def test_campaign_with_no_registered_claim_produces_no_claim_record(self, catalog_dir):
        run = Run(
            project_slug="p", command="x", argv=["x"], git_hash="abc", git_branch="main",
            git_dirty=False,
        )
        write_run(run, catalog_dir)
        compact_catalog(catalog_dir)
        db = connect_catalog_db(catalog_dir, read_only=False)
        campaign = create_campaign(db, "camp-noclaim", "p", "exploration", catalog_dir=catalog_dir)
        add_run_to_campaign(db, campaign.id, run.id, catalog_dir=catalog_dir)
        db.close()

        match = BlastRadiusMatch(
            run_id=run.id, git_hash="abc", command="x", campaign_id=campaign.id,
            matched_files=["x"], reason="r",
        )
        records = propagate_to_claims(_report(affected=[match]), catalog_dir)
        assert records == []

    def test_clause_not_backed_by_any_affected_run_is_not_implicated(self, catalog_dir, tmp_path):
        run = Run(
            project_slug="p", command="x", argv=["x"], git_hash="abc", git_branch="main",
            git_dirty=False, claim_discriminates=json.dumps(["H_primary"]),
        )
        write_run(run, catalog_dir)
        campaign_id, db = _campaign_with_claim(tmp_path, catalog_dir, run)
        db.close()

        # This run backs only clause-1 (hypothesis_ids=["H_primary"]) -- clause-2
        # (hypothesis_ids=["H_null"]) has no covering run at all, affected or not.
        match = BlastRadiusMatch(
            run_id=run.id, git_hash="abc", command="x", campaign_id=campaign_id,
            matched_files=["x"], reason="r",
        )
        records = propagate_to_claims(_report(affected=[match]), catalog_dir, workspace_root=tmp_path)

        assert len(records) == 1
        assert json.loads(records[0].matched_clauses) == ["clause-1"]
        assert "clause-2" not in json.loads(records[0].matched_clauses)
