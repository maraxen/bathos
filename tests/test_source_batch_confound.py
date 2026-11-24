"""Tests for #3095: source/batch confound check, diagnostics, STAT-008 card, and corpus flag."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from bathos.corpus import CONTEXT_COLUMNS, build_context, evaluate_applies_when, load_corpus
from bathos.linter import (
    IssueSeverity,
    check_source_batch_confound,
    diagnose_projection_axis_confound,
    lint_project,
)


def _write_sidecar(tmp_path: Path, body: str, stem: str = "eval_projection") -> Path:
    exp_dir = tmp_path / "scripts" / "experiments"
    exp_dir.mkdir(parents=True, exist_ok=True)
    script = exp_dir / f"{stem}.py"
    script.write_text("# script\n", encoding="utf-8")
    (exp_dir / f"{stem}.bth.toml").write_text(textwrap.dedent(body), encoding="utf-8")
    return script


def test_unstratified_classifier_with_source_batch_flagged(tmp_path: Path):
    script = _write_sidecar(
        tmp_path,
        """
        [experiment]
        hypothesis = "Classifier separates active vs inactive across batches and data_source"
        [outcomes.pass]
        condition = "auroc > 0.65"
        decision = "go"
        [outcomes.fail]
        condition = "1 = 1"
        decision = "stop"
        is_residual = true
        [result_schema]
        auroc = "float"
        data_source = "str"
        batch_id = "str"
        """,
    )
    issues = check_source_batch_confound(tmp_path)
    assert len(issues) == 1
    assert issues[0].issue == "unstratified_projection_or_classifier"
    assert issues[0].severity == IssueSeverity.WARNING
    assert build_context(script)["has_source_batch_confound_risk"] is True


def test_ungated_confound_diagnostic_column_flagged(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        """
        [experiment]
        hypothesis = "PCA projection across PDB coverage and MD frames"
        [outcomes.pass]
        condition = "pc1_separation > 1.5"
        decision = "go"
        [result_schema]
        pc1_separation = "float"
        pc1_source_r2 = "float"
        within_source_auroc = "float"
        """,
    )
    issues = check_source_batch_confound(tmp_path)
    assert any(i.issue == "confound_diagnostic_ungated" for i in issues)


def test_stratified_and_gated_projection_is_clean(tmp_path: Path):
    script = _write_sidecar(
        tmp_path,
        """
        [experiment]
        hypothesis = "PCA projection across PDB coverage and MD frames"
        [outcomes.pass]
        condition = "within_source_auroc > 0.65"
        decision = "go"
        [invariants.no_source_axis]
        condition = "pc1_source_r2 < 0.15 AND origin_collapse_fraction < 0.05"
        reasoning = "PC1 must not track data source or collapse PDB subset to origin"
        [result_schema]
        within_source_auroc = "float"
        pc1_source_r2 = "float"
        origin_collapse_fraction = "float"
        """,
    )
    assert check_source_batch_confound(tmp_path) == []
    assert build_context(script)["has_source_batch_confound_risk"] is False


def test_outcome_citation_source_field_does_not_false_positive(tmp_path: Path):
    """[outcomes.pass].source is a threshold citation, NOT a data_source covariate."""
    _write_sidecar(
        tmp_path,
        """
        [experiment]
        hypothesis = "Classifier AUROC on clean held-out split"
        [outcomes.pass]
        condition = "auroc > 0.75"
        decision = "go"
        source = "lit:2025-benchmark"
        [result_schema]
        auroc = "float"
        """,
    )
    assert check_source_batch_confound(tmp_path) == []


def test_waived_confound_is_clean(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        """
        [experiment]
        hypothesis = "Routing classifier across batches"
        [confounds]
        waive = "single-source synthetic pilot only"
        [outcomes.pass]
        condition = "auroc > 0.70"
        decision = "go"
        [result_schema]
        auroc = "float"
        batch = "str"
        """,
    )
    assert check_source_batch_confound(tmp_path) == []


def test_diagnose_projection_axis_confound_numerical():
    # Case 1: main axis separates source (PDB vs MD) and PDB collapses to origin
    diag = diagnose_projection_axis_confound(
        axis_values=[0.0, 0.0001, -0.0002, 0.0, 2.5, 2.8, 3.1, 2.9],
        secondary_axis=[0.0, 0.0001, 0.0, -0.0001, 1.2, -0.5, 0.9, 1.1],
        source=["pdb", "pdb", "pdb", "pdb", "md", "md", "md", "md"],
        batch=["b1", "b2", "b1", "b2", "b1", "b2", "b1", "b2"],
        coverage=[0.5, 0.55, 0.52, 0.48, 1.0, 1.0, 1.0, 1.0],
    )
    assert diag["confounded"] is True
    assert diag["source_r2"] > 0.9
    assert diag["coverage_r2"] > 0.9
    assert diag["batch_r2"] < 0.05
    assert "source:pdb" in diag["collapsed_groups"]

    # Case 2: orthogonal biological signal across balanced sources/batches/coverage
    clean = diagnose_projection_axis_confound(
        axis_values=[-1.5, 1.5, -1.4, 1.6, -1.6, 1.4, -1.5, 1.5],
        source=["pdb", "pdb", "pdb", "pdb", "md", "md", "md", "md"],
        batch=["b1", "b1", "b2", "b2", "b1", "b1", "b2", "b2"],
        coverage=[0.8, 0.8, 0.9, 0.9, 0.8, 0.8, 0.9, 0.9],
    )
    assert clean["confounded"] is False
    assert clean["reasons"] == []
    assert clean["collapsed_groups"] == []

    with pytest.raises(ValueError, match="secondary_axis"):
        diagnose_projection_axis_confound([1.0, 2.0], secondary_axis=[1.0])


def test_lint_project_and_stat008_card_wired(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        """
        [experiment]
        hypothesis = "UMAP embedding across batch covariates"
        [outcomes.pass]
        condition = "auroc > 0.7"
        decision = "go"
        [result_schema]
        auroc = "float"
        batch = "str"
        """,
    )
    assert "has_source_batch_confound_risk" in CONTEXT_COLUMNS
    assert any(
        i.issue == "unstratified_projection_or_classifier" for i in lint_project(tmp_path)
    )

    card = load_corpus().by_id()["STAT-008"]
    assert card.source_check == "check_source_batch_confound"
    assert card.applies_when == "has_source_batch_confound_risk"
    assert evaluate_applies_when(card.applies_when, {"has_source_batch_confound_risk": True})
    assert not evaluate_applies_when(card.applies_when, {"has_source_batch_confound_risk": False})


def test_open_source_and_single_source_prose_do_not_false_positive(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        """
        [experiment]
        hypothesis = "Classifier evaluated on an open-source benchmark and single-source dataset"
        [outcomes.pass]
        condition = "auroc > 0.80"
        decision = "go"
        [result_schema]
        auroc = "float"
        """,
    )
    assert check_source_batch_confound(tmp_path) == []

    # Explicit multi-source prose or schema source column still triggers
    _write_sidecar(
        tmp_path,
        """
        [experiment]
        hypothesis = "Classifier evaluated across multiple sources"
        [outcomes.pass]
        condition = "auroc > 0.80"
        decision = "go"
        [result_schema]
        auroc = "float"
        """,
        stem="multi_source_prose",
    )
    issues = check_source_batch_confound(tmp_path)
    assert any(i.issue == "unstratified_projection_or_classifier" for i in issues)


def test_generic_breakdown_waive_does_not_silence_stat008(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        """
        [experiment]
        hypothesis = "Routing classifier across batches"
        [breakdown]
        waive = "single metric"
        [outcomes.pass]
        condition = "auroc > 0.70"
        decision = "go"
        [result_schema]
        auroc = "float"
        batch = "str"
        """,
    )
    issues = check_source_batch_confound(tmp_path)
    assert any(i.issue == "unstratified_projection_or_classifier" for i in issues)


def test_diagnose_projection_axis_confound_rejects_nonfinite_and_mismatched_lengths():
    with pytest.raises(ValueError, match="finite"):
        diagnose_projection_axis_confound([1.0, float("nan"), 2.0], source=["a", "a", "b"])
    with pytest.raises(ValueError, match="finite"):
        diagnose_projection_axis_confound([1.0, float("inf"), 2.0], coverage=[0.1, 0.2, 0.3])
    with pytest.raises(ValueError, match="finite"):
        diagnose_projection_axis_confound([1.0, 2.0], coverage=[0.5, float("nan")])
    with pytest.raises(ValueError, match="finite"):
        diagnose_projection_axis_confound([1.0, 2.0], secondary_axis=[0.1, float("-inf")])
    with pytest.raises(ValueError, match="source length"):
        diagnose_projection_axis_confound([1.0, 2.0], source=["a"])
    with pytest.raises(ValueError, match="batch length"):
        diagnose_projection_axis_confound([1.0, 2.0], batch=["b1"])
    with pytest.raises(ValueError, match="coverage length"):
        diagnose_projection_axis_confound([1.0, 2.0], coverage=[0.5])
