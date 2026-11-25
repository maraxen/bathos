"""Parity report core (`parity_report.v1`), T3 bundle manifest, and MD/HTML renderers (WP30a).

Implements Spec 261123 §9.1, §9.2, §9.3.3, §9.3.4, and §12.2 WP30a.
"""

from __future__ import annotations

import hashlib
import html
import json
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from bathos.parity.badge import (
    BadgeValidationError,
    ReportLintError,
    format_kind_set,
    lint_rendered_text,
    render_t0,
    render_t1,
    validate_badge,
)
from bathos.parity.declare import load_parsed_declaration
from bathos.parity.evidence import load_cell_evidence_for_record
from bathos.parity.grader import CellOverlayHook, grade_parity_record
from bathos.parity.schema import ValidationIssue, canonical_json
from bathos.parity.store import (
    ParityStore,
    atomic_write_bytes,
    require_parity_store,
    resolve_start_dir,
    validate_safe_store_segment,
)

REPORT_SCHEMA_VERSION = "parity_report.v1"


class ParityReportVerifyError(RuntimeError):
    """Raised when report T0->T1->T2->T3 tier chain or bundle verification fails."""


def _verified_blob_status(store: ParityStore, sha: str) -> str:
    if not sha:
        return "absent"
    try:
        store.get_bytes(sha)
        return "present"
    except (FileNotFoundError, ValueError):
        return "absent"


def canonical_report_bytes(payload: Mapping[str, Any]) -> bytes:
    """Deterministically serialize report JSON with sorted keys and compact canonical float repr."""
    text = canonical_json(payload) + "\n"
    return text.encode("utf-8")


