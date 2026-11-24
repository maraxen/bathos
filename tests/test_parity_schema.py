"""Tests for WP03a: parity record schema core, validator, declare, and sidecar exclusion."""

from __future__ import annotations

import copy
import json
import subprocess
import tomllib
from pathlib import Path

import pytest

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
from bathos.sidecar import find_sidecar, is_parity_or_discrepancy_toml


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
            "grade_inputs": {
                "as_of": "2026-11-24T00:00:00Z",
                "target_revisions": {},
                "drift_run_id": None,
                "ledger_high_water_mark": None,
                "gate_stages": {},
                "comparison_backend": {"name": "numpy"},
            },
            "evidence_runs": [],
            "cells": [{"cell_id": "0" * 16, "verdict": "not_run"}],
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
