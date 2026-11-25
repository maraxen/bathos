"""Tests for WP03a: parity record schema core, validator, declare, and sidecar exclusion."""

from __future__ import annotations

import copy
import importlib.resources
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import jsonschema
import pytest

from bathos.archive_sweep import _find_companion_sidecars, scan_archive_candidates
from bathos.catalog import init_catalog
from bathos.cli_cyclopts import app
from bathos.correction_sweep import iter_sweep_artifacts
from bathos.linter import check_positive_control_missing, iter_project_sidecars
from bathos.parity.declare import declare_parity_record
from bathos.parity.schema import (
    STANDARD_POOL_CRITICAL_1_0_0,
    compute_cell_id,
    render_declaration_scaffold,
    validate_declaration,
    validate_result_structure,
)
from bathos.requires_gate import discover_sidecar
from bathos.sidecar import find_sidecar, is_parity_or_discrepancy_toml


def _load_json_schema(name: str) -> dict:
    raw = importlib.resources.files("bathos").joinpath("schemas", name).read_text(encoding="utf-8")
    return json.loads(raw)


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


def _minimal_valid_decl() -> dict:
    return tomllib.loads(render_declaration_scaffold("demo-record-v1"))


def _init_git_repo(repo_dir: Path) -> str:
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_dir), check=True)
    readme = repo_dir / "README.md"
    readme.write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "--no-verify", "-m", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo_dir), check=True, capture_output=True, text=True
    ).stdout.strip()


class TestParitySchemaCore:
    def test_scaffold_roundtrip_validates_cleanly(self) -> None:
        decl = _minimal_valid_decl()
        assert validate_declaration(decl) == []
        assert tuple(decl["environment"]["pool_critical"]) == STANDARD_POOL_CRITICAL_1_0_0
        jsonschema.validate(instance=decl, schema=_load_json_schema("parity_declaration.v1.json"))

    def test_cell_id_deterministic(self) -> None:
        c1 = compute_cell_id("model", "score", {"a": 1, "b": 2}, {"input": "1ubq"}, "toy", "fixture")
        c2 = compute_cell_id("model", "score", {"b": 2, "a": 1}, {"input": "1ubq"}, "toy", "fixture")
        assert c1 == c2
        assert len(c1) == 16

    def test_result_schema_roundtrip(self) -> None:
        res = {
            "schema_version": "parity_result.v1",
            "standard_version": "1.0.0",
            "record_id": "demo-record-v1",
            "record_sha256": "a" * 64,
            "ledger_entry_id": None,
            "grader_version": "0.18.0",
            "results_seen": False,
            "grade_inputs": {
                "as_of": "2026-11-24T00:00:00Z",
                "target_revisions": {},
                "drift_run_id": None,
                "ledger_high_water_mark": None,
                "gate_stages": {},
                "comparison_backend": {"name": "numpy"},
            },
            "evidence_runs": [],
            "cells": [
                {
                    "cell_id": "0" * 16,
                    "verdict": "not_run",
                    "overlays": [],
                    "achieved_kinds": [],
                    "declared_kind": "within_tolerance",
                    "metric_values": {},
                    "n": 0,
                    "tolerance_ref": None,
                    "evidence_run_ids": [],
                    "pooling_rule": "all_runs",
                    "cell_evidence_sha256": None,
                    "input_sha256": {"ours": [], "reference": []},
                    "pin_set": {},
                    "nondegeneracy": {"status": "not_checked_ring0"},
                    "evidence_level": "self_reported",
                    "flags": [],
                }
            ],
            "components": [],
            "supplementary": [],
            "checks_admissibility": [],
            "cell_counts": {
                "required": 1,
                "passing": 0,
                "by_kind": {},
                "not_covered": 0,
                "waived": 0,
                "not_run": 1,
                "declared_deviations": 0,
                "registry_knobs_non_excluded": None,
                "registry_knobs_covered": None,
                "unplanned_knobs": None,
            },
            "clauses": [
                {
                    "clause_id": "c0",
                    "kinds": ["within_tolerance"],
                    "passing": 0,
                    "required": 1,
                    "axes": {
                        "result": {"value": "NO RESULT"},
                        "coverage": {"value": "partial"},
                        "assurance": {"value": "PROVISIONAL"},
                        "freshness": {"value": "current"},
                        "registration": {"value": "retrospective"},
                    },
                }
            ],
            "gates": [{"code": "G01", "stage": "shadow"}],
            "amendments": [],
            "prior_records_on_pair": 0,
            "bundle_sha256": None,
        }
        assert validate_result_structure(res) == []
        jsonschema.validate(instance=res, schema=_load_json_schema("parity_result.v1.json"))

        # Missing nested cell key or invalid grade_inputs caught by validate_result_structure
        bad_res = copy.deepcopy(res)
        del bad_res["cells"][0]["flags"]
        del bad_res["grade_inputs"]["comparison_backend"]
        issues = validate_result_structure(bad_res)
        assert any("cells[0].flags" in i.path for i in issues)
        assert any("grade_inputs.comparison_backend" in i.path for i in issues)


