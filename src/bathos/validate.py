from __future__ import annotations

import json
import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from bathos.sidecar import (
    BREAKDOWN_DIRECTIONS,
    RESERVED_OUTCOME_LABELS,
    Sidecar,
    SidecarKind,
    _condition_value,
    breakdown_summary_columns,
    check_sanity_expr,
    columns_referenced,
    evaluate_adversarial_check_detailed,
    expand_breakdown_aggregates,
    parse_source,
    resolve_epistemic_class,
    uncovered_aggregate_message,
    uncovered_aggregates,
)
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


def _quote_sql_ident(name: str) -> str:
    escaped = str(name).replace('"', '""')
    return f'"{escaped}"'


def _schema_type_to_sql(spec_val: object) -> str | None:
    """Map a `[result_schema]` value (scalar type string or nested table dict) to DuckDB SQL."""
    if isinstance(spec_val, str):
        return _map_type_to_sql(spec_val)
    if isinstance(spec_val, dict):
        fields: list[str] = []
        for sub_k, sub_v in spec_val.items():
            sub_sql = _schema_type_to_sql(sub_v)
            if sub_sql is not None:
                fields.append(f"{_quote_sql_ident(sub_k)} {sub_sql}")
        if fields:
            return f"STRUCT({', '.join(fields)})"
        return "VARCHAR"
    return None


def _collect_schema_field_names(schema: dict[str, Any], prefix: str = "") -> set[str]:
    """Collect top-level and dotted nested field names from `[result_schema]`."""
    names: set[str] = set()
    for key, val in schema.items():
        dotted = f"{prefix}.{key}" if prefix else key
        names.add(dotted)
        if isinstance(val, dict):
            names.update(_collect_schema_field_names(val, dotted))
    return names


_SQL_STRING_RE = re.compile(r"'(?:''|[^'])*'")
_NULL_EQ_RIGHT_RE = re.compile(r"(?<![!<>])(?:==|!=|<>|=)\s*NULL\b", re.IGNORECASE)
_NULL_EQ_LEFT_RE = re.compile(r"\bNULL\s*(?:==|!=|<>|=)(?!=)", re.IGNORECASE)


def _strip_sql_string_literals(expr: str) -> str:
    """Replace SQL single-quoted string literals with empty quotes so regexes ignore literal text."""
    return _SQL_STRING_RE.sub("''", expr)


def _find_null_equality_ops(expr: str) -> list[str]:
    """Return SQL `= NULL` / `!= NULL` / `<> NULL` operators outside string literals."""
    stripped = _strip_sql_string_literals(expr)
    hits: list[str] = []
    for pat in (_NULL_EQ_RIGHT_RE, _NULL_EQ_LEFT_RE):
        for m in pat.finditer(stripped):
            hits.append(m.group(0).strip())
    return hits


_TAUTOLOGY_RE = re.compile(r"^\s*(?:true|1\s*==?\s*1|0\s*==?\s*0)\s*$", re.IGNORECASE)
_CONTRADICTION_RE = re.compile(r"^\s*(?:false|1\s*==?\s*0|0\s*==?\s*1)\s*$", re.IGNORECASE)
_IDENT_PAT = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
_NUM_PAT = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_CMP_LEFT_RE = re.compile(
    rf"^\s*({_IDENT_PAT})\s*(<=|>=|<>|!=|==|=|<|>)\s*({_NUM_PAT})\s*$"
)
_CMP_RIGHT_RE = re.compile(
    rf"^\s*({_NUM_PAT})\s*(<=|>=|<>|!=|==|=|<|>)\s*({_IDENT_PAT})\s*$"
)
_IS_NULL_RE = re.compile(rf"^\s*({_IDENT_PAT})\s+IS\s+(NOT\s+)?NULL\s*$", re.IGNORECASE)
_BOOL_EQ_RE = re.compile(
    rf"^\s*({_IDENT_PAT})\s*(==|=|!=|<>)\s*(true|false)\s*$", re.IGNORECASE
)
_STR_EQ_RE = re.compile(
    rf"^\s*({_IDENT_PAT})\s*(==|=|!=|<>)\s*'((?:''|[^'])*)'\s*$", re.IGNORECASE
)

_FLIP_OP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "=", "==": "==", "!=": "!=", "<>": "<>"}


@dataclass
class _NumConstraint:
    lo: float | None = None
    lo_inc: bool = False
    hi: float | None = None
    hi_inc: bool = False
    neq: set[float] = field(default_factory=set)

    def tighten(self, op: str, val: float) -> None:
        if op in ("=", "=="):
            if self.lo is None or val > self.lo:
                self.lo, self.lo_inc = val, True
            elif val == self.lo:
                self.lo_inc = self.lo_inc and True
            if self.hi is None or val < self.hi:
                self.hi, self.hi_inc = val, True
            elif val == self.hi:
                self.hi_inc = self.hi_inc and True
        elif op == ">":
            if self.lo is None or val > self.lo or (val == self.lo and self.lo_inc):
                self.lo, self.lo_inc = val, False
        elif op == ">=":
            if self.lo is None or val > self.lo:
                self.lo, self.lo_inc = val, True
        elif op == "<":
            if self.hi is None or val < self.hi or (val == self.hi and self.hi_inc):
                self.hi, self.hi_inc = val, False
        elif op == "<=":
            if self.hi is None or val < self.hi:
                self.hi, self.hi_inc = val, True
        elif op in ("!=", "<>"):
            self.neq.add(val)

    def is_empty(self) -> bool:
        if self.lo is not None and self.hi is not None:
            if self.lo > self.hi:
                return True
            if self.lo == self.hi and not (self.lo_inc and self.hi_inc):
                return True
            if self.lo == self.hi and self.lo in self.neq:
                return True
        return False

    def subset_of(self, other: _NumConstraint) -> bool:
        if self.is_empty():
            return True
        if other.lo is not None:
            if self.lo is None or self.lo < other.lo:
                return False
            if self.lo == other.lo and self.lo_inc and not other.lo_inc:
                return False
        if other.hi is not None:
            if self.hi is None or self.hi > other.hi:
                return False
            if self.hi == other.hi and self.hi_inc and not other.hi_inc:
                return False
        for v in other.neq:
            if v in self.neq:
                continue
            in_lo = self.lo is None or (v > self.lo or (v == self.lo and self.lo_inc))
            in_hi = self.hi is None or (v < self.hi or (v == self.hi and self.hi_inc))
            if in_lo and in_hi:
                return False
        return True


