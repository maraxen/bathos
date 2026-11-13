"""Tests for bth cite command and citation formatting."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from bathos.cite import format_citation
from bathos.schema import Run


@pytest.fixture
def mock_run_with_manifest():
    """Create a Run with manifest_sha256 and manifest_path set."""
    return Run(
        id=str(uuid4()),
        project_slug="test_project",
        command="uv run python test.py",
        argv=["uv", "run", "python", "test.py"],
        git_hash="abc123def456",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
        outcome="pass",
        manifest_sha256="sha256_1234567890abcdef",
        manifest_path="path/to/manifest.json",
        sidecar_sha256="sidecar_sha256_value",
    )


@pytest.fixture
def mock_run_no_manifest():
    """Create a Run with empty manifest_sha256 and manifest_path (pre-v0.6)."""
    return Run(
        id=str(uuid4()),
        project_slug="test_project",
        command="uv run python test.py",
        argv=["uv", "run", "python", "test.py"],
        git_hash="abc123def456",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
        outcome="pass",
        manifest_sha256="",
        manifest_path="",
        sidecar_sha256="",
    )


def test_cite_markdown_contains_manifest_hash(mock_run_with_manifest):
    """Test that markdown citation includes manifest hash."""
    output = format_citation(mock_run_with_manifest, fmt="markdown")
    assert "manifest" in output.lower()
    assert "sha256_12345678" in output


def test_cite_markdown_contains_run_id(mock_run_with_manifest):
    """Test that markdown citation includes run ID."""
    output = format_citation(mock_run_with_manifest, fmt="markdown")
    assert mock_run_with_manifest.id[:8] in output


def test_cite_pre_v6_run_shows_not_recorded(mock_run_no_manifest):
    """Test that pre-v0.6 runs show 'not recorded' for missing fields."""
    output = format_citation(mock_run_no_manifest, fmt="markdown")
    assert "not recorded" in output


def test_cite_json_format_has_required_keys(mock_run_with_manifest):
    """Test that JSON citation has required keys."""
    import json

    output = format_citation(mock_run_with_manifest, fmt="json")
    data = json.loads(output)
    assert "manifest_sha256" in data
    assert "run_id" in data
    assert "outcome" in data
    assert "timestamp" in data


def test_cite_json_format_values(mock_run_with_manifest):
    """Test that JSON citation has correct values."""
    import json

    output = format_citation(mock_run_with_manifest, fmt="json")
    data = json.loads(output)
    assert data["manifest_sha256"] == "sha256_1234567890abcdef"
    assert data["outcome"] == "pass"
    assert data["run_id"] == mock_run_with_manifest.id


def test_cite_markdown_default_format(mock_run_with_manifest):
    """Test that default format is markdown."""
    output = format_citation(mock_run_with_manifest)  # No fmt specified
    assert "Run" in output
    assert "manifest" in output.lower()


def test_cite_includes_git_sha(mock_run_with_manifest):
    """Test that citation includes git SHA."""
    output = format_citation(mock_run_with_manifest, fmt="markdown")
    assert "abc12345" in output or "abc123de" in output  # First 8 chars


def test_cite_includes_sidecar_sha256(mock_run_with_manifest):
    """Test that citation includes sidecar SHA256."""
    output = format_citation(mock_run_with_manifest, fmt="markdown")
    assert "sidecar" in output.lower()



def _make_sources(
    tmp_path: Path,
    files: dict[str, str],
) -> tuple[Path, dict[str, str]]:
    import hashlib
    import json

    sources_dir = tmp_path / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    shas: dict[str, str] = {}
    manifest_sources: dict[str, dict[str, str]] = {}
    for filename, content in files.items():
        fpath = sources_dir / filename
        raw_bytes = content.encode("utf-8")
        fpath.write_bytes(raw_bytes)
        digest = hashlib.sha256(raw_bytes).hexdigest()
        shas[filename] = digest
        manifest_sources[filename] = {"sha256": digest}
    (sources_dir / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "sources": manifest_sources}),
        encoding="utf-8",
    )
    return sources_dir, shas


def test_verify_citations_strict_coords_positive_normalization_and_attribution(
    tmp_path: Path,
) -> None:
    from bathos.cite import verify_citations

    source_body = (
        "[Page 1, Line 1] The catalytic efficiency of prote-\n"
        "[Page 1, Line 2] ase variant T17 remains \uff11\uff10\uff10-fold higher\n"
        "[Page 1, Line 3] than baseline controls on soft\u00adsubstrate.\n"
        "[Page 2, Line 1] Actual quote without outer quotation marks.\n"
    )
    sources_dir, shas = _make_sources(tmp_path, {"paper_alpha.numbered.txt": source_body})
    digest = shas["paper_alpha.numbered.txt"]

    doc = tmp_path / "notes.md"
    doc.write_text(
        f"""# Verified Notes

Claim before quote [source: paper_alpha.numbered.txt, sha256: {digest}, Page 1, Line 1-3]

> “The catalytic efficiency of protease
> variant T17 remains 100-fold higher”

