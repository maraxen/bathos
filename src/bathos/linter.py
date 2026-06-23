from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from bathos.sidecar import find_sidecar


class IssueSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class LintIssue:
    path: Path
    directory: str
    issue: str
    severity: IssueSeverity
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.severity.value}: {self.path} — {self.issue}: {self.detail}"


_VERB_NOUN_RE = re.compile(r"^[a-z][a-z0-9]*_[a-z][a-z0-9_]*$")
_YYMMDD_RE = re.compile(r"^\d{6}_[a-z][a-z0-9_]*$")
_SLURM_VERB_NOUN_RE = re.compile(r"^[a-z][a-z0-9]*_[a-z][a-z0-9_]*$")
_NUMERIC_LITERAL_RE = re.compile(r"(?<![a-zA-Z_])\b-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b")


_DIR_RULES: dict[str, dict] = {
    "experiments": {
        "pattern": _VERB_NOUN_RE,
        "extensions": {".py"},
        "sidecar": IssueSeverity.ERROR,
    },
    "benchmarks": {
        "pattern": _VERB_NOUN_RE,
        "extensions": {".py"},
        "sidecar": IssueSeverity.ERROR,
    },
    "validation": {
        "pattern": _VERB_NOUN_RE,
        "extensions": {".py"},
        "sidecar": IssueSeverity.WARNING,
    },
    "analysis": {
        "pattern": _VERB_NOUN_RE,
        "extensions": {".py"},
        "sidecar": None,
    },
    "data": {
        "pattern": _VERB_NOUN_RE,
        "extensions": {".py"},
        "sidecar": None,
    },
    "slurm": {
        "pattern": _SLURM_VERB_NOUN_RE,
        "extensions": {".slurm"},
        "sidecar": None,
    },
    "debug": {
        "pattern": _YYMMDD_RE,
        "extensions": {".py"},
        "sidecar": None,
    },
    "explore": {
        "pattern": _YYMMDD_RE,
        "extensions": {".py"},
        "sidecar": None,
    },
    "scratch": {
        "pattern": _YYMMDD_RE,
        "extensions": {".py"},
        "sidecar": None,
    },
}


def lint_project(project_root: Path) -> list[LintIssue]:
    scripts_dir = project_root / "scripts"
    if not scripts_dir.exists():
        return []

    issues: list[LintIssue] = []

    for dir_name, rules in _DIR_RULES.items():
        dir_path = scripts_dir / dir_name
        if not dir_path.exists():
            continue

        for script in sorted(dir_path.iterdir()):
            if script.name.startswith(".") or script.name.startswith("_") or script.is_dir():
                continue

            # Skip sidecar files
            if script.suffix == ".toml" and script.name.endswith(".bth.toml"):
                continue

            stem = script.stem
            ext = script.suffix

            if ext not in rules["extensions"]:
                issues.append(LintIssue(
                    path=script,
                    directory=dir_name,
                    issue="naming",
                    severity=IssueSeverity.ERROR,
                    detail=f"expected extension {rules['extensions']}, got {ext!r}",
                ))
                continue

            if not rules["pattern"].match(stem):
                expected = "verb_noun" if dir_name not in ("debug", "explore", "scratch") else "YYMMDD_desc"
                issues.append(LintIssue(
                    path=script,
                    directory=dir_name,
                    issue="naming",
                    severity=IssueSeverity.ERROR,
                    detail=f"expected {expected} style, got {stem!r}",
                ))

            if rules["sidecar"] is not None and find_sidecar(script) is None:
                issues.append(LintIssue(
                    path=script,
                    directory=dir_name,
                    issue="missing_sidecar",
                    severity=rules["sidecar"],
                    detail=f"create {stem}.bth.toml next to this script",
                ))

    # Tier-1 checks for validation/production experiments
    issues.extend(check_novel_or_reproduces_declared(project_root))

    return issues