def _split_top_level_and(expr: str) -> list[str] | None:
    """Split on top-level AND outside parentheses/quotes; return None if top-level OR appears."""
    parts: list[str] = []
    depth = 0
    in_quote = False
    i = 0
    start = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if ch == "'":
            if in_quote and i + 1 < n and expr[i + 1] == "'":
                i += 2
                continue
            in_quote = not in_quote
            i += 1
            continue
        if in_quote:
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            # Check keyword boundary for OR / AND
            prev_ok = i == 0 or not (expr[i - 1].isalnum() or expr[i - 1] == "_")
            if prev_ok:
                rest = expr[i:]
                m_or = re.match(r"OR(?![A-Za-z0-9_])", rest, re.IGNORECASE)
                if m_or:
                    return None
                m_and = re.match(r"AND(?![A-Za-z0-9_])", rest, re.IGNORECASE)
                if m_and:
                    parts.append(expr[start:i].strip())
                    i += m_and.end()
                    start = i
                    continue
        i += 1
    parts.append(expr[start:].strip())
    return [p for p in parts if p]


def _unwrap_parens(atom: str) -> str:
    s = atom.strip()
    while s.startswith("(") and s.endswith(")"):
        depth = 0
        in_quote = False
        balanced = True
        for idx, ch in enumerate(s):
            if ch == "'":
                if in_quote and idx + 1 < len(s) and s[idx + 1] == "'":
                    continue
                in_quote = not in_quote
            elif not in_quote:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0 and idx < len(s) - 1:
                        balanced = False
                        break
        if balanced and depth == 0:
            s = s[1:-1].strip()
        else:
            break
    return s


@dataclass
class _ParsedConjunction:
    is_tautology: bool = False
    is_empty: bool = False
    num: dict[str, _NumConstraint] = field(default_factory=dict)
    eq: dict[str, set[tuple[str, Any]]] = field(default_factory=dict)  # col -> {("eq", val) | ("neq", val)}
    nullness: dict[str, bool] = field(default_factory=dict)  # True=IS NULL, False=IS NOT NULL

    def subsumes(self, candidate: _ParsedConjunction) -> bool:
        """Return True when every assignment satisfying `candidate` must satisfy `self`."""
        if candidate.is_empty:
            return True
        if self.is_tautology:
            return True
        if self.is_empty or candidate.is_tautology:
            return False
        for col, req_null in self.nullness.items():
            if candidate.nullness.get(col) != req_null:
                return False
        for col, req_num in self.num.items():
            cand_num = candidate.num.get(col)
            if cand_num is None or not cand_num.subset_of(req_num):
                return False
        for col, req_eqs in self.eq.items():
            cand_eqs = candidate.eq.get(col, set())
            if not req_eqs.issubset(cand_eqs):
                return False
        return True


def _parse_conjunction(expr: str) -> _ParsedConjunction | None:
    """Parse simple SQL conjunctions for exact subsumption/unsatisfiability analysis."""
    stripped = _unwrap_parens(expr)
    if _TAUTOLOGY_RE.match(stripped):
        return _ParsedConjunction(is_tautology=True)
    if _CONTRADICTION_RE.match(stripped):
        return _ParsedConjunction(is_empty=True)

    clauses = _split_top_level_and(stripped)
    if clauses is None:
        return None

    parsed = _ParsedConjunction()
    for raw_clause in clauses:
        clause = _unwrap_parens(raw_clause)
        if _TAUTOLOGY_RE.match(clause):
            continue
        if _CONTRADICTION_RE.match(clause):
            return _ParsedConjunction(is_empty=True)

        m_l = _CMP_LEFT_RE.match(clause)
        if m_l:
            col, op, num_s = m_l.groups()
            parsed.nullness.setdefault(col, False)
            if parsed.nullness[col] is True:
                return _ParsedConjunction(is_empty=True)
            nc = parsed.num.setdefault(col, _NumConstraint())
            nc.tighten(op, float(num_s))
            if nc.is_empty():
                return _ParsedConjunction(is_empty=True)
            continue

        m_r = _CMP_RIGHT_RE.match(clause)
        if m_r:
            num_s, op, col = m_r.groups()
            parsed.nullness.setdefault(col, False)
            if parsed.nullness[col] is True:
                return _ParsedConjunction(is_empty=True)
            nc = parsed.num.setdefault(col, _NumConstraint())
            nc.tighten(_FLIP_OP[op], float(num_s))
            if nc.is_empty():
                return _ParsedConjunction(is_empty=True)
            continue

        m_null = _IS_NULL_RE.match(clause)
        if m_null:
            col, is_not = m_null.groups()
            want_null = is_not is None
            if col in parsed.nullness and parsed.nullness[col] != want_null:
                return _ParsedConjunction(is_empty=True)
            parsed.nullness[col] = want_null
            continue

        m_bool = _BOOL_EQ_RE.match(clause)
        if m_bool:
            col, op, b_str = m_bool.groups()
            val = b_str.lower() == "true"
            if op in ("!=", "<>"):
                val = not val
            if parsed.nullness.get(col) is True:
                return _ParsedConjunction(is_empty=True)
            parsed.nullness[col] = False
            bucket = parsed.eq.setdefault(col, set())
            if ("eq", not val) in bucket:
                return _ParsedConjunction(is_empty=True)
            bucket.add(("eq", val))
            continue

        m_str = _STR_EQ_RE.match(clause)
        if m_str:
            col, op, s_val = m_str.groups()
            if parsed.nullness.get(col) is True:
                return _ParsedConjunction(is_empty=True)
            parsed.nullness[col] = False
            bucket = parsed.eq.setdefault(col, set())
            kind = "neq" if op in ("!=", "<>") else "eq"
            other_kind = "eq" if kind == "neq" else "neq"
            if (other_kind, s_val) in bucket:
                return _ParsedConjunction(is_empty=True)
            if kind == "eq":
                for prev_k, prev_v in bucket:
                    if prev_k == "eq" and prev_v != s_val:
                        return _ParsedConjunction(is_empty=True)
            bucket.add((kind, s_val))
            continue

        # Unknown atom (function call, arithmetic, etc.) — bail safely
        return None

    return parsed


def _check_shadowed_outcomes(sidecar: Sidecar) -> list[ValidationError]:
    """Detect shadowed, duplicate, or unsatisfiable outcome branches in declaration order."""
    errors: list[ValidationError] = []
    prior: list[tuple[str, str, str, _ParsedConjunction | None]] = []

    for label, spec in sidecar.outcomes.items():
        cond = (spec.condition or "").strip()
        if not cond or "${" in cond:
            continue
        norm = re.sub(r"\s+", " ", cond).lower()
        parsed = _parse_conjunction(cond)

        if parsed is not None and parsed.is_empty:
            errors.append(
                ValidationError(
                    f"outcomes.{label}.condition",
                    f"Outcome {label!r} condition is unsatisfiable/unreachable: {cond!r}",
                )
            )
            continue

        shadowed_by: tuple[str, str] | None = None
        for prev_label, prev_raw, prev_norm, prev_parsed in prior:
            if norm == prev_norm:
                shadowed_by = (prev_label, prev_raw)
                break
            if prev_parsed is not None and prev_parsed.is_tautology:
                shadowed_by = (prev_label, prev_raw)
                break
            if prev_parsed is not None and parsed is not None and prev_parsed.subsumes(parsed):
                shadowed_by = (prev_label, prev_raw)
                break

        if shadowed_by is not None:
            p_label, p_raw = shadowed_by
            errors.append(
                ValidationError(
                    f"outcomes.{label}.condition",
                    f"Outcome {label!r} ({cond!r}) is shadowed by earlier outcome "
                    f"{p_label!r} ({p_raw!r}) and is unreachable",
                )
            )
        prior.append((label, cond, norm, parsed))

    return errors


