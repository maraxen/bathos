from __future__ import annotations

import itertools
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
    CONTINUOUS_POPPER_METHODS,
    FORBIDDEN_HOLDOUT_KINDS,
    FORBIDDEN_SPLIT_UNITS,
    HOLDOUT_KINDS,
    HOLDOUT_SPLIT_UNIT_RANKS,
    RESERVED_OUTCOME_LABELS,
    InvariantSpec,
    Sidecar,
    SidecarError,
    SidecarKind,
    _condition_value,
    breakdown_summary_columns,
    check_sanity_expr,
    columns_referenced,
    evaluate_adversarial_check_detailed,
    evaluate_invariants,
    evaluate_outcome,
    expand_breakdown_aggregates,
    holdout_rank,
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


_NUM_LITERAL_RE = re.compile(
    r"(?<![A-Za-z0-9_.])([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(?![A-Za-z0-9_.])"
)
_MAX_PARTITION_SAMPLES = 128


def _flatten_schema_types(schema: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Return dotted_field -> type_str for scalar leaf fields in `[result_schema]`."""
    out: dict[str, str] = {}
    for key, val in schema.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(val, str):
            out[dotted] = val
        elif isinstance(val, dict):
            out.update(_flatten_schema_types(val, dotted))
    return out


def _default_dummy_for_type(field_type: str) -> Any:
    return {
        "int": 0,
        "float": 0.0,
        "bool": False,
        "str": "",
        "dict": {},
        "list": [],
    }.get(field_type, "")


def _assign_dotted(target: dict[str, Any], dotted_key: str, value: Any) -> None:
    parts = dotted_key.split(".")
    cur = target
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _extract_condition_literals(
    conditions: list[str], schema_types: dict[str, str]
) -> tuple[dict[str, set[float]], set[float], set[str], set[str]]:
    """Extract per-field numeric thresholds, global numeric literals, string literals, and IS-NULL fields."""
    per_field_nums: dict[str, set[float]] = {k: set() for k in schema_types}
    global_nums: set[float] = set()
    str_literals: set[str] = set()
    null_checked_fields: set[str] = set()

    for cond in conditions:
        for m_str in _SQL_STRING_RE.finditer(cond):
            raw = m_str.group(0)[1:-1].replace("''", "'")
            str_literals.add(raw)
        stripped = _strip_sql_string_literals(cond)

        for col_name in schema_types:
            if re.search(
                rf"(?<![A-Za-z0-9_.]){re.escape(col_name)}\s+IS\s+(?:NOT\s+)?NULL\b",
                stripped,
                re.IGNORECASE,
            ):
                null_checked_fields.add(col_name)

        # Associate explicit field-comparison thresholds with that field
        for pat, field_idx, num_idx in ((_CMP_LEFT_RE, 1, 3), (_CMP_RIGHT_RE, 3, 1)):
            for part in _split_top_level_and(stripped) or [stripped]:
                m = pat.match(_unwrap_parens(part))
                if m and m.group(field_idx) in per_field_nums:
                    try:
                        val = float(m.group(num_idx))
                        if math.isfinite(val):
                            per_field_nums[m.group(field_idx)].add(val)
                    except ValueError:
                        pass

        for m_num in _NUM_LITERAL_RE.finditer(stripped):
            try:
                val = float(m_num.group(1))
                if math.isfinite(val):
                    global_nums.add(val)
            except ValueError:
                pass

    return per_field_nums, global_nums, str_literals, null_checked_fields


def _boundary_candidates_for_field(
    field_type: str,
    field_nums: set[float],
    global_nums: set[float],
    str_literals: set[str],
    include_null: bool,
) -> list[Any]:
    """Build compact synthetic boundary-case values for a single schema field."""
    vals: list[Any] = []
    if field_type == "bool":
        vals = [False, True]
    elif field_type == "str":
        seen_s = list(dict.fromkeys([*sorted(str_literals)[:5], "", "__unseen__"]))
        vals = seen_s[:7]
    elif field_type == "int":
        nums = field_nums or global_nums
        cands: set[int] = {-1, 0, 1}
        for c in sorted(nums)[:4]:
            cands.update({math.floor(c) - 1, math.floor(c), math.ceil(c), math.ceil(c) + 1})
        sorted_c = sorted(cands)
        for a, b in zip(sorted_c, sorted_c[1:]):
            if b - a > 1:
                cands.add((a + b) // 2)
        vals = sorted(cands)[:11]
    elif field_type == "float":
        nums = field_nums or global_nums
        cands_f: set[float] = {-1.0, 0.0, 1.0}
        for c in sorted(nums)[:4]:
            eps = max(1e-6, abs(c) * 1e-6)
            cands_f.update({c - eps, float(c), c + eps})
        sorted_f = sorted(cands_f)
        for a, b in zip(sorted_f, sorted_f[1:]):
            if b - a > 1e-3:
                cands_f.add((a + b) / 2.0)
        vals = sorted(cands_f)[:11]
    else:
        vals = [_default_dummy_for_type(field_type)]

    if include_null and None not in vals:
        return [*vals, None]
    return vals


def check_outcome_partition_gaps(sidecar: Sidecar) -> list[str]:
    """Probe `[outcomes]` against synthetic boundary inputs to detect partition gaps (#2224).

    Returns a list of warning strings when any synthetic boundary input evaluates to
    ``'unknown'`` (i.e. matches no declared outcome condition).
    """
    if not sidecar.outcomes or not sidecar.result_schema:
        return []

    conditions = [
        spec.condition.strip()
        for spec in sidecar.outcomes.values()
        if spec.condition and spec.condition.strip()
    ]
    if not conditions or any("${" in c for c in conditions):
        return []

    schema_types = _flatten_schema_types(sidecar.result_schema)
    if not schema_types:
        return []

    schema_keys = set(schema_types)
    referenced: list[str] = []
    for cond in conditions:
        stripped = _strip_sql_string_literals(cond)
        for col in sorted(columns_referenced(stripped, schema_keys)):
            if col not in referenced:
                referenced.append(col)
    if not referenced:
        return []

    per_field_nums, global_nums, str_literals, null_checked = _extract_condition_literals(
        conditions, schema_types
    )
    field_candidates: dict[str, list[Any]] = {
        col_name: _boundary_candidates_for_field(
            schema_types[col_name],
            per_field_nums[col_name],
            global_nums,
            str_literals,
            include_null=col_name in null_checked,
        )
        for col_name in referenced
    }

    base_row: dict[str, Any] = {}
    for col_name, ftype in schema_types.items():
        _assign_dotted(base_row, col_name, _default_dummy_for_type(ftype))

    # Build bounded combinations across referenced fields.
    total = 1
    for cands in field_candidates.values():
        total *= max(len(cands), 1)

    samples: list[dict[str, Any]] = []
    if total <= _MAX_PARTITION_SAMPLES:
        for combo in itertools.product(*(field_candidates[f] for f in referenced)):
            samples.append(dict(zip(referenced, combo, strict=True)))
    else:
        # One-at-a-time sweeps anchored at each representative value, plus diagonal corners.
        seen_keys: set[tuple[Any, ...]] = set()
        anchors = [field_candidates[f][0] for f in referenced]
        for idx, field in enumerate(referenced):
            for val in field_candidates[field]:
                row_vals = list(anchors)
                row_vals[idx] = val
                key = tuple(row_vals)
                if key not in seen_keys:
                    seen_keys.add(key)
                    samples.append(dict(zip(referenced, row_vals, strict=True)))
                    if len(samples) >= _MAX_PARTITION_SAMPLES:
                        break
            if len(samples) >= _MAX_PARTITION_SAMPLES:
                break
        if len(samples) < _MAX_PARTITION_SAMPLES:
            max_len = max(len(v) for v in field_candidates.values())
            for k in range(max_len):
                row_vals = [
                    field_candidates[f][min(k, len(field_candidates[f]) - 1)]
                    for f in referenced
                ]
                key = tuple(row_vals)
                if key not in seen_keys:
                    seen_keys.add(key)
                    samples.append(dict(zip(referenced, row_vals, strict=True)))
                    if len(samples) >= _MAX_PARTITION_SAMPLES:
                        break

    for assignment in samples:
        probe = json.loads(json.dumps(base_row))
        for field, val in assignment.items():
            _assign_dotted(probe, field, val)
        try:
            outcome = evaluate_outcome(sidecar, probe)
        except SidecarError:
            return []
        if outcome == "unknown":
            witness = ", ".join(f"{k}={v!r}" for k, v in assignment.items())
            return [
                f"outcomes: outcome partition gap detected for synthetic boundary input "
                f"({witness}) -> 'unknown'; tighten [outcomes] thresholds or add an "
                "unconditional residual branch"
            ]
    return []


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


def _validate_sql_condition(condition: str, result_schema: dict[str, Any]) -> str | None:
    """Validate a DuckDB SQL boolean fragment against a dummy table built from `result_schema`.

    Returns None if valid, or the DuckDB error string if invalid.
    """
    try:
        con = duckdb.connect()
        try:
            if result_schema:
                col_defs: list[str] = []
                for k, v in result_schema.items():
                    sql_t = _schema_type_to_sql(v)
                    if sql_t is not None:
                        col_defs.append(f"{_quote_sql_ident(k)} {sql_t}")
                cols = ", ".join(col_defs)
                if cols:
                    con.execute(f"CREATE TEMP TABLE _dummy ({cols})")
                    con.execute(f"SELECT ({condition}) FROM _dummy LIMIT 0")
                else:
                    con.execute(f"SELECT ({condition})")
            else:
                con.execute(f"SELECT ({condition})")
        finally:
            con.close()
    except Exception as e:
        return str(e)
    return None


def _check_dotted_refs_in_result(
    condition: str,
    field_prefix: str,
    label_desc: str,
    result: dict[str, Any],
    errors: list[ValidationError],
) -> None:
    """Ensure dotted nested references in `condition` resolve inside `result`."""
    if not condition or "${" in condition:
        return
    stripped = _strip_sql_string_literals(condition)
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
                    field_prefix,
                    f"Nested key {ref!r} referenced by {label_desc} is missing in result JSON",
                )
            )


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
        _check_dotted_refs_in_result(
            spec.condition,
            f"outcomes.{label}.condition",
            f"outcomes.{label}",
            result,
            errors,
        )
    for inv_name, inv_spec in sidecar.invariants.items():
        _check_dotted_refs_in_result(
            inv_spec.condition,
            f"invariants.{inv_name}.condition",
            f"invariants.{inv_name}",
            result,
            errors,
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

    if sidecar.invariants:
        for violated_name in evaluate_invariants(sidecar, result):
            errors.append(
                ValidationError(
                    f"invariants.{violated_name}",
                    f"Invariant {violated_name!r} failed against result JSON "
                    f"(condition: {sidecar.invariants[violated_name].condition!r})",
                )
            )

    cov_errors, _cov_warnings = check_factor_coverage(
        sidecar.declared_levels, result, sidecar.excluded_levels
    )
    errors.extend(cov_errors)

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


def _record_validation_error(
    errors: list[ValidationError],
    sidecar_path: Path | None,
    field: str,
    message: str,
    reason: str | None = None,
) -> None:
    """Append a ValidationError and emit `sidecar.validate_error` telemetry when path is set."""
    errors.append(ValidationError(field, message))
    if sidecar_path:
        event(
            "sidecar.validate_error",
            path=str(sidecar_path),
            field=field,
            reason=reason if reason is not None else message,
        )


_HYPOTHESIS_PLACEHOLDER_RE = re.compile(
    r"\$\{|"
    r"\b(?:TODO|TBD|FIXME)\b|"
    r"\bN/A\b|"
    r"<(?:\.\.\.|[A-Za-z_](?:[A-Za-z0-9_.: -]*[A-Za-z0-9_.])?)>",
    re.IGNORECASE,
)


def _find_hypothesis_placeholder(text: str) -> str | None:
    """Return the matched placeholder token if ``text`` contains an unfilled placeholder."""
    match = _HYPOTHESIS_PLACEHOLDER_RE.search(text)
    return match.group(0) if match else None


def _normalize_hypothesis_text(text: str) -> str:
    """Normalize whitespace, case, and punctuation for hypothesis/rival deduplication."""
    stripped_punct = re.sub(r"[^\w\s]", " ", text)
    return " ".join(stripped_punct.split()).lower()


def validate_hypothesis_block(
    sidecar: Sidecar,
    sidecar_path: Path | None = None,
    *,
    warnings: list[str] | None = None,
) -> list[ValidationError]:
    """Validate optional top-level `[hypothesis]` block (#3098).

    Opt-in: returns ``[]`` when ``sidecar.hypothesis_spec`` is ``None`` so existing
    sidecars remain valid. When declared, enforces non-empty structured fields for
    rivals, discriminating_pilot, which_source_may_lose, pitfalls, and remediations.
    """
    errors: list[ValidationError] = []
    spec = sidecar.hypothesis_spec
    if spec is None:
        return errors

    if sidecar.kind != SidecarKind.EXPERIMENT:
        _record_validation_error(
            errors,
            sidecar_path,
            "hypothesis",
            "[hypothesis] block is only valid in [experiment] sidecars",
        )
        return errors

    for key in spec.unknown_keys:
        _record_validation_error(
            errors,
            sidecar_path,
            f"hypothesis.{key}",
            f"Unknown key {key!r} in [hypothesis]",
        )

    for key, msg in spec.malformed_fields.items():
        _record_validation_error(
            errors,
            sidecar_path,
            f"hypothesis.{key}",
            f"[hypothesis].{key} {msg}",
        )

    if not (sidecar.hypothesis.strip() or (sidecar.question or "").strip()):
        _record_validation_error(
            errors,
            sidecar_path,
            "hypothesis",
            "[experiment].hypothesis (or question) must be non-empty when [hypothesis] is declared",
        )
    else:
        primary_source = sidecar.hypothesis.strip() or (sidecar.question or "").strip()
        if _find_hypothesis_placeholder(primary_source) is not None:
            _record_validation_error(
                errors,
                sidecar_path,
                "hypothesis",
                f"unfilled template placeholder in [experiment].hypothesis {primary_source!r}: "
                "placeholder must be filled in before the sidecar is valid",
            )

    if "rivals" not in spec.malformed_fields:
        if not spec.rivals:
            _record_validation_error(
                errors,
                sidecar_path,
                "hypothesis.rivals",
                "[hypothesis].rivals must be a non-empty list of rival hypotheses",
            )
        else:
            seen_rivals: set[str] = set()
            primary_norm = _normalize_hypothesis_text(sidecar.hypothesis)
            for idx, rival in enumerate(spec.rivals):
                if not isinstance(rival, str) or not rival.strip():
                    _record_validation_error(
                        errors,
                        sidecar_path,
                        "hypothesis.rivals",
                        f"[hypothesis].rivals[{idx}] must be a non-empty string",
                    )
                    break
                if _find_hypothesis_placeholder(rival) is not None:
                    _record_validation_error(
                        errors,
                        sidecar_path,
                        "hypothesis.rivals",
                        f"unfilled template placeholder in hypothesis.rivals[{idx}] {rival!r}: "
                        "placeholder must be filled in before the sidecar is valid",
                    )
                norm = _normalize_hypothesis_text(rival)
                if not norm:
                    _record_validation_error(
                        errors,
                        sidecar_path,
                        "hypothesis.rivals",
                        f"[hypothesis].rivals[{idx}] must contain non-punctuation text",
                    )
                    continue
                if primary_norm and norm == primary_norm:
                    _record_validation_error(
                        errors,
                        sidecar_path,
                        "hypothesis.rivals",
                        f"[hypothesis].rivals[{idx}] duplicates [experiment].hypothesis",
                    )
                if norm in seen_rivals:
                    _record_validation_error(
                        errors,
                        sidecar_path,
                        "hypothesis.rivals",
                        f"duplicate rival entry {rival!r} in [hypothesis].rivals",
                    )
                seen_rivals.add(norm)

    for field_key, val in (
        ("discriminating_pilot", spec.discriminating_pilot),
        ("which_source_may_lose", spec.which_source_may_lose),
    ):
        if field_key in spec.malformed_fields:
            continue
        if not isinstance(val, str) or not val.strip():
            _record_validation_error(
                errors,
                sidecar_path,
                f"hypothesis.{field_key}",
                f"[hypothesis].{field_key} must be a non-empty string",
            )
        elif _find_hypothesis_placeholder(val) is not None:
            _record_validation_error(
                errors,
                sidecar_path,
                f"hypothesis.{field_key}",
                f"unfilled template placeholder in hypothesis.{field_key} {val!r}: "
                "placeholder must be filled in before the sidecar is valid",
            )

    for list_key, items in (("pitfalls", spec.pitfalls), ("remediations", spec.remediations)):
        if list_key in spec.malformed_fields:
            continue
        if not items:
            _record_validation_error(
                errors,
                sidecar_path,
                f"hypothesis.{list_key}",
                f"[hypothesis].{list_key} must be a non-empty list",
            )
            continue
        for idx, item in enumerate(items):
            if not isinstance(item, str) or not item.strip():
                _record_validation_error(
                    errors,
                    sidecar_path,
                    f"hypothesis.{list_key}",
                    f"[hypothesis].{list_key}[{idx}] must be a non-empty string",
                )
                break
            if _find_hypothesis_placeholder(item) is not None:
                _record_validation_error(
                    errors,
                    sidecar_path,
                    f"hypothesis.{list_key}",
                    f"unfilled template placeholder in hypothesis.{list_key}[{idx}] {item!r}: "
                    "placeholder must be filled in before the sidecar is valid",
                )

    if (
        warnings is not None
        and "pitfalls" not in spec.malformed_fields
        and "remediations" not in spec.malformed_fields
        and spec.pitfalls
        and spec.remediations
        and len(spec.remediations) < len(spec.pitfalls)
    ):
        warnings.append(
            f"hypothesis.remediations: fewer remediations ({len(spec.remediations)}) than "
            f"pitfalls ({len(spec.pitfalls)}) — ensure each pitfall has a remediation"
        )

    return errors


def validate_popper_block(
    sidecar: Sidecar,
    sidecar_path: Path | None = None,
) -> list[ValidationError]:
    """Validate [popper] and optional [popper.continuous] block fields (Section 2.3 of #792 spec).

    Returns a list of ValidationError (empty if valid).
    WARNING-level entries use message prefixed with "WARNING:".

    Emits a ``sidecar.validate_error`` event per error when ``sidecar_path`` is
    given, matching validate_reproduction_block / _controls_block /
    _differential_block. WARNING-level entries emit nothing — they are advisory,
    and labelling them ``validate_error`` would misreport them.
    """
    errors: list[ValidationError] = []

    if sidecar.popper_null_pass_rate is None and sidecar.popper_continuous is None:
        return errors  # No [popper] block — nothing to validate

    # [popper] is only valid on experiment sidecars
    if sidecar.kind != SidecarKind.EXPERIMENT:
        _record_validation_error(
            errors,
            sidecar_path,
            "popper",
            "[popper] block is only valid in [experiment] sidecars",
        )
        return errors  # No point validating further

    null = sidecar.popper_null_pass_rate
    alt = sidecar.popper_alt_pass_rate
    threshold = sidecar.popper_stopping_threshold

    validate_discrete = (
        sidecar.popper_continuous is None or null is not None or alt is not None
    )
    if validate_discrete:
        # null_pass_rate range
        if null is None or not (0 < null < 1):
            _record_validation_error(
                errors,
                sidecar_path,
                "popper.null_pass_rate",
                f"null_pass_rate must be in (0, 1), got {null!r}",
                reason=f"null_pass_rate out of range: {null!r}",
            )

        # alt_pass_rate range
        if alt is None or not (0 < alt < 1):
            _record_validation_error(
                errors,
                sidecar_path,
                "popper.alt_pass_rate",
                f"alt_pass_rate must be in (0, 1), got {alt!r}",
                reason=f"alt_pass_rate out of range: {alt!r}",
            )

        # null != alt (no test power if equal)
        if null is not None and alt is not None and null == alt:
            _record_validation_error(
                errors,
                sidecar_path,
                "popper",
                "null_pass_rate and alt_pass_rate are identical; no test power",
            )

    # stopping_threshold >= 1.0
    if threshold is None or threshold < 1.0:
        _record_validation_error(
            errors,
            sidecar_path,
            "popper.stopping_threshold",
            f"stopping_threshold must be >= 1.0, got {threshold!r}",
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
            _record_validation_error(
                errors,
                sidecar_path,
                f"popper.weights.{key}",
                f"Unknown outcome label {key!r} in [popper.weights] — not declared in [outcomes]",
                reason=f"Unknown outcome label {key!r} not declared in [outcomes]",
            )
        if val <= 0:
            _record_validation_error(
                errors,
                sidecar_path,
                f"popper.weights.{key}",
                f"Weight for {key!r} must be > 0, got {val!r}",
            )
        if key == "error" and val != 1.0:
            _record_validation_error(
                errors,
                sidecar_path,
                "popper.weights.error",
                f"Weight for 'error' must be exactly 1.0 (non-overridable), got {val!r}",
                reason=f"Weight for 'error' must be exactly 1.0, got {val!r}",
            )

    # Validate [popper.continuous] if present
    cont = sidecar.popper_continuous
    if cont is not None:
        for key in cont.unknown_keys:
            _record_validation_error(
                errors,
                sidecar_path,
                f"popper.continuous.{key}",
                f"Unknown key {key!r} in [popper.continuous]",
            )

        if not cont.metric:
            _record_validation_error(
                errors,
                sidecar_path,
                "popper.continuous.metric",
                "[popper.continuous].metric must be a non-empty [result_schema] float field",
            )
        elif cont.metric not in sidecar.result_schema:
            _record_validation_error(
                errors,
                sidecar_path,
                "popper.continuous.metric",
                f"[popper.continuous].metric {cont.metric!r} is not declared in [result_schema]",
            )
        else:
            metric_type = sidecar.result_schema.get(cont.metric)
            if not isinstance(metric_type, str) or metric_type.lower() not in ("float", "double"):
                _record_validation_error(
                    errors,
                    sidecar_path,
                    "popper.continuous.metric",
                    f"[popper.continuous].metric {cont.metric!r} must have float type in "
                    f"[result_schema], got {metric_type!r}",
                )

        method = (cont.method or "gaussian").strip().lower()
        if method not in CONTINUOUS_POPPER_METHODS:
            _record_validation_error(
                errors,
                sidecar_path,
                "popper.continuous.method",
                f"Unsupported [popper.continuous].method {cont.method!r}; "
                f"must be one of {sorted(CONTINUOUS_POPPER_METHODS)}",
            )

        if cont.null_mean is None or not math.isfinite(cont.null_mean):
            _record_validation_error(
                errors,
                sidecar_path,
                "popper.continuous.null_mean",
                f"[popper.continuous].null_mean must be a finite number, got {cont.null_mean!r}",
            )
        if cont.alt_mean is None or not math.isfinite(cont.alt_mean):
            _record_validation_error(
                errors,
                sidecar_path,
                "popper.continuous.alt_mean",
                f"[popper.continuous].alt_mean must be a finite number, got {cont.alt_mean!r}",
            )

        sigma0 = cont.null_std if cont.null_std is not None else cont.std
        sigma1 = cont.alt_std if cont.alt_std is not None else cont.std
        if sigma0 is None or not math.isfinite(sigma0) or sigma0 <= 0:
            std_field = (
                "popper.continuous.null_std"
                if cont.null_std is not None
                else "popper.continuous.std"
            )
            _record_validation_error(
                errors,
                sidecar_path,
                std_field,
                f"[popper.continuous] standard deviation must be finite and > 0, got {sigma0!r}",
            )
        if sigma1 is None or not math.isfinite(sigma1) or sigma1 <= 0:
            std_field = (
                "popper.continuous.alt_std"
                if cont.alt_std is not None
                else "popper.continuous.std"
            )
            _record_validation_error(
                errors,
                sidecar_path,
                std_field,
                f"[popper.continuous] standard deviation must be finite and > 0, got {sigma1!r}",
            )
        if (
            method == "subgaussian"
            and sigma0 is not None
            and sigma1 is not None
            and sigma0 != sigma1
        ):
            _record_validation_error(
                errors,
                sidecar_path,
                "popper.continuous.alt_std",
                "subgaussian method requires a single std (null_std == alt_std)",
            )
        if (
            cont.null_mean is not None
            and cont.alt_mean is not None
            and sigma0 is not None
            and sigma1 is not None
            and cont.null_mean == cont.alt_mean
            and sigma0 == sigma1
        ):
            _record_validation_error(
                errors,
                sidecar_path,
                "popper.continuous",
                "null and alt continuous parameters are identical; no test power",
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

    for pin_field, pin_val in (
        ("reproduction.requires_pass_sidecar_sha256", repro.requires_pass_sidecar_sha256),
        ("reproduction.requires_pass_script_sha256", repro.requires_pass_script_sha256),
    ):
        if pin_val and not re.match(r"^(?:sha256:)?[0-9a-fA-F]{64}$", pin_val.strip()):
            errors.append(
                ValidationError(
                    pin_field,
                    f"{pin_field} must be a 64-character hex SHA256 digest, got {pin_val!r}",
                )
            )
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field=pin_field,
                    reason=f"Invalid SHA256 digest: {pin_val}",
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


def _validate_assertion_map(
    assertions: dict[str, InvariantSpec],
    prefix: str,
    sidecar: Sidecar,
    sidecar_path: Path | None,
    errors: list[ValidationError],
    warnings: list[str] | None = None,
) -> None:
    """Validate `{name: InvariantSpec}` entries (`[invariants.*]` or `[sanity.assertions.*]`)."""
    schema_keys = _collect_schema_field_names(sidecar.result_schema) if sidecar.result_schema else set()

    for name, spec in assertions.items():
        if not spec.condition.strip():
            err = ValidationError(f"{prefix}.{name}", "Missing 'condition' field")
            errors.append(err)
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field=err.field,
                    reason=err.message,
                )
        elif "${" in spec.condition:
            err = ValidationError(
                f"{prefix}.{name}.condition",
                f"unfilled placeholder in condition {spec.condition!r}: "
                "template '${...}' must be filled in before the sidecar is valid",
            )
            errors.append(err)
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field=err.field,
                    reason=err.message,
                )
        else:
            for null_op in _find_null_equality_ops(spec.condition):
                err = ValidationError(
                    f"{prefix}.{name}.condition",
                    f"Invalid SQL NULL comparison ({null_op!r}) in condition "
                    f"{spec.condition!r} — use 'IS NULL' or 'IS NOT NULL' instead",
                )
                errors.append(err)
                if sidecar_path:
                    event(
                        "sidecar.validate_error",
                        path=str(sidecar_path),
                        field=err.field,
                        reason=err.message,
                    )
            sql_err = _validate_sql_condition(spec.condition, sidecar.result_schema)
            if sql_err is not None:
                err = ValidationError(
                    f"{prefix}.{name}.condition",
                    f"DuckDB parse error: {sql_err}",
                )
                errors.append(err)
                if sidecar_path:
                    event(
                        "sidecar.validate_error",
                        path=str(sidecar_path),
                        field=err.field,
                        reason=err.message,
                    )
            elif warnings is not None and schema_keys:
                stripped = _strip_sql_string_literals(spec.condition)
                if not columns_referenced(stripped, schema_keys):
                    warnings.append(
                        f"{prefix}.{name}.condition: references no field declared in [result_schema]"
                    )

        if not spec.reasoning.strip():
            err = ValidationError(f"{prefix}.{name}", "Missing 'reasoning' field")
            errors.append(err)
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field=err.field,
                    reason=err.message,
                )


def validate_invariants_block(
    sidecar: Sidecar,
    sidecar_path: Path | None = None,
    *,
    warnings: list[str] | None = None,
) -> list[ValidationError]:
    """Validate optional `[invariants]` assertions (spec 260707 §5)."""
    errors: list[ValidationError] = []
    if not sidecar.invariants:
        return errors

    if sidecar.kind == SidecarKind.DEBUG:
        err = ValidationError(
            "invariants",
            "[invariants] is only valid in [experiment], [benchmark], or [validation] sidecars",
        )
        errors.append(err)
        if sidecar_path:
            event(
                "sidecar.validate_error",
                path=str(sidecar_path),
                field=err.field,
                reason=err.message,
            )
        return errors

    _validate_assertion_map(
        sidecar.invariants,
        "invariants",
        sidecar,
        sidecar_path,
        errors,
        warnings=warnings,
    )
    return errors


def validate_sanity_block(
    sidecar: Sidecar,
    sidecar_path: Path | None = None,
    *,
    warnings: list[str] | None = None,
) -> list[ValidationError]:
    """Validate optional confirmatory `[sanity]` pre-flight block (spec 260707 §7)."""
    errors: list[ValidationError] = []
    if sidecar.sanity is None:
        return errors

    if sidecar.kind == SidecarKind.DEBUG:
        err = ValidationError(
            "sanity",
            "[sanity] is only valid in [experiment], [benchmark], or [validation] sidecars",
        )
        errors.append(err)
        if sidecar_path:
            event(
                "sidecar.validate_error",
                path=str(sidecar_path),
                field=err.field,
                reason=err.message,
            )
        return errors

    if not sidecar.sanity.argv_suffix or any(
        not str(arg).strip() for arg in sidecar.sanity.argv_suffix
    ):
        err = ValidationError(
            "sanity.argv_suffix",
            "argv_suffix must be a non-empty list of non-blank CLI arguments",
        )
        errors.append(err)
        if sidecar_path:
            event(
                "sidecar.validate_error",
                path=str(sidecar_path),
                field=err.field,
                reason=err.message,
            )

    if not sidecar.sanity.assertions:
        err = ValidationError(
            "sanity.assertions",
            "[sanity] requires at least one [sanity.assertions.<name>] entry",
        )
        errors.append(err)
        if sidecar_path:
            event(
                "sidecar.validate_error",
                path=str(sidecar_path),
                field=err.field,
                reason=err.message,
            )
        return errors

    _validate_assertion_map(
        sidecar.sanity.assertions,
        "sanity.assertions",
        sidecar,
        sidecar_path,
        errors,
        warnings=warnings,
    )
    return errors


# --- #2662: Unmeasured prose data-quality / filtering claims -----------------------------------

_PROSE_QUALITY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:[Xx]|stop|gap|unk|unknown|ambiguous|non[- ]?standard|nan|null|duplicate|outlier|artifact|contamination)[- ]free\b"
    ),
    re.compile(
        r"\bfree\s+of\s+(?:[Xx]\b|unknown\s+tokens?|ambiguous|gaps?|stops?|NaNs?|nulls?|duplicates?|contaminants?|outliers?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:no|without|zero|0|exclud(?:e|es|ed|ing)|filtered\s+out|remov(?:e|ed|ing)|strip(?:ped|ping)?|clean(?:ed)?\s+of)\s+"
        r"(?:[Xx]\b(?:\s+(?:residues?|tokens?|characters?|letters?))?|unknown\s+(?:tokens?|residues?|letters?|aas?|amino\s+acids?)|"
        r"token\s+20|ambiguous\s+(?:residues?|bases?|tokens?|calls?)|stop\s+codons?|NaNs?\b|nulls?\b|duplicates?\b|contaminants?\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b\d+\s+(?:unique|filtered|deduplicated|valid|clean|retained|X[- ]?free)\s+"
        r"(?:queries|sequences|samples|rows|items|records|units|chains|structures|candidates)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:post[- ]?filter(?:ed)?|deduplicated|filtered\s+(?:queries|sequences|samples|dataset|set|inputs?))\b",
        re.IGNORECASE,
    ),
)

_DATA_QUALITY_SCHEMA_KEY_RE = re.compile(
    r"^(?:n_.*|count_.*|num_.*|x_.*|.*_count|.*_cnt|.*_filtered|.*_unique|.*_valid|.*_excluded|"
    r".*_missing|.*_nan|.*_null|.*_unknown|.*_x|has_.*|is_.*_free)$",
    re.IGNORECASE,
)


def _sidecar_prose_fields(sidecar: Sidecar) -> list[tuple[str, str]]:
    """Collect prose text fields from a sidecar for #2662 inspection."""
    texts: list[tuple[str, str]] = []
    for attr in (
        "hypothesis",
        "question",
        "reopen_reason",
        "property",
        "reference",
        "target",
        "symptom",
        "suspected_cause",
        "verification",
    ):
        val = getattr(sidecar, attr, None)
        if isinstance(val, str) and val.strip():
            texts.append((attr, val))
    if sidecar.observe and sidecar.observe.looking_for:
        texts.append(("observe.looking_for", sidecar.observe.looking_for))
    if sidecar.calibrate and sidecar.calibrate.quantity:
        texts.append(("calibrate.quantity", sidecar.calibrate.quantity))
    for label, spec in sidecar.outcomes.items():
        if spec.decision:
            texts.append((f"outcomes.{label}.decision", spec.decision))
        if spec.reasoning:
            texts.append((f"outcomes.{label}.reasoning", spec.reasoning))
    for name, inv in sidecar.invariants.items():
        if inv.reasoning:
            texts.append((f"invariants.{name}.reasoning", inv.reasoning))
    return texts


def validate_prose_data_quality_claims(
    sidecar: Sidecar,
    sidecar_path: Path | None = None,
) -> list[ValidationError]:
    """Guard against prose data-quality/filtering claims lacking a measured count + invariant (#2662)."""
    hits: list[tuple[str, str]] = []
    for field_name, text in _sidecar_prose_fields(sidecar):
        for pat in _PROSE_QUALITY_PATTERNS:
            m = pat.search(text)
            if m:
                hits.append((field_name, m.group(0)))
                break

    if not hits:
        return []

    schema_keys = _collect_schema_field_names(sidecar.result_schema)
    quality_keys = {k for k in schema_keys if _DATA_QUALITY_SCHEMA_KEY_RE.match(k.split(".")[-1])}

    assertion_fragments: list[str] = [
        inv.condition for inv in sidecar.invariants.values() if inv.condition
    ]
    if sidecar.observe is not None:
        assertion_fragments.extend(expr for expr in sidecar.observe.sanity if expr)
    if sidecar.sanity is not None:
        assertion_fragments.extend(
            a.condition for a in sidecar.sanity.assertions.values() if a.condition
        )

    referenced_quality = set()
    for frag in assertion_fragments:
        referenced_quality.update(columns_referenced(frag, quality_keys))

    if quality_keys and referenced_quality:
        return []

    errors: list[ValidationError] = []
    sample_field, sample_match = hits[0]
    if not quality_keys:
        reason = (
            f"{sample_field} makes prose data-quality/filtering claim ({sample_match!r}) "
            "without a measured count/quality column in [result_schema] (e.g. n_x / x_count / n_filtered) "
            "and a corresponding fail-closed [invariants.*] assertion"
        )
    else:
        reason = (
            f"{sample_field} makes prose data-quality/filtering claim ({sample_match!r}) — "
            f"[result_schema] declares {sorted(quality_keys)}, but no [invariants.*] "
            "(or [sanity]) condition asserts it fails closed"
        )
    err = ValidationError("prose_data_quality", reason)
    errors.append(err)
    if sidecar_path:
        event(
            "sidecar.validate_error",
            path=str(sidecar_path),
            field=err.field,
            reason=err.message,
        )
    return errors


# --- #2200: Declared-vs-analyzed factor/arm coverage & n_units_informative ---------------------


def validate_factor_declarations(
    sidecar: Sidecar,
    sidecar_path: Path | None = None,
    *,
    warnings: list[str] | None = None,
) -> list[ValidationError]:
    """Validate `[factors]` / `[excluded_levels]` declarations and `n_units_informative` convention (#2200)."""
    errors: list[ValidationError] = []

    for factor, levels in sidecar.declared_levels.items():
        if not levels or any(not str(lv).strip() for lv in levels):
            err = ValidationError(
                f"factors.declared_levels.{factor}",
                "declared_levels must be a non-empty list of non-blank level names",
            )
            errors.append(err)
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field=err.field,
                    reason=err.message,
                )

    for key, reason in sidecar.excluded_levels.items():
        if not isinstance(reason, str) or not reason.strip():
            err = ValidationError(
                f"excluded_levels.{key}",
                f"excluded_levels entry {key!r} must state a non-empty exclusion reason",
            )
            errors.append(err)
            if sidecar_path:
                event(
                    "sidecar.validate_error",
                    path=str(sidecar_path),
                    field=err.field,
                    reason=err.message,
                )

    if (
        warnings is not None
        and "n_units" in sidecar.result_schema
        and "n_units_informative" not in sidecar.result_schema
    ):
        warnings.append(
            "result_schema.n_units: declares 'n_units' without companion 'n_units_informative' "
            "(#2200 — report non-degenerate unit count alongside total n_units)"
        )

    return errors


def _extract_analyzed_levels(factor: str, result: dict[str, Any]) -> set[str] | None:
    """Best-effort extraction of analyzed level names for `factor` from a result dict."""
    analyzed_map = result.get("analyzed_levels") or result.get("covered_levels")
    if isinstance(analyzed_map, dict) and factor in analyzed_map:
        val = analyzed_map[factor]
        if isinstance(val, (list, tuple, set)):
            return {str(x) for x in val}
        if isinstance(val, dict):
            return {str(k) for k in val}

    plural = factor if factor.endswith("s") else f"{factor}s"
    candidates = (
        f"{factor}_analyzed",
        f"analyzed_{factor}",
        f"{plural}_analyzed",
        f"analyzed_{plural}",
        f"per_{factor}",
        factor,
        plural,
    )
    for key in candidates:
        val = result.get(key)
        if isinstance(val, dict):
            return {str(k) for k in val}
        if isinstance(val, list):
            if val and all(isinstance(x, dict) for x in val):
                extracted = {str(x[factor]) for x in val if factor in x}
                if extracted:
                    return extracted
            else:
                return {str(x) for x in val}
    return None


def _is_level_excluded(factor: str, level: str, excluded_levels: dict[str, str]) -> bool:
    for key in (f"{factor}.{level}", level):
        reason = excluded_levels.get(key)
        if isinstance(reason, str) and reason.strip():
            return True
    return False


def _extract_declared_from_result(result: dict[str, Any]) -> dict[str, list[str]]:
    """Collect declared factor levels from result manifest/metadata fields if present."""
    declared: dict[str, list[str]] = {}
    dl = result.get("declared_levels")
    if isinstance(dl, dict):
        for k, v in dl.items():
            if isinstance(v, (list, tuple)):
                declared[str(k)] = [str(x) for x in v]

    for container_key in ("input_manifest", "manifest"):
        container = result.get(container_key)
        if isinstance(container, dict):
            for k, v in container.items():
                if isinstance(v, (list, tuple)) and k not in declared:
                    declared[str(k)] = [str(x) for x in v]

    meta = result.get("metadata")
    if isinstance(meta, dict):
        for k in ("models_used", "arms", "states", "temperatures", "levels"):
            v = meta.get(k)
            if isinstance(v, (list, tuple)) and k not in declared:
                declared[k] = [str(x) for x in v]
    return declared


def check_factor_coverage(
    declared_levels: dict[str, list[str]],
    result: dict[str, Any],
    excluded_levels: dict[str, str] | None = None,
) -> tuple[list[ValidationError], list[str]]:
    """Check declared vs analyzed factor/arm coverage and `n_units_informative` (#2200).

    Returns ``(errors, warnings)``.
    """
    errors: list[ValidationError] = []
    warnings: list[str] = []
    exclusions = dict(excluded_levels or {})
    if isinstance(result.get("excluded_levels"), dict):
        for k, v in result["excluded_levels"].items():
            if isinstance(v, str):
                exclusions.setdefault(str(k), v.strip())

    merged_declared = dict(declared_levels)
    for k, v in _extract_declared_from_result(result).items():
        merged_declared.setdefault(k, v)

    for factor, levels in merged_declared.items():
        if not levels:
            continue
        analyzed = _extract_analyzed_levels(factor, result)
        if analyzed is None:
            is_explicit = factor in declared_levels or (
                isinstance(result.get("declared_levels"), dict)
                and factor in result["declared_levels"]
            )
            if not is_explicit:
                continue
            analyzed = set()

        missing = [
            lv
            for lv in levels
            if str(lv) not in analyzed and not _is_level_excluded(factor, str(lv), exclusions)
        ]
        if missing:
            errors.append(
                ValidationError(
                    "factor_coverage",
                    f"Declared factor {factor!r} levels missing from analyzed result and not in "
                    f"excluded_levels with a reason: {missing}",
                )
            )

    if "n_units" in result and result["n_units"] is not None:
        n_units = result["n_units"]
        if isinstance(n_units, (int, float)) and not isinstance(n_units, bool) and n_units > 0:
            if "n_units_informative" not in result or result["n_units_informative"] is None:
                errors.append(
                    ValidationError(
                        "n_units_informative",
                        f"Result reports n_units={n_units} without companion n_units_informative (#2200)",
                    )
                )
            else:
                n_inf = result["n_units_informative"]
                if not isinstance(n_inf, (int, float)) or isinstance(n_inf, bool):
                    errors.append(
                        ValidationError(
                            "n_units_informative",
                            f"n_units_informative must be numeric, got {type(n_inf).__name__}",
                        )
                    )
                elif n_inf < 0 or n_inf > n_units:
                    errors.append(
                        ValidationError(
                            "n_units_informative",
                            f"n_units_informative ({n_inf}) must be in [0, n_units={n_units}]",
                        )
                    )
                elif (n_inf / n_units) < 0.5:
                    warnings.append(
                        f"n_units_informative: low informative fraction {n_inf}/{n_units} "
                        f"({n_inf / n_units:.1%}) — possible rank-tie / padded-length dilution (#2200)"
                    )

    return errors, warnings


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

def _extract_result_field(data: dict[str, Any], field_path: str) -> tuple[bool, Any]:
    """Resolve top-level or dotted ``field_path`` from ``data``. Returns ``(found, value)``."""
    if field_path in data:
        return True, data[field_path]
    if "." in field_path:
        cur: Any = data
        for part in field_path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return False, None
            cur = cur[part]
        return True, cur
    return False, None


def read_run_result_metadata(run: Any, catalog_dir: Path) -> dict[str, Any]:
    """Read a run's emitted result JSON dict from ``run.metadata`` or log-mode ``run.finished``."""
    raw_meta = getattr(run, "metadata", None)
    if isinstance(raw_meta, dict) and raw_meta:
        return raw_meta
    if isinstance(raw_meta, str) and raw_meta.strip() and raw_meta.strip() != "{}":
        try:
            parsed = json.loads(raw_meta)
            if isinstance(parsed, dict) and parsed:
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass

    # In log mode (BTH_LOG_MODE=1), fold_run does not copy run.finished.data.metadata
    # into idx.runs.metadata; read it from the run.finished event when available.
    run_id = getattr(run, "id", "")
    if run_id and catalog_dir is not None:
        try:
            from bathos.index import catalog_readable, connect_read

            if catalog_readable(catalog_dir):
                con = connect_read(catalog_dir, read_only=True)
                try:
                    rows = con.execute(
                        "SELECT data FROM events WHERE kind = 'run.finished' AND entity_key = ? "
                        "ORDER BY ts DESC, eid DESC LIMIT 1",
                        [json.dumps([run_id])],
                    ).fetchall()
                    if rows:
                        ev_data = json.loads(rows[0][0]) if isinstance(rows[0][0], str) else rows[0][0]
                        if isinstance(ev_data, dict):
                            m = ev_data.get("metadata")
                            if isinstance(m, str) and m.strip():
                                parsed_m = json.loads(m)
                                if isinstance(parsed_m, dict):
                                    return parsed_m
                            elif isinstance(m, dict):
                                return m
                finally:
                    con.close()
        except Exception:
            pass
    return {}


def check_calibration_source_ref(
    source_str: str,
    catalog_dir: Path,
) -> tuple[str | None, str | None, Any | None, dict[str, Any]]:
    """Validate a ``run:<id>:<field>`` source against ``catalog_dir`` (spec §3.5, S5).

    Returns ``(issue_code, message, run_obj, result_dict)``. ``issue_code`` is None on success.
    """
    src = parse_source(source_str)
    if src is None or src.kind != "run":
        return None, None, None, {}

    from bathos.query import AmbiguousRunId, get_run

    try:
        run_obj = get_run(src.ref, catalog_dir, allow_cool_fallback=True, include_superseded=True)
    except AmbiguousRunId as exc:
        return "calibration_run_not_found", f"source {source_str!r}: {exc}", None, {}
    except Exception as exc:
        return (
            "calibration_run_not_found",
            f"source {source_str!r}: failed to look up run {src.ref!r} ({exc})",
            None,
            {},
        )

    if run_obj is None:
        return (
            "calibration_run_not_found",
            f"source {source_str!r}: calibration run {src.ref!r} not found in catalog",
            None,
            {},
        )

    if run_obj.epistemic_class != "calibrating":
        return (
            "non_calibrating_run_source",
            f"source {source_str!r}: run {run_obj.id[:8]} has epistemic_class="
            f"{run_obj.epistemic_class!r}, expected 'calibrating' (spec §3.5)",
            run_obj,
            {},
        )

    if run_obj.exit_code != 0 or run_obj.outcome in ("error", "invalid_measurement"):
        return (
            "calibration_run_unusable",
            f"source {source_str!r}: calibration run {run_obj.id[:8]} exited {run_obj.exit_code} "
            f"with outcome={run_obj.outcome!r} (C16)",
            run_obj,
            {},
        )

    result_dict = read_run_result_metadata(run_obj, catalog_dir)
    found, val = _extract_result_field(result_dict, src.field)
    if not found or val is None:
        detail = "is null" if found else "not found in result JSON"
        return (
            "calibration_field_missing",
            f"source {source_str!r}: field {src.field!r} {detail} on calibration run {run_obj.id[:8]}",
            run_obj,
            result_dict,
        )

    return None, None, run_obj, result_dict


def validate_holdout_and_observation(
    sidecar: Sidecar,
    sidecar_path: Path | None,
    epistemic_class: str | None,
    *,
    catalog_dir: Path | None = None,
    project_root: Path | None = None,
    errors: list[ValidationError],
) -> int | None:
    """Validate `[holdout]` and `[provenance].derived_from_observation` (spec §3.4, S4).

    Returns the effective holdout rank (0..3) if `[holdout]` is present and valid, else None.
    """
    holdout = sidecar.holdout
    obs_id = (sidecar.derived_from_observation or "").strip()

    if holdout is None and not obs_id:
        return None

    if sidecar.kind != SidecarKind.EXPERIMENT:
        if holdout is not None:
            errors.append(ValidationError("holdout", "[holdout] is only valid on [experiment] sidecars"))
        if obs_id:
            errors.append(
                ValidationError(
                    "provenance.derived_from_observation",
                    "[provenance].derived_from_observation is only valid on [experiment] sidecars",
                )
            )
        return None

    if obs_id and holdout is None:
        errors.append(
            ValidationError(
                "holdout",
                "[holdout] block is required when [provenance].derived_from_observation is set",
            )
        )

    effective_rank: int | None = None
    has_placeholder = False
    if holdout is not None:
        for hfield, hval in (
            ("kind", holdout.kind),
            ("unit", holdout.unit),
            ("split_manifest", holdout.split_manifest),
        ):
            if hval and "${" in hval:
                has_placeholder = True
                errors.append(
                    ValidationError(
                        f"holdout.{hfield}",
                        f"unfilled placeholder in holdout.{hfield} {hval!r}: "
                        "template '${...}' must be filled in before the sidecar is valid",
                    )
                )

        if not has_placeholder:
            if not holdout.kind:
                errors.append(ValidationError("holdout.kind", "[holdout] requires a non-empty 'kind'"))
            elif holdout.kind in FORBIDDEN_HOLDOUT_KINDS:
                errors.append(
                    ValidationError(
                        "holdout.kind",
                        f"{holdout.kind!r} is not a valid holdout kind: frame or time splits of "
                        "the same trajectories are not independent (C9)",
                    )
                )
            elif holdout.kind not in HOLDOUT_KINDS:
                errors.append(
                    ValidationError(
                        "holdout.kind",
                        f"invalid holdout kind {holdout.kind!r}; must be one of {list(HOLDOUT_KINDS)}",
                    )
                )
            elif holdout.kind == "held_out_split":
                if holdout.unit in FORBIDDEN_SPLIT_UNITS:
                    errors.append(
                        ValidationError(
                            "holdout.unit",
                            f"held_out_split unit {holdout.unit!r} is forbidden: frame or time splits "
                            "of the same trajectories are not valid (C9); use one of ['seed', 'build', 'system']",
                        )
                    )
                elif holdout.unit not in HOLDOUT_SPLIT_UNIT_RANKS:
                    errors.append(
                        ValidationError(
                            "holdout.unit",
                            f"held_out_split requires 'unit' in ['seed', 'build', 'system'], "
                            f"got {holdout.unit!r}",
                        )
                    )
                elif (
                    obs_id
                    and holdout.unit in ("build", "system")
                    and not holdout.split_manifest
                    and epistemic_class in ("confirmatory", "legacy")
                ):
                    errors.append(
                        ValidationError(
                            "holdout.split_manifest",
                            "held_out_split promoting an observation requires 'split_manifest' "
                            "(committed in git or emitted by an earlier bth run before the source run) "
                            "to rank >= 1 (C10)",
                        )
                    )

            effective_rank = holdout_rank(holdout)
            if (
                effective_rank == 0
                and epistemic_class in ("confirmatory", "legacy")
                and not (holdout.kind == "held_out_split" and holdout.unit not in HOLDOUT_SPLIT_UNIT_RANKS)
            ):
                errors.append(
                    ValidationError(
                        "holdout",
                        f"holdout kind={holdout.kind!r}"
                        + (f" (unit={holdout.unit!r})" if holdout.unit else "")
                        + " ranks 0 and cannot promote to a confirmatory test; use rank >= 1 "
                        "(new_builds, held_out_split with unit='build'|'system', new_system) "
                        "or scaffold an exploratory kill test",
                    )
                )

    if obs_id and catalog_dir is not None:
        from bathos.observations import (
            ObservationError,
            check_observation_promotion_eligibility,
            check_split_manifest_timing,
            get_observation,
        )

        try:
            obs = get_observation(obs_id, catalog_dir=catalog_dir)
        except ObservationError as exc:
            errors.append(ValidationError("provenance.derived_from_observation", str(exc)))
            obs = None

        if obs is None:
            if not any(e.field == "provenance.derived_from_observation" for e in errors):
                errors.append(
                    ValidationError(
                        "provenance.derived_from_observation",
                        f"observation {obs_id!r} not found in catalog",
                    )
                )
        else:
            h_kind = holdout.kind if (holdout is not None and "${" not in holdout.kind) else ""
            for msg in check_observation_promotion_eligibility(
                obs, holdout_kind=h_kind, catalog_dir=catalog_dir
            ):
                field_name = (
                    "holdout.kind"
                    if "literature_prediction" in msg
                    else "provenance.derived_from_observation"
                )
                errors.append(ValidationError(field_name, msg))

            if obs.run_id:
                for label, spec in sidecar.outcomes.items():
                    if not spec.source or "${" in spec.source:
                        continue
                    src = parse_source(spec.source)
                    if (
                        src is not None
                        and src.kind == "run"
                        and (
                            obs.run_id == src.ref
                            or obs.run_id.startswith(src.ref)
                            or src.ref.startswith(obs.run_id)
                        )
                    ):
                        errors.append(
                            ValidationError(
                                f"outcomes.{label}.source",
                                f"cannot cite exploratory observation source run {obs.run_id[:8]} "
                                "as a threshold source (optimistic selection bias); use a "
                                "calibrating run, SESOI, appraised literature, or provisional",
                            )
                        )

            if (
                holdout is not None
                and not has_placeholder
                and holdout.kind == "held_out_split"
                and holdout.unit in ("build", "system")
                and holdout.split_manifest
            ):
                probe_root = project_root
                if probe_root is None and sidecar_path is not None:
                    from bathos.config import find_project_config

                    cfg_p = find_project_config(sidecar_path.parent)
                    probe_root = cfg_p.parent if cfg_p is not None else sidecar_path.parent
                timing_ok, timing_msg = check_split_manifest_timing(
                    holdout.split_manifest,
                    obs,
                    project_root=probe_root,
                    catalog_dir=catalog_dir,
                )
                effective_rank = holdout_rank(holdout, split_timing_ok=timing_ok)
                if not timing_ok and epistemic_class in ("confirmatory", "legacy"):
                    errors.append(ValidationError("holdout.split_manifest", timing_msg))

    return effective_rank


def validate_sidecar(
    sidecar: Sidecar,
    sidecar_path: Path | None = None,
    claim=None,
    *,
    stem_has_prior_runs: bool = False,
    result: dict[str, Any] | Path | str | None = None,
    catalog_dir: Path | None = None,
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
            sql_err = _validate_sql_condition(spec.condition, sidecar.result_schema)
            if sql_err is not None:
                err = ValidationError(
                    f"outcomes.{label}.condition",
                    f"DuckDB parse error: {sql_err}",
                )
                errors.append(err)
                if sidecar_path:
                    event(
                        "sidecar.validate_error",
                        path=str(sidecar_path),
                        field=f"outcomes.{label}.condition",
                        reason=f"DuckDB parse error: {sql_err}",
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

    # Probe synthetic boundary inputs against [outcomes] to surface partition gaps (#2224)
    if sidecar.outcomes and not any(
        e.field.startswith("outcomes.") and e.field.endswith(".condition") for e in errors
    ):
        warnings.extend(check_outcome_partition_gaps(sidecar))

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

    # Validate optional [hypothesis] block (#3098)
    hypothesis_errors = validate_hypothesis_block(sidecar, sidecar_path, warnings=warnings)
    errors.extend(hypothesis_errors)

    # Validate [reproduction] block if present
    repro_errors = validate_reproduction_block(sidecar, sidecar_path)
    errors.extend(repro_errors)

    # Validate [controls] block if present
    controls_errors = validate_controls_block(sidecar, sidecar_path)
    errors.extend(controls_errors)

    # Validate [differential] block if present (debt #1071)
    differential_errors = validate_differential_block(sidecar, sidecar_path)
    errors.extend(differential_errors)

    # Validate [invariants] and [sanity] blocks (spec 260707; debt #2202)
    errors.extend(validate_invariants_block(sidecar, sidecar_path, warnings=warnings))
    errors.extend(validate_sanity_block(sidecar, sidecar_path, warnings=warnings))

    # Guard against unmeasured prose data-quality/filtering claims (debt #2662)
    errors.extend(validate_prose_data_quality_claims(sidecar, sidecar_path))

    # Validate factor/exclusion declarations & n_units_informative convention (debt #2200)
    errors.extend(validate_factor_declarations(sidecar, sidecar_path, warnings=warnings))

    # Cross-check claim_discriminates/claim_isolates if a registered claim was supplied (#3719)
    if claim is not None:
        errors.extend(validate_claim_discriminability(sidecar, claim, sidecar_path))

    # Validate [breakdown] / [result_schema_meta] and surface the aggregate advisories (spec 261026)
    breakdown_errors, breakdown_warnings = validate_breakdown_block(sidecar)
    errors.extend(breakdown_errors)
    warnings.extend(breakdown_warnings)

    # Validate [holdout] and [provenance].derived_from_observation (spec 261015 §3.4, S4)
    validate_holdout_and_observation(
        sidecar,
        sidecar_path,
        cls,
        catalog_dir=catalog_dir,
        errors=errors,
    )

    # Validate run:<id>:<field> calibration sources when catalog_dir is supplied (spec §3.5, S5)
    if catalog_dir is not None and sidecar.kind == SidecarKind.EXPERIMENT:
        for label, spec in sidecar.outcomes.items():
            if not spec.source or "${" in spec.source:
                continue
            issue_code, issue_msg, _run_obj, _res = check_calibration_source_ref(
                spec.source, catalog_dir
            )
            if issue_code is not None and issue_msg:
                errors.append(ValidationError(f"outcomes.{label}.source", issue_msg))

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
