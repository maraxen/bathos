"""Script classification engine for bth classify.

Maps flat scripts in scripts/ into the correct taxonomy subdirectory
based on filename patterns, with optional conflict detection and sidecar scaffolding.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ClassificationConfidence(str, Enum):
    """Confidence level for classification."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class ClassificationResult:
    """Result of classifying a single script."""

    source: Path  # e.g. scripts/benchmark_efa_vs_pme.py
    target_dir: str  # e.g. "benchmarks"
    confidence: ClassificationConfidence
    rationale: str  # Human-readable explanation
    rename_required: bool  # True if stem doesn't match target naming convention
    suggested_stem: str | None = None  # New stem if rename required (e.g. "260526_diagnose_fire_nan")
    sidecar_required: bool = False  # True if target dir has sidecar=ERROR
    sidecar_path: Path | None = None  # Where the sidecar would live post-move
    existing_sidecar: Path | None = None  # Adjacent .bth.toml if one already exists at source


@dataclass
class MoveAction:
    """Represents a git mv operation."""

    source: Path
    destination: Path  # Full path including new stem
    classification: ClassificationResult
    conflict: bool = False  # True if a file already exists at destination
    conflict_path: Path | None = None


@dataclass
class ClassifyPlanResult:
    """Result of building a full classification plan."""

    project_root: Path
    actions: list[MoveAction] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)  # (path, reason)
    high_confidence: int = 0
    medium_confidence: int = 0
    low_confidence: int = 0
    conflicts: int = 0
    sidecars_to_scaffold: int = 0
    dry_run: bool = True


# Classification rules: (prefix_pattern, target_dir, confidence, rationale_template)
_CLASSIFICATION_RULES = [
    (r"^benchmark_", "benchmarks", ClassificationConfidence.HIGH, "matches benchmark_ prefix"),
    (r"^bench_", "benchmarks", ClassificationConfidence.HIGH, "matches bench_ prefix"),
    (r"^debug_", "debug", ClassificationConfidence.HIGH, "matches debug_ prefix"),
    (r"^diagnose_", "debug", ClassificationConfidence.HIGH, "matches diagnose_ prefix"),
    (r"^validate_", "validation", ClassificationConfidence.HIGH, "matches validate_ prefix"),
    (r"^analyze_", "analysis", ClassificationConfidence.HIGH, "matches analyze_ prefix"),
    (r"^analyse_", "analysis", ClassificationConfidence.HIGH, "matches analyse_ prefix"),
    (r"^profile_", "analysis", ClassificationConfidence.MEDIUM, "matches profile_ prefix (likely analysis)"),
    (r"^simulate_", "experiments", ClassificationConfidence.MEDIUM, "matches simulate_ prefix (likely experiment)"),
    (r"^run_", "experiments", ClassificationConfidence.MEDIUM, "matches run_ prefix (likely experiment)"),
    (r"^generate_", "data", ClassificationConfidence.MEDIUM, "matches generate_ prefix (likely data processing)"),
    (r"^export_", "data", ClassificationConfidence.MEDIUM, "matches export_ prefix (likely data processing)"),
    (r"^convert_", "data", ClassificationConfidence.MEDIUM, "matches convert_ prefix (likely data processing)"),
    (r"^extract_", "data", ClassificationConfidence.MEDIUM, "matches extract_ prefix (likely data processing)"),
    (r"^visualize_", "analysis", ClassificationConfidence.MEDIUM, "matches visualize_ prefix (likely analysis)"),
    (r"^inspect_", "analysis", ClassificationConfidence.MEDIUM, "matches inspect_ prefix (likely analysis)"),
    (r"^smoke_", "validation", ClassificationConfidence.MEDIUM, "matches smoke_ prefix (likely validation)"),
    (r"^check_", "analysis", ClassificationConfidence.MEDIUM, "matches check_ prefix (classified as analysis, not validation)"),
    (r"^verify_", "analysis", ClassificationConfidence.MEDIUM, "matches verify_ prefix (classified as analysis, not validation)"),
    (r"^test_", "validation", ClassificationConfidence.MEDIUM, "matches test_ prefix (likely validation)"),
    (r"^ablation_", "experiments", ClassificationConfidence.MEDIUM, "matches ablation_ prefix (likely experiment)"),
    (r"^compare_", "analysis", ClassificationConfidence.LOW, "matches compare_ prefix (ambiguous: experiment or analysis?)"),
    (r"^phase\d+_", "analysis", ClassificationConfidence.LOW, "matches phase*_ prefix (ambiguous, defaulting to analysis)"),
    (r"^update_", "analysis", ClassificationConfidence.LOW, "matches update_ prefix (ambiguous prefix)"),
    (r"^write_", "analysis", ClassificationConfidence.LOW, "matches write_ prefix (ambiguous prefix)"),
    (r"^sync_", "analysis", ClassificationConfidence.LOW, "matches sync_ prefix (ambiguous prefix)"),
]