_EXPECTED_TYPES_DOC = ("int", "float", "str", "bool", "dict", "list")


def _find_nested_location(data: dict[str, Any], target_key: str, prefix: str = "") -> str | None:
    for k, v in data.items():
        path = f"{prefix}.{k}" if prefix else k
        if k == target_key and prefix:
            return path
        if isinstance(v, dict):
            found = _find_nested_location(v, target_key, path)
            if found is not None:
                return found
    return None


def _check_result_schema_recursive(
    schema: dict[str, Any],
    data: dict[str, Any],
    root_data: dict[str, Any],
    prefix: str,
    errors: list[ValidationError],
) -> None:
    for key, expected in schema.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if key not in data:
            nested_hint = ""
            if not prefix:
                nested_at = _find_nested_location(root_data, key)
                if nested_at is not None:
                    nested_hint = f" (found nested at {nested_at!r})"
            errors.append(
                ValidationError(
                    f"result.{dotted}",
                    f"Missing required [result_schema] field {dotted!r}{nested_hint}",
                )
            )
            continue

        val = data[key]
        if isinstance(expected, dict):
            if val is None:
                errors.append(
                    ValidationError(
                        f"result.{dotted}",
                        f"Field {dotted!r} is null (expected nested object)",
                    )
                )
            elif not isinstance(val, dict):
                errors.append(
                    ValidationError(
                        f"result.{dotted}",
                        f"Field {dotted!r} expected object/dict, got {type(val).__name__}",
                    )
                )
            else:
                _check_result_schema_recursive(expected, val, root_data, dotted, errors)
            continue

        if not isinstance(expected, str):
            continue

        if val is None:
            errors.append(
                ValidationError(
                    f"result.{dotted}",
                    f"Field {dotted!r} is null (expected {expected})",
                )
            )
            continue

        if expected == "int":
            if not (isinstance(val, int) and not isinstance(val, bool)):
                errors.append(
                    ValidationError(
                        f"result.{dotted}",
                        f"Field {dotted!r} expected int, got {type(val).__name__}",
                    )
                )
        elif expected == "float":
            if not (isinstance(val, (int, float)) and not isinstance(val, bool)):
                errors.append(
                    ValidationError(
                        f"result.{dotted}",
                        f"Field {dotted!r} expected float, got {type(val).__name__}",
                    )
                )
            elif not math.isfinite(float(val)):
                errors.append(
                    ValidationError(
                        f"result.{dotted}",
                        f"Field {dotted!r} must be a finite number, got {val!r}",
                    )
                )
        elif expected == "str":
            if not isinstance(val, str):
                errors.append(
                    ValidationError(
                        f"result.{dotted}",
                        f"Field {dotted!r} expected str, got {type(val).__name__}",
                    )
                )
        elif expected == "bool":
            if not isinstance(val, bool):
                errors.append(
                    ValidationError(
                        f"result.{dotted}",
                        f"Field {dotted!r} expected bool, got {type(val).__name__}",
                    )
                )
        elif expected == "dict":
            if not isinstance(val, dict):
                errors.append(
                    ValidationError(
                        f"result.{dotted}",
                        f"Field {dotted!r} expected dict, got {type(val).__name__}",
                    )
                )
        elif expected == "list" and not isinstance(val, list):
            errors.append(
                ValidationError(
                    f"result.{dotted}",
                    f"Field {dotted!r} expected list, got {type(val).__name__}",
                )
            )


_DOTTED_REF_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\b")


def validate_result_against_sidecar(
    sidecar: Sidecar,
    result: object,
    sidecar_path: Path | None = None,
) -> tuple[list[ValidationError], str | None]:
    """Validate an emitted result JSON object against `[result_schema]` and `[outcomes]`.

    Returns ``(errors, matched_outcome)``.
    """
    errors: list[ValidationError] = []
    if not isinstance(result, dict):
        err = ValidationError(
            "result",
            f"Emitted result JSON must be an object, got {type(result).__name__}",
        )
        errors.append(err)
        if sidecar_path:
            event("sidecar.validate_error", path=str(sidecar_path), field="result", reason=err.message)
        return errors, None

    if sidecar.result_schema:
        _check_result_schema_recursive(sidecar.result_schema, result, result, "", errors)

    # Check nested dotted keys referenced by conditions exist in `result`
    for label, spec in sidecar.outcomes.items():
        if not spec.condition or "${" in spec.condition:
            continue
        stripped = _strip_sql_string_literals(spec.condition)
        for ref in _DOTTED_REF_RE.findall(stripped):
            parts = ref.split(".")
            cur: Any = result
            missing = False
            for part in parts:
                if not isinstance(cur, dict) or part not in cur:
                    missing = True
                    break
                cur = cur[part]
            if missing:
                errors.append(
                    ValidationError(
                        f"outcomes.{label}.condition",
                        f"Nested key {ref!r} referenced by outcomes.{label} is missing in result JSON",
                    )
                )

    matched_outcome: str | None = None
    eval_errored = False
    if sidecar.outcomes:
        for label, spec in sidecar.outcomes.items():
            if not spec.condition or "${" in spec.condition:
                continue
            try:
                val = _condition_value(spec.condition, result)
            except Exception as e:
                eval_errored = True
                errors.append(
                    ValidationError(
                        f"outcomes.{label}.condition",
                        f"Condition evaluation error against result JSON: {e}",
                    )
                )
                continue
            if val and matched_outcome is None:
                matched_outcome = label

        if matched_outcome is None and not eval_errored and not errors:
            errors.append(
                ValidationError(
                    "outcomes",
                    "No [outcomes] condition matched the provided result JSON (evaluates to 'unknown')",
                )
            )

        if matched_outcome is not None:
            _adv_status, adv_err = evaluate_adversarial_check_detailed(
                sidecar, matched_outcome, result
            )
            if adv_err:
                errors.append(
                    ValidationError(
                        f"outcomes.{matched_outcome}.adversarial_check",
                        f"Adversarial check evaluation error against result JSON: {adv_err}",
                    )
                )

    if sidecar_path:
        for err in errors:
            event(
                "sidecar.validate_error",
                path=str(sidecar_path),
                field=err.field,
                reason=err.message,
            )

    return errors, matched_outcome


@dataclass
class ValidationError:
    field: str
    message: str


