"""Badge renderer (`T0`/`T1`), badge validation, and disclosure lints (WP30a).

Implements Spec 261123 §9.3.3, §9.3.4, §9.3.5, and §12.2 WP30a.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from bathos.parity.schema import DATA_TIERS, PARITY_KINDS_ORDER, STANDARD_VERSION

# Word 'parity' MUST be followed by one of: a kind word, "record", or "report" (§9.3.4).
_BARE_PARITY_RE = re.compile(
    r"\bparity\b(?!\s+(?:record|report|bitwise|within[-_]tolerance|distributional|behavioural|association|approximate)\b)",
    re.IGNORECASE,
)
_PASSES_OR_VALID_RE = re.compile(r"\b(?:PASSES|VALID)\b")
_STD_VERSION_RE = re.compile(r"\bstd\s+\d+\.\d+\.\d+\b")
_IMPL_REV_RE = re.compile(r"\b[a-zA-Z0-9._-]+@(?:[0-9a-f]{7,64}|mixed)\b")


class BadgeValidationError(ValueError):
    """Raised when a T0/T1 badge fails structural validation (§9.3.4)."""


class ReportLintError(ValueError):
    """Raised when rendered report text violates bare-'parity' or PROVISIONAL rules (§9.3.4)."""


def format_kind_set(kinds: Sequence[str], *, fallback_from: str | None = None, incompat_class: str | None = None) -> str:
    """Format KIND-SET in §3.4 order (uppercased with hyphen for within-tolerance)."""
    upper = [k.upper().replace("_", "-") for k in kinds]
    joined = " + ".join(upper)
    if fallback_from:
        fb_up = fallback_from.upper().replace("_", "-")
        cls_txt = incompat_class or "declared"
        return f"{joined} (fallback from {fb_up}: {cls_txt})"
    return joined


def format_reference_qualifier(references: Sequence[Mapping[str, Any]]) -> str:
    """Render `<REFERENCE-QUALIFIER>@<REF-REV>` per §9.3.4."""
    if not references:
        return "upstream unknown@000000000000"
    ref = references[0]
    kind = str(ref.get("kind", ""))
    ref_id = str(ref.get("id", "unknown"))
    ident = ref.get("identity", {})
    if kind == "upstream_live":
        rev = str(ident.get("commit", ""))[:12]
        base = f"upstream {ref_id}@{rev}"
    elif kind == "sealed_dump":
        ucommit = str(ident.get("upstream_commit", ""))[:12]
        dsha = str(ident.get("dump_sha256", ""))[:12]
        base = f"sealed dump {ref_id}@{ucommit} (dump {dsha})"
    elif kind == "published_values":
        doi = str(ident.get("doi", ref_id))
        trun = str(ident.get("transcription_run_id", ""))[:12]
        base = f"published values {doi}@{trun}"
    elif kind == "experimental_data":
        acc = str(ident.get("accession") or ref_id)
        dsha = str(ident.get("dataset_sha256") or ident.get("accession") or "")[:12]
        base = f"experiment {acc} (VALIDITY)@{dsha}"
    elif kind == "own_prior_version":
        rev = str(ident.get("commit", ""))[:12]
        base = f"own prior version (REGRESSION)@{rev}"
    elif kind == "reference_implementation":
        rev = str(ident.get("commit", ""))[:12]
        base = f"reference implementation {ref_id}@{rev}"
    else:
        base = f"{kind} {ref_id}@000000000000"
    if len(references) > 1:
        auth = next((r.get("id") for r in references if r.get("authoritative")), references[0].get("id"))
        base = f"{base} (authoritative: {auth})"
    return base


def _coverage_parts_token(cov: Mapping[str, Any]) -> str | None:
    parts: list[str] = []
    nr = int(cov.get("not_run") or 0)
    nc = int(cov.get("not_covered") or 0)
    u = cov.get("knobs_uncovered")
    if nr > 0:
        parts.append(f"{nr} not run")
    if nc > 0:
        parts.append(f"{nc} not covered")
    if u is not None and int(u) > 0:
        noun = "knob" if int(u) == 1 else "knobs"
        parts.append(f"{int(u)} {noun} uncovered")
    if not parts:
        return None
    return f"partial ({', '.join(parts)})"


def _axis_tokens_for_clause(
    clause: Mapping[str, Any],
    *,
    include_knobs_not_computed: bool,
) -> list[str]:
    axes = clause["axes"]
    tokens: list[str] = []

    cov = axes["coverage"]
    if cov.get("value") != "full":
        pt = _coverage_parts_token(cov)
        if pt is not None:
            tokens.append(pt)
        if cov.get("knobs_uncovered") is None and include_knobs_not_computed:
            tokens.append("knobs not computed")

    fresh = axes["freshness"]
    if fresh.get("value") == "stale":
        tokens.append(f"stale ({int(fresh.get('stale_count') or 0)})")

    reg = axes["registration"]
    if reg.get("value") in ("retrospective", "amended"):
        tokens.append(str(reg["value"]))

    return tokens


def _render_clauses_and_shared_tokens(
    clauses: Sequence[Mapping[str, Any]],
    *,
    include_knobs_not_computed_in_axes: bool,
    force_inline_for_segment: bool = False,
) -> tuple[str, list[str]]:
    if not clauses:
        return "NO RESULT WITHIN-TOLERANCE 0/0", []

    per_clause_tokens = [
        _axis_tokens_for_clause(cl, include_knobs_not_computed=include_knobs_not_computed_in_axes)
        for cl in clauses
    ]

    if force_inline_for_segment:
        shared: list[str] = []
    else:
        # Shared tokens = tokens present in EVERY clause (preserving canonical order)
        first_set = set(per_clause_tokens[0])
        shared = [
            tok
            for tok in per_clause_tokens[0]
            if tok in first_set and all(tok in set(tlist) for tlist in per_clause_tokens[1:])
        ]

    shared_set = set(shared)
    rendered_clauses: list[str] = []
    for cl, tlist in zip(clauses, per_clause_tokens, strict=True):
        res_val = str(cl["axes"]["result"]["value"])
        kset = format_kind_set(
            cl["kinds"],
            fallback_from=cl.get("fallback_from"),
            incompat_class=cl.get("incompatibility_class"),
        )
        counts_txt = f"{int(cl['passing'])}/{int(cl['required'])}"
        inline = [tok for tok in tlist if tok not in shared_set]
        head = f"{res_val} {kset} {counts_txt}"
        if inline:
            rendered_clauses.append(f"{head}, {', '.join(inline)}")
        else:
            rendered_clauses.append(head)

    return "; ".join(rendered_clauses), shared


def _render_dual_display(dual: Mapping[str, Any] | None) -> str | None:
    if not dual:
        return None
    basis = str(dual.get("basis", "first_declared"))
    prefix = "as registered:" if basis == "registered" else "as first declared:"
    seg_clauses_text, _ = _render_clauses_and_shared_tokens(
        dual.get("clauses", []),
        include_knobs_not_computed_in_axes=True,
        force_inline_for_segment=True,
    )
    return f"{prefix} {seg_clauses_text}"


def _gate_counts(gates: Sequence[Mapping[str, Any]]) -> tuple[int, int, int, int]:
    enforce = sum(1 for g in gates if g.get("stage") == "enforce")
    advisory = sum(1 for g in gates if g.get("stage") == "advisory")
    shadow = sum(1 for g in gates if g.get("stage") == "shadow")
    not_active = sum(1 for g in gates if g.get("stage") == "not_active")
    return enforce, advisory, shadow, not_active


def _pairing_summary(report_body: Mapping[str, Any]) -> str:
    pairings = {
        str(c.get("pairing"))
        for c in report_body.get("cells", [])
        if c.get("pairing") in ("fixture", "live")
    }
    if not pairings:
        return "fixture"
    if len(pairings) == 1:
        return next(iter(pairings))
    return "mixed"


def _qualifiers(report_body: Mapping[str, Any]) -> list[str]:
    quals: list[str] = []
    parity_meta = report_body.get("parity_metadata", {})
    tool = report_body.get("tool", {})
    cells = report_body.get("cells", [])
    counts = report_body.get("cell_counts", {})
    clauses = report_body.get("clauses", [])

    if parity_meta.get("instrumentation") == "black_box":
        quals.append("black-box")
    if any(c.get("incompatibility_class") == "rng_scheme" for c in cells):
        quals.append("no RNG injection")

    contested_cnt = sum(1 for c in cells if c.get("verdict") == "contested")
    if contested_cnt > 0:
        quals.append(f"contested ({contested_cnt})")

    # declared deviations (k) unless result step 5 set the result (§9.3.4)
    any_step5 = any(
        "EXCEPT DECLARED DEVIATIONS" in str(cl["axes"]["result"]["value"]) for cl in clauses
    )
    dev_cnt = int(counts.get("declared_deviations") or 0)
    if dev_cnt > 0 and not any_step5:
        quals.append(f"declared deviations ({dev_cnt})")

    late_dev_cnt = sum(
        1 for c in cells if "deviation_declared_after_results" in c.get("flags", [])
    )
    if late_dev_cnt > 0:
        quals.append(f"deviations declared after results ({late_dev_cnt})")

    waived_cnt = int(counts.get("waived") or 0)
    if waived_cnt > 0:
        quals.append(f"waived ({waived_cnt})")

    incompat_cnt = sum(1 for c in cells if c.get("incompatibility_class"))
    if incompat_cnt > 0:
        quals.append(f"incompatibility declared ({incompat_cnt})")

    am_obj = report_body.get("amendments")
    if isinstance(am_obj, list) and am_obj:
        quals.append(f"amendments after results ({len(am_obj)})")

    if parity_meta.get("has_owner_stated_tolerance"):
        quals.append("owner-stated tolerance")

    if tool.get("third_party"):
        quals.append("kinds declared by us")
    if parity_meta.get("control"):
        quals.append("control")

    return quals


def render_t1(report_body: Mapping[str, Any], h2_full: str) -> str:
    """Render the full T1 badge line carrying ``T2 <h2[:12]>`` (§9.3.4)."""
    parity_meta = report_body.get("parity_metadata", {})
    tool = report_body.get("tool", {})
    clauses = report_body.get("clauses", [])
    counts = report_body.get("cell_counts", {})
    gates = report_body.get("gates", [])

    parts: list[str] = []
    if parity_meta.get("mode") == "exploratory":
        parts.append("EXPLORATORY")

    assur = clauses[0]["axes"]["assurance"] if clauses else {"value": "PROVISIONAL", "pending_count": 21}
    if assur.get("value") == "PROVISIONAL":
        k_pend = int(assur.get("pending_count") or 0)
        parts.append(f"PROVISIONAL ({k_pend} checks pending)")

    clauses_txt, shared_tokens = _render_clauses_and_shared_tokens(
        clauses,
        include_knobs_not_computed_in_axes=False,  # T1 puts 'knobs not computed' once in knobs segment
        force_inline_for_segment=False,
    )
    parts.append(clauses_txt)
    parts.extend(shared_tokens)

    dual_txt = _render_dual_display(report_body.get("dual_display_result"))
    if dual_txt:
        parts.append(dual_txt)

    impl_name = str(tool.get("name", "unknown"))
    impl_rev = str(tool.get("revision", "000000000000"))[:12]
    ref_qual = format_reference_qualifier(report_body.get("references", []))
    parts.append(f"{impl_name}@{impl_rev} vs {ref_qual}")

    parts.append(f"layer {parity_meta.get('layer_under_test', 'model')}")
    parts.append(f"tier {parity_meta.get('badge_min_tier', 'toy')}")
    parts.append(f"pairing {_pairing_summary(report_body)}")

    cov_knobs = counts.get("registry_knobs_covered")
    tot_knobs = counts.get("registry_knobs_non_excluded")
    if cov_knobs is not None and tot_knobs is not None:
        parts.append(f"knobs {int(cov_knobs)}/{int(tot_knobs)}")
    else:
        parts.append("knobs not computed")

    enf, adv, shd, na = _gate_counts(gates)
    parts.append(f"gates: {enf} enforce, {adv} advisory, {shd} shadow, {na} not active")

    parts.extend(_qualifiers(report_body))

    std_ver = str(report_body.get("standard_version", STANDARD_VERSION))
    parts.append(f"std {std_ver}")
    parts.append(f"T2 {h2_full[:12]}")
    return " · ".join(parts)


def render_t0(report_body: Mapping[str, Any], h1_full: str) -> str:
    """Render the short T0 badge carrying ``T1 <h1[:12]>`` (§9.3.4)."""
    parity_meta = report_body.get("parity_metadata", {})
    tool = report_body.get("tool", {})
    clauses = report_body.get("clauses", [])

    parts: list[str] = []
    if parity_meta.get("mode") == "exploratory":
        parts.append("EXPLORATORY")

    assur = clauses[0]["axes"]["assurance"] if clauses else {"value": "PROVISIONAL"}
    if assur.get("value") == "PROVISIONAL":
        parts.append("PROVISIONAL")

    clauses_txt, shared_tokens = _render_clauses_and_shared_tokens(
        clauses,
        include_knobs_not_computed_in_axes=True,
        force_inline_for_segment=False,
    )
    parts.append(clauses_txt)
    parts.extend(shared_tokens)

    dual_txt = _render_dual_display(report_body.get("dual_display_result"))
    if dual_txt:
        parts.append(dual_txt)

    impl_name = str(tool.get("name", "unknown"))
    impl_rev = str(tool.get("revision", "000000000000"))[:12]
    ref_qual = format_reference_qualifier(report_body.get("references", []))
    parts.append(f"{impl_name}@{impl_rev} vs {ref_qual}")

    if parity_meta.get("has_owner_stated_tolerance"):
        parts.append("owner-stated tolerance")

    std_ver = str(report_body.get("standard_version", STANDARD_VERSION))
    parts.append(f"std {std_ver}")
    parts.append(f"T1 {h1_full[:12]}")
    return " · ".join(parts)


def validate_badge(t0: str, t1: str, report_body: Mapping[str, Any]) -> list[str]:
    """Validate structural completeness of rendered T0 and T1 strings (§9.3.4)."""
    errors: list[str] = []
    for label, text, expected_link in (("T0", t0, "T1 "), ("T1", t1, "T2 ")):
        if not _IMPL_REV_RE.search(text):
            errors.append(f"{label} missing <IMPL>@<rev>")
        if " vs " not in text or "@" not in text.split(" vs ", 1)[1]:
            errors.append(f"{label} missing <REFERENCE-QUALIFIER>@<REF-REV>")
        if not _STD_VERSION_RE.search(text):
            errors.append(f"{label} missing 'std x.y.z'")
        if f"· {expected_link}" not in text:
            errors.append(f"{label} missing tier link '{expected_link}<hash>'")

    clauses = report_body.get("clauses", [])
    for cl in clauses:
        axes = cl["axes"]
        assur = axes["assurance"]["value"]
        cov = axes["coverage"]
        fresh = axes["freshness"]["value"]
        reg = axes["registration"]["value"]

        if assur == "PROVISIONAL":
            if not t0.startswith("PROVISIONAL") and "PROVISIONAL ·" not in t0:
                errors.append("T0 missing PROVISIONAL when assurance is PROVISIONAL")
            if "PROVISIONAL (" not in t1:
                errors.append("T1 missing 'PROVISIONAL (<k> checks pending)'")
        if cov["value"] == "partial":
            pt = _coverage_parts_token(cov)
            if pt and (pt not in t0 or pt not in t1):
                errors.append(f"badge missing coverage token {pt!r}")
            if cov.get("knobs_uncovered") is None and (
                "knobs not computed" not in t0 or "knobs not computed" not in t1
            ):
                errors.append("badge missing 'knobs not computed' for uncomputed u")
        if fresh == "stale":
            st_tok = f"stale ({int(axes['freshness'].get('stale_count') or 0)})"
            if st_tok not in t0 or st_tok not in t1:
                errors.append(f"badge missing non-default freshness token {st_tok!r}")
        if reg in ("retrospective", "amended") and (reg not in t0 or reg not in t1):
            errors.append(f"badge missing non-default registration token {reg!r}")

    if report_body.get("dual_display_result") is not None:
        basis = report_body["dual_display_result"].get("basis", "first_declared")
        seg = "as registered:" if basis == "registered" else "as first declared:"
        if seg not in t0 or seg not in t1:
            errors.append(f"badge missing required dual-display segment {seg!r}")

    # Sanity check known tokens
    _ = PARITY_KINDS_ORDER, DATA_TIERS
    return errors


def lint_rendered_text(text: str, *, checks_pending: bool) -> list[str]:
    """Enforce WP30a renderer lints (§9.3.4):

    1. Generated text must never contain bare ``parity`` (not followed by a kind word,
       ``record``, or ``report``).
    2. ``PASSES`` or ``VALID`` must never appear without ``PROVISIONAL`` on the same line
       while checks are pending (``X`` non-empty).
    """
    issues: list[str] = []
    for match in _BARE_PARITY_RE.finditer(text):
        snippet = text[max(0, match.start() - 20) : min(len(text), match.end() + 20)].replace("\n", " ")
        issues.append(f"bare 'parity' without kind word/'record'/'report' near {snippet!r}")

    if checks_pending:
        for lineno, line in enumerate(text.splitlines(), 1):
            if _PASSES_OR_VALID_RE.search(line) and "PROVISIONAL" not in line:
                issues.append(
                    f"line {lineno}: PASSES/VALID printed without PROVISIONAL while checks are pending"
                )
    return issues
