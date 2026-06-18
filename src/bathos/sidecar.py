from __future__ import annotations

import hashlib
import logging
import re
import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import duckdb

from bathos.telemetry import event

logger = logging.getLogger(__name__)


class SidecarError(Exception):
    pass


class SidecarKind(str, Enum):
    EXPERIMENT = "experiment"
    BENCHMARK = "benchmark"
    VALIDATION = "validation"
    DEBUG = "debug"


@dataclass
class OutcomeSpec:
    condition: str
    decision: str
    reasoning: str = ""
    is_residual: bool = False
    adversarial_check: str | None = None
    source: str = ""


@dataclass
class ReproductionBlock:
    """Reproduction metadata for experiment sidecars (optional [reproduction] block)."""
    reproduces_paper: str = ""       # DOI or citation string
    reproduces_run: str = ""         # run UUID
    tolerance_pct: float | None = None
    requires_pass_stem: str = ""     # script stem that must have outcome='pass' first
    requires_parity_stem: str = ""   # script stem that must have a passing parity run first (F3 gate)


@dataclass
class ControlsBlock:
    """Control arm specification for experiment sidecars (optional [controls] block)."""
    positive_outcome: list[str] = field(default_factory=list)
    negative_outcome: list[str] = field(default_factory=list)


@dataclass
class Sidecar:
    kind: SidecarKind
    result_schema: dict[str, str]
    outcomes: dict[str, OutcomeSpec] = field(default_factory=dict)
    # experiment fields
    hypothesis: str = ""
    stage_name: str = "exploration"
    novel: bool = False
    # benchmark fields
    baseline_ref: str = ""
    metric: str = ""
    regression_threshold: float = 0.0
    regression_threshold_basis: str = ""
    target: str = ""
    # validation fields
    property: str = ""
    reference: str = ""
    tolerance: str = ""
    # debug fields
    symptom: str = ""
    suspected_cause: str = ""
    verification: str = ""
    # agent mode field (all kinds)
    agent_mode: str = ""
    # popper sequential test fields (experiment sidecars only)
    popper_null_pass_rate: float | None = None
    popper_alt_pass_rate: float | None = None
    popper_stopping_threshold: float | None = None
    popper_weights: dict[str, float] = field(default_factory=dict)
    # reproduction metadata (experiment sidecars only)
    reproduction: ReproductionBlock | None = None
    # controls metadata (experiment sidecars only)
    controls: ControlsBlock | None = None


ENFORCED_DIRS = {"experiments", "benchmarks", "validation"}

# Canonical set: the advisory vocabulary for stage_name values.
# These are best-practice values discovered from real stage_name usage.
# Not enforced in the schema — only advisory in CI lint.
CANONICAL_STAGES = {
    "exploration",
    "calibration",
    "validation",
    "ablation",
    "production",
}


def parse_sidecar(path: Path) -> Sidecar:
    try:
        data = tomllib.loads(path.read_text())
    except Exception as e:
        event("sidecar.parse_error", path=str(path), exc_type=type(e).__name__, exc_msg=str(e))
        raise SidecarError(f"Failed to parse {path}: {e}") from e

    sidecar = None
    if "experiment" in data:
        kind = SidecarKind.EXPERIMENT
        section = data["experiment"]
        outcomes = _parse_outcomes(data)

        # Parse stage_name with validation and coercion
        stage_name = section.get("stage_name", "exploration")
        if stage_name not in CANONICAL_STAGES:
            logger.warning(
                f"Invalid stage_name '{stage_name}' in {path}; must be one of {CANONICAL_STAGES}. "
                "Coercing to 'exploration'."
            )
            stage_name = "exploration"

        # Parse novel field
        novel = bool(section.get("novel", False))

        sidecar = Sidecar(
            kind=kind,
            hypothesis=section.get("hypothesis", ""),
            stage_name=stage_name,
            novel=novel,
            outcomes=outcomes,
            result_schema=data.get("result_schema", {}),
            agent_mode=section.get("agent_mode", ""),
        )
        popper = data.get("popper", {})
        if popper:
            sidecar.popper_null_pass_rate = popper.get("null_pass_rate")
            sidecar.popper_alt_pass_rate = popper.get("alt_pass_rate")
            sidecar.popper_stopping_threshold = popper.get("stopping_threshold")
            weights = popper.get("weights", {})
            if isinstance(weights, dict):
                sidecar.popper_weights = {k: float(v) for k, v in weights.items()}

        # Parse [reproduction] block (optional)
        repro_data = data.get("reproduction", {})
        if repro_data:
            sidecar.reproduction = ReproductionBlock(
                reproduces_paper=repro_data.get("reproduces_paper", ""),
                reproduces_run=repro_data.get("reproduces_run", ""),
                tolerance_pct=repro_data.get("tolerance_pct", None),
                requires_pass_stem=repro_data.get("requires_pass_stem", ""),
                requires_parity_stem=repro_data.get("requires_parity_stem", ""),
            )
            # Warn on unknown keys in [reproduction]
            for key in repro_data:
                if key not in {"reproduces_paper", "reproduces_run", "tolerance_pct", "requires_pass_stem", "requires_parity_stem"}:
                    logger.warning(f"Unknown key in [reproduction]: {key!r}")

        # Parse [controls] block (optional)
        if "controls" in data:
            controls_data = data.get("controls", {})
            sidecar.controls = ControlsBlock(
                positive_outcome=controls_data.get("positive_outcome", []),
                negative_outcome=controls_data.get("negative_outcome", []),
            )
            # Warn on unknown keys in [controls]
            for key in controls_data:
                if key not in {"positive_outcome", "negative_outcome"}:
                    logger.warning(f"Unknown key in [controls]: {key!r}")
    elif "benchmark" in data:
        kind = SidecarKind.BENCHMARK
        section = data["benchmark"]
        sidecar = Sidecar(
            kind=kind,
            baseline_ref=section.get("baseline_ref", ""),
            metric=section.get("metric", ""),
            regression_threshold=section.get("regression_threshold", 0.0),
            regression_threshold_basis=section.get("regression_threshold_basis", ""),
            target=section.get("target", ""),
            result_schema=data.get("result_schema", {}),
            agent_mode=section.get("agent_mode", ""),
        )
    elif "validation" in data:
        kind = SidecarKind.VALIDATION
        section = data["validation"]
        outcomes = _parse_outcomes(data)
        sidecar = Sidecar(
            kind=kind,
            property=section.get("property", ""),
            reference=section.get("reference", ""),
            tolerance=section.get("tolerance", ""),
            outcomes=outcomes,
            result_schema=data.get("result_schema", {}),
            agent_mode=section.get("agent_mode", ""),
        )
    elif "debug" in data:
        kind = SidecarKind.DEBUG
        section = data["debug"]
        outcomes = _parse_outcomes(data)
        sidecar = Sidecar(
            kind=kind,
            symptom=section.get("symptom", ""),
            suspected_cause=section.get("suspected_cause", ""),
            verification=section.get("verification", ""),
            outcomes=outcomes,
            result_schema=data.get("result_schema", {}) or data.get("verdict_schema", {}),
            agent_mode=section.get("agent_mode", ""),
        )
    else:
        raise SidecarError(
            f"{path}: must have one of [experiment], [benchmark], [validation], [debug] sections"
        )

    if sidecar:
        sha256_val = hashlib.sha256(path.read_bytes()).hexdigest()
        outcome_labels = list(sidecar.outcomes.keys())
        event("sidecar.parsed", path=str(path), sha256=sha256_val, outcomes=outcome_labels, kind=sidecar.kind.value)

    return sidecar


