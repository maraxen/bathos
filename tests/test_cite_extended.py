"""Tests for #2345 cite.py extensions: file:line quote/number checks & extraction guards."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bathos.cite import detect_two_column_interleaving, verify_citations


def _make_sources(tmp_path: Path, files: dict[str, str], extras: dict[str, dict] | None = None) -> tuple[Path, dict[str, str]]:
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    shas: dict[str, str] = {}
    manifest_sources: dict[str, dict] = {}
    for filename, content in files.items():
        raw_bytes = content.encode("utf-8")
        (sources_dir / filename).write_bytes(raw_bytes)
        digest = hashlib.sha256(raw_bytes).hexdigest()
        shas[filename] = digest
        entry: dict = {"sha256": digest}
        if extras and filename in extras:
            entry.update(extras[filename])
        manifest_sources[filename] = entry
    (sources_dir / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "sources": manifest_sources}),
        encoding="utf-8",
    )
    return sources_dir, shas


def test_verify_citations_inline_quote_and_number_at_file_line(tmp_path: Path) -> None:
    sources_dir, _ = _make_sources(
        tmp_path,
        {
            "paper.txt": (
                "Introduction text without metrics.\n"
                "The proposed solver achieved 94.2% accuracy with 12.5 ms latency.\n"
                "Exact inline phrasing: \"energy drift remained below 1e-5\" across runs.\n"
                "Table 1: Baseline | 88.1% | 19.0 ms\n"
            )
        },
    )

    good_doc = tmp_path / "good_inline.md"
    good_doc.write_text(
        "# Research Notes\n"
        "Solver reached 94.2% accuracy at 12.5 ms latency (paper.txt:2).\n"
        "Paper states \"energy drift remained below 1e-5\" (paper.txt:3).\n",
        encoding="utf-8",
    )
    res_good = verify_citations(good_doc, sources_dir, strict_coords=True)
    assert res_good.ok is True, res_good.errors
    assert res_good.checked_numbers >= 2

    # Wrong number at cited line fails in strict_coords=True and warns if elsewhere in strict_coords=False
    bad_num_doc = tmp_path / "bad_num.md"
    bad_num_doc.write_text(
        "Solver reached 88.1% accuracy (paper.txt:2).\n",
        encoding="utf-8",
    )
    strict_res = verify_citations(bad_num_doc, sources_dir, strict_coords=True)
    assert strict_res.ok is False
    assert any("88.1%" in e for e in strict_res.errors)

    loose_res = verify_citations(bad_num_doc, sources_dir, strict_coords=False)
    assert loose_res.ok is True
    assert len(loose_res.warnings) == 1

    # Fabricated number fails in both modes
    fab_num_doc = tmp_path / "fab_num.md"
    fab_num_doc.write_text(
        "Solver reached 99.9% accuracy (paper.txt:2).\n",
        encoding="utf-8",
    )
    assert verify_citations(fab_num_doc, sources_dir, strict_coords=True).ok is False
    assert verify_citations(fab_num_doc, sources_dir, strict_coords=False).ok is False


def test_verify_citations_numbered_tag_inline_number_verification(tmp_path: Path) -> None:
    sources_dir, shas = _make_sources(
        tmp_path,
        {
            "study.numbered.txt": (
                "[Page 1, Line 1] We evaluated 1,250 molecules at 300 K.\n"
                "[Page 1, Line 2] Mean error dropped to 0.042 kcal/mol.\n"
            )
        },
    )
    digest = shas["study.numbered.txt"]

    ok_doc = tmp_path / "ok.md"
    ok_doc.write_text(
        f"Evaluated 1250 molecules at 300 K [source: study.numbered.txt, sha256: {digest}, Page 1, Line 1].\n"
        f"Mean error was 0.042 [source: study.numbered.txt, sha256: {digest}, Page 1, Line 2].\n",
        encoding="utf-8",
    )
    res_ok = verify_citations(ok_doc, sources_dir, strict_coords=True)
    assert res_ok.ok is True, res_ok.errors

    bad_doc = tmp_path / "bad.md"
    bad_doc.write_text(
        f"Mean error was 0.015 [source: study.numbered.txt, sha256: {digest}, Page 1, Line 2].\n",
        encoding="utf-8",
    )
    res_bad = verify_citations(bad_doc, sources_dir, strict_coords=True)
    assert res_bad.ok is False
    assert any("0.015" in e for e in res_bad.errors)


def test_two_column_pdf_interleaving_guard(tmp_path: Path) -> None:
    interleaved_lines = [
        "[Page 1, Line 1] The left column discusses thermostat     Right column begins unrelated benchmark",
        "[Page 1, Line 2] Normal single column sentence.",
        "[Page 1, Line 3] Model A     94.2%     88.1%",
    ]
    findings = detect_two_column_interleaving(interleaved_lines)
    assert len(findings) == 1
    assert findings[0][0] == 1

    sources_dir, shas = _make_sources(
        tmp_path,
        {
            "two_col.numbered.txt": (
                "[Page 1, Line 1] The left column discusses thermostat     Right column begins unrelated benchmark\n"
                "[Page 1, Line 2] Clean single column sentence with 91.5% accuracy.\n"
            )
        },
    )
    digest = shas["two_col.numbered.txt"]

    # Citing the interleaved line is rejected by the extraction guard
    bad_doc = tmp_path / "interleaved_cite.md"
    bad_doc.write_text(
        f"> The left column discusses thermostat     Right column begins unrelated benchmark [source: two_col.numbered.txt, sha256: {digest}, Page 1, Line 1]\n",
        encoding="utf-8",
    )
    res_bad = verify_citations(bad_doc, sources_dir, strict_coords=True)
    assert res_bad.ok is False
    assert any("two-column PDF interleaving" in e for e in res_bad.errors)

    # Citing the clean line passes
    ok_doc = tmp_path / "clean_cite.md"
    ok_doc.write_text(
        f"Accuracy is 91.5% [source: two_col.numbered.txt, sha256: {digest}, Page 1, Line 2].\n",
        encoding="utf-8",
    )
    res_ok = verify_citations(ok_doc, sources_dir, strict_coords=True)
    assert res_ok.ok is True, res_ok.errors


def test_figure_read_numeric_grounding_guard(tmp_path: Path) -> None:
    sources_dir, _ = _make_sources(
        tmp_path,
        {
            "paper.txt": (
                "Figure 2: Training accuracy across learning rates.\n"
                "Table 1: Method X | 87.5% | 0.91\n"
                "See Figure 3 for qualitative trajectories.\n"
            )
        },
    )

    # Uncited figure-read number without table/numeric grounding fails
    uncited_fig_doc = tmp_path / "uncited_fig.md"
    uncited_fig_doc.write_text(
        "Visually read from Figure 2, accuracy reaches 73.4%.\n",
        encoding="utf-8",
    )
    res_uncited = verify_citations(uncited_fig_doc, sources_dir, strict_coords=True)
    assert res_uncited.ok is False
    assert any("figure-read numeric value(s)" in e and "73.4%" in e for e in res_uncited.errors)

    # Citing figure caption line with a number not present in caption fails
    caption_only_doc = tmp_path / "caption_only.md"
    caption_only_doc.write_text(
        "From Figure 2, accuracy reaches 73.4% (paper.txt:1).\n",
        encoding="utf-8",
    )
    res_cap = verify_citations(caption_only_doc, sources_dir, strict_coords=True)
    assert res_cap.ok is False
    assert any("73.4%" in e for e in res_cap.errors)

    # Referencing a figure WITHOUT claiming numeric values passes
    no_num_fig_doc = tmp_path / "no_num_fig.md"
    no_num_fig_doc.write_text(
        "See Figure 3 for qualitative trajectories (paper.txt:3).\n",
        encoding="utf-8",
    )
    assert verify_citations(no_num_fig_doc, sources_dir, strict_coords=True).ok is True

    # Figure number backed by verified Table 1 passes
    grounded_fig_doc = tmp_path / "grounded_fig.md"
    grounded_fig_doc.write_text(
        "Figure 2 (Table 1) reports 87.5% accuracy (paper.txt:2).\n",
        encoding="utf-8",
    )
    res_grounded = verify_citations(grounded_fig_doc, sources_dir, strict_coords=True)
    assert res_grounded.ok is True, res_grounded.errors
