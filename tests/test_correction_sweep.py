"""Tests for #3099: correction sweep module, linter integration, and HYG-008 card."""

from __future__ import annotations

import textwrap
from pathlib import Path

from bathos.corpus import load_corpus
from bathos.correction_sweep import MethodCorrection, load_corrections, sweep_corrections
from bathos.linter import IssueSeverity, check_correction_sweep, lint_project


def _write_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def test_sweep_finds_stale_sidecars_claims_and_specs(tmp_path: Path):
    _write_file(
        tmp_path / ".bth" / "corrections.toml",
        """
        [[corrections]]
        id = "CORR-001"
        summary = "Drop per-replica converged gate; pool replicas at build unit"
        old_assumptions = ["converged: true", "per_replica_converged"]
        old_patterns = ["\\\\bconverged\\\\s*=\\\\s*true\\\\b"]
        replacement = "pooled_se with [breakdown] unit = 'build'"

        [[corrections]]
        id = "CORR-002"
        summary = "Do not use Boltz-2 affinity"
        old_assumptions = ["Boltz-2 affinity", "boltz2_affinity"]
        replacement = "experimental SPR Kd"
        """,
    )

    # Active sidecar still gating on per_replica_converged and boltz2_affinity schema key
    _write_file(
        tmp_path / "scripts" / "experiments" / "run_md.py",
        "# script\n",
    )
    _write_file(
        tmp_path / "scripts" / "experiments" / "run_md.bth.toml",
        """
        [experiment]
        hypothesis = "Check MD ensemble with Boltz-2 affinity ranking"
        [outcomes.pass]
        condition = "per_replica_converged = true"
        decision = "proceed"
        [result_schema]
        per_replica_converged = "bool"
        boltz2_affinity = "float"
        """,
    )

    # Active spec still encoding converged: true
    _write_file(
        tmp_path / "specs" / "md_protocol.md",
        """
        ---
        status: active
        ---
        # MD Protocol
        Require converged: true on each replica before scoring.
        """,
    )

    # Active claim still referencing Boltz-2 affinity
    _write_file(
        tmp_path / ".bth" / "claims" / "c1.toml",
        """
        [claim]
        headline = "Boltz-2 affinity separates active binders"
        """,
    )

    report = sweep_corrections(tmp_path)
    assert not report.clean
    assert report.corrections_checked == 2
    assert report.errors == ()

    hit_ids = {(h.correction_id, h.artifact_kind, h.matched_term) for h in report.hits}
    assert ("CORR-001", "sidecar", "per_replica_converged") in hit_ids
    assert ("CORR-002", "sidecar", "Boltz-2 affinity") in hit_ids
    assert ("CORR-002", "sidecar", "boltz2_affinity") in hit_ids
    assert ("CORR-001", "spec", "converged: true") in hit_ids
    assert ("CORR-002", "claim", "Boltz-2 affinity") in hit_ids

    # Verify linter & lint_project surface these cleanly
    lint_issues = check_correction_sweep(tmp_path)
    assert any(
        i.issue == "stale_correction_assumption" and i.severity == IssueSeverity.WARNING
        for i in lint_issues
    )
    assert any(i.issue == "stale_correction_assumption" for i in lint_project(tmp_path))


def test_sweep_respects_stale_acknowledged_deliberate_exempt_and_boundary(tmp_path: Path):
    _write_file(
        tmp_path / ".bth" / "corrections" / "corr_01.toml",
        """
        id = "CORR-010"
        summary = "Replace PAE cutoff 15 with 10"
        old_assumptions = ["pae_cutoff_15"]
        exempt_paths = ["scripts/experiments/legacy_baseline.bth.toml"]
        """,
    )

    # 1. Stale sidecar whose stale_reason mentions the old term must NOT self-match
    _write_file(
        tmp_path / "scripts" / "experiments" / "old_run.bth.toml",
        """
        [experiment]
        hypothesis = "Legacy run using pae_cutoff_15"
        [status]
        stale = true
        stale_reason = "pae_cutoff_15 replaced by 10"
        [result_schema]
        score = "float"
        """,
    )
    # 2. Explicitly acknowledged active sidecar
    _write_file(
        tmp_path / "scripts" / "experiments" / "ack_run.bth.toml",
        """
        correction_ack = ["CORR-010"]
        [experiment]
        hypothesis = "AF2 comparison deliberately uses pae_cutoff_15"
        [result_schema]
        pae_cutoff_15 = "float"
        """,
    )
    # 3. Exempt path
    _write_file(
        tmp_path / "scripts" / "experiments" / "legacy_baseline.bth.toml",
        """
        [experiment]
        hypothesis = "Uses pae_cutoff_15"
        [result_schema]
        pae_cutoff_15 = "float"
        """,
    )
    # 4. Superseded spec & inline deliberate marker
    _write_file(
        tmp_path / "specs" / "old_spec.md",
        """
        ---
        status: superseded
        ---
        Uses pae_cutoff_15.
        """,
    )
    _write_file(
        tmp_path / "specs" / "active_spec.md",
        """
        Historical note: pae_cutoff_15 was used in AF2 <!-- bth:deliberate -->
        """,
    )
    # 5. Nested git boundary must not be scanned
    nested = tmp_path / "vendor" / "subrepo"
    nested.mkdir(parents=True)
    (nested / ".git").write_text("gitdir: /tmp/elsewhere\n", encoding="utf-8")
    _write_file(
        nested / "sub.bth.toml",
        """
        [experiment]
        hypothesis = "Uses pae_cutoff_15"
        [result_schema]
        pae_cutoff_15 = "float"
        """,
    )

    report = sweep_corrections(tmp_path)
    assert report.clean, f"unexpected hits: {report.hits}"


def test_word_boundary_prevents_substring_false_positives(tmp_path: Path):
    _write_file(
        tmp_path / "scripts" / "experiments" / "clean_exp.bth.toml",
        """
        [experiment]
        hypothesis = "Threshold calibration"
        [result_schema]
        threshold = "float"
        """,
    )
    corr = MethodCorrection(
        id="CORR-WORD",
        summary="Drop identifier 'old'",
        old_assumptions=("old",),
    )
    report = sweep_corrections(tmp_path, corrections=[corr])
    assert report.clean


def test_load_corrections_reports_malformed_registry_cleanly(tmp_path: Path):
    _write_file(
        tmp_path / ".bth" / "corrections" / "bad.toml",
        """
        id = "CORR-BAD"
        summary = "Invalid regex"
        old_patterns = ["[unclosed"]
        """,
    )
    corrections, errors = load_corrections(tmp_path)
    assert corrections == []
    assert len(errors) == 1
    assert "invalid regex" in errors[0]
    issues = check_correction_sweep(tmp_path)
    assert any(i.issue == "invalid_correction_registry" for i in issues)


def test_hyg008_card_is_wired():
    card = load_corpus().by_id()["HYG-008"]
    assert card.source_check == "check_correction_sweep"
    assert card.severity == "warning"