def _infer_date_prefix(script_path: Path) -> str:
    """Infer YYMMDD date prefix from git log or file mtime.

    First tries git log --follow --diff-filter=A for the file's first commit date.
    Falls back to file mtime if not yet committed.

    Returns:
        YYMMDD string, or "000000" if all else fails.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--follow", "--diff-filter=A", "--format=%ai", "--", str(script_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Output format: "2026-02-15 14:23:45 +0000"
            date_str = result.stdout.strip().split()[0]  # "2026-02-15"
            # Convert to YYMMDD (last 2 digits of year + month + day)
            parts = date_str.split("-")
            if len(parts) == 3:
                year, month, day = parts
                # Take last 2 digits of year
                yy = year[-2:]
                return f"{yy}{month}{day}"
    except Exception:
        pass

    # Fallback: use file mtime
    try:
        import stat
        from datetime import datetime

        mtime = script_path.stat().st_mtime
        dt = datetime.fromtimestamp(mtime)
        return dt.strftime("%y%m%d")
    except Exception:
        pass

    return "000000"


def _scaffold_sidecar(destination: Path, script_path: Path, target_dir: str) -> None:
    """Write a minimal sidecar stub at the destination.

    Creates either experiment, benchmark, or validation sidecar depending on target_dir.

    Args:
        destination: Full destination path (e.g., scripts/experiments/simulate_foo.py)
        script_path: Original script path (for reference)
        target_dir: Target directory (e.g., "experiments")
    """
    sidecar_path = destination.parent / f"{destination.stem}.bth.toml"
    if sidecar_path.exists():
        return

    if target_dir == "experiments":
        content = """# AUTO-GENERATED by bth classify -- fill in all TODO fields before running bth run
[experiment]
hypothesis = "TODO: clear, falsifiable statement"

[outcomes.pass]
condition = "TODO: DuckDB SQL fragment e.g. metric < 0.01"
decision = "TODO: next step if hypothesis confirmed"
is_residual = false

[outcomes.fail]
condition = "TODO: DuckDB SQL fragment"
decision = "TODO: root cause step"
is_residual = false

[outcomes.residual]
condition = "true"
decision = "TODO: marginal/inconclusive disposition"
is_residual = true

[result_schema]
# TODO: add typed output fields e.g. metric_name = "float"
"""
    elif target_dir == "benchmarks":
        content = """# AUTO-GENERATED by bth classify -- fill in all TODO fields before running bth run
[benchmark]
baseline_ref = "TODO: run_uuid of reference run"
metric = "TODO: e.g. ns_per_day"
regression_threshold = 0.05
target = "TODO: qualitative goal e.g. >50 ns/day on pi_so3"

[result_schema]
# TODO: add typed output fields e.g. ns_per_day = "float"
"""
    elif target_dir == "validation":
        content = """# AUTO-GENERATED by bth classify -- fill in all TODO fields before running bth run
[validation]
property = "TODO: property being validated"
reference = "TODO: reference implementation or spec"
tolerance = "TODO: acceptable deviation"

