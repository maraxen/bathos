"""Parity record schema core, closed vocabularies, cell_id, and validator (WP03a).

Implements Spec 261123 §3.2, §3.3, §3.4, §3.6, §3.7, §3.10, §3.16, §3.17.1-4, §6.4, and §12.2 WP03a.
Folds held debts #2322, #2677, #3096 via the spec's declaration schema.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

STANDARD_VERSION = "1.0.0"

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+\Z")
_RECORD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,80}\Z")
_SUPERSEDES_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,80}@[0-9a-f]{12}\Z")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}\Z")

# Closed vocabularies (§3.2 - §3.10)
PRONGS = frozenset({"reimplementation", "optimisation", "approximation"})
LAYERS_UNDER_TEST = frozenset({"kernel", "module", "model", "pipeline", "api", "cli"})
INSTRUMENTATIONS = frozenset({"instrumented", "black_box"})
MODES = frozenset({"confirmatory", "exploratory"})
STRICTNESS_VALUES = frozenset({"inconclusive_blocks", "inconclusive_allowed"})

DATA_TIERS_ORDER = ("smoke", "toy", "production_shape")
DATA_TIERS = frozenset(DATA_TIERS_ORDER)
DATA_TIER_RANK: dict[str, int] = {tier: idx for idx, tier in enumerate(DATA_TIERS_ORDER)}

PAIRINGS = frozenset({"fixture", "live"})
POOLING_RULES = frozenset({"all_runs", "per_input"})

REFERENCE_ROLES = frozenset({"comparator", "specification"})
REFERENCE_KINDS = frozenset(
    {
        "upstream_live",
        "sealed_dump",
        "published_values",
        "experimental_data",
        "own_prior_version",
        "reference_implementation",
    }
)
LIVE_PAIRING_ALLOWED_REFS = frozenset(
    {"upstream_live", "own_prior_version", "reference_implementation"}
)

# Exact §3.4 order
PARITY_KINDS_ORDER = (
    "bitwise",
    "within_tolerance",
    "distributional",
    "behavioural",
    "association",
    "approximate",
)
PARITY_KINDS = frozenset(PARITY_KINDS_ORDER)
PARITY_KIND_ORDER_INDEX: dict[str, int] = {k: i for i, k in enumerate(PARITY_KINDS_ORDER)}

SUPPORT_MATRIX: dict[str, frozenset[str]] = {
    "upstream_live": PARITY_KINDS,
    "sealed_dump": PARITY_KINDS,
    "published_values": frozenset(
        {"within_tolerance", "distributional", "behavioural", "association", "approximate"}
    ),
    "experimental_data": frozenset({"distributional", "behavioural", "association", "approximate"}),
    "own_prior_version": PARITY_KINDS,
    "reference_implementation": PARITY_KINDS,
}

# Closed 8 verdicts (§3.2)
VERDICTS = frozenset(
    {
        "pass",
        "fail",
        "not_run",
        "not_covered",
        "waived",
        "inconclusive",
        "contested",
        "declared_deviation",
    }
)

OUTPUT_TYPES_BASE = frozenset(
    {"logits", "probabilities", "scores", "samples", "rankings", "structures", "energies"}
)

TOLERANCE_BASES = frozenset(
    {
        "decomposition_floor",
        "published_precision",
        "analytic_bound",
        "owner_stated",
        "static_error_bound",
    }
)

WAIVER_REASON_CODES = frozenset(
    {
        # alias-map equivalence / reasons (§4.3)
        "identical",
        "semantic",
        "divergence",
        "exclusion",
        "extension",
        "io_only",
        "device",
        "checkpoint_derived",
        "visualization",
        "training_only",
        # §3.10 additional reason codes
        "out_of_scope",
        "reference_unavailable",
        "refused_by_design",
        "not_exposed",
    }
)

INCOMPATIBILITY_CLASSES = frozenset(
    {
        "rng_scheme",
        "float_precision",
        "device_backend",
        "operation_ordering",
        "algorithm_substitution",
        "convention",
        "reference_defect",
    }
)

DISTRIBUTIONAL_PROCEDURES = frozenset({"ci_within_margin", "tost"})
MULTIPLICITY_VALUES = frozenset({"holm", "none"})

CORRELATION_RANK_METRICS = frozenset(
    {
        "pearson",
        "pearson_r",
        "pearsonr",
        "spearman",
        "spearman_rho",
        "spearmanr",
        "kendall",
        "kendall_tau",
        "kendalltau",
        "rank_correlation",
    }
)

# Published standard values for 1.0.0 (§3.17.4, §6.4)
STANDARD_POOL_CRITICAL_1_0_0: tuple[str, ...] = (
    "x64_enabled",
    "xla_flags",
    "device_kind",
    "platform",
    "git_sha",
    "torch_deterministic_algorithms",
    "torch_tf32",
    "python_version",
    "jax_version",
    "jaxlib_version",
    "torch_version",
    "cuda_version",
    "cudnn_version",
    "driver_version",
)

STANDARD_VALUES_1_0_0: dict[str, Any] = {
    "badge_min_tier": "production_shape",
    "mode": "confirmatory",
    "drift_status_max_age_days": 30,
    "pool_critical": STANDARD_POOL_CRITICAL_1_0_0,
    "floor_multiple": 4.0,
}

# All 26 gate check codes (§3.11.2, §9.3)
ALL_G_CODES: tuple[str, ...] = tuple(f"G{i:02d}" for i in range(1, 27))
INFORMATIONAL_G_CODES = frozenset({"G17", "G18"})
APPLICABLE_G_CODES: tuple[str, ...] = tuple(
    c for c in ALL_G_CODES if c not in INFORMATIONAL_G_CODES
)

# Blocks deferred to WP03b (§3.2)
WP03B_BLOCK_ERRORS: dict[str, str] = {
    "component": "[[component]] is owned by WP03b (component tree) and is refused until WP03b lands",
    "rng": "[rng] is owned by WP03b (RNG strategies) and is refused until WP03b lands",
    "check": "[[check]] is owned by WP03b (supplementary checks) and is refused until WP03b lands",
    "reference_set": "[reference_set] is owned by WP03b (multi-source comparator selection) and is refused until WP03b lands",
    "provenance": "[provenance] is owned by WP03b (port provenance and clean-room) and is refused until WP03b lands",
}


@dataclass(frozen=True)
class ValidationIssue:
    """Structured schema or gate validation issue."""

    path: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.path}: {self.message}"


class ParityValidationError(ValueError):
    """Raised when a parity declaration or result fails schema/gate validation."""

    def __init__(self, issues: Sequence[ValidationIssue]) -> None:
        self.issues = list(issues)
        summary = "\n".join(str(i) for i in self.issues)
        super().__init__(f"Parity validation failed ({len(self.issues)} issue(s)):\n{summary}")


def canonical_json(obj: Any) -> str:
    """Serialize *obj* canonically with sorted keys and compact separators (shortest float repr)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def compute_cell_id(
    component_id: str,
    use_id: str,
    knob_setting: Any,
    axis_value: Any,
    data_tier: str,
    pairing: str,
) -> str:
    """Compute the 16-hex deterministic ``cell_id`` per §3.2."""
    payload = "|".join(
        [
            component_id,
            use_id,
            canonical_json(knob_setting),
            canonical_json(axis_value),
            data_tier,
            pairing,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _is_nonempty_str(val: Any) -> bool:
    return isinstance(val, str) and bool(val.strip())


def _require_fields(
    tbl: Mapping[str, Any],
    prefix: str,
    fields: Sequence[str],
    issues: list[ValidationIssue],
) -> None:
    for field in fields:
        if field not in tbl or tbl[field] is None:
            issues.append(
                ValidationIssue(f"{prefix}.{field}", "G01_MISSING_FIELD", f"missing required field '{prefix}.{field}'")
            )
        elif isinstance(tbl[field], str) and not tbl[field].strip():
            issues.append(
                ValidationIssue(f"{prefix}.{field}", "G01_EMPTY_FIELD", f"'{prefix}.{field}' must be non-empty")
            )


def _require_nonempty_str_fields(
    tbl: Mapping[str, Any],
    prefix: str,
    fields: Sequence[str],
    issues: list[ValidationIssue],
) -> None:
    for field in fields:
        if field not in tbl or tbl[field] is None:
            issues.append(
                ValidationIssue(f"{prefix}.{field}", "G01_MISSING_FIELD", f"missing required field '{prefix}.{field}'")
            )
        elif not isinstance(tbl[field], str):
            issues.append(
                ValidationIssue(f"{prefix}.{field}", "G01_TYPE", f"'{prefix}.{field}' must be a string")
            )
        elif not tbl[field].strip():
            issues.append(
                ValidationIssue(f"{prefix}.{field}", "G01_EMPTY_FIELD", f"'{prefix}.{field}' must be non-empty")
            )


def _validate_reference_identity(
    kind: str,
    identity: Any,
    prefix: str,
    issues: list[ValidationIssue],
) -> None:
    if not isinstance(identity, Mapping):
        issues.append(ValidationIssue(f"{prefix}.identity", "G01_TYPE", "identity must be a table"))
        return
    if kind == "upstream_live":
        _require_nonempty_str_fields(identity, f"{prefix}.identity", ("repo", "commit"), issues)
    elif kind == "sealed_dump":
        _require_nonempty_str_fields(identity, f"{prefix}.identity", ("dump_sha256", "upstream_commit"), issues)
        dsha = identity.get("dump_sha256")
        if isinstance(dsha, str) and dsha.strip() and not _SHA256_HEX_RE.match(dsha.strip().lower()):
            issues.append(
                ValidationIssue(f"{prefix}.identity.dump_sha256", "G01_VALUE", "dump_sha256 must be 64-hex sha256")
            )
        if not (_is_nonempty_str(identity.get("producer_record_id")) or _is_nonempty_str(identity.get("producer_run_id"))):
            issues.append(
                ValidationIssue(
                    f"{prefix}.identity.producer_run_id",
                    "G01_MISSING_FIELD",
                    "sealed_dump identity requires 'producer_record_id' or 'producer_run_id'",
                )
            )
    elif kind == "published_values":
        _require_nonempty_str_fields(identity, f"{prefix}.identity", ("doi", "locator", "transcription_run_id"), issues)
    elif kind == "experimental_data":
        _require_nonempty_str_fields(identity, f"{prefix}.identity", ("assay",), issues)
        if not (_is_nonempty_str(identity.get("accession")) or _is_nonempty_str(identity.get("dataset_sha256"))):
            issues.append(
                ValidationIssue(
                    f"{prefix}.identity.accession",
                    "G01_MISSING_FIELD",
                    "experimental_data identity requires 'accession' or 'dataset_sha256'",
                )
            )
    elif kind == "own_prior_version":
        _require_nonempty_str_fields(identity, f"{prefix}.identity", ("repo", "commit"), issues)
    elif kind == "reference_implementation":
        _require_nonempty_str_fields(identity, f"{prefix}.identity", ("repo", "commit", "entrypoint"), issues)


def _validate_use_entry(
    use: Any,
    prefix: str,
    issues: list[ValidationIssue],
    *,
    allow_fallback_only_kinds: bool = False,
) -> None:
    if not isinstance(use, Mapping):
        issues.append(ValidationIssue(prefix, "G01_TYPE", f"{prefix} must be a table"))
        return
    _require_fields(
        use,
        prefix,
        ("id", "purpose", "output_type", "quantity", "parity_kind", "metric", "negative_control", "judgement_id"),
        issues,
    )
    _require_nonempty_str_fields(
        use,
        prefix,
        ("id", "purpose", "output_type", "quantity", "parity_kind", "judgement_id"),
        issues,
    )
    if "output_type" in use:
        ot = use["output_type"]
        if not isinstance(ot, str) or (ot not in OUTPUT_TYPES_BASE and not ot.startswith("other:")):
            issues.append(
                ValidationIssue(
                    f"{prefix}.output_type",
                    "G01_ENUM",
                    f"invalid output_type {ot!r}",
                )
            )

    kind = use.get("parity_kind")
    if kind is not None and kind not in PARITY_KINDS:
        issues.append(ValidationIssue(f"{prefix}.parity_kind", "G01_ENUM", f"unknown parity_kind {kind!r}"))
    if allow_fallback_only_kinds and kind not in ("distributional", "behavioural"):
        issues.append(
            ValidationIssue(
                f"{prefix}.parity_kind",
                "G01_PRECISION_AMP_KIND",
                f"precision_amplification fallback_use.parity_kind must be 'distributional' or 'behavioural', got {kind!r}",
            )
        )

    metric = use.get("metric")
    if isinstance(metric, Mapping):
        _require_nonempty_str_fields(metric, f"{prefix}.metric", ("id", "impl"), issues)
        metric_id = str(metric.get("id", "")).strip().lower()
        if metric_id in CORRELATION_RANK_METRICS and kind in PARITY_KINDS and kind != "association":
            issues.append(
                ValidationIssue(
                    f"{prefix}.metric.id",
                    "G10_CORRELATION_REQUIRES_ASSOCIATION",
                    f"correlation/rank metric {metric_id!r} requires parity_kind='association', got {kind!r}",
                )
            )
    elif metric is not None:
        issues.append(ValidationIssue(f"{prefix}.metric", "G01_TYPE", "metric must be a table"))

    neg = use.get("negative_control")
    if isinstance(neg, Mapping):
        _require_nonempty_str_fields(neg, f"{prefix}.negative_control", ("kind", "impl", "expect"), issues)
    elif neg is not None:
        issues.append(
            ValidationIssue(f"{prefix}.negative_control", "G01_TYPE", "negative_control must be a table")
        )

    if kind == "association":
        _require_fields(use, prefix, ("pairing_unit", "min_n", "interval", "threshold", "multiplicity"), issues)
        _require_nonempty_str_fields(use, prefix, ("pairing_unit", "multiplicity"), issues)
        min_n = use.get("min_n")
        if min_n is not None and (isinstance(min_n, bool) or not isinstance(min_n, int) or min_n < 3):
            issues.append(
                ValidationIssue(f"{prefix}.min_n", "G01_MIN_N", f"association min_n must be integer >= 3, got {min_n!r}")
            )
        mult = use.get("multiplicity")
        if mult is not None and mult not in MULTIPLICITY_VALUES:
            issues.append(
                ValidationIssue(f"{prefix}.multiplicity", "G01_ENUM", f"multiplicity must be holm|none, got {mult!r}")
            )
    elif kind == "distributional":
        _require_fields(use, prefix, ("delta", "independent_unit", "procedure", "alpha", "multiplicity"), issues)
        _require_nonempty_str_fields(use, prefix, ("independent_unit", "procedure", "multiplicity"), issues)
        if use.get("procedure") not in DISTRIBUTIONAL_PROCEDURES:
            issues.append(
                ValidationIssue(
                    f"{prefix}.procedure",
                    "G01_ENUM",
                    f"distributional procedure must be ci_within_margin|tost, got {use.get('procedure')!r}",
                )
            )
        if use.get("multiplicity") not in MULTIPLICITY_VALUES:
            issues.append(
                ValidationIssue(
                    f"{prefix}.multiplicity",
                    "G01_ENUM",
                    f"multiplicity must be holm|none, got {use.get('multiplicity')!r}",
                )
            )
    elif kind == "behavioural":
        _require_fields(use, prefix, ("decision_function", "threshold"), issues)
        _require_nonempty_str_fields(use, prefix, ("decision_function",), issues)
    elif kind == "approximate":
        _require_fields(
            use,
            prefix,
            ("quality_metric", "quality_budget", "gained_quantity", "gained_measurement"),
            issues,
        )
        _require_nonempty_str_fields(use, prefix, ("quality_metric", "gained_quantity", "gained_measurement"), issues)

    if "floor_multiple" in use:
        fm = use["floor_multiple"]
        if (
            isinstance(fm, bool)
            or not isinstance(fm, (int, float))
            or not math.isfinite(float(fm))
            or fm <= 0
        ):
            issues.append(
                ValidationIssue(f"{prefix}.floor_multiple", "G01_VALUE", "floor_multiple must be finite and > 0")
            )
        if not _is_nonempty_str(use.get("judgement_id")):
            issues.append(
                ValidationIssue(
                    f"{prefix}.judgement_id",
                    "G01_MISSING_FIELD",
                    "per-use floor_multiple requires non-empty judgement_id",
                )
            )

    if "precision_amplification" in use:
        pa = use["precision_amplification"]
        if kind not in ("bitwise", "within_tolerance"):
            issues.append(
                ValidationIssue(
                    f"{prefix}.precision_amplification",
                    "G01_PRECISION_AMP_HOST_KIND",
                    "precision_amplification is valid only on bitwise or within_tolerance uses",
                )
            )
        if not isinstance(pa, Mapping):
            issues.append(
                ValidationIssue(
                    f"{prefix}.precision_amplification",
                    "G01_TYPE",
                    "precision_amplification must be a table",
                )
            )
        else:
            _require_fields(
                pa,
                f"{prefix}.precision_amplification",
                ("fallback_use", "max_useful_tolerance"),
                issues,
            )
            if "fallback_use" in pa:
                _validate_use_entry(
                    pa["fallback_use"],
                    f"{prefix}.precision_amplification.fallback_use",
                    issues,
                    allow_fallback_only_kinds=True,
                )
            mut = pa.get("max_useful_tolerance")
            if isinstance(mut, Mapping):
                for dtype_name, band in mut.items():
                    if not isinstance(band, Mapping):
                        issues.append(
                            ValidationIssue(
                                f"{prefix}.precision_amplification.max_useful_tolerance.{dtype_name}",
                                "G01_TYPE",
                                "max_useful_tolerance entry must have {atol, rtol}",
                            )
                        )
                    else:
                        _require_fields(
                            band,
                            f"{prefix}.precision_amplification.max_useful_tolerance.{dtype_name}",
                            ("atol", "rtol"),
                            issues,
                        )
            elif mut is not None:
                issues.append(
                    ValidationIssue(
                        f"{prefix}.precision_amplification.max_useful_tolerance",
                        "G01_TYPE",
                        "max_useful_tolerance must be a table keyed by dtype",
                    )
                )


def validate_declaration(data: Mapping[str, Any]) -> list[ValidationIssue]:
    """Validate a parsed parity declaration TOML dict for WP03a (G01 + G02)."""
    issues: list[ValidationIssue] = []

    # WP03b-owned block refusals
    for block_key, msg in WP03B_BLOCK_ERRORS.items():
        if block_key in data:
            issues.append(ValidationIssue(block_key, "WP03B_UNAVAILABLE", msg))  # gitleaks:allow

    # Required top-level sections (D blocks)
    for req_section in ("parity", "implementation", "reference", "registry", "use", "cell_plan", "environment"):
        if req_section not in data:
            issues.append(
                ValidationIssue(req_section, "G01_MISSING_SECTION", f"missing required section [{req_section}]")
            )

    # [parity]
    parity = data.get("parity")
    mode: str | None = None
    if isinstance(parity, Mapping):
        _require_fields(
            parity,
            "parity",
            (
                "schema_version",
                "record_id",
                "prong",
                "layer_under_test",
                "instrumentation",
                "mode",
                "strictness",
                "prior_runs",
                "badge_min_tier",
            ),
            issues,
        )
        _require_nonempty_str_fields(
            parity,
            "parity",
            (
                "schema_version",
                "record_id",
                "prong",
                "layer_under_test",
                "instrumentation",
                "mode",
                "strictness",
                "badge_min_tier",
            ),
            issues,
        )
        sv = parity.get("schema_version")
        if sv is not None and (not isinstance(sv, str) or not _SEMVER_RE.match(sv)):
            issues.append(
                ValidationIssue(
                    "parity.schema_version",
                    "G01_SCHEMA_VERSION",
                    f"schema_version must match MAJOR.MINOR.PATCH, got {sv!r}",
                )
            )
        rid = parity.get("record_id")
        if rid is not None and (not isinstance(rid, str) or not _RECORD_ID_RE.match(rid)):
            issues.append(
                ValidationIssue(
                    "parity.record_id",
                    "G01_RECORD_ID",
                    f"invalid record_id {rid!r}",
                )
            )
        if parity.get("prong") is not None and parity.get("prong") not in PRONGS:
            issues.append(ValidationIssue("parity.prong", "G01_ENUM", f"invalid prong {parity.get('prong')!r}"))
        if parity.get("layer_under_test") is not None and parity.get("layer_under_test") not in LAYERS_UNDER_TEST:
            issues.append(
                ValidationIssue(
                    "parity.layer_under_test",
                    "G01_ENUM",
                    f"invalid layer_under_test {parity.get('layer_under_test')!r}",
                )
            )
        if parity.get("instrumentation") is not None and parity.get("instrumentation") not in INSTRUMENTATIONS:
            issues.append(
                ValidationIssue(
                    "parity.instrumentation",
                    "G01_ENUM",
                    f"invalid instrumentation {parity.get('instrumentation')!r}",
                )
            )
        mode = parity.get("mode") if isinstance(parity.get("mode"), str) else None
        if parity.get("mode") is not None and mode not in MODES:
            issues.append(ValidationIssue("parity.mode", "G01_ENUM", f"invalid mode {parity.get('mode')!r}"))
        if parity.get("strictness") is not None and parity.get("strictness") not in STRICTNESS_VALUES:
            issues.append(
                ValidationIssue("parity.strictness", "G01_ENUM", f"invalid strictness {parity.get('strictness')!r}")
            )
        if parity.get("badge_min_tier") is not None and parity.get("badge_min_tier") not in DATA_TIERS:
            issues.append(
                ValidationIssue(
                    "parity.badge_min_tier",
                    "G01_ENUM",
                    f"invalid badge_min_tier {parity.get('badge_min_tier')!r}",
                )
            )
        if "prior_runs" in parity:
            pr_list = parity["prior_runs"]
            if not isinstance(pr_list, list) or any(not isinstance(item, str) for item in pr_list):
                issues.append(ValidationIssue("parity.prior_runs", "G01_TYPE", "prior_runs must be a list of strings"))
        if "control" in parity and not isinstance(parity["control"], bool):
            issues.append(ValidationIssue("parity.control", "G01_TYPE", "control must be a boolean"))
        sup = parity.get("supersedes")
        if sup not in (None, ""):
            if not isinstance(sup, str) or not _SUPERSEDES_RE.match(sup):
                issues.append(
                    ValidationIssue(
                        "parity.supersedes",
                        "G01_SUPERSEDES",
                        f"supersedes must match '<record_id>@<12-hex>', got {sup!r}",
                    )
                )
            if not isinstance(data.get("amendment"), Mapping):
                issues.append(
                    ValidationIssue(
                        "amendment",
                        "G01_MISSING_SECTION",
                        "[amendment] is required when parity.supersedes is set (§3.2)",
                    )
                )
    elif parity is not None:
        issues.append(ValidationIssue("parity", "G01_TYPE", "[parity] must be a table"))

    # [implementation]
    impl = data.get("implementation")
    if isinstance(impl, Mapping):
        _require_nonempty_str_fields(impl, "implementation", ("name", "repo", "licence"), issues)
        if "revision" in impl:
            issues.append(
                ValidationIssue(
                    "implementation.revision",
                    "G01_FORBIDDEN_FIELD",
                    "[implementation] must not declare 'revision' (recorded per run under G06)",
                )
            )
    elif impl is not None:
        issues.append(ValidationIssue("implementation", "G01_TYPE", "[implementation] must be a table"))

    # [[reference]]
    refs = data.get("reference")
    single_ref_kind: str | None = None
    if isinstance(refs, list):
        if not refs:
            issues.append(ValidationIssue("reference", "G01_EMPTY", "at least one [[reference]] entry is required"))
        elif len(refs) > 1:
            issues.append(
                ValidationIssue(
                    "reference",
                    "WP03B_UNAVAILABLE",
                    "multiple [[reference]] sources require WP03b ([reference_set])",
                )
            )
        else:
            ref = refs[0]
            if not isinstance(ref, Mapping):
                issues.append(ValidationIssue("reference[0]", "G01_TYPE", "reference[0] must be a table"))
            else:
                _require_fields(ref, "reference[0]", ("id", "role", "kind", "identity", "licence"), issues)
                _require_nonempty_str_fields(ref, "reference[0]", ("id", "role", "kind", "licence"), issues)
                if ref.get("role") is not None and ref.get("role") not in REFERENCE_ROLES:
                    issues.append(
                        ValidationIssue("reference[0].role", "G01_ENUM", f"invalid role {ref.get('role')!r}")
                    )
                rkind = ref.get("kind")
                if rkind is not None:
                    if rkind not in REFERENCE_KINDS:
                        issues.append(
                            ValidationIssue("reference[0].kind", "G01_ENUM", f"invalid reference kind {rkind!r}")
                        )
                    else:
                        single_ref_kind = rkind
                        if "identity" in ref:
                            _validate_reference_identity(rkind, ref["identity"], "reference[0]", issues)
                if _is_nonempty_str(ref.get("use_restriction")) and not _is_nonempty_str(
                    ref.get("permitted_use_basis")
                ):
                    issues.append(
                        ValidationIssue(
                            "reference[0].permitted_use_basis",
                            "G01_MISSING_FIELD",
                            "permitted_use_basis is required when use_restriction is non-empty",
                        )
                    )
    elif refs is not None:
        issues.append(ValidationIssue("reference", "G01_TYPE", "[[reference]] must be an array of tables"))

    # [registry]
    reg = data.get("registry")
    if isinstance(reg, Mapping):
        _require_fields(reg, "registry", ("registry_id", "drift_status_max_age_days"), issues)
        _require_nonempty_str_fields(reg, "registry", ("registry_id",), issues)
        reg_id = reg.get("registry_id")
        if isinstance(reg_id, str) and reg_id.strip() and not _SHA256_HEX_RE.match(reg_id.strip()):
            issues.append(
                ValidationIssue("registry.registry_id", "G01_VALUE", f"registry_id must be 64-hex sha256, got {reg_id!r}")
            )
        age = reg.get("drift_status_max_age_days")
        if age is not None and (isinstance(age, bool) or not isinstance(age, int) or age < 0):
            issues.append(
                ValidationIssue("registry.drift_status_max_age_days", "G01_TYPE", "must be non-negative int")
            )
    elif reg is not None:
        issues.append(ValidationIssue("registry", "G01_TYPE", "[registry] must be a table"))

    # [[use]]
    uses_by_id: dict[str, Mapping[str, Any]] = {}
    uses = data.get("use")
    any_within_tol = False
    if isinstance(uses, list):
        if not uses:
            issues.append(ValidationIssue("use", "G01_EMPTY", "at least one [[use]] is required"))
        for idx, use in enumerate(uses):
            _validate_use_entry(use, f"use[{idx}]", issues)
            if isinstance(use, Mapping):
                uid = use.get("id")
                if _is_nonempty_str(uid):
                    if uid in uses_by_id:
                        issues.append(
                            ValidationIssue(f"use[{idx}].id", "G01_DUPLICATE", f"duplicate use id {uid!r}")
                        )
                    uses_by_id[str(uid)] = use
                if use.get("parity_kind") == "within_tolerance":
                    any_within_tol = True
    elif uses is not None:
        issues.append(ValidationIssue("use", "G01_TYPE", "[[use]] must be an array of tables"))

    # [[tolerance]]
    tolerances = data.get("tolerance", [])
    if tolerances is not None and not isinstance(tolerances, list):
        issues.append(ValidationIssue("tolerance", "G01_TYPE", "[[tolerance]] must be an array of tables"))
        tolerances = []
    for idx, tol in enumerate(tolerances):
        prefix = f"tolerance[{idx}]"
        if not isinstance(tol, Mapping):
            issues.append(ValidationIssue(prefix, "G01_TYPE", "tolerance entry must be a table"))
            continue
        _require_fields(tol, prefix, ("id", "dtype", "applies_to", "atol", "rtol", "basis"), issues)
        _require_nonempty_str_fields(tol, prefix, ("id", "dtype", "basis"), issues)
        applies = tol.get("applies_to")
        if applies is not None and (
            not isinstance(applies, list)
            or not applies
            or any(not _is_nonempty_str(item) for item in applies)
        ):
            issues.append(
                ValidationIssue(f"{prefix}.applies_to", "G01_VALUE", "applies_to must be a non-empty list of strings")
            )
        for num_f in ("atol", "rtol"):
            val = tol.get(num_f)
            if val is not None and (
                isinstance(val, bool)
                or not isinstance(val, (int, float))
                or not math.isfinite(float(val))
                or val < 0
            ):
                issues.append(ValidationIssue(f"{prefix}.{num_f}", "G01_VALUE", f"{num_f} must be finite and >= 0"))
        basis = tol.get("basis")
        if basis is not None and basis not in TOLERANCE_BASES:
            issues.append(ValidationIssue(f"{prefix}.basis", "G01_ENUM", f"invalid tolerance basis {basis!r}"))
        if basis == "decomposition_floor":
            _require_fields(tol, prefix, ("floor_multiple", "basis_evidence"), issues)
            fm = tol.get("floor_multiple")
            if fm is not None and (
                isinstance(fm, bool)
                or not isinstance(fm, (int, float))
                or not math.isfinite(float(fm))
                or fm <= 0
            ):
                issues.append(ValidationIssue(f"{prefix}.floor_multiple", "G01_VALUE", "must be finite and > 0"))
            be = tol.get("basis_evidence")
            if isinstance(be, Mapping):
                _require_fields(be, f"{prefix}.basis_evidence", ("run_id", "data_tier", "shapes", "dtypes"), issues)
                _require_nonempty_str_fields(be, f"{prefix}.basis_evidence", ("run_id", "data_tier"), issues)
                if be.get("data_tier") is not None and be.get("data_tier") not in DATA_TIERS:
                    issues.append(
                        ValidationIssue(
                            f"{prefix}.basis_evidence.data_tier",
                            "G01_ENUM",
                            f"invalid data_tier {be.get('data_tier')!r}",
                        )
                    )
        if basis == "owner_stated" and not _is_nonempty_str(tol.get("judgement_id")):
            issues.append(
                ValidationIssue(
                    f"{prefix}.judgement_id",
                    "G01_MISSING_FIELD",
                    "owner_stated tolerance requires non-empty judgement_id",
                )
            )

    # [[cell_plan]]
    cell_plan = data.get("cell_plan")
    badge_min_tier = parity.get("badge_min_tier") if isinstance(parity, Mapping) else None
    badge_min_rank = DATA_TIER_RANK.get(badge_min_tier, 0) if isinstance(badge_min_tier, str) else 0
    required_cell_count = 0
    has_confirmatory_prod = False

    if isinstance(cell_plan, list):
        if not cell_plan:
            issues.append(
                ValidationIssue("cell_plan", "G01_ZERO_REQUIRED_CELLS", "cell_plan must declare at least one required cell")
            )
        seen_cell_ids: set[str] = set()
        for idx, cell in enumerate(cell_plan):
            prefix = f"cell_plan[{idx}]"
            if not isinstance(cell, Mapping):
                issues.append(ValidationIssue(prefix, "G01_TYPE", "cell_plan entry must be a table"))
                continue
            _require_fields(
                cell,
                prefix,
                ("use_id", "component_id", "knob_setting", "axis_value", "data_tier", "pairing", "pooling_rule"),
                issues,
            )
            _require_nonempty_str_fields(
                cell,
                prefix,
                ("use_id", "component_id", "data_tier", "pairing", "pooling_rule"),
                issues,
            )
            uid = cell.get("use_id")
            use_obj = uses_by_id.get(uid) if isinstance(uid, str) else None
            if uid is not None and use_obj is None:
                issues.append(ValidationIssue(f"{prefix}.use_id", "G01_UNKNOWN_USE", f"unknown use_id {uid!r}"))

            dt = cell.get("data_tier")
            if dt is not None and dt not in DATA_TIERS:
                issues.append(ValidationIssue(f"{prefix}.data_tier", "G01_ENUM", f"invalid data_tier {dt!r}"))
            pairing = cell.get("pairing")
            if pairing is not None and pairing not in PAIRINGS:
                issues.append(ValidationIssue(f"{prefix}.pairing", "G01_ENUM", f"invalid pairing {pairing!r}"))
            pr = cell.get("pooling_rule")
            if pr is not None and pr not in POOLING_RULES:
                issues.append(ValidationIssue(f"{prefix}.pooling_rule", "G01_ENUM", f"invalid pooling_rule {pr!r}"))

            if "dump_keys" in cell:
                issues.append(
                    ValidationIssue(
                        f"{prefix}.dump_keys",
                        "G01_FORBIDDEN_FIELD",
                        "dump_keys is forbidden; use reference_members = { ours_key = 'dump_member_key' }",
                    )
                )
            if "reference_members" in cell:
                rm = cell["reference_members"]
                if not isinstance(rm, Mapping) or not rm:
                    issues.append(
                        ValidationIssue(
                            f"{prefix}.reference_members",
                            "G01_VALUE",
                            "reference_members must be a non-empty table mapping ours_key -> dump_member_key",
                        )
                    )
                else:
                    for ok, rk in rm.items():
                        if not _is_nonempty_str(ok) or not _is_nonempty_str(rk):
                            issues.append(
                                ValidationIssue(
                                    f"{prefix}.reference_members",
                                    "G01_VALUE",
                                    "reference_members keys and values must be non-empty strings",
                                )
                            )

            # G02 checks on (reference_kind, parity_kind) and (reference_kind, pairing)
            eff_kind = use_obj.get("parity_kind") if use_obj else None
            incompat = cell.get("incompatibility")
            if incompat is not None:
                if not isinstance(incompat, Mapping):
                    issues.append(ValidationIssue(f"{prefix}.incompatibility", "G01_TYPE", "must be a table"))
                else:
                    _require_fields(
                        incompat, f"{prefix}.incompatibility", ("class", "evidence", "fallback_use"), issues
                    )
                    _require_nonempty_str_fields(
                        incompat, f"{prefix}.incompatibility", ("class", "evidence"), issues
                    )
                    icls = incompat.get("class")
                    if icls is not None and (
                        not isinstance(icls, str)
                        or (icls not in INCOMPATIBILITY_CLASSES and not icls.startswith("other:"))
                    ):
                        issues.append(
                            ValidationIssue(
                                f"{prefix}.incompatibility.class",
                                "G01_ENUM",
                                f"invalid incompatibility class {icls!r}",
                            )
                        )
                    fb = incompat.get("fallback_use")
                    if fb is not None:
                        _validate_use_entry(fb, f"{prefix}.incompatibility.fallback_use", issues)
                        if isinstance(fb, Mapping) and fb.get("parity_kind") in PARITY_KINDS:
                            eff_kind = fb.get("parity_kind")
                            if eff_kind == "within_tolerance":
                                any_within_tol = True

            if (
                single_ref_kind in REFERENCE_KINDS
                and isinstance(eff_kind, str)
                and eff_kind in PARITY_KINDS
                and eff_kind not in SUPPORT_MATRIX[single_ref_kind]
            ):
                issues.append(
                    ValidationIssue(
                        f"{prefix}.use_id",
                        "G02_UNSUPPORTED_KIND",
                        f"reference kind {single_ref_kind!r} cannot support parity_kind {eff_kind!r}",
                    )
                )
            if (
                pairing == "live"
                and single_ref_kind in REFERENCE_KINDS
                and single_ref_kind not in LIVE_PAIRING_ALLOWED_REFS
            ):
                issues.append(
                    ValidationIssue(
                        f"{prefix}.pairing",
                        "G02_LIVE_PAIRING_FORBIDDEN",
                        f"pairing='live' is not valid for reference kind {single_ref_kind!r}",
                    )
                )

            exploratory_flag = bool(cell.get("exploratory", False))
            if (
                isinstance(dt, str)
                and dt in DATA_TIER_RANK
                and DATA_TIER_RANK[dt] >= badge_min_rank
                and not exploratory_flag
            ):
                required_cell_count += 1
            if dt == "production_shape" and mode == "confirmatory" and not exploratory_flag:
                has_confirmatory_prod = True

            if all(
                k in cell
                for k in ("component_id", "use_id", "knob_setting", "axis_value", "data_tier", "pairing")
            ) and isinstance(dt, str) and isinstance(pairing, str):
                cid = compute_cell_id(
                    str(cell["component_id"]),
                    str(cell["use_id"]),
                    cell["knob_setting"],
                    cell["axis_value"],
                    dt,
                    pairing,
                )
                if cid in seen_cell_ids:
                    issues.append(
                        ValidationIssue(prefix, "G01_DUPLICATE_CELL", f"duplicate cell_id {cid}")
                    )
                seen_cell_ids.add(cid)

        if required_cell_count == 0:
            issues.append(
                ValidationIssue(
                    "cell_plan",
                    "G01_ZERO_REQUIRED_CELLS",
                    "plan has zero required cells at or above badge_min_tier",
                )
            )
    elif cell_plan is not None:
        issues.append(ValidationIssue("cell_plan", "G01_TYPE", "[[cell_plan]] must be an array of tables"))

    if any_within_tol and not tolerances:
        issues.append(
            ValidationIssue(
                "tolerance",
                "G01_MISSING_SECTION",
                "[[tolerance]] is required when any use or fallback_use declares within_tolerance",
            )
        )

    # [[waiver]]
    waivers = data.get("waiver", [])
    if waivers is not None and not isinstance(waivers, list):
        issues.append(ValidationIssue("waiver", "G01_TYPE", "[[waiver]] must be an array of tables"))
        waivers = []
    for idx, w in enumerate(waivers):
        prefix = f"waiver[{idx}]"
        if not isinstance(w, Mapping):
            issues.append(ValidationIssue(prefix, "G01_TYPE", "waiver entry must be a table"))
            continue
        _require_fields(w, prefix, ("cell_selector", "reason_code", "reason_text", "judgement_id"), issues)
        _require_nonempty_str_fields(w, prefix, ("reason_code", "reason_text", "judgement_id"), issues)
        rc = w.get("reason_code")
        if rc is not None and rc not in WAIVER_REASON_CODES:
            issues.append(
                ValidationIssue(f"{prefix}.reason_code", "G01_ENUM", f"invalid waiver reason_code {rc!r}")
            )

    # [[assumption]] (§3.16)
    assumptions = data.get("assumption", [])
    if assumptions is not None and not isinstance(assumptions, list):
        issues.append(ValidationIssue("assumption", "G01_TYPE", "[[assumption]] must be an array of tables"))
        assumptions = []
    if has_confirmatory_prod and not assumptions:
        issues.append(
            ValidationIssue(
                "assumption",
                "G01_ASSUMPTIONS_REQUIRED",
                "[[assumption]] must be non-empty when any confirmatory production_shape cell is declared (§3.16)",
            )
        )
    for idx, a in enumerate(assumptions):
        prefix = f"assumption[{idx}]"
        if not isinstance(a, Mapping):
            issues.append(ValidationIssue(prefix, "G01_TYPE", "assumption entry must be a table"))
            continue
        _require_fields(a, prefix, ("id", "text", "load_bearing", "status"), issues)
        _require_nonempty_str_fields(a, prefix, ("id", "text", "status"), issues)
        st = a.get("status")
        if st == "probed" and not _is_nonempty_str(a.get("evidence")):
            issues.append(
                ValidationIssue(f"{prefix}.evidence", "G01_MISSING_FIELD", "probed assumption requires 'evidence'")
            )
        elif st == "unprobed" and not _is_nonempty_str(a.get("probe")):
            issues.append(
                ValidationIssue(f"{prefix}.probe", "G01_MISSING_FIELD", "unprobed assumption requires 'probe'")
            )
        elif st is not None and st not in ("probed", "unprobed"):
            issues.append(ValidationIssue(f"{prefix}.status", "G01_ENUM", f"invalid status {st!r}"))

    # [environment]
    env = data.get("environment")
    if isinstance(env, Mapping):
        _require_fields(env, "environment", ("pool_critical", "frameworks"), issues)
        if "pool_critical" in env:
            pc = env["pool_critical"]
            if not isinstance(pc, list) or any(not isinstance(item, str) for item in pc):
                issues.append(ValidationIssue("environment.pool_critical", "G01_TYPE", "must be a list of strings"))
        fw = env.get("frameworks")
        if isinstance(fw, Mapping):
            _require_fields(fw, "environment.frameworks", ("ours", "reference"), issues)
            for side in ("ours", "reference"):
                if side in fw and (not isinstance(fw[side], list) or any(not isinstance(s, str) for s in fw[side])):
                    issues.append(
                        ValidationIssue(f"environment.frameworks.{side}", "G01_TYPE", f"{side} must be a list of strings")
                    )
        elif fw is not None:
            issues.append(ValidationIssue("environment.frameworks", "G01_TYPE", "frameworks must be a table"))
    elif env is not None:
        issues.append(ValidationIssue("environment", "G01_TYPE", "[environment] must be a table"))

    # [amendment]
    if "amendment" in data:
        am = data["amendment"]
        if not isinstance(am, Mapping):
            issues.append(ValidationIssue("amendment", "G01_TYPE", "[amendment] must be a table"))
        else:
            if "results_seen" in am:
                issues.append(
                    ValidationIssue(
                        "amendment.results_seen",
                        "G01_FORBIDDEN_FIELD",
                        "results_seen is derived, never declared (§3.17.1)",
                    )
                )
            _require_nonempty_str_fields(am, "amendment", ("reason", "judgement_id", "derived_from_observation"), issues)

    # [holdout]
    if "holdout" in data:
        ho = data["holdout"]
        if not isinstance(ho, Mapping):
            issues.append(ValidationIssue("holdout", "G01_TYPE", "[holdout] must be a table"))
        else:
            _require_fields(ho, "holdout", ("pool", "permutation", "floor"), issues)

    return issues


def validate_result_structure(result: Mapping[str, Any]) -> list[ValidationIssue]:
    """Validate structural conformance of a parity result object against WP03a/§3.2 result schema."""
    issues: list[ValidationIssue] = []
    required_top = (
        "schema_version",
        "standard_version",
        "record_id",
        "record_sha256",
        "grader_version",
        "grade_inputs",
        "evidence_runs",
        "cells",
        "components",
        "supplementary",
        "checks_admissibility",
        "cell_counts",
        "clauses",
        "gates",
        "amendments",
        "prior_records_on_pair",
    )
    _require_fields(result, "result", required_top, issues)
    _require_nonempty_str_fields(
        result, "result", ("schema_version", "standard_version", "record_id", "record_sha256", "grader_version"), issues
    )
    if "bundle_sha256" not in result:
        issues.append(
            ValidationIssue("result.bundle_sha256", "G01_MISSING_FIELD", "missing required field 'result.bundle_sha256'")
        )
    if result.get("schema_version") not in (None, "parity_result.v1"):
        issues.append(
            ValidationIssue("result.schema_version", "RESULT_SCHEMA", "schema_version must be 'parity_result.v1'")
        )
    sv = result.get("standard_version")
    if isinstance(sv, str) and not _SEMVER_RE.match(sv):
        issues.append(ValidationIssue("result.standard_version", "RESULT_SCHEMA", f"invalid semver {sv!r}"))
    rsha = result.get("record_sha256")
    if isinstance(rsha, str) and not _SHA256_HEX_RE.match(rsha):
        issues.append(ValidationIssue("result.record_sha256", "RESULT_SCHEMA", f"invalid record_sha256 {rsha!r}"))
    prp = result.get("prior_records_on_pair")
    if prp is not None and (isinstance(prp, bool) or not isinstance(prp, int) or prp < 0):
        issues.append(ValidationIssue("result.prior_records_on_pair", "RESULT_TYPE", "must be non-negative int"))

    gi = result.get("grade_inputs")
    if isinstance(gi, Mapping):
        for k in ("as_of", "target_revisions", "drift_run_id", "ledger_high_water_mark", "gate_stages", "comparison_backend"):
            if k not in gi:
                issues.append(ValidationIssue(f"result.grade_inputs.{k}", "RESULT_MISSING_FIELD", f"missing grade_inputs.{k}"))
        if "parity_store" in gi and gi["parity_store"] is not None:
            ps = gi["parity_store"]
            if not isinstance(ps, Mapping) or not _is_nonempty_str(ps.get("path")) or not _is_nonempty_str(ps.get("layer")):
                issues.append(ValidationIssue("result.grade_inputs.parity_store", "RESULT_TYPE", "parity_store must have non-empty path and layer"))
        if "evidence_run_ids_filter" in gi and gi["evidence_run_ids_filter"] is not None:
            erf = gi["evidence_run_ids_filter"]
            if not isinstance(erf, list) or any(not isinstance(x, str) for x in erf):
                issues.append(ValidationIssue("result.grade_inputs.evidence_run_ids_filter", "RESULT_TYPE", "evidence_run_ids_filter must be list of strings or null"))
        if "overlay_hooks" in gi and gi["overlay_hooks"] is not None:
            oh = gi["overlay_hooks"]
            if not isinstance(oh, list) or any(not isinstance(x, str) for x in oh):
                issues.append(ValidationIssue("result.grade_inputs.overlay_hooks", "RESULT_TYPE", "overlay_hooks must be list of strings"))
    elif gi is not None:
        issues.append(ValidationIssue("result.grade_inputs", "RESULT_TYPE", "grade_inputs must be an object"))

    if "parity_store" in result and result["parity_store"] is not None:
        ps = result["parity_store"]
        if not isinstance(ps, Mapping) or not _is_nonempty_str(ps.get("path")) or not _is_nonempty_str(ps.get("layer")):
            issues.append(ValidationIssue("result.parity_store", "RESULT_TYPE", "parity_store must have non-empty path and layer"))

    ev_runs = result.get("evidence_runs")
    if isinstance(ev_runs, list):
        for idx, er in enumerate(ev_runs):
            if not isinstance(er, Mapping):
                issues.append(ValidationIssue(f"result.evidence_runs[{idx}]", "RESULT_TYPE", "must be an object"))
                continue
            for k in ("run_id", "outcome", "validity_state", "env_blob_sha256", "declaration_blob_sha256"):
                if k not in er:
                    issues.append(
                        ValidationIssue(f"result.evidence_runs[{idx}].{k}", "RESULT_MISSING_FIELD", f"missing {k}")
                    )

    cells = result.get("cells")
    required_cell_keys = (
        "cell_id",
        "verdict",
        "overlays",
        "achieved_kinds",
        "declared_kind",
        "metric_values",
        "n",
        "tolerance_ref",
        "evidence_run_ids",
        "pooling_rule",
        "cell_evidence_sha256",
        "input_sha256",
        "pin_set",
        "nondegeneracy",
        "evidence_level",
        "flags",
    )
    if isinstance(cells, list):
        for idx, cell in enumerate(cells):
            if not isinstance(cell, Mapping):
                issues.append(ValidationIssue(f"result.cells[{idx}]", "RESULT_TYPE", "must be an object"))
                continue
            for ck in required_cell_keys:
                if ck not in cell:
                    issues.append(ValidationIssue(f"result.cells[{idx}].{ck}", "RESULT_MISSING_FIELD", f"missing {ck}"))
            if cell.get("verdict") not in VERDICTS:
                issues.append(
                    ValidationIssue(
                        f"result.cells[{idx}].verdict",
                        "RESULT_VERDICT",
                        f"invalid verdict {cell.get('verdict')!r}",
                    )
                )

    counts = result.get("cell_counts")
    if isinstance(counts, Mapping):
        for ck in (
            "required",
            "passing",
            "by_kind",
            "not_covered",
            "waived",
            "not_run",
            "declared_deviations",
            "registry_knobs_non_excluded",
            "registry_knobs_covered",
            "unplanned_knobs",
        ):
            if ck not in counts:
                issues.append(ValidationIssue(f"result.cell_counts.{ck}", "RESULT_MISSING_FIELD", f"missing {ck}"))

    clauses = result.get("clauses")
    if isinstance(clauses, list):
        for idx, clause in enumerate(clauses):
            if not isinstance(clause, Mapping):
                issues.append(ValidationIssue(f"result.clauses[{idx}]", "RESULT_TYPE", "must be an object"))
                continue
            _require_fields(clause, f"result.clauses[{idx}]", ("clause_id", "kinds", "passing", "required", "axes"), issues)
            axes = clause.get("axes")
            if not isinstance(axes, Mapping):
                issues.append(ValidationIssue(f"result.clauses[{idx}].axes", "RESULT_AXES", "axes missing"))
            else:
                _require_fields(
                    axes,
                    f"result.clauses[{idx}].axes",
                    ("result", "coverage", "assurance", "freshness", "registration"),
                    issues,
                )

    gates = result.get("gates")
    if isinstance(gates, list):
        for idx, g in enumerate(gates):
            if not isinstance(g, Mapping) or "code" not in g or "stage" not in g:
                issues.append(ValidationIssue(f"result.gates[{idx}]", "RESULT_GATES", "gate must have code and stage"))

    badge = result.get("badge")
    if badge is not None:
        if not isinstance(badge, Mapping):
            issues.append(ValidationIssue("result.badge", "RESULT_TYPE", "badge must be an object or null"))
        else:
            _require_nonempty_str_fields(badge, "result.badge", ("t0", "t1", "t1_sha256", "t2_sha256"), issues)

    return issues


def render_declaration_scaffold(
    record_id: str,
    *,
    impl_name: str = "my-tool",
    impl_repo: str = "https://github.com/example/my-tool",
    impl_licence: str = "MIT",
) -> str:
    """Render a commented Ring-0-valid ``*.parity.bth.toml`` scaffold template."""
    pool_str = ", ".join(f'"{k}"' for k in STANDARD_POOL_CRITICAL_1_0_0)
    return f'''[parity]
schema_version = "1.0.0"
record_id = "{record_id}"
prong = "reimplementation"
layer_under_test = "model"
instrumentation = "instrumented"
mode = "confirmatory"
strictness = "inconclusive_blocks"
prior_runs = []
control = false
badge_min_tier = "toy"

[implementation]
name = "{impl_name}"
repo = "{impl_repo}"
licence = "{impl_licence}"
third_party = false

[[reference]]
id = "upstream-ref"
role = "comparator"
kind = "sealed_dump"
identity = {{ dump_sha256 = "0000000000000000000000000000000000000000000000000000000000000000", producer_run_id = "run_0000", upstream_commit = "0000000000000000000000000000000000000000" }}
licence = "MIT"

[registry]
registry_id = "0000000000000000000000000000000000000000000000000000000000000000"
drift_status_max_age_days = 30

[[use]]
id = "score-main"
purpose = "Reproduce reference model scores"
output_type = "scores"
quantity = "Per-residue model score tensor"
parity_kind = "within_tolerance"
metric = {{ id = "max_abs_diff", impl = "bathos.parity.metrics:within_tolerance@builtin" }}
negative_control = {{ kind = "mutant", impl = "tests/parity/mutants.py::example_mutant@deadbeef", expect = "fail" }}
judgement_id = "jdg_example_use"

[[tolerance]]
id = "f32-owner"
dtype = "float32"
applies_to = ["model"]
atol = 1e-4
rtol = 1e-4
basis = "owner_stated"
judgement_id = "jdg_example_tol"

[[cell_plan]]
use_id = "score-main"
component_id = "model"
knob_setting = {{}}
axis_value = {{ input = "example" }}
data_tier = "toy"
pairing = "fixture"
pooling_rule = "all_runs"
tolerance_id = "f32-owner"

[environment]
pool_critical = [{pool_str}]
frameworks = {{ ours = ["jax"], reference = ["torch"] }}
'''
