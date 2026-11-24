"""Parity report core (`parity_report.v1`), T3 bundle manifest, and MD/HTML renderers (WP30a).

Implements Spec 261123 §9.1, §9.2, §9.3.3, §9.3.4, and §12.2 WP30a.
"""

from __future__ import annotations

import hashlib
import html
import json
from collections.abc import Mapping
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
from bathos.parity.schema import canonical_json
from bathos.parity.store import ParityStore, atomic_write_bytes, require_parity_store

REPORT_SCHEMA_VERSION = "parity_report.v1"


def canonical_report_bytes(payload: Mapping[str, Any]) -> bytes:
    """Deterministically serialize report JSON with sorted keys and compact canonical float repr."""
    text = canonical_json(payload) + "\n"
    return text.encode("utf-8")


def build_t3_bundle_manifest(
    *,
    result: Mapping[str, Any],
    declaration: Mapping[str, Any],
    store: ParityStore,
) -> tuple[str, dict[str, Any]]:
    """Build deterministic T3 bundle manifest (§9.3.3) with `absent` markers for un-stored members."""
    record_id = str(result["record_id"])
    decl_sha = str(result["record_sha256"])
    manifests = load_cell_evidence_for_record(store, record_id)

    inputs_entries: list[dict[str, Any]] = []
    seen_inputs: set[str] = set()
    arrays_entries: list[dict[str, Any]] = []
    seen_arrays: set[str] = set()
    for m in manifests:
        for side in ("ours", "reference"):
            for sha in m.get("input_sha256", {}).get(side, []):
                if sha in seen_inputs:
                    continue
                seen_inputs.add(sha)
                inputs_entries.append(
                    {
                        "sha256": sha,
                        "side": side,
                        "status": "present" if store.has(sha) else "absent",
                    }
                )
        for arr_side_key in ("ours_arrays", "reference_arrays"):
            side_map = m.get(arr_side_key) or {}
            for arr_name, meta in sorted(side_map.items()):
                cdig = str(meta.get("canonical_digest", ""))
                if not cdig or cdig in seen_arrays:
                    continue
                seen_arrays.add(cdig)
                bundle_sha = str(
                    m.get("ours_bundle_sha256" if arr_side_key == "ours_arrays" else "reference_bundle_sha256") or ""
                )
                arrays_entries.append(
                    {
                        "name": arr_name,
                        "canonical_digest": cdig,
                        "dtype": meta.get("dtype"),
                        "shape": meta.get("shape"),
                        "bundle_sha256": bundle_sha,
                        "status": "present" if (bundle_sha and store.has(bundle_sha)) else "absent",
                    }
                )

    inputs_entries.sort(key=lambda e: (e["sha256"], e["side"]))
    arrays_entries.sort(key=lambda e: e["canonical_digest"])

    decl_present = store.has(decl_sha)
    manifest: dict[str, Any] = {
        "schema_version": "parity_bundle.v1",
        "record_id": record_id,
        "declarations": [
            {
                "sha256": decl_sha,
                "status": "present" if decl_present else "absent",
            }
        ],
        "inputs": inputs_entries,
        "arrays": arrays_entries,
        "evidence_runs": [
            {"run_id": r["run_id"], "status": "present" if r.get("status") != "untracked_fixture" else "absent"}
            for r in sorted(result.get("evidence_runs", []), key=lambda x: str(x["run_id"]))
        ],
        "prior_runs": [{"run_id": rid} for rid in sorted(declaration.get("parity", {}).get("prior_runs", []))],
        "env_blobs": [{"status": "absent"}],
        "compared_calls": [{"status": "absent"}],
        "judgements": [{"status": "absent"}],
        "ledger_high_water_mark": result.get("grade_inputs", {}).get("ledger_high_water_mark"),
    }
    manifest_bytes = canonical_report_bytes(manifest)
    bundle_sha = hashlib.sha256(manifest_bytes).hexdigest()
    store.put_bytes(manifest_bytes)
    bundle_index = store.root / "bundles" / f"{record_id}.parity.bundle.json"
    atomic_write_bytes(bundle_index, manifest_bytes)
    return bundle_sha, manifest


