"""Tests for WP05d: minimal grader, five axes, dual display, grade --verify, and legacy alias."""

from __future__ import annotations

import hashlib
import importlib.resources
import itertools
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import jsonschema
import numpy as np
import pytest

from bathos.archived_items import (
    ArchivedItemRecord,
    append_archived_item_record,
    write_archived_item_fragment,
)
from bathos.catalog import init_catalog, read_runs, write_run
from bathos.compact import compact
from bathos.parity import ParityEvidence, compute_grade, legacy_literature_grade
from bathos.parity.axes import RING0_ACTIVE_G_CODES, compute_axes_and_clauses
from bathos.parity.declare import declare_parity_record
from bathos.parity.evidence import write_cell_evidence
from bathos.parity.grader import ParityVerifyError, grade_parity_record, register_overlay_hook
from bathos.parity.npy_stdlib import encode_npz
from bathos.parity.schema import (
    ALL_G_CODES,
    ParityValidationError,
    compute_cell_id,
    render_declaration_scaffold,
)
from bathos.parity.sealed_dump import ingest_sealed_dump
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


def _cell_id_from_decl(decl: dict, idx: int = 0) -> str:
    c = decl["cell_plan"][idx]
    return compute_cell_id(
        c["component_id"],
        c["use_id"],
        c["knob_setting"],
        c["axis_value"],
        c["data_tier"],
        c["pairing"],
    )


class TestAxisEnumeration:
    """Table-driven test enumerating combinations across §9.3 verdicts, overlays, and states."""

    @pytest.mark.parametrize(
        "verdict,stale,u_val,x_empty,has_reg,sha_same,is_exp",
        list(
            itertools.product(
                ("pass", "fail", "not_run", "not_covered", "waived", "inconclusive", "contested", "declared_deviation"),
                (False, True),
                (None, 0, 2),
                (True, False),
                (False, True),
                (True, False),
                (False, True),
            )
        ),
    )
    def test_total_five_axis_enumeration(
        self,
        verdict: str,
        stale: bool,
        u_val: int | None,
        x_empty: bool,
        has_reg: bool,
        sha_same: bool,
        is_exp: bool,
    ) -> None:
        decl = tomllib.loads(render_declaration_scaffold("enum-v1"))
        if is_exp:
            decl["reference"][0] = {
                "id": "exp-1",
                "role": "comparator",
                "kind": "experimental_data",
                "identity": {"accession": "PRJNA000", "assay": "dms"},
                "licence": "CC0",
            }
            decl["use"][0]["parity_kind"] = "association"
        active = set(ALL_G_CODES) if x_empty else set(RING0_ACTIVE_G_CODES)
        kind = "association" if is_exp else "within_tolerance"
        cells = [
            {
                "cell_id": "0" * 16,
                "data_tier": "toy",
                "declared_kind": kind,
                "verdict": verdict,
                "overlays": ["stale(code)"] if stale else [],
                "exploratory": False,
            }
        ]
        clauses, gates, _counts = compute_axes_and_clauses(
            declaration=decl,
            cells=cells,
            active_g_codes=active,
            baseline_sha="a" * 64,
            graded_sha="a" * 64 if sha_same else "b" * 64,
            has_registered_declaration=has_reg,
            knobs_uncovered=u_val,
        )
        assert len(gates) == 26
        assert len(clauses) == 1
        axes = clauses[0]["axes"]

        # Exactly one value per axis
        expected_pass = "VALID" if is_exp else "PASSES"
        r_val = axes["result"]["value"]
        if verdict in ("not_run", "not_covered", "waived"):
            assert r_val == "NO RESULT"
        elif verdict == "fail":
            assert r_val == "FAILS"
        elif verdict == "contested":
            assert r_val == "CONTESTED"
        elif verdict == "inconclusive":
            assert r_val == "INCONCLUSIVE"
        elif verdict == "declared_deviation":
            # V is empty when only D exists -> NO RESULT per §9.3.2 ("Deviations alone")
            assert r_val == "NO RESULT"
        else:
            assert r_val == expected_pass

        cov_val = axes["coverage"]["value"]
        if verdict not in ("not_run", "not_covered") and u_val == 0:
            assert cov_val == "full"
        else:
            assert cov_val == "partial"

        assur_val = axes["assurance"]["value"]
        assert assur_val == ("gated" if x_empty else "PROVISIONAL")

        fresh_val = axes["freshness"]["value"]
        in_r_or_d = verdict in ("pass", "fail", "not_run", "inconclusive", "contested", "declared_deviation")
        assert fresh_val == ("stale" if (stale and in_r_or_d) else "current")

        reg_val = axes["registration"]["value"]
        if not sha_same:
            assert reg_val == "amended"
        elif has_reg:
            assert reg_val == "prospective"
        else:
            assert reg_val == "retrospective"