def check_novel_or_reproduces_declared(project_root: Path) -> list[LintIssue]:
    """Check that validation/production experiments declare [reproduction] or novel=true (AC-7, Tier-1).

    Filesystem-only check: reads sidecars directly from disk.
    Error-severity for validation/production stage_name values.

    Args:
        project_root: Path to project root.

    Returns:
        List of LintIssue objects with severity ERROR for validation/production violations.
    """
    from bathos.sidecar import parse_sidecar, SidecarKind

    scripts_dir = project_root / "scripts"
    if not scripts_dir.exists():
        return []

    issues: list[LintIssue] = []

    # Only check experiments and validation sidecars
    for dir_name in ["experiments", "validation"]:
        dir_path = scripts_dir / dir_name
        if not dir_path.exists():
            continue

        for script in sorted(dir_path.iterdir()):
            if script.name.startswith(".") or script.name.startswith("_") or script.is_dir():
                continue

            # Skip non-sidecar files
            if not (script.suffix == ".toml" and script.name.endswith(".bth.toml")):
                continue

            # Parse sidecar
            try:
                sidecar = parse_sidecar(script)
            except Exception:
                # Silently skip unparseable sidecars — other lint checks will catch them
                continue

            # Only enforce for experiment sidecars in validation/production stages
            if sidecar.kind != SidecarKind.EXPERIMENT:
                continue

            if sidecar.stage_name not in ("validation", "production"):
                continue

            # Check: must have [reproduction] or novel=true
            has_reproduction = (
                sidecar.reproduction is not None
                and (sidecar.reproduction.reproduces_paper or sidecar.reproduction.reproduces_run)
            )

            if not has_reproduction and not sidecar.novel:
                issues.append(LintIssue(
                    path=script,
                    directory=dir_name,
                    issue="NOVEL_OR_REPRODUCES_REQUIRED",
                    severity=IssueSeverity.ERROR,
                    detail=f"validation/production experiment must declare [reproduction] or novel=true",
                ))

    return issues


def check_residual_rates(catalog_dir: Path, threshold: float = 0.10) -> list[LintIssue]:
    """Check for high residual rates in campaigns.

    Joins campaign_runs + runs, groups by campaign_id, computes residual_count/total.
    Returns WARNING if rate > threshold.

    Args:
        catalog_dir: Path to catalog directory.
        threshold: Residual rate threshold (default 0.10 = 10%).

    Returns:
        List of LintIssue objects with severity WARNING.
    """
    import duckdb

    db_path = catalog_dir / "bathos.db"
    if not db_path.exists():
        return []

    try:
        db = duckdb.connect(str(db_path), read_only=True)
        db.execute("SET TimeZone='UTC'")

        # Check if campaign_runs and runs tables exist
        try:
            rows = db.execute(
                """
                SELECT
                    COALESCE(cr.campaign_id, r.campaign_id, '') as campaign_id,
                    COUNT(*) as total,
                    SUM(CASE WHEN r.outcome_is_residual THEN 1 ELSE 0 END) as residual_count
                FROM runs r
                LEFT JOIN campaign_runs cr ON r.id = cr.run_id
                GROUP BY COALESCE(cr.campaign_id, r.campaign_id, '')
                HAVING total > 0
            """
            ).fetchall()
        except Exception:
            # Tables don't exist or query failed
            db.close()
            return []

        issues: list[LintIssue] = []
        for campaign_id, total, residual_count in rows:
            residual_count = residual_count or 0
            rate = residual_count / total if total > 0 else 0
            if rate > threshold:
                issues.append(
                    LintIssue(
                        path=catalog_dir / "bathos.db",
                        directory="catalog",
                        issue="high_residual_rate",
                        severity=IssueSeverity.WARNING,
                        detail=f"Campaign {campaign_id[:8]}: {residual_count}/{total} "
                        f"residual ({rate:.1%})",
                    )
                )

        db.close()
        return issues
    except Exception:
        return []


