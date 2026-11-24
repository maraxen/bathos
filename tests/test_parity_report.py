"""Tests for WP30a: report core, disclosure tiers T0-T3, badge worked examples, and lints."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
from pathlib import Path

import numpy as np
import pytest

from bathos.cli_cyclopts import app
from bathos.parity.axes import RING0_ACTIVE_G_CODES, compute_axes_and_clauses
from bathos.parity.badge import (
    lint_rendered_text,
    render_t0,
    render_t1,
    validate_badge,
)
from bathos.parity.declare import declare_parity_record
from bathos.parity.evidence import write_cell_evidence
from bathos.parity.grader import grade_parity_record
from bathos.parity.report import (
    build_parity_report,
    canonical_report_bytes,
    render_report_html,
    render_report_markdown,
)
from bathos.parity.schema import ALL_G_CODES, compute_cell_id, render_declaration_scaffold
from bathos.parity.store import ParityStore


def _init_git_repo(repo_dir: Path) -> str:
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_dir), check=True)
    (repo_dir / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "--no-verify", "-m", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo_dir), check=True, capture_output=True, text=True
    ).stdout.strip()


def _cell_id(decl: dict, idx: int = 0) -> str:
    c = decl["cell_plan"][idx]
    return compute_cell_id(
        c["component_id"], c["use_id"], c["knob_setting"], c["axis_value"], c["data_tier"], c["pairing"]
    )


class TestWorkedBadgeExamples:
    def test_example_1_ring0_and_example_20_loosened(self) -> None:
        decl = tomllib.loads(render_declaration_scaffold("aminx-frustrampnn-score-v1", impl_name="aminx"))
        decl["reference"][0] = {
            "id": "frustrampnn-1ubq",
            "role": "comparator",
            "kind": "sealed_dump",
            "identity": {
                "dump_sha256": "ded4dbb6f472" + "0" * 52,
                "producer_run_id": "run_prod",
                "upstream_commit": "3a03cdc300bf" + "0" * 28,
            },
            "licence": "MIT",
        }
        decl["waiver"] = [
            {"cell_selector": f"k{i}", "reason_code": "out_of_scope", "reason_text": "out of scope", "judgement_id": "jdg_w"}
            for i in range(3)
        ]
        cells = [
            {
                "cell_id": f"{i:016x}",
                "data_tier": "toy",
                "pairing": "fixture",
                "declared_kind": "within_tolerance",
                "verdict": "pass",
                "overlays": [],
                "exploratory": False,
            }
            for i in range(4)
        ]
        clauses, gates, counts = compute_axes_and_clauses(
            declaration=decl,
            cells=cells,
            active_g_codes=RING0_ACTIVE_G_CODES,
            baseline_sha="a" * 64,
            graded_sha="a" * 64,
            has_registered_declaration=False,
            knobs_uncovered=None,
        )
        body = {
            "standard_version": "1.0.0",
            "parity_metadata": {
                "layer_under_test": "model",
                "badge_min_tier": "toy",
                "instrumentation": "instrumented",
                "mode": "confirmatory",
                "has_owner_stated_tolerance": True,
                "control": False,
            },
            "tool": {"name": "aminx", "revision": "1234567890ab" + "0" * 28, "third_party": False},
            "references": decl["reference"],
            "cells": cells,
            "cell_counts": counts,
            "clauses": clauses,
            "gates": gates,
            "dual_display_result": None,
            "amendments": "not computed",
        }
        h2 = "bbbbbbbbbbbb" + "0" * 52
        t1 = render_t1(body, h2)
        h1 = hashlib.sha256(t1.encode("utf-8")).hexdigest()
        t0 = render_t0(body, h1)

        assert t0 == (
            f"PROVISIONAL · PASSES WITHIN-TOLERANCE 4/4 · knobs not computed · retrospective · "
            f"aminx@1234567890ab vs sealed dump frustrampnn-1ubq@3a03cdc300bf (dump ded4dbb6f472) · "
            f"owner-stated tolerance · std 1.0.0 · T1 {h1[:12]}"
        )
        assert t1 == (
            "PROVISIONAL (21 checks pending) · PASSES WITHIN-TOLERANCE 4/4 · retrospective · "
            "aminx@1234567890ab vs sealed dump frustrampnn-1ubq@3a03cdc300bf (dump ded4dbb6f472) · "
            "layer model · tier toy · pairing fixture · knobs not computed · "
            "gates: 0 enforce, 0 advisory, 3 shadow, 23 not active · waived (3) · "
            f"owner-stated tolerance · std 1.0.0 · T2 {h2[:12]}"
        )
        assert validate_badge(t0, t1, body) == []

        # Example 20: tolerance loosened after first declaration
        fail_cells = [{**c, "verdict": "fail"} for c in cells]
        dual_clauses, _, _ = compute_axes_and_clauses(
            declaration=decl,
            cells=fail_cells,
            active_g_codes=RING0_ACTIVE_G_CODES,
            baseline_sha="a" * 64,
            graded_sha="a" * 64,
            has_registered_declaration=False,
            knobs_uncovered=None,
        )
        amended_clauses, _, _ = compute_axes_and_clauses(
            declaration=decl,
            cells=cells,
            active_g_codes=RING0_ACTIVE_G_CODES,
            baseline_sha="a" * 64,
            graded_sha="b" * 64,
            has_registered_declaration=False,
            knobs_uncovered=None,
        )
        body["clauses"] = amended_clauses
        body["dual_display_result"] = {"basis": "first_declared", "clauses": dual_clauses}
        t1_20 = render_t1(body, h2)
        h1_20 = hashlib.sha256(t1_20.encode("utf-8")).hexdigest()
        t0_20 = render_t0(body, h1_20)
        assert (
            "PROVISIONAL · PASSES WITHIN-TOLERANCE 4/4 · knobs not computed · amended · "
            "as first declared: FAILS WITHIN-TOLERANCE 0/4, knobs not computed, retrospective"
        ) in t0_20
        assert validate_badge(t0_20, t1_20, body) == []

    def test_example_15_and_16_singular_knob_and_clause_axis_split(self) -> None:
        decl = tomllib.loads(render_declaration_scaffold("ex15-v1", impl_name="aminx"))
        cells_15 = [
            {"cell_id": f"{i:016x}", "data_tier": "toy", "pairing": "fixture", "declared_kind": "within_tolerance", "verdict": "not_run" if i == 0 else "pass", "overlays": []}
            for i in range(4)
        ]
        all_active = set(ALL_G_CODES)
        cl15, g15, cnt15 = compute_axes_and_clauses(
            declaration=decl, cells=cells_15, active_g_codes=all_active, has_registered_declaration=True, knobs_uncovered=1
        )
        body15 = {
            "standard_version": "1.0.0",
            "parity_metadata": {"layer_under_test": "model", "badge_min_tier": "toy", "instrumentation": "instrumented", "mode": "confirmatory", "has_owner_stated_tolerance": False, "control": False},
            "tool": {"name": "aminx", "revision": "1234567890ab" + "0" * 28, "third_party": False},
            "references": decl["reference"],
            "cells": cells_15,
            "cell_counts": cnt15,
            "clauses": cl15,
            "gates": g15,
            "dual_display_result": None,
            "amendments": [],
        }
        t0_15 = render_t0(body15, "c" * 64)
        assert "PASSES WITHIN-TOLERANCE 3/4 · partial (1 not run, 1 knob uncovered)" in t0_15

        # Example 16: differing stale axes across two kinds
        cells_16 = [
            {"cell_id": "a" * 16, "data_tier": "toy", "pairing": "fixture", "declared_kind": "within_tolerance", "verdict": "fail", "overlays": ["stale(code)"]},
            {"cell_id": "b" * 16, "data_tier": "toy", "pairing": "fixture", "declared_kind": "behavioural", "verdict": "pass", "overlays": []},
        ]
        cl16, g16, cnt16 = compute_axes_and_clauses(
            declaration=decl, cells=cells_16, active_g_codes=all_active, has_registered_declaration=True, knobs_uncovered=0
        )
        body16 = {**body15, "cells": cells_16, "cell_counts": cnt16, "clauses": cl16, "gates": g16}
        t0_16 = render_t0(body16, "d" * 64)
        assert "FAILS WITHIN-TOLERANCE 0/1, stale (1); PASSES BEHAVIOURAL 1/1" in t0_16


class TestReportGenerationAndNegativeControls:
    def _build_graded_fixture(self, tmp_path: Path, *, run_evidence: bool = True) -> tuple[Path, Path, Path, Path]:
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        store_dir = tmp_path / "store"
        cat_dir = tmp_path / "catalog"
        decl_path = repo / "demo.parity.bth.toml"
        decl_path.write_text(render_declaration_scaffold("demo-report-v1", impl_name="aminx"), encoding="utf-8")
        decl = tomllib.loads(decl_path.read_text(encoding="utf-8"))
        declare_parity_record(decl_path, parity_store=store_dir)
        if run_evidence:
            cid = _cell_id(decl)
            write_cell_evidence(
                store=store_dir,
                record_id="demo-report-v1",
                cell_id=cid,
                run_id="r1",
                ours_arrays={"scores": np.array([1.0, 2.0], dtype=np.float32)},
                reference_arrays={"scores": np.array([1.0, 2.0], dtype=np.float32)},
                input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
            )
        graded = grade_parity_record(
            decl_path, parity_store=store_dir, catalog_dir=cat_dir, as_of="2026-11-24T12:00:00Z", track_run=False
        )
        return decl_path, graded.result_path, store_dir, cat_dir

    def test_determinism_hash_links_and_not_computed_sections(self, tmp_path: Path) -> None:
        decl_path, res_path, store_dir, _cat = self._build_graded_fixture(tmp_path)
        out1 = tmp_path / "r1.parity.report.json"
        out2 = tmp_path / "r2.parity.report.json"
        _, rep1 = build_parity_report(res_path, decl_path=decl_path, parity_store=store_dir, output_path=out1)
        _, rep2 = build_parity_report(res_path, decl_path=decl_path, parity_store=store_dir, output_path=out2)

        # Determinism
        assert out1.read_bytes() == out2.read_bytes()

        # Recompute h1 and h2 independently from written report file
        disk_report = json.loads(out1.read_text(encoding="utf-8"))
        badge = disk_report["badge"]
        body_without_badge = {k: v for k, v in disk_report.items() if k != "badge"}
        assert hashlib.sha256(canonical_report_bytes(body_without_badge)).hexdigest() == badge["t2_sha256"]
        assert hashlib.sha256(badge["t1"].encode("utf-8")).hexdigest() == badge["t1_sha256"]

        # Optional sections render 'not computed' and G21 is not_active
        assert disk_report["discrepancies"] == "not computed"
        assert disk_report["staleness"] == "not computed"
        assert disk_report["amendments"] == "not computed"
        assert disk_report["audit"] == "not computed"
        assert disk_report["provenance"] == "not computed"
        assert next(g["stage"] for g in disk_report["gates"] if g["code"] == "G21") == "not_active"

        # T3 bundle manifest written with absent markers for un-stored inputs
        store = ParityStore(store_dir)
        t3_file = store.root / "bundles" / "demo-report-v1.parity.bundle.json"
        t3_data = json.loads(t3_file.read_text(encoding="utf-8"))
        assert hashlib.sha256(t3_file.read_bytes()).hexdigest() == disk_report["bundle_sha256"]
        assert all(item["status"] == "absent" for item in t3_data["inputs"])

        # MD and HTML renderers work cleanly from JSON only and pass lints
        md = render_report_markdown(disk_report)
        html_doc = render_report_html(disk_report)
        assert "not computed" in md
        assert "<table>" in html_doc

        # NC: hash-cycle test — changing badge must not alter h2
        mutated = json.loads(out1.read_text(encoding="utf-8"))
        mutated["badge"] = {"t0": "changed", "t1": "changed", "t1_sha256": "0" * 64, "t2_sha256": "0" * 64}
        body_after = {k: v for k, v in mutated.items() if k != "badge"}
        assert hashlib.sha256(canonical_report_bytes(body_after)).hexdigest() == badge["t2_sha256"]

    def test_nc_bare_parity_and_unprefixed_passes_lints(self) -> None:
        # Bare 'parity' without kind word/'record'/'report' fails lint
        issues_bare = lint_rendered_text("Our model achieves parity with upstream.", checks_pending=False)
        assert issues_bare
        assert lint_rendered_text("Our parity report shows 4/4.", checks_pending=False) == []

        # PASSES without PROVISIONAL while checks_pending=True fails lint
        issues_prov = lint_rendered_text("PASSES WITHIN-TOLERANCE 4/4", checks_pending=True)
        assert issues_prov
        assert lint_rendered_text("PROVISIONAL · PASSES WITHIN-TOLERANCE 4/4", checks_pending=True) == []

    def test_nc_badge_validation_rejects_omitted_axis(self) -> None:
        decl = tomllib.loads(render_declaration_scaffold("bad-t0-v1", impl_name="aminx"))
        cells = [{"cell_id": "0" * 16, "data_tier": "toy", "pairing": "fixture", "declared_kind": "within_tolerance", "verdict": "pass", "overlays": []}]
        clauses, gates, counts = compute_axes_and_clauses(
            declaration=decl, cells=cells, active_g_codes=RING0_ACTIVE_G_CODES, baseline_sha="a" * 64, graded_sha="a" * 64, knobs_uncovered=None
        )
        body = {
            "tool": {"name": "aminx", "revision": "1234567890ab"},
            "references": decl["reference"],
            "clauses": clauses,
            "gates": gates,
            "cell_counts": counts,
            "dual_display_result": None,
        }
        bad_t0 = "PROVISIONAL · PASSES WITHIN-TOLERANCE 1/1 · aminx@1234567890ab vs upstream x@123 · std 1.0.0 · T1 abc"
        t1 = render_t1(body, "b" * 64)
        errs = validate_badge(bad_t0, t1, body)
        assert any("knobs not computed" in e or "retrospective" in e for e in errs)

    def test_nc_not_run_and_not_covered_render_partial(self, tmp_path: Path) -> None:
        decl_path, res_path, store_dir, _cat = self._build_graded_fixture(tmp_path, run_evidence=False)
        _, rep = build_parity_report(res_path, decl_path=decl_path, parity_store=store_dir)
        t0 = rep["badge"]["t0"]
        assert "NO RESULT WITHIN-TOLERANCE 0/1" in t0
        assert "partial (1 not run)" in t0
        assert "PASSES" not in t0

        # Manually construct report with a not_covered cell and verify matrix draws it
        rep_nc = json.loads(json.dumps(rep))
        rep_nc["cells"][0]["verdict"] = "not_covered"
        md = render_report_markdown(rep_nc)
        assert "not_covered" in md

    def test_cli_build_and_render(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        decl_path, res_path, store_dir, _cat = self._build_graded_fixture(tmp_path)
        monkeypatch.setenv("BTH_PARITY_STORE", str(store_dir))
        rep_out = tmp_path / "cli_report.json"
        md_out = tmp_path / "cli_report.md"
        app(["parity", "report", "build", str(res_path), "--decl-path", str(decl_path), "--out", str(rep_out)], exit_on_error=False)
        app(["parity", "report", "render", str(rep_out), "--format", "md", "--out", str(md_out)], exit_on_error=False)
        assert rep_out.is_file()
        assert md_out.is_file()