class TestMinimalGraderAndNCs:
    def _setup_workspace(self, tmp_path: Path) -> tuple[Path, Path, Path, Path, dict]:
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        store = tmp_path / "store"
        cat = tmp_path / "catalog"
        init_catalog(cat)
        decl_path = repo / "demo.parity.bth.toml"
        decl = tomllib.loads(render_declaration_scaffold("demo-record-v1"))
        decl["reference"][0] = {
            "id": "upstream-ref",
            "role": "comparator",
            "kind": "upstream_live",
            "identity": {"repo": "https://github.com/example/upstream", "commit": "a" * 40},
            "licence": "MIT",
        }
        # Rewrite TOML using upstream_live for reference_arrays fixture tests
        raw_toml = render_declaration_scaffold("demo-record-v1").replace(
            'kind = "sealed_dump"\n'
            'identity = { dump_sha256 = "0000000000000000000000000000000000000000000000000000000000000000", producer_run_id = "run_0000", upstream_commit = "0000000000000000000000000000000000000000" }',
            'kind = "upstream_live"\n'
            f'identity = {{ repo = "https://github.com/example/upstream", commit = "{"a" * 40}" }}',
        )
        decl_path.write_text(raw_toml, encoding="utf-8")
        decl = tomllib.loads(decl_path.read_text(encoding="utf-8"))
        return repo, store, cat, decl_path, decl

    def test_good_record_passes_and_verify_succeeds(self, tmp_path: Path) -> None:
        _repo, store, cat, decl_path, decl = self._setup_workspace(tmp_path)
        res_decl = declare_parity_record(decl_path, parity_store=store)
        cid = _cell_id_from_decl(decl)
        ours = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        ref = np.array([1.0 + 1e-6, 2.0, 3.0], dtype=np.float32)
        write_cell_evidence(
            store=store,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="run-good",
            ours_arrays={"scores": ours},
            reference_arrays={"scores": ref},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )

        graded = grade_parity_record(
            decl_path, parity_store=store, catalog_dir=cat, as_of="2026-11-24T12:00:00Z"
        )
        assert graded.result["clauses"][0]["axes"]["result"]["value"] == "PASSES"
        assert graded.result["clauses"][0]["axes"]["registration"]["value"] == "retrospective"
        assert graded.result["clauses"][0]["axes"]["assurance"]["pending_count"] == 21
        assert graded.result["grade_inputs"]["target_revisions"]["implementation"] == res_decl.head_commit

        # Verify passes verbatim
        verified = grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, verify=True)
        assert verified.verified is True

        # NC: edited result byte fails --verify
        raw = graded.result_path.read_text(encoding="utf-8")
        graded.result_path.write_text(raw + " ", encoding="utf-8")
        with pytest.raises(ParityVerifyError, match="bytes differ"):
            grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, verify=True)

    def test_nc_g01_and_g02_refusals(self, tmp_path: Path) -> None:
        _repo, store, cat, decl_path, _decl = self._setup_workspace(tmp_path)
        bad_g01 = decl_path.read_text(encoding="utf-8").replace('mode = "confirmatory"\n', "")
        decl_path.write_text(bad_g01, encoding="utf-8")
        with pytest.raises(ParityValidationError, match="mode"):
            grade_parity_record(decl_path, parity_store=store, catalog_dir=cat)

        bad_g02 = render_declaration_scaffold("demo-record-v1").replace('pairing = "fixture"', 'pairing = "live"')
        decl_path.write_text(bad_g02, encoding="utf-8")
        with pytest.raises(ParityValidationError, match="G02_LIVE_PAIRING_FORBIDDEN"):
            grade_parity_record(decl_path, parity_store=store, catalog_dir=cat)

    def test_nc_g04_terminal_without_evidence_nonterminal_and_invariant_fail(self, tmp_path: Path) -> None:
        _repo, store, cat, decl_path, decl = self._setup_workspace(tmp_path)
        declare_parity_record(decl_path, parity_store=store)
        cid = _cell_id_from_decl(decl)

        # 1. Terminal run with no cell evidence -> inconclusive (run_failed)
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-term-empty", exit_code=0, status="completed"),
            cat,
        )
        g1 = grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, cell_runs={cid: ["r-term-empty"]}, track_run=False)
        assert g1.result["cells"][0]["verdict"] == "inconclusive"
        assert "run_failed" in g1.result["cells"][0]["flags"]

        # 2. Non-terminal run -> not_run
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-running", status="running"),
            cat,
        )
        g2 = grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, cell_runs={cid: ["r-running"]}, track_run=False)
        assert g2.result["cells"][0]["verdict"] == "not_run"

        # 3. outcome='invariant_fail' -> fail (invariant_fail) even if array values would match
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-inv", exit_code=0, status="completed", outcome="invariant_fail"),
            cat,
        )
        write_cell_evidence(
            store=store,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r-inv",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g3 = grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, evidence_run_ids=["r-inv"], track_run=False)
        assert g3.result["cells"][0]["verdict"] == "fail"
        assert "invariant_fail" in g3.result["cells"][0]["flags"]

        # 4. Archived run -> excluded via resolve_validity -> not_run
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-arch", exit_code=0, status="completed", outcome="pass"),
            cat,
        )
        write_cell_evidence(
            store=store,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r-arch",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        compact(cat)
        append_archived_item_record(
            ArchivedItemRecord(
                id="arch-item-1",
                project_slug="demo",
                event="archived",
                run_ids=["r-arch"],
                reason="test archive",
            ),
            cat,
        )
        g4 = grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, evidence_run_ids=["r-arch"], track_run=False)
        assert g4.result["cells"][0]["verdict"] == "not_run"

    def test_nc_g10_kind_shortfall_and_dual_display_after_loosening(self, tmp_path: Path) -> None:
        _repo, store, cat, decl_path, decl = self._setup_workspace(tmp_path)
        # First declaration has strict atol = 1e-6
        strict_toml = decl_path.read_text(encoding="utf-8").replace("atol = 1e-4", "atol = 1e-6").replace("rtol = 1e-4", "rtol = 0.0")
        decl_path.write_text(strict_toml, encoding="utf-8")
        declare_parity_record(decl_path, parity_store=store)

        cid = _cell_id_from_decl(decl)
        write_cell_evidence(
            store=store,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r1",
            ours_arrays={"scores": np.array([1.001], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        first_grade = grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, track_run=False)
        assert first_grade.result["cells"][0]["verdict"] == "fail"
        assert "kind_shortfall" in first_grade.result["cells"][0]["flags"]

        # Loosen tolerance after first declaration -> passes now, amended, dual_display FAILS
        loose_toml = strict_toml.replace("atol = 1e-6", "atol = 1e-2")
        decl_path.write_text(loose_toml, encoding="utf-8")
        second_grade = grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, track_run=False)
        assert second_grade.result["clauses"][0]["axes"]["result"]["value"] == "PASSES"
        assert second_grade.result["clauses"][0]["axes"]["registration"]["value"] == "amended"
        dual = second_grade.result["dual_display_result"]
        assert dual is not None
        assert dual["basis"] == "first_declared"
        assert dual["clauses"][0]["axes"]["result"]["value"] == "FAILS"

    def test_legacy_literature_grade_alias(self) -> None:
        ev = ParityEvidence(
            clause_parity_pct=1.0,
            adversarial_survived=True,
            invariant_pass=True,
            reproduction_rung="R0",
            ambiguity_load="none",
        )
        assert legacy_literature_grade(ev) == compute_grade(ev)

    def test_nc_contested_precedes_inconclusive_in_same_clause(self) -> None:
        decl = tomllib.loads(render_declaration_scaffold("step-order-v1"))
        cells = [
            {"cell_id": "0" * 16, "data_tier": "toy", "pairing": "fixture", "declared_kind": "within_tolerance", "verdict": "inconclusive", "overlays": []},
            {"cell_id": "1" * 16, "data_tier": "toy", "pairing": "fixture", "declared_kind": "within_tolerance", "verdict": "contested", "overlays": []},
        ]
        clauses, _gates, _counts = compute_axes_and_clauses(
            declaration=decl, cells=cells, active_g_codes=RING0_ACTIVE_G_CODES
        )
        assert clauses[0]["axes"]["result"]["value"] == "CONTESTED"

    def test_axis_merging_sums_counts_and_step5_and_tier_and_exploratory(self) -> None:
        decl = tomllib.loads(render_declaration_scaffold("merge-v1"))
        # Two kinds with identical 5 axis values but differing not_run, not_covered, stale_count, d_count
        cells = [
            {"cell_id": "0" * 16, "data_tier": "toy", "pairing": "fixture", "declared_kind": "bitwise", "verdict": "pass", "overlays": ["stale(code)"]},
            {"cell_id": "1" * 16, "data_tier": "toy", "pairing": "fixture", "declared_kind": "bitwise", "verdict": "not_run", "overlays": []},
            {"cell_id": "2" * 16, "data_tier": "toy", "pairing": "fixture", "declared_kind": "bitwise", "verdict": "declared_deviation", "overlays": []},
            {"cell_id": "3" * 16, "data_tier": "toy", "pairing": "fixture", "declared_kind": "within_tolerance", "verdict": "pass", "overlays": ["stale(code)"]},
            {"cell_id": "4" * 16, "data_tier": "toy", "pairing": "fixture", "declared_kind": "within_tolerance", "verdict": "pass", "overlays": ["stale(code)"]},
            {"cell_id": "5" * 16, "data_tier": "toy", "pairing": "fixture", "declared_kind": "within_tolerance", "verdict": "not_covered", "overlays": []},
            {"cell_id": "6" * 16, "data_tier": "toy", "pairing": "fixture", "declared_kind": "within_tolerance", "verdict": "declared_deviation", "overlays": []},
            {"cell_id": "7" * 16, "data_tier": "toy", "pairing": "fixture", "declared_kind": "within_tolerance", "verdict": "declared_deviation", "overlays": []},
        ]
        clauses, _gates, _counts = compute_axes_and_clauses(
            declaration=decl, cells=cells, active_g_codes=RING0_ACTIVE_G_CODES, knobs_uncovered=None
        )
        assert len(clauses) == 1
        cl = clauses[0]
        assert cl["kinds"] == ["bitwise", "within_tolerance"]
        assert cl["axes"]["result"]["value"] == "PASSES EXCEPT DECLARED DEVIATIONS (3)"
        assert cl["axes"]["coverage"]["not_run"] == 1
        assert cl["axes"]["coverage"]["not_covered"] == 1
        assert cl["axes"]["freshness"]["stale_count"] == 3
        assert cl["passing"] == 3 and cl["required"] == 4

        # experimental_data -> VALID EXCEPT DECLARED DEVIATIONS (k)
        decl_exp = tomllib.loads(render_declaration_scaffold("merge-exp-v1"))
        decl_exp["reference"][0] = {
            "id": "exp-1", "role": "comparator", "kind": "experimental_data",
            "identity": {"accession": "PRJNA1", "assay": "dms"}, "licence": "CC0",
        }
        cl_exp, _, _ = compute_axes_and_clauses(
            declaration=decl_exp,
            cells=[
                {"cell_id": "a" * 16, "data_tier": "toy", "pairing": "fixture", "declared_kind": "association", "verdict": "pass", "overlays": []},
                {"cell_id": "b" * 16, "data_tier": "toy", "pairing": "fixture", "declared_kind": "association", "verdict": "declared_deviation", "overlays": []},
            ],
        )
        assert cl_exp[0]["axes"]["result"]["value"] == "VALID EXCEPT DECLARED DEVIATIONS (1)"

        # badge_min_tier="production_shape" filters out toy/smoke; "smoke" keeps smoke
        decl["parity"]["badge_min_tier"] = "production_shape"
        cl_prod, _, _ = compute_axes_and_clauses(
            declaration=decl,
            cells=[
                {"cell_id": "a" * 16, "data_tier": "toy", "pairing": "fixture", "declared_kind": "within_tolerance", "verdict": "fail", "overlays": []},
                {"cell_id": "b" * 16, "data_tier": "production_shape", "pairing": "fixture", "declared_kind": "within_tolerance", "verdict": "pass", "overlays": []},
            ],
        )
        assert cl_prod[0]["passing"] == 1 and cl_prod[0]["required"] == 1

        decl["parity"]["badge_min_tier"] = "smoke"
        cl_smoke, _, _ = compute_axes_and_clauses(
            declaration=decl,
            cells=[{"cell_id": "a" * 16, "data_tier": "smoke", "pairing": "fixture", "declared_kind": "within_tolerance", "verdict": "pass", "overlays": []}],
        )
        assert cl_smoke[0]["passing"] == 1

    def test_sealed_dump_grading_and_spoofing_nc(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        store_dir = tmp_path / "store"
        cat = tmp_path / "catalog"
        init_catalog(cat)
        store = ParityStore(store_dir)

        # Ingest real oracle dump [100.0] and fake dump [1.0]
        real_bytes = encode_npz({"scores": np.array([100.0], dtype=np.float32)})
        real_sha = hashlib.sha256(real_bytes).hexdigest()
        real_p = tmp_path / "real.npz"
        real_p.write_bytes(real_bytes)
        ingest_sealed_dump(real_p, expected_sha256=real_sha, store=store, stem="real")

        fake_bytes = encode_npz({"scores": np.array([1.0], dtype=np.float32)})
        fake_sha = hashlib.sha256(fake_bytes).hexdigest()
        fake_p = tmp_path / "fake.npz"
        fake_p.write_bytes(fake_bytes)
        ingest_sealed_dump(fake_p, expected_sha256=fake_sha, store=store, stem="fake")

        decl_toml = render_declaration_scaffold("sealed-demo-v1").replace(
            "0000000000000000000000000000000000000000000000000000000000000000",
            real_sha,
            1,
        )
        decl_path = repo / "sealed.parity.bth.toml"
        decl_path.write_text(decl_toml, encoding="utf-8")
        decl = tomllib.loads(decl_toml)
        declare_parity_record(decl_path, parity_store=store_dir)
        cid = _cell_id_from_decl(decl)

        # Spoof attempt 1: self-reported reference_arrays=[1.0] matching ours=[1.0] MUST NOT override sealed_dump
        write_cell_evidence(
            store=store,
            record_id="sealed-demo-v1",
            cell_id=cid,
            run_id="r-spoof-bundle",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g_spoof1 = grade_parity_record(
            decl_path, parity_store=store_dir, catalog_dir=cat, evidence_run_ids=["r-spoof-bundle"], track_run=False
        )
        assert g_spoof1.result["cells"][0]["verdict"] == "fail"
        assert "kind_shortfall" in g_spoof1.result["cells"][0]["flags"]

        # Spoof attempt 2: mismatched reference_dump_sha256=fake_sha MUST fail with fixture_mismatch
        write_cell_evidence(
            store=store,
            record_id="sealed-demo-v1",
            cell_id=cid,
            run_id="r-spoof-dump",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_dump_sha256=fake_sha,
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g_spoof2 = grade_parity_record(
            decl_path, parity_store=store_dir, catalog_dir=cat, evidence_run_ids=["r-spoof-dump"], track_run=False
        )
        assert g_spoof2.result["cells"][0]["verdict"] == "fail"
        assert g_spoof2.result["cells"][0]["flags"] == ["fixture_mismatch"]

        # Valid sealed_dump grading passes
        write_cell_evidence(
            store=store,
            record_id="sealed-demo-v1",
            cell_id=cid,
            run_id="r-ok",
            ours_arrays={"scores": np.array([100.0], dtype=np.float32)},
            reference_dump_sha256=real_sha,
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g_ok = grade_parity_record(
            decl_path, parity_store=store_dir, catalog_dir=cat, evidence_run_ids=["r-ok"], track_run=False
        )
        assert g_ok.result["cells"][0]["verdict"] == "pass"

    def test_verify_tamper_resistance_and_cell_runs_and_decl_edit(self, tmp_path: Path) -> None:
        _repo, store, cat, decl_path, decl = self._setup_workspace(tmp_path)
        declare_parity_record(decl_path, parity_store=store)
        cid = _cell_id_from_decl(decl)
        write_cell_evidence(
            store=store,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r1",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        graded = grade_parity_record(
            decl_path, parity_store=store, catalog_dir=cat, cell_runs={cid: ["r1"]}, as_of="2026-11-24T12:00:00Z"
        )
        # cell_runs persisted -> --verify without passing cell_runs succeeds
        assert graded.result["grade_inputs"]["cell_runs"] == {cid: ["r1"]}
        assert grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, verify=True).verified is True
        # track_run=True wrote a Run into catalog
        assert graded.grade_run_id is not None
        assert any(r.id == graded.grade_run_id for r in read_runs(cat))

        # Tampering gate_stages or comparison_backend fails --verify
        obj = json.loads(graded.result_path.read_text(encoding="utf-8"))
        obj["grade_inputs"]["gate_stages"]["G01"] = "enforce"
        obj["gates"][0]["stage"] = "enforce"
        graded.result_path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with pytest.raises(ParityVerifyError):
            grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, verify=True)

        # Restore valid result, then add a new run to store -> fresh discovery detects divergence
        grade_parity_record(
            decl_path, parity_store=store, catalog_dir=cat, as_of="2026-11-24T12:00:00Z", track_run=False
        )
        write_cell_evidence(
            store=store,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r2-added",
            ours_arrays={"scores": np.array([99.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        with pytest.raises(ParityVerifyError):
            grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, verify=True)

        # Edited declaration fails --verify
        decl_path.write_text(decl_path.read_text(encoding="utf-8").replace("atol = 1e-4", "atol = 1e-2"), encoding="utf-8")
        with pytest.raises(ParityVerifyError):
            grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, verify=True)

    def test_g04_g10_edge_cases_bitwise_non_invariant_tol_selection_and_cool_validity(self, tmp_path: Path) -> None:
        _repo, store_dir, cat, decl_path, decl = self._setup_workspace(tmp_path)
        declare_parity_record(decl_path, parity_store=store_dir)
        cid = _cell_id_from_decl(decl)

        # 1. Non-invariant fail run with good arrays -> PASSES
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-fail-ok", exit_code=1, status="failed", outcome="fail"),
            cat,
        )
        write_cell_evidence(
            store=store_dir,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r-fail-ok",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g_ok = grade_parity_record(decl_path, parity_store=store_dir, catalog_dir=cat, evidence_run_ids=["r-fail-ok"], track_run=False)
        assert g_ok.result["cells"][0]["verdict"] == "pass"

        # 2. explicit cell_runs with invariant_fail (no manifest) -> fail (invariant_fail)
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-exp-inv", exit_code=1, status="failed", outcome="invariant_fail"),
            cat,
        )
        g_inv = grade_parity_record(decl_path, parity_store=store_dir, catalog_dir=cat, cell_runs={cid: ["r-exp-inv"]}, evidence_run_ids=[], track_run=False)
        assert g_inv.result["cells"][0]["verdict"] == "fail"
        assert g_inv.result["cells"][0]["flags"] == ["invariant_fail"]

        # 3. Cool-tier archived run (NO bathos.db!) -> resolve_validity still hides it -> not_run
        cool_cat = tmp_path / "cool_only_cat"
        cool_cat.mkdir()
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-cool-arch", exit_code=0, status="completed", outcome="pass"),
            cool_cat,
        )
        write_archived_item_fragment(
            ArchivedItemRecord(id="arch-cool", project_slug="demo", event="archived", run_ids=["r-cool-arch"], reason="cool archive"),
            cool_cat,
        )
        write_cell_evidence(
            store=store_dir,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r-cool-arch",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        assert not (cool_cat / "bathos.db").exists()
        g_cool = grade_parity_record(decl_path, parity_store=store_dir, catalog_dir=cool_cat, evidence_run_ids=["r-cool-arch"], track_run=False)
        assert g_cool.result["cells"][0]["verdict"] == "not_run"

        # 4. Non-finite / shape mismatch -> max_abs_diff is None (not 0.0)
        write_cell_evidence(
            store=store_dir,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r-shape",
            ours_arrays={"scores": np.array([1.0, 2.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g_shape = grade_parity_record(decl_path, parity_store=store_dir, catalog_dir=cat, evidence_run_ids=["r-shape"], track_run=False)
        assert g_shape.result["cells"][0]["verdict"] == "fail"
        assert g_shape.result["cells"][0]["metric_values"]["max_abs_diff"] is None

        # 5. Mismatched tolerance dtype -> fails with basis_insufficient (no tolerances[0] fallback)
        write_cell_evidence(
            store=store_dir,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r-f64",
            ours_arrays={"scores": np.array([1.0], dtype=np.float64)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float64)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g_tol = grade_parity_record(decl_path, parity_store=store_dir, catalog_dir=cat, evidence_run_ids=["r-f64"], track_run=False)
        assert g_tol.result["cells"][0]["verdict"] == "fail"
        assert g_tol.result["cells"][0]["flags"] == ["basis_insufficient"]

        # 6. Extra key in ref_arrays missing from ours_arrays -> fixture_mismatch
        write_cell_evidence(
            store=store_dir,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r-missing-ours",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32), "extra": np.array([2.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g_miss = grade_parity_record(decl_path, parity_store=store_dir, catalog_dir=cat, evidence_run_ids=["r-missing-ours"], track_run=False)
        assert g_miss.result["cells"][0]["verdict"] == "fail"
        assert g_miss.result["cells"][0]["flags"] == ["fixture_mismatch"]

        # 7. Bitwise +0.0 vs -0.0 graded through grade_parity_record() -> fail (kind_shortfall)
        bw_toml = decl_path.read_text(encoding="utf-8").replace('parity_kind = "within_tolerance"', 'parity_kind = "bitwise"')
        bw_path = tmp_path / "repo" / "bw.parity.bth.toml"
        bw_path.write_text(bw_toml, encoding="utf-8")
        bw_decl = tomllib.loads(bw_toml)
        bw_cid = _cell_id_from_decl(bw_decl)
        write_cell_evidence(
            store=store_dir,
            record_id="demo-record-v1",
            cell_id=bw_cid,
            run_id="r-bw-zero",
            ours_arrays={"scores": np.array([0.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([-0.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g_bw = grade_parity_record(bw_path, parity_store=store_dir, catalog_dir=cat, evidence_run_ids=["r-bw-zero"], track_run=False)
        assert g_bw.result["cells"][0]["verdict"] == "fail"
        assert g_bw.result["cells"][0]["flags"] == ["kind_shortfall"]

    def test_prior_runs_prior_records_on_pair_overlay_hooks_and_cli_grade(self, tmp_path: Path) -> None:
        repo, store_dir, cat, decl_path, _decl = self._setup_workspace(tmp_path)
        # Declare a second distinct record sharing (implementation.repo, comparator identity)
        other_path = repo / "other.parity.bth.toml"
        other_path.write_text(
            decl_path.read_text(encoding="utf-8").replace("demo-record-v1", "other-record-v1"), encoding="utf-8"
        )
        declare_parity_record(other_path, parity_store=store_dir)

        # Non-empty prior_runs on first declaration -> retrospective + results_seen=True + prior_records_on_pair=1
        p_toml = decl_path.read_text(encoding="utf-8").replace("prior_runs = []", 'prior_runs = ["run_prev"]')
        decl_path.write_text(p_toml, encoding="utf-8")
        declare_parity_record(decl_path, parity_store=store_dir)

        import bathos.parity.grader as grader_mod

        orig_hooks = list(grader_mod._REGISTERED_OVERLAY_HOOKS)
        try:
            register_overlay_hook(lambda _cell, _decl: ["stale(code)"])
            g = grade_parity_record(
                decl_path, parity_store=store_dir, catalog_dir=cat, as_of="2026-11-24T12:00:00Z", track_run=False
            )
            assert g.result["results_seen"] is True
            assert g.result["prior_records_on_pair"] == 1
            assert g.result["clauses"][0]["axes"]["registration"]["value"] == "retrospective"
            assert "stale(code)" in g.result["cells"][0]["overlays"]
            schema = json.loads(
                importlib.resources.files("bathos").joinpath("schemas", "parity_result.v1.json").read_text(encoding="utf-8")
            )
            jsonschema.validate(instance=g.result, schema=schema)
        finally:
            grader_mod._REGISTERED_OVERLAY_HOOKS[:] = orig_hooks

        # Subprocess CLI `bth parity grade` emits PROVISIONAL · ... WITHIN-TOLERANCE (never bare PASSES WITHIN_TOLERANCE)
        proc = _run_bth_cli(
            "parity", "grade", str(decl_path), "--parity-store", str(store_dir), "--catalog-dir", str(cat), "--no-track", "--as-of", "2026-11-24T12:00:00Z",
            cwd=repo,
        )
        assert proc.returncode == 0
        assert "PROVISIONAL · " in proc.stdout
        assert "WITHIN-TOLERANCE" in proc.stdout
        assert "WITHIN_TOLERANCE" not in proc.stdout

        proc_verify = _run_bth_cli(
            "parity", "grade", str(decl_path), "--parity-store", str(store_dir), "--catalog-dir", str(cat), "--verify",
            cwd=repo,
        )
        assert proc_verify.returncode == 0 and "VERIFIED:" in proc_verify.stdout

        proc_err = _run_bth_cli("parity", "grade", str(tmp_path / "missing.toml"), cwd=repo)
        assert proc_err.returncode == 1