@dataclass
class ValidationResult:
    ok: bool
    errors: list[ValidationError] = field(default_factory=list)
    # Advisory findings that never flip `ok`: unknown [experiment] keys (debt #2195) and the
    # spec 261015 §3.2 migration-window findings, each as "<field>: <message>".
    warnings: list[str] = field(default_factory=list)


# Likely misspellings that difflib's similarity cutoff would not catch on its own.
_EXPERIMENT_KEY_ALIASES = {"stage": "stage_name"}


def _unknown_experiment_key_warnings(sidecar: Sidecar) -> list[str]:
    """One warning per unrecognised [experiment] key, with a did-you-mean if one is close."""
    import difflib

    from bathos.sidecar import EXPERIMENT_KEYS

    warnings: list[str] = []
    for key in sidecar.unknown_experiment_keys:
        suggestion = _EXPERIMENT_KEY_ALIASES.get(key)
        if suggestion is None:
            close = difflib.get_close_matches(key, sorted(EXPERIMENT_KEYS), n=1, cutoff=0.6)
            suggestion = close[0] if close else None
        msg = f"experiment.{key}: unknown key in [experiment] -- it is ignored"
        if suggestion:
            msg += f" (did you mean '{suggestion}'?)"
        warnings.append(msg)
    return warnings


def validate_popper_block(
    sidecar: Sidecar,
    sidecar_path: Path | None = None,
) -> list[ValidationError]:
    """Validate [popper] block fields per the spec (Section 2.3 of #792 spec).

    Returns a list of ValidationError (empty if valid).
    WARNING-level entries use message prefixed with "WARNING:".

    Emits a ``sidecar.validate_error`` event per error when ``sidecar_path`` is
    given, matching validate_reproduction_block / _controls_block /
    _differential_block. WARNING-level entries emit nothing — they are advisory,
    and labelling them ``validate_error`` would misreport them.
    """
    errors: list[ValidationError] = []

    if sidecar.popper_null_pass_rate is None:
        return errors  # No [popper] block — nothing to validate

    # [popper] is only valid on experiment sidecars
    if sidecar.kind != SidecarKind.EXPERIMENT:
        errors.append(
            ValidationError(
                "popper",
                "[popper] block is only valid in [experiment] sidecars",
            )
        )
        if sidecar_path:
            event(
                "sidecar.validate_error",
                path=str(sidecar_path),
                field="popper",
                reason="[popper] block is only valid in [experiment] sidecars",
            )
        return errors  # No point validating further

    null = sidecar.popper_null_pass_rate
    alt = sidecar.popper_alt_pass_rate
    threshold = sidecar.popper_stopping_threshold

    # null_pass_rate range
    if null is None or not (0 < null < 1):
        errors.append(
            ValidationError(
                "popper.null_pass_rate",
                f"null_pass_rate must be in (0, 1), got {null!r}",
            )
        )
        if sidecar_path:
            event(
                "sidecar.validate_error",
                path=str(sidecar_path),
                field="popper.null_pass_rate",
                reason=f"null_pass_rate out of range: {null!r}",
            )

    # alt_pass_rate range
    if alt is None or not (0 < alt < 1):
        errors.append(
            ValidationError(
                "popper.alt_pass_rate",
                f"alt_pass_rate must be in (0, 1), got {alt!r}",
            )
        )
        if sidecar_path:
            event(
                "sidecar.validate_error",
                path=str(sidecar_path),
                field="popper.alt_pass_rate",
                reason=f"alt_pass_rate out of range: {alt!r}",
            )

    # null != alt (no test power if equal)
    if null is not None and alt is not None and null == alt:
        errors.append(
            ValidationError(
                "popper",
                "null_pass_rate and alt_pass_rate are identical; no test power",
            )
        )
        if sidecar_path:
            event(
                "sidecar.validate_error",
                path=str(sidecar_path),
                field="popper",
                reason="null_pass_rate and alt_pass_rate are identical; no test power",
            )

    # stopping_threshold >= 1.0
    if threshold is None or threshold < 1.0:
        errors.append(
            ValidationError(
                "popper.stopping_threshold",
                f"stopping_threshold must be >= 1.0, got {threshold!r}",
            )
        )
        if sidecar_path:
            event(
                "sidecar.validate_error",
                path=str(sidecar_path),
                field="popper.stopping_threshold",
                reason=f"stopping_threshold below 1.0: {threshold!r}",
            )
    elif threshold < 10.0:
        # Advisory, not an error — deliberately emits no sidecar.validate_error event.
        errors.append(
            ValidationError(
                "popper.stopping_threshold",
                "WARNING: stopping_threshold < 10.0 — consider a stricter threshold (alpha < 0.10)",
            )
        )

    # Validate [popper.weights] if present
    declared_labels = set(sidecar.outcomes.keys())
    for key, val in sidecar.popper_weights.items():
        if key not in declared_labels and key not in ("error", "unknown", "marginal"):
            errors.append(
                ValidationError(
                    f"popper.weights.{key}",
                    f"Unknown outcome label {key!r} in [popper.weights] — not declared in [outcomes]",
                )
            )
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field=f"popper.weights.{key}",
                    reason=f"Unknown outcome label {key!r} not declared in [outcomes]",
                )
        if val <= 0:
            errors.append(
                ValidationError(
                    f"popper.weights.{key}",
                    f"Weight for {key!r} must be > 0, got {val!r}",
                )
            )
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field=f"popper.weights.{key}",
                    reason=f"Weight for {key!r} must be > 0, got {val!r}",
                )
        if key == "error" and val != 1.0:
            errors.append(
                ValidationError(
                    "popper.weights.error",
                    f"Weight for 'error' must be exactly 1.0 (non-overridable), got {val!r}",
                )
            )
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field="popper.weights.error",
                    reason=f"Weight for 'error' must be exactly 1.0, got {val!r}",
                )

    return errors


def validate_reproduction_block(
    sidecar: Sidecar, sidecar_path: Path | None = None
) -> list[ValidationError]:
    """Validate [reproduction] block fields.

    Returns a list of ValidationError (empty if valid or no block present).
    """
    import re

    errors: list[ValidationError] = []

    if sidecar.reproduction is None:
        return errors  # No [reproduction] block — nothing to validate

    repro = sidecar.reproduction

    # tolerance_pct: if set, must be 0.0 <= v <= 100.0
    if repro.tolerance_pct is not None and not (0.0 <= repro.tolerance_pct <= 100.0):
        errors.append(
            ValidationError(
                "reproduction.tolerance_pct",
                f"tolerance_pct must be in [0.0, 100.0], got {repro.tolerance_pct!r}",
            )
        )
        if sidecar_path:
            event(
                "sidecar.validate_error",
                path=str(sidecar_path),
                field="reproduction.tolerance_pct",
                reason=f"tolerance_pct out of range: {repro.tolerance_pct}",
            )

    # reproduces_run: if non-empty, must match UUID regex r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    if repro.reproduces_run:
        uuid_pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        if not re.match(uuid_pattern, repro.reproduces_run):
            errors.append(
                ValidationError(
                    "reproduction.reproduces_run",
                    f"reproduces_run must be a valid UUID, got {repro.reproduces_run!r}",
                )
            )
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field="reproduction.reproduces_run",
                    reason=f"Invalid UUID format: {repro.reproduces_run}",
                )

    return errors


