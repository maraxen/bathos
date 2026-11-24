"""Minimal Ring 0 grader (`bth parity grade`, WP05d).

Implements Spec 261123 §3.2, §3.4, §3.11, §3.17.1-§3.17.3, §9.3.1-§9.3.2, and §12.2 WP05d:
- Active checks in Ring 0: G01, G02, G04 (stage = `shadow`)
- Partial checks executed in Ring 0 (kept in X until owning packages land):
  - planned-cell half of G09
  - elementwise/bitwise half of G10
- Per-cell overlay hook interface for WP05a/c/e/21b/44/45/55
- Dual display (`as first declared:`) whenever declaration sha differs from baseline
- Deterministic JSON output and `--verify` byte-for-byte replay check
- Tracked `bth` run recording in catalog when `track_run=True`
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from bathos.catalog import read_runs, write_run
from bathos.config import default_catalog_dir
from bathos.git import capture_git_state
from bathos.parity.axes import RING0_ACTIVE_G_CODES, compute_axes_and_clauses
from bathos.parity.declare import (
    load_declaration_baseline,
    load_declaration_version_meta,
    load_parsed_declaration,
)
from bathos.parity.evidence import load_cell_evidence_for_record, validate_cell_evidence
from bathos.parity.metrics import compare_bitwise, compare_within_tolerance, get_backend_identity
from bathos.parity.schema import (
    STANDARD_VERSION,
    ParityValidationError,
    compute_cell_id,
    validate_declaration,
)
from bathos.parity.sealed_dump import load_sealed_dump_arrays
from bathos.parity.store import ParityStore, atomic_write_bytes, require_parity_store
from bathos.schema import Run
from bathos.validity import ValidityResult, resolve_validity
from bathos.workspace import resolve_workspace

CellOverlayHook = Callable[[dict[str, Any], Mapping[str, Any]], Sequence[str]]

_REGISTERED_OVERLAY_HOOKS: list[CellOverlayHook] = []


def register_overlay_hook(hook: CellOverlayHook) -> None:
    """Register a module-level per-cell overlay hook for downstream WPs."""
    if hook not in _REGISTERED_OVERLAY_HOOKS:
        _REGISTERED_OVERLAY_HOOKS.append(hook)


class ParityVerifyError(RuntimeError):
    """Raised when ``bth parity grade --verify`` sees byte divergence on replay."""


@dataclass(frozen=True)
class GradeResult:
    """Return payload from :func:`grade_parity_record`."""

    record_id: str
    result_path: Path
    result: dict[str, Any]
    grade_run_id: str | None
    verified: bool


def _package_version() -> str:
    try:
        from bathos import __version__

        return str(__version__)
    except Exception:
        return "0.18.0"


def _resolve_decl_revision(store: ParityStore, decl_path: Path, record_id: str, decl_sha: str, first_meta: dict[str, Any] | None) -> str:
    ver_meta = load_declaration_version_meta(store, record_id, decl_sha)
    if ver_meta and ver_meta.get("head_commit"):
        return str(ver_meta["head_commit"])
    if first_meta and first_meta.get("head_commit"):
        return str(first_meta["head_commit"])
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(decl_path.parent),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except OSError:
        pass
    return "0" * 40


def _load_runs_by_id(catalog_dir: Path | None) -> dict[str, Run]:
    if catalog_dir is None or not catalog_dir.exists():
        return {}
    by_id: dict[str, Run] = {}
    for run in read_runs(catalog_dir):
        by_id[run.id] = run
    return by_id


def _load_bundle_arrays(store: ParityStore, bundle_sha: str) -> dict[str, np.ndarray]:
    npz_bytes = store.get_bytes(bundle_sha)
    with np.load(io.BytesIO(npz_bytes), allow_pickle=False) as z:
        return {k: np.asarray(z[k]) for k in sorted(z.files)}


def _select_tolerance(
    declaration: Mapping[str, Any],
    cell_plan_entry: Mapping[str, Any],
    arr_dtype_str: str,
) -> dict[str, Any] | None:
    tolerances = declaration.get("tolerance", [])
    if not tolerances:
        return None
    tol_id = cell_plan_entry.get("tolerance_id")
    if tol_id:
        for tol in tolerances:
            if tol.get("id") == tol_id:
                return dict(tol)
    norm_dtype = arr_dtype_str.lstrip("<>=|")
    dtype_aliases = {
        "f2": "float16",
        "f4": "float32",
        "f8": "float64",
        "float16": "float16",
        "float32": "float32",
        "float64": "float64",
    }
    friendly = dtype_aliases.get(norm_dtype, arr_dtype_str)
    comp_id = str(cell_plan_entry.get("component_id", "model"))
    for tol in tolerances:
        applies_to = tol.get("applies_to", [])
        if tol.get("dtype") in (friendly, arr_dtype_str) and (
            comp_id in applies_to or "*" in applies_to or not applies_to
        ):
            return dict(tol)
    return dict(tolerances[0])


def _target_revisions_from_decl(declaration: Mapping[str, Any], impl_rev: str) -> dict[str, Any]:
    refs = declaration.get("reference", [])
    ref_entry = refs[0] if refs else {}
    return {
        "implementation": impl_rev,
        "reference": {
            "id": ref_entry.get("id"),
            "kind": ref_entry.get("kind"),
            "identity": ref_entry.get("identity", {}),
        },
        "registry_id": declaration.get("registry", {}).get("registry_id"),
    }


def _evaluate_cell(
    *,
    declaration: Mapping[str, Any],
    plan_cell: Mapping[str, Any],
    use_obj: Mapping[str, Any],
    store: ParityStore,
    manifests_by_cell: Mapping[str, list[dict[str, Any]]],
    runs_by_id: Mapping[str, Run],
    validity_by_id: Mapping[str, ValidityResult],
    declared_cell_runs: Mapping[str, Sequence[str]],
    sealed_dump_cache: dict[str, dict[str, np.ndarray]],
    hooks: Sequence[CellOverlayHook],
) -> dict[str, Any]:
    comp_id = str(plan_cell["component_id"])
    use_id = str(plan_cell["use_id"])
    data_tier = str(plan_cell["data_tier"])
    pairing = str(plan_cell["pairing"])
    pooling_rule = str(plan_cell["pooling_rule"])
    cell_id = compute_cell_id(
        comp_id,
        use_id,
        plan_cell["knob_setting"],
        plan_cell["axis_value"],
        data_tier,
        pairing,
    )

    incompat = plan_cell.get("incompatibility")
    if isinstance(incompat, Mapping) and isinstance(incompat.get("fallback_use"), Mapping):
        fb_use = incompat["fallback_use"]
        declared_kind = str(fb_use["parity_kind"])
        fallback_from: str | None = str(use_obj["parity_kind"])
        incompat_cls: str | None = str(incompat["class"])
    else:
        declared_kind = str(use_obj["parity_kind"])
        fallback_from = None
        incompat_cls = None

    refs = declaration.get("reference", [])
    ref_entry = refs[0] if refs else {}
    ref_kind = str(ref_entry.get("kind", ""))

    cell_manifests = list(manifests_by_cell.get(cell_id, []))
    explicit_runs = list(declared_cell_runs.get(cell_id, ()))

    # Filter/inspect runs via G04: validity, terminal status, invariant_fail
    usable_manifests: list[dict[str, Any]] = []
    saw_non_terminal = False
    saw_invariant_fail = False
    saw_terminal_without_evidence = False
    evidence_run_ids: list[str] = []

    manifest_run_ids = [str(m["run_id"]) for m in cell_manifests]
    for m in cell_manifests:
        rid = str(m["run_id"])
        evidence_run_ids.append(rid)
        run = runs_by_id.get(rid)
        val = validity_by_id.get(rid)
        if val is not None and val.is_hidden:
            continue
        if run is not None:
            if run.status not in ("completed", "failed"):
                saw_non_terminal = True
                continue
            if run.outcome == "invariant_fail":
                saw_invariant_fail = True
        usable_manifests.append(m)

    for rid in explicit_runs:
        if rid not in evidence_run_ids:
            evidence_run_ids.append(rid)
        val = validity_by_id.get(rid)
        if val is not None and val.is_hidden:
            continue
        run = runs_by_id.get(rid)
        if run is None:
            continue
        if run.status not in ("completed", "failed"):
            saw_non_terminal = True
        elif rid not in manifest_run_ids:
            saw_terminal_without_evidence = True

    base_row: dict[str, Any] = {
        "cell_id": cell_id,
        "component_id": comp_id,
        "use_id": use_id,
        "knob_setting": plan_cell["knob_setting"],
        "axis_value": plan_cell["axis_value"],
        "data_tier": data_tier,
        "pairing": pairing,
        "pooling_rule": pooling_rule,
        "exploratory": bool(plan_cell.get("exploratory", False)),
        "declared_kind": declared_kind,
        "fallback_from": fallback_from,
        "incompatibility_class": incompat_cls,
        "achieved_kinds": [],
        "metric_values": {},
        "n": 0,
        "tolerance_ref": plan_cell.get("tolerance_id"),
        "evidence_run_ids": evidence_run_ids,
        "cell_evidence_sha256": None,
        "input_sha256": {"ours": [], "reference": []},
        "pin_set": {},
        "nondegeneracy": {"status": "not_checked_ring0"},
        "evidence_level": "self_reported",
        "overlays": [],
        "flags": [],
        "verdict": "not_run",
    }

    # G04 evidence-absence and invariant rules take precedence before G10
    if saw_invariant_fail:
        base_row["verdict"] = "fail"
        base_row["flags"] = ["invariant_fail"]
    elif not usable_manifests:
        if saw_terminal_without_evidence:
            base_row["verdict"] = "inconclusive"
            base_row["flags"] = ["run_failed"]
        elif saw_non_terminal or not evidence_run_ids:
            base_row["verdict"] = "not_run"
        else:
            # All manifests belonged to validity-hidden runs -> effectively no valid evidence
            base_row["verdict"] = "not_run"
    else:
        # Validate manifest integrity & run built-in comparison (G10 elementwise/bitwise half)
        selected_manifest = usable_manifests[-1]
        base_row["cell_evidence_sha256"] = selected_manifest.get("manifest_sha256")
        base_row["input_sha256"] = selected_manifest.get("input_sha256", {"ours": [], "reference": []})
        ingest_errors = validate_cell_evidence(selected_manifest, store=store)
        if ingest_errors:
            base_row["verdict"] = "fail"
            base_row["flags"] = ["digest_mismatch"]
        else:
            ours_arrays = _load_bundle_arrays(store, str(selected_manifest["ours_bundle_sha256"]))
            ref_arrays: dict[str, np.ndarray] = {}
            if selected_manifest.get("reference_bundle_sha256"):
                ref_arrays = _load_bundle_arrays(store, str(selected_manifest["reference_bundle_sha256"]))
            elif ref_kind == "sealed_dump":
                dump_sha = str(
                    selected_manifest.get("reference_dump_sha256")
                    or ref_entry.get("identity", {}).get("dump_sha256", "")
                )
                if dump_sha:
                    if dump_sha not in sealed_dump_cache:
                        sealed_dump_cache[dump_sha] = load_sealed_dump_arrays(store, dump_sha)
                    dump_all = sealed_dump_cache[dump_sha]
                    dump_key_map = selected_manifest.get("reference_dump_keys") or plan_cell.get("dump_keys") or {}
                    for out_name in ours_arrays:
                        ref_key = str(dump_key_map.get(out_name, out_name))
                        if ref_key in dump_all:
                            ref_arrays[out_name] = dump_all[ref_key]

            missing_ref = [k for k in ours_arrays if k not in ref_arrays]
            if missing_ref:
                base_row["verdict"] = "fail"
                base_row["flags"] = ["fixture_mismatch"]
            elif declared_kind in ("bitwise", "within_tolerance"):
                all_bitwise = True
                all_within_tol = True
                worst_abs = 0.0
                worst_rel = 0.0
                per_array_metrics: dict[str, Any] = {}
                tol_used: str | None = None
                total_elems = 0
                for arr_name in sorted(ours_arrays):
                    ours_a = ours_arrays[arr_name]
                    ref_a = ref_arrays[arr_name]
                    bw = compare_bitwise(ours_a, ref_a)
                    if not bw.equal:
                        all_bitwise = False
                    tol = _select_tolerance(declaration, plan_cell, ours_a.dtype.str)
                    if tol is not None:
                        tol_used = str(tol["id"])
                        ew = compare_within_tolerance(
                            ours_a,
                            ref_a,
                            atol=float(tol["atol"]),
                            rtol=float(tol["rtol"]),
                        )
                        if not ew.within_tolerance:
                            all_within_tol = False
                        if ew.max_abs_diff is not None:
                            worst_abs = max(worst_abs, ew.max_abs_diff)
                        if ew.max_rel_diff is not None:
                            worst_rel = max(worst_rel, ew.max_rel_diff)
                        total_elems += ew.n_elements
                        per_array_metrics[arr_name] = {
                            "bitwise_equal": bw.equal,
                            "within_tolerance": ew.within_tolerance,
                            "max_abs_diff": ew.max_abs_diff,
                            "max_rel_diff": ew.max_rel_diff,
                            "mean_abs_diff": ew.mean_abs_diff,
                            "n_exceeding": ew.n_exceeding,
                            "atol": float(tol["atol"]),
                            "rtol": float(tol["rtol"]),
                        }
                    else:
                        all_within_tol = all_bitwise
                        total_elems += int(ours_a.size)
                        per_array_metrics[arr_name] = {"bitwise_equal": bw.equal}

                achieved: list[str] = []
                if all_bitwise:
                    achieved.append("bitwise")
                if all_bitwise or all_within_tol:
                    achieved.append("within_tolerance")

                base_row["achieved_kinds"] = achieved
                base_row["tolerance_ref"] = tol_used
                base_row["n"] = total_elems
                base_row["metric_values"] = {
                    "max_abs_diff": worst_abs,
                    "max_rel_diff": worst_rel,
                    "arrays": per_array_metrics,
                }
                if declared_kind in achieved:
                    base_row["verdict"] = "pass"
                    base_row["flags"] = []
                else:
                    base_row["verdict"] = "fail"
                    base_row["flags"] = ["kind_shortfall"]
            else:
                # Other kinds wait for WP49c / WP05c in Ring 3
                base_row["verdict"] = "inconclusive"
                base_row["flags"] = ["kind_grader_not_active_ring0"]

    for hook in hooks:
        extra_overlays = hook(base_row, declaration)
        for ov in extra_overlays:
            if ov not in base_row["overlays"]:
                base_row["overlays"].append(ov)

    return base_row


def _build_result_payload(
    *,
    declaration: Mapping[str, Any],
    decl_sha: str,
    impl_rev: str,
    baseline_sha: str | None,
    first_decl: Mapping[str, Any] | None,
    store: ParityStore,
    catalog_dir: Path | None,
    evidence_run_ids: Sequence[str] | None,
    declared_cell_runs: Mapping[str, Sequence[str]],
    as_of: str,
    gate_stages: Mapping[str, str],
    comparison_backend: Mapping[str, Any],
    overlay_hooks: Sequence[CellOverlayHook],
) -> dict[str, Any]:
    record_id = str(declaration["parity"]["record_id"])
    uses_by_id = {str(u["id"]): u for u in declaration.get("use", [])}
    manifests = load_cell_evidence_for_record(store, record_id)
    if evidence_run_ids is not None:
        allowed_runs = set(evidence_run_ids)
        manifests = [m for m in manifests if m.get("run_id") in allowed_runs]

    manifests_by_cell: dict[str, list[dict[str, Any]]] = {}
    referenced_run_ids: list[str] = []
    for m in manifests:
        cid = str(m["cell_id"])
        manifests_by_cell.setdefault(cid, []).append(m)
        rid = str(m["run_id"])
        if rid not in referenced_run_ids:
            referenced_run_ids.append(rid)
    for rids in declared_cell_runs.values():
        for rid in rids:
            if rid not in referenced_run_ids:
                referenced_run_ids.append(rid)

    runs_by_id = _load_runs_by_id(catalog_dir)
    known_run_ids = [rid for rid in referenced_run_ids if rid in runs_by_id]
    validity_by_id: dict[str, ValidityResult] = {}
    if known_run_ids and catalog_dir is not None and (catalog_dir / "bathos.db").is_file():
        validity_by_id = resolve_validity(known_run_ids, catalog_dir=catalog_dir)

    evidence_runs_summary: list[dict[str, Any]] = []
    for rid in referenced_run_ids:
        run = runs_by_id.get(rid)
        val = validity_by_id.get(rid)
        evidence_runs_summary.append(
            {
                "run_id": rid,
                "status": run.status if run else "untracked_fixture",
                "outcome": run.outcome if run else "",
                "validity_state": val.state if val else "current",
                "is_hidden": bool(val.is_hidden) if val else False,
                "env_blob_sha256": None,
                "declaration_blob_sha256": decl_sha,
            }
        )

    all_hooks = [*_REGISTERED_OVERLAY_HOOKS, *overlay_hooks]
    sealed_dump_cache: dict[str, dict[str, np.ndarray]] = {}
    evaluated_cells: list[dict[str, Any]] = []
    for plan_cell in declaration.get("cell_plan", []):
        uid = str(plan_cell["use_id"])
        use_obj = uses_by_id[uid]
        evaluated_cells.append(
            _evaluate_cell(
                declaration=declaration,
                plan_cell=plan_cell,
                use_obj=use_obj,
                store=store,
                manifests_by_cell=manifests_by_cell,
                runs_by_id=runs_by_id,
                validity_by_id=validity_by_id,
                declared_cell_runs=declared_cell_runs,
                sealed_dump_cache=sealed_dump_cache,
                hooks=all_hooks,
            )
        )
    evaluated_cells.sort(key=lambda c: str(c["cell_id"]))

    clauses, gates, cell_counts = compute_axes_and_clauses(
        declaration=declaration,
        cells=evaluated_cells,
        active_g_codes=RING0_ACTIVE_G_CODES,
        gate_stages=gate_stages,
        baseline_sha=baseline_sha,
        graded_sha=decl_sha,
        has_registered_declaration=False,  # No ledger anchoring in Ring 0 (§3.17.3)
        knobs_uncovered=None,  # G09 WP05e not active in Ring 0
        knobs_non_excluded=None,
        knobs_covered=None,
        accounted_differences=False,
    )

    # Dual display: whenever graded declaration differs from first declaration (§3.17.3, WP05d)
    dual_display_result: dict[str, Any] | None = None
    if baseline_sha and first_decl is not None and decl_sha != baseline_sha:
        first_uses = {str(u["id"]): u for u in first_decl.get("use", [])}
        first_cells: list[dict[str, Any]] = []
        for plan_cell in first_decl.get("cell_plan", []):
            uid = str(plan_cell["use_id"])
            if uid not in first_uses:
                continue
            first_cells.append(
                _evaluate_cell(
                    declaration=first_decl,
                    plan_cell=plan_cell,
                    use_obj=first_uses[uid],
                    store=store,
                    manifests_by_cell=manifests_by_cell,
                    runs_by_id=runs_by_id,
                    validity_by_id=validity_by_id,
                    declared_cell_runs=declared_cell_runs,
                    sealed_dump_cache=sealed_dump_cache,
                    hooks=all_hooks,
                )
            )
        first_cells.sort(key=lambda c: str(c["cell_id"]))
        first_clauses, _first_gates, first_counts = compute_axes_and_clauses(
            declaration=first_decl,
            cells=first_cells,
            active_g_codes=RING0_ACTIVE_G_CODES,
            gate_stages=gate_stages,
            baseline_sha=baseline_sha,
            graded_sha=baseline_sha,
            has_registered_declaration=False,
            knobs_uncovered=None,
            accounted_differences=False,
        )
        dual_display_result = {
            "basis": "first_declared",
            "declaration_sha256": baseline_sha,
            "clauses": first_clauses,
            "cell_counts": first_counts,
        }

    # Derived results_seen (§3.17.1): true iff prior_runs non-empty OR any evidence run exists
    prior_runs = list(declaration.get("parity", {}).get("prior_runs", []))
    results_seen = bool(prior_runs or referenced_run_ids)

    grade_inputs = {
        "as_of": as_of,
        "target_revisions": _target_revisions_from_decl(declaration, impl_rev),
        "drift_run_id": None,
        "ledger_high_water_mark": None,
        "gate_stages": {g["code"]: g["stage"] for g in gates},
        "comparison_backend": dict(comparison_backend),
    }

    return {
        "schema_version": "parity_result.v1",
        "standard_version": STANDARD_VERSION,
        "record_id": record_id,
        "record_sha256": decl_sha,
        "ledger_entry_id": None,
        "grader_version": _package_version(),
        "results_seen": results_seen,
        "grade_inputs": grade_inputs,
        "evidence_runs": evidence_runs_summary,
        "cells": evaluated_cells,
        "components": [],
        "supplementary": [],
        "checks_admissibility": [],
        "cell_counts": cell_counts,
        "clauses": clauses,
        "gates": gates,
        "amendments": [],
        "dual_display_result": dual_display_result,
        "prior_records_on_pair": len(prior_runs),
        "bundle_sha256": None,
    }


def serialize_result(result: Mapping[str, Any]) -> bytes:
    """Deterministically serialize a result dictionary to UTF-8 JSON bytes."""
    text = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    return text.encode("utf-8")


def _default_result_path(decl_path: Path) -> Path:
    name = decl_path.name
    stem = name[: -len(".parity.bth.toml")] if name.endswith(".parity.bth.toml") else decl_path.stem
    return decl_path.parent / f"{stem}.parity.result.json"


def grade_parity_record(
    decl_path: Path | str,
    *,
    parity_store: Path | str | None = None,
    catalog_dir: Path | str | None = None,
    evidence_run_ids: Sequence[str] | None = None,
    cell_runs: Mapping[str, Sequence[str]] | None = None,
    as_of: str | None = None,
    output_path: Path | str | None = None,
    verify: bool = False,
    track_run: bool = True,
    overlay_hooks: Sequence[CellOverlayHook] = (),
) -> GradeResult:
    """Grade a parity declaration against stored ``cell_evidence.v1`` (WP05d)."""
    dpath = Path(decl_path).resolve()
    raw_bytes, declaration = load_parsed_declaration(dpath)
    issues = validate_declaration(declaration)
    if issues:
        raise ParityValidationError(issues)

    decl_sha = hashlib.sha256(raw_bytes).hexdigest()
    record_id = str(declaration["parity"]["record_id"])
    out_p = Path(output_path).resolve() if output_path is not None else _default_result_path(dpath)

    store, _res = require_parity_store(explicit=parity_store, start_dir=dpath.parent)
    cat_dir: Path | None = None
    if catalog_dir is not None:
        cat_dir = Path(catalog_dir).resolve()
    elif os.environ.get("BTH_CATALOG_DIR"):
        cat_dir = Path(os.environ["BTH_CATALOG_DIR"]).resolve()
    else:
        cat_dir = default_catalog_dir()

    first_meta, first_decl = load_declaration_baseline(store, record_id)
    baseline_sha = str(first_meta["declaration_sha256"]) if first_meta else None
    impl_rev = _resolve_decl_revision(store, dpath, record_id, decl_sha, first_meta)

    if verify:
        if not out_p.is_file():
            raise ParityVerifyError(f"Cannot verify: result file not found at {out_p}")
        existing_bytes = out_p.read_bytes()
        try:
            existing_obj = json.loads(existing_bytes.decode("utf-8"))
        except Exception as exc:
            raise ParityVerifyError(f"Malformed result JSON at {out_p}: {exc}") from exc
        recorded_inputs = existing_obj.get("grade_inputs", {})
        replay_as_of = str(recorded_inputs.get("as_of") or datetime.now(UTC).isoformat())
        replay_gate_stages = dict(recorded_inputs.get("gate_stages") or {})
        replay_backend = dict(recorded_inputs.get("comparison_backend") or get_backend_identity())
        replay_ev_runs = (
            evidence_run_ids
            if evidence_run_ids is not None
            else ([r["run_id"] for r in existing_obj.get("evidence_runs", [])] if existing_obj.get("evidence_runs") else None)
        )
        replayed = _build_result_payload(
            declaration=declaration,
            decl_sha=decl_sha,
            impl_rev=impl_rev,
            baseline_sha=baseline_sha,
            first_decl=first_decl,
            store=store,
            catalog_dir=cat_dir,
            evidence_run_ids=replay_ev_runs,
            declared_cell_runs=cell_runs or {},
            as_of=replay_as_of,
            gate_stages=replay_gate_stages,
            comparison_backend=replay_backend,
            overlay_hooks=overlay_hooks,
        )
        # Preserve stored declaration if verify re-read a baseline? If record_sha256 differs, bytes differ!
        replayed_bytes = serialize_result(replayed)
        if replayed_bytes != existing_bytes:
            raise ParityVerifyError(
                f"Result verification failed: regenerated bytes differ from {out_p}"
            )
        return GradeResult(
            record_id=record_id,
            result_path=out_p,
            result=replayed,
            grade_run_id=None,
            verified=True,
        )

    effective_as_of = as_of or datetime.now(UTC).isoformat()
    payload = _build_result_payload(
        declaration=declaration,
        decl_sha=decl_sha,
        impl_rev=impl_rev,
        baseline_sha=baseline_sha,
        first_decl=first_decl,
        store=store,
        catalog_dir=cat_dir,
        evidence_run_ids=evidence_run_ids,
        declared_cell_runs=cell_runs or {},
        as_of=effective_as_of,
        gate_stages={code: "shadow" for code in RING0_ACTIVE_G_CODES},
        comparison_backend=get_backend_identity(),
        overlay_hooks=overlay_hooks,
    )
    out_bytes = serialize_result(payload)
    atomic_write_bytes(out_p, out_bytes)

    grade_run_id: str | None = None
    if track_run and cat_dir is not None:
        ws = resolve_workspace(dpath.parent)
        try:
            git_state = capture_git_state(ws.fs_root)
            git_hash = str(getattr(git_state, "git_hash", None) or getattr(git_state, "commit", "failed:not_a_repo"))
            git_branch = str(getattr(git_state, "git_branch", None) or getattr(git_state, "branch", "unknown"))
            git_dirty = bool(getattr(git_state, "git_dirty", getattr(git_state, "dirty", False)))
        except Exception:
            git_hash = "failed:not_a_repo"
            git_branch = "unknown"
            git_dirty = False
        slug = ws.slug or "bathos"
        bth_toml = ws.fs_root / ".bth.toml"
        if bth_toml.is_file():
            try:
                cfg = tomllib.loads(bth_toml.read_text(encoding="utf-8"))
                slug = str(cfg.get("project", {}).get("slug") or slug)
            except Exception:
                pass
        run = Run(
            project_slug=slug,
            command=f"bth parity grade {dpath.name}",
            argv=["bth", "parity", "grade", str(dpath)],
            git_hash=git_hash,
            git_branch=git_branch,
            git_dirty=git_dirty,
            exit_code=0,
            status="completed",
            output_paths=[str(out_p)],
            tags=["parity_grade", f"record:{record_id}"],
        )
        write_run(run, cat_dir)
        grade_run_id = run.id

    return GradeResult(
        record_id=record_id,
        result_path=out_p,
        result=payload,
        grade_run_id=grade_run_id,
        verified=False,
    )