def _default_report_path(result_path: Path, record_id: str) -> Path:
    return result_path.parent / f"{record_id}.parity.report.json"


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
    output_path: Path | str | None = None,
    optional_sections: Mapping[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Build T2 report JSON (`parity_report.v1`), T3 manifest, and T0/T1 badge."""
    rpath = Path(result_path).resolve()
    res_bytes = rpath.read_bytes()
    result = json.loads(res_bytes.decode("utf-8"))
    result_sha = hashlib.sha256(res_bytes).hexdigest()

    dpath = Path(decl_path).resolve() if decl_path is not None else _default_decl_path(rpath)
    _raw_decl, declaration = load_parsed_declaration(dpath)

    store, _store_res = require_parity_store(explicit=parity_store, start_dir=rpath.parent)
    bundle_sha, _t3_manifest = build_t3_bundle_manifest(
        result=result, declaration=declaration, store=store
    )

    parity_sec = declaration.get("parity", {})
    impl_sec = declaration.get("implementation", {})
    tolerances = declaration.get("tolerance", [])
    has_owner_stated_tol = any(t.get("basis") == "owner_stated" for t in tolerances)
    impl_rev = str(
        result.get("grade_inputs", {}).get("target_revisions", {}).get("implementation", "0" * 40)
    )

    opt = dict(optional_sections or {})
    cells_sorted = sorted(result.get("cells", []), key=lambda c: str(c["cell_id"]))

    # Report body WITHOUT badge (§9.3.3 no hash cycle)
    report_body: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "standard_version": result.get("standard_version", "1.0.0"),
        "record_id": result["record_id"],
        "record_sha256": result["record_sha256"],
        "result_sha256": result_sha,
        "parity_metadata": {
            "prong": parity_sec.get("prong"),
            "layer_under_test": parity_sec.get("layer_under_test"),
            "instrumentation": parity_sec.get("instrumentation"),
            "mode": parity_sec.get("mode"),
            "strictness": parity_sec.get("strictness"),
            "badge_min_tier": parity_sec.get("badge_min_tier"),
            "control": bool(parity_sec.get("control", False)),
            "has_owner_stated_tolerance": has_owner_stated_tol,
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
        "evidence_level": "self_reported",
        "bundle_sha256": bundle_sha,
        "generator": {
            "name": "bathos",
            "version": result.get("grader_version", "0.18.0"),
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

    checks_pending = any(
        cl["axes"]["assurance"]["value"] == "PROVISIONAL" for cl in report_body.get("clauses", [])
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
    badge = report["badge"]
    tool = report["tool"]
    counts = report["cell_counts"]
    cells = report["cells"]
    checks_pending = any(
        cl["axes"]["assurance"]["value"] == "PROVISIONAL" for cl in report.get("clauses", [])
    )

    lines: list[str] = [
        f"# Comparison Report: `{report['record_id']}`",
        "",
        f"- **T0 Badge:** `{badge['t0']}`",
        f"- **T1 Badge:** `{badge['t1']}`",
        f"- **T2 Hash:** `{badge['t2_sha256']}`",
        f"- **T3 Bundle Hash:** `{report['bundle_sha256']}`",
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
            f"| `{c['cell_id']}` | `{c['component_id']}` | `{c['use_id']}` | {c['data_tier']} | {c['pairing']} | {ktxt} | **{c['verdict']}** | {abs_s} | {flags_s} |"
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
    badge = report["badge"]
    tool = report["tool"]
    counts = report["cell_counts"]
    cells = report["cells"]
    checks_pending = any(
        cl["axes"]["assurance"]["value"] == "PROVISIONAL" for cl in report.get("clauses", [])
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
  <li><strong>Implementation:</strong> <code>{_e(tool['name'])}@{_e(str(tool['revision'])[:12])}</code></li>
  <li><strong>Coverage:</strong> {_e(counts['passing'])}/{_e(counts['required'])} passing (not_run={_e(counts['not_run'])}, not_covered={_e(counts['not_covered'])}, waived={_e(counts['waived'])})</li>
  <li><strong>Gates:</strong> {_e(_render_gates_summary(report['gates']))}</li>
  <li><strong>Discrepancies:</strong> {_e(report['discrepancies'])}</li>
  <li><strong>Staleness:</strong> {_e(report['staleness'])}</li>
  <li><strong>Amendments:</strong> {_e(report['amendments'])}</li>
  <li><strong>Audit:</strong> {_e(report['audit'])}</li>
  <li><strong>Provenance:</strong> {_e(report['provenance'])}</li>
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