> “Actual quote without outer quotation marks.” [source: paper_alpha.numbered.txt, sha256: {digest}, Page 2, Line 1]

Multi-line quote followed by em-dash attribution:

> Actual quote
> without outer quotation marks.
> — [source: paper_alpha.numbered.txt, sha256: {digest}, Page 2, Line 1]

Following non-blank line attribution:

> softsubstrate.
[source: paper_alpha.numbered.txt, sha256: {digest}, Page 1, Line 3]

Prose alongside tag is preserved and must match:

> remains 100-fold [source: paper_alpha.numbered.txt, sha256: {digest}, Page 1, Line 2]
""",
        encoding="utf-8",
    )
    result = verify_citations(doc, sources_dir, strict_coords=True)
    assert result.ok is True, result.errors
    assert result.warnings == ()
    assert result.checked_quotes == 5
    assert result.checked_coords == 5


def test_verify_citations_strict_coords_negative_controls_and_empty_bodies(
    tmp_path: Path,
) -> None:
    from bathos.cite import verify_citations

    source_body = (
        "[Page 1, Line 1] First line alpha.\n"
        "[Page 1, Line 2] Second line beta.\n"
        "[Page 1, Line 4] Fourth line with line three missing.\n"
        "[Page 2, Line 1] Elsewhere quote gamma.\n"
    )
    sources_dir, shas = _make_sources(tmp_path, {"paper_beta.numbered.txt": source_body})
    digest = shas["paper_beta.numbered.txt"]
    wrong_digest = "0" * 64

    # 1. Wrong SHA-256 tag fails under both strict and non-strict modes
    sha_doc = tmp_path / "bad_sha.md"
    sha_doc.write_text(
        f"> First line alpha. [source: paper_beta.numbered.txt, sha256: {wrong_digest}, Page 1, Line 1]\n",
        encoding="utf-8",
    )
    assert verify_citations(sha_doc, sources_dir, strict_coords=True).ok is False
    assert verify_citations(sha_doc, sources_dir, strict_coords=False).ok is False

    # 2. Out-of-window quote: fails in strict_coords=True, warns in strict_coords=False
    oow_doc = tmp_path / "out_of_window.md"
    oow_doc.write_text(
        f"> Elsewhere quote gamma. [source: paper_beta.numbered.txt, sha256: {digest}, Page 1, Line 1]\n",
        encoding="utf-8",
    )
    strict_oow = verify_citations(oow_doc, sources_dir, strict_coords=True)
    assert strict_oow.ok is False
    non_strict_oow = verify_citations(oow_doc, sources_dir, strict_coords=False)
    assert non_strict_oow.ok is True
    assert len(non_strict_oow.warnings) == 1

    # 3. Fabricated quote (single-line and multi-line) fails even under strict_coords=False
    fab_doc = tmp_path / "fabricated.md"
    fab_doc.write_text(
        f"""> Completely fabricated text [source: paper_beta.numbered.txt, sha256: {digest}, Page 1, Line 1]

