"""Tests for non-code defect anchors (`sidecar`, `input_artifact`, `instrument`) (#2686)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from bathos.blast_radius import (
    assess_blast_radius,
    flag_blast_radius,
    fold_blast_radius_state,
    latest_ledger_record,
)
from bathos.catalog import init_catalog, write_run
from bathos.schema import Run


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=r, check=True)
    return r


@pytest.fixture
def catalog_dir(tmp_path: Path) -> Path:
    cat = tmp_path / "catalog"
    init_catalog(cat)
    return cat


class TestSidecarAnchor:
    def test_matches_by_sidecar_path_and_optional_clause(self, repo: Path, catalog_dir: Path) -> None:
        sc_dir = repo / "scripts" / "experiments"
        sc_dir.mkdir(parents=True)
        sc1 = sc_dir / "exp_a.bth.toml"
        sc1.write_text(
            '[experiment]\nhypothesis = "h1"\n\n[outcomes.pass]\ncondition = "loss < 0.5"\ndecision = "ok"\n'
        )
        sc2 = sc_dir / "exp_b.bth.toml"
        sc2.write_text(
            '[experiment]\nhypothesis = "h2"\n\n[outcomes.fail]\ncondition = "loss >= 1.0"\ndecision = "no"\n'
        )
        sha1 = hashlib.sha256(sc1.read_bytes()).hexdigest()
        sha2 = hashlib.sha256(sc2.read_bytes()).hexdigest()

        r1 = Run(
            project_slug="proj",
            command="uv run python scripts/experiments/exp_a.py",
            argv=["uv", "run", "python", "scripts/experiments/exp_a.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            sidecar_path="scripts/experiments/exp_a.bth.toml",
            sidecar_sha256=sha1,
        )
        r2 = Run(
            project_slug="proj",
            command="uv run python scripts/experiments/exp_b.py",
            argv=["uv", "run", "python", "scripts/experiments/exp_b.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            sidecar_path="scripts/experiments/exp_b.bth.toml",
            sidecar_sha256=sha2,
        )
        write_run(r1, catalog_dir)
        write_run(r2, catalog_dir)

        # Path-only anchor
        rep_path = assess_blast_radius(
            catalog_dir, repo, sidecar="scripts/experiments/exp_a.bth.toml"
        )
        assert rep_path.anchor_kind == "sidecar"
        assert [m.run_id for m in rep_path.affected] == [r1.id]
        assert r2.id in rep_path.unaffected_run_ids

        # Path + clause anchor matching outcomes.pass
        rep_clause = assess_blast_radius(
            catalog_dir, repo, sidecar="scripts/experiments/exp_a.bth.toml#outcomes.pass"
        )
        assert [m.run_id for m in rep_clause.affected] == [r1.id]
        assert "outcomes.pass" in rep_clause.affected[0].reason

        # Path + non-matching clause -> unaffected
        rep_miss = assess_blast_radius(
            catalog_dir, repo, sidecar="scripts/experiments/exp_a.bth.toml#outcomes.nonexistent"
        )
        assert rep_miss.affected == []
        assert r1.id in rep_miss.unaffected_run_ids

        # SHA prefix anchor
        rep_sha = assess_blast_radius(catalog_dir, repo, sidecar=sha2[:12])
        assert [m.run_id for m in rep_sha.affected] == [r2.id]

    def test_clause_with_unreadable_sidecar_is_unverifiable(
        self, repo: Path, catalog_dir: Path
    ) -> None:
        r = Run(
            project_slug="proj",
            command="uv run python scripts/experiments/gone.py",
            argv=["uv", "run", "python", "scripts/experiments/gone.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            sidecar_path="scripts/experiments/gone.bth.toml",
            sidecar_sha256="deadbeef" * 8,
        )
        write_run(r, catalog_dir)

        rep = assess_blast_radius(
            catalog_dir, repo, sidecar="scripts/experiments/gone.bth.toml#outcomes.pass"
        )
        assert rep.affected == []
        assert [m.run_id for m in rep.unverifiable] == [r.id]
        assert "unreadable" in rep.unverifiable[0].reason


class TestInputArtifactAnchor:
    def test_matches_path_and_path_at_digest(self, repo: Path, catalog_dir: Path) -> None:
        r_digest = Run(
            project_slug="proj",
            command="python train.py --data data/bad_split.parquet",
            argv=["python", "train.py", "--data", "data/bad_split.parquet"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            metadata=json.dumps({"input_digest": "sha256:abcd1234ef"}),
        )
        r_path_only = Run(
            project_slug="proj",
            command="python eval.py --data data/bad_split.parquet",
            argv=["python", "eval.py", "--data", "data/bad_split.parquet"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            metadata="{}",
        )
        r_other = Run(
            project_slug="proj",
            command="python train.py --data data/clean_split.parquet",
            argv=["python", "train.py", "--data", "data/clean_split.parquet"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
        )
        write_run(r_digest, catalog_dir)
        write_run(r_path_only, catalog_dir)
        write_run(r_other, catalog_dir)

        # Path-only anchor flags both runs referencing data/bad_split.parquet
        rep = assess_blast_radius(catalog_dir, repo, input_artifact="data/bad_split.parquet")
        assert rep.anchor_kind == "input_artifact"
        assert {m.run_id for m in rep.affected} == {r_digest.id, r_path_only.id}
        assert r_other.id in rep.unaffected_run_ids

        # path@digest: r_digest is affected, r_path_only (missing digest) is unverifiable
        rep_pin = assess_blast_radius(
            catalog_dir, repo, input_artifact="data/bad_split.parquet@sha256:abcd1234ef"
        )
        assert [m.run_id for m in rep_pin.affected] == [r_digest.id]
        assert [m.run_id for m in rep_pin.unverifiable] == [r_path_only.id]
        assert r_other.id in rep_pin.unaffected_run_ids

    def test_unmatched_input_artifact_emits_coverage_warning(
        self, repo: Path, catalog_dir: Path
    ) -> None:
        write_run(
            Run(
                project_slug="proj",
                command="python noop.py",
                argv=["python", "noop.py"],
                git_hash="abc",
                git_branch="main",
                git_dirty=False,
            ),
            catalog_dir,
        )
        rep = assess_blast_radius(catalog_dir, repo, input_artifact="data/never_seen.csv")
        assert rep.affected == []
        assert any("unverified, not clean" in w for w in rep.warnings)


class TestInstrumentAnchor:
    def test_instrument_package_version_and_defect_range(
        self, repo: Path, catalog_dir: Path
    ) -> None:
        inst_dir = repo / ".bth" / "instruments"
        inst_dir.mkdir(parents=True)
        (inst_dir / "aminx.toml").write_text(
            """
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "AMINX-2026-001"
severity = "void"
description = "bad radius"
[[entries.ranges]]
type = "ECOSYSTEM"
events = [{ introduced = "0.12.0" }, { fixed = "0.12.4" }]
"""
        )

        r_bad = Run(
            project_slug="proj",
            command="python a.py",
            argv=["python", "a.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            dependency_provenance=json.dumps({"aminx_version": "0.12.1"}),
        )
        r_fixed = Run(
            project_slug="proj",
            command="python b.py",
            argv=["python", "b.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            dependency_provenance=json.dumps({"aminx_version": "0.12.4"}),
        )
        r_unknown = Run(
            project_slug="proj",
            command="python c.py",
            argv=["python", "c.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            dependency_provenance=json.dumps({"aminx_version": "unknown"}),
        )
        r_missing_prov = Run(
            project_slug="proj",
            command="python d.py",
            argv=["python", "d.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            dependency_provenance=None,
        )
        for r in (r_bad, r_fixed, r_unknown, r_missing_prov):
            write_run(r, catalog_dir)

        # Defect ID anchor checks version range [0.12.0, 0.12.4)
        rep_def = assess_blast_radius(catalog_dir, repo, instrument="AMINX-2026-001")
        assert rep_def.anchor_kind == "instrument"
        assert [m.run_id for m in rep_def.affected] == [r_bad.id]
        assert {m.run_id for m in rep_def.unverifiable} == {r_unknown.id, r_missing_prov.id}
        assert r_fixed.id in rep_def.unaffected_run_ids

        # Exact version anchor pkg@version
        rep_pin = assess_blast_radius(catalog_dir, repo, instrument="aminx@0.12.1")
        assert [m.run_id for m in rep_pin.affected] == [r_bad.id]
        assert r_fixed.id in rep_pin.unaffected_run_ids

        # Whole-package anchor
        rep_pkg = assess_blast_radius(catalog_dir, repo, instrument="aminx")
        assert {m.run_id for m in rep_pkg.affected} == {r_bad.id, r_fixed.id}

        # Durable ledger flagging works with non-code anchor_kind
        records = flag_blast_radius(rep_def, catalog_dir)
        assert len(records) == 3
        assert fold_blast_radius_state(catalog_dir, "run", r_bad.id) == "affected"
        latest = latest_ledger_record(catalog_dir, "run", r_bad.id)
        assert latest is not None and latest.anchor_kind == "instrument"


class TestNonCodeAnchorValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"sidecar": ""},
            {"sidecar": "   "},
            {"sidecar": "#clause"},
            {"sidecar": "foo.bth.toml#"},
            {"input_artifact": ""},
            {"input_artifact": "@sha256:abc"},
            {"input_artifact": "data.csv@"},
            {"instrument": ""},
            {"instrument": "aminx:"},
            {"instrument": "aminx@"},
            {"commit": "abc", "sidecar": "foo.bth.toml"},
            {"sidecar": "foo.bth.toml", "instrument": "aminx"},
        ],
    )
    def test_malformed_or_conflicting_anchors_raise(
        self, repo: Path, catalog_dir: Path, kwargs: dict
    ) -> None:
        with pytest.raises(ValueError):
            assess_blast_radius(catalog_dir, repo, **kwargs)