def check_bypass_trend(catalog_dir: Path) -> list[LintIssue]:
    """Check for increasing bypass rate trend week-over-week.

    Queries runs from last 4 weeks grouped by week.
    Returns WARNING if latest week bypass rate > previous week.

    Args:
        catalog_dir: Path to catalog directory.

    Returns:
        List of LintIssue objects with severity WARNING.
    """
    import duckdb
    from datetime import UTC, datetime, timedelta

    db_path = catalog_dir / "bathos.db"
    if not db_path.exists():
        return []

    try:
        db = duckdb.connect(str(db_path), read_only=True)
        db.execute("SET TimeZone='UTC'")

        # Get last 4 weeks of data
        four_weeks_ago = datetime.now(UTC) - timedelta(weeks=4)

        try:
            rows = db.execute(
                """
                SELECT
                    DATE_TRUNC('week', timestamp) as week,
                    COUNT(*) as total,
                    SUM(CASE WHEN sidecar_mode = 'bypassed' THEN 1 ELSE 0 END) as bypassed_count
                FROM runs
                WHERE timestamp > ?
                GROUP BY week
                ORDER BY week DESC
                LIMIT 4
            """,
                [four_weeks_ago],
            ).fetchall()
        except Exception:
            db.close()
            return []

        if len(rows) < 2:
            db.close()
            return []

        # rows[0] is latest week, rows[1] is previous week
        latest_week, latest_total, latest_bypassed = rows[0]
        prev_week, prev_total, prev_bypassed = rows[1]

        latest_bypassed = latest_bypassed or 0
        prev_bypassed = prev_bypassed or 0

        latest_rate = latest_bypassed / latest_total if latest_total > 0 else 0
        prev_rate = prev_bypassed / prev_total if prev_total > 0 else 0

        issues: list[LintIssue] = []
        if latest_rate > prev_rate:
            issues.append(
                LintIssue(
                    path=catalog_dir / "bathos.db",
                    directory="catalog",
                    issue="increasing_bypass_trend",
                    severity=IssueSeverity.WARNING,
                    detail=f"Bypass rate increased: {prev_rate:.1%} "
                    f"(prev week) → {latest_rate:.1%} (latest week)",
                )
            )

        db.close()
        return issues
    except Exception:
        return []


def check_popper_adversarial(project_root: Path) -> list[LintIssue]:
    """Tier-2 advisory: warn for POPPER sidecars missing adversarial_check in all outcome branches.

    Scans scripts/experiments/**/*.bth.toml for sidecars with a [popper] block.
    Issues WARNING if none of the [outcomes.*] branches declare adversarial_check.

    Args:
        project_root: Path to project root.

    Returns:
        List of LintIssue objects with severity WARNING.
    """
    scripts_dir = project_root / "scripts" / "experiments"
    if not scripts_dir.exists():
        return []

    issues: list[LintIssue] = []
    for sidecar_path in sorted(scripts_dir.rglob("*.bth.toml")):
        try:
            data = tomllib.loads(sidecar_path.read_text())
        except Exception:
            continue

        # Only process sidecars with [experiment] + [popper] blocks
        if "experiment" not in data or "popper" not in data:
            continue

        outcomes = data.get("outcomes", {})
        has_adversarial = any(
            branch.get("adversarial_check") for branch in outcomes.values()
            if isinstance(branch, dict)
        )
        if not has_adversarial:
            issues.append(
                LintIssue(
                    path=sidecar_path,
                    directory="experiments",
                    issue="popper_missing_adversarial_check",
                    severity=IssueSeverity.WARNING,
                    detail=(
                        "POPPER sidecar has no adversarial_check in any [outcomes.*] branch — "
                        "add adversarial_check to at least one outcome for stronger validity"
                    ),
                )
            )

    return issues


def check_unfired_branches(catalog_dir: Path, min_runs: int = 5) -> list[LintIssue]:
    """Check for branches (command+sidecar_sha256) that always produce same outcome.

    Query runs grouped by (command, sidecar_sha256) where count >= min_runs.
    Returns WARNING if all runs have same outcome label.

    Args:
        catalog_dir: Path to catalog directory.
        min_runs: Minimum number of runs per branch (default 5).

    Returns:
        List of LintIssue objects with severity WARNING.
    """
    import duckdb

    db_path = catalog_dir / "bathos.db"
    if not db_path.exists():
        return []

    try:
        db = duckdb.connect(str(db_path), read_only=True)
        db.execute("SET TimeZone='UTC'")

        try:
            rows = db.execute(
                """
                SELECT
                    command,
                    sidecar_sha256,
                    COUNT(*) as total,
                    COUNT(DISTINCT outcome) as outcome_count
                FROM runs
                WHERE outcome IS NOT NULL AND outcome != ''
                GROUP BY command, sidecar_sha256
                HAVING total >= ? AND outcome_count = 1
            """,
                [min_runs],
            ).fetchall()
        except Exception:
            db.close()
            return []

        issues: list[LintIssue] = []
        for command, sidecar_sha256, total, outcome_count in rows:
            issues.append(
                LintIssue(
                    path=catalog_dir / "bathos.db",
                    directory="catalog",
                    issue="single_outcome_branch_fired",
                    severity=IssueSeverity.WARNING,
                    detail=f"Branch fired consistently ({total} runs, "
                    f"1 outcome) — consider hypothesis validated: "
                    f"Script {command[:40]} (sidecar: {sidecar_sha256[:8]})",
                )
            )

        db.close()
        return issues
    except Exception:
        return []


