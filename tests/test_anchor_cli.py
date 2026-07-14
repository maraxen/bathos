"""CLI tests for `bth anchor` subcommands (S2 anchor-insert WRITE seam).

Backlog item 3483, task 260713_figure-eda-build-dag. Mirrors tests/test_claim_cli.py
conventions (CliRunner + BTH_CATALOG_DIR env var).
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from bathos.cli import app

runner = CliRunner()


@pytest.fixture
def anchor_cli_env(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    return catalog


def test_anchor_help_lists_subcommands():
    result = runner.invoke(app, ["anchor", "--help"])
    assert result.exit_code == 0
    assert "insert" in result.output
    assert "get" in result.output


def test_anchor_insert_then_get_round_trips(anchor_cli_env):
    _ = anchor_cli_env  # fixture sets BTH_CATALOG_DIR via monkeypatch as a side effect
    result = runner.invoke(
        app,
        [
            "anchor",
            "insert",
            "fig.png",
            "a" * 64,
            "--kind",
            "figure",
            "--content-hash",
            "b" * 64,
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["path"] == "fig.png"
    assert payload["sha256"] == "a" * 64
    assert payload["content_hash"] == "b" * 64

    get_result = runner.invoke(app, ["anchor", "get", "fig.png", "a" * 64])
    assert get_result.exit_code == 0, get_result.output
    fetched = json.loads(get_result.output)
    assert fetched["kind"] == "figure"
    assert fetched["content_hash"] == "b" * 64


def test_anchor_get_missing_prints_null(anchor_cli_env):
    _ = anchor_cli_env  # fixture sets BTH_CATALOG_DIR via monkeypatch as a side effect
    result = runner.invoke(app, ["anchor", "get", "nope.png", "a" * 64])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "null"
