"""Badge renderer (`T0`/`T1`), badge validation, and disclosure lints (WP30a).

Implements Spec 261123 §9.3.3, §9.3.4, §9.3.5, and §12.2 WP30a.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from bathos.parity.schema import DATA_TIERS, PARITY_KINDS_ORDER, STANDARD_VERSION

# Word 'parity' MUST be followed by one of: a kind word, "record(s)", or "report(s)" (§9.3.4, Rev 9 L2260).
# Hyphenated/underscored identifiers (e.g. "aminx-parity-v1", "my_parity_check") are excluded.
_BARE_PARITY_RE = re.compile(
    r"(?<![\w./-])parity(?![\w/-]|\.\w)(?!\s+(?:bitwise|within[- ]tolerance|distributional|behavioural|association|approximate|records?|reports?)\b)",
    re.IGNORECASE,
)
_PASSES_OR_VALID_RE = re.compile(r"\b(?:PASSES|VALID)\b")
_PROVISIONAL_TOKEN_RE = re.compile(r"(?<![a-zA-Z0-9_-])\bPROVISIONAL\b(?![_-])")
_STD_VERSION_RE = re.compile(r"\bstd\s+\d+\.\d+\.\d+\b")


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


def _cell_coverage_token(cov: Mapping[str, Any]) -> str | None:
    parts: list[str] = []
    nr = int(cov.get("not_run") or 0)
    nc = int(cov.get("not_covered") or 0)
    if nr > 0:
        parts.append(f"{nr} not run")
    if nc > 0:
        parts.append(f"{nc} not covered")
    if not parts:
        return None
    return f"partial ({', '.join(parts)})"


def _knob_coverage_token(cov: Mapping[str, Any]) -> str | None:
    u = cov.get("knobs_uncovered")
    if u is not None and int(u) > 0:
        noun = "knob" if int(u) == 1 else "knobs"
        return f"partial ({int(u)} {noun} uncovered)"
    return None


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


def _clauses_differ_in_cell_coverage(clauses: Sequence[Mapping[str, Any]]) -> bool:
    if len(clauses) <= 1:
        return False
    pairs = {
        (
            int(cl["axes"]["coverage"].get("not_run") or 0),
            int(cl["axes"]["coverage"].get("not_covered") or 0),
        )
        for cl in clauses
    }
    return len(pairs) > 1


def _axis_tokens_for_clause(
    clause: Mapping[str, Any],
    *,
    include_knobs_not_computed: bool,
    split_cell_and_knob_coverage: bool = False,
) -> list[str]:
    axes = clause["axes"]
    tokens: list[str] = []

    cov = axes["coverage"]
    if cov.get("value") != "full":
        if split_cell_and_knob_coverage:
            cell_pt = _cell_coverage_token(cov)
            if cell_pt is not None:
                tokens.append(cell_pt)
            if include_knobs_not_computed:
                if cov.get("knobs_uncovered") is None:
                    tokens.append("knobs not computed")
                else:
                    knob_pt = _knob_coverage_token(cov)
                    if knob_pt is not None:
                        tokens.append(knob_pt)
        else:
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

    split_cov = (not force_inline_for_segment) and _clauses_differ_in_cell_coverage(clauses)
    per_clause_tokens = [
        _axis_tokens_for_clause(
            cl,
            include_knobs_not_computed=include_knobs_not_computed_in_axes,
            split_cell_and_knob_coverage=split_cov,
        )
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
        include_knobs_not_computed_in_axes=False,
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
    if parity_meta.get("weights_attested_only"):
        quals.append("weights attested-only")

    if tool.get("third_party"):
        quals.append("kinds declared by us")
    if parity_meta.get("control"):
        quals.append("control")

    ev_level = str(report_body.get("evidence_level", "self_reported"))
    quals.append("reexecuted" if ev_level == "reexecuted" else "self-reported")
    if parity_meta.get("reference_self_reported"):
        quals.append("reference self-reported")

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

    ev_level = str(report_body.get("evidence_level", "self_reported"))
    parts.append("reexecuted" if ev_level == "reexecuted" else "self-reported")
    if parity_meta.get("reference_self_reported"):
        parts.append("reference self-reported")

    std_ver = str(report_body.get("standard_version", STANDARD_VERSION))
    parts.append(f"std {std_ver}")
    parts.append(f"T1 {h1_full[:12]}")
    return " · ".join(parts)


def _has_impl_vs_ref_segment(segments: Sequence[str]) -> bool:
    for seg in segments:
        if " vs " not in seg:
            continue
        left, right = seg.split(" vs ", 1)
        if "@" not in left or "@" not in right:
            continue
        impl_name, impl_rev = left.rsplit("@", 1)
        ref_qual, ref_rev = right.rsplit("@", 1)
        if impl_name.strip() and impl_rev.strip() and ref_qual.strip() and ref_rev.strip():
            return True
    return False


def _split_top_level_commas(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")" and depth > 0:
            depth -= 1
        elif depth == 0 and text.startswith(", ", i):
            parts.append(text[start:i].strip())
            start = i + 2
            i += 2
            continue
        i += 1
    parts.append(text[start:].strip())
    return parts


def _segment_has_token(segments: Sequence[str], token: str) -> bool:
    if token in segments:
        return True
    for seg in segments:
        for clause_seg in seg.split("; "):
            # Inline axis tokens on clause segments are comma-separated after the clause head
            if ", " in clause_seg:
                _, tail = clause_seg.split(", ", 1)
                if token in _split_top_level_commas(tail):
                    return True
    return False


def validate_badge(t0: str, t1: str, report_body: Mapping[str, Any]) -> list[str]:
    """Validate structural completeness of rendered T0 and T1 strings (§9.3.4)."""
    errors: list[str] = []
    t0_segs = t0.split(" · ")
    t1_segs = t1.split(" · ")
    for label, text, segs, expected_link in (
        ("T0", t0, t0_segs, "T1 "),
        ("T1", t1, t1_segs, "T2 "),
    ):
        if not _has_impl_vs_ref_segment(segs):
            errors.append(f"{label} missing <IMPL>@<rev> vs <REFERENCE-QUALIFIER>@<REF-REV>")
        if not _STD_VERSION_RE.search(text):
            errors.append(f"{label} missing 'std x.y.z'")
        if not segs or not segs[-1].startswith(expected_link) or len(segs[-1]) <= len(expected_link):
            errors.append(f"{label} missing tier link '{expected_link}<hash>'")

    parity_meta = report_body.get("parity_metadata", {})
    if parity_meta.get("mode") == "exploratory":
        if not t0_segs or t0_segs[0] != "EXPLORATORY":
            errors.append("T0 missing leading EXPLORATORY when mode is exploratory")
        if not t1_segs or t1_segs[0] != "EXPLORATORY":
            errors.append("T1 missing leading EXPLORATORY when mode is exploratory")

    if parity_meta.get("has_owner_stated_tolerance"):
        if "owner-stated tolerance" not in t0_segs:
            errors.append("T0 missing 'owner-stated tolerance' qualifier")
        if "owner-stated tolerance" not in t1_segs:
            errors.append("T1 missing 'owner-stated tolerance' qualifier")

    ev_level = str(report_body.get("evidence_level", "self_reported"))
    expected_ev_tok = "reexecuted" if ev_level == "reexecuted" else "self-reported"
    if expected_ev_tok not in t0_segs:
        errors.append(f"T0 missing evidence level token {expected_ev_tok!r}")
    if expected_ev_tok not in t1_segs:
        errors.append(f"T1 missing evidence level token {expected_ev_tok!r}")
    if parity_meta.get("reference_self_reported"):
        if "reference self-reported" not in t0_segs:
            errors.append("T0 missing 'reference self-reported' qualifier")
        if "reference self-reported" not in t1_segs:
            errors.append("T1 missing 'reference self-reported' qualifier")
    if parity_meta.get("weights_attested_only") and "weights attested-only" not in t1_segs:
        errors.append("T1 missing 'weights attested-only' qualifier")

    clauses = report_body.get("clauses", [])
    split_cov = _clauses_differ_in_cell_coverage(clauses)
    saw_amended = False
    for cl in clauses:
        axes = cl["axes"]
        assur = axes["assurance"]["value"]
        cov = axes["coverage"]
        fresh = axes["freshness"]["value"]
        reg = axes["registration"]["value"]
        if reg == "amended":
            saw_amended = True

        if assur == "PROVISIONAL":
            if "PROVISIONAL" not in t0_segs:
                errors.append("T0 missing PROVISIONAL when assurance is PROVISIONAL")
            if not any(s.startswith("PROVISIONAL (") and "checks pending)" in s for s in t1_segs):
                errors.append("T1 missing 'PROVISIONAL (<k> checks pending)'")
        if cov["value"] == "partial":
            if split_cov:
                cell_pt = _cell_coverage_token(cov)
                if cell_pt and (not _segment_has_token(t0_segs, cell_pt) or not _segment_has_token(t1_segs, cell_pt)):
                    errors.append(f"badge missing coverage token {cell_pt!r}")
                if cov.get("knobs_uncovered") is None:
                    if not _segment_has_token(t0_segs, "knobs not computed") or not _segment_has_token(t1_segs, "knobs not computed"):
                        errors.append("badge missing 'knobs not computed' for uncomputed u")
                else:
                    knob_pt = _knob_coverage_token(cov)
                    if knob_pt:
                        if not _segment_has_token(t0_segs, knob_pt):
                            errors.append(f"T0 missing knob coverage token {knob_pt!r}")
                        if not _segment_has_token(t1_segs, knob_pt) and not any(s.startswith("knobs ") for s in t1_segs):
                            errors.append(f"T1 missing knob coverage token {knob_pt!r} or 'knobs <cov>/<tot>'")
            else:
                pt = _coverage_parts_token(cov)
                if pt and (not _segment_has_token(t0_segs, pt) or not _segment_has_token(t1_segs, pt)):
                    errors.append(f"badge missing coverage token {pt!r}")
                if cov.get("knobs_uncovered") is None and (
                    not _segment_has_token(t0_segs, "knobs not computed")
                    or not _segment_has_token(t1_segs, "knobs not computed")
                ):
                    errors.append("badge missing 'knobs not computed' for uncomputed u")
        if fresh == "stale":
            st_tok = f"stale ({int(axes['freshness'].get('stale_count') or 0)})"
            if not _segment_has_token(t0_segs, st_tok) or not _segment_has_token(t1_segs, st_tok):
                errors.append(f"badge missing non-default freshness token {st_tok!r}")
        if reg in ("retrospective", "amended") and (
            not _segment_has_token(t0_segs, reg) or not _segment_has_token(t1_segs, reg)
        ):
            errors.append(f"badge missing non-default registration token {reg!r}")

    dual = report_body.get("dual_display_result")
    if saw_amended and dual is None:
        errors.append("registration is 'amended' but dual_display_result is missing")
    if dual is not None:
        basis = dual.get("basis", "first_declared")
        seg = "as registered:" if basis == "registered" else "as first declared:"
        if not any(s.startswith(seg) for s in t0_segs) or not any(s.startswith(seg) for s in t1_segs):
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
            pass_match = _PASSES_OR_VALID_RE.search(line)
            if pass_match is not None:
                prov_match = _PROVISIONAL_TOKEN_RE.search(line)
                if prov_match is None or prov_match.start() > pass_match.start():
                    issues.append(
                        f"line {lineno}: PASSES/VALID printed without PROVISIONAL while checks are pending"
                    )
    return issues
