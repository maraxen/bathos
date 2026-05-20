from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from bathos.sidecar import find_sidecar


class IssueSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


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
                    COALESCE(cr.campaign_id, '') as campaign_id,
                    COUNT(*) as total,
                    SUM(CASE WHEN r.outcome_is_residual THEN 1 ELSE 0 END) as residual_count
                FROM runs r
                LEFT JOIN campaign_runs cr ON r.id = cr.run_id
                GROUP BY campaign_id
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
                    f"{command[:40][:8]}...",
                )
            )

        db.close()
        return issues
    except Exception:
        return []