def build_t3_bundle_manifest(
    *,
    result: Mapping[str, Any],
    declaration: Mapping[str, Any],
    store: ParityStore,
    store_meta: Mapping[str, str] | None = None,
    write: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Build deterministic T3 bundle manifest (§9.3.3) with `absent` markers for un-stored members."""
    record_id = validate_safe_store_segment(str(result["record_id"]), "record_id")
    decl_sha = str(result["record_sha256"])
    all_manifests = load_cell_evidence_for_record(store, record_id)

    graded_run_ids = {
        str(r["run_id"])
        for r in result.get("evidence_runs", [])
        if isinstance(r, Mapping) and r.get("run_id")
    }
    graded_ev_shas = {
        str(c["cell_evidence_sha256"])
        for c in result.get("cells", [])
        if isinstance(c, Mapping) and c.get("cell_evidence_sha256")
    }
    manifests = [
        m
        for m in all_manifests
        if str(m.get("run_id", "")) in graded_run_ids
        and (not graded_ev_shas or str(m.get("manifest_sha256", "")) in graded_ev_shas or str(m.get("run_id", "")) in graded_run_ids)
    ]

    inputs_entries: list[dict[str, Any]] = []
    seen_inputs: set[tuple[str, str]] = set()
    arrays_entries: list[dict[str, Any]] = []
    seen_arrays: set[tuple[str, str, str]] = set()
    env_blob_shas: set[str] = set()
    compared_calls_entries: list[dict[str, Any]] = []
    manifest_witness_ids: set[str] = set()

    for r in result.get("evidence_runs", []):
        if isinstance(r, Mapping) and isinstance(r.get("env_blob_sha256"), str) and r["env_blob_sha256"]:
            env_blob_shas.add(str(r["env_blob_sha256"]))

    for m in manifests:
        ref_env = m.get("ref_env_blob_sha256")
        if isinstance(ref_env, str) and ref_env:
            env_blob_shas.add(ref_env)
        for cc in m.get("compared_calls") or []:
            if isinstance(cc, Mapping):
                compared_calls_entries.append(dict(cc))
        wit = m.get("witnesses")
        if isinstance(wit, Mapping):
            for wkey in wit:
                manifest_witness_ids.add(str(wkey))

        for side in ("ours", "reference"):
            for sha in m.get("input_sha256", {}).get(side, []):
                key = (str(sha), side)
                if key in seen_inputs:
                    continue
                seen_inputs.add(key)
                inputs_entries.append(
                    {
                        "sha256": str(sha),
                        "side": side,
                        "dtype": None,
                        "shape": None,
                        "declared_span": None,
                        "status": _verified_blob_status(store, str(sha)),
                    }
                )
        for arr_side_key in ("ours_arrays", "reference_arrays"):
            side_label = "ours" if arr_side_key == "ours_arrays" else "reference"
            side_map = m.get(arr_side_key) or {}
            for arr_name, meta in sorted(side_map.items()):
                cdig = str(meta.get("canonical_digest", ""))
                akey = (side_label, str(arr_name), cdig)
                if not cdig or akey in seen_arrays:
                    continue
                seen_arrays.add(akey)
                bundle_sha = str(
                    m.get("ours_bundle_sha256" if arr_side_key == "ours_arrays" else "reference_bundle_sha256") or ""
                )
                arrays_entries.append(
                    {
                        "side": side_label,
                        "name": str(arr_name),
                        "canonical_digest": cdig,
                        "dtype": meta.get("dtype"),
                        "shape": meta.get("shape"),
                        "bundle_sha256": bundle_sha,
                        "status": _verified_blob_status(store, bundle_sha),
                    }
                )

    inputs_entries.sort(key=lambda e: (e["sha256"], e["side"]))
    arrays_entries.sort(key=lambda e: (e["canonical_digest"], e["side"], e["name"]))

    declarations_list: list[dict[str, Any]] = [
        {
            "sha256": decl_sha,
            "role": "graded",
            "status": _verified_blob_status(store, decl_sha),
        }
    ]
    dual_display = result.get("dual_display_result")
    if isinstance(dual_display, Mapping):
        dual_sha = dual_display.get("declaration_sha256")
        if isinstance(dual_sha, str) and dual_sha and dual_sha != decl_sha:
            declarations_list.append(
                {
                    "sha256": dual_sha,
                    "role": "first_declared",
                    "status": _verified_blob_status(store, dual_sha),
                }
            )

    refs = declaration.get("reference", [])
    ref_entry = refs[0] if refs and isinstance(refs[0], Mapping) else {}
    ref_dump_sha: str | None = None
    if ref_entry.get("kind") == "sealed_dump":
        raw_dump = str(ref_entry.get("identity", {}).get("dump_sha256", "")).strip().lower()
        ref_dump_sha = raw_dump or None

    judgement_ids: set[str] = set()
    for u in declaration.get("use", []):
        if isinstance(u, Mapping) and u.get("judgement_id"):
            judgement_ids.add(str(u["judgement_id"]))
    for t in declaration.get("tolerance", []):
        if isinstance(t, Mapping) and t.get("judgement_id"):
            judgement_ids.add(str(t["judgement_id"]))
    for w in declaration.get("waiver", []):
        if isinstance(w, Mapping) and w.get("judgement_id"):
            judgement_ids.add(str(w["judgement_id"]))
    am = declaration.get("amendment")
    if isinstance(am, Mapping) and am.get("judgement_id"):
        judgement_ids.add(str(am["judgement_id"]))

    judgements_entries = (
        [
            {
                "judgement_id": jid,
                "status": "present" if jid in manifest_witness_ids else "absent",
            }
            for jid in sorted(judgement_ids)
        ]
        if judgement_ids
        else [{"status": "absent"}]
    )
    env_blobs_entries = (
        [{"sha256": sha, "status": _verified_blob_status(store, sha)} for sha in sorted(env_blob_shas)]
        if env_blob_shas
        else [{"status": "absent"}]
    )
    compared_calls_out = compared_calls_entries if compared_calls_entries else [{"status": "absent"}]

    grade_run_id = result.get("grade_run_id") or result.get("grade_inputs", {}).get("grade_run_id")
    grade_runs_entries = [{"run_id": str(grade_run_id), "status": "present"}] if grade_run_id else []

    manifest: dict[str, Any] = {
        "schema_version": "parity_bundle.v1",
        "record_id": record_id,
        "declarations": declarations_list,
        "inputs": inputs_entries,
        "arrays": arrays_entries,
        "reference_dump_sha256": ref_dump_sha,
        "sealed_dumps": (
            [{"dump_sha256": ref_dump_sha, "status": _verified_blob_status(store, ref_dump_sha)}]
            if ref_dump_sha
            else []
        ),
        "evidence_runs": [
            {
                "run_id": r["run_id"],
                "status": "present" if r.get("status") not in ("untracked_fixture", "unknown", None, "") else "absent",
                "validity_state": r.get("validity_state", "current"),
                "declaration_blob_sha256": r.get("declaration_blob_sha256"),
                "env_blob_sha256": r.get("env_blob_sha256"),
            }
            for r in sorted(result.get("evidence_runs", []), key=lambda x: str(x["run_id"]))
        ],
        "grade_runs": grade_runs_entries,
        "pilot_runs": [],
        "prior_runs": [{"run_id": rid} for rid in sorted(declaration.get("parity", {}).get("prior_runs", []))],
        "env_blobs": env_blobs_entries,
        "compared_calls": compared_calls_out,
        "judgements": judgements_entries,
        "ledger_high_water_mark": result.get("grade_inputs", {}).get("ledger_high_water_mark"),
    }
    effective_store_meta = store_meta if store_meta is not None else result.get("parity_store")
    if isinstance(effective_store_meta, Mapping):
        manifest["parity_store"] = dict(effective_store_meta)
    manifest_bytes = canonical_report_bytes(manifest)
    bundle_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if write:
        store.put_bytes(manifest_bytes)
        bundles_root = (store.root / "bundles").resolve()
        bundle_index = (bundles_root / f"{record_id}.parity.bundle.json").resolve()
        if bundles_root not in bundle_index.parents:
            raise ValueError(f"Bundle index path escapes bundles directory: {record_id!r}")
        atomic_write_bytes(bundle_index, manifest_bytes)
    return bundle_sha, manifest


def _default_report_path(result_path: Path, record_id: str) -> Path:
    safe_rid = validate_safe_store_segment(record_id, "record_id")
    parent_dir = result_path.parent.resolve()
    target = (parent_dir / f"{safe_rid}.parity.report.json").resolve()
    if target.parent != parent_dir:
        raise ValueError(f"Report path escapes result directory: {record_id!r}")
    return target


def _default_decl_path(result_path: Path) -> Path:
    name = result_path.name
    if name.endswith(".parity.result.json"):
        stem = name[: -len(".parity.result.json")]
        candidate = result_path.parent / f"{stem}.parity.bth.toml"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"Cannot locate companion *.parity.bth.toml for {result_path}; pass decl_path explicitly"
    )


def build_parity_report(
    result_path: Path | str,
    *,
    decl_path: Path | str | None = None,
    parity_store: Path | str | None = None,
    catalog_dir: Path | str | None = None,
    mode_state_dir: Path | str | None = None,
    evidence_run_ids: Sequence[str] | None = None,
    cell_runs: Mapping[str, Sequence[str]] | None = None,
    overlay_hooks: Sequence[CellOverlayHook] = (),
    output_path: Path | str | None = None,
    optional_sections: Mapping[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Build T2 report JSON (`parity_report.v1`), T3 manifest, and T0/T1 badge."""
    rpath = Path(result_path).resolve()
    res_bytes = rpath.read_bytes()
    result = json.loads(res_bytes.decode("utf-8"))
    result_sha = hashlib.sha256(res_bytes).hexdigest()

    dpath = Path(decl_path).resolve() if decl_path is not None else _default_decl_path(rpath)
    raw_decl, declaration = load_parsed_declaration(dpath)
    decl_sha = hashlib.sha256(raw_decl).hexdigest()
    decl_rid = str(declaration.get("parity", {}).get("record_id", ""))
    res_rid = str(result.get("record_id", ""))
    if res_rid != decl_rid:
        raise ValueError(
            f"Result record_id {res_rid!r} does not match declaration record_id {decl_rid!r}"
        )
    if str(result.get("record_sha256", "")) != decl_sha:
        raise ValueError(
            f"Result record_sha256 {result.get('record_sha256')!r} does not match declaration sha256 {decl_sha!r}"
        )

    # B2: Refuse forged/stale result.json by replaying deterministic verification before building T2/T3
    grade_parity_record(
        dpath,
        parity_store=parity_store,
        catalog_dir=catalog_dir,
        mode_state_dir=mode_state_dir,
        evidence_run_ids=evidence_run_ids,
        cell_runs=cell_runs,
        output_path=rpath,
        verify=True,
        track_run=False,
        overlay_hooks=overlay_hooks,
    )

    store, store_res = require_parity_store(explicit=parity_store, start_dir=resolve_start_dir(dpath))
    store_meta = {"path": str(store.root), "layer": store_res.source}
    bundle_sha, _t3_manifest = build_t3_bundle_manifest(
        result=result, declaration=declaration, store=store, store_meta=store_meta
    )

    parity_sec = declaration.get("parity", {})
    impl_sec = declaration.get("implementation", {})
    weights_sec = impl_sec.get("weights")
    weights_attested_only = isinstance(weights_sec, Mapping) and str(
        weights_sec.get("source_checkpoint_status", "")
    ).strip().startswith("attested_only")
    superseded_ids = result.get("superseded_run_ids")
    superseded_runs_count = (
        len(superseded_ids)
        if isinstance(superseded_ids, Sequence) and not isinstance(superseded_ids, (str, bytes))
        else int(result.get("cell_counts", {}).get("superseded_runs") or 0)
    )
    is_legacy = bool(result.get("legacy") or parity_sec.get("legacy"))
    tolerances = declaration.get("tolerance", [])
    has_owner_stated_tol = any(t.get("basis") == "owner_stated" for t in tolerances)
    impl_rev = str(
        result.get("grade_inputs", {}).get("target_revisions", {}).get("implementation", "0" * 40)
    )

    opt = dict(optional_sections or {})
    cells_sorted = sorted(result.get("cells", []), key=lambda c: str(c["cell_id"]))
    evaluated_cells = [c for c in cells_sorted if c.get("evidence_level") not in (None, "none")]
    if evaluated_cells and all(c.get("evidence_level") == "reexecuted" for c in evaluated_cells):
        evidence_level = "reexecuted"
    elif evaluated_cells:
        evidence_level = "self_reported"
    else:
        evidence_level = "none"
    reference_self_reported = any(c.get("reference_source") == "evidence_bundle" for c in cells_sorted)
    grader_ver = result.get("grader_version")
    if not isinstance(grader_ver, str) or not grader_ver.strip():
        raise ParityReportVerifyError("Result JSON missing required grader_version")

    # Report body WITHOUT badge (§9.3.3 no hash cycle)
    report_body: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "standard_version": result.get("standard_version", "1.0.0"),
        "record_id": result["record_id"],
        "record_sha256": result["record_sha256"],
        "result_sha256": result_sha,
        "parity_store": store_meta,
        "parity_metadata": {
            "prong": parity_sec.get("prong"),
            "layer_under_test": parity_sec.get("layer_under_test"),
            "instrumentation": parity_sec.get("instrumentation"),
            "mode": parity_sec.get("mode"),
            "strictness": parity_sec.get("strictness"),
            "badge_min_tier": parity_sec.get("badge_min_tier"),
            "control": bool(parity_sec.get("control", False)),
            "has_owner_stated_tolerance": has_owner_stated_tol,
            "reference_self_reported": reference_self_reported,
            "weights_attested_only": weights_attested_only,
            **(
                {"superseded_runs_count": superseded_runs_count}
                if superseded_runs_count > 0
                else {}
            ),
            **({"legacy": True} if is_legacy else {}),
        },
        "tool": {
            "name": impl_sec.get("name"),
            "repo": impl_sec.get("repo"),
            "revision": impl_rev,
            "licence": impl_sec.get("licence"),
            "third_party": bool(impl_sec.get("third_party", False)),
        },
        "references": list(declaration.get("reference", [])),
        "environment": {
            "pool_critical": list(declaration.get("environment", {}).get("pool_critical", [])),
            "frameworks": dict(declaration.get("environment", {}).get("frameworks", {})),
            "evidence_runs": list(result.get("evidence_runs", [])),
        },
        "uses": list(declaration.get("use", [])),
        "tolerances": list(tolerances),
        "waivers": list(declaration.get("waiver", [])),
        "components": list(result.get("components", [])) or [{"id": "model", "parent": "", "kind": "model"}],
        "cells": cells_sorted,
        "waived_cells": [c for c in cells_sorted if c.get("verdict") == "waived"],
        "not_covered_cells": [c for c in cells_sorted if c.get("verdict") == "not_covered"],
        "supplementary": list(result.get("supplementary", [])),
        "checks_admissibility": list(result.get("checks_admissibility", [])),
        "consistency_flags": [],
        "first_divergence": "not computed",
        "discrepancies": opt.get("discrepancies", "not computed"),
        "staleness": opt.get("staleness", "not computed"),
        "amendments": opt.get("amendments", "not computed"),
        "audit": opt.get("audit", "not computed"),
        "provenance": opt.get("provenance", "not computed"),
        "cell_counts": result["cell_counts"],
        "clauses": result["clauses"],
        "dual_display_result": result.get("dual_display_result"),
        "gates": result["gates"],
        "prior_records_on_pair": result["prior_records_on_pair"],
        "evidence_level": evidence_level,
        "bundle_sha256": bundle_sha,
        "generator": {
            "name": "bathos",
            "version": grader_ver,
            "schema": REPORT_SCHEMA_VERSION,
        },
    }

    h2 = hashlib.sha256(canonical_report_bytes(report_body)).hexdigest()
    t1 = render_t1(report_body, h2)
    h1 = hashlib.sha256(t1.encode("utf-8")).hexdigest()
    t0 = render_t0(report_body, h1)

    badge_errors = validate_badge(t0, t1, report_body)
    if badge_errors:
        raise BadgeValidationError("; ".join(badge_errors))

    clauses_list = report_body.get("clauses", [])
    checks_pending = (not clauses_list) or any(
        cl["axes"]["assurance"]["value"] == "PROVISIONAL" for cl in clauses_list
    )
    lint_errors = lint_rendered_text(t0, checks_pending=checks_pending) + lint_rendered_text(
        t1, checks_pending=checks_pending
    )
    if lint_errors:
        raise ReportLintError("; ".join(lint_errors))

    full_report = dict(report_body)
    full_report["badge"] = {
        "t0": t0,
        "t1": t1,
        "t1_sha256": h1,
        "t2_sha256": h2,
    }

    out_p = (
        Path(output_path).resolve()
        if output_path is not None
        else _default_report_path(rpath, str(result["record_id"]))
    )
    atomic_write_bytes(out_p, canonical_report_bytes(full_report))
    return out_p, full_report