class TestWP03aNegativeControls:
    @pytest.mark.parametrize(
        "section,field_name",
        [
            ("parity", "schema_version"),
            ("parity", "record_id"),
            ("parity", "prong"),
            ("parity", "layer_under_test"),
            ("parity", "instrumentation"),
            ("parity", "mode"),
            ("parity", "strictness"),
            ("parity", "prior_runs"),
            ("parity", "badge_min_tier"),
            ("implementation", "name"),
            ("implementation", "repo"),
            ("implementation", "licence"),
            ("registry", "registry_id"),
            ("registry", "drift_status_max_age_days"),
            ("environment", "pool_critical"),
            ("environment", "frameworks"),
        ],
    )
    def test_nc_missing_required_d_field_names_field(self, section: str, field_name: str) -> None:
        decl = _minimal_valid_decl()
        del decl[section][field_name]
        issues = validate_declaration(decl)
        assert any(field_name in i.path or field_name in i.message for i in issues), issues

    def test_nc_published_values_with_bitwise_fails(self) -> None:
        decl = _minimal_valid_decl()
        decl["reference"][0] = {
            "id": "paper-1",
            "role": "comparator",
            "kind": "published_values",
            "identity": {"doi": "10.1000/x", "locator": "Table 1", "transcription_run_id": "run_1"},
            "licence": "CC-BY-4.0",
        }
        decl["use"][0]["parity_kind"] = "bitwise"
        issues = validate_declaration(decl)
        assert any(i.code == "G02_UNSUPPORTED_KIND" for i in issues)

    @pytest.mark.parametrize("missing_key", ["judgement_id", "negative_control"])
    def test_nc_use_without_judgement_or_neg_control_fails(self, missing_key: str) -> None:
        decl = _minimal_valid_decl()
        del decl["use"][0][missing_key]
        issues = validate_declaration(decl)
        assert any(missing_key in i.path for i in issues)

    def test_nc_two_part_schema_version_fails(self) -> None:
        decl = _minimal_valid_decl()
        decl["parity"]["schema_version"] = "1.0"
        issues = validate_declaration(decl)
        assert any(i.code == "G01_SCHEMA_VERSION" for i in issues)

    def test_nc_zero_required_cells_fails(self) -> None:
        decl = _minimal_valid_decl()
        decl["cell_plan"][0]["exploratory"] = True
        issues = validate_declaration(decl)
        assert any(i.code == "G01_ZERO_REQUIRED_CELLS" for i in issues)

    def test_nc_precision_amp_within_tolerance_fallback_fails(self) -> None:
        decl = _minimal_valid_decl()
        fb = copy.deepcopy(decl["use"][0])
        fb["id"] = "fb-bad"
        fb["parity_kind"] = "within_tolerance"
        decl["use"][0]["precision_amplification"] = {
            "fallback_use": fb,
            "max_useful_tolerance": {"float32": {"atol": 1e-3, "rtol": 1e-3}},
        }
        issues = validate_declaration(decl)
        assert any(i.code == "G01_PRECISION_AMP_KIND" for i in issues)

    def test_nc_implementation_revision_forbidden(self) -> None:
        decl = _minimal_valid_decl()
        decl["implementation"]["revision"] = "deadbeef"
        issues = validate_declaration(decl)
        assert any(i.path == "implementation.revision" for i in issues)

    def test_nc_correlation_metric_under_within_tolerance_fails(self) -> None:
        decl = _minimal_valid_decl()
        decl["use"][0]["metric"]["id"] = "spearman_rho"
        issues = validate_declaration(decl)
        assert any(i.code == "G10_CORRELATION_REQUIRES_ASSOCIATION" for i in issues)

    @pytest.mark.parametrize("wp3b_key", ["component", "rng", "check", "reference_set", "provenance"])
    def test_wp03b_blocks_refused_naming_package(self, wp3b_key: str) -> None:
        decl = _minimal_valid_decl()
        decl[wp3b_key] = {}
        issues = validate_declaration(decl)
        assert any(i.code == "WP03B_UNAVAILABLE" and "WP03b" in i.message for i in issues)

    def test_nc_sidecar_walker_and_find_sidecar_exclude_parity_and_discrepancies(
        self, tmp_path: Path
    ) -> None:
        exp_dir = tmp_path / "scripts" / "experiments"
        exp_dir.mkdir(parents=True)
        script = exp_dir / "x.parity.py"
        script.write_text("print('hi')\n", encoding="utf-8")
        parity_decl = exp_dir / "x.parity.bth.toml"
        parity_decl.write_text(render_declaration_scaffold("demo-v1"), encoding="utf-8")
        disc_toml = exp_dir / "x.discrepancies.bth.toml"
        disc_toml.write_text("[discrepancy]\n", encoding="utf-8")
        normal_sidecar = exp_dir / "train_model.bth.toml"
        normal_sidecar.write_text("[experiment]\nhypothesis = 'h'\n", encoding="utf-8")

        assert is_parity_or_discrepancy_toml(parity_decl)
        assert is_parity_or_discrepancy_toml(disc_toml)
        assert find_sidecar(script) is None
        walked = iter_project_sidecars(tmp_path)
        assert parity_decl not in walked
        assert disc_toml not in walked
        assert normal_sidecar in walked
        assert check_positive_control_missing(tmp_path) == []
        sweep_paths = [p for p, _kind in iter_sweep_artifacts(tmp_path)]
        assert parity_decl not in sweep_paths
        assert disc_toml not in sweep_paths
        # P2 walker gaps: extra_paths, discover_sidecar, archive_sweep
        assert discover_sidecar(script) is None
        extra_sweep = [p for p, _k in iter_sweep_artifacts(tmp_path, extra_paths=[parity_decl, disc_toml])]
        assert parity_decl not in extra_sweep and disc_toml not in extra_sweep
        assert _find_companion_sidecars(tmp_path, script) == []
        cat_dir = tmp_path / "cat"
        init_catalog(cat_dir)
        stale_parity = exp_dir / "stale.parity.bth.toml"
        stale_parity.write_text(render_declaration_scaffold("stale-v1") + "\n[status]\nstale = true\n", encoding="utf-8")
        cands = scan_archive_candidates(tmp_path, cat_dir, "demo")
        assert all(Path(c["path"]).name != "stale.parity.bth.toml" for c in cands)

    def test_nc_expanded_required_d_fields_empty_strings_and_amendments(self) -> None:
        # Empty string rejected on required string fields
        decl = _minimal_valid_decl()
        decl["implementation"]["name"] = ""
        assert any(i.code == "G01_EMPTY_FIELD" and "implementation.name" in i.path for i in validate_declaration(decl))

        # Non-64-hex registry_id rejected
        decl_reg = _minimal_valid_decl()
        decl_reg["registry"]["registry_id"] = "short"
        assert any("registry_id" in i.path for i in validate_declaration(decl_reg))

        # Missing [[reference]] identity fields across kinds
        for kind, identity in (
            ("upstream_live", {"repo": "https://x"}),
            ("sealed_dump", {"dump_sha256": "a" * 64}),
            ("published_values", {"doi": "10.1/x", "locator": "T1"}),
            ("experimental_data", {"accession": "ACC1"}),
            ("own_prior_version", {"repo": "https://x"}),
            ("reference_implementation", {"repo": "https://x", "commit": "a" * 40}),
        ):
            d = _minimal_valid_decl()
            d["reference"][0]["kind"] = kind
            d["reference"][0]["identity"] = identity
            if kind == "experimental_data":
                d["use"][0]["parity_kind"] = "association"
                d["use"][0].update(
                    {"pairing_unit": "item", "min_n": 3, "interval": "ci", "threshold": 0.5, "multiplicity": "none"}
                )
            assert validate_declaration(d), f"Expected validation failure for incomplete {kind} identity"

        # Kind-specific [[use]] required fields
        for kind in ("association", "distributional", "behavioural", "approximate"):
            d = _minimal_valid_decl()
            d["use"][0]["parity_kind"] = kind
            issues = validate_declaration(d)
            assert issues, f"Expected missing kind-specific fields for {kind}"

        # Missing [[tolerance]] when within_tolerance (top-level or fallback_use)
        d_tol = _minimal_valid_decl()
        del d_tol["tolerance"]
        assert any(i.code == "G01_MISSING_SECTION" and i.path == "tolerance" for i in validate_declaration(d_tol))

        d_fb_tol = _minimal_valid_decl()
        del d_fb_tol["tolerance"]
        d_fb_tol["use"][0]["parity_kind"] = "bitwise"
        fb_use = copy.deepcopy(d_fb_tol["use"][0])
        fb_use["id"] = "fb-tol"
        fb_use["parity_kind"] = "within_tolerance"
        d_fb_tol["cell_plan"][0]["incompatibility"] = {
            "class": "convention",
            "evidence": "note",
            "fallback_use": fb_use,
        }
        assert any(i.code == "G01_MISSING_SECTION" and i.path == "tolerance" for i in validate_declaration(d_fb_tol))

        # Empty applies_to rejected
        d_app = _minimal_valid_decl()
        d_app["tolerance"][0]["applies_to"] = []
        assert any("applies_to" in i.path for i in validate_declaration(d_app))

        # supersedes requires [amendment], [amendment] requires derived_from_observation
        d_sup = _minimal_valid_decl()
        d_sup["parity"]["supersedes"] = "demo-record-v1@1234567890ab"
        assert any(i.path == "amendment" for i in validate_declaration(d_sup))
        d_sup["amendment"] = {"reason": "fix", "judgement_id": "jdg_1"}
        assert any("derived_from_observation" in i.path for i in validate_declaration(d_sup))
        d_sup["amendment"]["derived_from_observation"] = "obs_1"
        assert validate_declaration(d_sup) == []

        # Missing cell_plan / frameworks subfields
        d_cp = _minimal_valid_decl()
        del d_cp["cell_plan"][0]["pooling_rule"]
        assert any("pooling_rule" in i.path for i in validate_declaration(d_cp))
        d_fw = _minimal_valid_decl()
        del d_fw["environment"]["frameworks"]["reference"]
        assert any("frameworks.reference" in i.path for i in validate_declaration(d_fw))

    def test_cli_validate_and_scaffold_end_to_end(self, tmp_path: Path) -> None:
        decl_path = tmp_path / "demo.parity.bth.toml"
        out_path = tmp_path / "scaffolded.parity.bth.toml"
        proc_scaf = _run_bth_cli("parity", "scaffold", "cli-demo-v1", "--out", str(out_path), cwd=tmp_path)
        assert proc_scaf.returncode == 0
        assert out_path.is_file()

        # Invalid record_id rejected by scaffold
        proc_bad_scaf = _run_bth_cli("parity", "scaffold", "BAD/ID", cwd=tmp_path)
        assert proc_bad_scaf.returncode == 1

        # validate valid + --json
        proc_val = _run_bth_cli("parity", "validate", str(out_path), "--json", cwd=tmp_path)
        assert proc_val.returncode == 0
        assert json.loads(proc_val.stdout)["ok"] is True

        # validate invalid exits 1
        decl_path.write_text(out_path.read_text(encoding="utf-8").replace('mode = "confirmatory"\n', ""), encoding="utf-8")
        proc_val_bad = _run_bth_cli("parity", "validate", str(decl_path), "--json", cwd=tmp_path)
        assert proc_val_bad.returncode == 1
        assert json.loads(proc_val_bad.stdout)["ok"] is False


