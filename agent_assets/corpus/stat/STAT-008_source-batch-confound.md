+++
id = "STAT-008"
title = "Flag projections or classifiers whose main axis tracks source, batch, or coverage"
severity = "warning"
applies_when = "has_source_batch_confound_risk"
source_check = "check_source_batch_confound"
tags = ["statistics", "confound", "batch", "projection", "classifier"]
see_also = ["STAT-004", "DSGN-001", "DSGN-007"]
+++

When combining heterogeneous sources (such as PDB structures with missing residues and MD
frames, or multiple datasets/batches) into a reduced space or classifier, the leading axis
(`PC1`, UMAP/t-SNE coordinates, or classifier routing score) often separates data source,
batch identity, or coverage rather than the scientific signal — or collapses one subset to
the origin.

Declaring a confound metric in `[result_schema]` without gating on it, or gating on pooled
`auroc` / `pc1` without within-source / within-batch stratification, turns a source artifact
into an apparent biological or structural finding.

**What to do.** Stratify classifier/projection metrics within source and batch
(`within_source_auroc`, `within_batch_auroc`, `stratified_auroc`), inspect raw distributions
underneath any projection, and bind confound checks (`pc1_source_r2`, `pc1_batch_r2`,
`pc1_coverage_r2`, `origin_collapse_fraction`) to fail-closed `[invariants.*]` or
`[outcomes.*]` gates.