def check_adversarial_checks(project_root: Path) -> list[LintIssue]:
    """Tier-2: Warn when adversarial_check is absent from outcomes.pass blocks.

    Scans all .bth.toml files in the project and checks for missing adversarial_check
    fields in outcomes.pass blocks.

    Args:
        project_root: Root directory of the project.

    Returns:
        List of LintIssue objects with severity WARNING.
    """
    issues: list[LintIssue] = []

    # Find all .bth.toml files in the project
    for sidecar_path in project_root.rglob("*.bth.toml"):
        try:
            with open(sidecar_path, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            # Skip files that can't be parsed
            continue

        outcomes = data.get("outcomes", {})
        for label, outcome in outcomes.items():
            if label == "pass" and "adversarial_check" not in outcome:
                issues.append(LintIssue(
                    path=sidecar_path,
                    directory="sidecar",
                    issue="missing_adversarial_check",
                    severity=IssueSeverity.WARNING,
                    detail=(
                        f"outcomes.{label} missing adversarial_check — "
                        "add a condition designed to falsify the hypothesis "
                        "(syntactic proxy only; verify it actually strengthens the claim)"
                    ),
                ))

    return issues


def check_threshold_basis(project_root: Path) -> list[LintIssue]:
    """Tier-2: Warn when numeric thresholds lack justification.

    Scans all .bth.toml files in the project. For each numeric literal found in:
    - outcome.condition (experiment, validation, debug sidecars)
    - benchmark.regression_threshold (if set and non-zero)

    Returns WARNING if numeric found AND corresponding justification field is empty.

    Consistent with check_adversarial_checks — no worktree/venv exclusion.

    Args:
        project_root: Root directory of the project.

    Returns:
        List of LintIssue objects with severity WARNING.
    """
    issues: list[LintIssue] = []

    # Find all .bth.toml files in the project
    for sidecar_path in project_root.rglob("*.bth.toml"):
        try:
            with open(sidecar_path, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            # Skip files that can't be parsed (consistent with check_adversarial_checks)
            continue

        # Check outcome conditions for numeric literals
        outcomes = data.get("outcomes", {})
        for label, outcome in outcomes.items():
            condition = outcome.get("condition", "")
            source = outcome.get("source", "")

            # Check if condition contains numeric literal
            if condition and _NUMERIC_LITERAL_RE.search(condition) and not source:
                issues.append(LintIssue(
                    path=sidecar_path,
                    directory="sidecar",
                    issue="unjustified_threshold",
                    severity=IssueSeverity.WARNING,
                    detail=(
                        f"outcomes.{label}.condition contains numeric literal without justification — "
                        "add source = 'explanation' field to document the threshold basis"
                    ),
                ))

        # Check benchmark regression_threshold
        benchmark = data.get("benchmark", {})
        if benchmark:
            regression_threshold = benchmark.get("regression_threshold", 0.0)
            regression_threshold_basis = benchmark.get("regression_threshold_basis", "")

            # Warn if threshold is set (non-zero) but has no basis
            if regression_threshold > 0 and not regression_threshold_basis:
                issues.append(LintIssue(
                    path=sidecar_path,
                    directory="sidecar",
                    issue="unjustified_threshold",
                    severity=IssueSeverity.WARNING,
                    detail=(
                        f"[benchmark] regression_threshold = {regression_threshold} "
                        "without justification — add regression_threshold_basis field to document the threshold basis"
                    ),
                ))

    return issues


def check_claim_opaque_labels(project_root: Path) -> list[LintIssue]:
    """Tier-1: Warn on opaque claim IDs with missing or placeholder labels.

    Scans `.bth/claims/**/*.toml` for hypothesis/confound entries matching
    the same opaque-id rule used by validate_claim (AC-03/AC-14).
    """
    from bathos.claim import _OPAQUE_ID_RE, is_placeholder_label

    issues: list[LintIssue] = []
    claims_dir = project_root / ".bth" / "claims"
    if not claims_dir.exists():
        return issues

    for claim_path in sorted(claims_dir.rglob("*.toml")):
        try:
            with open(claim_path, "rb") as f:
                data = tomllib.load(f)
        except Exception as exc:
            issues.append(
                LintIssue(
                    path=claim_path,
                    directory="claims",
                    issue="claim_parse_error",
                    severity=IssueSeverity.WARNING,
                    detail=f"failed to parse claim TOML: {exc}",
                )
            )
            continue

        for section, entity_kind in (("hypotheses", "hypothesis"), ("confounds", "confound")):
            for entity in data.get(section, []):
                entity_id = str(entity.get("id", "")).strip()
                if not entity_id or not _OPAQUE_ID_RE.match(entity_id):
                    continue
                label = str(entity.get("label", "")).strip()
                if not label:
                    issues.append(
                        LintIssue(
                            path=claim_path,
                            directory="claims",
                            issue=f"opaque_{entity_kind}_label",
                            severity=IssueSeverity.WARNING,
                            detail=(
                                f"opaque {entity_kind} id '{entity_id}' missing descriptive label "
                                "(bth claim validate will error at register/conclude)"
                            ),
                        )
                    )
                elif is_placeholder_label(entity_id, label):
                    issues.append(
                        LintIssue(
                            path=claim_path,
                            directory="claims",
                            issue=f"placeholder_{entity_kind}_label",
                            severity=IssueSeverity.WARNING,
                            detail=(
                                f"opaque {entity_kind} id '{entity_id}' still has placeholder label "
                                f"'{label}'"
                            ),
                        )
                    )

    return issues


def check_todo_strings_in_scaffold(project_root: Path) -> list[LintIssue]:
    """Tier-2: Warn when sidecar hypothesis or outcome decisions contain TODO placeholders.

    Scans all .bth.toml files in the project for remnants of the scaffold template.
    Checks:
    - [experiment].hypothesis for "TODO" substring
    - Each [outcomes.*].decision for "TODO" substring

    Returns WARNING if TODO found in either location.

    Args:
        project_root: Root directory of the project.

    Returns:
        List of LintIssue objects with severity WARNING.
    """
    issues: list[LintIssue] = []

    # Find all .bth.toml files in the project
    for sidecar_path in project_root.rglob("*.bth.toml"):
        try:
            with open(sidecar_path, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            # Skip files that can't be parsed
            continue

        # Check hypothesis field
        experiment = data.get("experiment", {})
        hypothesis = experiment.get("hypothesis", "")
        if hypothesis and "TODO" in hypothesis:
            issues.append(LintIssue(
                path=sidecar_path,
                directory="sidecar",
                issue="todo_in_scaffold",
                severity=IssueSeverity.WARNING,
                detail=(
                    "hypothesis contains TODO placeholder — replace with actual hypothesis"
                ),
            ))

        # Check decision fields in outcomes
        outcomes = data.get("outcomes", {})
        for label, outcome in outcomes.items():
            if not isinstance(outcome, dict):
                continue
            decision = outcome.get("decision", "")
            if decision and "TODO" in decision:
                issues.append(LintIssue(
                    path=sidecar_path,
                    directory="sidecar",
                    issue="todo_in_scaffold",
                    severity=IssueSeverity.WARNING,
                    detail=(
                        f"outcome '{label}' decision contains TODO placeholder — "
                        "replace with actual next step"
                    ),
                ))

    return issues


def check_ephemeral_output_paths(catalog_dir: Path) -> list[LintIssue]:
    """Check for runs that registered ephemeral output paths.

    Scans the warm catalog for runs where output_paths contains paths under
    /tmp, /var/tmp, or the system temp directory.

    Args:
        catalog_dir: Path to catalog directory.

    Returns:
        List of LintIssue objects with severity WARNING.
    """
    import duckdb
    import tempfile

    db_path = catalog_dir / "bathos.db"
    if not db_path.exists():
        return []

    temp_root = str(Path(tempfile.gettempdir()).resolve())
    temp_patterns = list({"/tmp/%", "/var/tmp/%", temp_root + "/%"})

    try:
        db = duckdb.connect(str(db_path), read_only=True)
        db.execute("SET TimeZone='UTC'")

        like_clauses = " OR ".join(f"p LIKE '{pat}'" for pat in temp_patterns)
        query = f"""
            SELECT id, command
            FROM runs
            WHERE output_paths IS NOT NULL
              AND len(output_paths) > 0
              AND EXISTS (
                  SELECT 1 FROM UNNEST(output_paths) AS t(p)
                  WHERE {like_clauses}
              )
            LIMIT 50
        """
        try:
            rows = db.execute(query).fetchall()
        except Exception:
            db.close()
            return []

        issues: list[LintIssue] = []
        for run_id, command in rows:
            issues.append(
                LintIssue(
                    path=catalog_dir / "bathos.db",
                    directory="catalog",
                    issue="ephemeral_output_path",
                    severity=IssueSeverity.WARNING,
                    detail=(
                        f"Run {run_id[:8]} ({command[:40]!r}) registered a temp-dir output — "
                        "outputs in /tmp are lost on reboot; use a persistent project path"
                    ),
                )
            )

        db.close()
        return issues
    except Exception:
        return []


def check_canonical_stage_names(catalog_dir: Path) -> list[LintIssue]:
    """ADVISORY: warn on non-canonical stage_name values in runs.

    Scans the warm catalog for runs where stage_name does not match the
    canonical set (exploration, calibration, validation, ablation, production).
    This is an ADVISORY-only lint that ALWAYS exits 0 — never blocks runs or builds.

    Canonical set is maintained as lint config (not schema), allowing the vocabulary
    to evolve independently of the schema and supporting future tightening to an enum
    once real stage data exists (see ops-systematization spec, decision (c)-FREEFORM-NOW).

    Args:
        catalog_dir: Path to catalog directory.

    Returns:
        List of LintIssue objects with severity WARNING for non-canonical stage_name values.
        Returns empty list if no runs or no non-canonical stages found.
        Always returns (never raises), so lint can continue even if DB unreachable.
    """
    import duckdb
    from bathos.schema import STAGE_NAME_REGEX

    db_path = catalog_dir / "bathos.db"
    if not db_path.exists():
        return []

    # Canonical set: the advisory vocabulary for stage_name values.
    # These are best-practice values discovered from real stage_name usage.
    # Not enforced in the schema — only advisory in CI lint.
    # Promotion to enforced enum deferred until real data reveals true vocabulary.
    CANONICAL_STAGES = {
        "exploration",
        "calibration",
        "validation",
        "ablation",
        "production",
    }

    try:
        db = duckdb.connect(str(db_path), read_only=True)
        db.execute("SET TimeZone='UTC'")

        try:
            rows = db.execute(
                """
                SELECT id, stage_name, command
                FROM runs
                WHERE stage_name IS NOT NULL AND stage_name != ''
                LIMIT 1000
            """
            ).fetchall()
        except Exception:
            db.close()
            return []

        issues: list[LintIssue] = []
        for run_id, stage_name, command in rows:
            # First: validate format against STAGE_NAME_REGEX (single source of truth)
            if not STAGE_NAME_REGEX.match(stage_name):
                issues.append(
                    LintIssue(
                        path=catalog_dir / "bathos.db",
                        directory="catalog",
                        issue="invalid_stage_name_format",
                        severity=IssueSeverity.WARNING,
                        detail=(
                            f"Run {run_id[:8]} has stage_name='{stage_name}' — "
                            f"does not match required format: lowercase letters, digits, hyphens only. "
                            f"Must start with a letter (e.g., 'exploration', 'my-stage'). "
                            f"This is advisory only and does not block runs."
                        ),
                    )
                )
            # Second: check if stage_name is NOT in canonical set (advisory)
            elif stage_name not in CANONICAL_STAGES:
                issues.append(
                    LintIssue(
                        path=catalog_dir / "bathos.db",
                        directory="catalog",
                        issue="non_canonical_stage_name",
                        severity=IssueSeverity.WARNING,
                        detail=(
                            f"Run {run_id[:8]} has stage_name='{stage_name}' — "
                            f"not in canonical set {CANONICAL_STAGES}. "
                            f"This is advisory only and does not block runs. "
                            f"Consider using one of the canonical stages, or this value will be "
                            f"proposed for canonicalization after real data accumulates."
                        ),
                    )
                )

        db.close()
        return issues
    except Exception:
        # Always return (never raise), so lint continues
        return []


def check_baseline_ref_exists(project_root: Path, catalog_dir: Path, db_path: Path) -> list[LintIssue]:
    """Tier-2: Validate that baseline_ref values exist in the warm catalog.

    Scans all benchmark sidecars in scripts/benchmarks/ for baseline_ref fields.
    For each non-empty baseline_ref, queries the warm-tier DuckDB catalog using:
      SELECT outcome, started_at FROM runs WHERE id = ? OR id LIKE ? LIMIT 1

    Supports both full UUIDs and short prefixes (e.g., "abc12345%").

    If baseline_ref not found: WARNING
    If baseline_ref found: returns issue with baseline outcome info for audit purposes

    Args:
        project_root: Path to project root.
        catalog_dir: Path to catalog directory (for scanning).
        db_path: Path to bathos.db warm catalog.

    Returns:
        List of LintIssue objects with severity WARNING (not found) or informational details.
    """
    import duckdb

    if not db_path.exists():
        return []

    scripts_dir = project_root / "scripts" / "benchmarks"
    if not scripts_dir.exists():
        return []

    issues: list[LintIssue] = []

    for sidecar_path in sorted(scripts_dir.rglob("*.bth.toml")):
        try:
            data = tomllib.loads(sidecar_path.read_text())
        except Exception:
            continue

        # Only process sidecars with [benchmark] block
        if "benchmark" not in data:
            continue

        benchmark_section = data.get("benchmark", {})
        baseline_ref = benchmark_section.get("baseline_ref", "") or ""

        if not baseline_ref:
            continue

        # Query warm DuckDB for the baseline run
        try:
            db = duckdb.connect(str(db_path), read_only=True)
            db.execute("SET TimeZone='UTC'")

            row = db.execute(
                "SELECT outcome, timestamp FROM runs WHERE id = ? OR id LIKE ? LIMIT 1",
                [baseline_ref, baseline_ref + "%"]
            ).fetchone()

            db.close()
        except Exception:
            # Database error, skip
            continue

        if row is None:
            issues.append(
                LintIssue(
                    path=sidecar_path,
                    directory="benchmarks",
                    issue="baseline_ref_not_found",
                    severity=IssueSeverity.WARNING,
                    detail=f"baseline_ref {baseline_ref!r} not found in warm-tier catalog",
                )
            )
        else:
            outcome, started_at = row
            # Emit informational issue showing the baseline was found
            issues.append(
                LintIssue(
                    path=sidecar_path,
                    directory="benchmarks",
                    issue="baseline_ref_ok",
                    severity=IssueSeverity.INFO,
                    detail=f"baseline_ref {baseline_ref!r}: outcome={outcome}, started_at={started_at}",
                )
            )

    return issues


def check_single_cell_gate(
    claim_discriminability: list[dict],
    campaign_id: str,
    db: "duckdb.DuckDBPyConnection",
) -> list[LintIssue]:
    """AC-06: warn if all confirmatory runs in a campaign share identical metadata values.

    Args:
        claim_discriminability: List of discriminability entries from claim
        campaign_id: Campaign ID to check
        db: DuckDB connection

    Returns:
        List of LintIssue objects with severity WARNING if single-cell pattern detected
    """
    import json
    import duckdb

    issues = []
    try:
        rows = db.execute(
            "SELECT r.metadata FROM runs r JOIN campaign_runs cr ON r.id = cr.run_id WHERE cr.campaign_id = ? AND r.metadata IS NOT NULL",
            [campaign_id],
        ).fetchall()
    except Exception:
        return issues  # DB not available or table missing — skip silently

    if len(rows) < 2:
        return issues  # need >= 2 runs to detect single-cell

    # Parse all metadata blobs
    metas = []
    for (meta_str,) in rows:
        try:
            metas.append(json.loads(meta_str))
        except (json.JSONDecodeError, TypeError):
            pass

    if len(metas) < 2:
        return issues

    # Find keys present in ALL runs
    common_keys = set(metas[0].keys())
    for m in metas[1:]:
        common_keys &= set(m.keys())

    if not common_keys:
        return issues

    # Check if all runs have the same value for every common key
    uniform_keys = [
        k for k in common_keys
        if len({str(m.get(k)) for m in metas}) == 1
    ]

    if uniform_keys and len(uniform_keys) == len(common_keys):
        issues.append(
            LintIssue(
                path=Path(campaign_id),
                directory="campaign",
                issue="single-cell-gate smell",
                severity=IssueSeverity.WARNING,
                detail=f"all {len(metas)} confirmatory runs use identical values for: {sorted(uniform_keys)[:5]}; claim.regime may not be covered",
            )
        )
    return issues