[result_schema]
# TODO: add typed output fields
"""
    else:
        return

    sidecar_path.write_text(content)


def classify_flat_scripts(project_root: Path) -> list[ClassificationResult]:
    """Scan scripts/ root and classify all flat .py files.

    Skips:
    - Files already in subdirectories (e.g., scripts/experiments/foo.py)
    - Files starting with _ or __
    - .toml sidecar files

    Args:
        project_root: Project root directory (where scripts/ lives)

    Returns:
        List of ClassificationResult objects for flat scripts found.
    """
    scripts_dir = project_root / "scripts"
    if not scripts_dir.exists():
        return []

    results = []

    for script in sorted(scripts_dir.iterdir()):
        # Skip non-files and files starting with _
        if script.is_dir() or script.name.startswith("_"):
            continue

        # Only .py files
        if script.suffix != ".py":
            continue

        # Classify this script
        result = _classify_single_script(script, project_root)
        if result:
            results.append(result)

    return results


def _classify_single_script(script_path: Path, project_root: Path) -> ClassificationResult | None:
    """Classify a single script file.

    Args:
        script_path: Full path to the script (e.g., /proj/scripts/benchmark_foo.py)
        project_root: Project root

    Returns:
        ClassificationResult, or None if not classified (should not happen with LOW fallback)
    """
    stem = script_path.stem

    # Try each classification rule in order
    for prefix_pattern, target_dir, confidence, rationale in _CLASSIFICATION_RULES:
        if re.match(prefix_pattern, stem):
            return _build_classification_result(script_path, target_dir, confidence, rationale, project_root)

    # Fallback: anything else goes to analysis/LOW
    return _build_classification_result(
        script_path,
        "analysis",
        ClassificationConfidence.LOW,
        "no matching pattern, defaulting to analysis",
        project_root,
    )


def _build_classification_result(
    script_path: Path,
    target_dir: str,
    confidence: ClassificationConfidence,
    rationale: str,
    project_root: Path,
) -> ClassificationResult:
    """Build a ClassificationResult for a script.

    Checks naming convention for the target directory and infers rename if needed.

    Args:
        script_path: Full path to script (e.g., /proj/scripts/debug_foo.py)
        target_dir: Target directory name (e.g., "debug")
        confidence: Confidence level
        rationale: Explanation of classification
        project_root: Project root

    Returns:
        ClassificationResult with all fields populated.
    """
    stem = script_path.stem

    # Check if target directory requires YYMMDD or verb_noun naming
    from bathos.linter import _DIR_RULES

    target_rule = _DIR_RULES.get(target_dir, {})
    target_pattern = target_rule.get("pattern")
    sidecar_rule = target_rule.get("sidecar")

    # Determine if renaming is required
    rename_required = False
    suggested_stem = None

    if target_pattern:
        if not target_pattern.match(stem):
            # Naming doesn't match target convention
            rename_required = True

            # If target uses YYMMDD_desc convention, infer the date
            if target_dir in ("debug", "explore", "scratch"):
                date_prefix = _infer_date_prefix(script_path)
                suggested_stem = f"{date_prefix}_{stem}"
            # verb_noun directories keep the existing stem (assuming it already follows verb_noun)

    # Build sidecar paths
    sidecar_required = sidecar_rule is not None and sidecar_rule.value == "error"
    destination_stem = suggested_stem if rename_required else stem
    destination = project_root / "scripts" / target_dir / f"{destination_stem}.py"
    sidecar_path = destination.parent / f"{destination_stem}.bth.toml" if sidecar_required else None

    # Check for existing sidecar at source
    existing_sidecar = script_path.parent / f"{stem}.bth.toml"
    if not existing_sidecar.exists():
        existing_sidecar = None

    return ClassificationResult(
        source=script_path,
        target_dir=target_dir,
        confidence=confidence,
        rationale=rationale,
        rename_required=rename_required,
        suggested_stem=suggested_stem,
        sidecar_required=sidecar_required,
        sidecar_path=sidecar_path,
        existing_sidecar=existing_sidecar,
    )


def build_move_plan(project_root: Path, results: list[ClassificationResult]) -> ClassifyPlanResult:
    """Build a move plan from classification results, checking for conflicts.

    Args:
        project_root: Project root
        results: List of ClassificationResult objects

    Returns:
        ClassifyPlanResult with MoveActions and conflict detection.
    """
    plan = ClassifyPlanResult(project_root=project_root, dry_run=True)

    for result in results:
        # Count by confidence
        if result.confidence == ClassificationConfidence.HIGH:
            plan.high_confidence += 1
        elif result.confidence == ClassificationConfidence.MEDIUM:
            plan.medium_confidence += 1
        else:
            plan.low_confidence += 1

        if result.sidecar_required:
            plan.sidecars_to_scaffold += 1

        # Determine destination path
        destination_stem = result.suggested_stem if result.rename_required else result.source.stem
        destination = project_root / "scripts" / result.target_dir / f"{destination_stem}.py"

        # Check for conflict
        conflict = destination.exists()
        if conflict:
            plan.conflicts += 1

        action = MoveAction(
            source=result.source,
            destination=destination,
            classification=result,
            conflict=conflict,
            conflict_path=destination if conflict else None,
        )
        plan.actions.append(action)

    return plan


def apply_classify_plan(plan: ClassifyPlanResult, scaffold_sidecars: bool = True) -> None:
    """Execute the classification plan: git mv and sidecar scaffolding.

    Pre-validates all moves before executing any. Hard blocks if any move would fail
    (conflict, untracked source, missing destination parent).

    Args:
        plan: ClassifyPlanResult from build_move_plan
        scaffold_sidecars: Whether to create sidecar stubs for experiments/benchmarks

    Raises:
        RuntimeError: If validation fails or any git mv fails.
    """
    # Pre-validate all moves
    untracked_files = []
    for action in plan.actions:
        # Check if source is tracked
        try:
            result = subprocess.run(
                ["git", "ls-files", "--error-unmatch", str(action.source)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                untracked_files.append(action.source)
        except Exception as e:
            untracked_files.append(action.source)

    if untracked_files:
        files_list = "\n  ".join(str(f) for f in untracked_files)
        raise RuntimeError(
            f"Cannot apply plan: the following source files are untracked in git:\n"
            f"  {files_list}\n\n"
            f"Please `git add` or commit these files first, then retry."
        )

    # Check for conflicts
    conflicts = [a for a in plan.actions if a.conflict]
    if conflicts:
        conflicts_list = "\n  ".join(f"{a.source} → {a.conflict_path}" for a in conflicts)
        raise RuntimeError(
            f"Cannot apply plan: destination conflicts detected:\n"
            f"  {conflicts_list}\n\n"
            f"Please resolve these manually before retrying."
        )

    # Pre-create destination directories
    for action in plan.actions:
        action.destination.parent.mkdir(parents=True, exist_ok=True)

    # Execute git mv for each action
    for action in plan.actions:
        try:
            subprocess.run(
                ["git", "mv", str(action.source), str(action.destination)],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"git mv failed for {action.source} → {action.destination}:\n"
                f"{e.stderr}"
            ) from e

    # Scaffold sidecars if requested
    if scaffold_sidecars:
        for action in plan.actions:
            if action.classification.sidecar_required:
                _scaffold_sidecar(
                    action.destination,
                    action.source,
                    action.classification.target_dir,
                )
