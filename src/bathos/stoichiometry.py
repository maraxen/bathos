"""Physical stoichiometry stage-graph conservation, cleanup, subpool sizing, and reagent amortization."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bathos.sidecar import ReagentSpec, Sidecar, StoichiometryBlock, StoichiometryStage
    from bathos.validate import ValidationError


class StageKind(str, Enum):  # noqa: UP042 - str Enum preserves serialization equality with raw stage kind names
    INPUT_MOLECULES = "input_molecules"
    ENZYMATIC_SPECIES_CLEANUP = "enzymatic_species_cleanup"
    COLONY_CFU = "colony_cfu"
    BARCODE_COLLISION = "barcode_collision"
    FOUNDER_INOCULUM = "founder_inoculum"
    PCR_TEMPLATE_MOLECULES = "pcr_template_molecules"
    BULK_FLUOROMETRY = "bulk_fluorometry"
    PHIX_ADJUSTED_NET_READS = "phix_adjusted_net_reads"


ENZYME_REMOVES_SPECIES: dict[str, frozenset[str]] = {
    "DpnI": frozenset({"methylated_circular_vector"}),
    "linear_selective_nuclease": frozenset({"linear_partial_ligations"}),
}

DISALLOWED_CONTAMINANTS_BY_STAGE_KIND: dict[str, frozenset[str]] = {
    StageKind.COLONY_CFU.value: frozenset({"methylated_circular_vector"}),
    StageKind.BULK_FLUOROMETRY.value: frozenset(
        {"methylated_circular_vector", "linear_partial_ligations"}
    ),
    StageKind.PCR_TEMPLATE_MOLECULES.value: frozenset(
        {"methylated_circular_vector", "linear_partial_ligations"}
    ),
    StageKind.PHIX_ADJUSTED_NET_READS.value: frozenset(
        {"methylated_circular_vector", "linear_partial_ligations"}
    ),
}

SIDECAR_KIND_TABLES: tuple[str, ...] = ("experiment", "benchmark", "validation", "debug")
PROJECT_CONFIG_TABLES: frozenset[str] = frozenset(
    {
        "project",
        "remotes",
        "slurm",
        "obligations",
        "claim",
        "enforcement",
        "gate",
        "provenance",
        "stepback",
        "defaults",
        "bearings",
        "instruments",
    }
)
NON_SIDECAR_ARTIFACT_TABLES: frozenset[str] = frozenset({"claim", "attestation", "parity"})
SUBMIT_CONFIG_SIDECAR_TABLES: frozenset[str] = frozenset({"cluster"})


@dataclass(frozen=True)
class CollisionMetrics:
    """Birthday-paradox Poisson collision summary for a `barcode_collision` stage."""

    stage_name: str
    entities_in: float
    barcode_diversity: int
    per_clone_collision_prob: float
    expected_duplicate_fraction: float
    occupied_barcodes: float


@dataclass(frozen=True)
class StageState:
    """Propagated entity, species, and tracked-barcode counts leaving one DAG node."""

    name: str
    kind: str
    entities_in: float
    entities_out: float
    tracked_barcodes_in: float
    tracked_barcodes_out: float
    species_in: frozenset[str]
    species_out: frozenset[str]
    collision: CollisionMetrics | None = None
    reads_per_barcode: float | None = None


@dataclass(frozen=True)
class StoichiometryEvaluation:
    """Structured result of evaluating a `[stoichiometry]` stage graph."""

    errors: tuple[ValidationError, ...]
    warnings: tuple[str, ...]
    topo_order: tuple[str, ...]
    stage_states: dict[str, StageState]
    collision_metrics: dict[str, CollisionMetrics]


@dataclass(frozen=True)
class ReagentAmortization:
    """Exact Decimal-derived amortization summary reported for one `[reagents.<name>]`."""

    name: str
    pack_rxns: int
    experiment_rxns: int | float
    packs_required: int
    purchased_rxns: int
    unused_rxns: float
    waste_fraction: float
    total_cost_usd: float | None = None
    amortized_cost_per_rxn_usd: float | None = None


@dataclass(frozen=True)
class ReagentsEvaluation:
    """Structured result of validating `[reagents.*]` pack-size bounds."""

    errors: tuple[ValidationError, ...]
    warnings: tuple[str, ...]
    reports: dict[str, ReagentAmortization]


def _make_error(field: str, message: str) -> ValidationError:
    from bathos.validate import ValidationError

    return ValidationError(field, message)


def _topological_sort_stages(
    stages: tuple[StoichiometryStage, ...],
) -> tuple[list[str], list[ValidationError]]:
    errors: list[ValidationError] = []
    stages_by_name = {stage.name: stage for stage in stages}

    if not any(stage.kind == StageKind.INPUT_MOLECULES.value for stage in stages):
        errors.append(
            _make_error(
                "stoichiometry.stages",
                "Stoichiometry stage graph must declare at least one 'input_molecules' stage",
            )
        )

    children: dict[str, list[str]] = {stage.name: [] for stage in stages}
    in_degree: dict[str, int] = {stage.name: 0 for stage in stages}

    for stage in stages:
        if stage.kind == StageKind.INPUT_MOLECULES.value:
            if stage.depends_on:
                errors.append(
                    _make_error(
                        f"stoichiometry.stages.{stage.name}.depends_on",
                        "input_molecules stage must declare depends_on = []",
                    )
                )
            continue

        if not stage.depends_on:
            errors.append(
                _make_error(
                    f"stoichiometry.stages.{stage.name}.depends_on",
                    f"{stage.kind} stage must declare at least one depends_on parent",
                )
            )
            continue

        seen_parents: set[str] = set()
        for parent_name in stage.depends_on:
            if parent_name == stage.name:
                errors.append(
                    _make_error(
                        f"stoichiometry.stages.{stage.name}.depends_on",
                        f"Stage {stage.name!r} cannot depend on itself",
                    )
                )
                continue
            if parent_name not in stages_by_name:
                errors.append(
                    _make_error(
                        f"stoichiometry.stages.{stage.name}.depends_on",
                        f"Unknown parent stage {parent_name!r}",
                    )
                )
                continue
            if parent_name in seen_parents:
                errors.append(
                    _make_error(
                        f"stoichiometry.stages.{stage.name}.depends_on",
                        f"Duplicate parent stage {parent_name!r} in depends_on",
                    )
                )
                continue
            seen_parents.add(parent_name)
            children[parent_name].append(stage.name)
            in_degree[stage.name] += 1

    queue: deque[str] = deque(stage.name for stage in stages if in_degree[stage.name] == 0)
    topo_order: list[str] = []
    while queue:
        current = queue.popleft()
        topo_order.append(current)
        for child in children[current]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if len(topo_order) != len(stages):
        cycle_nodes = [stage.name for stage in stages if in_degree[stage.name] > 0]
        errors.append(
            _make_error(
                "stoichiometry.stages",
                f"Cycle detected in stoichiometry DAG involving: {', '.join(cycle_nodes)}",
            )
        )

    return topo_order, errors


def _nearest_pcr_template_ancestors(
    stage: StoichiometryStage,
    stages_by_name: dict[str, StoichiometryStage],
) -> list[str]:
    nearest_pcr: list[str] = []
    visited: set[str] = set()
    queue: deque[str] = deque(stage.depends_on)
    while queue:
        parent_name = queue.popleft()
        if parent_name in visited:
            continue
        visited.add(parent_name)
        parent_stage = stages_by_name[parent_name]
        if parent_stage.kind == StageKind.PCR_TEMPLATE_MOLECULES.value:
            nearest_pcr.append(parent_name)
        else:
            queue.extend(parent_stage.depends_on)
    return nearest_pcr


def _check_stage_contaminants(
    stage: StoichiometryStage,
    species_in: frozenset[str],
    errors: list[ValidationError],
) -> None:
    disallowed = (
        frozenset(stage.forbidden_species)
        if stage.forbidden_species is not None
        else DISALLOWED_CONTAMINANTS_BY_STAGE_KIND.get(stage.kind, frozenset())
    )
    present = sorted(species_in & disallowed)
    if present:
        errors.append(
            _make_error(
                f"stoichiometry.stages.{stage.name}.species",
                f"Stage {stage.name!r} ({stage.kind}) received uncleaned disallowed species: "
                f"{', '.join(present)}",
            )
        )


def _evaluate_single_stage(
    stage: StoichiometryStage,
    block: StoichiometryBlock,
    effective_warn_prob: float,
    stages_by_name: dict[str, StoichiometryStage],
    state_by_name: dict[str, StageState],
    errors: list[ValidationError],
    warnings: list[str],
) -> StageState:
    if not stage.depends_on:
        entities_in = float(stage.molecules or 0.0)
        tracked_barcodes_in = entities_in
        species_in: frozenset[str] = frozenset(stage.species)
    else:
        entities_in = sum(state_by_name[parent].entities_out for parent in stage.depends_on)
        tracked_barcodes_in = sum(
            state_by_name[parent].tracked_barcodes_out for parent in stage.depends_on
        )
        species_in = frozenset().union(
            *(state_by_name[parent].species_out for parent in stage.depends_on)
        )

    _check_stage_contaminants(stage, species_in, errors)

    kind = stage.kind
    collision: CollisionMetrics | None = None
    reads_per_barcode: float | None = None

    if kind == StageKind.INPUT_MOLECULES.value:
        entities_out = float(stage.molecules or 0.0)
        species_out = frozenset(stage.species)
        tracked_barcodes_out = entities_out
    elif kind == StageKind.ENZYMATIC_SPECIES_CLEANUP.value:
        if stage.enzyme and stage.enzyme not in ENZYME_REMOVES_SPECIES and not stage.removes_species:
            errors.append(
                _make_error(
                    f"stoichiometry.stages.{stage.name}.enzyme",
                    f"Unknown cleanup enzyme {stage.enzyme!r} without removes_species",
                )
            )
        elif not stage.enzyme and not stage.removes_species:
            errors.append(
                _make_error(
                    f"stoichiometry.stages.{stage.name}.enzyme",
                    "enzymatic_species_cleanup requires a known enzyme or non-empty removes_species",
                )
            )
        removed = ENZYME_REMOVES_SPECIES.get(stage.enzyme, frozenset()) | frozenset(
            stage.removes_species
        )
        entities_out = entities_in * stage.retention_fraction
        species_out = species_in - removed
        tracked_barcodes_out = tracked_barcodes_in * stage.retention_fraction
    elif kind == StageKind.COLONY_CFU.value:
        if stage.efficiency is not None:
            entities_out = entities_in * stage.efficiency
        else:
            assert stage.colony_cfu is not None
            if stage.colony_cfu > entities_in:
                errors.append(
                    _make_error(
                        f"stoichiometry.stages.{stage.name}.colony_cfu",
                        f"colony_cfu ({stage.colony_cfu}) exceeds incoming entities ({entities_in})",
                    )
                )
            entities_out = float(stage.colony_cfu)
        species_out = species_in
        tracked_barcodes_out = entities_out
    elif kind == StageKind.BARCODE_COLLISION.value:
        diversity = stage.barcode_diversity or 0
        if entities_in <= 0.0 or diversity <= 0:
            errors.append(
                _make_error(
                    f"stoichiometry.stages.{stage.name}",
                    f"barcode_collision requires positive entities_in ({entities_in}) and "
                    f"barcode_diversity ({diversity})",
                )
            )
            entities_out = entities_in
            tracked_barcodes_out = 0.0
        else:
            per_clone_collision_prob = -math.expm1(-entities_in / diversity)
            expected_duplicate_fraction = 1.0 - (diversity / entities_in) * per_clone_collision_prob
            occupied_barcodes = diversity * per_clone_collision_prob
            collision = CollisionMetrics(
                stage_name=stage.name,
                entities_in=entities_in,
                barcode_diversity=diversity,
                per_clone_collision_prob=per_clone_collision_prob,
                expected_duplicate_fraction=expected_duplicate_fraction,
                occupied_barcodes=occupied_barcodes,
            )
            if per_clone_collision_prob > block.max_collision_prob:
                errors.append(
                    _make_error(
                        f"stoichiometry.stages.{stage.name}",
                        f"per_clone_collision_prob={per_clone_collision_prob:.6f} exceeds "
                        f"max_collision_prob={block.max_collision_prob:.6f} "
                        f"(expected_duplicate_fraction={expected_duplicate_fraction:.6f}, "
                        f"occupied_barcodes={occupied_barcodes:.2f})",
                    )
                )
            elif per_clone_collision_prob >= effective_warn_prob:
                warnings.append(
                    f"Stage {stage.name!r}: per_clone_collision_prob="
                    f"{per_clone_collision_prob:.6f} reaches warn_collision_prob="
                    f"{effective_warn_prob:.6f} (max={block.max_collision_prob:.6f}, "
                    f"expected_duplicate_fraction={expected_duplicate_fraction:.6f})"
                )
            entities_out = entities_in
            tracked_barcodes_out = occupied_barcodes
        species_out = species_in
    elif kind == StageKind.FOUNDER_INOCULUM.value:
        if stage.sampling_fraction is not None:
            inoculum_out = entities_in * stage.sampling_fraction
        else:
            assert stage.inoculum_cells is not None
            if stage.inoculum_cells > entities_in:
                errors.append(
                    _make_error(
                        f"stoichiometry.stages.{stage.name}.inoculum_cells",
                        f"inoculum_cells ({stage.inoculum_cells}) exceeds incoming entities ({entities_in})",
                    )
                )
            inoculum_out = float(stage.inoculum_cells)
        entities_out = inoculum_out
        species_out = species_in
        if tracked_barcodes_in <= 0.0:
            errors.append(
                _make_error(
                    f"stoichiometry.stages.{stage.name}",
                    f"founder_inoculum requires positive tracked_barcodes_in, got {tracked_barcodes_in}",
                )
            )
            tracked_barcodes_out = 0.0
        else:
            tracked_barcodes_out = tracked_barcodes_in * (
                -math.expm1(-inoculum_out / tracked_barcodes_in)
            )
    elif kind == StageKind.PCR_TEMPLATE_MOLECULES.value:
        entities_out = (
            entities_in
            * stage.copies_per_cell
            * stage.lysis_recovery_yield
            * stage.pcr_aliquot_fraction
        )
        species_out = species_in
        if tracked_barcodes_in <= 0.0:
            errors.append(
                _make_error(
                    f"stoichiometry.stages.{stage.name}",
                    f"pcr_template_molecules requires positive tracked_barcodes_in, got {tracked_barcodes_in}",
                )
            )
            tracked_barcodes_out = 0.0
        else:
            tracked_barcodes_out = tracked_barcodes_in * (
                -math.expm1(-entities_out / tracked_barcodes_in)
            )
    elif kind == StageKind.BULK_FLUOROMETRY.value:
        entities_out = entities_in
        species_out = species_in
        tracked_barcodes_out = tracked_barcodes_in
    elif kind == StageKind.PHIX_ADJUSTED_NET_READS.value:
        assert stage.raw_reads is not None
        net_reads = stage.raw_reads * (1.0 - stage.phix_fraction) * stage.qc_pass_fraction
        effective_target = float(block.target_reads) if block.target_reads is not None else net_reads

        nearest_pcr = _nearest_pcr_template_ancestors(stage, stages_by_name)
        if not nearest_pcr:
            errors.append(
                _make_error(
                    f"stoichiometry.stages.{stage.name}.depends_on",
                    "phix_adjusted_net_reads stage requires at least one pcr_template_molecules ancestor",
                )
            )
        else:
            min_pcr_templates = min(state_by_name[name].entities_out for name in nearest_pcr)
            required_templates = block.min_template_ratio * effective_target
            if min_pcr_templates < required_templates:
                errors.append(
                    _make_error(
                        f"stoichiometry.stages.{stage.name}",
                        f"ancestor pcr_template_molecules={min_pcr_templates:.2f} < "
                        f"min_template_ratio * effective_target ({required_templates:.2f})",
                    )
                )

        if block.target_reads is not None and net_reads < block.target_reads:
            errors.append(
                _make_error(
                    f"stoichiometry.stages.{stage.name}",
                    f"net_reads={net_reads:.2f} < target_reads={block.target_reads}",
                )
            )

        if tracked_barcodes_in <= 0.0:
            errors.append(
                _make_error(
                    f"stoichiometry.stages.{stage.name}",
                    f"phix_adjusted_net_reads requires positive tracked_barcodes_in, got {tracked_barcodes_in}",
                )
            )
        else:
            reads_per_barcode = net_reads / tracked_barcodes_in
            if reads_per_barcode < block.min_consensus_reads:
                errors.append(
                    _make_error(
                        f"stoichiometry.stages.{stage.name}",
                        f"reads_per_barcode={reads_per_barcode:.6f} < "
                        f"min_consensus_reads={block.min_consensus_reads:.6f} "
                        f"(net_reads={net_reads:.2f}, tracked_barcodes={tracked_barcodes_in:.2f})",
                    )
                )

        entities_out = net_reads
        species_out = species_in
        tracked_barcodes_out = tracked_barcodes_in
    else:  # pragma: no cover - gated at parse time
        errors.append(
            _make_error(
                f"stoichiometry.stages.{stage.name}.kind",
                f"Unsupported stage kind {kind!r}",
            )
        )
        entities_out = entities_in
        species_out = species_in
        tracked_barcodes_out = tracked_barcodes_in

    return StageState(
        name=stage.name,
        kind=kind,
        entities_in=entities_in,
        entities_out=entities_out,
        tracked_barcodes_in=tracked_barcodes_in,
        tracked_barcodes_out=tracked_barcodes_out,
        species_in=species_in,
        species_out=species_out,
        collision=collision,
        reads_per_barcode=reads_per_barcode,
    )


def _validate_weighted_subpools(
    block: StoichiometryBlock,
    stage_states: dict[str, StageState],
    errors: list[ValidationError],
) -> None:
    if not block.subpools:
        return

    weights_dec: dict[str, Decimal] = {}
    resolved_mins: dict[str, Decimal] = {}
    for subpool_name, spec in block.subpools.items():
        weights_dec[subpool_name] = Decimal(str(spec.weight))
        min_group = spec.min_group_size if spec.min_group_size is not None else block.min_group_size
        if min_group is None:
            errors.append(
                _make_error(
                    f"stoichiometry.subpools.{subpool_name}.min_group_size",
                    f"Subpool {subpool_name!r} requires min_group_size or [stoichiometry].min_group_size",
                )
            )
            continue
        resolved_mins[subpool_name] = Decimal(min_group)

    if not resolved_mins:
        return

    total_weight_dec = sum(weights_dec.values(), Decimal("0"))
    pool_checkpoints: list[tuple[str, float]] = []

    colony_states = [
        state for state in stage_states.values() if state.kind == StageKind.COLONY_CFU.value
    ]
    if colony_states:
        pool_checkpoints.append(
            ("colony_cfu.entities_out", min(state.entities_out for state in colony_states))
        )

    read_states = [
        state
        for state in stage_states.values()
        if state.kind == StageKind.PHIX_ADJUSTED_NET_READS.value
    ]
    if read_states:
        pool_checkpoints.append(
            (
                "phix_adjusted_net_reads.tracked_barcodes_in",
                min(state.tracked_barcodes_in for state in read_states),
            )
        )

    for checkpoint_label, actual_pool_size in pool_checkpoints:
        actual_pool_dec = Decimal(str(actual_pool_size))
        required_total = float(
            max(
                (min_dec * total_weight_dec) / weights_dec[subpool_name]
                for subpool_name, min_dec in resolved_mins.items()
            )
        )
        for subpool_name, min_dec in resolved_mins.items():
            weight_dec = weights_dec[subpool_name]
            if actual_pool_dec * weight_dec < min_dec * total_weight_dec:
                allocated = float(actual_pool_dec * weight_dec / total_weight_dec)
                errors.append(
                    _make_error(
                        f"stoichiometry.subpools.{subpool_name}",
                        f"Subpool {subpool_name!r} undersized at {checkpoint_label}={actual_pool_size}: "
                        f"allocated={allocated:.6f} < min_group_size={int(min_dec)} "
                        f"(required_total={required_total:.6f})",
                    )
                )


def evaluate_stoichiometry(block: StoichiometryBlock) -> StoichiometryEvaluation:
    """Evaluate a `StoichiometryBlock` DAG and return structured states and errors."""
    errors: list[ValidationError] = []
    warnings: list[str] = []

    if block.warn_collision_prob is not None and block.warn_collision_prob > block.max_collision_prob:
        errors.append(
            _make_error(
                "stoichiometry.warn_collision_prob",
                f"warn_collision_prob ({block.warn_collision_prob}) exceeds "
                f"max_collision_prob ({block.max_collision_prob})",
            )
        )
    effective_warn_prob = (
        block.warn_collision_prob
        if block.warn_collision_prob is not None
        else 0.5 * block.max_collision_prob
    )

    topo_order, dag_errors = _topological_sort_stages(block.stages)
    errors.extend(dag_errors)
    if dag_errors:
        _validate_weighted_subpools(block, {}, errors)
        return StoichiometryEvaluation(
            errors=tuple(errors),
            warnings=tuple(warnings),
            topo_order=tuple(topo_order),
            stage_states={},
            collision_metrics={},
        )

    stages_by_name = {stage.name: stage for stage in block.stages}
    state_by_name: dict[str, StageState] = {}
    collision_metrics: dict[str, CollisionMetrics] = {}
    ancestors_by_name: dict[str, set[str]] = {}

    for stage_name in topo_order:
        stage = stages_by_name[stage_name]
        ancestors: set[str] = set(stage.depends_on)
        for parent_name in stage.depends_on:
            ancestors.update(ancestors_by_name[parent_name])
        ancestors_by_name[stage_name] = ancestors

        state = _evaluate_single_stage(
            stage=stage,
            block=block,
            effective_warn_prob=effective_warn_prob,
            stages_by_name=stages_by_name,
            state_by_name=state_by_name,
            errors=errors,
            warnings=warnings,
        )
        state_by_name[stage_name] = state
        if state.collision is not None:
            collision_metrics[stage_name] = state.collision

    if block.barcoding_mode == "pre_barcoded_vector":
        has_colony_collision = any(
            stages_by_name[stage_name].kind == StageKind.BARCODE_COLLISION.value
            and any(
                stages_by_name[anc].kind == StageKind.COLONY_CFU.value
                for anc in ancestors_by_name[stage_name]
            )
            for stage_name in topo_order
        )
        if not has_colony_collision:
            errors.append(
                _make_error(
                    "stoichiometry.barcoding_mode",
                    "barcoding_mode='pre_barcoded_vector' requires a barcode_collision stage "
                    "downstream of a colony_cfu stage",
                )
            )

    _validate_weighted_subpools(block, state_by_name, errors)

    return StoichiometryEvaluation(
        errors=tuple(errors),
        warnings=tuple(warnings),
        topo_order=tuple(topo_order),
        stage_states=state_by_name,
        collision_metrics=collision_metrics,
    )


def validate_stoichiometry_block(sidecar: Sidecar) -> tuple[list[ValidationError], list[str]]:
    """Validate optional `[stoichiometry]` block on a parsed `Sidecar`."""
    if sidecar.stoichiometry is None:
        return [], []
    evaluation = evaluate_stoichiometry(sidecar.stoichiometry)
    return list(evaluation.errors), list(evaluation.warnings)


def evaluate_reagents(reagents: dict[str, ReagentSpec]) -> ReagentsEvaluation:
    """Evaluate `[reagents.*]` pack amortization using exact cross-multiplied Decimal math."""
    errors: list[ValidationError] = []
    warnings: list[str] = []
    reports: dict[str, ReagentAmortization] = {}

    for reagent_name, spec in reagents.items():
        experiment_rxns_dec = Decimal(str(spec.experiment_rxns))
        pack_rxns_dec = Decimal(spec.pack_rxns)
        packs_required = int(
            (experiment_rxns_dec / pack_rxns_dec).to_integral_value(rounding=ROUND_CEILING)
        )
        purchased_rxns = packs_required * spec.pack_rxns
        purchased_dec = Decimal(purchased_rxns)
        unused_rxns_dec = purchased_dec - experiment_rxns_dec
        waste_fraction_dec = unused_rxns_dec / purchased_dec

        total_cost_dec: Decimal | None = None
        amortized_cost_dec: Decimal | None = None
        if spec.pack_cost_usd is not None:
            total_cost_dec = Decimal(packs_required) * Decimal(str(spec.pack_cost_usd))
            amortized_cost_dec = total_cost_dec / experiment_rxns_dec

        if (
            spec.max_total_cost_usd is not None or spec.max_cost_per_rxn_usd is not None
        ) and spec.pack_cost_usd is None:
            errors.append(
                _make_error(
                    f"reagents.{reagent_name}.pack_cost_usd",
                    f"Reagent {reagent_name!r} declares cost bounds without pack_cost_usd",
                )
            )

        if spec.max_unused_rxns is not None:
            max_unused_dec = Decimal(str(spec.max_unused_rxns))
            if unused_rxns_dec > max_unused_dec:
                errors.append(
                    _make_error(
                        f"reagents.{reagent_name}.max_unused_rxns",
                        f"Reagent {reagent_name!r} unused_rxns={float(unused_rxns_dec):.6f} "
                        f"exceeds max_unused_rxns={spec.max_unused_rxns}",
                    )
                )

        if spec.max_waste_fraction is not None:
            max_waste_dec = Decimal(str(spec.max_waste_fraction))
            if unused_rxns_dec > max_waste_dec * purchased_dec:
                errors.append(
                    _make_error(
                        f"reagents.{reagent_name}.max_waste_fraction",
                        f"Reagent {reagent_name!r} waste_fraction={float(waste_fraction_dec):.6f} "
                        f"exceeds max_waste_fraction={spec.max_waste_fraction}",
                    )
                )

        if total_cost_dec is not None:
            if spec.max_total_cost_usd is not None:
                max_total_cost_dec = Decimal(str(spec.max_total_cost_usd))
                if total_cost_dec > max_total_cost_dec:
                    errors.append(
                        _make_error(
                            f"reagents.{reagent_name}.max_total_cost_usd",
                            f"Reagent {reagent_name!r} total_cost_usd={float(total_cost_dec):.6f} "
                            f"exceeds max_total_cost_usd={spec.max_total_cost_usd}",
                        )
                    )
            if spec.max_cost_per_rxn_usd is not None:
                max_per_rxn_dec = Decimal(str(spec.max_cost_per_rxn_usd))
                if total_cost_dec > max_per_rxn_dec * experiment_rxns_dec:
                    assert amortized_cost_dec is not None
                    errors.append(
                        _make_error(
                            f"reagents.{reagent_name}.max_cost_per_rxn_usd",
                            f"Reagent {reagent_name!r} amortized_cost_per_rxn_usd="
                            f"{float(amortized_cost_dec):.6f} exceeds max_cost_per_rxn_usd="
                            f"{spec.max_cost_per_rxn_usd}",
                        )
                    )

        reports[reagent_name] = ReagentAmortization(
            name=reagent_name,
            pack_rxns=spec.pack_rxns,
            experiment_rxns=spec.experiment_rxns,
            packs_required=packs_required,
            purchased_rxns=purchased_rxns,
            unused_rxns=float(unused_rxns_dec),
            waste_fraction=float(waste_fraction_dec),
            total_cost_usd=float(total_cost_dec) if total_cost_dec is not None else None,
            amortized_cost_per_rxn_usd=(
                float(amortized_cost_dec) if amortized_cost_dec is not None else None
            ),
        )

    return ReagentsEvaluation(
        errors=tuple(errors),
        warnings=tuple(warnings),
        reports=reports,
    )


def validate_reagents_block(sidecar: Sidecar) -> tuple[list[ValidationError], list[str]]:
    """Validate optional `[reagents.<name>]` entries on a parsed `Sidecar`."""
    if not sidecar.reagents:
        return [], []
    evaluation = evaluate_reagents(sidecar.reagents)
    return list(evaluation.errors), list(evaluation.warnings)
