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
import math
import os
import tomllib
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from bathos.archived_items import read_archived_item_fragments
from bathos.catalog import write_run
from bathos.config import default_catalog_dir
from bathos.git import capture_git_state
from bathos.parity.axes import RING0_ACTIVE_G_CODES, compute_axes_and_clauses
from bathos.parity.declare import (
    ParityDeclareError,
    load_declaration_baseline,
    load_declaration_version_meta,
    load_parsed_declaration,
)
from bathos.parity.evidence import (
    CellEvidenceError,
    load_cell_evidence_for_record,
    validate_cell_evidence,
)
from bathos.parity.metrics import compare_bitwise, compare_within_tolerance, get_backend_identity
from bathos.parity.schema import (
    PARITY_KINDS_ORDER,
    STANDARD_VERSION,
    ParityValidationError,
    canonical_json,
    compute_cell_id,
    validate_declaration,
)
from bathos.parity.sealed_dump import SealedDumpError, load_sealed_dump_arrays
from bathos.parity.store import (
    ParityStore,
    ParityStoreResolution,
    atomic_write_bytes,
    require_parity_store,
    resolve_start_dir,
)
from bathos.query import AmbiguousRunId, _fold_once, _resolve_backend, get_run
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
    import bathos

    ver = str(bathos.__version__).strip()
    if not ver:
        raise RuntimeError("bathos.__version__ is empty")
    return ver


def _resolve_record_project_slug(dpath: Path, declaration: Mapping[str, Any]) -> str:
    from bathos.config import find_project_config

    cfg_path = find_project_config(dpath.parent)
    if cfg_path is not None and cfg_path.is_file():
        try:
            cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
            slug = str(cfg.get("project", {}).get("slug", "")).strip()
            if slug:
                return slug
        except Exception:
            pass
    return str(declaration.get("implementation", {}).get("name", "")).strip()


def _derive_cell_evidence_level(
    per_run_evals: Sequence[Mapping[str, Any]],
    *,
    overlays: Sequence[str] = (),
    flags: Sequence[str] = (),
    reexecuted: bool = False,
) -> str:
    if not per_run_evals:
        return "none"
    if (
        reexecuted
        or "reexecuted" in overlays
        or "reexecuted" in flags
        or all(ev.get("evidence_level") == "reexecuted" for ev in per_run_evals)
    ):
        return "reexecuted"
    return "self_reported"


def _resolve_decl_revision(
    store: ParityStore,
    record_id: str,
    decl_sha: str,
    first_meta: dict[str, Any] | None,
) -> str:
    ver_meta = load_declaration_version_meta(store, record_id, decl_sha)
    if ver_meta and ver_meta.get("head_commit"):
        return str(ver_meta["head_commit"])
    if first_meta and first_meta.get("head_commit"):
        return str(first_meta["head_commit"])
    raise ParityDeclareError(
        f"Record {record_id!r} (sha {decl_sha[:12]}) is missing declared head_commit in store {store.root}; "
        "run 'bth parity declare' first"
    )


def _hook_identity(hook: CellOverlayHook) -> str:
    mod = getattr(hook, "__module__", "") or ""
    qual = getattr(hook, "__qualname__", None) or getattr(hook, "__name__", None) or repr(hook)
    return f"{mod}:{qual}" if mod else str(qual)


def _load_runs_by_id(catalog_dir: Path | None, run_ids: Sequence[str]) -> dict[str, Run]:
    if catalog_dir is None or not catalog_dir.exists() or not run_ids:
        return {}
    by_id: dict[str, Run] = {}
    ingest_state: dict[str, Any] = {}
    backend = _resolve_backend(catalog_dir)
    for rid in dict.fromkeys(str(r) for r in run_ids if r):
        try:
            run = get_run(rid, catalog_dir, allow_cool_fallback=True, include_superseded=True)
        except AmbiguousRunId:
            run = None
        if run is None and backend == "index" and _fold_once(catalog_dir, ingest_state):
            try:
                run = get_run(rid, catalog_dir, allow_cool_fallback=True, include_superseded=True)
            except AmbiguousRunId:
                run = None
        if run is not None and run.id == rid:
            by_id[rid] = run
    return by_id


def _load_bundle_arrays(store: ParityStore, bundle_sha: str) -> dict[str, np.ndarray]:
    npz_bytes = store.get_bytes(bundle_sha)
    with np.load(io.BytesIO(npz_bytes), allow_pickle=False) as z:
        return {k: np.asarray(z[k]) for k in sorted(z.files)}


_DTYPE_ALIASES: dict[str, str] = {
    "b1": "bool",
    "bool": "bool",
    "bool_": "bool",
    "u1": "uint8",
    "uint8": "uint8",
    "i1": "int8",
    "int8": "int8",
    "u2": "uint16",
    "uint16": "uint16",
    "i2": "int16",
    "int16": "int16",
    "u4": "uint32",
    "uint32": "uint32",
    "i4": "int32",
    "int32": "int32",
    "u8": "uint64",
    "uint64": "uint64",
    "i8": "int64",
    "int64": "int64",
    "f2": "float16",
    "float16": "float16",
    "f4": "float32",
    "float32": "float32",
    "f8": "float64",
    "float64": "float64",
    "c8": "complex64",
    "complex64": "complex64",
    "c16": "complex128",
    "complex128": "complex128",
}


