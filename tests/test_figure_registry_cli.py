"""CLI tests for `bth anchor figure-register` (S7 figure_entry write seam).

Backlog item 3490, task 260713_figure-eda-build-dag. Mirrors
tests/test_anchor_cli.py conventions (CliRunner + BTH_CATALOG_DIR env var).
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


def test_anchor_help_lists_figure_register_subcommand():
    result = runner.invoke(app, ["anchor", "--help"])
    assert result.exit_code == 0
    assert "figure-register" in result.output


def test_figure_register_then_query_figures_round_trips(anchor_cli_env):
    _ = anchor_cli_env
    result = runner.invoke(
        app,
        [
            "anchor",
            "figure-register",
            "a" * 64,
            "fig.figure.toml",
            "--figure-kind",
            "chord_diagram",
            "--fig-trust-state",
            "final",
            "--attestation-ref",
            "b" * 64,
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["asset_sha256"] == "a" * 64
    assert payload["sidecar_ref"] == "fig.figure.toml"
    assert payload["figure_kind"] == "chord_diagram"
    assert payload["fig_trust_state"] == "final"
    assert payload["attestation_ref"] == "b" * 64
    for bad in ("verdict", "strength", "content_hash", "outcome", "gate"):
        assert bad not in payload

    query_result = runner.invoke(app, ["query", "figures", "--asset-sha256", "a" * 64])
    assert query_result.exit_code == 0, query_result.output
    figures = json.loads(query_result.output)
    assert len(figures) == 1
    assert figures[0]["figure_kind"] == "chord_diagram"


def test_figure_register_rejects_invalid_fig_trust_state(anchor_cli_env):
    _ = anchor_cli_env
    result = runner.invoke(
        app,
        [
            "anchor",
            "figure-register",
            "c" * 64,
            "fig.figure.toml",
            "--figure-kind",
            "chord_diagram",
            "--fig-trust-state",
            "not-a-real-state",
        ],
    )
    assert result.exit_code != 0
