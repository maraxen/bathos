"""Tests for bathos.viz.html HTML export functions."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

# Skip all tests in this module if jinja2 is not available
pytest.importorskip("jinja2")

from bathos.campaigns import Campaign
from bathos.schema import Run
from bathos.viz.html import estimate_html_size, export_html, render_html_report


def _make_test_run(
    id: str | None = None,
    command: str = "python test.py",
) -> Run:
    """Create a minimal Run for testing."""
    return Run(
        id=id or str(uuid.uuid4()),
        project_slug="test-project",
        command=command,
        argv=["python", "test.py"],
        git_hash="abc123def456789",
        git_branch="main",
        git_dirty=False,
        hostname="test-host",
        slurm_job_id="",
        status="completed",
        exit_code=0,
        duration_s=42.5,
        timestamp=datetime(2026, 5, 21, 13, 47, 23, tzinfo=UTC),
        tags=[],
        output_paths=[],
        campaign_id="",
        outcome="pass",
        outcome_is_residual=False,
        script_sha256="",
        sidecar_path="",
        sidecar_mode="",
        agent_mode="",
        parent_run_id="",
        postmortem_status="",
        postmortem_hypothesis_status="",
        postmortem_verdict_override="",
        postmortem_author="",
        postmortem_summary="",
        postmortem_path="",
        postmortem_has_anomalies=False,
        postmortem_asset_links="",
    )


def _make_test_campaign(
    id: str | None = None,
    name: str = "test-campaign",
) -> Campaign:
    """Create a minimal Campaign for testing."""
    return Campaign(
        id=id or str(uuid.uuid4()),
        project_slug="test-project",
        name=name,
        mode="diagnostic",
        question="",
        hypothesis="",
        status="active",
        started_at="2026-05-21",
        concluded_at="",
        conclusion="",
        outcome_label="",
        parent_campaign_id="",
    )


def test_render_html_report_basic():
    """Test rendering a basic HTML report with one run."""
    run = _make_test_run()
    html = render_html_report([run])

    assert "<!DOCTYPE html>" in html
    assert "window.__BATHOS_DATA__" in html
    assert run.id in html
    assert "Alpine" in html or "window" in html  # Alpine.js is inlined


def test_render_html_report_empty():
    """Test rendering an empty HTML report with no runs or campaigns."""
    html = render_html_report([])

    assert "<!DOCTYPE html>" in html
    # Either shows empty state or renders without error
    assert html is not None and len(html) > 100


def test_render_html_report_with_campaign():
    """Test rendering an HTML report with a campaign."""
    run = _make_test_run()
    campaign = _make_test_campaign(name="my-test-campaign")

    html = render_html_report(
        [run],
        campaigns=[campaign],
    )

    assert "<!DOCTYPE html>" in html
    assert "window.__BATHOS_DATA__" in html
    assert "my-test-campaign" in html


def test_estimate_html_size():
    """Test HTML size estimation."""
    run = _make_test_run()
    html = render_html_report([run])
    size_mb = estimate_html_size(html)

    # Size should be positive and reasonable (< 10 MB)
    assert size_mb > 0
    assert size_mb < 10


def test_export_html_default_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Test export_html with default output path."""
    monkeypatch.chdir(tmp_path)
    run = _make_test_run()
    output_path, warned = export_html([run])

    assert output_path == "report.html"
    assert warned is False
    assert (tmp_path / "report.html").is_file()


def test_export_html_custom_output(tmp_path: Path):
    """Test export_html with custom output path."""
    run = _make_test_run()
    output_file = tmp_path / "custom_report.html"
    output_path, warned = export_html([run], output_path=str(output_file))

    assert output_path == str(output_file)
    assert output_file.exists()
    content = output_file.read_text()
    assert "<!DOCTYPE html>" in content


def test_render_html_report_data_blob():
    """Test that data blob is properly embedded as JSON."""
    run = _make_test_run(id="test-run-123")
    campaign = _make_test_campaign(id="campaign-456")

    html = render_html_report(
        [run],
        campaigns=[campaign],
    )

    # Extract the data blob from between <script> tags
    script_start = html.find("window.__BATHOS_DATA__ = ")
    assert script_start != -1

    # Find the end of the JSON (marked by };\n    </script>)
    script_end = html.find("};\n", script_start)
    assert script_end != -1

    json_str = html[script_start + len("window.__BATHOS_DATA__ = ") : script_end + 1]
    data = json.loads(json_str)

    # Verify structure
    assert "runs" in data
    assert "campaigns" in data
    assert len(data["runs"]) == 1
    assert len(data["campaigns"]) == 1
    assert data["runs"][0]["id"] == "test-run-123"
    assert data["campaigns"][0]["id"] == "campaign-456"


def test_render_html_report_catalog_state():
    """Test catalog_state is passed to template."""
    run = _make_test_run()
    html = render_html_report([run], catalog_state="cool")

    # Check for cool-tier banner
    assert "cool-tier" in html.lower() or "cool" in html.lower()


def test_render_html_report_total_run_count():
    """Test total_run_count parameter."""
    run = _make_test_run()
    html = render_html_report([run], total_run_count=100)

    # Should not error; value is passed to template
    assert "<!DOCTYPE html>" in html


def test_render_html_report_escapes_xss_in_fields_and_script_blob():
    """All campaign/run fields are HTML-escaped and JSON script blob escapes </script> and </head>."""
    xss_tag = "<script>alert(1)</script>"
    head_break = "</head><script>alert(2)</script>"
    attr_break = "x');alert(3);//"

    run = _make_test_run(
        id=attr_break,
        command=f"python {head_break}",
    )
    run.outcome = xss_tag
    run.git_branch = xss_tag

    campaign = Campaign(
        id=attr_break,
        project_slug="test-project",
        name=xss_tag,
        mode="sequential",
        question=head_break,
        hypothesis=xss_tag,
        status="active",
        started_at="2026-05-21",
        conclusion=xss_tag,
        outcome_label=xss_tag,
    )

    html = render_html_report([run], campaigns=[campaign])

    # Unescaped payload must never appear anywhere in rendered HTML
    assert "<script>alert(" not in html
    assert "</head><script>" not in html
    assert "toggleCampaign('x')" not in html
    assert "toggleRun('x')" not in html
    assert "expandedCampaigns['x" not in html
    assert "expandedRuns['x" not in html

    # Escaped HTML text is present in the body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;/head&gt;&lt;script&gt;alert(2)&lt;/script&gt;" in html

    # JSON blob inside <head> uses \u003c / \u003e escaping and parses cleanly back to originals
    marker = "window.__BATHOS_DATA__ = "
    start = html.find(marker)
    assert start != -1
    end = html.find("};\n", start)
    assert end != -1
    raw_json = html[start + len(marker) : end + 1]
    assert "<" not in raw_json and ">" not in raw_json and "&" not in raw_json
    parsed = json.loads(raw_json)
    assert parsed["runs"][0]["id"] == attr_break
    assert parsed["runs"][0]["command"] == f"python {head_break}"
    assert parsed["campaigns"][0]["name"] == xss_tag
    assert parsed["campaigns"][0]["question"] == head_break