class TestDeclareCommand:
    def test_declare_writes_blob_head_and_preserves_first_blob(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        head_sha = _init_git_repo(repo)
        store_dir = tmp_path / "store"
        monkeypatch.setenv("BTH_PARITY_STORE", str(store_dir))

        decl_path = repo / "demo.parity.bth.toml"
        raw_v1 = render_declaration_scaffold("demo-record-v1")
        decl_path.write_text(raw_v1, encoding="utf-8")

        app(["parity", "declare", str(decl_path), "--json"], exit_on_error=False)
        res1 = json.loads(capsys.readouterr().out)
        assert res1["record_id"] == "demo-record-v1"
        assert res1["head_commit"] == head_sha
        assert res1["is_first_declaration"] is True
        assert Path(res1["blob_path"]).is_file()

        # Second declare with modified tolerance updates blob, keeps first_declaration_sha256 unchanged
        raw_v2 = raw_v1.replace("atol = 1e-4", "atol = 1e-2")
        decl_path.write_text(raw_v2, encoding="utf-8")
        res2 = declare_parity_record(decl_path, parity_store=store_dir)
        assert res2.is_first_declaration is False
        assert res2.declaration_sha256 != res1["declaration_sha256"]
        assert res2.first_declaration_sha256 == res1["declaration_sha256"]


class TestParityDeclarationSchema:
    def test_item7_schema_rejects_nan_and_inf_tolerances(self) -> None:
        # 1. [[tolerance]] atol/rtol with nan, +inf, -inf
        for field, bad_val in (
            ("atol", float("nan")),
            ("rtol", float("inf")),
            ("atol", float("-inf")),
        ):
            decl = _minimal_valid_decl()
            decl["tolerance"][0][field] = bad_val
            issues = validate_declaration(decl)
            assert any(i.code == "G01_VALUE" and f"tolerance[0].{field}" in i.path for i in issues), (field, bad_val, issues)

        # 2. floor_multiple = inf on [[tolerance]] and [[use]]
        decl_fm_tol = _minimal_valid_decl()
        decl_fm_tol["tolerance"][0]["basis"] = "decomposition_floor"
        decl_fm_tol["tolerance"][0]["floor_multiple"] = float("inf")
        decl_fm_tol["tolerance"][0]["basis_evidence"] = {
            "run_id": "r1",
            "data_tier": "toy",
            "shapes": [1],
            "dtypes": ["float32"],
        }
        assert any(
            i.code == "G01_VALUE" and "tolerance[0].floor_multiple" in i.path
            for i in validate_declaration(decl_fm_tol)
        )

        decl_fm_use = _minimal_valid_decl()
        decl_fm_use["use"][0]["floor_multiple"] = float("inf")
        assert any(
            i.code == "G01_VALUE" and "use[0].floor_multiple" in i.path
            for i in validate_declaration(decl_fm_use)
        )

        # 3. precision_amplification.max_useful_tolerance.<dtype>.{atol,rtol} = nan/inf
        for field, bad_val in (
            ("atol", float("nan")),
            ("rtol", float("inf")),
            ("atol", float("-inf")),
            ("rtol", -1.0),
        ):
            decl_pa = _minimal_valid_decl()
            fb = copy.deepcopy(decl_pa["use"][0])
            fb["id"] = "fb-dist"
            fb["parity_kind"] = "distributional"
            fb.update(
                {
                    "delta": 0.1,
                    "independent_unit": "sample",
                    "procedure": "tost",
                    "alpha": 0.05,
                    "multiplicity": "none",
                }
            )
            mut_entry = {"atol": 1e-3, "rtol": 1e-3}
            mut_entry[field] = bad_val
            decl_pa["use"][0]["precision_amplification"] = {
                "fallback_use": fb,
                "max_useful_tolerance": {"float32": mut_entry},
            }
            issues_pa = validate_declaration(decl_pa)
            assert any(
                i.code == "G01_VALUE"
                and f"use[0].precision_amplification.max_useful_tolerance.float32.{field}" in i.path
                for i in issues_pa
            ), (field, bad_val, issues_pa)


class TestImplementationWeightsSchema:
    @pytest.mark.parametrize("status", ["rederived", "attested_only", "attested_only_source_deleted"])
    def test_valid_weights_statuses_accepted(self, status: str) -> None:
        decl = _minimal_valid_decl()
        decl["implementation"]["loads_trained_parameters"] = True
        decl["implementation"]["weights"] = {
            "artifact_path": "data/weights/model.eqx.zst",
            "artifact_sha256": "a" * 64,
            "source_checkpoint_status": status,
            "hf_repo": "owner/repo",
            "hf_revision": "b" * 40,
        }
        assert validate_declaration(decl) == []
        jsonschema.validate(instance=decl, schema=_load_json_schema("parity_declaration.v1.json"))

    @pytest.mark.parametrize(
        "bad_status",
        ["verified_local", "Attested_Only", "attested-only", "REDERIVED", "", "unknown"],
    )
    def test_nc_invalid_source_checkpoint_status_rejected(self, bad_status: str) -> None:
        decl = _minimal_valid_decl()
        decl["implementation"]["weights"] = {
            "artifact_path": "data/weights/model.eqx.zst",
            "artifact_sha256": "a" * 64,
            "source_checkpoint_status": bad_status,
        }
        issues = validate_declaration(decl)
        assert any("source_checkpoint_status" in i.path for i in issues), (bad_status, issues)

    def test_nc_missing_weights_fields_and_bad_sha_rejected(self) -> None:
        for missing_field in ("artifact_path", "artifact_sha256", "source_checkpoint_status"):
            decl = _minimal_valid_decl()
            decl["implementation"]["weights"] = {
                "artifact_path": "data/weights/model.eqx.zst",
                "artifact_sha256": "a" * 64,
                "source_checkpoint_status": "rederived",
            }
            del decl["implementation"]["weights"][missing_field]
            issues = validate_declaration(decl)
            assert any(missing_field in i.path for i in issues), (missing_field, issues)

        # Uppercase or non-64 hex sha256 rejected
        for bad_sha in ("A" * 64, "abc", "g" * 64, ""):
            decl = _minimal_valid_decl()
            decl["implementation"]["weights"] = {
                "artifact_path": "data/weights/model.eqx.zst",
                "artifact_sha256": bad_sha,
                "source_checkpoint_status": "attested_only",
            }
            issues = validate_declaration(decl)
            assert any("artifact_sha256" in i.path for i in issues), (bad_sha, issues)

        # Empty optional hf_repo / hf_revision rejected
        for opt_field in ("hf_repo", "hf_revision"):
            decl = _minimal_valid_decl()
            decl["implementation"]["weights"] = {
                "artifact_path": "data/weights/model.eqx.zst",
                "artifact_sha256": "a" * 64,
                "source_checkpoint_status": "attested_only",
                opt_field: "   ",
            }
            issues = validate_declaration(decl)
            assert any(opt_field in i.path for i in issues), (opt_field, issues)

    def test_loads_trained_parameters_requires_weights_and_must_be_bool(self) -> None:
        # Omitted or false -> missing [implementation.weights] allowed
        decl_omitted = _minimal_valid_decl()
        assert validate_declaration(decl_omitted) == []

        decl_false = _minimal_valid_decl()
        decl_false["implementation"]["loads_trained_parameters"] = False
        assert validate_declaration(decl_false) == []

        # Non-bool loads_trained_parameters -> rejected
        decl_bad_type = _minimal_valid_decl()
        decl_bad_type["implementation"]["loads_trained_parameters"] = "yes"
        assert any("loads_trained_parameters" in i.path for i in validate_declaration(decl_bad_type))

        # True without [implementation.weights] -> rejected
        decl_true_missing = _minimal_valid_decl()
        decl_true_missing["implementation"]["loads_trained_parameters"] = True
        issues = validate_declaration(decl_true_missing)
        assert any("implementation.weights" in i.path for i in issues), issues

