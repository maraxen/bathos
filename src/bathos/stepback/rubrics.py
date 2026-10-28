"""5-Dimension Scored Rubric Evaluator and TOML Loader (spec 261027).

Implements:
- Hierarchical gap-free 3-tier recommendation logic
- Hard-floor veto (any score == 1 forces HOLD and Level 2)
- Flaw capping (any score == 2 caps recommendation at PIVOT)
- Canonical TOML loading and SHA-256 content verification
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomlkit as tomllib  # type: ignore[no-redef]

from bathos.errors import (
    RubricInvalidError,
    RubricNotFoundError,
    StepBackInvalidSchemaError,
)

DEFAULT_RUBRIC_REL_PATH = Path("agent_assets") / "rubrics" / "stepback_v1.toml"

CANONICAL_DIMENSIONS = [
    "question_validity",
    "empirical_progress",
    "theoretical_grounding",
    "resource_efficiency",
    "risk_confound_surface",
]

DEFAULT_WEIGHTS: dict[str, float] = {
    "question_validity": 0.20,
    "empirical_progress": 0.25,
    "theoretical_grounding": 0.20,
    "resource_efficiency": 0.15,
    "risk_confound_surface": 0.20,
}


def find_default_rubric_path() -> Path:
    """Resolve default rubric path relative to workspace or package."""
    # Check current directory and parents
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        cand = parent / DEFAULT_RUBRIC_REL_PATH
        if cand.is_file():
            return cand
    # Fallback: check relative to bathos package
    pkg_root = Path(__file__).parent.parent.parent.parent
    cand = pkg_root / DEFAULT_RUBRIC_REL_PATH
    if cand.is_file():
        return cand
    return cand


def load_rubric(rubric_path: Path | str | None = None) -> tuple[dict[str, Any], str]:
    """Load rubric TOML and return (rubric_dict, sha256_hash).

    Raises RubricNotFoundError if the file does not exist,
    or RubricInvalidError if the TOML syntax is bad or weights do not sum to 1.0.
    """
    path = Path(rubric_path) if rubric_path else find_default_rubric_path()
    if not path.is_file():
        raise RubricNotFoundError(f"Rubric file not found at: {path}")

    raw_bytes = path.read_bytes()
    rubric_sha256 = hashlib.sha256(raw_bytes).hexdigest()

    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise RubricInvalidError(f"Failed to parse rubric TOML: {exc}") from exc

    dimensions = data.get("dimensions", {})
    if not isinstance(dimensions, dict) or len(dimensions) != 5:
        raise RubricInvalidError(f"Rubric must define exactly 5 dimensions, found {len(dimensions)}")

    total_weight = 0.0
    for dim_name, dim_cfg in dimensions.items():
        if not isinstance(dim_cfg, dict) or "weight" not in dim_cfg:
            raise RubricInvalidError(f"Dimension '{dim_name}' missing required 'weight' field")
        try:
            total_weight += float(dim_cfg["weight"])
        except (ValueError, TypeError) as exc:
            raise RubricInvalidError(f"Invalid weight for dimension '{dim_name}': {exc}") from exc

    if abs(total_weight - 1.0) > 1e-5:
        raise RubricInvalidError(f"Rubric dimension weights must sum to 1.0, got {total_weight:.4f}")

    return data, rubric_sha256


def resolve_rubric_recommendation(scores: dict[str, int], composite: float) -> tuple[str, int]:
    """Hierarchical, gap-free recommendation and minimum escalation mapping.

    Tier 1: Hard-Floor Veto (Any dimension scored 1) -> ("HOLD", 2)
    Tier 2: Sub-nominal dimension present (Any dimension scored 2, all others >= 2)
            composite >= 3.4 -> ("PIVOT", 1)
            composite >= 2.6 -> ("HOLD", 2)
            composite >= 2.0 -> ("ESCALATE", 2)
            else             -> ("CONCLUDE_ABANDON", 2)
    Tier 3: Nominal/Strong performance (All dimensions >= 3)
            composite >= 4.2 -> ("CONTINUE", 0)
            composite >= 3.4 -> ("PIVOT", 1)
            composite >= 2.6 -> ("HOLD", 2)
            composite >= 2.0 -> ("ESCALATE", 2)
            else             -> ("CONCLUDE_ABANDON", 2)
    """
    # Tier 1: Hard-Floor Veto
    if any(s == 1 for s in scores.values()):
        return ("HOLD", 2)

    # Tier 2: Sub-nominal dimension present (Score 2 present, none is 1)
    if any(s == 2 for s in scores.values()):
        if composite >= 3.4:
            return ("PIVOT", 1)  # Capped at PIVOT even if composite >= 4.2
        elif composite >= 2.6:
            return ("HOLD", 2)
        elif composite >= 2.0:
            return ("ESCALATE", 2)
        else:
            return ("CONCLUDE_ABANDON", 2)

    # Tier 3: Nominal/Strong performance (All dimensions >= 3)
    if composite >= 4.2:
        return ("CONTINUE", 0)
    elif composite >= 3.4:
        return ("PIVOT", 1)
    elif composite >= 2.6:
        return ("HOLD", 2)
    elif composite >= 2.0:
        return ("ESCALATE", 2)
    else:
        return ("CONCLUDE_ABANDON", 2)


def evaluate_rubric(
    scores: dict[str, float | int],
    rubric_path: Path | str | None = None,
) -> tuple[float, str, int, str]:
    """Evaluate 5-dimension rubric scores and return (composite_score, recommendation, escalation_level, rubric_sha256).

    Raises:
    - StepBackInvalidSchemaError: if scores dict is missing dimensions or scores are out of bounds [1, 5].
    - RubricNotFoundError / RubricInvalidError: if rubric TOML cannot be loaded/verified.
    """
    rubric_data, rubric_sha256 = load_rubric(rubric_path)
    dimensions_cfg = rubric_data.get("dimensions", {})

    int_scores: dict[str, int] = {}
    composite = 0.0

    for dim_name in CANONICAL_DIMENSIONS:
        if dim_name not in scores:
            raise StepBackInvalidSchemaError(f"Missing required rubric dimension: '{dim_name}'")
        raw_val = scores[dim_name]
        if isinstance(raw_val, bool) or not isinstance(raw_val, int):
            raise StepBackInvalidSchemaError(f"Dimension '{dim_name}' score must be integer, got: {raw_val}")
        val = raw_val

        if val < 1 or val > 5:
            raise StepBackInvalidSchemaError(
                f"Dimension '{dim_name}' score {val} out of valid range [1, 5]"
            )
        int_scores[dim_name] = val

        weight = float(dimensions_cfg.get(dim_name, {}).get("weight", DEFAULT_WEIGHTS.get(dim_name, 0.2)))
        composite += weight * val

    composite = round(composite, 4)
    rec, level = resolve_rubric_recommendation(int_scores, composite)

    return composite, rec, level, rubric_sha256
