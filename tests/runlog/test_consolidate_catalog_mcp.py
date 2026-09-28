"""MCP-level coverage for `consolidate_catalog_tool` (the plain, synchronously
callable impl behind the `consolidate_catalog` MCP tool -- debt #1998, PR #74
review fix 11). Mirrors the testable-function convention documented in
`tests/test_anchor_mcp.py`: the `@cisternal.tool`-decorated async wrapper just
forwards to this plain function, so it is tested directly rather than through
the FastMCP transport.

Uses the same isolation as the rest of `tests/runlog/` (`isolated_home`,
autouse from `conftest.py`).
"""

from __future__ import annotations

from pathlib import Path

from bathos.mcp import consolidate_catalog_tool


def _write_bth_toml(root: Path, *, catalog_dir: Path | None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    lines = ["[project]", 'slug = "proj"', f'root = "{root}"']
    if catalog_dir is not None:
        lines.append(f'catalog_dir = "{catalog_dir}"')
    (root / ".bth.toml").write_text("\n".join(lines) + "\n")


def test_mcp_consolidate_catalog_tool_clean(tmp_path: Path):
    root = tmp_path / "root"
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)
    _write_bth_toml(root, catalog_dir=source)

    (source / "campaigns").mkdir(parents=True)
    (source / "campaigns" / "c.json").write_text('{"id": "c"}')

    result = consolidate_catalog_tool(root=str(root), catalog_dir=str(dest))

    assert result["status"] == "ok"
    assert result["copied"] == 1
    assert (dest / "campaigns" / "c.json").is_file()


def test_mcp_consolidate_catalog_tool_needs_review(tmp_path: Path):
    root = tmp_path / "root"
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    (source / "campaigns").mkdir(parents=True)
    (dest / "campaigns").mkdir(parents=True)
    _write_bth_toml(root, catalog_dir=source)

    (source / "campaigns" / "c.json").write_text("source-version")
    (dest / "campaigns" / "c.json").write_text("dest-version")

    result = consolidate_catalog_tool(root=str(root), catalog_dir=str(dest))

    assert result["status"] == "needs_review"
    assert result["conflicts"] == 1
    assert result["conflict_paths"] == ["campaigns/c.json"]


def test_mcp_consolidate_catalog_tool_dry_run_no_writes(tmp_path: Path):
    root = tmp_path / "root"
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)
    _write_bth_toml(root, catalog_dir=source)

    (source / "campaigns").mkdir(parents=True)
    (source / "campaigns" / "c.json").write_text('{"id": "c"}')

    result = consolidate_catalog_tool(root=str(root), catalog_dir=str(dest), dry_run=True)

    assert result["status"] == "ok"
    assert result["applied"] is False
    assert result["copied"] == 1
    assert not (dest / "campaigns" / "c.json").exists()


def test_mcp_consolidate_catalog_tool_missing_key_without_source_catalog_errors(
    tmp_path: Path,
):
    root = tmp_path / "root"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)
    _write_bth_toml(root, catalog_dir=None)

    result = consolidate_catalog_tool(root=str(root), catalog_dir=str(dest))

    assert "error" in result
    assert "nothing to consolidate" in result["error"]


def test_mcp_consolidate_catalog_tool_source_catalog_override(tmp_path: Path):
    """Second pass of the race-window procedure: key already removed from
    `.bth.toml`, `source_catalog` supplies the path explicitly instead."""
    root = tmp_path / "root"
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)
    _write_bth_toml(root, catalog_dir=None)

    (source / "campaigns").mkdir(parents=True)
    (source / "campaigns" / "c.json").write_text('{"id": "c"}')

    result = consolidate_catalog_tool(
        root=str(root), catalog_dir=str(dest), source_catalog=str(source)
    )

    assert result["status"] == "ok"
    assert result["copied"] == 1
    assert (dest / "campaigns" / "c.json").is_file()
