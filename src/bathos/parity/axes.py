"""Five badge axes, clause formation, and gate-status computation (WP05d).

Implements Spec 261123 §9.3.1, §9.3.2, §9.3.4 (merging/ordering), and §12.4.1.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from bathos.parity.schema import (
    ALL_G_CODES,
    APPLICABLE_G_CODES,
    DATA_TIER_RANK,
    PARITY_KIND_ORDER_INDEX,
)

RING0_ACTIVE_G_CODES: frozenset[str] = frozenset({"G01", "G02", "G04"})

_STAGE_ORDER = {"shadow": 0, "advisory": 1, "enforce": 2}
_RESULT_ORDER = {
    "NO RESULT": 0,
    "FAILS": 1,
    "CONTESTED": 2,
    "INCONCLUSIVE": 3,
    "PASSES EXCEPT DECLARED DEVIATIONS": 4,
    "VALID EXCEPT DECLARED DEVIATIONS": 4,
    "PASSES": 5,
    "VALID": 5,
}


def build_gate_records(
    *,
    active_g_codes: Iterable[str] = RING0_ACTIVE_G_CODES,
    gate_stages: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """Return the 26 G-code gate status objects (stage or ``not_active``)."""
    active_set = set(active_g_codes)
    stages = dict(gate_stages or {})
    records: list[dict[str, str]] = []
    for code in ALL_G_CODES:
        if code in active_set:
            stage = stages.get(code, "shadow")
            if stage not in _STAGE_ORDER:
                stage = "shadow"
            records.append({"code": code, "stage": stage})
        else:
            records.append({"code": code, "stage": "not_active"})
    return records


def _result_sort_rank(result_val: str) -> int:
    if result_val.startswith("PASSES EXCEPT DECLARED DEVIATIONS"):
        return 4
    if result_val.startswith("VALID EXCEPT DECLARED DEVIATIONS"):
        return 4
    return _RESULT_ORDER.get(result_val, 99)


def _compute_result_axis(
    v_cells: list[dict[str, Any]],
    d_cells: list[dict[str, Any]],
    *,
    is_experimental_data: bool,
) -> dict[str, Any]:
    pass_word = "VALID" if is_experimental_data else "PASSES"
    if not v_cells:
        val = "NO RESULT"
    elif any(c["verdict"] == "fail" for c in v_cells):
        val = "FAILS"
    elif any(c["verdict"] == "contested" for c in v_cells):
        val = "CONTESTED"
    elif any(c["verdict"] == "inconclusive" for c in v_cells):
        val = "INCONCLUSIVE"
    elif d_cells:
        val = f"{pass_word} EXCEPT DECLARED DEVIATIONS ({len(d_cells)})"
    else:
        val = pass_word
    return {
        "value": val,
        "v_count": len(v_cells),
        "d_count": len(d_cells),
        "axis_basis_pending": [],
    }


def _axis_signature(clause: Mapping[str, Any]) -> tuple[Any, ...]:
    axes = clause["axes"]
    cov = axes["coverage"]
    assur = axes["assurance"]
    fresh = axes["freshness"]
    reg = axes["registration"]
    res_val = str(axes["result"]["value"])
    if res_val.startswith("PASSES EXCEPT DECLARED DEVIATIONS"):
        res_sig = "PASSES EXCEPT DECLARED DEVIATIONS"
    elif res_val.startswith("VALID EXCEPT DECLARED DEVIATIONS"):
        res_sig = "VALID EXCEPT DECLARED DEVIATIONS"
    else:
        res_sig = res_val
    return (
        clause.get("fallback_from"),
        clause.get("incompatibility_class"),
        res_sig,
        cov["value"],
        cov["knobs_uncovered"],
        assur["value"],
        assur["pending_count"],
        assur["lowest_stage"],
        fresh["value"],
        reg["value"],
    )


def compute_axes_and_clauses(
    *,
    declaration: Mapping[str, Any],
    cells: Sequence[Mapping[str, Any]],
    active_g_codes: Iterable[str] = RING0_ACTIVE_G_CODES,
    gate_stages: Mapping[str, str] | None = None,
    baseline_sha: str | None = None,
    graded_sha: str | None = None,
    has_registered_declaration: bool = False,
    knobs_uncovered: int | None = None,
    knobs_non_excluded: int | None = None,
    knobs_covered: int | None = None,
    accounted_differences: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, Any]]:
    """Compute clauses with five axes, 26 gate records, and cell_counts per §9.3."""
    active_set = frozenset(active_g_codes)
    gate_records = build_gate_records(active_g_codes=active_set, gate_stages=gate_stages)
    pending_codes = [c for c in APPLICABLE_G_CODES if c not in active_set]
    active_applicable_stages = [
        rec["stage"]
        for rec in gate_records
        if rec["code"] in APPLICABLE_G_CODES and rec["stage"] != "not_active"
    ]

    parity_cfg = declaration.get("parity", {})
    badge_min_tier = str(parity_cfg.get("badge_min_tier", "production_shape"))
    min_rank = DATA_TIER_RANK.get(badge_min_tier, 2)
    refs = declaration.get("reference", [])
    is_experimental = bool(refs and refs[0].get("kind") == "experimental_data")

    # Registration comparison (§9.3.2 step 5)
    unaccounted_diff = bool(
        baseline_sha
        and graded_sha
        and graded_sha != baseline_sha
        and not accounted_differences
    )
    if has_registered_declaration:
        reg_val = "amended" if unaccounted_diff else "prospective"
    else:
        reg_val = "amended" if unaccounted_diff else "retrospective"
    reg_pending = [c for c in ("G03", "G26") if c not in active_set]

    # Assurance axis (identical across clauses at std 1.0.0)
    if pending_codes:
        assur_axis: dict[str, Any] = {
            "value": "PROVISIONAL",
            "pending_count": len(pending_codes),
            "lowest_stage": None,
            "axis_basis_pending": list(pending_codes),
        }
    else:
        lowest = min(active_applicable_stages, key=lambda s: _STAGE_ORDER[s]) if active_applicable_stages else "shadow"
        assur_axis = {
            "value": "gated",
            "pending_count": 0,
            "lowest_stage": lowest,
            "axis_basis_pending": [],
        }

    # Group counted cells by (effective_kind, fallback_from, incompatibility_class)
    # Include planned kinds even if 0 counted cells? Only declared counted cells form clauses,
    # and WP03a guarantees at least 1 required cell at or above badge_min_tier.
    grouped: dict[tuple[str, str | None, str | None], list[dict[str, Any]]] = {}
    all_counted: list[dict[str, Any]] = []
    for raw_cell in cells:
        cell = dict(raw_cell)
        cell_tier = str(cell.get("data_tier", "toy"))
        tier_rank = DATA_TIER_RANK.get(cell_tier, 0)
        is_exp_flag = bool(cell.get("exploratory", False)) and has_registered_declaration
        if tier_rank < min_rank or is_exp_flag:
            continue
        all_counted.append(cell)
        key = (
            str(cell["declared_kind"]),
            cell.get("fallback_from"),
            cell.get("incompatibility_class"),
        )
        grouped.setdefault(key, []).append(cell)

    unmerged_clauses: list[dict[str, Any]] = []
    for (kind, fallback_from, incompat_cls), grp_cells in sorted(
        grouped.items(),
        key=lambda item: (PARITY_KIND_ORDER_INDEX.get(item[0][0], 99), item[0][1] or "", item[0][2] or ""),
    ):
        r_cells = [
            c
            for c in grp_cells
            if c["verdict"] in ("pass", "fail", "not_run", "inconclusive", "contested")
        ]
        v_cells = [c for c in r_cells if c["verdict"] != "not_run"]
        d_cells = [c for c in grp_cells if c["verdict"] == "declared_deviation"]
        waived_cells = [c for c in grp_cells if c["verdict"] == "waived"]
        not_covered_cells = [c for c in grp_cells if c["verdict"] == "not_covered"]
        not_run_cnt = sum(1 for c in r_cells if c["verdict"] == "not_run")
        passing_cnt = sum(1 for c in r_cells if c["verdict"] == "pass")

        result_axis = _compute_result_axis(
            v_cells, d_cells, is_experimental_data=is_experimental
        )
        cov_full = (
            not_run_cnt == 0
            and len(not_covered_cells) == 0
            and knobs_uncovered is not None
            and knobs_uncovered == 0
        )
        cov_pending = [] if "G09" in active_set and knobs_uncovered is not None else ["G09"]
        coverage_axis = {
            "value": "full" if cov_full else "partial",
            "not_run": not_run_cnt,
            "not_covered": len(not_covered_cells),
            "knobs_uncovered": knobs_uncovered,
            "axis_basis_pending": cov_pending,
        }

        stale_cnt = sum(
            1
            for c in (r_cells + d_cells)
            if any(str(ov).startswith("stale") for ov in c.get("overlays", []))
        )
        fresh_axis = {
            "value": "stale" if stale_cnt > 0 else "current",
            "stale_count": stale_cnt,
            "axis_basis_pending": [] if "G22" in active_set else ["G22"],
        }

        reg_axis = {
            "value": reg_val,
            "has_registered_declaration": has_registered_declaration,
            "axis_basis_pending": list(reg_pending),
        }

        unmerged_clauses.append(
            {
                "kinds": [kind],
                "fallback_from": fallback_from,
                "incompatibility_class": incompat_cls,
                "passing": passing_cnt,
                "required": len(r_cells),
                "deviations": len(d_cells),
                "waived": len(waived_cells),
                "not_covered": len(not_covered_cells),
                "axes": {
                    "result": result_axis,
                    "coverage": coverage_axis,
                    "assurance": dict(assur_axis),
                    "freshness": fresh_axis,
                    "registration": reg_axis,
                },
            }
        )

    # Merge clauses with identical five axis values (except fallback clauses stay separate)
    merged: list[dict[str, Any]] = []
    by_sig: dict[tuple[Any, ...], dict[str, Any]] = {}
    for clause in unmerged_clauses:
        sig = _axis_signature(clause)
        if clause.get("fallback_from") is None and sig in by_sig:
            existing = by_sig[sig]
            for k in clause["kinds"]:
                if k not in existing["kinds"]:
                    existing["kinds"].append(k)
            existing["kinds"].sort(key=lambda k: PARITY_KIND_ORDER_INDEX.get(k, 99))
            existing["passing"] += clause["passing"]
            existing["required"] += clause["required"]
            existing["deviations"] += clause["deviations"]
            existing["waived"] += clause["waived"]
            existing["not_covered"] += clause["not_covered"]
            existing["axes"]["result"]["v_count"] += clause["axes"]["result"]["v_count"]
            existing["axes"]["result"]["d_count"] += clause["axes"]["result"]["d_count"]
            existing["axes"]["coverage"]["not_run"] += clause["axes"]["coverage"]["not_run"]
            existing["axes"]["coverage"]["not_covered"] += clause["axes"]["coverage"]["not_covered"]
            existing["axes"]["freshness"]["stale_count"] += clause["axes"]["freshness"]["stale_count"]
        else:
            merged.append(clause)
            if clause.get("fallback_from") is None:
                by_sig[sig] = clause

    for cl in merged:
        res_ax = cl["axes"]["result"]
        r_val = str(res_ax["value"])
        if r_val.startswith("PASSES EXCEPT DECLARED DEVIATIONS"):
            res_ax["value"] = f"PASSES EXCEPT DECLARED DEVIATIONS ({int(res_ax['d_count'])})"
        elif r_val.startswith("VALID EXCEPT DECLARED DEVIATIONS"):
            res_ax["value"] = f"VALID EXCEPT DECLARED DEVIATIONS ({int(res_ax['d_count'])})"

    merged.sort(
        key=lambda cl: (
            _result_sort_rank(cl["axes"]["result"]["value"]),
            min(PARITY_KIND_ORDER_INDEX.get(k, 99) for k in cl["kinds"]),
            cl.get("fallback_from") or "",
        )
    )
    for idx, cl in enumerate(merged):
        cl["clause_id"] = f"clause_{idx}"

    # Overall cell_counts across all counted cells
    by_kind: dict[str, dict[str, int]] = {}
    for c in all_counted:
        k = str(c["declared_kind"])
        bucket = by_kind.setdefault(k, {"required": 0, "passing": 0, "not_run": 0, "not_covered": 0, "waived": 0, "declared_deviations": 0})
        v = c["verdict"]
        if v in ("pass", "fail", "not_run", "inconclusive", "contested"):
            bucket["required"] += 1
        if v == "pass":
            bucket["passing"] += 1
        elif v == "not_run":
            bucket["not_run"] += 1
        elif v == "not_covered":
            bucket["not_covered"] += 1
        elif v == "waived":
            bucket["waived"] += 1
        elif v == "declared_deviation":
            bucket["declared_deviations"] += 1

    r_all = [c for c in all_counted if c["verdict"] in ("pass", "fail", "not_run", "inconclusive", "contested")]
    waived_cell_cnt = sum(1 for c in all_counted if c["verdict"] == "waived")
    cell_counts = {
        "required": len(r_all),
        "passing": sum(1 for c in r_all if c["verdict"] == "pass"),
        "by_kind": by_kind,
        "not_covered": sum(1 for c in all_counted if c["verdict"] == "not_covered"),
        "waived": waived_cell_cnt or len(declaration.get("waiver", [])),
        "not_run": sum(1 for c in r_all if c["verdict"] == "not_run"),
        "declared_deviations": sum(1 for c in all_counted if c["verdict"] == "declared_deviation"),
        "registry_knobs_non_excluded": knobs_non_excluded,
        "registry_knobs_covered": knobs_covered,
        "unplanned_knobs": knobs_uncovered,
    }

    return merged, gate_records, cell_counts
