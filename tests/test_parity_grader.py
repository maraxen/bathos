"""Tests for WP05d: minimal grader, five axes, dual display, grade --verify, and legacy alias."""

from __future__ import annotations

import itertools
import subprocess
import tomllib
from pathlib import Path

import numpy as np
import pytest

from bathos.archived_items import ArchivedItemRecord, append_archived_item_record
from bathos.catalog import init_catalog, write_run
from bathos.compact import compact
from bathos.parity import ParityEvidence, compute_grade, legacy_literature_grade
from bathos.parity.axes import RING0_ACTIVE_G_CODES, compute_axes_and_clauses
from bathos.parity.declare import declare_parity_record
from bathos.parity.evidence import write_cell_evidence
from bathos.parity.grader import ParityVerifyError, grade_parity_record
from bathos.parity.schema import (
    ALL_G_CODES,
    ParityValidationError,
    compute_cell_id,
    render_declaration_scaffold,
)
from bathos.schema import Run


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
        decl_path.write_text(render_declaration_scaffold("demo-record-v1"), encoding="utf-8")
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
