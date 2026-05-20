from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from bathos.sidecar import Sidecar, SidecarKind


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


def validate_sidecar(sidecar: Sidecar) -> ValidationResult:
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
        return ValidationResult(ok=False, errors=errors)

    # Each branch must have condition, decision, reasoning
    for label, spec in sidecar.outcomes.items():
        if not spec.condition:
            errors.append(
                ValidationError(
                    f"outcomes.{label}", "Missing 'condition' field"
                )
            )
        if not spec.decision:
            errors.append(
                ValidationError(
                    f"outcomes.{label}", "Missing 'decision' field"
                )
            )
        if not spec.reasoning:
            errors.append(
                ValidationError(
                    f"outcomes.{label}",
                    "Missing 'reasoning' field",
                )
            )

        # DuckDB SQL must parse
        if spec.condition:
            try:
                con = duckdb.connect()
                # Build a dummy table from result_schema to validate SQL
                if sidecar.result_schema:
                    cols = ", ".join(
                        f"{k} {_map_type_to_sql(v)}"
                        for k, v in sidecar.result_schema.items()
                    )
                    dummy_table = f"CREATE TEMP TABLE _dummy ({cols})"
                    con.execute(dummy_table)
                    con.execute(f"SELECT ({spec.condition}) FROM _dummy LIMIT 0")
                else:
                    # If no schema, just validate as boolean expression
                    con.execute(f"SELECT ({spec.condition})")
                con.close()
            except Exception as e:
                errors.append(
                    ValidationError(
                        f"outcomes.{label}.condition",
                        f"DuckDB parse error: {e}",
                    )
                )

    # At least one result_schema field must appear in conditions
    if sidecar.result_schema:
        schema_keys = set(sidecar.result_schema.keys())
        all_conditions = " ".join(
            s.condition for s in sidecar.outcomes.values() if s.condition
        )
        if not any(key in all_conditions for key in schema_keys):
            errors.append(
                ValidationError(
                    "result_schema",
                    "No result_schema fields referenced in any outcome condition",
                )
            )

    # Must have at least one is_residual=true fallback branch
    has_residual = any(s.is_residual for s in sidecar.outcomes.values())
    if not has_residual:
        errors.append(
            ValidationError(
                "outcomes",
                "No fallback branch with is_residual=true — add a catch-all outcome",
            )
        )

    return ValidationResult(ok=len(errors) == 0, errors=errors)