def validate_controls_block(
    sidecar: Sidecar, sidecar_path: Path | None = None
) -> list[ValidationError]:
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
            errors.append(
                ValidationError(
                    "CONTROLS_LABEL_NOT_FOUND",
                    f"Label {label!r} not declared in [outcomes]",
                )
            )
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field="CONTROLS_LABEL_NOT_FOUND",
                    reason=f"Label {label!r} not found in outcomes",
                )

    # Check negative_outcome labels exist in outcomes
    for label in controls.negative_outcome:
        if label not in outcome_keys:
            errors.append(
                ValidationError(
                    "CONTROLS_LABEL_NOT_FOUND",
                    f"Label {label!r} not declared in [outcomes]",
                )
            )
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field="CONTROLS_LABEL_NOT_FOUND",
                    reason=f"Label {label!r} not found in outcomes",
                )

    return errors


def validate_differential_block(
    sidecar: Sidecar, sidecar_path: Path | None = None
) -> list[ValidationError]:
    """Validate [differential] block fields (debt #1071).

    Returns a list of ValidationError (empty if valid or no block present).
    """
    errors: list[ValidationError] = []

    if sidecar.differential is None:
        return errors  # No [differential] block — nothing to validate

    diff = sidecar.differential

    if not diff.knob:
        errors.append(
            ValidationError(
                "differential.knob",
                "knob must be non-empty",
            )
        )
        if sidecar_path:
            event(
                "sidecar.validate_error",
                path=str(sidecar_path),
                field="differential.knob",
                reason="knob is empty",
            )

    if diff.off == diff.on:
        errors.append(
            ValidationError(
                "differential",
                f"off and on must differ, both are {diff.off!r} — no invariant to check",
            )
        )
        if sidecar_path:
            event(
                "sidecar.validate_error",
                path=str(sidecar_path),
                field="differential",
                reason=f"off == on == {diff.off!r}",
            )

    if diff.expect not in ("differs", "identical"):
        errors.append(
            ValidationError(
                "differential.expect",
                f"expect must be 'differs' or 'identical', got {diff.expect!r}",
            )
        )
        if sidecar_path:
            event(
                "sidecar.validate_error",
                path=str(sidecar_path),
                field="differential.expect",
                reason=f"invalid expect {diff.expect!r}",
            )

    if diff.metric:
        if diff.metric not in sidecar.result_schema:
            errors.append(
                ValidationError(
                    "differential.metric",
                    f"metric {diff.metric!r} not declared in [result_schema]",
                )
            )
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field="differential.metric",
                    reason=f"{diff.metric!r} not in result_schema",
                )
        elif sidecar.result_schema.get(diff.metric) in ("int", "float") and diff.min_effect is None:
            errors.append(
                ValidationError(
                    "differential.min_effect",
                    f"min_effect is required when metric {diff.metric!r} is numeric",
                )
            )
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field="differential.min_effect",
                    reason="min_effect missing for numeric metric",
                )
        if diff.min_effect is not None and diff.min_effect < 0:
            errors.append(
                ValidationError(
                    "differential.min_effect",
                    f"min_effect must be >= 0, got {diff.min_effect!r}",
                )
            )
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field="differential.min_effect",
                    reason=f"negative min_effect {diff.min_effect!r}",
                )
    elif diff.min_effect is not None:
        # Whole-dict-diff mode: an effect-size threshold isn't well-defined over an
        # arbitrary dict, so requiring min_effect without a metric is a validation error
        # rather than a silently-ignored field.
        errors.append(
            ValidationError(
                "differential.min_effect",
                "min_effect requires metric to be set (whole-dict comparison has no effect size)",
            )
        )
        if sidecar_path:
            event(
                "sidecar.validate_error",
                path=str(sidecar_path),
                field="differential.min_effect",
                reason="min_effect set without metric",
            )

    return errors


def validate_claim_discriminability(
    sidecar: Sidecar, claim, sidecar_path: Path | None = None
) -> list[ValidationError]:
    """Cross-check sidecar claim_discriminates/claim_isolates against a registered claim. (#3719)

    Only meaningful for EXPERIMENT-kind sidecars (the fields are experiment-only). `claim` is
    typed loosely (avoid a validate.py -> claim.py import cycle; callers pass a `ClaimFile` from
    `bathos.claim.load_registered_claim`) and duck-typed against its `.hypotheses`/`.confounds`
    attributes, each a list of dicts with an `id` key.

    Catches exactly the #3717 authoring mistake at validate time instead of at conclude time:
    the sidecar's claim_discriminates was set to outcome labels instead of hypothesis ids, and
    `bth validate-sidecar` had no way to notice because it never saw the claim at all.
    """
    errors: list[ValidationError] = []
    if sidecar.kind != SidecarKind.EXPERIMENT:
        return errors

    valid_hypothesis_ids = {h.get("id") for h in claim.hypotheses}
    valid_confound_ids = {c.get("id") for c in claim.confounds}

    for disc_id in sidecar.claim_discriminates:
        if disc_id not in valid_hypothesis_ids:
            err = ValidationError(
                "claim_discriminates",
                f"{disc_id!r} is not a hypothesis id in the registered claim "
                f"(valid: {sorted(i for i in valid_hypothesis_ids if i)})",
            )
            errors.append(err)
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field="claim_discriminates",
                    reason=f"unknown hypothesis id {disc_id!r}",
                )

    for iso_id in sidecar.claim_isolates:
        if iso_id not in valid_confound_ids:
            err = ValidationError(
                "claim_isolates",
                f"{iso_id!r} is not a confound id in the registered claim "
                f"(valid: {sorted(i for i in valid_confound_ids if i)})",
            )
            errors.append(err)
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field="claim_isolates",
                    reason=f"unknown confound id {iso_id!r}",
                )

    return errors


def _bound_in_condition_literals(bound: float, condition: str) -> bool:
    """Does `bound` appear as a numeric literal in a SQL condition?"""
    for lit in re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|(?<![\w.])\.\d+", condition):
        try:
            if math.isclose(float(lit), abs(bound), rel_tol=1e-9, abs_tol=0.0):
                return True
        except ValueError:
            continue
    return False