def _tol_matches(
    tol: Mapping[str, Any],
    *,
    friendly_dtype: str,
    raw_dtype_str: str,
    comp_id: str,
) -> bool:
    tol_dtype = str(tol.get("dtype", "")).strip()
    tol_friendly = _DTYPE_ALIASES.get(tol_dtype.lstrip("<>=|"), tol_dtype)
    if tol_dtype not in (friendly_dtype, raw_dtype_str) and tol_friendly != friendly_dtype:
        return False
    applies_to = tol.get("applies_to")
    if not isinstance(applies_to, Sequence) or isinstance(applies_to, (str, bytes)) or not applies_to:
        return False
    return comp_id in applies_to or "*" in applies_to


def _select_tolerance(
    declaration: Mapping[str, Any],
    cell_plan_entry: Mapping[str, Any],
    arr_dtype_str: str,
) -> dict[str, Any] | None:
    tolerances = declaration.get("tolerance", [])
    if not tolerances:
        return None
    norm_dtype = arr_dtype_str.lstrip("<>=|")
    friendly = _DTYPE_ALIASES.get(norm_dtype, arr_dtype_str)
    comp_id = str(cell_plan_entry.get("component_id", "model"))
    tol_id = cell_plan_entry.get("tolerance_id")
    if tol_id:
        for tol in tolerances:
            if tol.get("id") == tol_id:
                if _tol_matches(tol, friendly_dtype=friendly, raw_dtype_str=arr_dtype_str, comp_id=comp_id):
                    return dict(tol)
                return None
        return None
    for tol in tolerances:
        if _tol_matches(tol, friendly_dtype=friendly, raw_dtype_str=arr_dtype_str, comp_id=comp_id):
            return dict(tol)
    return None


def _finite_or_none(val: float | None) -> float | None:
    if val is None or not math.isfinite(val):
        return None
    return float(val)


def _pair_identity_key(declaration: Mapping[str, Any]) -> tuple[str, str, str]:
    impl_repo = str(declaration.get("implementation", {}).get("repo", "")).strip()
    refs = declaration.get("reference", [])
    primary_ref = refs[0] if refs and isinstance(refs[0], Mapping) else {}
    ref_kind = str(primary_ref.get("kind", "")).strip()
    ref_identity = primary_ref.get("identity", {})
    ident_json = canonical_json(ref_identity) if isinstance(ref_identity, Mapping) else "{}"
    return (impl_repo, ref_kind, ident_json)


