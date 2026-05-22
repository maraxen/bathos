"""Tests for viz module packaging and asset availability."""

import importlib.resources
import pytest


def test_viz_templates_exist():
    """Verify all template files are packaged."""
    template_files = [
        "index.html",
        "_runs.html",
        "_run_detail.html",
        "_campaign.html",
    ]
    templates = importlib.resources.files("bathos.viz").joinpath("templates")
    for filename in template_files:
        assert (templates / filename).is_file(), f"Template {filename} not found"


def test_viz_static_assets_exist():
    """Verify static assets (JS, CSS) are packaged."""
    static_files = ["alpine.min.js", "pico.min.css"]
    static = importlib.resources.files("bathos.viz").joinpath("static")
    for filename in static_files:
        asset = static / filename
        assert asset.is_file(), f"Static asset {filename} not found"
        content = asset.read_text(encoding="utf-8")
        assert len(content) > 0, f"Static asset {filename} is empty"


def test_viz_versions_md_exists():
    """Verify VERSIONS.md with license attribution exists."""
    static = importlib.resources.files("bathos.viz").joinpath("static")
    versions = static / "VERSIONS.md"
    assert versions.is_file(), "VERSIONS.md not found"
    content = versions.read_text(encoding="utf-8")
    assert "MIT" in content, "VERSIONS.md missing MIT license reference"
    assert "Alpine" in content, "VERSIONS.md missing Alpine attribution"
    assert "Pico" in content, "VERSIONS.md missing Pico attribution"


def test_viz_data_imports():
    """Verify viz.data module exports required TypedDicts."""
    from bathos.viz.data import RunDisplay, CampaignDisplay, project_run, project_campaign

    assert RunDisplay is not None
    assert CampaignDisplay is not None
    assert callable(project_run)
    assert callable(project_campaign)


def test_viz_html_imports():
    """Verify viz.html module is importable and functional."""
    pytest.importorskip("jinja2")
    from bathos.viz.html import render_html_report, export_html

    assert callable(render_html_report)
    assert callable(export_html)


def test_viz_server_imports():
    """Verify viz.server module is importable."""
    pytest.importorskip("fastapi")
    from bathos.viz.server import create_app, run_server

    assert callable(create_app)
    assert callable(run_server)
