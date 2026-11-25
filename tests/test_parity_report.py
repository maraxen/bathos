"""Tests for WP30a: report core, disclosure tiers T0-T3, badge worked examples, and lints."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import jsonschema
import numpy as np
import pytest

from bathos.catalog import init_catalog, write_run
from bathos.cli_cyclopts import app
from bathos.parity.axes import RING0_ACTIVE_G_CODES, compute_axes_and_clauses
from bathos.parity.badge import (
    ReportLintError,
    lint_rendered_text,
    render_t0,
    render_t1,
    validate_badge,
)
from bathos.parity.declare import declare_parity_record
from bathos.parity.evidence import write_cell_evidence
from bathos.parity.grader import ParityVerifyError, grade_parity_record
from bathos.parity.report import (
    ParityReportVerifyError,
    build_parity_report,
    canonical_report_bytes,
    render_report_html,
    render_report_markdown,
    validate_report_structure,
    verify_parity_report,
)
from bathos.parity.schema import ALL_G_CODES, compute_cell_id, render_declaration_scaffold
from bathos.parity.store import ParityStore
from bathos.schema import Run


def _run_bth_cli(*args: str, env_extra: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    src_dir = str(Path(__file__).resolve().parent.parent / "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = src_dir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "bathos.cli_cyclopts", *args],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


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
            f"owner-stated tolerance · self-reported · std 1.0.0 · T1 {h1[:12]}"
        )
        assert t1 == (
            "PROVISIONAL (21 checks pending) · PASSES WITHIN-TOLERANCE 4/4 · retrospective · "
            "aminx@1234567890ab vs sealed dump frustrampnn-1ubq@3a03cdc300bf (dump ded4dbb6f472) · "
            "layer model · tier toy · pairing fixture · knobs not computed · "
            "gates: 0 enforce, 0 advisory, 3 shadow, 23 not active · waived (3) · "
            f"owner-stated tolerance · self-reported · std 1.0.0 · T2 {h2[:12]}"
        )
        assert validate_badge(t0, t1, body) == []

        # Example 20: tolerance loosened after first declaration (Rev 9: no knobs not computed inside dual-display)
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
            "as first declared: FAILS WITHIN-TOLERANCE 0/4, retrospective"
        ) in t0_20
        assert (
            "as first declared: FAILS WITHIN-TOLERANCE 0/4, retrospective"
        ) in t1_20
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


def _recompute_badge(rep: dict) -> dict:
    body = {k: v for k, v in rep.items() if k != "badge"}
    h2 = hashlib.sha256(canonical_report_bytes(body)).hexdigest()
    t1 = render_t1(body, h2)
    h1 = hashlib.sha256(t1.encode("utf-8")).hexdigest()
    t0 = render_t0(body, h1)
    out = dict(body)
    out["badge"] = {"t0": t0, "t1": t1, "t1_sha256": h1, "t2_sha256": h2}
    return out


def _build_graded_fixture(tmp_path: Path, *, run_evidence: bool = True) -> tuple[Path, Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / ".bth.toml").write_text('[project]\nslug = "demo"\n', encoding="utf-8")
    store_dir = tmp_path / "store"
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)
    decl_path = repo / "demo.parity.bth.toml"
    raw_toml = render_declaration_scaffold("demo-report-v1", impl_name="aminx").replace(
        'kind = "sealed_dump"\n'
        'identity = { dump_sha256 = "0000000000000000000000000000000000000000000000000000000000000000", producer_run_id = "run_0000", upstream_commit = "0000000000000000000000000000000000000000" }',
        'kind = "upstream_live"\n'
        f'identity = {{ repo = "https://github.com/example/upstream", commit = "{"a" * 40}" }}',
    )
    decl_path.write_text(raw_toml, encoding="utf-8")
    decl = tomllib.loads(decl_path.read_text(encoding="utf-8"))
    declare_parity_record(decl_path, parity_store=store_dir)
    if run_evidence:
        cid = _cell_id(decl)
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r1", exit_code=0, status="completed", outcome="pass"),
            cat_dir,
        )
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


class TestReportGenerationAndNegativeControls:
    def _build_graded_fixture(self, tmp_path: Path, *, run_evidence: bool = True) -> tuple[Path, Path, Path, Path]:
        return _build_graded_fixture(tmp_path, run_evidence=run_evidence)

    def test_determinism_hash_links_and_not_computed_sections(self, tmp_path: Path) -> None:
        decl_path, res_path, store_dir, cat_dir = self._build_graded_fixture(tmp_path)
        out1 = tmp_path / "r1.parity.report.json"
        out2 = tmp_path / "r2.parity.report.json"
        _, rep1 = build_parity_report(res_path, decl_path=decl_path, parity_store=store_dir, catalog_dir=cat_dir, output_path=out1)
        _, rep2 = build_parity_report(res_path, decl_path=decl_path, parity_store=store_dir, catalog_dir=cat_dir, output_path=out2)

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

    def test_nc_not_run_and_not_covered_render_partial(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        decl_path, res_path, store_dir, cat_dir = self._build_graded_fixture(tmp_path, run_evidence=False)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(cat_dir))
        _, rep = build_parity_report(res_path, decl_path=decl_path, parity_store=store_dir)
        t0 = rep["badge"]["t0"]
        assert "NO RESULT WITHIN-TOLERANCE 0/1" in t0
        assert "partial (1 not run)" in t0
        assert "PASSES" not in t0

        # Editing result.json to forge verdict="not_covered" MUST be refused by build_parity_report
        decl = tomllib.loads(decl_path.read_text(encoding="utf-8"))
        res_obj = json.loads(res_path.read_text(encoding="utf-8"))
        res_obj["cells"][0]["verdict"] = "not_covered"
        cl, g, cnt = compute_axes_and_clauses(
            declaration=decl,
            cells=res_obj["cells"],
            active_g_codes=RING0_ACTIVE_G_CODES,
            baseline_sha=res_obj["record_sha256"],
            graded_sha=res_obj["record_sha256"],
            has_registered_declaration=False,
        )
        res_obj["clauses"] = cl
        res_obj["gates"] = g
        res_obj["cell_counts"] = cnt
        nc_res_path = tmp_path / "nc.parity.result.json"
        nc_res_path.write_text(json.dumps(res_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with pytest.raises(ParityVerifyError):
            build_parity_report(nc_res_path, decl_path=decl_path, parity_store=store_dir)

        # In-memory report with not_covered and consistent badge renders partial (1 not covered) cleanly
        rep_nc = json.loads(json.dumps(rep))
        rep_nc["cells"][0]["verdict"] = "not_covered"
        rep_nc["not_covered_cells"] = list(rep_nc["cells"])
        rep_nc["clauses"] = cl
        rep_nc["gates"] = g
        rep_nc["cell_counts"] = cnt
        body_nc = {k: v for k, v in rep_nc.items() if k != "badge"}
        h2 = hashlib.sha256(canonical_report_bytes(body_nc)).hexdigest()
        t1_nc = render_t1(body_nc, h2)
        h1 = hashlib.sha256(t1_nc.encode("utf-8")).hexdigest()
        t0_nc = render_t0(body_nc, h1)
        rep_nc["badge"] = {"t0": t0_nc, "t1": t1_nc, "t1_sha256": h1, "t2_sha256": h2}
        assert "partial (1 not covered" in rep_nc["badge"]["t0"]
        assert "partial (1 not covered" in rep_nc["badge"]["t1"]
        md = render_report_markdown(rep_nc)
        html_doc = render_report_html(rep_nc)
        assert "not_covered" in md
        assert "not_covered" in html_doc

    def test_full_ex20_grade_to_report_build_and_t3_fidelity(self, tmp_path: Path) -> None:
        decl_path, _res_path, store_dir, cat_dir = self._build_graded_fixture(tmp_path, run_evidence=False)
        decl = tomllib.loads(decl_path.read_text(encoding="utf-8"))
        cid = _cell_id(decl)
        # First declaration has strict atol=1e-6; evidence fails strict, passes after loosening
        strict_toml = decl_path.read_text(encoding="utf-8").replace("atol = 1e-4", "atol = 1e-6").replace("rtol = 1e-4", "rtol = 0.0")
        decl_path.write_text(strict_toml, encoding="utf-8")
        res_dir = tmp_path / "ex20"
        res_dir.mkdir()
        (res_dir / ".bth.toml").write_text('[project]\nslug = "demo"\n', encoding="utf-8")
        d20 = res_dir / "ex20.parity.bth.toml"
        d20.write_text(strict_toml.replace("demo-report-v1", "aminx-parity-v1"), encoding="utf-8")
        first_decl_res = declare_parity_record(d20, parity_store=store_dir, repo_root=decl_path.parent)
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-ex20", exit_code=0, status="completed", outcome="pass"),
            cat_dir,
        )
        write_cell_evidence(
            store=store_dir,
            record_id="aminx-parity-v1",
            cell_id=cid,
            run_id="r-ex20",
            ours_arrays={"scores": np.array([1.001], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["a" * 64]},
        )
        d20.write_text(d20.read_text(encoding="utf-8").replace("atol = 1e-6", "atol = 1e-2"), encoding="utf-8")
        graded = grade_parity_record(d20, parity_store=store_dir, catalog_dir=cat_dir, as_of="2026-11-24T12:00:00Z", track_run=False)
        _, rep = build_parity_report(graded.result_path, decl_path=d20, parity_store=store_dir, catalog_dir=cat_dir)
        assert "as first declared: FAILS WITHIN-TOLERANCE 0/1" in rep["badge"]["t0"]
        assert "as first declared: FAILS WITHIN-TOLERANCE 0/1" in rep["badge"]["t1"]

        # Hyphenated aminx-parity-v1 must NOT false-trigger bare-parity lint in MD/HTML
        md = render_report_markdown(rep)
        assert "aminx-parity-v1" in md
        html_doc = render_report_html(rep)
        assert "Prior records on pair:" in html_doc
        assert validate_report_structure(rep) == []

        # JSON schema validation of actual report + T3 manifest fidelity
        rep_schema = json.loads(
            importlib.resources.files("bathos").joinpath("schemas", "parity_report.v1.json").read_text(encoding="utf-8")
        )
        jsonschema.validate(instance=rep, schema=rep_schema)

        t3_data = json.loads((ParityStore(store_dir).root / "bundles" / "aminx-parity-v1.parity.bundle.json").read_text(encoding="utf-8"))
        decl_shas = {d["sha256"] for d in t3_data["declarations"]}
        assert first_decl_res.declaration_sha256 in decl_shas
        # Distinct ours and reference entries even when input sha is shared
        assert {(i["sha256"], i["side"]) for i in t3_data["inputs"]} == {("a" * 64, "ours"), ("a" * 64, "reference")}
        assert {(a["side"], a["name"]) for a in t3_data["arrays"]} == {("ours", "scores"), ("reference", "scores")}

    def test_report_lint_error_from_renderers_and_non_provisional_bypass_blocked(self, tmp_path: Path) -> None:
        decl_path, res_path, store_dir, cat_dir = self._build_graded_fixture(tmp_path)
        _, rep = build_parity_report(res_path, decl_path=decl_path, parity_store=store_dir, catalog_dir=cat_dir)

        # NON-PROVISIONAL must NOT satisfy PROVISIONAL check
        assert lint_rendered_text("NON-PROVISIONAL · PASSES WITHIN-TOLERANCE 1/1", checks_pending=True)
        assert lint_rendered_text("Our parity records show 4/4.", checks_pending=False) == []

        # render_report_markdown & render_report_html raise ReportLintError on bare "parity"
        bad_rep = _recompute_badge(json.loads(json.dumps(rep)) | {"discrepancies": "achieves parity on toy"})
        with pytest.raises(ReportLintError, match="bare 'parity'"):
            render_report_markdown(bad_rep)
        with pytest.raises(ReportLintError, match="bare 'parity'"):
            render_report_html(bad_rep)

        # build_parity_report raises ReportLintError when bare 'parity' enters rendered badge via tool name
        bad_decl = decl_path.read_text(encoding="utf-8").replace('name = "aminx"', 'name = "tool achieves parity"')
        bad_dpath = decl_path.parent / "bad_lint.parity.bth.toml"
        bad_dpath.write_text(bad_decl, encoding="utf-8")
        g_bad = grade_parity_record(bad_dpath, parity_store=store_dir, catalog_dir=cat_dir, as_of="2026-11-24T12:00:00Z", track_run=False)
        with pytest.raises(ReportLintError):
            build_parity_report(g_bad.result_path, decl_path=bad_dpath, parity_store=store_dir, catalog_dir=cat_dir)

    def test_validate_badge_failures_exploratory_and_nonhex_rev(self) -> None:
        decl = tomllib.loads(render_declaration_scaffold("badge-checks-v1", impl_name="aminx!"))
        cells = [{"cell_id": "0" * 16, "data_tier": "toy", "pairing": "fixture", "declared_kind": "within_tolerance", "verdict": "pass", "overlays": []}]
        clauses, gates, counts = compute_axes_and_clauses(
            declaration=decl, cells=cells, active_g_codes=RING0_ACTIVE_G_CODES, baseline_sha="a" * 64, graded_sha="b" * 64
        )
        body = {
            "standard_version": "1.0.0",
            "parity_metadata": {"layer_under_test": "model", "badge_min_tier": "toy", "instrumentation": "instrumented", "mode": "exploratory", "has_owner_stated_tolerance": True, "control": False},
            "tool": {"name": "aminx!", "revision": "failed:not_a_repo", "third_party": False},
            "references": decl["reference"],
            "cells": cells,
            "cell_counts": counts,
            "clauses": clauses,
            "gates": gates,
            "dual_display_result": None,
        }
        t1 = render_t1(body, "b" * 64)
        t0 = render_t0(body, "c" * 64)
        assert t0.startswith("EXPLORATORY · ") and t1.startswith("EXPLORATORY · ")
        errs = validate_badge(t0, t1, body)
        assert any("dual_display_result is missing" in e for e in errs)
        assert not any("<IMPL>@<rev>" in e for e in errs)

        # Non-exploratory badge when mode=exploratory fails validate_badge
        body["dual_display_result"] = {"basis": "first_declared", "clauses": clauses}
        t1_ok = render_t1(body, "b" * 64)
        t0_ok = render_t0(body, "c" * 64)
        assert validate_badge(t0_ok, t1_ok, body) == []
        assert any("EXPLORATORY" in e for e in validate_badge(t0_ok.replace("EXPLORATORY · ", ""), t1_ok, body))
        assert any("owner-stated tolerance" in e for e in validate_badge(t0_ok, t1_ok.replace("owner-stated tolerance · ", ""), body))

    def test_html_xss_md_pipe_escaping_and_mismatched_decl_rejected(self, tmp_path: Path) -> None:
        decl_path, res_path, store_dir, cat_dir = self._build_graded_fixture(tmp_path)
        _, rep = build_parity_report(res_path, decl_path=decl_path, parity_store=store_dir, catalog_dir=cat_dir)

        # Mismatched declaration rejected by build_parity_report
        other_decl = decl_path.parent / "other.parity.bth.toml"
        other_decl.write_text(decl_path.read_text(encoding="utf-8").replace("demo-report-v1", "other-report-v1"), encoding="utf-8")
        with pytest.raises(ValueError, match="record_id"):
            build_parity_report(res_path, decl_path=other_decl, parity_store=store_dir, catalog_dir=cat_dir)

        # HTML escapes <script> and MD escapes | in table cells
        xss_rep = json.loads(json.dumps(rep))
        xss_rep["cells"][0]["component_id"] = "<script>alert(1)</script>|pipe"
        xss_rep = _recompute_badge(xss_rep)
        html_doc = render_report_html(xss_rep)
        assert "<script>" not in html_doc
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_doc
        md = render_report_markdown(xss_rep)
        assert "\\|pipe" in md

    def test_cli_build_and_render(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        decl_path, res_path, store_dir, cat_dir = self._build_graded_fixture(tmp_path)
        monkeypatch.setenv("BTH_PARITY_STORE", str(store_dir))
        monkeypatch.setenv("BTH_CATALOG_DIR", str(cat_dir))
        rep_out = tmp_path / "cli_report.json"
        md_out = tmp_path / "cli_report.md"
        html_out = tmp_path / "cli_report.html"
        app(
            [
                "parity",
                "report",
                "build",
                str(res_path),
                "--decl-path",
                str(decl_path),
                "--parity-store",
                str(store_dir),
                "--catalog-dir",
                str(cat_dir),
                "--out",
                str(rep_out),
            ],
            exit_on_error=False,
        )
        app(["parity", "report", "render", str(rep_out), "--format", "md", "--out", str(md_out)], exit_on_error=False)
        assert rep_out.is_file()
        assert md_out.is_file()

        # Subprocess HTML render + CLI error paths exit 1 cleanly
        proc_html = _run_bth_cli("parity", "report", "render", str(rep_out), "--format", "html", "--out", str(html_out), cwd=tmp_path)
        assert proc_html.returncode == 0 and "<table>" in html_out.read_text(encoding="utf-8")

        assert _run_bth_cli("parity", "report", "render", str(rep_out), "--format", "xml", cwd=tmp_path).returncode == 1
        assert _run_bth_cli("parity", "report", "render", str(tmp_path / "missing.json"), cwd=tmp_path).returncode == 1
        bad_json = tmp_path / "bad.json"
        bad_json.write_text("{not-json", encoding="utf-8")
        assert _run_bth_cli("parity", "report", "render", str(bad_json), cwd=tmp_path).returncode == 1
        assert _run_bth_cli("parity", "report", "build", str(bad_json), "--decl-path", str(decl_path), cwd=tmp_path).returncode == 1


class TestReportCoreAndDisclosureTiers:
    def test_b2_edit_result_then_build_report_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        decl_path, res_path, store_dir, cat_dir = _build_graded_fixture(tmp_path, run_evidence=False)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(cat_dir))
        res_obj = json.loads(res_path.read_text(encoding="utf-8"))
        # Hand-edit axes.result.value to PASSES and cells[0]["verdict"] to pass
        res_obj["clauses"][0]["axes"]["result"]["value"] = "PASSES"
        res_obj["cells"][0]["verdict"] = "pass"
        res_path.write_text(json.dumps(res_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with pytest.raises(ParityVerifyError):
            build_parity_report(res_path, decl_path=decl_path, parity_store=store_dir)

    def test_s7_report_verify_tier_chain_cli_and_render_tamper(self, tmp_path: Path) -> None:
        decl_path, res_path, store_dir, cat_dir = _build_graded_fixture(tmp_path)
        rep_path = decl_path.parent / "demo.parity.report.json"
        _, rep = build_parity_report(
            res_path, decl_path=decl_path, parity_store=store_dir, catalog_dir=cat_dir, output_path=rep_path
        )
        # S4: parity_store recorded in report_body and T3 bundle
        assert rep["parity_store"] == {"path": str(store_dir.resolve()), "layer": "cli_arg"}
        store = ParityStore(store_dir)
        t3_obj = json.loads(store.get_bytes(rep["bundle_sha256"]).decode("utf-8"))
        assert t3_obj["parity_store"] == rep["parity_store"]

        # API verify succeeds (with companion discovery and explicit args)
        info = verify_parity_report(rep_path, parity_store=store_dir, catalog_dir=cat_dir)
        assert info["verified"] is True and info["result_verified"] is True and info["present_members"] >= 1

        # CLI `bth parity report verify` succeeds + --json
        proc_v = _run_bth_cli(
            "parity", "report", "verify", str(rep_path),
            "--parity-store", str(store_dir), "--catalog-dir", str(cat_dir), "--json",
            cwd=tmp_path,
        )
        assert proc_v.returncode == 0
        assert json.loads(proc_v.stdout)["verified"] is True

        # Tamper T0 -> TIER_LINK_BROKEN:T0 (and render_report_markdown/html refuse)
        t0_bad = json.loads(rep_path.read_text(encoding="utf-8"))
        t0_bad["badge"]["t0"] = "FORGED"
        with pytest.raises(ParityReportVerifyError, match="TIER_LINK_BROKEN:T0"):
            render_report_markdown(t0_bad)
        with pytest.raises(ParityReportVerifyError, match="TIER_LINK_BROKEN:T0"):
            render_report_html(t0_bad)
        t0_bad_p = tmp_path / "t0_bad.json"
        t0_bad_p.write_text(json.dumps(t0_bad), encoding="utf-8")
        with pytest.raises(ParityReportVerifyError, match="TIER_LINK_BROKEN:T0"):
            verify_parity_report(t0_bad_p, parity_store=store_dir, catalog_dir=cat_dir)

        # Tamper T1 -> TIER_LINK_BROKEN:T1
        t1_bad = json.loads(rep_path.read_text(encoding="utf-8"))
        t1_bad["badge"]["t1"] = "FORGED"
        t1_bad_p = tmp_path / "t1_bad.json"
        t1_bad_p.write_text(json.dumps(t1_bad), encoding="utf-8")
        with pytest.raises(ParityReportVerifyError, match="TIER_LINK_BROKEN:T1"):
            verify_parity_report(t1_bad_p, parity_store=store_dir, catalog_dir=cat_dir)

        # Tamper T2 body -> TIER_LINK_BROKEN:T2
        t2_bad = json.loads(rep_path.read_text(encoding="utf-8"))
        t2_bad["prior_records_on_pair"] = 99
        t2_bad_p = tmp_path / "t2_bad.json"
        t2_bad_p.write_text(json.dumps(t2_bad), encoding="utf-8")
        with pytest.raises(ParityReportVerifyError, match="TIER_LINK_BROKEN:T2"):
            verify_parity_report(t2_bad_p, parity_store=store_dir, catalog_dir=cat_dir)

        # Tamper T3 bundle -> TIER_LINK_BROKEN:T3 and CLI exits 1
        store.blob_path(rep["bundle_sha256"]).unlink()
        with pytest.raises(ParityReportVerifyError, match="TIER_LINK_BROKEN:T3"):
            verify_parity_report(rep_path, parity_store=store_dir, catalog_dir=cat_dir)
        assert _run_bth_cli(
            "parity", "report", "verify", str(rep_path),
            "--parity-store", str(store_dir), "--catalog-dir", str(cat_dir),
            cwd=tmp_path,
        ).returncode == 1

    def test_s7_rev9_badge_multiclause_smoke_and_within_tolerance_lint(self) -> None:
        # Rev 9 L2260: `parity within-tolerance` and `parity within tolerance` allowed; bare `parity` rejected
        assert lint_rendered_text("Achieved parity within-tolerance and parity within tolerance.", checks_pending=False) == []
        assert lint_rendered_text("Achieved parity on smoke.", checks_pending=False)
        assert lint_rendered_text("Achieved within-tolerance parity.", checks_pending=False)

        # Multi-clause badge differing in not_run with knobs_uncovered=2 and tier="smoke"
        decl = tomllib.loads(render_declaration_scaffold("rev9-mc-v1", impl_name="aminx"))
        decl["parity"]["badge_min_tier"] = "smoke"
        cells = [
            {"cell_id": "a" * 16, "data_tier": "smoke", "pairing": "fixture", "declared_kind": "within_tolerance", "verdict": "not_run", "overlays": []},
            {"cell_id": "b" * 16, "data_tier": "smoke", "pairing": "fixture", "declared_kind": "behavioural", "verdict": "pass", "overlays": []},
        ]
        clauses, gates, counts = compute_axes_and_clauses(
            declaration=decl,
            cells=cells,
            active_g_codes=set(ALL_G_CODES),
            has_registered_declaration=True,
            knobs_uncovered=2,
            knobs_non_excluded=5,
            knobs_covered=3,
        )
        body = {
            "standard_version": "1.0.0",
            "parity_metadata": {"layer_under_test": "model", "badge_min_tier": "smoke", "instrumentation": "instrumented", "mode": "confirmatory", "has_owner_stated_tolerance": False, "control": False},
            "tool": {"name": "aminx", "revision": "1234567890ab" + "0" * 28, "third_party": False},
            "references": decl["reference"],
            "cells": cells,
            "cell_counts": counts,
            "clauses": clauses,
            "gates": gates,
            "dual_display_result": None,
            "evidence_level": "self_reported",
        }
        t1 = render_t1(body, "a" * 64)
        h1 = hashlib.sha256(t1.encode("utf-8")).hexdigest()
        t0 = render_t0(body, h1)
        assert "NO RESULT WITHIN-TOLERANCE 0/1, partial (1 not run); PASSES BEHAVIOURAL 1/1 · partial (2 knobs uncovered)" in t0
        assert "tier smoke" in t1
        assert "knobs 3/5" in t1
        assert validate_badge(t0, t1, body) == []

    def test_item1_report_verify_refuses_unreplayed_or_forged_result_with_default_names(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        (repo / ".bth.toml").write_text('[project]\nslug = "demo"\n', encoding="utf-8")
        store_dir = tmp_path / "store"
        cat_dir = tmp_path / "catalog"
        init_catalog(cat_dir)

        decl_path = repo / "frustra_demo.parity.bth.toml"
        raw_toml = render_declaration_scaffold("aminx-demo-v1", impl_name="aminx").replace(
            'kind = "sealed_dump"\n'
            'identity = { dump_sha256 = "0000000000000000000000000000000000000000000000000000000000000000", producer_run_id = "run_0000", upstream_commit = "0000000000000000000000000000000000000000" }',
            'kind = "upstream_live"\n'
            f'identity = {{ repo = "https://github.com/example/upstream", commit = "{"a" * 40}" }}',
        )
        decl_path.write_text(raw_toml, encoding="utf-8")
        decl = tomllib.loads(raw_toml)
        declare_parity_record(decl_path, parity_store=store_dir)
        cid = _cell_id(decl)
        write_run(
            Run(
                project_slug="demo",
                command="bth run ev.py",
                argv=[],
                git_hash="0" * 40,
                git_branch="main",
                git_dirty=False,
                id="r-fail",
                exit_code=0,
                status="completed",
                outcome="pass",
            ),
            cat_dir,
        )
        write_cell_evidence(
            store=store_dir,
            record_id="aminx-demo-v1",
            cell_id=cid,
            run_id="r-fail",
            ours_arrays={"scores": np.array([2.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        graded = grade_parity_record(
            decl_path,
            parity_store=store_dir,
            catalog_dir=cat_dir,
            as_of="2026-11-24T12:00:00Z",
            track_run=False,
        )
        assert graded.result_path.name == "frustra_demo.parity.result.json"
        assert graded.result["clauses"][0]["axes"]["result"]["value"] == "FAILS"

        rep_path, _rep = build_parity_report(
            graded.result_path,
            parity_store=store_dir,
            catalog_dir=cat_dir,
        )
        assert rep_path.name == "aminx-demo-v1.parity.report.json"

        # (a) Missing result file -> ParityReportVerifyError and CLI exit 1 (never VERIFIED)
        orig_res_bytes = graded.result_path.read_bytes()
        graded.result_path.unlink()
        with pytest.raises(ParityReportVerifyError):
            verify_parity_report(rep_path, parity_store=store_dir, catalog_dir=cat_dir)
        proc_missing = _run_bth_cli(
            "parity",
            "report",
            "verify",
            str(rep_path),
            "--parity-store",
            str(store_dir),
            "--catalog-dir",
            str(cat_dir),
            cwd=repo,
        )
        assert proc_missing.returncode == 1
        assert "VERIFIED" not in proc_missing.stdout

        # (b) Forged result + recomputed report_body/T2/T1/T0 -> verify_parity_report refuses without --result-path
        forged_res = json.loads(orig_res_bytes.decode("utf-8"))
        forged_res["clauses"][0]["axes"]["result"]["value"] = "PASSES"
        forged_res_bytes = (json.dumps(forged_res, indent=2, sort_keys=True) + "\n").encode("utf-8")
        graded.result_path.write_bytes(forged_res_bytes)

        forged_rep = json.loads(rep_path.read_text(encoding="utf-8"))
        forged_rep["result_sha256"] = hashlib.sha256(forged_res_bytes).hexdigest()
        forged_rep["clauses"] = forged_res["clauses"]
        forged_rep = _recompute_badge(forged_rep)
        rep_path.write_bytes(canonical_report_bytes(forged_rep))

        with pytest.raises((ParityVerifyError, ParityReportVerifyError)):
            verify_parity_report(rep_path, parity_store=store_dir, catalog_dir=cat_dir)
        proc_forged = _run_bth_cli(
            "parity",
            "report",
            "verify",
            str(rep_path),
            "--parity-store",
            str(store_dir),
            "--catalog-dir",
            str(cat_dir),
            cwd=repo,
        )
        assert proc_forged.returncode == 1
        assert "VERIFIED" not in proc_forged.stdout

    def test_item5_non_sealed_reference_disclosed_on_badge_and_evidence_level_derived(
        self, tmp_path: Path
    ) -> None:
        from bathos.parity.npy_stdlib import encode_npz
        from bathos.parity.sealed_dump import ingest_sealed_dump

        # 1. upstream_live with reference_arrays in manifest -> reference_source=="evidence_bundle", reference_self_reported==True
        (tmp_path / "live").mkdir()
        decl_path, res_path, store_dir, cat_dir = _build_graded_fixture(tmp_path / "live", run_evidence=True)
        res_obj = json.loads(res_path.read_text(encoding="utf-8"))
        assert res_obj["cells"][0]["reference_source"] == "evidence_bundle"
        assert res_obj["cells"][0]["evidence_level"] == "self_reported"
        _, rep_live = build_parity_report(
            res_path, decl_path=decl_path, parity_store=store_dir, catalog_dir=cat_dir
        )
        assert rep_live["parity_metadata"]["reference_self_reported"] is True
        assert "reference self-reported" in rep_live["badge"]["t0"]
        assert "reference self-reported" in rep_live["badge"]["t1"]
        live_body = {k: v for k, v in rep_live.items() if k != "badge"}
        assert validate_badge(rep_live["badge"]["t0"], rep_live["badge"]["t1"], live_body) == []

        # 2. sealed_dump -> reference_source=="sealed_dump", reference_self_reported==False, NOT in T0 or T1
        s_root = tmp_path / "sealed"
        s_repo = s_root / "repo"
        s_repo.mkdir(parents=True)
        _init_git_repo(s_repo)
        (s_repo / ".bth.toml").write_text('[project]\nslug = "demo"\n', encoding="utf-8")
        s_store_dir = s_root / "store"
        s_cat_dir = s_root / "catalog"
        init_catalog(s_cat_dir)
        s_store = ParityStore(s_store_dir)
        dump_bytes = encode_npz({"scores": np.array([1.0, 2.0], dtype=np.float32)})
        dump_sha = hashlib.sha256(dump_bytes).hexdigest()
        dump_p = s_root / "dump.npz"
        dump_p.write_bytes(dump_bytes)
        ingest_sealed_dump(dump_p, expected_sha256=dump_sha, store=s_store, stem="dump")

        s_decl_path = s_repo / "sealed.parity.bth.toml"
        s_toml = render_declaration_scaffold("sealed-badge-v1", impl_name="aminx").replace(
            "0000000000000000000000000000000000000000000000000000000000000000",
            dump_sha,
            1,
        )
        s_decl_path.write_text(s_toml, encoding="utf-8")
        s_decl = tomllib.loads(s_toml)
        declare_parity_record(s_decl_path, parity_store=s_store_dir)
        s_cid = _cell_id(s_decl)
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-sealed", exit_code=0, status="completed", outcome="pass"),
            s_cat_dir,
        )
        write_cell_evidence(
            store=s_store,
            record_id="sealed-badge-v1",
            cell_id=s_cid,
            run_id="r-sealed",
            ours_arrays={"scores": np.array([1.0, 2.0], dtype=np.float32)},
            reference_dump_sha256=dump_sha,
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g_sealed = grade_parity_record(
            s_decl_path, parity_store=s_store_dir, catalog_dir=s_cat_dir, as_of="2026-11-24T12:00:00Z", track_run=False
        )
        assert g_sealed.result["cells"][0]["reference_source"] == "sealed_dump"
        assert g_sealed.result["cells"][0]["evidence_level"] == "self_reported"
        _, rep_sealed = build_parity_report(
            g_sealed.result_path, decl_path=s_decl_path, parity_store=s_store_dir, catalog_dir=s_cat_dir
        )
        assert rep_sealed["parity_metadata"]["reference_self_reported"] is False
        assert "reference self-reported" not in rep_sealed["badge"]["t0"]
        assert "reference self-reported" not in rep_sealed["badge"]["t1"]

        # 3. Derived evidence_level: "none" when not_run; "reexecuted" when reexecuted hook propagates
        (tmp_path / "nr").mkdir()
        nr_decl_path, nr_res_path, nr_store, nr_cat = _build_graded_fixture(tmp_path / "nr", run_evidence=False)
        nr_res = json.loads(nr_res_path.read_text(encoding="utf-8"))
        assert nr_res["cells"][0]["evidence_level"] == "none"

        def _reexec_hook(_cell: dict, _decl: dict) -> list[str]:
            return ["reexecuted"]

        g_re = grade_parity_record(
            s_decl_path,
            parity_store=s_store_dir,
            catalog_dir=s_cat_dir,
            as_of="2026-11-24T12:00:00Z",
            track_run=False,
            overlay_hooks=(_reexec_hook,),
        )
        assert g_re.result["cells"][0]["evidence_level"] == "reexecuted"
        _, rep_re = build_parity_report(
            g_re.result_path,
            decl_path=s_decl_path,
            parity_store=s_store_dir,
            catalog_dir=s_cat_dir,
            overlay_hooks=(_reexec_hook,),
        )
        assert rep_re["evidence_level"] == "reexecuted"
        assert "reexecuted" in rep_re["badge"]["t0"].split(" · ")
        assert "reexecuted" in rep_re["badge"]["t1"].split(" · ")

    def test_a3_weights_attested_only_t1_qualifier(self, tmp_path: Path) -> None:
        (tmp_path / "attested").mkdir()
        decl_path, _res_path, store_dir, cat_dir = _build_graded_fixture(
            tmp_path / "attested", run_evidence=False
        )
        decl_text = decl_path.read_text(encoding="utf-8") + (
            '\n[implementation.weights]\n'
            'artifact_path = "data/weights/example.eqx.zst"\n'
            'source_checkpoint_status = "attested_only_source_deleted"\n'
        )
        decl_path.write_text(decl_text, encoding="utf-8")
        declare_parity_record(decl_path, parity_store=store_dir)
        graded = grade_parity_record(
            decl_path,
            parity_store=store_dir,
            catalog_dir=cat_dir,
            as_of="2026-11-24T12:00:00Z",
            track_run=False,
        )
        _, rep = build_parity_report(
            graded.result_path,
            decl_path=decl_path,
            parity_store=store_dir,
            catalog_dir=cat_dir,
        )
        assert rep["parity_metadata"]["weights_attested_only"] is True
        t0_segs = rep["badge"]["t0"].split(" · ")
        t1_segs = rep["badge"]["t1"].split(" · ")
        assert "weights attested-only" in t1_segs
        assert "weights attested-only" not in t0_segs

        report_body = {k: v for k, v in rep.items() if k != "badge"}
        t1_missing = " · ".join(s for s in t1_segs if s != "weights attested-only")
        errs = validate_badge(rep["badge"]["t0"], t1_missing, report_body)
        assert any("weights attested-only" in e for e in errs)

        md = render_report_markdown(rep)
        html_doc = render_report_html(rep)
        assert "attested-only" in md
        assert "attested-only" in html_doc

        # Negative control: non-attested_only status and missing [implementation.weights] omit the qualifier
        (tmp_path / "local").mkdir()
        decl_local, _res_local, store_local, cat_local = _build_graded_fixture(
            tmp_path / "local", run_evidence=False
        )
        decl_local_text = decl_local.read_text(encoding="utf-8") + (
            '\n[implementation.weights]\n'
            'artifact_path = "data/weights/example.eqx.zst"\n'
            'source_checkpoint_status = "verified_local"\n'
        )
        decl_local.write_text(decl_local_text, encoding="utf-8")
        declare_parity_record(decl_local, parity_store=store_local)
        g_local = grade_parity_record(
            decl_local,
            parity_store=store_local,
            catalog_dir=cat_local,
            as_of="2026-11-24T12:00:00Z",
            track_run=False,
        )
        _, rep_local = build_parity_report(
            g_local.result_path,
            decl_path=decl_local,
            parity_store=store_local,
            catalog_dir=cat_local,
        )
        assert rep_local["parity_metadata"]["weights_attested_only"] is False
        assert "weights attested-only" not in rep_local["badge"]["t1"].split(" · ")
        assert "weights attested-only" not in rep_local["badge"]["t0"].split(" · ")
