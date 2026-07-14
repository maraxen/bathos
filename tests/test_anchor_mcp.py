"""Tests for the S2 anchor-insert MCP tool surface (bathos.mcp.anchor_insert_tool /
anchor_get_tool / anchor_find_tool).

Backlog item 3483, task 260713_figure-eda-build-dag. Mirrors the testable-function
convention used for the S1 read-back tools (resolve_pin_tool, figure_lookup_tool, ...)
in bathos.mcp — the *_tool functions are plain, synchronously-callable functions;
the @app.tool-decorated async wrappers just forward to them.
"""

from __future__ import annotations

from bathos.mcp import anchor_find_tool, anchor_get_tool, anchor_insert_tool


class TestAnchorInsertTool:
    def test_insert_then_get_round_trips(self, tmp_catalog):
        result = anchor_insert_tool(
            path="fig.png",
            sha256="a" * 64,
            kind="figure",
            content_hash="b" * 64,
            catalog_dir=str(tmp_catalog),
        )
        assert result["ok"] is True
        assert result["anchor"]["path"] == "fig.png"
        assert result["anchor"]["sha256"] == "a" * 64

        fetched = anchor_get_tool(path="fig.png", sha256="a" * 64, catalog_dir=str(tmp_catalog))
        assert fetched["ok"] is True
        assert fetched["anchor"]["content_hash"] == "b" * 64

    def test_insert_requires_path_sha256_kind(self):
        assert anchor_insert_tool(path="", sha256="a" * 64, kind="figure")["ok"] is False
        assert anchor_insert_tool(path="p", sha256="", kind="figure")["ok"] is False
        assert anchor_insert_tool(path="p", sha256="a" * 64, kind="")["ok"] is False


class TestAnchorGetTool:
    def test_get_missing_returns_null_anchor(self, tmp_catalog):
        result = anchor_get_tool(path="nope.png", sha256="a" * 64, catalog_dir=str(tmp_catalog))
        assert result["ok"] is True
        assert result["anchor"] is None


class TestAnchorFindTool:
    def test_find_by_kind(self, tmp_catalog):
        anchor_insert_tool(
            path="fig.png", sha256="a" * 64, kind="figure", catalog_dir=str(tmp_catalog)
        )
        anchor_insert_tool(
            path="attest.json", sha256="b" * 64, kind="attestation", catalog_dir=str(tmp_catalog)
        )

        result = anchor_find_tool(kind="figure", catalog_dir=str(tmp_catalog))
        assert result["count"] == 1
        assert result["anchors"][0]["path"] == "fig.png"