def verify_report_tier_chain_in_memory(report: Mapping[str, Any]) -> None:
    """Recompute and verify T0 -> T1 -> T2 hash and badge consistency for an in-memory report."""
    badge = report.get("badge")
    if not isinstance(badge, Mapping):
        raise ParityReportVerifyError("TIER_LINK_BROKEN:T0 missing or invalid badge object")
    report_body = {k: v for k, v in report.items() if k != "badge"}
    try:
        h2 = hashlib.sha256(canonical_report_bytes(report_body)).hexdigest()
        t1 = render_t1(report_body, h2)
        h1 = hashlib.sha256(t1.encode("utf-8")).hexdigest()
        t0 = render_t0(report_body, h1)
    except (KeyError, TypeError, ValueError) as exc:
        raise ParityReportVerifyError(f"TIER_LINK_BROKEN:T2 malformed report body: {exc}") from exc

    if badge.get("t2_sha256") != h2:
        raise ParityReportVerifyError(
            f"TIER_LINK_BROKEN:T2 t2_sha256 mismatch: stored={badge.get('t2_sha256')!r}, computed={h2!r}"
        )
    if badge.get("t1") != t1:
        raise ParityReportVerifyError("TIER_LINK_BROKEN:T1 rendered T1 badge mismatch")
    if badge.get("t1_sha256") != h1:
        raise ParityReportVerifyError(
            f"TIER_LINK_BROKEN:T1 t1_sha256 mismatch: stored={badge.get('t1_sha256')!r}, computed={h1!r}"
        )
    if badge.get("t0") != t0:
        raise ParityReportVerifyError("TIER_LINK_BROKEN:T0 rendered T0 badge mismatch")
    badge_errs = validate_badge(t0, t1, report_body)
    if badge_errs:
        raise ParityReportVerifyError(f"TIER_LINK_BROKEN:T0 {'; '.join(badge_errs)}")


