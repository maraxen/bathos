import io
from datetime import datetime, UTC

import pytest
from rich.console import Console

from bathos.schema import Run
from bathos.campaigns import Campaign
from bathos.rich_fmt import (
    render_runs_table,
    render_run_detail,
    render_campaign_table,
    render_campaign_review,
)


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
