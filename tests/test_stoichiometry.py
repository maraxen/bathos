"""Unit tests for stoichiometry stage-graph conservation, cleanup, collision, and subpool sizing."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from bathos.sidecar import parse_sidecar
from bathos.stoichiometry import (
    DISALLOWED_CONTAMINANTS_BY_STAGE_KIND,
    ENZYME_REMOVES_SPECIES,
    StageKind,
    evaluate_stoichiometry,
    validate_stoichiometry_block,
)
from bathos.validate import validate_sidecar

_BASE_HEADER = """
[experiment]
hypothesis = "Library stoichiometry conservation holds"
[outcomes.pass]
condition = "metric > 0"
decision = "proceed"
reasoning = "Meets library depth"
[outcomes.fail]
condition = "metric <= 0"
decision = "abort"
reasoning = "Failed"
is_residual = true
[result_schema]
metric = "float"
"""


def _write_sidecar(tmp_path: Path, body: str, name: str = "library_prep.bth.toml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(_BASE_HEADER + "\n" + body), encoding="utf-8")
    return path


def test_stoichiometry_constants_closed_sets() -> None:
    assert len(StageKind) == 8
    assert ENZYME_REMOVES_SPECIES["DpnI"] == frozenset({"methylated_circular_vector"})
    assert ENZYME_REMOVES_SPECIES["linear_selective_nuclease"] == frozenset(
        {"linear_partial_ligations"}
    )
    assert set(DISALLOWED_CONTAMINANTS_BY_STAGE_KIND) == {
        "colony_cfu",
        "bulk_fluorometry",
        "pcr_template_molecules",
        "phix_adjusted_net_reads",
    }


def test_stoichiometry_1e8_cfu_with_7e6_net_reads_fails_reads_per_barcode(tmp_path: Path) -> None:
    path = _write_sidecar(
        tmp_path,
        """
        [stoichiometry]
        barcoding_mode = "primer_pcr"
        min_consensus_reads = 3.0
        min_template_ratio = 10.0

        [[stoichiometry.stages]]
        name = "ligated_input"
        kind = "input_molecules"
        molecules = 1e10
        species = ["methylated_circular_vector", "linear_partial_ligations"]

        [[stoichiometry.stages]]
        name = "dpni_digest"
        kind = "enzymatic_species_cleanup"
        depends_on = ["ligated_input"]
        enzyme = "DpnI"

        [[stoichiometry.stages]]
        name = "plasmid_safe_digest"
        kind = "enzymatic_species_cleanup"
        depends_on = ["dpni_digest"]
        enzyme = "linear_selective_nuclease"

        [[stoichiometry.stages]]
        name = "transformants"
        kind = "colony_cfu"
        depends_on = ["plasmid_safe_digest"]
        colony_cfu = 1e8

        [[stoichiometry.stages]]
        name = "outgrowth"
        kind = "founder_inoculum"
        depends_on = ["transformants"]
        sampling_fraction = 1.0

        [[stoichiometry.stages]]
        name = "pcr_pool"
        kind = "pcr_template_molecules"
        depends_on = ["outgrowth"]
        copies_per_cell = 10.0

        [[stoichiometry.stages]]
        name = "ont_run"
        kind = "phix_adjusted_net_reads"
        depends_on = ["pcr_pool"]
        raw_reads = 7000000
        """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.stoichiometry is not None
    evaluation = evaluate_stoichiometry(sidecar.stoichiometry)
    assert len(evaluation.errors) >= 1
    assert any("reads_per_barcode" in err.message for err in evaluation.errors)
    ont_state = evaluation.stage_states["ont_run"]
    assert ont_state.reads_per_barcode is not None
    # colony=1e8, inoculum=1e8 -> tracked_barcodes after founder + pcr saturation vs 7e6 reads < 3.0
    assert ont_state.reads_per_barcode < 3.0

    # Also check exact 0.07 reads_per_barcode regime directly with huge copies/tracked barcodes
    exact_path = _write_sidecar(
        tmp_path,
        """
        [stoichiometry]
        barcoding_mode = "primer_pcr"
        min_consensus_reads = 3.0
        min_template_ratio = 10.0

        [[stoichiometry.stages]]
        name = "ligated_input"
        kind = "input_molecules"
        molecules = 1e10
        species = []

        [[stoichiometry.stages]]
        name = "transformants"
        kind = "colony_cfu"
        depends_on = ["ligated_input"]
        colony_cfu = 1e8

        [[stoichiometry.stages]]
        name = "pcr_pool"
        kind = "pcr_template_molecules"
        depends_on = ["transformants"]
        copies_per_cell = 500.0

        [[stoichiometry.stages]]
        name = "ont_run"
        kind = "phix_adjusted_net_reads"
        depends_on = ["pcr_pool"]
        raw_reads = 7000000
        """,
        name="exact_1e8.bth.toml",
    )
    exact_sidecar = parse_sidecar(exact_path)
    assert exact_sidecar.stoichiometry is not None
    exact_eval = evaluate_stoichiometry(exact_sidecar.stoichiometry)
    assert exact_eval.stage_states["ont_run"].reads_per_barcode == pytest.approx(0.07, abs=1e-6)
    result = validate_sidecar(exact_sidecar)
    assert result.ok is False
    assert any("reads_per_barcode=0.070000" in err.message for err in result.errors)


def test_stoichiometry_pre_barcoded_collision_5e6_diversity_1_5e6_cfu_fails(tmp_path: Path) -> None:
    path = _write_sidecar(
        tmp_path,
        """
        [stoichiometry]
        barcoding_mode = "pre_barcoded_vector"
        max_collision_prob = 0.05

        [[stoichiometry.stages]]
        name = "dna_in"
        kind = "input_molecules"
        molecules = 1e9
        species = ["methylated_circular_vector"]

        [[stoichiometry.stages]]
        name = "cleanup_dpni"
        kind = "enzymatic_species_cleanup"
        depends_on = ["dna_in"]
        enzyme = "DpnI"

        [[stoichiometry.stages]]
        name = "colonies"
        kind = "colony_cfu"
        depends_on = ["cleanup_dpni"]
        colony_cfu = 1500000.0

        [[stoichiometry.stages]]
        name = "vector_bc"
        kind = "barcode_collision"
        depends_on = ["colonies"]
        barcode_diversity = 5000000
        """,
    )
    sidecar = parse_sidecar(path)
    assert sidecar.stoichiometry is not None
    evaluation = evaluate_stoichiometry(sidecar.stoichiometry)
    metrics = evaluation.collision_metrics["vector_bc"]
    assert metrics.per_clone_collision_prob == pytest.approx(0.259182, abs=1e-6)
    assert metrics.expected_duplicate_fraction == pytest.approx(0.136061, abs=1e-6)
    assert metrics.occupied_barcodes == pytest.approx(1295908.90, abs=0.5)
    result = validate_sidecar(sidecar)
    assert result.ok is False
    assert any("0.259182" in err.message and "0.136061" in err.message for err in result.errors)


def test_stoichiometry_collision_warn_threshold_and_conflict(tmp_path: Path) -> None:
    # Default warn threshold = 0.5 * 0.05 = 0.025; 40_000 / 1_000_000 -> 0.039211 (warns, passes)
    warn_path = _write_sidecar(
        tmp_path,
        """
        [stoichiometry]
        barcoding_mode = "pre_barcoded_vector"
        max_collision_prob = 0.05

        [[stoichiometry.stages]]
        name = "dna_in"
        kind = "input_molecules"
        molecules = 1e8

        [[stoichiometry.stages]]
        name = "colonies"
        kind = "colony_cfu"
        depends_on = ["dna_in"]
        colony_cfu = 40000.0

        [[stoichiometry.stages]]
        name = "vector_bc"
        kind = "barcode_collision"
        depends_on = ["colonies"]
        barcode_diversity = 1000000
        """,
        name="warn_case.bth.toml",
    )
    warn_res = validate_sidecar(parse_sidecar(warn_path))
    assert warn_res.ok is True
    assert any("warn_collision_prob=0.025000" in msg for msg in warn_res.warnings)

    conflict_path = _write_sidecar(
        tmp_path,
        """
        [stoichiometry]
        barcoding_mode = "pre_barcoded_vector"
        max_collision_prob = 0.05
        warn_collision_prob = 0.10

        [[stoichiometry.stages]]
        name = "dna_in"
        kind = "input_molecules"
        molecules = 1e8

        [[stoichiometry.stages]]
        name = "colonies"
        kind = "colony_cfu"
        depends_on = ["dna_in"]
        colony_cfu = 1000.0

        [[stoichiometry.stages]]
        name = "vector_bc"
        kind = "barcode_collision"
        depends_on = ["colonies"]
        barcode_diversity = 1000000
        """,
        name="conflict_case.bth.toml",
    )
    conflict_res = validate_sidecar(parse_sidecar(conflict_path))
    assert conflict_res.ok is False
    assert any(err.field == "stoichiometry.warn_collision_prob" for err in conflict_res.errors)


def test_stoichiometry_uncleaned_methylated_circular_vector_fails(tmp_path: Path) -> None:
    colony_path = _write_sidecar(
        tmp_path,
        """
        [stoichiometry]
        barcoding_mode = "primer_pcr"

        [[stoichiometry.stages]]
        name = "dna_in"
        kind = "input_molecules"
        molecules = 1e9
        species = ["methylated_circular_vector"]

        [[stoichiometry.stages]]
        name = "colonies"
        kind = "colony_cfu"
        depends_on = ["dna_in"]
        colony_cfu = 1e6
        """,
        name="methylated_colony.bth.toml",
    )
    colony_res = validate_sidecar(parse_sidecar(colony_path))
    assert colony_res.ok is False
    assert any(
        err.field == "stoichiometry.stages.colonies.species"
        and "methylated_circular_vector" in err.message
        for err in colony_res.errors
    )

    bulk_path = _write_sidecar(
        tmp_path,
        """
        [stoichiometry]
        barcoding_mode = "primer_pcr"

        [[stoichiometry.stages]]
        name = "dna_in"
        kind = "input_molecules"
        molecules = 1e9
        species = ["methylated_circular_vector"]

        [[stoichiometry.stages]]
        name = "qubit_qc"
        kind = "bulk_fluorometry"
        depends_on = ["dna_in"]
        """,
        name="methylated_bulk.bth.toml",
    )
    bulk_res = validate_sidecar(parse_sidecar(bulk_path))
    assert bulk_res.ok is False
    assert any(
        err.field == "stoichiometry.stages.qubit_qc.species"
        and "methylated_circular_vector" in err.message
        for err in bulk_res.errors
    )


def test_stoichiometry_uncleaned_linear_partial_ligations_fails_with_and_without_bulk(
    tmp_path: Path,
) -> None:
    bulk_path = _write_sidecar(
        tmp_path,
        """
        [stoichiometry]
        barcoding_mode = "primer_pcr"

        [[stoichiometry.stages]]
        name = "dna_in"
        kind = "input_molecules"
        molecules = 1e10
        species = ["linear_partial_ligations"]

        [[stoichiometry.stages]]
        name = "colonies"
        kind = "colony_cfu"
        depends_on = ["dna_in"]
        colony_cfu = 1e5

        [[stoichiometry.stages]]
        name = "qubit_qc"
        kind = "bulk_fluorometry"
        depends_on = ["colonies"]
        """,
        name="linear_bulk.bth.toml",
    )
    bulk_res = validate_sidecar(parse_sidecar(bulk_path))
    assert bulk_res.ok is False
    assert any(
        err.field == "stoichiometry.stages.qubit_qc.species"
        and "linear_partial_ligations" in err.message
        for err in bulk_res.errors
    )

    no_bulk_path = _write_sidecar(
        tmp_path,
        """
        [stoichiometry]
        barcoding_mode = "primer_pcr"

        [[stoichiometry.stages]]
        name = "dna_in"
        kind = "input_molecules"
        molecules = 1e10
        species = ["linear_partial_ligations"]

        [[stoichiometry.stages]]
        name = "colonies"
        kind = "colony_cfu"
        depends_on = ["dna_in"]
        colony_cfu = 1e5

        [[stoichiometry.stages]]
        name = "pcr_pool"
        kind = "pcr_template_molecules"
        depends_on = ["colonies"]
        copies_per_cell = 500.0
        """,
        name="linear_no_bulk.bth.toml",
    )
    no_bulk_res = validate_sidecar(parse_sidecar(no_bulk_path))
    assert no_bulk_res.ok is False
    assert any(
        err.field == "stoichiometry.stages.pcr_pool.species"
        and "linear_partial_ligations" in err.message
        for err in no_bulk_res.errors
    )


def test_stoichiometry_passing_50m_ont_primer_pcr_and_16nt_umi(tmp_path: Path) -> None:
    primer_pcr_path = _write_sidecar(
        tmp_path,
        """
        [stoichiometry]
        barcoding_mode = "primer_pcr"
        min_consensus_reads = 3.0
        min_template_ratio = 10.0
        target_reads = 40000000

        [[stoichiometry.stages]]
        name = "gibson_product"
        kind = "input_molecules"
        molecules = 1e11
        species = ["methylated_circular_vector", "linear_partial_ligations"]

        [[stoichiometry.stages]]
        name = "dpni_cleanup"
        kind = "enzymatic_species_cleanup"
        depends_on = ["gibson_product"]
        enzyme = "DpnI"
        retention_fraction = 0.95

        [[stoichiometry.stages]]
        name = "exo_cleanup"
        kind = "enzymatic_species_cleanup"
        depends_on = ["dpni_cleanup"]
        enzyme = "linear_selective_nuclease"
        retention_fraction = 0.9

        [[stoichiometry.stages]]
        name = "transformants"
        kind = "colony_cfu"
        depends_on = ["exo_cleanup"]
        colony_cfu = 1500000.0

        [[stoichiometry.stages]]
        name = "bulk_qc"
        kind = "bulk_fluorometry"
        depends_on = ["transformants"]

        [[stoichiometry.stages]]
        name = "pcr_templates"
        kind = "pcr_template_molecules"
        depends_on = ["bulk_qc"]
        copies_per_cell = 1000.0
        lysis_recovery_yield = 0.9
        pcr_aliquot_fraction = 0.5

        [[stoichiometry.stages]]
        name = "ont_reads"
        kind = "phix_adjusted_net_reads"
        depends_on = ["pcr_templates"]
        raw_reads = 50000000
        phix_fraction = 0.05
        qc_pass_fraction = 0.9
        """,
        name="ont_primer_pcr.bth.toml",
    )
    primer_sidecar = parse_sidecar(primer_pcr_path)
    primer_res = validate_sidecar(primer_sidecar)
    assert primer_res.ok is True, primer_res.errors
    assert primer_res.warnings == []

    # Optional 16-nt UMI stage (4**16 = 4294967296): collision_prob = 0.000349 <= 0.05
    umi_path = _write_sidecar(
        tmp_path,
        """
        [stoichiometry]
        barcoding_mode = "primer_pcr"
        min_consensus_reads = 3.0
        min_template_ratio = 10.0

        [[stoichiometry.stages]]
        name = "gibson_product"
        kind = "input_molecules"
        molecules = 1e11
        species = ["methylated_circular_vector", "linear_partial_ligations"]

        [[stoichiometry.stages]]
        name = "dpni_cleanup"
        kind = "enzymatic_species_cleanup"
        depends_on = ["gibson_product"]
        enzyme = "DpnI"

        [[stoichiometry.stages]]
        name = "exo_cleanup"
        kind = "enzymatic_species_cleanup"
        depends_on = ["dpni_cleanup"]
        enzyme = "linear_selective_nuclease"

        [[stoichiometry.stages]]
        name = "transformants"
        kind = "colony_cfu"
        depends_on = ["exo_cleanup"]
        colony_cfu = 1500000.0

        [[stoichiometry.stages]]
        name = "umi_tag"
        kind = "barcode_collision"
        depends_on = ["transformants"]
        barcode_diversity = 4294967296

        [[stoichiometry.stages]]
        name = "bulk_qc"
        kind = "bulk_fluorometry"
        depends_on = ["umi_tag"]

        [[stoichiometry.stages]]
        name = "pcr_templates"
        kind = "pcr_template_molecules"
        depends_on = ["bulk_qc"]
        copies_per_cell = 500.0

        [[stoichiometry.stages]]
        name = "ont_reads"
        kind = "phix_adjusted_net_reads"
        depends_on = ["pcr_templates"]
        raw_reads = 50000000
        """,
        name="ont_umi16.bth.toml",
    )
    umi_sidecar = parse_sidecar(umi_path)
    assert umi_sidecar.stoichiometry is not None
    umi_eval = evaluate_stoichiometry(umi_sidecar.stoichiometry)
    assert umi_eval.errors == ()
    assert umi_eval.collision_metrics["umi_tag"].per_clone_collision_prob == pytest.approx(
        0.000349, abs=1e-6
    )
    assert validate_sidecar(umi_sidecar).ok is True


@pytest.mark.parametrize(
    ("weights", "mins", "actual_cfu", "expected_ok"),
    [
        ((0.2, 0.8), (100, 100), 500.0, True),
        ((0.2, 0.8), (100, 100), 400.0, False),
        ((1.0, 48.0), (100, 100), 4900.0, True),
        ((1.0, 48.0), (100, 100), 4899.0, False),
        ((1.0, 2.0), (100, 100), 300.0, True),
        ((1.0, 2.0), (100, 100), 299.0, False),
    ],
)
def test_stoichiometry_weighted_subpool_exact_decimal_boundaries(
    tmp_path: Path,
    weights: tuple[float, float],
    mins: tuple[int, int],
    actual_cfu: float,
    expected_ok: bool,
) -> None:
    path = _write_sidecar(
        tmp_path,
        f"""
        [stoichiometry]
        barcoding_mode = "primer_pcr"

        [[stoichiometry.stages]]
        name = "dna_in"
        kind = "input_molecules"
        molecules = 1e8

        [[stoichiometry.stages]]
        name = "colonies"
        kind = "colony_cfu"
        depends_on = ["dna_in"]
        colony_cfu = {actual_cfu}

        [stoichiometry.subpools.pool_alpha]
        weight = {weights[0]}
        min_group_size = {mins[0]}

        [stoichiometry.subpools.pool_beta]
        weight = {weights[1]}
        min_group_size = {mins[1]}
        """,
        name=f"subpool_{actual_cfu}.bth.toml",
    )
    res = validate_sidecar(parse_sidecar(path))
    assert res.ok is expected_ok, res.errors


def test_stoichiometry_dag_errors_merge_cleanup_overrides_and_subpool_defaults(
    tmp_path: Path,
) -> None:
    # Cycle + missing input_molecules + missing subpool min_group_size
    cycle_path = _write_sidecar(
        tmp_path,
        """
        [stoichiometry]
        barcoding_mode = "primer_pcr"

        [[stoichiometry.stages]]
        name = "step_a"
        kind = "bulk_fluorometry"
        depends_on = ["step_b"]

        [[stoichiometry.stages]]
        name = "step_b"
        kind = "bulk_fluorometry"
        depends_on = ["step_a"]

        [stoichiometry.subpools.only_pool]
        weight = 1.0
        """,
        name="cycle.bth.toml",
    )
    cycle_res = validate_sidecar(parse_sidecar(cycle_path))
    assert cycle_res.ok is False
    msgs = " ".join(err.message for err in cycle_res.errors)
    assert "input_molecules" in msgs
    assert "Cycle detected" in msgs
    assert "min_group_size" in msgs

    # Unknown parent and pre_barcoded_vector missing barcode_collision downstream of colony
    missing_bc_path = _write_sidecar(
        tmp_path,
        """
        [stoichiometry]
        barcoding_mode = "pre_barcoded_vector"

        [[stoichiometry.stages]]
        name = "dna_in"
        kind = "input_molecules"
        molecules = 1e8

        [[stoichiometry.stages]]
        name = "colonies"
        kind = "colony_cfu"
        depends_on = ["dna_in"]
        colony_cfu = 1000.0
        """,
        name="missing_bc.bth.toml",
    )
    missing_bc_res = validate_sidecar(parse_sidecar(missing_bc_path))
    assert missing_bc_res.ok is False
    assert any(err.field == "stoichiometry.barcoding_mode" for err in missing_bc_res.errors)

    # Branch merge sums entities and unions species, unknown enzyme error, missing pcr ancestor
    merge_path = _write_sidecar(
        tmp_path,
        """
        [stoichiometry]
        barcoding_mode = "primer_pcr"

        [[stoichiometry.stages]]
        name = "branch_one"
        kind = "input_molecules"
        molecules = 1e9
        species = ["custom_contaminant"]

        [[stoichiometry.stages]]
        name = "branch_two"
        kind = "input_molecules"
        molecules = 2e9
        species = ["methylated_circular_vector"]

        [[stoichiometry.stages]]
        name = "cleanup_bad"
        kind = "enzymatic_species_cleanup"
        depends_on = ["branch_one", "branch_two"]
        enzyme = "mystery_nuclease"

        [[stoichiometry.stages]]
        name = "colonies"
        kind = "colony_cfu"
        depends_on = ["cleanup_bad"]
        efficiency = 0.001
        forbidden_species = ["methylated_circular_vector", "custom_contaminant"]

        [[stoichiometry.stages]]
        name = "reads_missing_pcr"
        kind = "phix_adjusted_net_reads"
        depends_on = ["colonies"]
        raw_reads = 10000000
        """,
        name="merge_bad.bth.toml",
    )
    merge_sidecar = parse_sidecar(merge_path)
    errors, _warnings = validate_stoichiometry_block(merge_sidecar)
    fields = {err.field for err in errors}
    assert "stoichiometry.stages.cleanup_bad.enzyme" in fields
    assert "stoichiometry.stages.colonies.species" in fields
    assert "stoichiometry.stages.reads_missing_pcr.depends_on" in fields