def _discover_companion_result_path(rpath: Path, report: Mapping[str, Any]) -> Path | None:
    if rpath.name.endswith(".parity.report.json"):
        stem = rpath.name[: -len(".parity.report.json")]
        cand_res = rpath.parent / f"{stem}.parity.result.json"
        if cand_res.is_file():
            return cand_res
    expected_sha = str(report.get("result_sha256", ""))
    expected_rid = str(report.get("record_id", ""))
    id_matches: list[Path] = []
    for cand in sorted(rpath.parent.glob("*.parity.result.json")):
        try:
            raw = cand.read_bytes()
        except OSError:
            continue
        if expected_sha and hashlib.sha256(raw).hexdigest() == expected_sha:
            return cand
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(obj, Mapping) and str(obj.get("record_id", "")) == expected_rid:
            id_matches.append(cand)
    if len(id_matches) == 1:
        return id_matches[0]
    if len(id_matches) > 1:
        raise ParityReportVerifyError(
            f"Multiple *.parity.result.json files in {rpath.parent} match record_id {expected_rid!r}; pass --result-path"
        )
    return None


def _discover_companion_decl_path(rpath: Path, res_p: Path | None, report: Mapping[str, Any]) -> Path | None:
    if rpath.name.endswith(".parity.report.json"):
        stem = rpath.name[: -len(".parity.report.json")]
        cand_decl = rpath.parent / f"{stem}.parity.bth.toml"
        if cand_decl.is_file():
            return cand_decl
    if res_p is not None and res_p.name.endswith(".parity.result.json"):
        res_stem = res_p.name[: -len(".parity.result.json")]
        cand_decl = res_p.parent / f"{res_stem}.parity.bth.toml"
        if cand_decl.is_file():
            return cand_decl
    expected_sha = str(report.get("record_sha256", ""))
    expected_rid = str(report.get("record_id", ""))
    search_dirs: list[Path] = [rpath.parent]
    if res_p is not None and res_p.parent != rpath.parent:
        search_dirs.append(res_p.parent)
    id_matches: list[Path] = []
    for sdir in search_dirs:
        for cand in sorted(sdir.glob("*.parity.bth.toml")):
            try:
                raw = cand.read_bytes()
            except OSError:
                continue
            if expected_sha and hashlib.sha256(raw).hexdigest() == expected_sha:
                return cand
            try:
                obj = tomllib.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, tomllib.TOMLDecodeError):
                continue
            if isinstance(obj, Mapping) and str(obj.get("parity", {}).get("record_id", "")) == expected_rid:
                id_matches.append(cand)
    if len(id_matches) == 1:
        return id_matches[0]
    if len(id_matches) > 1:
        raise ParityReportVerifyError(
            f"Multiple *.parity.bth.toml files match record_id {expected_rid!r}; pass --decl-path"
        )
    return None