def _count_prior_records_on_pair(
    store: ParityStore,
    declaration: Mapping[str, Any],
    current_record_id: str,
) -> int:
    """Count distinct other record_ids in store sharing (implementation.repo, comparator identity) (§3.17.1)."""
    decl_root = store.root / "declarations"
    if not decl_root.is_dir():
        return 0
    target_key = _pair_identity_key(declaration)
    if not target_key[0]:
        return 0
    count = 0
    for rec_dir in sorted(decl_root.iterdir()):
        if not rec_dir.is_dir() or rec_dir.name == current_record_id:
            continue
        meta_files: list[Path] = []
        first_file = rec_dir / "first.json"
        if first_file.is_file():
            meta_files.append(first_file)
        for jf in sorted(rec_dir.glob("*.json")):
            if jf != first_file:
                meta_files.append(jf)
        matched = False
        for mf in meta_files:
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ParityDeclareError(f"Failed to read prior declaration metadata {mf}: {exc}") from exc
            if not isinstance(meta, Mapping):
                raise ParityDeclareError(f"Prior declaration metadata {mf} must be a JSON object")
            decl_sha = meta.get("declaration_sha256")
            if not isinstance(decl_sha, str) or not decl_sha.strip():
                raise ParityDeclareError(f"Prior declaration metadata {mf} missing valid declaration_sha256")
            try:
                other_decl = tomllib.loads(store.get_bytes(decl_sha).decode("utf-8"))
            except (FileNotFoundError, ValueError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
                raise ParityDeclareError(
                    f"Failed to load prior declaration blob {decl_sha} referenced by {mf}: {exc}"
                ) from exc
            if _pair_identity_key(other_decl) == target_key:
                matched = True
                break
        if matched:
            count += 1
    return count


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


def _evaluate_single_manifest(
    *,
    manifest: Mapping[str, Any],
    declaration: Mapping[str, Any],
    plan_cell: Mapping[str, Any],
    declared_kind: str,
    ref_kind: str,
    ref_entry: Mapping[str, Any],
    store: ParityStore,
    sealed_dump_cache: dict[str, dict[str, np.ndarray]],
) -> dict[str, Any]:
    rid = str(manifest["run_id"])
    m_sha = manifest.get("manifest_sha256")
    inp = manifest.get("input_sha256", {"ours": [], "reference": []})
    input_key = (
        tuple(sorted(str(x) for x in inp.get("ours", []))),
        tuple(sorted(str(x) for x in inp.get("reference", []))),
    )
    if ref_kind == "sealed_dump":
        ref_source: str | None = "sealed_dump"
    elif manifest.get("reference_bundle_sha256"):
        ref_source = "evidence_bundle"
    else:
        ref_source = None
    ingest_errors = validate_cell_evidence(manifest, store=store)
    if ingest_errors:
        return {
            "run_id": rid,
            "manifest_sha256": m_sha,
            "verdict": "fail",
            "flags": ["digest_mismatch"],
            "achieved_kinds": [],
            "max_abs_diff": None,
            "max_rel_diff": None,
            "saw_nonfinite_abs": True,
            "saw_nonfinite_rel": True,
            "per_array_metrics": {},
            "tolerance_ref": plan_cell.get("tolerance_id"),
            "n": 0,
            "nondegeneracy": {"status": "not_checked_ring0"},
            "input_key": input_key,
            "input_sha256": inp,
            "reference_source": ref_source,
        }

    ours_arrays: dict[str, np.ndarray] = {}
    ref_arrays: dict[str, np.ndarray] = {}
    missing_ours: list[str] = []
    load_flag: str | None = None

    try:
        ours_arrays = _load_bundle_arrays(store, str(manifest["ours_bundle_sha256"]))
    except (FileNotFoundError, ValueError, KeyError, zipfile.BadZipFile, EOFError, OSError):
        load_flag = "digest_mismatch"

    if load_flag is None:
        if ref_kind == "sealed_dump":
            # NEVER load reference_bundle_sha256 for sealed_dump; require authoritative identity.dump_sha256.
            authoritative_dump_sha = str(
                ref_entry.get("identity", {}).get("dump_sha256", "")
            ).strip().lower()
            manifest_dump_sha = manifest.get("reference_dump_sha256")
            if not authoritative_dump_sha or (
                manifest_dump_sha is not None
                and str(manifest_dump_sha).strip().lower() != authoritative_dump_sha
            ):
                load_flag = "fixture_mismatch"
            else:
                try:
                    if authoritative_dump_sha not in sealed_dump_cache:
                        sealed_dump_cache[authoritative_dump_sha] = load_sealed_dump_arrays(
                            store, authoritative_dump_sha
                        )
                    dump_all = sealed_dump_cache[authoritative_dump_sha]
                except (
                    SealedDumpError,
                    FileNotFoundError,
                    ValueError,
                    zipfile.BadZipFile,
                    EOFError,
                    OSError,
                ):
                    load_flag = "digest_mismatch"
                else:
                    decl_ref_members = plan_cell.get("reference_members")
                    if isinstance(decl_ref_members, Mapping) and decl_ref_members:
                        required_map = {str(k): str(v) for k, v in decl_ref_members.items()}
                    else:
                        required_map = {k: k for k in sorted(dump_all)}
                    manifest_map_raw = manifest.get("reference_dump_keys")
                    manifest_map_mismatch = False
                    if manifest_map_raw is not None:
                        if not isinstance(manifest_map_raw, Mapping):
                            manifest_map_mismatch = True
                        else:
                            manifest_map = {str(k): str(v) for k, v in manifest_map_raw.items()}
                            if manifest_map != required_map:
                                manifest_map_mismatch = True
                    missing_dump_members = [
                        ref_key for ref_key in required_map.values() if ref_key not in dump_all
                    ]
                    if (
                        manifest_map_mismatch
                        or missing_dump_members
                        or set(ours_arrays.keys()) != set(required_map.keys())
                    ):
                        load_flag = "fixture_mismatch"
                    else:
                        ref_arrays = {
                            out_name: dump_all[ref_key]
                            for out_name, ref_key in required_map.items()
                        }
                        missing_ours = []
        elif manifest.get("reference_bundle_sha256"):
            try:
                ref_arrays = _load_bundle_arrays(store, str(manifest["reference_bundle_sha256"]))
            except (FileNotFoundError, ValueError, KeyError, zipfile.BadZipFile, EOFError, OSError):
                load_flag = "digest_mismatch"
            else:
                missing_ours = [k for k in ref_arrays if k not in ours_arrays]

    if load_flag is not None:
        return {
            "run_id": rid,
            "manifest_sha256": m_sha,
            "verdict": "fail",
            "flags": [load_flag],
            "achieved_kinds": [],
            "max_abs_diff": None,
            "max_rel_diff": None,
            "saw_nonfinite_abs": True,
            "saw_nonfinite_rel": True,
            "per_array_metrics": {},
            "tolerance_ref": plan_cell.get("tolerance_id"),
            "n": 0,
            "nondegeneracy": {"status": "not_checked_ring0"},
            "input_key": input_key,
            "input_sha256": inp,
            "reference_source": ref_source,
        }

    missing_ref = [k for k in ours_arrays if k not in ref_arrays]
    if missing_ref or missing_ours:
        return {
            "run_id": rid,
            "manifest_sha256": m_sha,
            "verdict": "fail",
            "flags": ["fixture_mismatch"],
            "achieved_kinds": [],
            "max_abs_diff": None,
            "max_rel_diff": None,
            "saw_nonfinite_abs": True,
            "saw_nonfinite_rel": True,
            "per_array_metrics": {},
            "tolerance_ref": plan_cell.get("tolerance_id"),
            "n": 0,
            "nondegeneracy": {"status": "not_checked_ring0"},
            "input_key": input_key,
            "input_sha256": inp,
            "reference_source": ref_source,
        }

    if declared_kind not in ("bitwise", "within_tolerance"):
        return {
            "run_id": rid,
            "manifest_sha256": m_sha,
            "verdict": "inconclusive",
            "flags": ["kind_grader_not_active_ring0"],
            "achieved_kinds": [],
            "max_abs_diff": None,
            "max_rel_diff": None,
            "saw_nonfinite_abs": True,
            "saw_nonfinite_rel": True,
            "per_array_metrics": {},
            "tolerance_ref": plan_cell.get("tolerance_id"),
            "n": 0,
            "nondegeneracy": {"status": "not_checked_ring0"},
            "input_key": input_key,
            "input_sha256": inp,
            "reference_source": ref_source,
        }

    all_bitwise = True
    all_within_tol = True
    missing_tolerance = False
    saw_degenerate = False
    degenerate_reasons: list[str] = []
    worst_abs: float | None = None
    worst_rel: float | None = None
    saw_nonfinite_abs = False
    saw_nonfinite_rel = False
    per_array_metrics: dict[str, Any] = {}
    tol_used: str | None = None
    total_elems = 0

    for arr_name in sorted(ours_arrays):
        ours_a = ours_arrays[arr_name]
        ref_a = ref_arrays[arr_name]
        bw = compare_bitwise(ours_a, ref_a)
        if not bw.equal:
            all_bitwise = False
        if bw.degenerate or ours_a.size == 0 or ref_a.size == 0:
            saw_degenerate = True
            reason_txt = f"{arr_name}: {bw.reason or 'degenerate 0-element array'}"
            if reason_txt not in degenerate_reasons:
                degenerate_reasons.append(reason_txt)

        tol = _select_tolerance(declaration, plan_cell, ref_a.dtype.str)
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
            if ew.degenerate:
                saw_degenerate = True
                ew_reason = f"{arr_name}: {ew.reason}"
                if ew_reason not in degenerate_reasons:
                    degenerate_reasons.append(ew_reason)
            fin_abs = _finite_or_none(ew.max_abs_diff)
            fin_rel = _finite_or_none(ew.max_rel_diff)
            fin_mean = _finite_or_none(ew.mean_abs_diff)
            if fin_abs is None:
                saw_nonfinite_abs = True
            elif not saw_nonfinite_abs:
                worst_abs = fin_abs if worst_abs is None else max(worst_abs, fin_abs)
            if fin_rel is None:
                saw_nonfinite_rel = True
            elif not saw_nonfinite_rel:
                worst_rel = fin_rel if worst_rel is None else max(worst_rel, fin_rel)
            total_elems += ew.n_elements
            per_array_metrics[arr_name] = {
                "bitwise_equal": bw.equal,
                "within_tolerance": ew.within_tolerance,
                "dtype_mismatch": ew.dtype_mismatch,
                "degenerate": ew.degenerate or bw.degenerate,
                "max_abs_diff": fin_abs,
                "max_rel_diff": fin_rel,
                "mean_abs_diff": fin_mean,
                "n_exceeding": ew.n_exceeding,
                "atol": float(tol["atol"]),
                "rtol": float(tol["rtol"]),
            }
        elif declared_kind == "within_tolerance":
            missing_tolerance = True
            all_within_tol = False
            saw_nonfinite_abs = True
            saw_nonfinite_rel = True
            total_elems += int(ours_a.size)
            per_array_metrics[arr_name] = {
                "bitwise_equal": bw.equal,
                "dtype_mismatch": ours_a.dtype.newbyteorder("<") != ref_a.dtype.newbyteorder("<"),
                "degenerate": bw.degenerate,
                "tolerance_missing": True,
            }
        else:
            all_within_tol = all_bitwise
            total_elems += int(ours_a.size)
            per_array_metrics[arr_name] = {
                "bitwise_equal": bw.equal,
                "dtype_mismatch": ours_a.dtype.newbyteorder("<") != ref_a.dtype.newbyteorder("<"),
                "degenerate": bw.degenerate,
            }

    achieved: list[str] = []
    if not saw_degenerate:
        if all_bitwise:
            achieved.append("bitwise")
        if all_bitwise or (all_within_tol and not missing_tolerance):
            achieved.append("within_tolerance")

    if saw_degenerate:
        verdict = "inconclusive"
        flags = ["degenerate_capture"]
        nondeg: dict[str, Any] = {"status": "degenerate", "reasons": degenerate_reasons}
    elif missing_tolerance:
        verdict = "fail"
        flags = ["basis_insufficient"]
        nondeg = {"status": "not_checked_ring0"}
    elif declared_kind in achieved:
        verdict = "pass"
        flags = []
        nondeg = {"status": "not_checked_ring0"}
    else:
        verdict = "fail"
        flags = ["kind_shortfall"]
        nondeg = {"status": "not_checked_ring0"}

    return {
        "run_id": rid,
        "manifest_sha256": m_sha,
        "verdict": verdict,
        "flags": flags,
        "achieved_kinds": achieved,
        "max_abs_diff": None if saw_nonfinite_abs else worst_abs,
        "max_rel_diff": None if saw_nonfinite_rel else worst_rel,
        "saw_nonfinite_abs": saw_nonfinite_abs,
        "saw_nonfinite_rel": saw_nonfinite_rel,
        "per_array_metrics": per_array_metrics,
        "tolerance_ref": tol_used if tol_used is not None else plan_cell.get("tolerance_id"),
        "n": total_elems,
        "nondegeneracy": nondeg,
        "input_key": input_key,
        "input_sha256": inp,
        "reference_source": ref_source,
    }


def _aggregate_evals(evals: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    verdicts = [str(ev["verdict"]) for ev in evals]
    if "fail" in verdicts:
        agg_verdict = "fail"
    elif "inconclusive" in verdicts:
        agg_verdict = "inconclusive"
    else:
        agg_verdict = "pass"

    kind_sets = [set(ev["achieved_kinds"]) for ev in evals]
    common_kinds = set.intersection(*kind_sets) if kind_sets else set()
    agg_kinds = [k for k in PARITY_KINDS_ORDER if k in common_kinds]

    saw_nf_abs = any(bool(ev["saw_nonfinite_abs"]) or ev["max_abs_diff"] is None for ev in evals)
    saw_nf_rel = any(bool(ev["saw_nonfinite_rel"]) or ev["max_rel_diff"] is None for ev in evals)
    agg_abs: float | None = None
    agg_rel: float | None = None
    if not saw_nf_abs:
        abs_vals = [float(ev["max_abs_diff"]) for ev in evals if ev["max_abs_diff"] is not None]
        agg_abs = max(abs_vals) if abs_vals else None
    if not saw_nf_rel:
        rel_vals = [float(ev["max_rel_diff"]) for ev in evals if ev["max_rel_diff"] is not None]
        agg_rel = max(rel_vals) if rel_vals else None

    agg_flags: list[str] = []
    for ev in evals:
        for fl in ev["flags"]:
            if fl not in agg_flags:
                agg_flags.append(fl)

    deg_reasons: list[str] = []
    for ev in evals:
        nd = ev.get("nondegeneracy", {})
        if nd.get("status") == "degenerate":
            for r in nd.get("reasons", []):
                if r not in deg_reasons:
                    deg_reasons.append(r)

    return {
        "verdict": agg_verdict,
        "achieved_kinds": agg_kinds,
        "max_abs_diff": agg_abs,
        "max_rel_diff": agg_rel,
        "saw_nonfinite_abs": saw_nf_abs,
        "saw_nonfinite_rel": saw_nf_rel,
        "flags": agg_flags,
        "nondegeneracy": {"status": "degenerate", "reasons": deg_reasons} if deg_reasons else {"status": "not_checked_ring0"},
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
    expected_project_slug: str,
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

    # Filter/inspect runs via G04: catalog existence (B1), project ownership, validity, terminal status, invariant_fail
    usable_manifests: list[dict[str, Any]] = []
    saw_non_terminal = False
    saw_invariant_fail = False
    saw_failed_run_status = False
    saw_terminal_without_evidence = False
    saw_unknown_run = False
    saw_wrong_project = False
    evidence_run_ids: list[str] = []

    expected_slug_norm = expected_project_slug.strip().lower()
    manifest_run_ids = [str(m["run_id"]) for m in cell_manifests]
    for m in cell_manifests:
        rid = str(m["run_id"])
        evidence_run_ids.append(rid)
        run = runs_by_id.get(rid)
        val = validity_by_id.get(rid)
        if val is not None and val.is_hidden:
            continue
        if run is None:
            saw_unknown_run = True
            continue
        if expected_slug_norm and run.project_slug.strip().lower() != expected_slug_norm:
            saw_wrong_project = True
            continue
        if run.outcome == "invariant_fail":
            saw_invariant_fail = True
            usable_manifests.append(m)
            continue
        if run.status == "failed":
            saw_failed_run_status = True
            continue
        if run.status != "completed":
            saw_non_terminal = True
            continue
        usable_manifests.append(m)

    for rid in explicit_runs:
        if rid not in evidence_run_ids:
            evidence_run_ids.append(rid)
        val = validity_by_id.get(rid)
        if val is not None and val.is_hidden:
            continue
        run = runs_by_id.get(rid)
        if run is None:
            saw_unknown_run = True
            continue
        if expected_slug_norm and run.project_slug.strip().lower() != expected_slug_norm:
            saw_wrong_project = True
            continue
        if run.outcome == "invariant_fail":
            saw_invariant_fail = True
            continue
        if run.status == "failed":
            saw_failed_run_status = True
            continue
        if run.status != "completed":
            saw_non_terminal = True
            continue
        if rid not in manifest_run_ids:
            saw_terminal_without_evidence = True

    extra_flags: list[str] = []
    if saw_unknown_run:
        extra_flags.append("unknown_evidence_run")
    if saw_wrong_project:
        extra_flags.append("wrong_project_run")

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
        "cell_evidence_sha256s": [],
        "run_verdicts": [],
        "input_sha256": {"ours": [], "reference": []},
        "pin_set": {},
        "nondegeneracy": {"status": "not_checked_ring0"},
        "evidence_level": _derive_cell_evidence_level(()),
        "reference_source": "sealed_dump" if ref_kind == "sealed_dump" else None,
        "overlays": [],
        "flags": [],
        "verdict": "not_run",
    }

    per_run_evals: list[dict[str, Any]] = []
    # G04 evidence-absence, terminal-status, and invariant rules take precedence before G10
    if saw_invariant_fail:
        base_row["verdict"] = "fail"
        base_row["flags"] = ["invariant_fail", *extra_flags]
    elif saw_failed_run_status:
        base_row["verdict"] = "inconclusive"
        base_row["flags"] = ["run_failed", *extra_flags]
    elif saw_non_terminal:
        base_row["verdict"] = "not_run"
        base_row["flags"] = list(extra_flags)
    elif saw_terminal_without_evidence:
        base_row["verdict"] = "inconclusive"
        base_row["flags"] = ["run_failed", *extra_flags]
    elif not usable_manifests:
        base_row["verdict"] = "not_run"
        base_row["flags"] = list(extra_flags)
    else:
        # Sort deterministically by catalog run order (never writer-set created_at) (S2)
        usable_manifests.sort(
            key=lambda m: (
                str(runs_by_id[str(m["run_id"])].timestamp),
                str(m["run_id"]),
                str(m.get("manifest_sha256", "")),
            )
        )
        per_run_evals = [
            _evaluate_single_manifest(
                manifest=m,
                declaration=declaration,
                plan_cell=plan_cell,
                declared_kind=declared_kind,
                ref_kind=ref_kind,
                ref_entry=ref_entry,
                store=store,
                sealed_dump_cache=sealed_dump_cache,
            )
            for m in usable_manifests
        ]
        latest_manifest = usable_manifests[-1]
        latest_eval = per_run_evals[-1]

        if pooling_rule == "per_input":
            groups: dict[tuple[tuple[str, ...], tuple[str, ...]], list[dict[str, Any]]] = {}
            for ev in per_run_evals:
                groups.setdefault(ev["input_key"], []).append(ev)
            group_aggs = [_aggregate_evals(groups[k]) for k in sorted(groups)]
            agg = _aggregate_evals(group_aggs)
        else:
            agg = _aggregate_evals(per_run_evals)

        flags = list(agg["flags"])
        if len(per_run_evals) > 1:
            verdict_seq = [str(ev["verdict"]) for ev in per_run_evals]
            saw_mixed = len(set(verdict_seq)) > 1
            saw_fail_then_pass = False
            seen_bad = False
            for v in verdict_seq:
                if v in ("fail", "inconclusive"):
                    seen_bad = True
                elif v == "pass" and seen_bad:
                    saw_fail_then_pass = True
                    break
            if (saw_mixed or saw_fail_then_pass) and "pooled_run_failure" not in flags:
                flags.append("pooled_run_failure")
        for ef in extra_flags:
            if ef not in flags:
                flags.append(ef)

        verdict = str(agg["verdict"])
        if (saw_unknown_run or saw_wrong_project) and verdict == "pass":
            verdict = "inconclusive"

        ref_sources = [ev.get("reference_source") for ev in per_run_evals if ev.get("reference_source")]
        if "evidence_bundle" in ref_sources:
            base_row["reference_source"] = "evidence_bundle"
        elif ref_sources:
            base_row["reference_source"] = ref_sources[-1]

        base_row["cell_evidence_sha256"] = latest_manifest.get("manifest_sha256")
        base_row["cell_evidence_sha256s"] = [m.get("manifest_sha256") for m in usable_manifests]
        base_row["run_verdicts"] = [
            {
                "run_id": ev["run_id"],
                "manifest_sha256": ev["manifest_sha256"],
                "verdict": ev["verdict"],
                "flags": list(ev["flags"]),
                "max_abs_diff": ev["max_abs_diff"],
            }
            for ev in per_run_evals
        ]
        base_row["input_sha256"] = latest_manifest.get("input_sha256", {"ours": [], "reference": []})
        base_row["achieved_kinds"] = agg["achieved_kinds"]
        base_row["tolerance_ref"] = latest_eval["tolerance_ref"]
        base_row["n"] = max(int(ev["n"]) for ev in per_run_evals)
        base_row["nondegeneracy"] = agg["nondegeneracy"]
        base_row["metric_values"] = {
            "max_abs_diff": agg["max_abs_diff"],
            "max_rel_diff": agg["max_rel_diff"],
            "arrays": latest_eval["per_array_metrics"],
        }
        base_row["verdict"] = verdict
        base_row["flags"] = flags

    for hook in hooks:
        extra_overlays = hook(base_row, declaration)
        for ov in extra_overlays:
            if ov not in base_row["overlays"]:
                base_row["overlays"].append(ov)

    base_row["evidence_level"] = _derive_cell_evidence_level(
        per_run_evals,
        overlays=base_row["overlays"],
        flags=base_row["flags"],
    )
    return base_row


def _build_result_payload(
    *,
    declaration: Mapping[str, Any],
    decl_sha: str,
    impl_rev: str,
    baseline_sha: str | None,
    first_decl: Mapping[str, Any] | None,
    store: ParityStore,
    store_res: ParityStoreResolution,
    catalog_dir: Path | None,
    evidence_run_ids: Sequence[str] | None,
    declared_cell_runs: Mapping[str, Sequence[str]],
    expected_project_slug: str,
    as_of: str,
    gate_stages: Mapping[str, str],
    comparison_backend: Mapping[str, Any],
    overlay_hooks: Sequence[CellOverlayHook],
) -> dict[str, Any]:
    record_id = str(declaration["parity"]["record_id"])
    uses_by_id = {str(u["id"]): u for u in declaration.get("use", [])}
    allowed_cell_ids = {
        compute_cell_id(
            str(pc["component_id"]),
            str(pc["use_id"]),
            pc["knob_setting"],
            pc["axis_value"],
            str(pc["data_tier"]),
            str(pc["pairing"]),
        )
        for pc in declaration.get("cell_plan", [])
    }
    if first_decl is not None:
        for pc in first_decl.get("cell_plan", []):
            allowed_cell_ids.add(
                compute_cell_id(
                    str(pc["component_id"]),
                    str(pc["use_id"]),
                    pc["knob_setting"],
                    pc["axis_value"],
                    str(pc["data_tier"]),
                    str(pc["pairing"]),
                )
            )
    manifests = load_cell_evidence_for_record(store, record_id)
    for m in manifests:
        if m.get("record_id") != record_id:
            raise CellEvidenceError(
                f"Evidence manifest record_id {m.get('record_id')!r} does not match {record_id!r}"
            )
        if str(m.get("cell_id", "")) not in allowed_cell_ids:
            raise CellEvidenceError(
                f"Evidence manifest cell_id {m.get('cell_id')!r} is not in declared cell_plan for {record_id!r}"
            )
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

    runs_by_id = _load_runs_by_id(catalog_dir, referenced_run_ids)
    known_run_ids = [rid for rid in referenced_run_ids if rid in runs_by_id]
    validity_by_id: dict[str, ValidityResult] = {}
    if known_run_ids and catalog_dir is not None:
        validity_by_id = resolve_validity(known_run_ids, catalog_dir=catalog_dir)
        if not (catalog_dir / "bathos.db").is_file() and (catalog_dir / "archived_items").is_dir():
            # Cool-tier archived_items fallback before `bth compact` creates bathos.db (S5: never swallow errors)
            latest_arch: dict[str, Any] = {}
            for rec in sorted(read_archived_item_fragments(catalog_dir), key=lambda r: r.recorded_at):
                latest_arch[rec.id] = rec
            archived_rids = {
                rid
                for rec in latest_arch.values()
                if rec.event == "archived"
                for rid in rec.run_ids
            }
            for rid in known_run_ids:
                if rid in archived_rids and (rid not in validity_by_id or not validity_by_id[rid].is_hidden):
                    cur = validity_by_id.get(rid)
                    validity_by_id[rid] = ValidityResult(
                        run_id=rid,
                        state="archived",
                        reason="cool-tier archived_items",
                        replacement_id=None,
                        version_source=cur.version_source if cur else "unknown",
                        evidence_tier=cur.evidence_tier if cur else "undeclared",
                        is_hidden=True,
                    )

    manifest_decl_sha_by_run: dict[str, str] = {}
    for m in manifests:
        rid = str(m.get("run_id", ""))
        if rid and isinstance(m.get("declaration_blob_sha256"), str) and m["declaration_blob_sha256"]:
            manifest_decl_sha_by_run[rid] = str(m["declaration_blob_sha256"])

    evidence_runs_summary: list[dict[str, Any]] = []
    for rid in referenced_run_ids:
        run = runs_by_id.get(rid)
        val = validity_by_id.get(rid)
        evidence_runs_summary.append(
            {
                "run_id": rid,
                "status": run.status if run else "unknown",
                "outcome": run.outcome if run else "",
                "validity_state": val.state if val else ("current" if run else "unknown"),
                "is_hidden": bool(val.is_hidden) if val else False,
                "env_blob_sha256": None,
                "declaration_blob_sha256": manifest_decl_sha_by_run.get(rid, decl_sha),
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
                expected_project_slug=expected_project_slug,
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
                    expected_project_slug=expected_project_slug,
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
    prior_records_count = _count_prior_records_on_pair(store, declaration, record_id)

    store_meta = {"path": str(store.root), "layer": store_res.source}
    norm_cell_runs = {
        str(cid): [str(r) for r in rids]
        for cid, rids in sorted(declared_cell_runs.items())
        if rids
    }
    grade_inputs = {
        "as_of": as_of,
        "target_revisions": _target_revisions_from_decl(declaration, impl_rev),
        "drift_run_id": None,
        "ledger_high_water_mark": None,
        "gate_stages": {g["code"]: g["stage"] for g in gates},
        "comparison_backend": dict(comparison_backend),
        "cell_runs": norm_cell_runs,
        "parity_store": store_meta,
        "evidence_run_ids_filter": (
            sorted(str(r) for r in evidence_run_ids) if evidence_run_ids is not None else None
        ),
        "overlay_hooks": [_hook_identity(h) for h in all_hooks],
    }

    return {
        "schema_version": "parity_result.v1",
        "standard_version": STANDARD_VERSION,
        "record_id": record_id,
        "record_sha256": decl_sha,
        "ledger_entry_id": None,
        "grader_version": _package_version(),
        "results_seen": results_seen,
        "parity_store": store_meta,
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
        "prior_records_on_pair": prior_records_count,
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

    store, store_res = require_parity_store(explicit=parity_store, start_dir=resolve_start_dir(dpath))
    cat_dir: Path | None = None
    if catalog_dir is not None:
        cat_dir = Path(catalog_dir).resolve()
    elif os.environ.get("BTH_CATALOG_DIR"):
        cat_dir = Path(os.environ["BTH_CATALOG_DIR"]).resolve()
    else:
        cat_dir = default_catalog_dir()

    first_meta, first_decl = load_declaration_baseline(store, record_id)
    if first_meta is None:
        raise ParityDeclareError(
            f"Record {record_id!r} has no declared baseline in store {store.root}; run 'bth parity declare' first"
        )
    baseline_sha = str(first_meta["declaration_sha256"])
    impl_rev = _resolve_decl_revision(store, record_id, decl_sha, first_meta)
    expected_project_slug = _resolve_record_project_slug(dpath, declaration)

    if verify:
        if not out_p.is_file():
            raise ParityVerifyError(f"Cannot verify: result file not found at {out_p}")
        existing_bytes = out_p.read_bytes()
        try:
            existing_obj = json.loads(existing_bytes.decode("utf-8"))
        except Exception as exc:
            raise ParityVerifyError(f"Malformed result JSON at {out_p}: {exc}") from exc
        if not isinstance(existing_obj, Mapping):
            raise ParityVerifyError(f"Result JSON at {out_p} must be an object")
        recorded_inputs = existing_obj.get("grade_inputs")
        if not isinstance(recorded_inputs, Mapping) or not recorded_inputs.get("as_of"):
            raise ParityVerifyError(f"Result JSON at {out_p} missing grade_inputs.as_of")
        replay_as_of = str(recorded_inputs["as_of"])
        # Always recompute gate_stages and comparison_backend; never trust recorded values on verify.
        replay_gate_stages = {code: "shadow" for code in RING0_ACTIVE_G_CODES}
        replay_backend = get_backend_identity()
        # Discover runs fresh from store (respecting only caller-passed evidence_run_ids).
        replay_ev_runs = evidence_run_ids
        if cell_runs is not None:
            replay_cell_runs: Mapping[str, Sequence[str]] = cell_runs
        else:
            raw_cell_runs = recorded_inputs.get("cell_runs")
            if raw_cell_runs is None:
                replay_cell_runs = {}
            elif isinstance(raw_cell_runs, Mapping):
                replay_cell_runs = raw_cell_runs
            else:
                raise ParityVerifyError(f"Result JSON at {out_p} has invalid grade_inputs.cell_runs")

        replayed = _build_result_payload(
            declaration=declaration,
            decl_sha=decl_sha,
            impl_rev=impl_rev,
            baseline_sha=baseline_sha,
            first_decl=first_decl,
            store=store,
            store_res=store_res,
            catalog_dir=cat_dir,
            evidence_run_ids=replay_ev_runs,
            declared_cell_runs=replay_cell_runs,
            expected_project_slug=expected_project_slug,
            as_of=replay_as_of,
            gate_stages=replay_gate_stages,
            comparison_backend=replay_backend,
            overlay_hooks=overlay_hooks,
        )
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
        store_res=store_res,
        catalog_dir=cat_dir,
        evidence_run_ids=evidence_run_ids,
        declared_cell_runs=cell_runs or {},
        expected_project_slug=expected_project_slug,
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
        store_meta = {"path": str(store.root), "layer": store_res.source}
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
            metadata=json.dumps(
                {
                    "parity_store": store_meta,
                    "record_id": record_id,
                    "record_sha256": decl_sha,
                },
                sort_keys=True,
            ),
            tags=[
                "parity_grade",
                f"record:{record_id}",
                f"parity_store_layer:{store_res.source}",
            ],
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
