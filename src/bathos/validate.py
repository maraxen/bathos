from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from bathos.sidecar import Sidecar, SidecarKind, ReproductionBlock, ControlsBlock
from bathos.telemetry import event


def _map_type_to_sql(python_type: str) -> str:
    """Map Python type strings to DuckDB SQL types."""
    mapping = {
        "int": "INTEGER",
        "float": "DOUBLE",
        "str": "VARCHAR",
        "bool": "BOOLEAN",
    }
    return mapping.get(python_type, "VARCHAR")


@dataclass
class ValidationError:
    field: str
    message: str


@dataclass
class ValidationResult:
    ok: bool
    errors: list[ValidationError] = field(default_factory=list)


def validate_popper_block(sidecar: Sidecar, sidecar_path: Path | None = None) -> list[ValidationError]:
    """Validate [popper] block fields per the spec (Section 2.3 of #792 spec).

    Returns a list of ValidationError (empty if valid).
    WARNING-level entries use message prefixed with "WARNING:".
    """
    errors: list[ValidationError] = []

    if sidecar.popper_null_pass_rate is None:
        return errors  # No [popper] block — nothing to validate

    # [popper] is only valid on experiment sidecars
    if sidecar.kind != SidecarKind.EXPERIMENT:
        errors.append(ValidationError(
            "popper",
            "[popper] block is only valid in [experiment] sidecars",
        ))
        return errors  # No point validating further

    null = sidecar.popper_null_pass_rate
    alt = sidecar.popper_alt_pass_rate
    threshold = sidecar.popper_stopping_threshold

    # null_pass_rate range
    if null is None or not (0 < null < 1):
        errors.append(ValidationError(
            "popper.null_pass_rate",
            f"null_pass_rate must be in (0, 1), got {null!r}",
        ))

    # alt_pass_rate range
    if alt is None or not (0 < alt < 1):
        errors.append(ValidationError(
            "popper.alt_pass_rate",
            f"alt_pass_rate must be in (0, 1), got {alt!r}",
        ))

    # null != alt (no test power if equal)
    if null is not None and alt is not None and null == alt:
        errors.append(ValidationError(
            "popper",
            "null_pass_rate and alt_pass_rate are identical; no test power",
        ))

    # stopping_threshold >= 1.0
    if threshold is None or threshold < 1.0:
        errors.append(ValidationError(
            "popper.stopping_threshold",
            f"stopping_threshold must be >= 1.0, got {threshold!r}",
        ))
    elif threshold < 10.0:
        errors.append(ValidationError(
            "popper.stopping_threshold",
            f"WARNING: stopping_threshold < 10.0 — consider a stricter threshold (alpha < 0.10)",
        ))

    # Validate [popper.weights] if present
    declared_labels = set(sidecar.outcomes.keys())
    for key, val in sidecar.popper_weights.items():
        if key not in declared_labels and key not in ("error", "unknown", "marginal"):
            errors.append(ValidationError(
                f"popper.weights.{key}",
                f"Unknown outcome label {key!r} in [popper.weights] — not declared in [outcomes]",
            ))
        if val <= 0:
            errors.append(ValidationError(
                f"popper.weights.{key}",
                f"Weight for {key!r} must be > 0, got {val!r}",
            ))
        if key == "error" and val != 1.0:
            errors.append(ValidationError(
                "popper.weights.error",
                f"Weight for 'error' must be exactly 1.0 (non-overridable), got {val!r}",
            ))

    return errors


def validate_reproduction_block(sidecar: Sidecar, sidecar_path: Path | None = None) -> list[ValidationError]:
    """Validate [reproduction] block fields.

    Returns a list of ValidationError (empty if valid or no block present).
    """
    import re

    errors: list[ValidationError] = []

    if sidecar.reproduction is None:
        return errors  # No [reproduction] block — nothing to validate

    repro = sidecar.reproduction

    # tolerance_pct: if set, must be 0.0 <= v <= 100.0
    if repro.tolerance_pct is not None:
        if not (0.0 <= repro.tolerance_pct <= 100.0):
            errors.append(ValidationError(
                "reproduction.tolerance_pct",
                f"tolerance_pct must be in [0.0, 100.0], got {repro.tolerance_pct!r}",
            ))
            if sidecar_path:
                event("sidecar.validate_error", path=str(sidecar_path), field="reproduction.tolerance_pct", reason=f"tolerance_pct out of range: {repro.tolerance_pct}")

    # reproduces_run: if non-empty, must match UUID regex r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    if repro.reproduces_run:
        uuid_pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        if not re.match(uuid_pattern, repro.reproduces_run):
            errors.append(ValidationError(
                "reproduction.reproduces_run",
                f"reproduces_run must be a valid UUID, got {repro.reproduces_run!r}",
            ))
            if sidecar_path:
                event("sidecar.validate_error", path=str(sidecar_path), field="reproduction.reproduces_run", reason=f"Invalid UUID format: {repro.reproduces_run}")

    return errors


