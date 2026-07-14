"""Tests for the S7 figure_entry MCP tool surface
(bathos.mcp.figure_entry_register_tool).

Backlog item 3490, task 260713_figure-eda-build-dag. Mirrors the testable-function
convention used for the S2 anchor-insert MCP tools (tests/test_anchor_mcp.py) — the
`*_tool` functions are plain, synchronously-callable functions; the
`@app.tool`-decorated async wrapper (`mcp_figure_entry_register_tool`) just forwards.
"""

from __future__ import annotations

from bathos.mcp import figure_entry_register_tool, figure_lookup_tool


class TestFigureEntryRegisterTool:
    def test_register_then_lookup_round_trips(self, tmp_catalog):
        result = figure_entry_register_tool(
            asset_sha256="a" * 64,
            sidecar_ref="fig.figure.toml",
            figure_kind="chord_diagram",
            render_state="ready",
            fig_trust_state="draft",
            catalog_dir=str(tmp_catalog),
        )
        assert result["ok"] is True
        assert result["figure_entry"]["asset_sha256"] == "a" * 64
        assert result["figure_entry"]["sidecar_ref"] == "fig.figure.toml"

        looked_up = figure_lookup_tool(asset_sha256="a" * 64, catalog_dir=str(tmp_catalog))
        assert looked_up["count"] == 1
        assert looked_up["figures"][0]["figure_kind"] == "chord_diagram"

    def test_register_requires_asset_sha256_sidecar_ref_figure_kind(self):
        assert figure_entry_register_tool(
            asset_sha256="", sidecar_ref="fig.figure.toml", figure_kind="chord"
        )["ok"] is False
        assert figure_entry_register_tool(
            asset_sha256="a" * 64, sidecar_ref="", figure_kind="chord"
        )["ok"] is False
        assert figure_entry_register_tool(
            asset_sha256="a" * 64, sidecar_ref="fig.figure.toml", figure_kind=""
        )["ok"] is False

    def test_register_rejects_invalid_render_state(self, tmp_catalog):
        result = figure_entry_register_tool(
            asset_sha256="b" * 64,
            sidecar_ref="fig.figure.toml",
            figure_kind="chord",
            render_state="not-a-state",
            catalog_dir=str(tmp_catalog),
        )
        assert result["ok"] is False
        assert "render_state" in result["error"]
