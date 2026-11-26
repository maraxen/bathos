"""WP17 parity gate ladder kit: mode-state templates, corpus layout, isolation checks, and legacy labeller.

Implements Spec 261123 §7.7 and §12.2 WP17 on top of ``cisternal.shadow``:
- Mode-state TOML template + strict validator requiring ``min_labels``,
  ``min_precision_lower_bound``, ``min_recall_lower_bound``, ``threshold_basis``, and
  ``writes_isolated = true``.
- Family corpus layout (``tests/gates/<family>/corpus/{positive,negative}``) for PF1-PF6.
- Shadow verdict/label emission and fail-closed ``writes_isolated`` verification via
  ``cisternal.shadow.isolation.prove_isolation``.
- Per-gate stage record discovery for the parity grader (without altering cell verdicts).
- Non-mutating ``legacy`` labeller preserving catalog verdict-column hashes.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import re
import tempfile
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cisternal.shadow.isolation import IsolationResult, prove_isolation
from cisternal.shadow.mode_state import VALID_STAGES, _validate_mode_data
from cisternal.shadow.resolution import ResolutionError, resolve_mode_state_dir

if TYPE_CHECKING:
    from bathos.schema import Run

# Spec 261123 §7.7: the 6 gate families (31 gates; PF1-PF5 + PF6 clean-room).
GATE_FAMILIES: dict[str, tuple[str, ...]] = {
    "PF1": ("G01", "G02", "G16", "G20", "pre_commit_judgement"),
    "PF2": ("G03", "G05", "G26", "run_start_anchoring", "union_gate"),
    "PF3": (
        "G04",
        "G06",
        "G10",
        "G11",
        "G12",
        "G13",
        "G14",
        "G15",
        "G19",
        "G21",
        "G23",
        "G24",
        "G25",
    ),
    "PF4": ("G07", "G22", "env_completeness", "blast_radius_propagation"),
    "PF5": ("G08", "G09", "alias_review"),
    "PF6": ("clean_room_hook",),
}

DEFAULT_MIN_LABELS = 60
DEFAULT_MIN_LOWER_BOUND = 0.90
MANDATORY_PROMOTION_KEYS: tuple[str, ...] = (
    "min_labels",
    "min_precision_lower_bound",
    "min_recall_lower_bound",
    "threshold_basis",
)

_G_CODE_FEATURE_RE = re.compile(r"^parity\.(g\d{2}[a-z]?)$", re.IGNORECASE)


class GateLadderConfigError(ValueError):
    """Raised when a WP17 gate mode-state TOML or family corpus configuration is invalid."""


def validate_gate_mode_state(data: Mapping[str, Any], *, where: str = "<mode_state>") -> dict[str, Any]:
    """Validate a gate mode-state mapping against cisternal.shadow + WP17 mandatory keys."""
    if not isinstance(data, Mapping):
        raise GateLadderConfigError(f"Gate mode-state at {where} must be a mapping")
    plain: dict[str, Any] = dict(data)
    try:
        _validate_mode_data(plain, where)
    except Exception as exc:
        raise GateLadderConfigError(str(exc)) from exc

    shadow = plain.get("shadow", {})
    if shadow.get("writes_isolated") is not True:
        raise GateLadderConfigError(
            f"Gate mode-state at {where}: [shadow] writes_isolated must be true"
        )
    enforced_by = shadow.get("writes_isolated_enforced_by")
    if not isinstance(enforced_by, str) or not enforced_by.strip():
        raise GateLadderConfigError(
            f"Gate mode-state at {where}: missing non-empty [shadow] writes_isolated_enforced_by"
        )

    promo = plain.get("promotion", {})
    missing = [k for k in MANDATORY_PROMOTION_KEYS if k not in promo or promo[k] is None]
    if missing:
        raise GateLadderConfigError(
            f"Gate mode-state at {where}: missing mandatory [promotion] key(s): {', '.join(missing)}"
        )
    if not isinstance(promo["threshold_basis"], str) or not promo["threshold_basis"].strip():
        raise GateLadderConfigError(
            f"Gate mode-state at {where}: [promotion] threshold_basis must be non-empty"
        )
    if int(promo["min_labels"]) < 1:
        raise GateLadderConfigError(
            f"Gate mode-state at {where}: [promotion] min_labels must be >= 1"
        )
    return plain


def load_gate_mode_state(path: Path | str) -> dict[str, Any]:
    """Load and validate a WP17 gate mode-state TOML file."""
    p = Path(path).resolve()
    if not p.is_file():
        raise GateLadderConfigError(f"Gate mode-state file not found: {p}")
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise GateLadderConfigError(f"Failed to parse gate mode-state TOML at {p}: {exc}") from exc
    return validate_gate_mode_state(data, where=str(p))


def render_gate_mode_state_template(
    feature_name: str,
    *,
    description: str,
    enforcer_symbol: str,
    threshold_basis: str,
    version: str = "0.1.0",
    current_stage: str = "shadow",
    target_stage: str = "advisory",
    min_labels: int = DEFAULT_MIN_LABELS,
    min_precision_lower_bound: float = DEFAULT_MIN_LOWER_BOUND,
    min_recall_lower_bound: float = DEFAULT_MIN_LOWER_BOUND,
    min_verdicts: int | None = None,
    allowed_label_sources: Sequence[str] = ("frozen_gold", "owner_confirmed", "at_work"),
) -> str:
    """Render a strict WP17 mode-state TOML string and validate it before returning."""
    if current_stage not in VALID_STAGES or target_stage not in VALID_STAGES:
        raise GateLadderConfigError(
            f"Invalid stage transition {current_stage!r} -> {target_stage!r}; expected one of {VALID_STAGES}"
        )
    eff_min_verdicts = min_verdicts if min_verdicts is not None else min_labels
    srcs_toml = ", ".join(json.dumps(str(s)) for s in allowed_label_sources)
    text = (
        f"[feature]\n"
        f"name = {json.dumps(feature_name)}\n"
        f"description = {json.dumps(description)}\n"
        f"version = {json.dumps(version)}\n\n"
        f"[ladder]\n"
        f"current_stage = {json.dumps(current_stage)}\n\n"
        f"[shadow]\n"
        f"traffic_fraction = 1.0\n"
        f"writes_isolated = true\n"
        f"writes_isolated_enforced_by = {json.dumps(enforcer_symbol)}\n\n"
        f"[scoring]\n"
        f"positive_label = true\n\n"
        f"[promotion]\n"
        f"target_stage = {json.dumps(target_stage)}\n"
        f"min_verdicts = {int(eff_min_verdicts)}\n"
        f"min_labels = {int(min_labels)}\n"
        f"allowed_label_sources = [{srcs_toml}]\n"
        f"min_precision_lower_bound = {float(min_precision_lower_bound):.2f}\n"
        f"min_recall_lower_bound = {float(min_recall_lower_bound):.2f}\n"
        f"isolation_violations_max = 0\n"
        f"threshold_basis = {json.dumps(threshold_basis)}\n"
    )
    validate_gate_mode_state(tomllib.loads(text), where=feature_name)
    return text


def corpus_dirs(root: Path | str, family: str) -> tuple[Path, Path]:
    """Return ``(positive_dir, negative_dir)`` for *family* under ``tests/gates/<family>/corpus``."""
    fam = family.strip().upper()
    if fam not in GATE_FAMILIES:
        raise GateLadderConfigError(f"Unknown gate family {family!r}; expected one of {sorted(GATE_FAMILIES)}")
    base = Path(root) / "tests" / "gates" / fam / "corpus"
    return base / "positive", base / "negative"


def ensure_corpus_layout(root: Path | str, family: str) -> tuple[Path, Path]:
    """Create and return the canonical PF-family positive/negative corpus directories."""
    pos_dir, neg_dir = corpus_dirs(root, family)
    pos_dir.mkdir(parents=True, exist_ok=True)
    neg_dir.mkdir(parents=True, exist_ok=True)
    return pos_dir, neg_dir


def load_corpus_cases(root: Path | str, family: str) -> dict[str, list[Path]]:
    """List sorted JSON/TOML case files in the family's positive and negative corpus dirs."""
    pos_dir, neg_dir = corpus_dirs(root, family)
    if not pos_dir.is_dir() or not neg_dir.is_dir():
        raise GateLadderConfigError(
            f"Missing corpus directories for {family!r}: expected {pos_dir} and {neg_dir}"
        )
    exts = {".json", ".toml"}
    pos = sorted(p for p in pos_dir.iterdir() if p.is_file() and p.suffix in exts)
    neg = sorted(p for p in neg_dir.iterdir() if p.is_file() and p.suffix in exts)
    return {"positive": pos, "negative": neg}