def validate_controls_block(sidecar: Sidecar, sidecar_path: Path | None = None) -> list[ValidationError]:
    """Validate [controls] block fields.

    Checks that all labels in positive_outcome and negative_outcome exist in [outcomes].
    Returns a list of ValidationError (empty if valid or no block present).
    """
    errors: list[ValidationError] = []

    if sidecar.controls is None:
        return errors  # No [controls] block — nothing to validate

    controls = sidecar.controls
    outcome_keys = set(sidecar.outcomes.keys())

    # Check positive_outcome labels exist in outcomes
    for label in controls.positive_outcome:
        if label not in outcome_keys:
            errors.append(ValidationError(
                f"controls.positive_outcome",
                f"Label {label!r} not declared in [outcomes]",
            ))
            if sidecar_path:
                event("sidecar.validate_error", path=str(sidecar_path), field="controls.positive_outcome", reason=f"Label {label!r} not found in outcomes")

    # Check negative_outcome labels exist in outcomes
    for label in controls.negative_outcome:
        if label not in outcome_keys:
            errors.append(ValidationError(
                f"controls.negative_outcome",
                f"Label {label!r} not declared in [outcomes]",
            ))
            if sidecar_path:
                event("sidecar.validate_error", path=str(sidecar_path), field="controls.negative_outcome", reason=f"Label {label!r} not found in outcomes")

    return errors


def validate_sidecar(sidecar: Sidecar, sidecar_path: Path | None = None) -> ValidationResult:
    """Validate a parsed Sidecar for structural integrity and logical consistency.

    Checks:
    - Outcomes section exists
    - Each outcome branch has condition, decision, and reasoning
    - DuckDB SQL conditions parse correctly
    - At least one result_schema field is referenced in conditions
    - At least one outcome branch has is_residual=true (catch-all fallback)

    Returns ValidationResult with ok=True and errors=[] if valid, or ok=False with a list of errors.
    """
    errors = []

    # Must have outcomes
    if not sidecar.outcomes:
        errors.append(
            ValidationError("outcomes", "No [outcomes] section found")
        )
        if sidecar_path:
            event("sidecar.validate_error", path=str(sidecar_path), field="outcomes", reason="No [outcomes] section found")
        return ValidationResult(ok=False, errors=errors)

    # Each branch must have condition, decision, reasoning
    for label, spec in sidecar.outcomes.items():
        if not spec.condition:
            err = ValidationError(
                f"outcomes.{label}", "Missing 'condition' field"
            )
            errors.append(err)
            if sidecar_path:
                event("sidecar.validate_error", path=str(sidecar_path), field=f"outcomes.{label}", reason="Missing 'condition' field")
        if not spec.decision:
            err = ValidationError(
                f"outcomes.{label}", "Missing 'decision' field"
            )
            errors.append(err)
            if sidecar_path:
                event("sidecar.validate_error", path=str(sidecar_path), field=f"outcomes.{label}", reason="Missing 'decision' field")
        if not spec.reasoning:
            err = ValidationError(
                f"outcomes.{label}",
                "Missing 'reasoning' field",
            )
            errors.append(err)
            if sidecar_path:
                event("sidecar.validate_error", path=str(sidecar_path), field=f"outcomes.{label}", reason="Missing 'reasoning' field")

        # DuckDB SQL must parse
        if spec.condition:
            try:
                con = duckdb.connect()
                # Build a dummy table from result_schema to validate SQL
                if sidecar.result_schema:
                    cols = ", ".join(
                        f"{k} {_map_type_to_sql(v)}"
                        for k, v in sidecar.result_schema.items()
                        if isinstance(v, str)  # skip nested tables (e.g. [result_schema.provenance])
                    )
                    if cols:
                        con.execute(f"CREATE TEMP TABLE _dummy ({cols})")
                        con.execute(f"SELECT ({spec.condition}) FROM _dummy LIMIT 0")
                    else:
                        # result_schema has only nested sub-tables (e.g. [result_schema.provenance])
                        con.execute(f"SELECT ({spec.condition})")
                else:
                    # If no schema, just validate as boolean expression
                    con.execute(f"SELECT ({spec.condition})")
                con.close()
            except Exception as e:
                err = ValidationError(
                    f"outcomes.{label}.condition",
                    f"DuckDB parse error: {e}",
                )
                errors.append(err)
                if sidecar_path:
                    event("sidecar.validate_error", path=str(sidecar_path), field=f"outcomes.{label}.condition", reason=f"DuckDB parse error: {e}")

    # At least one result_schema field must appear in conditions
    if sidecar.result_schema:
        schema_keys = set(sidecar.result_schema.keys())
        all_conditions = " ".join(
            s.condition for s in sidecar.outcomes.values() if s.condition
        )
        if not any(key in all_conditions for key in schema_keys):
            err = ValidationError(
                "result_schema",
                "No result_schema fields referenced in any outcome condition",
            )
            errors.append(err)
            if sidecar_path:
                event("sidecar.validate_error", path=str(sidecar_path), field="result_schema", reason="No result_schema fields referenced in any outcome condition")

    # Must have at least one is_residual=true fallback branch
    has_residual = any(s.is_residual for s in sidecar.outcomes.values())
    if not has_residual:
        err = ValidationError(
            "outcomes",
            "No fallback branch with is_residual=true — add a catch-all outcome",
        )
        errors.append(err)
        if sidecar_path:
            event("sidecar.validate_error", path=str(sidecar_path), field="outcomes", reason="No fallback branch with is_residual=true")

    # Validate [popper] block if present
    popper_errors = validate_popper_block(sidecar, sidecar_path)
    errors.extend(popper_errors)

    # Validate [reproduction] block if present
    repro_errors = validate_reproduction_block(sidecar, sidecar_path)
    errors.extend(repro_errors)

    # Validate [controls] block if present
    controls_errors = validate_controls_block(sidecar, sidecar_path)
    errors.extend(controls_errors)

    return ValidationResult(ok=len(errors) == 0, errors=errors)
