from __future__ import annotations

from datetime import datetime, UTC

import pytest

from bathos.schema import Run
from bathos.campaigns import Campaign
from bathos.viz.data import project_run, project_campaign, RunDisplay, CampaignDisplay


class TestProjectRunBasic:
    def test_project_run_basic(self):
        """Test basic projection of Run to RunDisplay."""
        run = Run(
            id="12345678-abcd-efgh-ijkl-mnopqrstuvwx",
            project_slug="test-project",
            command="python scripts/test.py",
            argv=["python", "scripts/test.py"],
            git_hash="abc123def456",
            git_branch="main",
            git_dirty=False,
            timestamp=datetime(2026, 5, 21, 13, 47, 23, tzinfo=UTC),
            duration_s=7.3,
        )

        display = project_run(run)

        assert display["id"] == run.id
        assert display["id_short"] == run.id[:8]
        assert display["duration_display"] == "7.3s"
        assert display["timestamp"] == "2026-05-21 13:47:23 UTC"
        assert display["git_hash_short"] == run.git_hash[:8]
        assert display["campaign_name"] == ""


class TestProjectRunWithPostmortem:
    def test_project_run_with_postmortem(self):
        """Test that postmortem_asset_links is parsed from JSON string."""
        run = Run(
            id="87654321-zyxw-vuts-rqpo-nmlkjihgfedcba",
            project_slug="test-project",
            command="python scripts/test.py",
            argv=["python", "scripts/test.py"],
            git_hash="def456abc123",
            git_branch="develop",
            git_dirty=True,
            postmortem_asset_links='{"image": "/path/to/image.png"}',
        )

        display = project_run(run)

        assert display["postmortem_asset_links"] == {"image": "/path/to/image.png"}


class TestProjectRunDurationDisplay:
    def test_project_run_duration_display_seconds(self):
        """Test duration_display for sub-minute durations."""
        run = Run(
            id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            project_slug="test-project",
            command="python scripts/test.py",
            argv=["python", "scripts/test.py"],
            git_hash="aaa000aaa000",
            git_branch="main",
            git_dirty=False,
            duration_s=45.5,
        )

        display = project_run(run)
        assert display["duration_display"] == "45.5s"

    def test_project_run_duration_display_minutes(self):
        """Test duration_display for multi-minute durations."""
        run = Run(
            id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            project_slug="test-project",
            command="python scripts/test.py",
            argv=["python", "scripts/test.py"],
            git_hash="bbb111bbb111",
            git_branch="main",
            git_dirty=False,
            duration_s=134.0,
        )

        display = project_run(run)
        assert display["duration_display"] == "2m 14s"


class TestProjectCampaign:
    def test_project_campaign(self):
        """Test projection of Campaign to CampaignDisplay."""
        campaign = Campaign(
            id="campaign-id-1234567890",
            project_slug="test-project",
            name="Test Campaign",
            mode="exploration",
            question="Does approach X work?",
            hypothesis="If we do X, we should see Y",
            status="concluded",
            started_at="2026-05-21T10:00:00+00:00",
            concluded_at="2026-05-22T10:00:00+00:00",
            conclusion="Approach X worked",
            outcome_label="success",
            parent_campaign_id=None,
        )

        aggregates = {
            "run_count": 42,
            "outcome_distribution": {"pass": 30, "fail": 10, "unknown": 2},
            "residual_rate": 0.05,
            "bypass_rate": 0.02,
            "unknown_rate": 0.05,
            "anomalies": ["High failure rate"],
        }

        display = project_campaign(campaign, aggregates)

        assert display["id"] == campaign.id
        assert display["id_short"] == campaign.id[:8]
        assert display["name"] == "Test Campaign"
        assert display["mode"] == "exploration"
        assert display["question"] == "Does approach X work?"
        assert display["hypothesis"] == "If we do X, we should see Y"
        assert display["status"] == "concluded"
        assert display["started_at"] == "2026-05-21T10:00:00+00:00"
        assert display["concluded_at"] == "2026-05-22T10:00:00+00:00"
        assert display["conclusion"] == "Approach X worked"
        assert display["outcome_label"] == "success"
        assert display["parent_campaign_id"] == ""
        assert display["run_count"] == 42
        assert display["outcome_distribution"] == {"pass": 30, "fail": 10, "unknown": 2}
        assert display["residual_rate"] == 0.05
        assert display["bypass_rate"] == 0.02
        assert display["unknown_rate"] == 0.05
        assert display["anomalies"] == ["High failure rate"]

    def test_project_campaign_with_parent(self):
        """Test campaign with parent_campaign_id."""
        campaign = Campaign(
            id="campaign-child-123",
            project_slug="test-project",
            name="Child Campaign",
            mode="confirmation",
            parent_campaign_id="campaign-parent-456",
        )

        aggregates = {
            "run_count": 0,
            "outcome_distribution": {},
            "residual_rate": 0.0,
            "bypass_rate": 0.0,
            "unknown_rate": 0.0,
            "anomalies": [],
        }

        display = project_campaign(campaign, aggregates)

        assert display["parent_campaign_id"] == "campaign-parent-456"

    def test_project_campaign_concluded_at_none(self):
        """Test campaign with concluded_at=None."""
        campaign = Campaign(
            id="campaign-open-123",
            project_slug="test-project",
            name="Open Campaign",
            mode="exploration",
            status="open",
            concluded_at=None,
        )

        aggregates = {
            "run_count": 5,
            "outcome_distribution": {"pass": 3, "fail": 2},
            "residual_rate": 0.0,
            "bypass_rate": 0.0,
            "unknown_rate": 0.0,
            "anomalies": [],
        }

        display = project_campaign(campaign, aggregates)

        assert display["concluded_at"] == ""
