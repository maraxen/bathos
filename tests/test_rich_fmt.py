import io
from datetime import UTC, datetime

import pytest
from rich.console import Console

from bathos.campaigns import Campaign
from bathos.rich_fmt import (
    render_campaign_review,
    render_campaign_table,
    render_run_detail,
    render_runs_table,
)
from bathos.schema import Run


@pytest.fixture
def sample_run():
    """Create a minimal Run for testing."""
    return Run(
        project_slug="test_proj",
        command="python script.py",
        argv=["python", "script.py"],
        git_hash="abc123def456",
        git_branch="main",
        git_dirty=False,
        id="run-001",
        timestamp=datetime(2026, 5, 22, 10, 30, 0, tzinfo=UTC),
        duration_s=45.3,
        status="completed",
        exit_code=0,
        outcome="pass",
        tags=["test", "smoke"],
        campaign_id="camp-001",
        postmortem_status="unassigned",
    )


@pytest.fixture
def sample_campaign():
    """Create a minimal Campaign for testing."""
    return Campaign(
        id="camp-001",
        project_slug="test_proj",
        name="Stability Test",
        mode="exploration",
        question="Does NVT maintain temp?",
    )


def test_render_runs_table_basic(sample_run):
    """Test render_runs_table with one run."""
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=200)

    render_runs_table([sample_run], console=console)

    result = output.getvalue()
    assert "Runs" in result
    # Rich truncates wide tables with ellipsis; assert stable cell values, not headers.
    assert "run-001" in result
    assert "45.3s" in result
    assert "pass" in result
    assert "main" in result


def test_render_runs_table_empty():
    """Test render_runs_table with empty list."""
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=200)

    render_runs_table([], console=console)

    result = output.getvalue()
    assert len(result) > 0


def test_render_run_detail_with_postmortem(sample_run):
    """Test render_run_detail with postmortem assigned."""
    sample_run.postmortem_status = "assigned"
    sample_run.postmortem_author = "alice"
    sample_run.postmortem_summary = "All checks passed"

    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=200)

    render_run_detail(sample_run, console=console)

    result = output.getvalue()
    assert "Postmortem" in result
    assert "alice" in result


def test_render_run_detail_without_postmortem(sample_run):
    """Test render_run_detail with postmortem unassigned."""
    sample_run.postmortem_status = "unassigned"

    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=200)

    render_run_detail(sample_run, console=console)

    result = output.getvalue()
    assert "Postmortem" not in result


def test_render_campaign_table(sample_campaign):
    """Test render_campaign_table with one campaign."""
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=200)

    render_campaign_table([sample_campaign], console=console)

    result = output.getvalue()
    assert "Stability Test" in result
    assert "exploration" in result


def test_render_campaign_review_with_anomalies(sample_campaign):
    """Test render_campaign_review with anomalies."""
    review = {
        "outcome_distribution": {"pass": 8, "fail": 2, "marginal": 1},
        "anomalies": ["Run 5 temp spike to 320K", "Run 8 NaN forces detected"],
    }

    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=200)

    render_campaign_review(sample_campaign, review, console=console)

    result = output.getvalue()
    assert "Anomalies" in result or "anomal" in result.lower()
    assert "320K" in result or "Run 5" in result


def test_render_campaign_review_shows_blast_radius_status(sample_campaign):
    """AC-12 slice (Phase 2a, #4552): render_campaign_review must display
    blast_radius_status/claim_blast_radius_status -- campaigns.review_campaign()
    computes and forwards them, but the CLI's human-facing display previously
    dropped them silently (confirmed spec-adherence gap, PR #54 second jury round)."""
    review = {
        "outcome_distribution": {"pass": 8},
        "blast_radius_status": "affected",
        "claim_blast_radius_status": "unverifiable",
    }

    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=200)

    render_campaign_review(sample_campaign, review, console=console)

    result = output.getvalue()
    assert "affected" in result
    assert "unverifiable" in result


def test_render_campaign_review_clean_blast_radius_is_shown(sample_campaign):
    """Clean status must render explicitly, not be silently omitted -- an absent
    line would be indistinguishable from the display gap this regresses against."""
    review = {
        "outcome_distribution": {"pass": 8},
        "blast_radius_status": "clean",
        "claim_blast_radius_status": "clean",
    }

    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=200)

    render_campaign_review(sample_campaign, review, console=console)

    result = output.getvalue()
    assert "Blast radius" in result
    assert "clean" in result.lower()


def test_invalid_measurement_outcome_color_distinct_from_fail_and_error():
    """debt #1071: invalid_measurement must read as visually distinct from fail/error."""
    from bathos.rich_fmt import _get_outcome_color

    invalid_color = _get_outcome_color("invalid_measurement")
    assert invalid_color not in (_get_outcome_color("fail"), _get_outcome_color("error"))
