from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import duckdb


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


@dataclass
class Sidecar:
    kind: SidecarKind
    result_schema: dict[str, str]
    outcomes: dict[str, OutcomeSpec] = field(default_factory=dict)
    # experiment fields
    hypothesis: str = ""
    # benchmark fields
    baseline_ref: str = ""
    metric: str = ""
    regression_threshold: float = 0.0
    target: str = ""
    # validation fields
    property: str = ""
    reference: str = ""
    tolerance: str = ""
    # debug fields
    symptom: str = ""
    suspected_cause: str = ""
    verification: str = ""


ENFORCED_DIRS = {"experiments", "benchmarks", "validation"}


def parse_sidecar(path: Path) -> Sidecar:
    try:
        data = tomllib.loads(path.read_text())
    except Exception as e:
        raise SidecarError(f"Failed to parse {path}: {e}") from e

    if "experiment" in data:
        kind = SidecarKind.EXPERIMENT
        section = data["experiment"]
        outcomes = _parse_outcomes(data)
        return Sidecar(
            kind=kind,
            hypothesis=section.get("hypothesis", ""),
            outcomes=outcomes,
            result_schema=data.get("result_schema", {}),
        )
    elif "benchmark" in data:
        kind = SidecarKind.BENCHMARK
        section = data["benchmark"]
        return Sidecar(
            kind=kind,
            baseline_ref=section.get("baseline_ref", ""),
            metric=section.get("metric", ""),
            regression_threshold=section.get("regression_threshold", 0.0),
            target=section.get("target", ""),
            result_schema=data.get("result_schema", {}),
        )
    elif "validation" in data:
        kind = SidecarKind.VALIDATION
        section = data["validation"]
        outcomes = _parse_outcomes(data)
        return Sidecar(
            kind=kind,
            property=section.get("property", ""),
            reference=section.get("reference", ""),
            tolerance=section.get("tolerance", ""),
            outcomes=outcomes,
            result_schema=data.get("result_schema", {}),
        )
    elif "debug" in data:
        kind = SidecarKind.DEBUG
        section = data["debug"]
        outcomes = _parse_outcomes(data)
        return Sidecar(
            kind=kind,
            symptom=section.get("symptom", ""),
            suspected_cause=section.get("suspected_cause", ""),
            verification=section.get("verification", ""),
            outcomes=outcomes,
            result_schema=data.get("result_schema", {}) or data.get("verdict_schema", {}),
        )
    else:
        raise SidecarError(
            f"{path}: must have one of [experiment], [benchmark], [validation], [debug] sections"
        )


def _parse_outcomes(data: dict) -> dict[str, OutcomeSpec]:
    outcomes_data = data.get("outcomes", {})
    return {
        label: OutcomeSpec(
            condition=spec.get("condition", ""),
            decision=spec.get("decision", ""),
            reasoning=spec.get("reasoning", ""),
            is_residual=bool(spec.get("is_residual", False)),
        )
        for label, spec in outcomes_data.items()
    }


def find_sidecar(script_path: Path) -> Path | None:
    """Return the .bth.toml adjacent to script_path, or None if absent."""
    candidate = script_path.parent / f"{script_path.stem}.bth.toml"
    return candidate if candidate.exists() else None


def is_in_enforced_dir(script_path: Path) -> bool:
    """Return True if script is inside a directory name in ENFORCED_DIRS."""
    return any(part in ENFORCED_DIRS for part in script_path.parts)


def evaluate_outcome(sidecar: Sidecar, result: dict) -> str:
    """Evaluate DuckDB SQL fragments against result dict; return matching label or 'unknown'."""
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
        except Exception:
            continue
    return "unknown"