def validate_breakdown_block(sidecar: Sidecar) -> tuple[list[ValidationError], list[str]]:
    """Validate `[breakdown]` and `[result_schema_meta]` (spec 261026 D1/D2).

    Returns (errors, warnings). Warnings are the advisories (uncovered aggregates, a `bound`
    that no outcome condition uses); neither the absence of `[breakdown]` nor an uncovered
    aggregate ever errors here -- the claim-bound gates are what refuse.
    """
    errors: list[ValidationError] = []
    warnings: list[str] = []
    schema = {k: v for k, v in sidecar.result_schema.items() if isinstance(v, str)}

    for col, meta in sidecar.result_schema_meta.items():
        where = f"result_schema_meta.{col}"
        if col not in schema:
            errors.append(ValidationError(where, f"{col!r} is not a [result_schema] column"))
        for key, value in meta.items():
            if key != "aggregate":
                errors.append(
                    ValidationError(where, f"unknown key {key!r}; only 'aggregate' is allowed")
                )
            elif not isinstance(value, bool):
                errors.append(ValidationError(f"{where}.aggregate", "must be a boolean"))

    block = sidecar.breakdown
    if block is not None:
        expanded, unmatched = expand_breakdown_aggregates(sidecar)
        for key in block.stray_aggregate_keys:
            col, _, stray = key.partition(".")
            hint = (
                "; `waive` belongs directly under [breakdown], before any sub-table"
                if stray == "waive"
                else ""
            )
            errors.append(
                ValidationError(f"breakdown.aggregates.{col}", f"unknown key {stray!r}{hint}")
            )
        if block.waive is not None and not block.waive:
            errors.append(ValidationError("breakdown.waive", "waive reason must not be blank"))
        if not block.aggregates and not block.waive:
            errors.append(
                ValidationError(
                    "breakdown",
                    "declare [breakdown.aggregates.<column>] or a `waive` reason",
                )
            )
        if block.aggregates and not block.unit:
            errors.append(
                ValidationError("breakdown.unit", "required when aggregates are declared")
            )
        for key in unmatched:
            errors.append(
                ValidationError(
                    f"breakdown.aggregates.{key}",
                    "prefix matches no float [result_schema] column without '__'",
                )
            )
        for key, agg in block.aggregates.items():
            where = f"breakdown.aggregates.{key}"
            if "__" in key:
                errors.append(
                    ValidationError(where, "aggregate key must not contain '__' (summary columns)")
                )
            elif not key.endswith("*") and key not in schema:
                errors.append(ValidationError(where, f"{key!r} is not in [result_schema]"))
            if agg.direction not in BREAKDOWN_DIRECTIONS:
                errors.append(
                    ValidationError(
                        f"{where}.direction", f"must be one of {sorted(BREAKDOWN_DIRECTIONS)}"
                    )
                )
            if agg.margin is not None and agg.bound is None:
                errors.append(ValidationError(f"{where}.margin", "requires a `bound`"))
            if agg.bound == 0 and agg.margin is None:
                errors.append(
                    ValidationError(
                        where,
                        "bound = 0 needs an explicit `margin` (the default band is 10% of the bound)",
                    )
                )
        for col in breakdown_summary_columns(sidecar):
            if col not in schema:
                errors.append(
                    ValidationError(
                        "result_schema", f"breakdown summary column {col!r} is not declared"
                    )
                )
        for col, agg in expanded.items():
            if agg.bound is None:
                continue
            referencing = [
                s.condition
                for s in sidecar.outcomes.values()
                if s.condition and columns_referenced(s.condition, [col])
            ]
            if referencing and not any(
                _bound_in_condition_literals(agg.bound, c) for c in referencing
            ):
                warnings.append(
                    f"breakdown.aggregates.{col}: bound {agg.bound:g} does not appear as a literal "
                    "in any outcome condition referencing it"
                )

    for u in uncovered_aggregates(sidecar):
        warnings.append(f"breakdown.aggregates.{u.column}: {uncovered_aggregate_message(u)}")
    return errors, warnings



def _apply_stoichiometry_and_reagents(
    sidecar: Sidecar,
    sidecar_path: Path | None,
    errors: list[ValidationError],
    warnings: list[str],
) -> None:
    """Validate `[stoichiometry]` / `[reagents.*]` and honor BTH_STOICHIOMETRY_WARN_ONLY."""
    if sidecar.stoichiometry is None and not sidecar.reagents:
        return
    from bathos.config import env_override
    from bathos.stoichiometry import validate_reagents_block, validate_stoichiometry_block

    stoich_errors, stoich_warnings = validate_stoichiometry_block(sidecar)
    reagent_errors, reagent_warnings = validate_reagents_block(sidecar)
    combined_errors = [*stoich_errors, *reagent_errors]
    warnings.extend(stoich_warnings)
    warnings.extend(reagent_warnings)

    if combined_errors and env_override("BTH_STOICHIOMETRY_WARN_ONLY") is True:
        for err in combined_errors:
            warnings.append(f"[stoichiometry warn-only] {err.field}: {err.message}")
        event(
            "stoichiometry.warn_only_downgrade",
            path=str(sidecar_path) if sidecar_path else "",
            error_count=len(combined_errors),
            fields=[err.field for err in combined_errors],
        )
        return

    for err in combined_errors:
        errors.append(err)
        if sidecar_path:
            event(
                "sidecar.validate_error",
                path=str(sidecar_path),
                field=err.field,
                reason=err.message,
            )

