"""Tests for WP05d: minimal grader, five axes, dual display, grade --verify, and legacy alias."""

from __future__ import annotations

import hashlib
import importlib.resources
import io
import itertools
import json
import os
import subprocess
import sys
import tomllib
import zipfile
from datetime import UTC, datetime, timedelta
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
from bathos.parity.declare import ParityDeclareError, declare_parity_record
from bathos.parity.evidence import CellEvidenceError, write_cell_evidence
from bathos.parity.grader import ParityVerifyError, grade_parity_record, register_overlay_hook
from bathos.parity.npy_stdlib import canonical_array_digest, encode_npz
from bathos.parity.schema import (
    ALL_G_CODES,
    ParityValidationError,
    compute_cell_id,
    render_declaration_scaffold,
)
from bathos.parity.sealed_dump import ingest_sealed_dump
from bathos.parity.store import ParityStore, atomic_write_json
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
        (repo / ".bth.toml").write_text('[project]\nslug = "demo"\n', encoding="utf-8")
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
        # Note: previously passed without registering a Run row in cat; B1 requires a terminal Run in catalog.
        _repo, store, cat, decl_path, decl = self._setup_workspace(tmp_path)
        res_decl = declare_parity_record(decl_path, parity_store=store)
        cid = _cell_id_from_decl(decl)
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="run-good", exit_code=0, status="completed", outcome="pass"),
            cat,
        )
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

    def test_b1_unknown_run_and_missing_catalog_give_not_run(self, tmp_path: Path) -> None:
        _repo, store, cat, decl_path, decl = self._setup_workspace(tmp_path)
        declare_parity_record(decl_path, parity_store=store)
        cid = _cell_id_from_decl(decl)
        write_cell_evidence(
            store=store,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="run-unknown-not-in-catalog",
            ours_arrays={"scores": np.array([1.0, 2.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0, 2.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )

        g_unknown = grade_parity_record(
            decl_path, parity_store=store, catalog_dir=cat, track_run=False
        )
        assert g_unknown.result["cells"][0]["verdict"] == "not_run"
        assert g_unknown.result["evidence_runs"][0]["status"] == "unknown"
        assert g_unknown.result["evidence_runs"][0]["validity_state"] == "unknown"

        missing_cat = tmp_path / "does_not_exist_catalog"
        g_missing = grade_parity_record(
            decl_path, parity_store=store, catalog_dir=missing_cat, track_run=False
        )
        assert g_missing.result["cells"][0]["verdict"] == "not_run"

    def test_b3_zero_length_and_all_nan_through_grader_never_pass(self, tmp_path: Path) -> None:
        _repo, store, cat, decl_path, decl = self._setup_workspace(tmp_path)
        declare_parity_record(decl_path, parity_store=store)
        cid = _cell_id_from_decl(decl)
        for rid, arr in (
            ("r-empty", np.array([], dtype=np.float32)),
            ("r-all-nan", np.array([np.nan, np.inf], dtype=np.float32)),
        ):
            write_run(
                Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id=rid, exit_code=0, status="completed", outcome="pass"),
                cat,
            )
            write_cell_evidence(
                store=store,
                record_id="demo-record-v1",
                cell_id=cid,
                run_id=rid,
                ours_arrays={"scores": arr},
                reference_arrays={"scores": arr},
                input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
            )
            g = grade_parity_record(
                decl_path, parity_store=store, catalog_dir=cat, evidence_run_ids=[rid], track_run=False
            )
            assert g.result["cells"][0]["verdict"] == "inconclusive"
            assert "degenerate_capture" in g.result["cells"][0]["flags"]
            assert g.result["cells"][0]["nondegeneracy"]["status"] == "degenerate"

    def test_b4_sealed_dump_subset_and_manifest_map_mismatch_fail(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        (repo / ".bth.toml").write_text('[project]\nslug = "demo"\n', encoding="utf-8")
        store_dir = tmp_path / "store"
        cat = tmp_path / "catalog"
        init_catalog(cat)
        store = ParityStore(store_dir)

        dump_bytes = encode_npz(
            {
                "logits": np.array([1.0, 2.0], dtype=np.float32),
                "hidden": np.array([3.0, 4.0], dtype=np.float32),
            }
        )
        dump_sha = hashlib.sha256(dump_bytes).hexdigest()
        dump_path = tmp_path / "multi.npz"
        dump_path.write_bytes(dump_bytes)
        ingest_sealed_dump(dump_path, expected_sha256=dump_sha, store=store, stem="multi")

        base_toml = render_declaration_scaffold("sealed-b4-v1").replace(
            "0000000000000000000000000000000000000000000000000000000000000000",
            dump_sha,
            1,
        )
        decl_path = repo / "b4.parity.bth.toml"
        decl_path.write_text(base_toml, encoding="utf-8")
        decl = tomllib.loads(base_toml)
        declare_parity_record(decl_path, parity_store=store_dir)
        cid = _cell_id_from_decl(decl)

        # 1. Dump has logits + hidden; port supplies only hidden -> fail + fixture_mismatch
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-subset", exit_code=0, status="completed", outcome="pass"),
            cat,
        )
        write_cell_evidence(
            store=store,
            record_id="sealed-b4-v1",
            cell_id=cid,
            run_id="r-subset",
            ours_arrays={"hidden": np.array([3.0, 4.0], dtype=np.float32)},
            reference_dump_sha256=dump_sha,
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g_sub = grade_parity_record(
            decl_path, parity_store=store_dir, catalog_dir=cat, evidence_run_ids=["r-subset"], track_run=False
        )
        assert g_sub.result["cells"][0]["verdict"] == "fail"
        assert g_sub.result["cells"][0]["flags"] == ["fixture_mismatch"]

        # 2. Legacy dump_keys rejected in declaration (G01_FORBIDDEN_FIELD)
        bad_dump_keys_toml = base_toml.replace(
            'tolerance_id = "f32-owner"',
            'tolerance_id = "f32-owner"\ndump_keys = { out = "logits" }',
        )
        bad_dk_path = repo / "b4_dk.parity.bth.toml"
        bad_dk_path.write_text(bad_dump_keys_toml, encoding="utf-8")
        with pytest.raises(ParityValidationError, match="G01_FORBIDDEN_FIELD"):
            grade_parity_record(bad_dk_path, parity_store=store_dir, catalog_dir=cat, track_run=False)

        # 3. Declaration with reference_members: manifest reference_dump_keys mismatch -> fail + fixture_mismatch
        rm_toml = base_toml.replace(
            'tolerance_id = "f32-owner"',
            'tolerance_id = "f32-owner"\nreference_members = { out = "logits" }',
        )
        rm_path = repo / "b4_rm.parity.bth.toml"
        rm_path.write_text(rm_toml, encoding="utf-8")
        rm_decl = tomllib.loads(rm_toml)
        declare_parity_record(rm_path, parity_store=store_dir)
        rm_cid = _cell_id_from_decl(rm_decl)
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-map-mismatch", exit_code=0, status="completed", outcome="pass"),
            cat,
        )
        write_cell_evidence(
            store=store,
            record_id="sealed-b4-v1",
            cell_id=rm_cid,
            run_id="r-map-mismatch",
            ours_arrays={"out": np.array([3.0, 4.0], dtype=np.float32)},
            reference_dump_sha256=dump_sha,
            reference_dump_keys={"out": "hidden"},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g_map = grade_parity_record(
            rm_path, parity_store=store_dir, catalog_dir=cat, evidence_run_ids=["r-map-mismatch"], track_run=False
        )
        assert g_map.result["cells"][0]["verdict"] == "fail"
        assert g_map.result["cells"][0]["flags"] == ["fixture_mismatch"]

        # 4. Missing dump member referenced by declaration reference_members -> fail + fixture_mismatch
        miss_toml = base_toml.replace(
            'tolerance_id = "f32-owner"',
            'tolerance_id = "f32-owner"\nreference_members = { out = "nonexistent_member" }',
        )
        miss_path = repo / "b4_miss.parity.bth.toml"
        miss_path.write_text(miss_toml, encoding="utf-8")
        miss_decl = tomllib.loads(miss_toml)
        declare_parity_record(miss_path, parity_store=store_dir)
        miss_cid = _cell_id_from_decl(miss_decl)
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-miss-member", exit_code=0, status="completed", outcome="pass"),
            cat,
        )
        write_cell_evidence(
            store=store,
            record_id="sealed-b4-v1",
            cell_id=miss_cid,
            run_id="r-miss-member",
            ours_arrays={"out": np.array([1.0, 2.0], dtype=np.float32)},
            reference_dump_sha256=dump_sha,
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g_miss = grade_parity_record(
            miss_path, parity_store=store_dir, catalog_dir=cat, evidence_run_ids=["r-miss-member"], track_run=False
        )
        assert g_miss.result["cells"][0]["verdict"] == "fail"
        assert g_miss.result["cells"][0]["flags"] == ["fixture_mismatch"]

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
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r1", exit_code=0, status="completed", outcome="pass"),
            cat,
        )
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
        (repo / ".bth.toml").write_text('[project]\nslug = "demo"\n', encoding="utf-8")
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
        for rid_s in ("r-spoof-bundle", "r-spoof-dump", "r-ok"):
            write_run(
                Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id=rid_s, exit_code=0, status="completed", outcome="pass"),
                cat,
            )
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
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r1", exit_code=0, status="completed", outcome="pass"),
            cat,
        )
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r2-added", exit_code=0, status="completed", outcome="pass"),
            cat,
        )
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

        # 1. Non-invariant fail run (status="completed", outcome="fail") with good arrays -> PASSES
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-fail-ok", exit_code=1, status="completed", outcome="fail"),
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
        for rid_e in ("r-shape", "r-f64", "r-missing-ours", "r-bw-zero"):
            write_run(
                Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id=rid_e, exit_code=0, status="completed", outcome="pass"),
                cat,
            )
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

    def test_s7_raw_zipfile_cell_evidence_and_digest_tamper(self, tmp_path: Path) -> None:
        _repo, store_dir, cat, decl_path, decl = self._setup_workspace(tmp_path)
        declare_parity_record(decl_path, parity_store=store_dir)
        cid = _cell_id_from_decl(decl)
        store = ParityStore(store_dir)

        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-raw", exit_code=0, status="completed", outcome="pass"),
            cat,
        )

        def _make_raw_npz(arr: np.ndarray) -> tuple[str, dict]:
            npy_buf = io.BytesIO()
            np.save(npy_buf, arr, allow_pickle=False)
            npy_bytes = npy_buf.getvalue()
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("scores.npy", npy_bytes)
            bundle_sha = store.put_bytes(zip_buf.getvalue())
            le = arr.astype(arr.dtype.newbyteorder("<"), copy=False) if arr.dtype.byteorder == ">" else arr
            le_c = np.ascontiguousarray(le)
            desc = {
                "scores": {
                    "member_name": "scores.npy",
                    "dtype": le_c.dtype.str,
                    "shape": list(le_c.shape),
                    "canonical_digest": canonical_array_digest(le_c.dtype.str, le_c.shape, le_c.tobytes("C")),
                }
            }
            return bundle_sha, desc

        ours_arr = np.array([1.0, 2.0], dtype=np.float32)
        ref_arr = np.array([1.0, 2.0], dtype=np.float32)
        ours_sha, ours_desc = _make_raw_npz(ours_arr)
        ref_sha, ref_desc = _make_raw_npz(ref_arr)

        manifest = {
            "schema_version": "cell_evidence.v1",
            "record_id": "demo-record-v1",
            "cell_id": cid,
            "run_id": "r-raw",
            "input_sha256": {"ours": ["a" * 64], "reference": ["b" * 64]},
            "ours_bundle_sha256": ours_sha,
            "ours_arrays": ours_desc,
            "reference_bundle_sha256": ref_sha,
            "reference_arrays": ref_desc,
            "adapter": {"name": "raw.zipfile", "version": "1.0.0"},
            "created_at": "2026-11-24T10:00:00Z",
        }
        m_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        m_sha = store.put_bytes(m_bytes)
        atomic_write_json(store.root / "evidence" / "demo-record-v1" / "r-raw" / f"{cid}.json", {"manifest_sha256": m_sha, **manifest})

        g = grade_parity_record(decl_path, parity_store=store_dir, catalog_dir=cat, track_run=False)
        assert g.result["cells"][0]["verdict"] == "pass"

        # Tamper canonical_digest in manifest blob -> digest_mismatch
        bad_m = dict(manifest)
        bad_m["ours_arrays"] = {"scores": {**ours_desc["scores"], "canonical_digest": "0" * 64}}
        bad_bytes = (json.dumps(bad_m, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        bad_sha = store.put_bytes(bad_bytes)
        atomic_write_json(store.root / "evidence" / "demo-record-v1" / "r-raw" / f"{cid}.json", {"manifest_sha256": bad_sha, **bad_m})
        g_bad = grade_parity_record(decl_path, parity_store=store_dir, catalog_dir=cat, track_run=False)
        assert g_bad.result["cells"][0]["verdict"] == "fail"
        assert g_bad.result["cells"][0]["flags"] == ["digest_mismatch"]

    @pytest.mark.parametrize(
        "verdicts,expected_result",
        [
            (["fail", "contested"], "FAILS"),
            (["contested", "inconclusive"], "CONTESTED"),
            (["inconclusive", "declared_deviation"], "INCONCLUSIVE"),
            (["pass", "declared_deviation"], "PASSES EXCEPT DECLARED DEVIATIONS (1)"),
            (["not_run", "pass"], "PASSES"),
        ],
    )
    def test_s7_multicell_axis_table_step_precedence(self, verdicts: list[str], expected_result: str) -> None:
        decl = tomllib.loads(render_declaration_scaffold("axis-table-v1"))
        cells = [
            {
                "cell_id": f"{idx:016x}",
                "data_tier": "toy",
                "pairing": "fixture",
                "declared_kind": "within_tolerance",
                "verdict": v,
                "overlays": [],
            }
            for idx, v in enumerate(verdicts)
        ]
        clauses, _gates, _counts = compute_axes_and_clauses(
            declaration=decl, cells=cells, active_g_codes=RING0_ACTIVE_G_CODES
        )
        assert clauses[0]["axes"]["result"]["value"] == expected_result

    def test_s7_tightening_and_smoke_two_band_and_dtype_mismatch(self, tmp_path: Path) -> None:
        _repo, store_dir, cat, decl_path, decl = self._setup_workspace(tmp_path)
        # 1. Tightening: first declaration loose (passes), then tightened (fails + dual display PASSES)
        loose_toml = decl_path.read_text(encoding="utf-8").replace("atol = 1e-4", "atol = 1e-2")
        decl_path.write_text(loose_toml, encoding="utf-8")
        declare_parity_record(decl_path, parity_store=store_dir)
        cid = _cell_id_from_decl(decl)
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-tight", exit_code=0, status="completed", outcome="pass"),
            cat,
        )
        write_cell_evidence(
            store=store_dir,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r-tight",
            ours_arrays={"scores": np.array([1.001], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        tight_toml = loose_toml.replace("atol = 1e-2", "atol = 1e-6").replace("rtol = 1e-4", "rtol = 0.0")
        decl_path.write_text(tight_toml, encoding="utf-8")
        g_tight = grade_parity_record(decl_path, parity_store=store_dir, catalog_dir=cat, track_run=False)
        assert g_tight.result["clauses"][0]["axes"]["result"]["value"] == "FAILS"
        assert g_tight.result["clauses"][0]["axes"]["registration"]["value"] == "amended"
        assert g_tight.result["dual_display_result"]["clauses"][0]["axes"]["result"]["value"] == "PASSES"

        # 2. S3 dtype mismatch: ours=float64, ref=float32 (tol selected from ref dtype!) -> fail + kind_shortfall
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-dtype-mm", exit_code=0, status="completed", outcome="pass"),
            cat,
        )
        write_cell_evidence(
            store=store_dir,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r-dtype-mm",
            ours_arrays={"scores": np.array([1.0], dtype=np.float64)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g_mm = grade_parity_record(
            decl_path, parity_store=store_dir, catalog_dir=cat, evidence_run_ids=["r-dtype-mm"], track_run=False
        )
        assert g_mm.result["cells"][0]["verdict"] == "fail"
        assert g_mm.result["cells"][0]["flags"] == ["kind_shortfall"]
        assert g_mm.result["cells"][0]["metric_values"]["arrays"]["scores"]["dtype_mismatch"] is True

        # 3. Two-band: smoke + toy cells with badge_min_tier="smoke" vs "toy"
        smoke_decl = tomllib.loads(render_declaration_scaffold("smoke-two-band-v1"))
        smoke_decl["parity"]["badge_min_tier"] = "smoke"
        cl_smoke, _, cnt_smoke = compute_axes_and_clauses(
            declaration=smoke_decl,
            cells=[
                {"cell_id": "a" * 16, "data_tier": "smoke", "pairing": "fixture", "declared_kind": "within_tolerance", "verdict": "pass", "overlays": []},
                {"cell_id": "b" * 16, "data_tier": "toy", "pairing": "fixture", "declared_kind": "within_tolerance", "verdict": "fail", "overlays": []},
            ],
        )
        assert cl_smoke[0]["passing"] == 1 and cl_smoke[0]["required"] == 2
        assert cnt_smoke["required"] == 2

    def test_s7_s2_pooling_all_runs_and_per_input_fail_then_pass(self, tmp_path: Path) -> None:
        repo, store_dir, cat, decl_path, decl = self._setup_workspace(tmp_path)
        declare_parity_record(decl_path, parity_store=store_dir)
        cid = _cell_id_from_decl(decl)
        store = ParityStore(store_dir)

        t0 = datetime(2026, 11, 24, 10, 0, 0, tzinfo=UTC)
        # Run 1 fails, Run 2 passes later in catalog timestamp (even if created_at in manifest is earlier!)
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-pool-1", timestamp=t0, exit_code=0, status="completed", outcome="pass"),
            cat,
        )
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-pool-2", timestamp=t0 + timedelta(minutes=5), exit_code=0, status="completed", outcome="pass"),
            cat,
        )
        _s1, _ = write_cell_evidence(
            store=store,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r-pool-1",
            ours_arrays={"scores": np.array([1.5], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        _s2, _ = write_cell_evidence(
            store=store,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r-pool-2",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )

        # Backdate r-pool-2's manifest created_at before r-pool-1 to prove catalog timestamp controls sort order
        idx2_path = store.root / "evidence" / "demo-record-v1" / "r-pool-2" / f"{cid}.json"
        idx2 = json.loads(idx2_path.read_text(encoding="utf-8"))
        m2_body = {k: v for k, v in idx2.items() if k != "manifest_sha256"}
        m2_body["created_at"] = "2020-01-01T00:00:00Z"
        m2_sha = store.put_bytes((json.dumps(m2_body, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
        atomic_write_json(idx2_path, {"manifest_sha256": m2_sha, **m2_body})

        g_all = grade_parity_record(decl_path, parity_store=store_dir, catalog_dir=cat, track_run=False)
        c_all = g_all.result["cells"][0]
        assert c_all["verdict"] == "fail"
        assert "pooled_run_failure" in c_all["flags"]
        assert "kind_shortfall" in c_all["flags"]
        assert [rv["run_id"] for rv in c_all["run_verdicts"]] == ["r-pool-1", "r-pool-2"]
        assert c_all["cell_evidence_sha256"] == m2_sha
        assert len(c_all["cell_evidence_sha256s"]) == 2
        assert c_all["metric_values"]["max_abs_diff"] == pytest.approx(0.5)

        # per_input pooling across two inputs where one input had a fail-then-pass
        pi_toml = decl_path.read_text(encoding="utf-8").replace('pooling_rule = "all_runs"', 'pooling_rule = "per_input"').replace("demo-record-v1", "demo-per-input-v1")
        pi_path = repo / "pi.parity.bth.toml"
        pi_path.write_text(pi_toml, encoding="utf-8")
        pi_decl = tomllib.loads(pi_toml)
        declare_parity_record(pi_path, parity_store=store_dir)
        pi_cid = _cell_id_from_decl(pi_decl)
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-pool-3", timestamp=t0 + timedelta(minutes=10), exit_code=0, status="completed", outcome="pass"),
            cat,
        )
        write_cell_evidence(
            store=store, record_id="demo-per-input-v1", cell_id=pi_cid, run_id="r-pool-1",
            ours_arrays={"scores": np.array([1.5], dtype=np.float32)}, reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        write_cell_evidence(
            store=store, record_id="demo-per-input-v1", cell_id=pi_cid, run_id="r-pool-2",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)}, reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        write_cell_evidence(
            store=store, record_id="demo-per-input-v1", cell_id=pi_cid, run_id="r-pool-3",
            ours_arrays={"scores": np.array([2.0], dtype=np.float32)}, reference_arrays={"scores": np.array([2.0], dtype=np.float32)},
            input_sha256={"ours": ["c" * 64], "reference": ["d" * 64]},
        )
        g_pi = grade_parity_record(pi_path, parity_store=store_dir, catalog_dir=cat, track_run=False)
        c_pi = g_pi.result["cells"][0]
        assert c_pi["verdict"] == "fail"
        assert "pooled_run_failure" in c_pi["flags"]
        assert len(c_pi["run_verdicts"]) == 3

    def test_n2_nan_tolerance_cli_exits_cleanly(self, tmp_path: Path) -> None:
        repo, store_dir, cat, decl_path, _decl = self._setup_workspace(tmp_path)
        nan_toml = decl_path.read_text(encoding="utf-8").replace("atol = 1e-4", "atol = nan")
        nan_path = repo / "nan.parity.bth.toml"
        nan_path.write_text(nan_toml, encoding="utf-8")
        proc_nan = _run_bth_cli(
            "parity", "grade", str(nan_path), "--parity-store", str(store_dir), "--catalog-dir", str(cat), "--no-track",
            cwd=repo,
        )
        assert proc_nan.returncode == 1

    def test_s4_undeclared_record_raises_and_store_identity_recorded(self, tmp_path: Path) -> None:
        repo, store_dir, cat, decl_path, decl = self._setup_workspace(tmp_path)
        undec_path = repo / "undeclared.parity.bth.toml"
        undec_path.write_text(decl_path.read_text(encoding="utf-8").replace("demo-record-v1", "undeclared-v1"), encoding="utf-8")
        with pytest.raises(ParityDeclareError, match="no declared baseline"):
            grade_parity_record(undec_path, parity_store=store_dir, catalog_dir=cat, track_run=False)

        declare_parity_record(decl_path, parity_store=store_dir)
        cid = _cell_id_from_decl(decl)
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-s4", exit_code=0, status="completed", outcome="pass"),
            cat,
        )
        write_cell_evidence(
            store=store_dir,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r-s4",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g = grade_parity_record(
            decl_path,
            parity_store=store_dir,
            catalog_dir=cat,
            evidence_run_ids=["r-s4"],
            as_of="2026-11-24T12:00:00Z",
            track_run=True,
        )
        assert g.result["parity_store"] == {"path": str(Path(store_dir).resolve()), "layer": "cli_arg"}
        assert g.result["grade_inputs"]["parity_store"] == g.result["parity_store"]

        grade_run = next(r for r in read_runs(cat) if r.id == g.grade_run_id)
        assert "parity_store_layer:cli_arg" in grade_run.tags
        run_meta = json.loads(grade_run.metadata)
        assert run_meta["parity_store"] == g.result["parity_store"]
        assert run_meta["record_id"] == "demo-record-v1"

    def test_s6_evidence_run_filter_overlay_hooks_and_no_head_fallback(self, tmp_path: Path) -> None:
        _repo, store_dir, cat, decl_path, decl = self._setup_workspace(tmp_path)
        declare_parity_record(decl_path, parity_store=store_dir)
        cid = _cell_id_from_decl(decl)
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-s6", exit_code=0, status="completed", outcome="pass"),
            cat,
        )
        write_cell_evidence(
            store=store_dir,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r-s6",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )

        def _custom_hook(_cell: dict, _decl: dict) -> list[str]:
            return ["stale(code)"]

        g = grade_parity_record(
            decl_path,
            parity_store=store_dir,
            catalog_dir=cat,
            evidence_run_ids=["r-s6"],
            as_of="2026-11-24T12:00:00Z",
            track_run=False,
            overlay_hooks=(_custom_hook,),
        )
        assert g.result["grade_inputs"]["evidence_run_ids_filter"] == ["r-s6"]
        assert len(g.result["grade_inputs"]["overlay_hooks"]) == 1

        # --verify with matching args succeeds, mismatched evidence_run_ids or hooks fails ParityVerifyError
        assert grade_parity_record(
            decl_path, parity_store=store_dir, catalog_dir=cat, evidence_run_ids=["r-s6"], verify=True, overlay_hooks=(_custom_hook,)
        ).verified is True
        with pytest.raises(ParityVerifyError):
            grade_parity_record(decl_path, parity_store=store_dir, catalog_dir=cat, verify=True, overlay_hooks=())

        # missing head_commit in store raises ParityDeclareError (no git rev-parse fallback)
        store = ParityStore(store_dir)
        first_json = store.root / "declarations" / "demo-record-v1" / "first.json"
        first_obj = json.loads(first_json.read_text(encoding="utf-8"))
        sha = first_obj["declaration_sha256"]
        first_obj["head_commit"] = ""
        first_json.write_text(json.dumps(first_obj), encoding="utf-8")
        ver_json = store.root / "declarations" / "demo-record-v1" / f"{sha}.json"
        ver_obj = json.loads(ver_json.read_text(encoding="utf-8"))
        ver_obj["head_commit"] = ""
        ver_json.write_text(json.dumps(ver_obj), encoding="utf-8")
        with pytest.raises(ParityDeclareError, match="missing declared head_commit"):
            grade_parity_record(decl_path, parity_store=store_dir, catalog_dir=cat, track_run=False)

    def test_s5_corrupt_cool_archived_items_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _repo, store_dir, _cat, decl_path, decl = self._setup_workspace(tmp_path)
        declare_parity_record(decl_path, parity_store=store_dir)
        cid = _cell_id_from_decl(decl)
        cool_cat = tmp_path / "corrupt_cool_cat"
        cool_cat.mkdir()
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id="r-s5", exit_code=0, status="completed", outcome="pass"),
            cool_cat,
        )
        write_cell_evidence(
            store=store_dir,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r-s5",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        arch_dir = cool_cat / "archived_items"
        arch_dir.mkdir(parents=True)

        def _boom(_cat: Path | str) -> list:
            raise RuntimeError("corrupt archived_items ledger")

        monkeypatch.setattr("bathos.parity.grader.read_archived_item_fragments", _boom)
        with pytest.raises(RuntimeError, match="corrupt archived_items ledger"):
            grade_parity_record(decl_path, parity_store=store_dir, catalog_dir=cool_cat, evidence_run_ids=["r-s5"], track_run=False)

    def test_exact_run_lookup_skips_mixed_timestamp_cool_fragments_and_rejects_prefixes(self, tmp_path: Path) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        _repo, store_dir, cat, decl_path, decl = self._setup_workspace(tmp_path)
        declare_parity_record(decl_path, parity_store=store_dir)
        cid = _cell_id_from_decl(decl)

        full_rid = "run-exact-1234567890"
        write_run(
            Run(project_slug="demo", command="bth run ev.py", argv=[], git_hash="0" * 40, git_branch="main", git_dirty=False, id=full_rid, exit_code=0, status="completed", outcome="pass"),
            cat,
        )
        compact(cat)

        # Drop an unrelated legacy fragment whose `timestamp` column is string instead of timestamp[us, tz=UTC]
        legacy_dir = cat / "runs" / "legacy-other"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.table({"id": ["old-run"], "timestamp": ["2025-01-01T00:00:00Z"]}),
            legacy_dir / "run_old.parquet",
        )

        # Prefix of an existing run_id must NOT match (treated as unknown -> not_run)
        write_cell_evidence(
            store=store_dir,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="run-exact",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g_prefix = grade_parity_record(
            decl_path, parity_store=store_dir, catalog_dir=cat, evidence_run_ids=["run-exact"], track_run=False
        )
        assert g_prefix.result["cells"][0]["verdict"] == "not_run"

        # Exact run_id resolves cleanly from warm/index catalog despite incompatible cool fragment
        write_cell_evidence(
            store=store_dir,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id=full_rid,
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g_ok = grade_parity_record(
            decl_path, parity_store=store_dir, catalog_dir=cat, evidence_run_ids=[full_rid], as_of="2026-11-24T12:00:00Z", track_run=False
        )
        assert g_ok.result["cells"][0]["verdict"] == "pass"
        assert grade_parity_record(
            decl_path, parity_store=store_dir, catalog_dir=cat, evidence_run_ids=[full_rid], verify=True
        ).verified is True

    def test_item3_run_project_ownership_failed_status_unknown_flag_and_explicit_cell_runs(
        self, tmp_path: Path
    ) -> None:
        _repo, store_dir, cat, decl_path, decl = self._setup_workspace(tmp_path)
        declare_parity_record(decl_path, parity_store=store_dir)
        cid = _cell_id_from_decl(decl)
        store = ParityStore(store_dir)

        # Subcase A: run from another project_slug with passing arrays -> not_run + wrong_project_run
        write_run(
            Run(
                project_slug="other-project",
                command="bth run ev.py",
                argv=[],
                git_hash="0" * 40,
                git_branch="main",
                git_dirty=False,
                id="r-other-proj",
                exit_code=0,
                status="completed",
                outcome="pass",
            ),
            cat,
        )
        write_cell_evidence(
            store=store,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r-other-proj",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g_a = grade_parity_record(
            decl_path, parity_store=store_dir, catalog_dir=cat, evidence_run_ids=["r-other-proj"], track_run=False
        )
        assert g_a.result["cells"][0]["verdict"] == "not_run"
        assert "wrong_project_run" in g_a.result["cells"][0]["flags"]

        # Subcase B: status="failed" with matching arrays -> inconclusive + run_failed (never pass)
        # whereas status="completed", outcome="fail" still passes per G04
        write_run(
            Run(
                project_slug="demo",
                command="bth run ev.py",
                argv=[],
                git_hash="0" * 40,
                git_branch="main",
                git_dirty=False,
                id="r-status-failed",
                exit_code=1,
                status="failed",
                outcome="fail",
            ),
            cat,
        )
        write_cell_evidence(
            store=store,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r-status-failed",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g_b1 = grade_parity_record(
            decl_path, parity_store=store_dir, catalog_dir=cat, evidence_run_ids=["r-status-failed"], track_run=False
        )
        assert g_b1.result["cells"][0]["verdict"] == "inconclusive"
        assert "run_failed" in g_b1.result["cells"][0]["flags"]

        write_run(
            Run(
                project_slug="demo",
                command="bth run ev.py",
                argv=[],
                git_hash="0" * 40,
                git_branch="main",
                git_dirty=False,
                id="r-completed-outcome-fail",
                exit_code=1,
                status="completed",
                outcome="fail",
            ),
            cat,
        )
        write_cell_evidence(
            store=store,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r-completed-outcome-fail",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g_b2 = grade_parity_record(
            decl_path,
            parity_store=store_dir,
            catalog_dir=cat,
            evidence_run_ids=["r-completed-outcome-fail"],
            track_run=False,
        )
        assert g_b2.result["cells"][0]["verdict"] == "pass"

        # Subcase C: unknown manifest run beside a passing known run -> inconclusive + unknown_evidence_run
        write_run(
            Run(
                project_slug="demo",
                command="bth run ev.py",
                argv=[],
                git_hash="0" * 40,
                git_branch="main",
                git_dirty=False,
                id="r-known-pass",
                exit_code=0,
                status="completed",
                outcome="pass",
            ),
            cat,
        )
        write_cell_evidence(
            store=store,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r-known-pass",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        write_cell_evidence(
            store=store,
            record_id="demo-record-v1",
            cell_id=cid,
            run_id="r-unknown-beside",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        g_c = grade_parity_record(
            decl_path,
            parity_store=store_dir,
            catalog_dir=cat,
            evidence_run_ids=["r-known-pass", "r-unknown-beside"],
            track_run=False,
        )
        assert g_c.result["cells"][0]["verdict"] == "inconclusive"
        assert "unknown_evidence_run" in g_c.result["cells"][0]["flags"]

        # Subcase D: explicit cell_runs={cid: ["unknown-explicit-run"]} not in catalog -> flags unknown_evidence_run
        g_d = grade_parity_record(
            decl_path,
            parity_store=store_dir,
            catalog_dir=cat,
            evidence_run_ids=[],
            cell_runs={cid: ["unknown-explicit-run"]},
            track_run=False,
        )
        assert "unknown_evidence_run" in g_d.result["cells"][0]["flags"]

    def test_item4_reject_foreign_record_or_unplanned_cell_manifest(self, tmp_path: Path) -> None:
        _repo, store_dir, cat, decl_path, _decl = self._setup_workspace(tmp_path)
        declare_parity_record(decl_path, parity_store=store_dir)
        store = ParityStore(store_dir)

        write_run(
            Run(
                project_slug="demo",
                command="bth run ev.py",
                argv=[],
                git_hash="0" * 40,
                git_branch="main",
                git_dirty=False,
                id="r-unplanned",
                exit_code=0,
                status="completed",
                outcome="pass",
            ),
            cat,
        )
        write_cell_evidence(
            store=store,
            record_id="demo-record-v1",
            cell_id="deadbeefcafebabe",
            run_id="r-unplanned",
            ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        with pytest.raises(CellEvidenceError, match="cell_id"):
            grade_parity_record(decl_path, parity_store=store_dir, catalog_dir=cat, track_run=False)

    def test_item6_prior_records_read_error_fails_loudly_and_no_version_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import bathos
        from bathos.parity.grader import _package_version

        _repo, store_dir, cat, decl_path, _decl = self._setup_workspace(tmp_path)
        declare_parity_record(decl_path, parity_store=store_dir)
        store = ParityStore(store_dir)

        corrupt_dir = store.root / "declarations" / "other-rec"
        corrupt_dir.mkdir(parents=True, exist_ok=True)
        (corrupt_dir / "first.json").write_text("{corrupt json", encoding="utf-8")

        with pytest.raises(ParityDeclareError):
            grade_parity_record(decl_path, parity_store=store_dir, catalog_dir=cat, track_run=False)

        assert _package_version() == str(bathos.__version__)
        monkeypatch.delattr(bathos, "__version__")
        with pytest.raises(AttributeError):
            _package_version()

    def test_item_a_strict_project_slug_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BTH_PROJECT_SLUG", raising=False)
        repo = tmp_path / "no_cfg_repo"
        repo.mkdir()
        _init_git_repo(repo)
        store = tmp_path / "store"
        cat = tmp_path / "catalog"
        init_catalog(cat)
        decl_path = repo / "demo.parity.bth.toml"
        decl_path.write_text(render_declaration_scaffold("item-a-v1", impl_name="aminx"), encoding="utf-8")
        declare_parity_record(decl_path, parity_store=store)

        # Missing .bth.toml and unset BTH_PROJECT_SLUG MUST raise (never fall back to implementation.name)
        with pytest.raises(ParityDeclareError, match="Cannot resolve project slug"):
            grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, track_run=False)

        # Empty [project].slug MUST raise
        (repo / ".bth.toml").write_text('[project]\nslug = ""\n', encoding="utf-8")
        with pytest.raises(ParityDeclareError, match=r"missing non-empty \[project\]\.slug"):
            grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, track_run=False)

        # Malformed .bth.toml MUST raise (never swallowed)
        (repo / ".bth.toml").write_text("[project\nbroken\n", encoding="utf-8")
        with pytest.raises(ParityDeclareError, match="Failed to read project config"):
            grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, track_run=False)

    def test_item_b_prior_declaration_blob_failures(self, tmp_path: Path) -> None:
        _repo, store_dir, cat, decl_path, _decl = self._setup_workspace(tmp_path)
        declare_parity_record(decl_path, parity_store=store_dir)
        store = ParityStore(store_dir)

        # Missing referenced blob SHA -> raises ParityDeclareError
        other_dir = store.root / "declarations" / "prior-missing-blob"
        other_dir.mkdir(parents=True, exist_ok=True)
        (other_dir / "first.json").write_text(
            json.dumps({"declaration_sha256": "a" * 64}),
            encoding="utf-8",
        )
        with pytest.raises(ParityDeclareError, match="Failed to load prior declaration blob"):
            grade_parity_record(decl_path, parity_store=store_dir, catalog_dir=cat, track_run=False)

        # Invalid TOML bytes in referenced prior blob -> raises ParityDeclareError
        bad_toml_sha = store.put_bytes(b"[not valid toml\n")
        (other_dir / "first.json").write_text(
            json.dumps({"declaration_sha256": bad_toml_sha}),
            encoding="utf-8",
        )
        with pytest.raises(ParityDeclareError, match="Failed to load prior declaration blob"):
            grade_parity_record(decl_path, parity_store=store_dir, catalog_dir=cat, track_run=False)

    def test_item_e_failed_and_nonterminal_supersession_rules(self, tmp_path: Path) -> None:
        from bathos.supersession import add_supersession_edge

        def _make_env(name: str):
            sub = tmp_path / name
            sub.mkdir()
            repo, store_dir, cat_dir, decl_path, base_decl = self._setup_workspace(sub)
            base_cell = base_decl["cell_plan"][0]
            # Two cells so we can also test cross-cell supersession rejection
            two_toml = decl_path.read_text(encoding="utf-8") + (
                '\n[[cell_plan]]\n'
                f'use_id = "{base_cell["use_id"]}"\n'
                f'component_id = "{base_cell["component_id"]}"\n'
                'knob_setting = { k = 2 }\n'
                'axis_value = { target = "1ubq", arm = "alt" }\n'
                'data_tier = "toy"\n'
                'pairing = "fixture"\n'
                'pooling_rule = "all_runs"\n'
                f'tolerance_id = "{base_cell["tolerance_id"]}"\n'
            )
            decl_path.write_text(two_toml, encoding="utf-8")
            decl = tomllib.loads(two_toml)
            declare_parity_record(decl_path, parity_store=store_dir)
            cid0 = _cell_id_from_decl(decl, 0)
            cid1 = _cell_id_from_decl(decl, 1)
            store = ParityStore(store_dir)
            # Populate cell 1 with a passing run in every fixture
            write_run(
                Run(
                    project_slug="demo",
                    command="bth run ev.py",
                    argv=[],
                    git_hash="0" * 40,
                    git_branch="main",
                    git_dirty=False,
                    id=f"r-c1-{name}",
                    timestamp=datetime(2026, 11, 25, 10, 30, tzinfo=UTC),
                    exit_code=0,
                    status="completed",
                    outcome="pass",
                ),
                cat_dir,
            )
            write_cell_evidence(
                store=store,
                record_id="demo-record-v1",
                cell_id=cid1,
                run_id=f"r-c1-{name}",
                ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
                reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
                input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
            )
            return repo, store_dir, cat_dir, decl_path, cid0, cid1

        def _add_run(cat_dir: Path, run_id: str, hour: int, *, status: str = "completed", outcome: str = "pass", exit_code: int = 0) -> None:
            write_run(
                Run(
                    project_slug="demo",
                    command="bth run ev.py",
                    argv=[],
                    git_hash="0" * 40,
                    git_branch="main",
                    git_dirty=False,
                    id=run_id,
                    timestamp=datetime(2026, 11, 25, hour, 0, tzinfo=UTC),
                    exit_code=exit_code,
                    status=status,
                    outcome=outcome,
                ),
                cat_dir,
            )

        def _write_ev(store_dir: Path, cid: str, run_id: str) -> None:
            write_cell_evidence(
                store=store_dir,
                record_id="demo-record-v1",
                cell_id=cid,
                run_id=run_id,
                ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
                reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
                input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
            )

        # 1. Failed run + later passing run with NO supersession edge -> inconclusive (run_failed)
        _r, store1, cat1, decl1, cid1_0, _ = _make_env("no_edge_fail")
        _add_run(cat1, "r-fail-1", 9, status="failed", outcome="fail", exit_code=1)
        _add_run(cat1, "r-pass-1", 10)
        _write_ev(store1, cid1_0, "r-fail-1")
        _write_ev(store1, cid1_0, "r-pass-1")
        g1 = grade_parity_record(decl1, parity_store=store1, catalog_dir=cat1, track_run=False)
        c1 = next(c for c in g1.result["cells"] if c["cell_id"] == cid1_0)
        assert c1["verdict"] == "inconclusive"
        assert "run_failed" in c1["flags"]
        assert g1.result.get("superseded_run_ids", []) == []

        # 2. Non-terminal run + later passing run with NO supersession edge -> not_run
        _r, store2, cat2, decl2, cid2_0, _ = _make_env("no_edge_running")
        _add_run(cat2, "r-run-2", 9, status="running", outcome="running", exit_code=0)
        _add_run(cat2, "r-pass-2", 10)
        _write_ev(store2, cid2_0, "r-run-2")
        _write_ev(store2, cid2_0, "r-pass-2")
        g2 = grade_parity_record(decl2, parity_store=store2, catalog_dir=cat2, track_run=False)
        c2 = next(c for c in g2.result["cells"] if c["cell_id"] == cid2_0)
        assert c2["verdict"] == "not_run"

        # 3. Failed (t1) superseded by later passing (t2 > t1) on same cell with non-empty reason -> pass
        _r, store3, cat3, decl3, cid3_0, _ = _make_env("sup_fail_ok")
        _add_run(cat3, "r-fail-3", 9, status="failed", outcome="fail", exit_code=1)
        _add_run(cat3, "r-pass-3", 10)
        _write_ev(store3, cid3_0, "r-fail-3")
        _write_ev(store3, cid3_0, "r-pass-3")
        compact(cat3)
        add_supersession_edge("run", "r-fail-3", "run", "r-pass-3", "OOM on node resolved on retry", catalog_dir=cat3)
        g3 = grade_parity_record(decl3, parity_store=store3, catalog_dir=cat3, track_run=False)
        c3 = next(c for c in g3.result["cells"] if c["cell_id"] == cid3_0)
        assert c3["verdict"] == "pass"
        assert c3["superseded_run_ids"] == ["r-fail-3"]
        assert g3.result["superseded_run_ids"] == ["r-fail-3"]
        assert g3.result["cell_counts"]["superseded_runs"] == 1

        # 4. Non-terminal (t1) superseded by later passing (t2 > t1) on same cell with non-empty reason -> pass
        _r, store4, cat4, decl4, cid4_0, _ = _make_env("sup_run_ok")
        _add_run(cat4, "r-run-4", 9, status="running", outcome="running")
        _add_run(cat4, "r-pass-4", 10)
        _write_ev(store4, cid4_0, "r-run-4")
        _write_ev(store4, cid4_0, "r-pass-4")
        compact(cat4)
        add_supersession_edge("run", "r-run-4", "run", "r-pass-4", "preempted SLURM job restarted", catalog_dir=cat4)
        g4 = grade_parity_record(decl4, parity_store=store4, catalog_dir=cat4, track_run=False)
        c4 = next(c for c in g4.result["cells"] if c["cell_id"] == cid4_0)
        assert c4["verdict"] == "pass"
        assert g4.result["superseded_run_ids"] == ["r-run-4"]

        # 5. NC: empty reason does NOT supersede -> stays inconclusive (run_failed)
        _r, store5, cat5, decl5, cid5_0, _ = _make_env("empty_reason")
        _add_run(cat5, "r-fail-5", 9, status="failed", outcome="fail", exit_code=1)
        _add_run(cat5, "r-pass-5", 10)
        _write_ev(store5, cid5_0, "r-fail-5")
        _write_ev(store5, cid5_0, "r-pass-5")
        compact(cat5)
        add_supersession_edge("run", "r-fail-5", "run", "r-pass-5", "   ", catalog_dir=cat5)
        g5 = grade_parity_record(decl5, parity_store=store5, catalog_dir=cat5, track_run=False)
        c5 = next(c for c in g5.result["cells"] if c["cell_id"] == cid5_0)
        assert c5["verdict"] == "inconclusive"
        assert "run_failed" in c5["flags"]
        assert g5.result.get("superseded_run_ids", []) == []

        # 6. NC (wrong direction): earlier passing (t1) listed as superseding later failed (t2 > t1) -> does NOT supersede
        _r, store6, cat6, decl6, cid6_0, _ = _make_env("wrong_direction")
        _add_run(cat6, "r-pass-6", 9)
        _add_run(cat6, "r-fail-6", 10, status="failed", outcome="fail", exit_code=1)
        _write_ev(store6, cid6_0, "r-pass-6")
        _write_ev(store6, cid6_0, "r-fail-6")
        compact(cat6)
        add_supersession_edge("run", "r-fail-6", "run", "r-pass-6", "invalid backward edge", catalog_dir=cat6)
        g6 = grade_parity_record(decl6, parity_store=store6, catalog_dir=cat6, track_run=False)
        c6 = next(c for c in g6.result["cells"] if c["cell_id"] == cid6_0)
        assert c6["verdict"] == "inconclusive"
        assert "run_failed" in c6["flags"]
        assert g6.result.get("superseded_run_ids", []) == []

        # 7. NC (cross-cell): later passing run belongs to a different cell -> does NOT supersede
        _r, store7, cat7, decl7, cid7_0, cid7_1 = _make_env("cross_cell")
        _add_run(cat7, "r-fail-7", 9, status="failed", outcome="fail", exit_code=1)
        _write_ev(store7, cid7_0, "r-fail-7")
        compact(cat7)
        add_supersession_edge("run", "r-fail-7", "run", "r-c1-cross_cell", "different cell run", catalog_dir=cat7)
        g7 = grade_parity_record(decl7, parity_store=store7, catalog_dir=cat7, track_run=False)
        c7 = next(c for c in g7.result["cells"] if c["cell_id"] == cid7_0)
        assert c7["verdict"] == "inconclusive"
        assert "run_failed" in c7["flags"]
        assert g7.result.get("superseded_run_ids", []) == []


class TestRing1FindingsWeightsAndDirtyRuns:
    def _setup_repo_with_run(self, tmp_path: Path, *, git_dirty: bool = False) -> tuple[Path, Path, Path, Path, dict]:
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        (repo / ".bth.toml").write_text('[project]\nslug = "demo"\n', encoding="utf-8")
        store = tmp_path / "store"
        cat = tmp_path / "catalog"
        init_catalog(cat)
        decl_path = repo / "demo.parity.bth.toml"
        raw_toml = render_declaration_scaffold("demo-weights-v1", impl_name="aminx").replace(
            'kind = "sealed_dump"\n'
            'identity = { dump_sha256 = "0000000000000000000000000000000000000000000000000000000000000000", producer_run_id = "run_0000", upstream_commit = "0000000000000000000000000000000000000000" }',
            'kind = "upstream_live"\n'
            f'identity = {{ repo = "https://github.com/example/upstream", commit = "{"a" * 40}" }}',
        )
        decl_path.write_text(raw_toml, encoding="utf-8")
        decl = tomllib.loads(raw_toml)
        cid = _cell_id_from_decl(decl)
        write_run(
            Run(
                project_slug="demo",
                command="bth run ev.py",
                argv=[],
                git_hash="0" * 40,
                git_branch="main",
                git_dirty=git_dirty,
                id="r-ev-1",
                exit_code=0,
                status="completed",
                outcome="pass",
            ),
            cat,
        )
        write_cell_evidence(
            store=store,
            record_id="demo-weights-v1",
            cell_id=cid,
            run_id="r-ev-1",
            ours_arrays={"scores": np.array([1.0, 2.0], dtype=np.float32)},
            reference_arrays={"scores": np.array([1.0, 2.0], dtype=np.float32)},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        return repo, store, cat, decl_path, decl

    def test_finding1_weights_artifact_hash_verified_and_tamper_refused(self, tmp_path: Path) -> None:
        from bathos.parity.grader import ParityGradeError

        repo, store, cat, decl_path, _decl = self._setup_repo_with_run(tmp_path)
        weights_file = repo / "data" / "weights" / "model.eqx.zst"
        weights_file.parent.mkdir(parents=True)
        weights_bytes = b"trained-weights-payload-v1"
        weights_file.write_bytes(weights_bytes)
        weights_sha = hashlib.sha256(weights_bytes).hexdigest()

        base_toml = decl_path.read_text(encoding="utf-8")
        decl_path.write_text(
            base_toml
            + (
                '\n[implementation.weights]\n'
                'artifact_path = "data/weights/model.eqx.zst"\n'
                f'artifact_sha256 = "{weights_sha}"\n'
                'source_checkpoint_status = "rederived"\n'
                'hf_repo = "example/aminx-weights"\n'
                f'hf_revision = "{"c" * 40}"\n'
            ),
            encoding="utf-8",
        )
        declare_parity_record(decl_path, parity_store=store)

        graded = grade_parity_record(
            decl_path, parity_store=store, catalog_dir=cat, as_of="2026-11-24T12:00:00Z", track_run=False
        )
        assert graded.result["implementation_weights"] == {
            "artifact_path": "data/weights/model.eqx.zst",
            "artifact_sha256": weights_sha,
            "source_checkpoint_status": "rederived",
            "hf_repo": "example/aminx-weights",
            "hf_revision": "c" * 40,
        }
        res_schema = json.loads(
            importlib.resources.files("bathos").joinpath("schemas", "parity_result.v1.json").read_text(encoding="utf-8")
        )
        jsonschema.validate(instance=graded.result, schema=res_schema)

        # (a) Mutate bytes on disk so hash != artifact_sha256 -> grader refuses with ParityGradeError
        weights_file.write_bytes(b"corrupted-weights-bytes")
        with pytest.raises(ParityGradeError, match="artifact_sha256 mismatch"):
            grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, track_run=False)
        with pytest.raises(ParityGradeError, match="artifact_sha256 mismatch"):
            grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, verify=True)

        # (b) Remove weights file on disk -> grader refuses with ParityGradeError
        weights_file.unlink()
        with pytest.raises(ParityGradeError, match="does not exist"):
            grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, track_run=False)

    def test_finding3_loads_trained_params_and_invalid_status_fail_closed_in_grader(self, tmp_path: Path) -> None:
        from bathos.parity.grader import ParityGradeError, _verify_implementation_weights

        repo, store, cat, decl_path, _decl = self._setup_repo_with_run(tmp_path)
        base_toml = decl_path.read_text(encoding="utf-8")

        # loads_trained_parameters = true without [implementation.weights] refused
        decl_path.write_text(
            base_toml.replace("third_party = false", "third_party = false\nloads_trained_parameters = true"),
            encoding="utf-8",
        )
        with pytest.raises(ParityValidationError):
            grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, track_run=False)
        with pytest.raises(ParityGradeError, match="loads_trained_parameters"):
            _verify_implementation_weights(
                declaration=tomllib.loads(decl_path.read_text(encoding="utf-8")),
                decl_path=decl_path,
                manifests=[],
            )

        # Invalid source_checkpoint_status refused both by validate_declaration and grader fail-closed helper
        wfile = repo / "data" / "weights" / "model.eqx.zst"
        wfile.parent.mkdir(parents=True)
        wbytes = b"bytes"
        wfile.write_bytes(wbytes)
        wsha = hashlib.sha256(wbytes).hexdigest()
        for bad_st in ("verified_local", "Attested_Only", "attested-only"):
            decl_path.write_text(
                base_toml
                + (
                    '\n[implementation.weights]\n'
                    'artifact_path = "data/weights/model.eqx.zst"\n'
                    f'artifact_sha256 = "{wsha}"\n'
                    f'source_checkpoint_status = "{bad_st}"\n'
                ),
                encoding="utf-8",
            )
            with pytest.raises(ParityValidationError):
                grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, track_run=False)
            with pytest.raises(ParityGradeError, match="source_checkpoint_status"):
                _verify_implementation_weights(
                    declaration=tomllib.loads(decl_path.read_text(encoding="utf-8")),
                    decl_path=decl_path,
                    manifests=[],
                )

    def test_finding3_manifest_weights_requires_declaration_weights(self, tmp_path: Path) -> None:
        from bathos.parity.grader import ParityGradeError

        (tmp_path / "no_weights_needed").mkdir()
        _r0, store0, cat0, decl0, _d0 = self._setup_repo_with_run(tmp_path / "no_weights_needed")
        declare_parity_record(decl0, parity_store=store0)
        g0 = grade_parity_record(decl0, parity_store=store0, catalog_dir=cat0, track_run=False)
        assert "implementation_weights" not in g0.result

        # Manifest declaring weights_path or weights_sha256 without [implementation.weights] -> ParityGradeError
        for extra_key, extra_val in (("weights_path", "data/w.bin"), ("weights_sha256", "d" * 64)):
            sub = tmp_path / f"manifest_{extra_key}"
            sub.mkdir()
            repo, store, cat, decl_path, decl = self._setup_repo_with_run(sub)
            declare_parity_record(decl_path, parity_store=store)
            cid = _cell_id_from_decl(decl)
            ev_index_path = store / "evidence" / "demo-weights-v1" / "r-ev-1" / f"{cid}.json"
            idx_obj = json.loads(ev_index_path.read_text(encoding="utf-8"))
            idx_body = {k: v for k, v in idx_obj.items() if k != "manifest_sha256"}
            idx_body[extra_key] = extra_val
            m_bytes = (json.dumps(idx_body, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
            m_sha = ParityStore(store).put_bytes(m_bytes)
            atomic_write_json(ev_index_path, {"manifest_sha256": m_sha, **idx_body})

            with pytest.raises(ParityGradeError, match="implementation.weights"):
                grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, track_run=False)

    def test_finding4_dirty_evidence_runs_recorded_and_mock_defaults_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Clean run
        (tmp_path / "clean").mkdir()
        _rc, store_c, cat_c, decl_c, _dc = self._setup_repo_with_run(tmp_path / "clean", git_dirty=False)
        declare_parity_record(decl_c, parity_store=store_c)
        gc = grade_parity_record(decl_c, parity_store=store_c, catalog_dir=cat_c, track_run=False)
        assert gc.result["evidence_runs"][0]["git_dirty"] is False
        assert gc.result["has_dirty_evidence_run"] is False
        assert gc.result["dirty_evidence_run_ids"] == []

        # Dirty run
        (tmp_path / "dirty").mkdir()
        _rd, store_d, cat_d, decl_d, _dd = self._setup_repo_with_run(tmp_path / "dirty", git_dirty=True)
        declare_parity_record(decl_d, parity_store=store_d)
        gd = grade_parity_record(decl_d, parity_store=store_d, catalog_dir=cat_d, track_run=False)
        assert gd.result["evidence_runs"][0]["git_dirty"] is True
        assert gd.result["has_dirty_evidence_run"] is True
        assert gd.result["dirty_evidence_run_ids"] == ["r-ev-1"]
        # Does NOT block cell verdict yet
        assert gd.result["cells"][0]["verdict"] == "pass"

        # Mock run without git_dirty attribute defaults cleanly to False; mock with git_dirty=True preserves True
        from types import SimpleNamespace

        monkeypatch.setattr(
            "bathos.parity.grader._load_runs_by_id",
            lambda _cat, _rids: {
                "r-ev-1": SimpleNamespace(
                    id="r-ev-1",
                    project_slug="demo",
                    status="completed",
                    outcome="pass",
                    timestamp="2026-11-24T00:00:00Z",
                )
            },
        )
        gm = grade_parity_record(decl_c, parity_store=store_c, catalog_dir=cat_c, track_run=False)
        assert gm.result["evidence_runs"][0]["git_dirty"] is False
        assert gm.result["has_dirty_evidence_run"] is False

        # Mock run with git_dirty=True preserves True
        monkeypatch.setattr(
            "bathos.parity.grader._load_runs_by_id",
            lambda _cat, _rids: {
                "r-ev-1": SimpleNamespace(
                    id="r-ev-1",
                    project_slug="demo",
                    status="completed",
                    outcome="pass",
                    timestamp="2026-11-24T00:00:00Z",
                    git_dirty=True,
                )
            },
        )
        gm_dirty = grade_parity_record(decl_c, parity_store=store_c, catalog_dir=cat_c, track_run=False)
        assert gm_dirty.result["evidence_runs"][0]["git_dirty"] is True
        assert gm_dirty.result["has_dirty_evidence_run"] is True
        assert gm_dirty.result["dirty_evidence_run_ids"] == ["r-ev-1"]