> First line alpha.
> Not actually present [source: paper_beta.numbered.txt, sha256: {digest}, Page 1, Line 1-2]
""",
        encoding="utf-8",
    )
    assert verify_citations(fab_doc, sources_dir, strict_coords=True).ok is False
    assert verify_citations(fab_doc, sources_dir, strict_coords=False).ok is False

    # 4. Empty normalized blockquote bodies fail under BOTH strict_coords modes
    for idx, empty_md in enumerate(
        [
            ">\n",
            f"> [source: paper_beta.numbered.txt, sha256: {digest}, Page 1, Line 1]\n",
            f'> "" [source: paper_beta.numbered.txt, sha256: {digest}, Page 1, Line 1]\n',
            f'> "   " [source: paper_beta.numbered.txt, sha256: {digest}, Page 1, Line 1]\n',
            f"> — [source: paper_beta.numbered.txt, sha256: {digest}, Page 1, Line 1]\n",
        ]
    ):
        empty_doc = tmp_path / f"empty_{idx}.md"
        empty_doc.write_text(empty_md, encoding="utf-8")
        strict_empty = verify_citations(empty_doc, sources_dir, strict_coords=True)
        non_strict_empty = verify_citations(empty_doc, sources_dir, strict_coords=False)
        assert strict_empty.ok is False, empty_md
        assert non_strict_empty.ok is False, empty_md

    # 5. Missing line gap & inverted range fail under BOTH modes
    gap_doc = tmp_path / "gap.md"
    gap_doc.write_text(
        f"> First line alpha. [source: paper_beta.numbered.txt, sha256: {digest}, Page 1, Line 1-4]\n",
        encoding="utf-8",
    )
    assert verify_citations(gap_doc, sources_dir, strict_coords=True).ok is False
    assert verify_citations(gap_doc, sources_dir, strict_coords=False).ok is False

    inv_doc = tmp_path / "inverted.md"
    inv_doc.write_text(
        f"> First line alpha. [source: paper_beta.numbered.txt, sha256: {digest}, Page 1, Line 2-1]\n",
        encoding="utf-8",
    )
    assert verify_citations(inv_doc, sources_dir, strict_coords=True).ok is False
    assert verify_citations(inv_doc, sources_dir, strict_coords=False).ok is False

    # 6. Uncited non-empty blockquote fails in strict_coords=True, warns in strict_coords=False
    uncited_doc = tmp_path / "uncited.md"
    uncited_doc.write_text("> Uncited quote body.\n", encoding="utf-8")
    assert verify_citations(uncited_doc, sources_dir, strict_coords=True).ok is False
    uncited_loose = verify_citations(uncited_doc, sources_dir, strict_coords=False)
    assert uncited_loose.ok is True
    assert len(uncited_loose.warnings) == 1

    # 7. Trailing prose before [source: ...] that is not in source fails
    extra_prose_doc = tmp_path / "trailing_prose.md"
    extra_prose_doc.write_text(
        f"> First line alpha. unverified suffix [source: paper_beta.numbered.txt, sha256: {digest}, Page 1, Line 1]\n",
        encoding="utf-8",
    )
    assert verify_citations(extra_prose_doc, sources_dir, strict_coords=True).ok is False


def test_verify_citations_manifest_and_numbered_file_errors(tmp_path: Path) -> None:
    import json

    from bathos.cite import verify_citations
    from bathos.errors import CitationVerificationError

    doc = tmp_path / "doc.md"
    doc.write_text("> Quote.\n", encoding="utf-8")

    # Missing manifest
    empty_sources = tmp_path / "no_manifest"
    empty_sources.mkdir()
    with pytest.raises(CitationVerificationError):
        verify_citations(doc, empty_sources)

    # Corrupt SHA-256 between manifest and file
    bad_sha_dir, _ = _make_sources(tmp_path / "bad_sha", {"s.numbered.txt": "[Page 1, Line 1] Hi\n"})
    (bad_sha_dir / "s.numbered.txt").write_text("[Page 1, Line 1] Mutated\n", encoding="utf-8")
    with pytest.raises(CitationVerificationError, match="SHA-256 mismatch"):
        verify_citations(doc, bad_sha_dir)

    # Duplicate (page, line)
    dup_dir, _ = _make_sources(
        tmp_path / "dup_coord",
        {"d.numbered.txt": "[Page 1, Line 1] One\n[Page 1, Line 1] Two\n"},
    )
    with pytest.raises(CitationVerificationError, match="duplicate coordinate"):
        verify_citations(doc, dup_dir)

    # Invalid key traversal in manifest
    trav_dir = tmp_path / "trav"
    trav_dir.mkdir()
    (trav_dir / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "sources": {"../evil.numbered.txt": {"sha256": "a" * 64}}}),
        encoding="utf-8",
    )
    with pytest.raises(CitationVerificationError, match="invalid source filename"):
        verify_citations(doc, trav_dir)


def test_verify_citations_mcp_envelope_surfaces_citation_invalid(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import asyncio

    from bathos.errors import RESOLUTION_HINTS, BathosErrorCode
    from bathos.mcp import mcp_verify_citations_tool

    monkeypatch.setattr("bathos.mcp.event", lambda *_args, **_kwargs: None)

    sources_dir, shas = _make_sources(
        tmp_path, {"paper.numbered.txt": "[Page 1, Line 1] Exact verified sentence.\n"}
    )
    digest = shas["paper.numbered.txt"]

    valid_doc = tmp_path / "valid.md"
    valid_doc.write_text(
        f"> Exact verified sentence. [source: paper.numbered.txt, sha256: {digest}, Page 1, Line 1]\n",
        encoding="utf-8",
    )
    ok_env = asyncio.run(
        mcp_verify_citations_tool(doc_path=str(valid_doc), sources_dir=str(sources_dir))
    )
    assert ok_env["ok"] is True
    assert ok_env["error_code"] is None
    assert ok_env["error"] is None

    bad_doc = tmp_path / "bad.md"
    bad_doc.write_text(
        f"> Wrong quote. [source: paper.numbered.txt, sha256: {digest}, Page 1, Line 1]\n",
        encoding="utf-8",
    )
    fail_env = asyncio.run(
        mcp_verify_citations_tool(doc_path=str(bad_doc), sources_dir=str(sources_dir))
    )
    assert fail_env["ok"] is False
    assert fail_env["error_code"] == BathosErrorCode.CITATION_INVALID.value
    assert isinstance(fail_env["error"], str) and len(fail_env["error"]) > 0
    assert fail_env["resolution_hint"] == RESOLUTION_HINTS[BathosErrorCode.CITATION_INVALID]

    raised_env = asyncio.run(
        mcp_verify_citations_tool(
            doc_path=str(valid_doc),
            sources_dir=str(tmp_path / "missing_sources_dir"),
        )
    )
    assert raised_env["ok"] is False
    assert raised_env["error_code"] == BathosErrorCode.CITATION_INVALID.value
    assert isinstance(raised_env["error"], str) and len(raised_env["error"]) > 0
    assert raised_env["resolution_hint"] == RESOLUTION_HINTS[BathosErrorCode.CITATION_INVALID]