def validate_sidecar(
    sidecar: Sidecar,
    sidecar_path: Path | None = None,
    claim=None,
    *,
    stem_has_prior_runs: bool = False,
    result: dict[str, Any] | Path | str | None = None,
) -> ValidationResult:
    """Validate a parsed Sidecar for structural integrity and logical consistency.

    Checks:
    - Outcomes section exists
    - Each outcome branch has condition, decision, and reasoning
    - DuckDB SQL conditions parse correctly
    - At least one result_schema field is referenced in conditions
    - At least one outcome branch has is_residual=true (catch-all fallback)
    - If `claim` is given (a `bathos.claim.ClaimFile`): claim_discriminates/claim_isolates
      resolve to real hypothesis/confound ids in it (#3719). Omitting `claim` skips this check
      entirely — a sidecar authored before a campaign/claim exists must still validate.

    Returns ValidationResult with ok=True and errors=[] if valid, or ok=False with a list of errors.
    """
    errors = []
    warnings = _unknown_experiment_key_warnings(sidecar)

    # ── [charter] purpose label (spec 261104 T7, owner 261109) ───────────────────
    # Shape only: a readable slug or a list of slugs. Whether each slug names a confirmed charter purpose is checked
    # against the charter, not here. A malformed label is an error because a scorer must never guess at it.
    if sidecar_path is not None:
        from bathos.bearings.purpose_label import PurposeLabelError, read_purpose_label

        try:
            read_purpose_label(tomllib.loads(Path(sidecar_path).read_text(encoding="utf-8")))
        except PurposeLabelError as e:
            errors.append(ValidationError("charter.purpose", str(e)))
        except (OSError, tomllib.TOMLDecodeError):
            pass  # the sidecar parser already reports an unreadable file

    # ── [review] block structural validation (build-order step 2) ────────────────
    # Advisory tier: this validates SHAPE only. Whether the review is adequate is the Review
    # Coverage Gate's job at conclude, not this function's.
    if sidecar.review is not None:
        from bathos.sidecar import IMPLEMENTATION_DISPOSITIONS, LITERATURE_DISPOSITIONS

        for i, entry in enumerate(sidecar.review.literature):
            where = f"review.literature[{i}]"
            if not entry.ref:
                errors.append(
                    ValidationError(
                        where, "literature entry needs a 'ref' (DOI, arXiv id, or run UUID)"
                    )
                )
            if not entry.claim:
                errors.append(
                    ValidationError(
                        where, "literature entry needs a 'claim' stating what the reference reports"
                    )
                )
            # disposition is optional, but a WRONG one is an error: obligation trigger (4)
            # keys on disposition == 'supports', so a typo would make the citation silently
            # incapable of ever being contradicted.
            if entry.disposition and entry.disposition not in LITERATURE_DISPOSITIONS:
                errors.append(
                    ValidationError(
                        where,
                        f"disposition {entry.disposition!r} is not one of {sorted(LITERATURE_DISPOSITIONS)}",
                    )
                )

        for i, entry in enumerate(sidecar.review.implementation):
            where = f"review.implementation[{i}]"
            if not entry.source:
                errors.append(
                    ValidationError(where, "implementation entry needs a 'source' (path or URL)")
                )
            if not entry.what_was_checked:
                errors.append(
                    ValidationError(where, "implementation entry needs 'what_was_checked'")
                )
            if entry.disposition and entry.disposition not in IMPLEMENTATION_DISPOSITIONS:
                errors.append(
                    ValidationError(
                        where,
                        f"disposition {entry.disposition!r} is not one of {sorted(IMPLEMENTATION_DISPOSITIONS)}",
                    )
                )

        # bears_on resolution — only when a claim is supplied. Per decision D2, bears_on is
        # OPTIONAL until confirmatory: a sidecar authored before any claim exists must still
        # validate. This mirrors how claim_discriminates is treated below; the gate at
        # conclude is what requires coverage.
        if claim is not None:
            known = {
                h.get("id", "") for h in getattr(claim, "hypotheses", []) if isinstance(h, dict)
            }
            known |= {
                c.get("id", "") for c in getattr(claim, "confounds", []) if isinstance(c, dict)
            }
            known.discard("")
            for kind, entries in (
                ("literature", sidecar.review.literature),
                ("implementation", sidecar.review.implementation),
            ):
                for i, entry in enumerate(entries):
                    if entry.bears_on and entry.bears_on not in known:
                        errors.append(
                            ValidationError(
                                f"review.{kind}[{i}]",
                                f"bears_on {entry.bears_on!r} is not a hypothesis or confound id in the claim",
                            )
                        )

    # ── epistemic class (spec §3.1, §3.2, §3.5) ──────────────────────────────────
    # Only experiment sidecars carry a class; every other kind resolves to "legacy" and keeps
    # today's rules untouched. `warnings` (initialised above, plain strings) is non-blocking and
    # never changes `ok`.
    cls = None
    if sidecar.kind == SidecarKind.EXPERIMENT:
        cls = resolve_epistemic_class(sidecar, stem_has_prior_runs=stem_has_prior_runs)
        if cls is None:
            raw = sidecar.stage_name_raw
            errors.append(
                ValidationError(
                    "stage_name",
                    f"stage_name {raw!r} requires an [observe] block (exploration) / "
                    "[calibrate] block (calibration); add it, or choose a confirmatory stage "
                    "(validation/ablation/production)",
                )
            )
            _apply_stoichiometry_and_reagents(sidecar, sidecar_path, errors, warnings)
            return ValidationResult(ok=False, errors=errors, warnings=warnings)

    # Source placeholder guard applies to every class: an unfilled template is never a source.
    for label, spec in sidecar.outcomes.items():
        if "${" in (spec.source or ""):
            errors.append(
                ValidationError(
                    f"outcomes.{label}.source",
                    f"unfilled template placeholder in source {spec.source!r}",
                )
            )

    # Exploratory and calibrating sidecars may omit [outcomes]; confirmatory/legacy may not.
    outcomes_optional = cls in ("exploratory", "calibrating")

    if cls == "exploratory":
        if not (sidecar.question or sidecar.hypothesis):
            errors.append(
                ValidationError(
                    "question",
                    "exploratory sidecar needs a 'question' (or 'hypothesis') stating what you are looking at",
                )
            )
        if sidecar.observe is None or not sidecar.observe.outputs:
            errors.append(
                ValidationError(
                    "observe.outputs",
                    "exploratory sidecar needs a non-empty [observe].outputs list",
                )
            )
        else:
            schema_fields = set(sidecar.result_schema.keys())
            for expr in sidecar.observe.sanity:
                for msg in check_sanity_expr(expr, schema_fields):
                    errors.append(ValidationError("observe.sanity", f"{expr!r}: {msg}"))
        # Held lightly (§3.2.1): every non-residual branch needs a source. A missing or malformed
        # source is an error for a new stem and a warning for a stem that already has runs.
        for label, spec in sidecar.outcomes.items():
            if spec.is_residual or "${" in (spec.source or ""):
                continue
            if not spec.source:
                problem = "missing 'source'"
            elif parse_source(spec.source) is None:
                problem = f"malformed source {spec.source!r}"
            else:
                continue
            source_error = ValidationError(
                f"outcomes.{label}.source",
                f"branch {label!r}: {problem}; use one of lit:<ref> | run:<id>:<field> | "
                "sesoi:<value>:<why> | provisional:<why> | card:<id>",
            )
            if stem_has_prior_runs:
                # Migration window: a stem with prior runs gets a warning, not an error.
                warnings.append(f"{source_error.field}: {source_error.message}")
            else:
                errors.append(source_error)
    elif cls == "calibrating":
        if sidecar.calibrate is None or not sidecar.calibrate.quantity.strip():
            errors.append(
                ValidationError(
                    "calibrate.quantity",
                    "calibration sidecar needs a non-empty [calibrate].quantity",
                )
            )
        if not sidecar.result_schema:
            errors.append(
                ValidationError(
                    "result_schema",
                    "calibration sidecar needs a non-empty [result_schema] containing the calibrated field",
                )
            )

    # Must have outcomes (confirmatory/legacy always; exploratory/calibrating only when present)
    if not sidecar.outcomes and not outcomes_optional:
        errors.append(ValidationError("outcomes", "No [outcomes] section found"))
        if sidecar_path:
            event(
                "sidecar.validate_error",
                path=str(sidecar_path),
                field="outcomes",
                reason="No [outcomes] section found",
            )
        _apply_stoichiometry_and_reagents(sidecar, sidecar_path, errors, warnings)
        return ValidationResult(ok=False, errors=errors, warnings=warnings)

    # Each branch must have condition, decision, reasoning
    for label, spec in sidecar.outcomes.items():
        if label in RESERVED_OUTCOME_LABELS:
            err = ValidationError(
                f"outcomes.{label}",
                f"{label!r} is a reserved outcome label (bathos sets it automatically) — "
                "choose a different outcome name",
            )
            errors.append(err)
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field=f"outcomes.{label}",
                    reason=f"{label!r} is reserved",
                )
        # An unfilled template placeholder in a condition or adversarial_check is never valid, for
        # any class. An unfilled adversarial_check would otherwise silently disable the check.
        # The "${" in the message is load-bearing: scaffold code uses it to classify fill-in items.
        for placeholder_field, text in (
            ("condition", spec.condition),
            ("adversarial_check", spec.adversarial_check),
        ):
            if text and "${" in text:
                errors.append(
                    ValidationError(
                        f"outcomes.{label}.{placeholder_field}",
                        f"unfilled placeholder in {placeholder_field} {text!r}: "
                        "template '${...}' must be filled in before the sidecar is valid",
                    )
                )
        if not spec.condition:
            err = ValidationError(f"outcomes.{label}", "Missing 'condition' field")
            errors.append(err)
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field=f"outcomes.{label}",
                    reason="Missing 'condition' field",
                )
        if not spec.decision:
            err = ValidationError(f"outcomes.{label}", "Missing 'decision' field")
            errors.append(err)
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field=f"outcomes.{label}",
                    reason="Missing 'decision' field",
                )
        if not spec.reasoning:
            err = ValidationError(
                f"outcomes.{label}",
                "Missing 'reasoning' field",
            )
            errors.append(err)
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field=f"outcomes.{label}",
                    reason="Missing 'reasoning' field",
                )

        # SQL `= NULL` / `!= NULL` / `<> NULL` is syntactically valid in DuckDB but always
        # evaluates to NULL (never matches). Flag it explicitly before DuckDB parses it.
        for placeholder_field, text in (
            ("condition", spec.condition),
            ("adversarial_check", spec.adversarial_check),
        ):
            if text and "${" not in text:
                for null_op in _find_null_equality_ops(text):
                    err = ValidationError(
                        f"outcomes.{label}.{placeholder_field}",
                        f"Invalid SQL NULL comparison ({null_op!r}) in {placeholder_field} "
                        f"{text!r} — use 'IS NULL' or 'IS NOT NULL' instead",
                    )
                    errors.append(err)
                    if sidecar_path:
                        event(
                            "sidecar.validate_error",
                            path=str(sidecar_path),
                            field=f"outcomes.{label}.{placeholder_field}",
                            reason=err.message,
                        )

        # DuckDB SQL must parse. A placeholder condition is reported above; it is not parsed here.
        if spec.condition and "${" not in spec.condition:
            try:
                con = duckdb.connect()
                # Build a dummy table from result_schema to validate SQL (including nested STRUCTs)
                if sidecar.result_schema:
                    col_defs: list[str] = []
                    for k, v in sidecar.result_schema.items():
                        sql_t = _schema_type_to_sql(v)
                        if sql_t is not None:
                            col_defs.append(f"{_quote_sql_ident(k)} {sql_t}")
                    cols = ", ".join(col_defs)
                    if cols:
                        con.execute(f"CREATE TEMP TABLE _dummy ({cols})")
                        con.execute(f"SELECT ({spec.condition}) FROM _dummy LIMIT 0")
                    else:
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
                    event(
                        "sidecar.validate_error",
                        path=str(sidecar_path),
                        field=f"outcomes.{label}.condition",
                        reason=f"DuckDB parse error: {e}",
                    )

    # Catch shadowed / duplicate / unsatisfiable outcome branches
    if sidecar.outcomes:
        shadow_errors = _check_shadowed_outcomes(sidecar)
        errors.extend(shadow_errors)
        if sidecar_path:
            for err in shadow_errors:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field=err.field,
                    reason=err.message,
                )

    # At least one result_schema field must appear in conditions
    if sidecar.result_schema and sidecar.outcomes:
        schema_keys = _collect_schema_field_names(sidecar.result_schema)
        all_conditions = " ".join(s.condition for s in sidecar.outcomes.values() if s.condition)
        if not any(key in all_conditions for key in schema_keys):
            err = ValidationError(
                "result_schema",
                "No result_schema fields referenced in any outcome condition",
            )
            errors.append(err)
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field="result_schema",
                    reason="No result_schema fields referenced in any outcome condition",
                )

    # Must have at least one is_residual=true fallback branch
    has_residual = any(s.is_residual for s in sidecar.outcomes.values())
    if sidecar.outcomes and not has_residual:
        err = ValidationError(
            "outcomes",
            "No fallback branch with is_residual=true — add a catch-all outcome",
        )
        errors.append(err)
        if sidecar_path:
            event(
                "sidecar.validate_error",
                path=str(sidecar_path),
                field="outcomes",
                reason="No fallback branch with is_residual=true",
            )

    # Validate [popper] block if present
    popper_errors = validate_popper_block(sidecar, sidecar_path)
    errors.extend(popper_errors)

    # Validate [reproduction] block if present
    repro_errors = validate_reproduction_block(sidecar, sidecar_path)
    errors.extend(repro_errors)

    # Validate [controls] block if present
    controls_errors = validate_controls_block(sidecar, sidecar_path)
    errors.extend(controls_errors)

    # Validate [differential] block if present (debt #1071)
    differential_errors = validate_differential_block(sidecar, sidecar_path)
    errors.extend(differential_errors)

    # Cross-check claim_discriminates/claim_isolates if a registered claim was supplied (#3719)
    if claim is not None:
        errors.extend(validate_claim_discriminability(sidecar, claim, sidecar_path))

    # Validate [breakdown] / [result_schema_meta] and surface the aggregate advisories (spec 261026)
    breakdown_errors, breakdown_warnings = validate_breakdown_block(sidecar)
    errors.extend(breakdown_errors)
    warnings.extend(breakdown_warnings)

    _apply_stoichiometry_and_reagents(sidecar, sidecar_path, errors, warnings)

    if result is not None:
        loaded_result: Any = result
        if isinstance(result, (Path, str)):
            r_path = Path(result)
            if not r_path.is_file():
                errors.append(ValidationError("result", f"Result JSON file not found: {result}"))
                loaded_result = None
            else:
                try:
                    loaded_result = json.loads(r_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    errors.append(
                        ValidationError("result", f"Invalid result JSON in {result}: {exc}")
                    )
                    loaded_result = None
        if loaded_result is not None:
            res_errors, _matched = validate_result_against_sidecar(
                sidecar, loaded_result, sidecar_path=sidecar_path
            )
            errors.extend(res_errors)

    return ValidationResult(ok=len(errors) == 0, errors=errors, warnings=warnings)