def flush_shadow_pipeline(timeout: float = 2.0) -> None:
    """Wait for any active cisternal telemetry queue to drain, then shut it down cleanly."""
    from cisternal.telemetry.pipeline import get_pipeline, shutdown_pipeline

    pipeline = get_pipeline()
    if pipeline is not None:
        q = getattr(pipeline, "_queue", None)
        if q is not None:
            deadline = time.monotonic() + max(0.0, timeout)
            while not q.empty() and time.monotonic() < deadline:
                time.sleep(0.005)
    shutdown_pipeline()


def _ensure_shadow_pipeline(log_dir: Path | str | None) -> None:
    if log_dir is None:
        return
    import cisternal
    from cisternal.telemetry.pipeline import get_pipeline, shutdown_pipeline

    target = Path(log_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    if get_pipeline() is not None:
        shutdown_pipeline()
    atexit.register(shutdown_pipeline)
    cisternal.init(log_dir=target)


def emit_gate_shadow_verdict(
    *,
    feature: str,
    subject_key: str,
    verdict: bool | dict[str, Any] | str | int,
    stage: str = "shadow",
    feature_version: str = "0.1.0",
    inputs_sha256: str = "",
    log_dir: Path | str | None = None,
    **extra_fields: Any,
) -> bool:
    """Emit a ``shadow.verdict`` telemetry record through cisternal."""
    from cisternal.shadow.events import emit_verdict

    _ensure_shadow_pipeline(log_dir)
    return emit_verdict(
        feature,
        subject_key,
        verdict,
        stage=stage,
        feature_version=feature_version,
        inputs_sha256=inputs_sha256,
        **extra_fields,
    )


def emit_gate_shadow_label(
    *,
    feature: str,
    subject_key: str,
    label: Any,
    label_source: str = "owner_confirmed",
    labeller: str = "owner",
    log_dir: Path | str | None = None,
    **extra_fields: Any,
) -> bool:
    """Emit a ``shadow.label`` telemetry record through cisternal."""
    from cisternal.shadow.events import emit_label

    _ensure_shadow_pipeline(log_dir)
    return emit_label(
        feature,
        subject_key,
        label,
        label_source=label_source,
        labeller=labeller,
        **extra_fields,
    )


def check_gate_writes_isolated(
    actor_live_fn: Callable[[], Any],
    actor_shadow_fn: Callable[[], Any],
    *,
    live_roots: Sequence[Path | str],
    shadow_log_dir: Path | str | None = None,
    feature: str | None = None,
    enforced_by: str | None = None,
) -> IsolationResult:
    """Verify via ``cisternal.shadow.prove_isolation`` that shadow execution writes nothing outside ``shadow_log_dir``."""
    roots = [Path(r).resolve() for r in live_roots]
    if shadow_log_dir is None:
        sdir = Path(tempfile.mkdtemp(prefix="bth_wp17_shadow_")).resolve()
    else:
        sdir = Path(shadow_log_dir).resolve()
        sdir.mkdir(parents=True, exist_ok=True)
    return prove_isolation(
        actor_live_fn=actor_live_fn,
        actor_shadow_fn=actor_shadow_fn,
        live_roots=roots,
        shadow_log_dir=sdir,
        check_roots=roots,
        feature=feature or "",
        enforced_by=enforced_by or "",
    )


def _default_repo_mode_state_dir(start_dir: Path | None) -> Path | None:
    if start_dir is not None:
        cur = Path(start_dir).resolve()
        for parent in (cur, *cur.parents):
            cand = parent / ".praxia" / "mode_state"
            if cand.is_dir():
                return cand
    pkg_repo_cand = Path(__file__).resolve().parents[3] / ".praxia" / "mode_state"
    if pkg_repo_cand.is_dir():
        return pkg_repo_cand
    return None


def load_parity_gate_stages(
    mode_state_dir: Path | str | None = None,
    *,
    start_dir: Path | None = None,
) -> dict[str, str]:
    """Load validated per-gate ``current_stage`` records from ``parity.g*.toml`` files."""
    if mode_state_dir is not None:
        mdir: Path | None = Path(mode_state_dir).resolve()
        if not mdir.is_dir():
            raise GateLadderConfigError(f"Mode-state directory not found: {mdir}")
    else:
        try:
            res = resolve_mode_state_dir(repo_root=start_dir)
            mdir = res.path
        except ResolutionError as exc:
            if "Layer 5 (off)" not in str(exc):
                raise GateLadderConfigError(str(exc)) from exc
            mdir = _default_repo_mode_state_dir(start_dir)
        if mdir is None or not mdir.is_dir():
            return {}

    stages: dict[str, str] = {}
    for toml_path in sorted(mdir.glob("parity.*.toml")):
        data = load_gate_mode_state(toml_path)
        feat_name = str(data["feature"]["name"]).strip()
        match = _G_CODE_FEATURE_RE.match(feat_name)
        if match is None:
            continue
        code = match.group(1).upper()
        stages[code] = str(data["ladder"]["current_stage"])
    return stages


def verdict_column_hash(runs: Sequence[Run]) -> str:
    """Compute canonical SHA-256 over ``(run.id, run.outcome)`` preserving immutability verification (§12.2 WP17)."""
    rows = sorted((str(r.id), str(r.outcome)) for r in runs)
    payload = json.dumps(rows, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def label_legacy_runs(runs: Sequence[Run], *, label: str = "legacy") -> list[dict[str, Any]]:
    """Produce non-mutating ``legacy`` labels for existing runs without modifying catalog verdict columns."""
    vhash = verdict_column_hash(runs)
    return [
        {
            "run_id": str(r.id),
            "label": label,
            "outcome": str(r.outcome),
            "verdict_column_sha256": vhash,
        }
        for r in runs
    ]


def label_legacy_grade(grade_record: Mapping[str, Any], *, label: str = "legacy") -> dict[str, Any]:
    """Return a non-mutating legacy label view of a Ring 0/literature grade dict."""
    out = dict(grade_record)
    out["label"] = label
    return out


def label_legacy_attestation(attestation_record: Mapping[str, Any], *, label: str = "legacy") -> dict[str, Any]:
    """Return a non-mutating legacy label view of an existing ``oracle_match`` attestation."""
    out = dict(attestation_record)
    out["label"] = label
    return out
