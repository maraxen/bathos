"""Tests for #3094: unit-of-independence sidecar lint, STAT-007 card, and corpus flag."""

from __future__ import annotations

import textwrap
from pathlib import Path

from bathos.corpus import CONTEXT_COLUMNS, build_context, evaluate_applies_when, load_corpus
from bathos.linter import IssueSeverity, check_unit_of_independence, lint_project


def _write_sidecar(tmp_path: Path, body: str, stem: str = "run_ensemble") -> Path:
    exp_dir = tmp_path / "scripts" / "experiments"
    exp_dir.mkdir(parents=True, exist_ok=True)
    script = exp_dir / f"{stem}.py"
    script.write_text("# script\n", encoding="utf-8")
    (exp_dir / f"{stem}.bth.toml").write_text(textwrap.dedent(body), encoding="utf-8")
    return script


def test_per_replica_convergence_gate_flagged(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        """
        [experiment]
        hypothesis = "Pool 5 MD replicas per condition"
        [outcomes.pass]
        condition = "per_replica_converged = true AND delta_rmsd < 1.0"
        decision = "go"
        [outcomes.fail]
        condition = "1 = 1"
        decision = "stop"
        is_residual = true
        [breakdown]
        unit = "build"
        [result_schema]
        n_replicas = "int"
        per_replica_converged = "bool"
        delta_rmsd = "float"
        """,
    )
    issues = check_unit_of_independence(tmp_path)
    assert any(
        i.issue == "per_replica_convergence_gate" and i.severity == IssueSeverity.WARNING
        for i in issues
    )


def test_generic_converged_with_replicas_flagged(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        """
        [experiment]
        hypothesis = "MD replicas concatenated"
        [outcomes.pass]
        condition = "converged = true AND dG < -1.0"
        decision = "go"
        [breakdown]
        unit = "build"
        [result_schema]
        n_replicas = "int"
        converged = "bool"
        dG = "float"
        """,
    )
    issues = check_unit_of_independence(tmp_path)
    assert any(i.issue == "per_replica_convergence_gate" for i in issues)


def test_replica_split_null_and_spread_error_bar_flagged(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        """
        [experiment]
        hypothesis = "Test coupling across MD seeds"
        [outcomes.pass]
        condition = "z_score > 3.0 AND replica_split_null_p < 0.05"
        decision = "go"
        [result_schema]
        n_seeds = "int"
        replica_spread = "float"
        replica_split_null_p = "float"
        z_score = "float"
        """,
    )
    issues = check_unit_of_independence(tmp_path)
    kinds = {i.issue for i in issues}
    assert "replica_split_null" in kinds
    assert "replica_spread_error_bar" in kinds
    assert "missing_unit_of_independence" in kinds


def test_seeds_as_n_flagged_when_unit_is_seed(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        """
        [experiment]
        hypothesis = "Compare seed replicates"
        [outcomes.pass]
        condition = "effect / (sd / sqrt(n_seeds)) > 2.0"
        decision = "go"
        [breakdown]
        unit = "seed"
        [result_schema]
        n_seeds = "int"
        effect = "float"
        sd = "float"
        """,
    )
    issues = check_unit_of_independence(tmp_path)
    kinds = {i.issue for i in issues}
    assert "replica_spread_error_bar" in kinds
    assert "non_independent_unit" in kinds


def test_clean_pooled_ensemble_and_waiver_and_negated_prose(tmp_path: Path):
    script = _write_sidecar(
        tmp_path,
        """
        [experiment]
        hypothesis = "Pool exchangeable MD replicas across builds; avoid replica-split null and per-replica convergence"
        [outcomes.pass]
        condition = "pooled_mean_delta > 0.5 AND pooled_se < 0.1"
        decision = "go"
        [breakdown]
        unit = "build"
        [result_schema]
        n_replicas = "int"
        pooled_mean_delta = "float"
        pooled_se = "float"
        """,
    )
    assert check_unit_of_independence(tmp_path) == []
    assert build_context(script)["has_unit_of_independence_risk"] is False

    # Explicit waiver also suppresses warnings
    _write_sidecar(
        tmp_path,
        """
        [experiment]
        hypothesis = "Check each distinct starting seed separately"
        [outcomes.pass]
        condition = "per_replica_converged = true"
        decision = "go"
        [independence]
        waive = "starting structures differ by 7.3A and are non-exchangeable"
        [result_schema]
        n_replicas = "int"
        per_replica_converged = "bool"
        """,
        stem="run_waived",
    )
    assert check_unit_of_independence(tmp_path) == []


def test_unrelated_sidecar_is_clean(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        """
        [experiment]
        hypothesis = "Thermostat stability"
        [outcomes.pass]
        condition = "temp_std < 5.0"
        decision = "go"
        [result_schema]
        temp_std = "float"
        """,
    )
    assert check_unit_of_independence(tmp_path) == []


def test_lint_project_and_stat007_card_and_context(tmp_path: Path):
    script = _write_sidecar(
        tmp_path,
        """
        [experiment]
        hypothesis = "Pool replicas"
        [outcomes.pass]
        condition = "replica_std < 0.2"
        decision = "go"
        [result_schema]
        n_replicas = "int"
        replica_std = "float"
        """,
    )
    assert "has_unit_of_independence_risk" in CONTEXT_COLUMNS
    assert build_context(script)["has_unit_of_independence_risk"] is True
    assert any(i.issue == "replica_spread_error_bar" for i in lint_project(tmp_path))

    card = load_corpus().by_id()["STAT-007"]
    assert card.source_check == "check_unit_of_independence"
    assert card.applies_when == "has_unit_of_independence_risk"
    assert evaluate_applies_when(card.applies_when, {"has_unit_of_independence_risk": True})
    assert not evaluate_applies_when(card.applies_when, {"has_unit_of_independence_risk": False})