def _parse_outcomes(data: dict) -> dict[str, OutcomeSpec]:
    outcomes_data = data.get("outcomes", {})
    return {
        label: OutcomeSpec(
            condition=spec.get("condition", ""),
            decision=spec.get("decision", ""),
            reasoning=spec.get("reasoning", ""),
            is_residual=bool(spec.get("is_residual", False)),
            adversarial_check=spec.get("adversarial_check"),
            source=spec.get("source", ""),
        )
        for label, spec in outcomes_data.items()
    }


def compute_evalue(
    sidecar: Sidecar,
    outcome_label: str,
    pass_labels: set[str] | None = None,
) -> float:
    """Compute the likelihood-ratio e-value for a single run outcome.

    Returns 1.0 (neutral) if the sidecar has no [popper] block.
    Error and unknown outcomes always return 1.0 (non-overridable).
    """
    if sidecar.popper_null_pass_rate is None:
        return 1.0

    if outcome_label in ("error", "unknown"):
        return 1.0

    # Explicit weight override takes priority
    if outcome_label in sidecar.popper_weights:
        return float(sidecar.popper_weights[outcome_label])

    null = sidecar.popper_null_pass_rate
    alt = sidecar.popper_alt_pass_rate

    # Marginal: hard default 1.0
    if outcome_label == "marginal":
        return 1.0

    # Determine pass-direction labels if not provided
    if pass_labels is None:
        pass_labels = {
            label
            for label, spec in sidecar.outcomes.items()
            if not spec.is_residual and label not in ("marginal", "error", "unknown")
        }

    if outcome_label in pass_labels:
        evalue = alt / null
    else:
        evalue = (1.0 - alt) / (1.0 - null)

    assert evalue > 0, f"compute_evalue produced non-positive value {evalue} for outcome '{outcome_label}'"
    return evalue


def find_sidecar(script_path: Path) -> Path | None:
    """Return the .bth.toml adjacent to script_path, or None if absent."""
    candidate = script_path.parent / f"{script_path.stem}.bth.toml"
    return candidate if candidate.exists() else None


def is_in_enforced_dir(script_path: Path) -> bool:
    """Return True if script is inside a directory name in ENFORCED_DIRS."""
    return any(part in ENFORCED_DIRS for part in script_path.parts)


def evaluate_outcome(sidecar: Sidecar, result: dict) -> str:
    """Evaluate DuckDB SQL fragments against result dict; return matching label or 'unknown'.

    Raises SidecarError if a SQL condition is malformed.
    """
    if not sidecar.outcomes or not result:
        return "unknown"

    def _sql_literal(v: object, k: str) -> str:
        if isinstance(v, bool):
            return f"{'TRUE' if v else 'FALSE'} AS {k}"
        if isinstance(v, float):
            return f"{v!r}::DOUBLE AS {k}"
        return f"{v!r} AS {k}"

    cols = ", ".join(_sql_literal(v, k) for k, v in result.items())
    for label, spec in sidecar.outcomes.items():
        try:
            rows = duckdb.execute(f"SELECT ({spec.condition}) FROM (SELECT {cols})").fetchall()
            if rows and rows[0][0]:
                return label
        except Exception as e:
            raise SidecarError(f"Failed to evaluate outcome '{label}': {e}") from e
    return "unknown"
