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


class TestTrackAP0P1NonCodePrecisionRegressions:
    def test_resolve_sidecar_text_prefers_frozen_toml_snapshot_and_verifies_live_sha(
        self, repo: Path, catalog_dir: Path
    ) -> None:
        from bathos.sidecar import sidecar_snapshot_path

        sc_dir = repo / "scripts" / "experiments"
        sc_dir.mkdir(parents=True)
        sc = sc_dir / "exp_snap.bth.toml"
        original_toml = (
            '[experiment]\nhypothesis = "h1"\n\n'
            '[outcomes.pass]\ncondition = "loss < 0.5"\ndecision = "ok"\n'
        )
        sc.write_text(original_toml)
        orig_sha = hashlib.sha256(sc.read_bytes()).hexdigest()

        # Freeze raw TOML snapshot, then mutate live file so it no longer has [outcomes.pass]
        snap_path = sidecar_snapshot_path(catalog_dir, orig_sha)
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(original_toml, encoding="utf-8")
        sc.write_text('[experiment]\nhypothesis = "mutated"\n')

        r_snap = Run(
            project_slug="proj",
            command="uv run python scripts/experiments/exp_snap.py",
            argv=["uv", "run", "python", "scripts/experiments/exp_snap.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            sidecar_path="scripts/experiments/exp_snap.bth.toml",
            sidecar_sha256=orig_sha,
        )

        # Live file without frozen snapshot whose SHA does not match -> fail closed (unverifiable)
        sc_unfrozen = sc_dir / "exp_tampered.bth.toml"
        sc_unfrozen.write_text(
            '[experiment]\nhypothesis = "edited"\n\n'
            '[outcomes.pass]\ncondition = "loss < 0.5"\ndecision = "ok"\n'
        )
        r_tampered = Run(
            project_slug="proj",
            command="uv run python scripts/experiments/exp_tampered.py",
            argv=["uv", "run", "python", "scripts/experiments/exp_tampered.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            sidecar_path="scripts/experiments/exp_tampered.bth.toml",
            sidecar_sha256="0" * 64,
        )
        write_run(r_snap, catalog_dir)
        write_run(r_tampered, catalog_dir)

        rep_snap = assess_blast_radius(
            catalog_dir, repo, sidecar="scripts/experiments/exp_snap.bth.toml#outcomes.pass"
        )
        assert [m.run_id for m in rep_snap.affected] == [r_snap.id]

        rep_tampered = assess_blast_radius(
            catalog_dir, repo, sidecar="scripts/experiments/exp_tampered.bth.toml#outcomes.pass"
        )
        assert rep_tampered.affected == []
        assert [m.run_id for m in rep_tampered.unverifiable] == [r_tampered.id]

    def test_sidecar_anchor_boundary_toml_clause_and_sha_prefix(
        self, repo: Path, catalog_dir: Path
    ) -> None:
        sc_dir = repo / "scripts" / "experiments"
        sc_dir.mkdir(parents=True)
        # Uses dotted key under [outcomes], plus a comment/prose mention in the other file
        sc_good = sc_dir / "exp_a.bth.toml"
        sc_good.write_text(
            '[experiment]\nhypothesis = "h"\n\n[outcomes]\npass.condition = "x < 1"\n'
        )
        sc_decoy = sc_dir / "foo_exp_a.bth.toml"
        sc_decoy.write_text(
            '# [outcomes.pass]\n[experiment]\nhypothesis = "mentions outcomes.pass in prose"\n'
        )
        sha_good = hashlib.sha256(sc_good.read_bytes()).hexdigest()
        sha_decoy = hashlib.sha256(sc_decoy.read_bytes()).hexdigest()

        r_good = Run(
            project_slug="proj",
            command="python scripts/experiments/exp_a.py",
            argv=["python", "scripts/experiments/exp_a.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            sidecar_path="scripts/experiments/exp_a.bth.toml",
            sidecar_sha256=sha_good,
        )
        r_decoy = Run(
            project_slug="proj",
            command="python scripts/experiments/foo_exp_a.py",
            argv=["python", "scripts/experiments/foo_exp_a.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            sidecar_path="scripts/experiments/foo_exp_a.bth.toml",
            sidecar_sha256=sha_decoy,
        )
        write_run(r_good, catalog_dir)
        write_run(r_decoy, catalog_dir)

        # Boundary: exp_a.bth.toml matches scripts/experiments/exp_a.bth.toml, NOT foo_exp_a.bth.toml
        rep_boundary = assess_blast_radius(catalog_dir, repo, sidecar="exp_a.bth.toml")
        assert [m.run_id for m in rep_boundary.affected] == [r_good.id]
        assert r_decoy.id in rep_boundary.unaffected_run_ids

        # Dotted TOML clause matches r_good; comment/prose decoy does NOT match outcomes.pass
        rep_clause = assess_blast_radius(
            catalog_dir, repo, sidecar="exp_a.bth.toml#outcomes.pass.condition"
        )
        assert [m.run_id for m in rep_clause.affected] == [r_good.id]
        rep_decoy_clause = assess_blast_radius(
            catalog_dir, repo, sidecar="foo_exp_a.bth.toml#outcomes.pass"
        )
        assert rep_decoy_clause.affected == []
        assert r_decoy.id in rep_decoy_clause.unaffected_run_ids

        # sha256:<prefix> works
        rep_sha = assess_blast_radius(catalog_dir, repo, sidecar=f"sha256:{sha_good[:12]}")
        assert [m.run_id for m in rep_sha.affected] == [r_good.id]

    def test_input_artifact_digest_only_boundary_and_coupled_match(
        self, repo: Path, catalog_dir: Path
    ) -> None:
        r_both = Run(
            project_slug="proj",
            command="python run.py --in data/a.csv",
            argv=["python", "run.py", "--in", "data/a.csv"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            metadata=json.dumps({"digest": "sha256:abcd1234ef"}),
        )
        r_digest_only_other_path = Run(
            project_slug="proj",
            command="python run.py --in data/other.csv",
            argv=["python", "run.py", "--in", "data/other.csv"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            metadata=json.dumps({"digest": "sha256:abcd1234ef"}),
        )
        r_decoy_boundary = Run(
            project_slug="proj",
            command="python run.py --in data/a.csv_backup",
            argv=["python", "run.py", "--in", "data/a.csv_backup"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            metadata=json.dumps({"digest": "sha256:0abcd1234ef"}),
        )
        for r in (r_both, r_digest_only_other_path, r_decoy_boundary):
            write_run(r, catalog_dir)

        # Digest-only without '@' matches hex boundary, not '0abcd1234ef'
        rep_d = assess_blast_radius(catalog_dir, repo, input_artifact="sha256:abcd1234ef")
        assert {m.run_id for m in rep_d.affected} == {r_both.id, r_digest_only_other_path.id}
        assert r_decoy_boundary.id in rep_d.unaffected_run_ids

        # Path-only respects boundary
        rep_p = assess_blast_radius(catalog_dir, repo, input_artifact="data/a.csv")
        assert [m.run_id for m in rep_p.affected] == [r_both.id]

        # Coupled path@digest requires BOTH path AND digest -> r_digest_only_other_path is unaffected
        rep_coupled = assess_blast_radius(
            catalog_dir, repo, input_artifact="data/a.csv@sha256:abcd1234ef"
        )
        assert [m.run_id for m in rep_coupled.affected] == [r_both.id]
        assert r_digest_only_other_path.id in rep_coupled.unaffected_run_ids
        assert r_decoy_boundary.id in rep_coupled.unaffected_run_ids

    def test_instrument_partial_prov_unparseable_and_git_last_affected(
        self, repo: Path, catalog_dir: Path
    ) -> None:
        # Build 3 commits in repo for GIT range testing
        (repo / "lib.py").write_text("v = 1\n")
        subprocess.run(["git", "add", "lib.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "c1"], cwd=repo, check=True)
        c_intro = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

        (repo / "lib.py").write_text("v = 2\n")
        subprocess.run(["git", "add", "lib.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "c2"], cwd=repo, check=True)
        c_bad = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

        (repo / "lib.py").write_text("v = 3\n")
        subprocess.run(["git", "add", "lib.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "c3"], cwd=repo, check=True)
        c_fix = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

        inst_dir = repo / ".bth" / "instruments"
        inst_dir.mkdir(parents=True)
        (inst_dir / "aminx.toml").write_text(
            f"""
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "AMINX-2026-LAST"
severity = "void"
description = "last_affected range"
[[entries.ranges]]
type = "ECOSYSTEM"
events = [{{ introduced = "1.0.0" }}, {{ last_affected = "1.2.0" }}]

[[entries]]
id = "AMINX-2026-GIT"
severity = "void"
description = "git range"
[[entries.ranges]]
type = "GIT"
events = [{{ introduced = "{c_intro}" }}, {{ fixed = "{c_fix}" }}]
"""
        )

        r_partial = Run(
            project_slug="proj",
            command="python p.py",
            argv=["python", "p.py"],
            git_hash=c_bad,
            git_branch="main",
            git_dirty=False,
            dependency_provenance=json.dumps({"other_pkg_version": "9.9.9"}),
        )
        r_unparseable = Run(
            project_slug="proj",
            command="python u.py",
            argv=["python", "u.py"],
            git_hash=c_bad,
            git_branch="main",
            git_dirty=False,
            dependency_provenance=json.dumps({"aminx_version": "not-a-semver"}),
        )
        r_last_hit = Run(
            project_slug="proj",
            command="python l.py",
            argv=["python", "l.py"],
            git_hash=c_bad,
            git_branch="main",
            git_dirty=False,
            dependency_provenance=json.dumps({"aminx_version": "1.2.0"}),
        )
        r_git_fixed = Run(
            project_slug="proj",
            command="python f.py",
            argv=["python", "f.py"],
            git_hash=c_fix,
            git_branch="main",
            git_dirty=False,
            dependency_provenance=json.dumps({"aminx_source_commit": c_fix}),
        )
        r_git_bad = Run(
            project_slug="proj",
            command="python g.py",
            argv=["python", "g.py"],
            git_hash=c_bad,
            git_branch="main",
            git_dirty=False,
            dependency_provenance=json.dumps({"aminx_source_commit": c_bad}),
        )
        for r in (r_partial, r_unparseable, r_last_hit, r_git_fixed, r_git_bad):
            write_run(r, catalog_dir)

        rep_last = assess_blast_radius(catalog_dir, repo, instrument="AMINX-2026-LAST")
        assert [m.run_id for m in rep_last.affected] == [r_last_hit.id]
        assert {r_partial.id, r_unparseable.id}.issubset({m.run_id for m in rep_last.unverifiable})

        rep_git = assess_blast_radius(catalog_dir, repo, instrument="AMINX-2026-GIT")
        assert [m.run_id for m in rep_git.affected] == [r_git_bad.id]
        assert r_git_fixed.id in rep_git.unaffected_run_ids

        # Warning formats inst_pkg cleanly when no run recorded a different package
        rep_none = assess_blast_radius(catalog_dir, repo, instrument="untracked_pkg")
        assert rep_none.affected == []
        assert any("untracked_pkg" in w and "{inst_pkg}" not in w for w in rep_none.warnings)