def verify_parity_report(
    report_path: Path | str,
    *,
    result_path: Path | str | None = None,
    decl_path: Path | str | None = None,
    parity_store: Path | str | None = None,
    catalog_dir: Path | str | None = None,
    mode_state_dir: Path | str | None = None,
    evidence_run_ids: Sequence[str] | None = None,
    cell_runs: Mapping[str, Sequence[str]] | None = None,
    overlay_hooks: Sequence[CellOverlayHook] = (),
) -> dict[str, Any]:
    """Verify full T0 -> T1 -> T2 -> T3 disclosure chain and result/declaration replay."""
    rpath = Path(report_path).resolve()
    if not rpath.is_file():
        raise ParityReportVerifyError(f"Report file not found: {rpath}")
    try:
        report = json.loads(rpath.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParityReportVerifyError(f"Malformed report JSON at {rpath}: {exc}") from exc
    if not isinstance(report, Mapping):
        raise ParityReportVerifyError(f"Report JSON at {rpath} must be an object")

    struct_issues = validate_report_structure(report)
    if struct_issues:
        raise ParityReportVerifyError(
            "Report structure validation failed: " + "; ".join(str(i) for i in struct_issues)
        )
    verify_report_tier_chain_in_memory(report)

    res_p: Path | None = Path(result_path).resolve() if result_path is not None else _discover_companion_result_path(rpath, report)
    dpath: Path | None = Path(decl_path).resolve() if decl_path is not None else _discover_companion_decl_path(rpath, res_p, report)

    if res_p is None:
        raise ParityReportVerifyError(
            f"Cannot verify report {rpath}: companion *.parity.result.json not found; pass --result-path"
        )
    if dpath is None:
        raise ParityReportVerifyError(
            f"Cannot verify report {rpath}: companion *.parity.bth.toml not found; pass --decl-path"
        )

    store_anchor = resolve_start_dir(dpath if dpath is not None else rpath)
    store, _store_res = require_parity_store(explicit=parity_store, start_dir=store_anchor)

    bundle_sha = str(report["bundle_sha256"])
    try:
        bundle_bytes = store.get_bytes(bundle_sha)
        t3_manifest = json.loads(bundle_bytes.decode("utf-8"))
    except (FileNotFoundError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParityReportVerifyError(
            f"TIER_LINK_BROKEN:T3 bundle {bundle_sha} missing or corrupt in store {store.root}: {exc}"
        ) from exc
    if not isinstance(t3_manifest, Mapping):
        raise ParityReportVerifyError(f"TIER_LINK_BROKEN:T3 bundle {bundle_sha} must be a JSON object")

    present_count = 0
    absent_count = 0
    for section, sha_key in (
        ("declarations", "sha256"),
        ("inputs", "sha256"),
        ("arrays", "bundle_sha256"),
        ("sealed_dumps", "dump_sha256"),
        ("env_blobs", "sha256"),
    ):
        entries = t3_manifest.get(section, [])
        if not isinstance(entries, list):
            raise ParityReportVerifyError(f"TIER_LINK_BROKEN:T3 manifest {section!r} must be a list")
        for idx, item in enumerate(entries):
            if not isinstance(item, Mapping):
                raise ParityReportVerifyError(
                    f"TIER_LINK_BROKEN:T3 manifest {section}[{idx}] must be an object"
                )
            status = item.get("status")
            if status == "absent":
                absent_count += 1
            elif status == "present":
                member_sha = item.get(sha_key)
                if not isinstance(member_sha, str) or not member_sha:
                    raise ParityReportVerifyError(
                        f"TIER_LINK_BROKEN:T3 manifest {section}[{idx}] missing {sha_key}"
                    )
                try:
                    store.get_bytes(member_sha)
                except (FileNotFoundError, ValueError) as exc:
                    raise ParityReportVerifyError(
                        f"TIER_LINK_BROKEN:T3 present member {section}[{idx}] ({member_sha}) missing or corrupt: {exc}"
                    ) from exc
                present_count += 1
            else:
                raise ParityReportVerifyError(
                    f"TIER_LINK_BROKEN:T3 manifest {section}[{idx}] has invalid status {status!r}"
                )

    result_verified = False
    if res_p is not None and dpath is not None:
        if not res_p.is_file():
            raise ParityReportVerifyError(f"Result file not found: {res_p}")
        res_bytes = res_p.read_bytes()
        actual_res_sha = hashlib.sha256(res_bytes).hexdigest()
        if actual_res_sha != report.get("result_sha256"):
            raise ParityReportVerifyError(
                f"TIER_LINK_BROKEN:T2 result_sha256 mismatch: report={report.get('result_sha256')!r}, actual={actual_res_sha!r}"
            )
        grade_parity_record(
            dpath,
            parity_store=parity_store,
            catalog_dir=catalog_dir,
            mode_state_dir=mode_state_dir,
            evidence_run_ids=evidence_run_ids,
            cell_runs=cell_runs,
            output_path=res_p,
            verify=True,
            track_run=False,
            overlay_hooks=overlay_hooks,
        )
        result_obj = json.loads(res_bytes.decode("utf-8"))
        _raw_decl, decl_obj = load_parsed_declaration(dpath)
        recomputed_bundle_sha, _ = build_t3_bundle_manifest(
            result=result_obj,
            declaration=decl_obj,
            store=store,
            store_meta=report.get("parity_store") if isinstance(report.get("parity_store"), Mapping) else None,
            write=False,
        )
        if recomputed_bundle_sha != bundle_sha:
            raise ParityReportVerifyError(
                f"TIER_LINK_BROKEN:T3 bundle_sha256 mismatch: stored={bundle_sha!r}, recomputed={recomputed_bundle_sha!r}"
            )
        result_verified = True

    return {
        "verified": True,
        "record_id": str(report["record_id"]),
        "report_path": str(rpath),
        "t0_verified": True,
        "t1_sha256": str(report["badge"]["t1_sha256"]),
        "t2_sha256": str(report["badge"]["t2_sha256"]),
        "bundle_sha256": bundle_sha,
        "present_members": present_count,
        "absent_members": absent_count,
        "result_verified": result_verified,
    }


def _md_cell(val: Any) -> str:
    return str(val).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _worst_cells(cells: list[Mapping[str, Any]], limit: int = 5) -> list[Mapping[str, Any]]:
    def _score(cell: Mapping[str, Any]) -> tuple[int, float]:
        verdict = str(cell.get("verdict"))
        rank = 0 if verdict in ("fail", "contested", "inconclusive") else 1
        max_abs = float(cell.get("metric_values", {}).get("max_abs_diff") or 0.0)
        return (rank, -max_abs)

    return sorted(cells, key=_score)[:limit]


def _render_gates_summary(gates: list[Mapping[str, Any]]) -> str:
    enf = sum(1 for g in gates if g.get("stage") == "enforce")
    adv = sum(1 for g in gates if g.get("stage") == "advisory")
    shd = sum(1 for g in gates if g.get("stage") == "shadow")
    na = sum(1 for g in gates if g.get("stage") == "not_active")
    return f"{enf} enforce, {adv} advisory, {shd} shadow, {na} not active"


def render_report_markdown(report: Mapping[str, Any]) -> str:
    """Render deterministic Markdown summary + matrix strictly from report JSON (§9.2)."""
    verify_report_tier_chain_in_memory(report)
    badge = report["badge"]
    tool = report["tool"]
    counts = report["cell_counts"]
    cells = report["cells"]
    clauses_list = report.get("clauses", [])
    checks_pending = (not clauses_list) or any(
        cl["axes"]["assurance"]["value"] == "PROVISIONAL" for cl in clauses_list
    )

    lines: list[str] = [
        f"# Comparison Report: `{report['record_id']}`",
        "",
        f"- **T0 Badge:** `{badge['t0']}`",
        f"- **T1 Badge:** `{badge['t1']}`",
        f"- **T2 Hash:** `{badge['t2_sha256']}`",
        f"- **T3 Bundle Hash:** `{report['bundle_sha256']}`",
        f"- **Evidence level:** {report.get('evidence_level', 'self_reported')}",
        f"- **Implementation:** `{tool['name']}@{str(tool['revision'])[:12]}` ({tool['licence']})",
        f"- **Coverage:** {counts['passing']}/{counts['required']} passing "
        f"(not_run={counts['not_run']}, not_covered={counts['not_covered']}, waived={counts['waived']})",
        f"- **Gates:** {_render_gates_summary(report['gates'])}",
        f"- **Prior records on pair:** {report['prior_records_on_pair']}",
        f"- **Discrepancies:** {report['discrepancies']}",
        f"- **Staleness:** {report['staleness']}",
        f"- **Amendments:** {report['amendments']}",
        f"- **Audit:** {report['audit']}",
        f"- **Provenance:** {report['provenance']}",
        *(
            ["- **Weights provenance:** attested-only (source checkpoint not re-derived)"]
            if report.get("parity_metadata", {}).get("weights_attested_only")
            else []
        ),
        "",
        "## Uses",
        "",
    ]
    for u in report.get("uses", []):
        lines.append(
            f"- `{u['id']}` ({u['parity_kind']}): {u['purpose']} — metric `{u['metric']['id']}`"
        )

    lines.extend(["", "## Cell Verdict Matrix", "", "| cell_id | component | use | tier | pairing | kind | verdict | max_abs_diff | flags |", "|---|---|---|---|---|---|---|---|---|"])
    for c in cells:
        ktxt = format_kind_set(
            [c["declared_kind"]],
            fallback_from=c.get("fallback_from"),
            incompat_class=c.get("incompatibility_class"),
        )
        max_abs = c.get("metric_values", {}).get("max_abs_diff")
        abs_s = f"{max_abs:.3e}" if isinstance(max_abs, (int, float)) else "—"
        flags_s = ", ".join(c.get("flags", [])) or "—"
        lines.append(
            f"| `{_md_cell(c['cell_id'])}` | `{_md_cell(c['component_id'])}` | `{_md_cell(c['use_id'])}` | "
            f"{_md_cell(c['data_tier'])} | {_md_cell(c['pairing'])} | {_md_cell(ktxt)} | "
            f"**{_md_cell(c['verdict'])}** | {_md_cell(abs_s)} | {_md_cell(flags_s)} |"
        )

    lines.extend(["", "## Five Worst Cells", ""])
    for c in _worst_cells(cells):
        max_abs = c.get("metric_values", {}).get("max_abs_diff")
        abs_s = f"{max_abs:.3e}" if isinstance(max_abs, (int, float)) else "n/a"
        lines.append(f"- `{c['cell_id']}` (`{c['use_id']}`): **{c['verdict']}** (max_abs_diff={abs_s})")

    rendered = "\n".join(lines) + "\n"
    lint_issues = lint_rendered_text(rendered, checks_pending=checks_pending)
    if lint_issues:
        raise ReportLintError("; ".join(lint_issues))
    return rendered


_VERDICT_COLORS: dict[str, str] = {
    "pass": "#dff5e1",
    "fail": "#fde0e0",
    "not_run": "#f0f0f0",
    "not_covered": "#fff3cd",
    "waived": "#e6ecf5",
    "inconclusive": "#ffe5cc",
    "contested": "#f3d9fa",
    "declared_deviation": "#e0f2fe",
}


def render_report_html(report: Mapping[str, Any]) -> str:
    """Render deterministic self-contained HTML summary + verdict matrix strictly from report JSON (§9.2)."""
    verify_report_tier_chain_in_memory(report)
    badge = report["badge"]
    tool = report["tool"]
    counts = report["cell_counts"]
    cells = report["cells"]
    clauses_list = report.get("clauses", [])
    checks_pending = (not clauses_list) or any(
        cl["axes"]["assurance"]["value"] == "PROVISIONAL" for cl in clauses_list
    )

    def _e(v: Any) -> str:
        return html.escape(str(v), quote=True)

    rows_html: list[str] = []
    for c in cells:
        bg = _VERDICT_COLORS.get(str(c["verdict"]), "#ffffff")
        ktxt = format_kind_set(
            [c["declared_kind"]],
            fallback_from=c.get("fallback_from"),
            incompat_class=c.get("incompatibility_class"),
        )
        max_abs = c.get("metric_values", {}).get("max_abs_diff")
        abs_s = f"{max_abs:.3e}" if isinstance(max_abs, (int, float)) else "—"
        flags_s = ", ".join(c.get("flags", [])) or "—"
        rows_html.append(
            f"<tr style=\"background:{bg}\">"
            f"<td><code>{_e(c['cell_id'])}</code></td>"
            f"<td><code>{_e(c['component_id'])}</code></td>"
            f"<td><code>{_e(c['use_id'])}</code></td>"
            f"<td>{_e(c['data_tier'])}</td>"
            f"<td>{_e(c['pairing'])}</td>"
            f"<td>{_e(ktxt)}</td>"
            f"<td><strong>{_e(c['verdict'])}</strong></td>"
            f"<td>{_e(abs_s)}</td>"
            f"<td>{_e(flags_s)}</td>"
            f"</tr>"
        )

    worst_html = "".join(
        f"<li><code>{_e(c['cell_id'])}</code> ({_e(c['use_id'])}): "
        f"<strong>{_e(c['verdict'])}</strong></li>"
        for c in _worst_cells(cells)
    )

    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Comparison Report: {_e(report['record_id'])}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #111; }}
code {{ background: #f4f4f6; padding: 0.1rem 0.3rem; border-radius: 3px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
th, td {{ border: 1px solid #d0d7de; padding: 0.4rem 0.6rem; text-align: left; font-size: 0.9rem; }}
th {{ background: #f6f8fa; }}
.badge {{ background: #f6f8fa; border: 1px solid #d0d7de; padding: 0.75rem; border-radius: 6px; margin: 0.5rem 0; font-family: monospace; }}
</style>
</head>
<body>
<h1>Comparison Report: <code>{_e(report['record_id'])}</code></h1>
<div class="badge"><strong>T0:</strong> {_e(badge['t0'])}</div>
<div class="badge"><strong>T1:</strong> {_e(badge['t1'])}</div>
<ul>
  <li><strong>T2 Body SHA-256:</strong> <code>{_e(badge['t2_sha256'])}</code></li>
  <li><strong>T3 Bundle SHA-256:</strong> <code>{_e(report['bundle_sha256'])}</code></li>
  <li><strong>Evidence level:</strong> {_e(report.get('evidence_level', 'self_reported'))}</li>
  <li><strong>Implementation:</strong> <code>{_e(tool['name'])}@{_e(str(tool['revision'])[:12])}</code></li>
  <li><strong>Coverage:</strong> {_e(counts['passing'])}/{_e(counts['required'])} passing (not_run={_e(counts['not_run'])}, not_covered={_e(counts['not_covered'])}, waived={_e(counts['waived'])})</li>
  <li><strong>Gates:</strong> {_e(_render_gates_summary(report['gates']))}</li>
  <li><strong>Prior records on pair:</strong> {_e(report['prior_records_on_pair'])}</li>
  <li><strong>Discrepancies:</strong> {_e(report['discrepancies'])}</li>
  <li><strong>Staleness:</strong> {_e(report['staleness'])}</li>
  <li><strong>Amendments:</strong> {_e(report['amendments'])}</li>
  <li><strong>Audit:</strong> {_e(report['audit'])}</li>
  <li><strong>Provenance:</strong> {_e(report['provenance'])}</li>
  {("<li><strong>Weights provenance:</strong> attested-only (source checkpoint not re-derived)</li>" if report.get("parity_metadata", {}).get("weights_attested_only") else "")}
</ul>
<h2>Cell Verdict Matrix</h2>
<table>
<thead><tr><th>cell_id</th><th>component</th><th>use</th><th>tier</th><th>pairing</th><th>kind</th><th>verdict</th><th>max_abs_diff</th><th>flags</th></tr></thead>
<tbody>
{"".join(rows_html)}
</tbody>
</table>
<h2>Five Worst Cells</h2>
<ul>{worst_html}</ul>
</body>
</html>
"""
    lint_issues = lint_rendered_text(doc, checks_pending=checks_pending)
    if lint_issues:
        raise ReportLintError("; ".join(lint_issues))
    return doc


def validate_report_structure(report: Mapping[str, Any]) -> list[ValidationIssue]:
    """Validate structural conformance of a ``parity_report.v1`` object."""
    issues: list[ValidationIssue] = []
    required_top = (
        "schema_version",
        "standard_version",
        "record_id",
        "record_sha256",
        "result_sha256",
        "parity_metadata",
        "tool",
        "references",
        "environment",
        "uses",
        "tolerances",
        "waivers",
        "components",
        "cells",
        "waived_cells",
        "not_covered_cells",
        "supplementary",
        "checks_admissibility",
        "consistency_flags",
        "first_divergence",
        "discrepancies",
        "staleness",
        "amendments",
        "audit",
        "provenance",
        "cell_counts",
        "clauses",
        "gates",
        "prior_records_on_pair",
        "evidence_level",
        "bundle_sha256",
        "generator",
        "badge",
    )
    for key in required_top:
        if key not in report or report[key] is None:
            issues.append(
                ValidationIssue(f"report.{key}", "REPORT_MISSING_FIELD", f"missing required field 'report.{key}'")
            )
    if report.get("schema_version") not in (None, REPORT_SCHEMA_VERSION):
        issues.append(
            ValidationIssue("report.schema_version", "REPORT_SCHEMA", f"schema_version must be {REPORT_SCHEMA_VERSION!r}")
        )
    for hex_key in ("record_sha256", "result_sha256", "bundle_sha256"):
        val = report.get(hex_key)
        if val is not None and (not isinstance(val, str) or len(val) != 64 or any(c not in "0123456789abcdef" for c in val)):
            issues.append(
                ValidationIssue(f"report.{hex_key}", "REPORT_SHA256", f"{hex_key} must be a 64-char lowercase hex string")
            )
    badge = report.get("badge")
    if isinstance(badge, Mapping):
        for bk in ("t0", "t1", "t1_sha256", "t2_sha256"):
            if not isinstance(badge.get(bk), str) or not str(badge.get(bk)).strip():
                issues.append(ValidationIssue(f"report.badge.{bk}", "REPORT_BADGE", f"badge.{bk} must be non-empty string"))
    elif badge is not None:
        issues.append(ValidationIssue("report.badge", "REPORT_TYPE", "badge must be an object"))
    tool = report.get("tool")
    if isinstance(tool, Mapping):
        for tk in ("name", "repo", "revision", "licence"):
            if not isinstance(tool.get(tk), str) or not str(tool.get(tk)).strip():
                issues.append(ValidationIssue(f"report.tool.{tk}", "REPORT_TOOL", f"tool.{tk} must be non-empty string"))
    gen = report.get("generator")
    if isinstance(gen, Mapping):
        for gk in ("name", "version", "schema"):
            if not isinstance(gen.get(gk), str) or not str(gen.get(gk)).strip():
                issues.append(ValidationIssue(f"report.generator.{gk}", "REPORT_GEN", f"generator.{gk} must be non-empty string"))
    ps = report.get("parity_store")
    if ps is not None and (
        not isinstance(ps, Mapping)
        or not isinstance(ps.get("path"), str)
        or not str(ps.get("path")).strip()
        or not isinstance(ps.get("layer"), str)
        or not str(ps.get("layer")).strip()
    ):
        issues.append(ValidationIssue("report.parity_store", "REPORT_STORE", "parity_store must have non-empty path and layer"))
    return issues
