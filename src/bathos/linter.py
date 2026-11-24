from __future__ import annotations

import fnmatch
import math
import re
import tomllib
from collections.abc import Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import duckdb

from bathos.index import catalog_readable, connect_read
from bathos.sidecar import columns_referenced, find_sidecar
from bathos.walk import iter_project_files


class IssueSeverity(str, Enum):  # noqa: UP042 - inheriting str preserves str(Enum) returning Enum member value for serialization
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
_PVALUE_TERM_RE = re.compile(r"\bp[\w]*\s*(?:<=|<)\s*0?\.\d+", re.IGNORECASE)
_OR_SPLIT_RE = re.compile(r"\bOR\b", re.IGNORECASE)

# Run-lock files written by `_write_manifest` (bathos/runner.py): the manifest
# filename is built from the *sidecar's* stem (which already ends in ".bth"),
# so the file on disk is "<script_stem>.bth.<run_id>.bth.lock.toml" -- distinct
# from, and not matched by, the plain "*.bth.toml" sidecar suffix.
_SIDECAR_SUFFIX = ".bth.toml"
_LOCK_FILE_SUFFIX = ".bth.lock.toml"

def iter_project_sidecars(root: Path) -> list[Path]:
    """Find every "*.bth.toml" sidecar at or below `root`, in sorted order.

    Thin wrapper over `bathos.walk.iter_project_files`, which owns the
    boundary-pruning logic and documents why a bare rglob is wrong here. The
    `lint.boundary_pruned` event name is preserved rather than taking the shared
    default, so lint's scope reductions stay distinguishable in telemetry from
    compaction's and postmortem lookup's.
    """
    return iter_project_files(root, _SIDECAR_SUFFIX, event_name="lint.boundary_pruned")


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


_NAMING_TOP_KEYS = frozenset({"exempt", "exempt_dirs"})
_NAMING_DIR_KEYS = frozenset(
    {"exempt", "extensions", "extra_extensions", "pattern", "enabled", "sidecar"}
)
_SIDECAR_LEVELS: dict[str, IssueSeverity | None] = {
    "error": IssueSeverity.ERROR,
    "warning": IssueSeverity.WARNING,
    "none": None,
}


def _copy_default_dir_rules() -> dict[str, dict[str, Any]]:
    return {
        dir_name: {
            "pattern": rules["pattern"],
            "extensions": set(rules["extensions"]),
            "sidecar": rules["sidecar"],
            "enabled": True,
            "custom_pattern": False,
        }
        for dir_name, rules in _DIR_RULES.items()
    }


def _parse_str_list(val: object, field_name: str) -> tuple[str, ...] | str:
    """Return tuple of non-empty strings, or an error detail string."""
    if not isinstance(val, list) or not all(isinstance(x, str) and x.strip() for x in val):
        return f"{field_name} must be a list of non-empty strings"
    return tuple(val)


def _resolve_naming_config(
    project_root: Path,
) -> tuple[
    dict[str, dict[str, Any]],
    tuple[str, ...],
    dict[str, tuple[str, ...]],
    set[str],
    list[LintIssue],
]:
    """Load `.bth.toml` `[lint.naming]` overrides and exemptions on top of `_DIR_RULES`."""
    effective = _copy_default_dir_rules()
    global_exempt: tuple[str, ...] = ()
    dir_exempt: dict[str, tuple[str, ...]] = {}
    exempt_dirs: set[str] = set()
    config_issues: list[LintIssue] = []

    bth_toml = project_root / ".bth.toml"
    if not bth_toml.is_file():
        return effective, global_exempt, dir_exempt, exempt_dirs, config_issues

    def _err(detail: str) -> None:
        config_issues.append(
            LintIssue(
                path=bth_toml,
                directory="config",
                issue="invalid_lint_config",
                severity=IssueSeverity.ERROR,
                detail=detail,
            )
        )

    try:
        raw = bth_toml.read_bytes()
    except OSError as exc:
        _err(f"cannot read {bth_toml}: {exc}")
        return effective, global_exempt, dir_exempt, exempt_dirs, config_issues

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        if b"naming" in raw.lower() or b"[lint" in raw.lower():
            _err(f"cannot parse {bth_toml}: {exc}")
        return effective, global_exempt, dir_exempt, exempt_dirs, config_issues

    lint_tbl = data.get("lint")
    if lint_tbl is None:
        return effective, global_exempt, dir_exempt, exempt_dirs, config_issues
    if not isinstance(lint_tbl, dict):
        _err("[lint] must be a table")
        return effective, global_exempt, dir_exempt, exempt_dirs, config_issues

    naming_tbl = lint_tbl.get("naming")
    if naming_tbl is None:
        return effective, global_exempt, dir_exempt, exempt_dirs, config_issues
    if not isinstance(naming_tbl, dict):
        _err("[lint.naming] must be a table")
        return effective, global_exempt, dir_exempt, exempt_dirs, config_issues

    allowed_top = _NAMING_TOP_KEYS | set(_DIR_RULES)
    unknown_top = sorted(set(naming_tbl) - allowed_top)
    if unknown_top:
        _err(f"[lint.naming] has unknown key(s): {unknown_top}")

    if "exempt" in naming_tbl:
        parsed = _parse_str_list(naming_tbl["exempt"], "[lint.naming].exempt")
        if isinstance(parsed, str):
            _err(parsed)
        else:
            global_exempt = parsed

    if "exempt_dirs" in naming_tbl:
        parsed = _parse_str_list(naming_tbl["exempt_dirs"], "[lint.naming].exempt_dirs")
        if isinstance(parsed, str):
            _err(parsed)
        else:
            for raw_dir in parsed:
                norm_dir = raw_dir.strip().replace("\\", "/").removeprefix("scripts/").strip("/")
                if norm_dir not in _DIR_RULES:
                    _err(
                        f"[lint.naming].exempt_dirs contains unknown directory {raw_dir!r}; "
                        f"expected one of {sorted(_DIR_RULES)}"
                    )
                else:
                    exempt_dirs.add(norm_dir)

    for dir_name in _DIR_RULES:
        if dir_name not in naming_tbl:
            continue
        dir_cfg = naming_tbl[dir_name]
        section = f"[lint.naming.{dir_name}]"
        if not isinstance(dir_cfg, dict):
            _err(f"{section} must be a table")
            continue
        unknown_dir = sorted(set(dir_cfg) - _NAMING_DIR_KEYS)
        if unknown_dir:
            _err(f"{section} has unknown key(s): {unknown_dir}")

        if "exempt" in dir_cfg:
            parsed = _parse_str_list(dir_cfg["exempt"], f"{section}.exempt")
            if isinstance(parsed, str):
                _err(parsed)
            else:
                dir_exempt[dir_name] = parsed

        if "enabled" in dir_cfg:
            if not isinstance(dir_cfg["enabled"], bool):
                _err(f"{section}.enabled must be a boolean")
            else:
                effective[dir_name]["enabled"] = dir_cfg["enabled"]

        if "extensions" in dir_cfg:
            parsed = _parse_str_list(dir_cfg["extensions"], f"{section}.extensions")
            if isinstance(parsed, str):
                _err(parsed)
            elif any(not ext.startswith(".") or len(ext) < 2 for ext in parsed):
                _err(f"{section}.extensions entries must start with '.' (e.g. '.py')")
            else:
                effective[dir_name]["extensions"] = set(parsed)

        if "extra_extensions" in dir_cfg:
            parsed = _parse_str_list(dir_cfg["extra_extensions"], f"{section}.extra_extensions")
            if isinstance(parsed, str):
                _err(parsed)
            elif any(not ext.startswith(".") or len(ext) < 2 for ext in parsed):
                _err(f"{section}.extra_extensions entries must start with '.' (e.g. '.sh')")
            else:
                effective[dir_name]["extensions"] = effective[dir_name]["extensions"] | set(parsed)

        if "pattern" in dir_cfg:
            pat_val = dir_cfg["pattern"]
            if not isinstance(pat_val, str) or not pat_val:
                _err(f"{section}.pattern must be a non-empty regex string")
            else:
                try:
                    effective[dir_name]["pattern"] = re.compile(pat_val)
                    effective[dir_name]["custom_pattern"] = True
                except re.error as exc:
                    _err(f"{section}.pattern is not a valid regex: {exc}")

        if "sidecar" in dir_cfg:
            sc_val = dir_cfg["sidecar"]
            if not isinstance(sc_val, str) or sc_val.lower() not in _SIDECAR_LEVELS:
                _err(f"{section}.sidecar must be one of {sorted(_SIDECAR_LEVELS)}")
            else:
                effective[dir_name]["sidecar"] = _SIDECAR_LEVELS[sc_val.lower()]

    return effective, global_exempt, dir_exempt, exempt_dirs, config_issues


def _is_naming_exempt(
    script: Path,
    project_root: Path,
    scripts_dir: Path,
    global_exempt: tuple[str, ...],
    dir_exempt: tuple[str, ...],
) -> bool:
    rel_root = script.relative_to(project_root).as_posix()
    rel_scripts = script.relative_to(scripts_dir).as_posix()
    name = script.name
    for pat in (*global_exempt, *dir_exempt):
        norm = pat.strip().replace("\\", "/").lstrip("./")
        if not norm:
            continue
        if "/" in norm:
            if fnmatch.fnmatchcase(rel_root, norm) or fnmatch.fnmatchcase(rel_scripts, norm):
                return True
        elif fnmatch.fnmatchcase(name, norm):
            return True
    return False


def lint_project(project_root: Path) -> list[LintIssue]:
    scripts_dir = project_root / "scripts"
    effective_rules, global_exempt, dir_exempt, exempt_dirs, config_issues = (
        _resolve_naming_config(project_root)
    )
    issues: list[LintIssue] = list(config_issues)

    if scripts_dir.exists():
        for dir_name, rules in effective_rules.items():
            dir_path = scripts_dir / dir_name
            if not dir_path.exists():
                continue

            dir_naming_disabled = dir_name in exempt_dirs or not rules.get("enabled", True)
            per_dir_pats = dir_exempt.get(dir_name, ())

            for script in sorted(dir_path.iterdir()):
                if script.name.startswith(".") or script.name.startswith("_") or script.is_dir():
                    continue

                # Skip sidecar and run-lock files (the latter written by `_write_manifest`
                # in runner.py as "<script_stem>.bth.<run_id>.bth.lock.toml" -- it ends in
                # ".bth.lock.toml", not ".bth.toml", so needs its own check here).
                if script.suffix == ".toml" and (
                    script.name.endswith(_SIDECAR_SUFFIX) or script.name.endswith(_LOCK_FILE_SUFFIX)
                ):
                    continue

                stem = script.stem
                ext = script.suffix
                exempt = dir_naming_disabled or _is_naming_exempt(
                    script, project_root, scripts_dir, global_exempt, per_dir_pats
                )

                if not exempt:
                    if ext not in rules["extensions"]:
                        issues.append(
                            LintIssue(
                                path=script,
                                directory=dir_name,
                                issue="naming",
                                severity=IssueSeverity.ERROR,
                                detail=f"expected extension {rules['extensions']}, got {ext!r}",
                            )
                        )
                        continue

                    if not rules["pattern"].match(stem):
                        if rules.get("custom_pattern"):
                            expected = f"pattern {rules['pattern'].pattern!r}"
                        elif dir_name not in ("debug", "explore", "scratch"):
                            expected = "verb_noun"
                        else:
                            expected = "YYMMDD_desc"
                        issues.append(
                            LintIssue(
                                path=script,
                                directory=dir_name,
                                issue="naming",
                                severity=IssueSeverity.ERROR,
                                detail=f"expected {expected} style, got {stem!r}",
                            )
                        )

                if (
                    rules["sidecar"] is not None
                    and ext in rules["extensions"]
                    and find_sidecar(script) is None
                ):
                    issues.append(
                        LintIssue(
                            path=script,
                            directory=dir_name,
                            issue="missing_sidecar",
                            severity=rules["sidecar"],
                            detail=f"create {stem}.bth.toml next to this script",
                        )
                    )

        # Tier-1 checks for validation/production experiments
        issues.extend(check_novel_or_reproduces_declared(project_root))

        # Tier-1 check: null-capable outcomes need a positive control (debt #1071)
        issues.extend(check_positive_control_missing(project_root))

        # Tier-1 check: uncorrected multiple-comparisons in outcome conditions (debt #1071)
        issues.extend(check_multiple_comparisons(project_root))

        # Worst-case aggregates gating a verdict with no [breakdown] (spec 261026 D4)
        issues.extend(check_aggregate_without_breakdown(project_root))

        # Epistemic-class contract: stage_name canonicality, restage hints, source grammar (spec §3.1-3.2)
        issues.extend(check_epistemic_sidecars(project_root))

        # Observation promotion, [holdout], laundering tripwire, & calibration run sources (spec §3.4-3.5, S4/S5)
        issues.extend(check_holdout_and_observation_lint(project_root))
        issues.extend(check_calibration_sources(project_root))

        # Unmeasured prose data-quality/filtering claims (#2662) and factor coverage declarations (#2200)
        issues.extend(check_unmeasured_data_quality_claims(project_root))
        issues.extend(check_factor_coverage_lint(project_root))

        # Unit of independence (#3094) and source/batch confound (#3095) checks
        issues.extend(check_unit_of_independence(project_root))
        issues.extend(check_source_batch_confound(project_root))

        # Step-back review records lint
        issues.extend(check_stepback_reviews(project_root))

    # Landed method-correction sweep (#3099)
    issues.extend(check_correction_sweep(project_root))

    # AC-06 (#2199): wire check_single_cell_gate into lint_project when a project catalog exists
    issues.extend(_lint_single_cell_campaigns(project_root))

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
    from bathos.sidecar import SidecarKind, parse_sidecar

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

            if sidecar.gate_stage not in ("validation", "production"):
                continue

            # Check: must have [reproduction] or novel=true
            has_reproduction = sidecar.reproduction is not None and (
                sidecar.reproduction.reproduces_paper or sidecar.reproduction.reproduces_run
            )

            if not has_reproduction and not sidecar.novel:
                issues.append(
                    LintIssue(
                        path=script,
                        directory=dir_name,
                        issue="NOVEL_OR_REPRODUCES_REQUIRED",
                        severity=IssueSeverity.ERROR,
                        detail="validation/production experiment must declare [reproduction] or novel=true",
                    )
                )

    return issues


def check_positive_control_missing(project_root: Path) -> list[LintIssue]:
    """Flag experiment sidecars with a null-capable outcome but no positive control (debt #1071).

    Filesystem-only check: reads sidecars directly from disk. WARNING-severity (not ERROR --
    advisory, so it doesn't retroactively break every pre-existing sidecar in a lint-clean
    project the day this check ships).

    A "null-capable" outcome is one that could legitimately fire when the measured effect is
    genuinely absent -- a `fail` label, or any branch declared `is_residual=true` (the
    catch-all fallback). Without a positive control ([differential] or
    [controls].positive_outcome), such a sidecar has no way to tell "the effect is genuinely
    absent" apart from "the measurement pipeline is broken" -- the exact incident this debt
    was filed after (13 experiments where a broken/insensitive pipeline silently read as a
    legitimate null result).
    """
    from bathos.sidecar import SidecarKind, parse_sidecar

    scripts_dir = project_root / "scripts"
    if not scripts_dir.exists():
        return []

    issues: list[LintIssue] = []

    for dir_name in ["experiments", "validation"]:
        dir_path = scripts_dir / dir_name
        if not dir_path.exists():
            continue

        for script in sorted(dir_path.iterdir()):
            if script.name.startswith(".") or script.name.startswith("_") or script.is_dir():
                continue

            if not (script.suffix == ".toml" and script.name.endswith(".bth.toml")):
                continue

            try:
                sidecar = parse_sidecar(script)
            except Exception:
                # Silently skip unparseable sidecars — other lint checks will catch them
                continue

            if sidecar.kind != SidecarKind.EXPERIMENT:
                continue

            is_null_capable = any(
                label == "fail" or spec.is_residual for label, spec in sidecar.outcomes.items()
            )
            if not is_null_capable:
                continue

            has_differential = sidecar.differential is not None
            has_positive_control = bool(
                sidecar.controls is not None and sidecar.controls.positive_outcome
            )
            if has_differential or has_positive_control:
                continue

            issues.append(
                LintIssue(
                    path=script,
                    directory=dir_name,
                    issue="POSITIVE_CONTROL_MISSING",
                    severity=IssueSeverity.WARNING,
                    detail=(
                        "sidecar has a null-capable outcome (fail/is_residual) but no "
                        "[differential] block and no [controls].positive_outcome — cannot "
                        "distinguish 'effect genuinely absent' from 'measurement broken'"
                    ),
                )
            )

    return issues


def check_aggregate_without_breakdown(project_root: Path) -> list[LintIssue]:
    """Flag sidecars whose verdict rests on a worst-case aggregate with no `[breakdown]` (STAT-005).

    Filesystem-only (Tier-1, WARNING). A maximum over units says nothing about how many units
    produced it; `[breakdown]` makes the run emit the worst unit, the runner-up, the median and
    the count over the bound. Covered by a `[breakdown.aggregates]` entry or a run-time `waive`.
    Detection is `bathos.sidecar.uncovered_aggregates` (the same one `bth run` prints from).
    """
    from bathos.sidecar import parse_sidecar, uncovered_aggregates

    scripts_dir = project_root / "scripts"
    if not scripts_dir.exists():
        return []

    issues: list[LintIssue] = []
    for dir_name in ["experiments", "benchmarks", "validation"]:
        dir_path = scripts_dir / dir_name
        if not dir_path.exists():
            continue
        for script in sorted(dir_path.iterdir()):
            if script.name.startswith(".") or script.name.startswith("_") or script.is_dir():
                continue
            if not script.name.endswith(".bth.toml"):
                continue
            try:
                sidecar = parse_sidecar(script)
            except Exception:
                # Unparseable sidecars are reported by the other checks
                continue
            for u in uncovered_aggregates(sidecar):
                issues.append(
                    LintIssue(
                        path=script,
                        directory=dir_name,
                        issue="AGGREGATE_WITHOUT_BREAKDOWN",
                        severity=IssueSeverity.WARNING,
                        detail=(
                            f"{u.column!r} gates a verdict ({', '.join(u.sources)}) with no "
                            f"[breakdown]; declare [breakdown.aggregates.{u.column}] or "
                            '[breakdown] waive = "reason" (rule STAT-005)'
                        ),
                    )
                )
    return issues


def check_multiple_comparisons(project_root: Path, min_tests: int = 3) -> list[LintIssue]:
    """Flag sidecar outcome conditions OR-ing together >= min_tests apparent significance
    tests with no visible multiple-comparison correction (debt #1071 statistical corollary).

    Filesystem-only, static heuristic (Tier-1): a naive "any of N tests significant" union
    inflates the family-wise false-positive rate to roughly 1-(1-alpha)^N. This is exactly
    the shape of the incident that motivated this check -- a gate that ran 90 tests and
    initially fired on a single uncorrected p=0.0497. AND-joined comparisons are excluded:
    ANDing tests is conservative (every one must fire), not the anti-pattern; only OR-chains
    are flagged.

    A live runtime check over actually-recorded p-values (e.g. wiring
    bathos.stats_gates.holm_correction into a battery of real per-run results) is explicitly
    out of scope here -- nothing today persists per-run p-values anywhere queryable, so that
    would need new capture plumbing comparable in size to this debt's other pieces.

    Suppressed per-outcome by setting `multiple_comparisons_correction` to a non-empty value
    (e.g. "holm") once a real correction has actually been applied upstream to that outcome's
    underlying test battery.
    """
    from bathos.sidecar import parse_sidecar

    scripts_dir = project_root / "scripts"
    if not scripts_dir.exists():
        return []

    issues: list[LintIssue] = []

    for dir_name in ["experiments", "benchmarks", "validation"]:
        dir_path = scripts_dir / dir_name
        if not dir_path.exists():
            continue

        for script in sorted(dir_path.iterdir()):
            if script.name.startswith(".") or script.name.startswith("_") or script.is_dir():
                continue
            if not (script.suffix == ".toml" and script.name.endswith(".bth.toml")):
                continue

            try:
                sidecar = parse_sidecar(script)
            except Exception:
                # Silently skip unparseable sidecars — other lint checks will catch them
                continue

            for label, spec in sidecar.outcomes.items():
                if spec.multiple_comparisons_correction:
                    continue

                segments = _OR_SPLIT_RE.split(spec.condition or "")
                test_count = sum(1 for seg in segments if _PVALUE_TERM_RE.search(seg))
                if test_count < min_tests:
                    continue

                issues.append(
                    LintIssue(
                        path=script,
                        directory=dir_name,
                        issue="UNCORRECTED_MULTIPLE_COMPARISONS",
                        severity=IssueSeverity.WARNING,
                        detail=(
                            f"outcome '{label}' ORs together {test_count} apparent "
                            "significance tests with no visible multiple-comparison "
                            "correction — a naive 'any of N' union inflates the family-wise "
                            "false-positive rate to ~1-(1-alpha)^N; apply Holm-Bonferroni "
                            "(bathos.stats_gates.holm_correction) or set "
                            "multiple_comparisons_correction on this outcome"
                        ),
                    )
                )

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

    if not catalog_readable(catalog_dir):
        return []

    try:
        db = connect_read(catalog_dir, read_only=True)
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
    from datetime import UTC, datetime, timedelta


    if not catalog_readable(catalog_dir):
        return []

    try:
        db = connect_read(catalog_dir, read_only=True)
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
    for sidecar_path in iter_project_sidecars(scripts_dir):
        try:
            data = tomllib.loads(sidecar_path.read_text())
        except Exception:
            continue

        # Only process sidecars with [experiment] + [popper] blocks
        if "experiment" not in data or "popper" not in data:
            continue

        outcomes = data.get("outcomes", {})
        has_adversarial = any(
            branch.get("adversarial_check")
            for branch in outcomes.values()
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

    if not catalog_readable(catalog_dir):
        return []

    try:
        db = connect_read(catalog_dir, read_only=True)
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


def check_run_concentration(catalog_dir: Path, threshold: int = 20) -> list[LintIssue]:
    """Check for campaigns or scripts accumulating unvalidated runs past a soft cap (C3).

    Two aggregations over the runs table:
    - per campaign_id (COALESCE empty -> an '<uncampaigned>' bucket),
    - per script_sha256 (with a command label).
    Counts runs where outcome is unset/unknown/none. Emits one WARNING LintIssue per bucket
    whose unvalidated count is strictly greater than threshold — silent accumulation of
    unvalidated work is a forced-checkpoint signal, not a hard failure.

    Args:
        catalog_dir: Path to catalog directory.
        threshold: Unvalidated-run count threshold (default 20; strict `>`).

    Returns:
        List of LintIssue objects with severity WARNING.
    """

    if not catalog_readable(catalog_dir):
        return []

    unvalidated = "(outcome IS NULL OR trim(outcome) IN ('', 'unknown', 'none'))"

    try:
        db = connect_read(catalog_dir, read_only=True)
        db.execute("SET TimeZone='UTC'")

        issues: list[LintIssue] = []

        try:
            campaign_rows = db.execute(
                f"""
                SELECT
                    COALESCE(NULLIF(campaign_id, ''), '<uncampaigned>') AS bucket,
                    COUNT(*) FILTER (WHERE {unvalidated}) AS noout,
                    COUNT(*) AS total
                FROM runs
                GROUP BY 1
                HAVING COUNT(*) FILTER (WHERE {unvalidated}) > ?
                ORDER BY 2 DESC
                """,
                [threshold],
            ).fetchall()
        except Exception:
            db.close()
            return []

        for bucket, noout, total in campaign_rows:
            issues.append(
                LintIssue(
                    path=catalog_dir / "bathos.db",
                    directory="catalog",
                    issue="run-concentration",
                    severity=IssueSeverity.WARNING,
                    detail=f"{bucket}: {noout}/{total} unvalidated",
                )
            )

        try:
            script_rows = db.execute(
                f"""
                SELECT
                    COALESCE(NULLIF(script_sha256, ''), '<no-script-hash>') AS bucket,
                    any_value(command) AS cmd,
                    COUNT(*) FILTER (WHERE {unvalidated}) AS noout,
                    COUNT(*) AS total
                FROM runs
                GROUP BY 1
                HAVING COUNT(*) FILTER (WHERE {unvalidated}) > ?
                ORDER BY 3 DESC
                """,
                [threshold],
            ).fetchall()
        except Exception:
            db.close()
            return issues

        for bucket, cmd, noout, total in script_rows:
            label = bucket[:16] if bucket != "<no-script-hash>" else bucket
            cmd_label = (cmd or "")[:48]
            issues.append(
                LintIssue(
                    path=catalog_dir / "bathos.db",
                    directory="catalog",
                    issue="run-concentration",
                    severity=IssueSeverity.WARNING,
                    detail=f"{label} ({cmd_label}): {noout}/{total} unvalidated",
                )
            )

        db.close()
        return issues
    except Exception:
        return []


#: Tautological conjuncts that make an adversarial_check *look* stronger while adding nothing.
#: Spec Item 6 heuristic (a). `\btrue\b` is matched only as a bare conjunct so a legitimate
#: `flag = true` comparison is not swept up.
_TAUTOLOGY_RES = (
    re.compile(r"\b1\s*=\s*1\b"),
    re.compile(r"(?:^|\b(?:and|or)\b)\s*true\s*(?:$|\b(?:and|or)\b)", re.IGNORECASE),
    re.compile(r"\b(\w+)\s*=\s*\1\b", re.IGNORECASE),  # col = col
)


# Which declared result_schema columns a SQL fragment mentions: a word-boundary search against
# the declared schema, not a SQL parse. One implementation, shared with `uncovered_aggregates`.
_columns_referenced = columns_referenced


def check_adversarial_checks(project_root: Path) -> list[LintIssue]:
    """Tier-2: quality checks on `adversarial_check`, per spec Item 6 (a)/(b).

    Three findings, all WARNING:

    - `missing_adversarial_check` — absent from an `[outcomes.pass]` block.
    - `adversarial_check_tautology` — heuristic (a): contains a conjunct that is always true,
      so the check adds no strength.
    - `adversarial_check_same_column` — heuristic (b): every column it references also appears
      in `condition`. Tightening a threshold on the same variable is weak; a genuine adversarial
      check "would flip the outcome if the hypothesis were wrong", which typically needs a
      *different* observed variable.

    (b) is the one that catches the degenerate `condition = "metric < 5.0"` /
    `adversarial_check = "metric >= 5.0"` shape: as a conjunct that is a contradiction, so it
    would fire on every single pass.

    **This is a syntactic proxy and nothing more.** Full logical implication is undecidable in
    SQL, so a determined author can still satisfy every check here with a vacuous condition. A
    clean lint is not evidence the check strengthens the claim; only a human reading it is.

    Args:
        project_root: Root directory of the project.

    Returns:
        List of LintIssue objects with severity WARNING.
    """
    issues: list[LintIssue] = []

    # Find all .bth.toml files in the project
    for sidecar_path in iter_project_sidecars(project_root):
        try:
            with open(sidecar_path, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            # Skip files that can't be parsed
            continue

        known_columns = list((data.get("result_schema") or {}).keys())
        outcomes = data.get("outcomes", {})
        for label, outcome in outcomes.items():
            if not isinstance(outcome, dict):
                continue

            check = outcome.get("adversarial_check")
            if label == "pass" and not check:
                issues.append(
                    LintIssue(
                        path=sidecar_path,
                        directory="sidecar",
                        issue="missing_adversarial_check",
                        severity=IssueSeverity.WARNING,
                        detail=(
                            f"outcomes.{label} missing adversarial_check — "
                            "add a condition designed to falsify the hypothesis "
                            "(syntactic proxy only; verify it actually strengthens the claim)"
                        ),
                    )
                )
            if not check or not isinstance(check, str):
                continue

            # (a) tautological conjuncts
            if any(rx.search(check) for rx in _TAUTOLOGY_RES):
                issues.append(
                    LintIssue(
                        path=sidecar_path,
                        directory="sidecar",
                        issue="adversarial_check_tautology",
                        severity=IssueSeverity.WARNING,
                        detail=(
                            f"outcomes.{label}.adversarial_check contains an always-true "
                            f"conjunct ({check!r}) — it cannot strengthen the outcome"
                        ),
                    )
                )

            # (b) distinct-column preference
            condition = outcome.get("condition")
            if known_columns and isinstance(condition, str):
                check_cols = _columns_referenced(check, known_columns)
                cond_cols = _columns_referenced(condition, known_columns)
                if check_cols and not (check_cols - cond_cols):
                    issues.append(
                        LintIssue(
                            path=sidecar_path,
                            directory="sidecar",
                            issue="adversarial_check_same_column",
                            severity=IssueSeverity.WARNING,
                            detail=(
                                f"outcomes.{label}.adversarial_check references only column(s) "
                                f"already in condition ({', '.join(sorted(check_cols))}) — "
                                "tightening the same variable is weak, and a check that simply "
                                "negates condition is a contradiction that would fire on every "
                                f"{label}. Reference a different observed variable."
                            ),
                        )
                    )

    return issues


def check_threshold_basis(project_root: Path) -> list[LintIssue]:
    """Tier-2: Warn when numeric thresholds lack justification.

    Scans all .bth.toml files in the project. For each numeric literal found in:
    - outcome.condition (experiment, validation, debug sidecars)
    - benchmark.regression_threshold (if set and non-zero)

    Returns WARNING if numeric found AND corresponding justification field is empty.

    Consistent with check_adversarial_checks: both scan via iter_project_sidecars,
    which excludes nested git boundaries (submodules, worktrees) and dependency/
    cache directories such as .venv.

    Args:
        project_root: Root directory of the project.

    Returns:
        List of LintIssue objects with severity WARNING.
    """
    issues: list[LintIssue] = []

    # Find all .bth.toml files in the project
    for sidecar_path in iter_project_sidecars(project_root):
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
                issues.append(
                    LintIssue(
                        path=sidecar_path,
                        directory="sidecar",
                        issue="unjustified_threshold",
                        severity=IssueSeverity.WARNING,
                        detail=(
                            f"outcomes.{label}.condition contains numeric literal without justification — "
                            "add source = 'explanation' field to document the threshold basis"
                        ),
                    )
                )

        # Check benchmark regression_threshold
        benchmark = data.get("benchmark", {})
        if benchmark:
            regression_threshold = benchmark.get("regression_threshold", 0.0)
            regression_threshold_basis = benchmark.get("regression_threshold_basis", "")

            # Warn if threshold is set (non-zero) but has no basis
            if regression_threshold > 0 and not regression_threshold_basis:
                issues.append(
                    LintIssue(
                        path=sidecar_path,
                        directory="sidecar",
                        issue="unjustified_threshold",
                        severity=IssueSeverity.WARNING,
                        detail=(
                            f"[benchmark] regression_threshold = {regression_threshold} "
                            "without justification — add regression_threshold_basis field to document the threshold basis"
                        ),
                    )
                )

    return issues


def check_epistemic_sidecars(project_root: Path) -> list[LintIssue]:
    """Tier-1/Tier-2 lint for the epistemic-class contract (spec §3.1, §3.2 source grammar).

    - ERROR: a declared `stage_name` that is not canonical (a typo fails closed as confirmatory).
    - WARNING: `exploration`/`calibration` declared without its `[observe]`/`[calibrate]` block.
      Lint does not know prior runs, so it warns unconditionally; the validator decides error vs.
      legacy at run time.
    - WARNING: a `source` on a confirmatory/legacy branch that `parse_source` rejects.
    """
    from bathos.sidecar import (
        CANONICAL_STAGES,
        SidecarKind,
        parse_sidecar,
        parse_source,
        resolve_epistemic_class,
    )

    issues: list[LintIssue] = []
    for sidecar_path in iter_project_sidecars(project_root):
        try:
            sidecar = parse_sidecar(sidecar_path)
        except Exception:
            # Unparseable sidecars are reported by the other sidecar checks.
            continue
        if sidecar.kind != SidecarKind.EXPERIMENT:
            continue
        if not sidecar.stage_name_declared:
            # Legacy (spec 261015 §3.1 row 1): behaviour unchanged, but a sidecar that carries
            # [outcomes] with no stage_name gets a Tier-2 nudge to declare one explicitly.
            if sidecar.outcomes:
                issues.append(
                    LintIssue(
                        path=sidecar_path,
                        directory="sidecar",
                        issue="declare_stage_name",
                        severity=IssueSeverity.WARNING,
                        detail=(
                            "no stage_name declared; treated as legacy. "
                            "declare stage_name explicitly (exploration | calibration | "
                            "validation | ablation | production)"
                        ),
                    )
                )
            continue

        raw = sidecar.stage_name_raw
        if raw not in CANONICAL_STAGES:
            issues.append(
                LintIssue(
                    path=sidecar_path,
                    directory="sidecar",
                    issue="non_canonical_stage_name",
                    severity=IssueSeverity.ERROR,
                    detail=(
                        f"non-canonical stage_name {raw!r}; treated as confirmatory; "
                        f"must be one of {sorted(CANONICAL_STAGES)}"
                    ),
                )
            )
            continue

        if (raw == "exploration" and sidecar.observe is None) or (
            raw == "calibration" and sidecar.calibrate is None
        ):
            block = "[observe]" if raw == "exploration" else "[calibrate]"
            issues.append(
                LintIssue(
                    path=sidecar_path,
                    directory="sidecar",
                    issue="restage",
                    severity=IssueSeverity.WARNING,
                    detail=(
                        f"stage_name {raw!r} without {block} block; "
                        "restage: add [observe]/[calibrate] or choose a confirmatory stage"
                    ),
                )
            )

        cls = resolve_epistemic_class(sidecar, stem_has_prior_runs=True)
        if cls in ("confirmatory", "legacy"):
            for label, spec in sidecar.outcomes.items():
                if spec.source and parse_source(spec.source) is None:
                    issues.append(
                        LintIssue(
                            path=sidecar_path,
                            directory="sidecar",
                            issue="malformed_source",
                            severity=IssueSeverity.WARNING,
                            detail=(
                                f"outcomes.{label}.source {spec.source!r} is not in the closed grammar "
                                "(lit:<ref> | run:<id>:<field> | sesoi:<value>:<why> | "
                                "provisional:<why> | card:<id>)"
                            ),
                        )
                    )
    return issues


def _companion_script_sha256(sidecar_path: Path) -> str:
    """Return SHA256 hex digest of `<stem>.py` adjacent to `<stem>.bth.toml`, or ''."""
    import hashlib

    if not sidecar_path.name.endswith(_SIDECAR_SUFFIX):
        return ""
    stem = sidecar_path.name[: -len(_SIDECAR_SUFFIX)]
    script_path = sidecar_path.parent / f"{stem}.py"
    if not script_path.is_file():
        return ""
    try:
        return hashlib.sha256(script_path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _finite_float(val: object) -> float | None:
    import math

    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    f = float(val)
    return f if math.isfinite(f) else None


_MDE_FIELD_NAMES = frozenset(
    {
        "mde",
        "min_detectable_effect",
        "minimum_detectable_effect",
        "noise_floor",
        "detectable_effect",
    }
)


def check_calibration_sources(
    project_root: Path,
    catalog_dir: Path | None = None,
) -> list[LintIssue]:
    """Tier-1/Tier-2 lint for `run:<id>:<field>` calibration sources & SESOI-vs-MDE (spec §3.5, S5).

    Checks:
    - ERROR (`calibration_run_not_found`, `non_calibrating_run_source`, `calibration_run_unusable`,
      `calibration_field_missing`): every `run:<id>:<field>` must resolve to an existing
      `epistemic_class='calibrating'` run that exited 0 with non-error/non-invalid_measurement
      outcome and carries the named field. Also rejects exploratory self-sourcing (§3.4).
    - WARNING (`underpowered_vs_sesoi`): if a sidecar declares both a `sesoi:<value>:<why>`
      source and a calibration run whose MDE/noise_floor exceeds the SESOI value (C21).
    """
    from bathos.observations import get_observation
    from bathos.sidecar import SidecarKind, parse_sidecar, parse_source, resolve_sidecar_for_run
    from bathos.validate import check_calibration_source_ref

    cd = catalog_dir or _resolve_project_catalog_dir(project_root)
    if cd is None:
        return []

    issues: list[LintIssue] = []
    for sidecar_path in iter_project_sidecars(project_root):
        try:
            sidecar = parse_sidecar(sidecar_path)
        except Exception:
            continue
        if sidecar.kind != SidecarKind.EXPERIMENT:
            continue

        obs_run_id = ""
        if sidecar.derived_from_observation:
            with suppress(Exception):
                obs = get_observation(sidecar.derived_from_observation, catalog_dir=cd)
                if obs is not None:
                    obs_run_id = obs.run_id

        sesoi_sources: list[tuple[str, float]] = []
        resolved_cals: list[tuple[str, str, object, dict]] = []

        for label, spec in sidecar.outcomes.items():
            if not spec.source or "${" in spec.source:
                continue
            src = parse_source(spec.source)
            if src is None:
                continue
            if src.kind == "sesoi" and src.value is not None and src.value > 0:
                sesoi_sources.append((label, float(src.value)))
                continue
            if src.kind != "run":
                continue

            issue_code, issue_msg, run_obj, res_dict = check_calibration_source_ref(spec.source, cd)
            if issue_code is not None and issue_msg:
                if (
                    issue_code == "non_calibrating_run_source"
                    and obs_run_id
                    and run_obj is not None
                    and run_obj.id == obs_run_id
                ):
                    issue_msg += " — exploratory observation source run cannot source a threshold (§3.4)"
                issues.append(
                    LintIssue(
                        path=sidecar_path,
                        directory=sidecar_path.parent.name,
                        issue=issue_code,
                        severity=IssueSeverity.ERROR,
                        detail=f"outcomes.{label}.source: {issue_msg}",
                    )
                )
            elif run_obj is not None:
                resolved_cals.append((label, src.field, run_obj, res_dict))

        if sesoi_sources and resolved_cals:
            db = None
            if catalog_readable(cd):
                with suppress(Exception):
                    db = connect_read(cd, read_only=True)
            try:
                for _cal_label, cal_field, cal_run, cal_res in resolved_cals:
                    mde_candidates: list[tuple[str, float]] = []
                    cal_quantity = ""
                    with suppress(Exception):
                        cal_sc = resolve_sidecar_for_run(
                            getattr(cal_run, "sidecar_path", None),
                            catalog_dir=cd,
                            sidecar_sha256=getattr(cal_run, "sidecar_sha256", "") or "",
                            run_id=getattr(cal_run, "id", "") or "",
                            db=db,
                        )
                        if cal_sc is not None and cal_sc.calibrate is not None:
                            cal_quantity = cal_sc.calibrate.quantity.lower()

                    field_lower = cal_field.lower()
                    is_mde_field = (
                        field_lower in _MDE_FIELD_NAMES
                        or field_lower.endswith("_mde")
                        or "mde" in cal_quantity
                        or "detectable" in cal_quantity
                        or "noise floor" in cal_quantity
                    )
                    if is_mde_field:
                        v = _finite_float(cal_res.get(cal_field))
                        if v is not None:
                            mde_candidates.append((cal_field, v))

                    for extra_k in ("mde", "min_detectable_effect", "minimum_detectable_effect"):
                        if extra_k == cal_field and any(k == extra_k for k, _ in mde_candidates):
                            continue
                        ev = _finite_float(cal_res.get(extra_k))
                        if ev is not None:
                            mde_candidates.append((extra_k, ev))

                    for sesoi_label, sesoi_val in sesoi_sources:
                        for mde_name, mde_val in mde_candidates:
                            if mde_val > sesoi_val:
                                issues.append(
                                    LintIssue(
                                        path=sidecar_path,
                                        directory=sidecar_path.parent.name,
                                        issue="underpowered_vs_sesoi",
                                        severity=IssueSeverity.WARNING,
                                        detail=(
                                            f"outcomes.{sesoi_label} SESOI ({sesoi_val:g}) is smaller than "
                                            f"calibration {mde_name} ({mde_val:g}) from run "
                                            f"{getattr(cal_run, 'id', '')[:8]} — design is underpowered "
                                            "by construction (C21)"
                                        ),
                                    )
                                )
            finally:
                if db is not None:
                    db.close()

    return issues


def check_holdout_and_observation_lint(
    project_root: Path,
    catalog_dir: Path | None = None,
) -> list[LintIssue]:
    """Tier-1/Tier-2 lint for `[holdout]`, observation promotion, C3 laundering, & same-argv (S4)."""
    from bathos.observations import get_observation
    from bathos.query import get_run, list_runs
    from bathos.sidecar import (
        SidecarKind,
        canonical_outcomes_signature,
        parse_sidecar,
        resolve_epistemic_class,
        resolve_sidecar_for_run,
    )
    from bathos.validate import (
        ValidationError,
        read_run_result_metadata,
        validate_holdout_and_observation,
    )

    cd = catalog_dir or _resolve_project_catalog_dir(project_root)
    issues: list[LintIssue] = []

    for sidecar_path in iter_project_sidecars(project_root):
        try:
            sidecar = parse_sidecar(sidecar_path)
        except Exception:
            continue
        if sidecar.kind != SidecarKind.EXPERIMENT:
            continue
        cls = resolve_epistemic_class(sidecar, stem_has_prior_runs=True)
        errs: list[ValidationError] = []
        validate_holdout_and_observation(
            sidecar,
            sidecar_path,
            cls,
            catalog_dir=cd,
            project_root=project_root,
            errors=errs,
        )
        for err in errs:
            # Skip unfilled scaffold placeholders and run: self-sourcing (reported by check_calibration_sources).
            if "${" in err.message:
                continue
            if cd is not None and err.field.startswith("outcomes.") and err.field.endswith(".source"):
                continue
            issues.append(
                LintIssue(
                    path=sidecar_path,
                    directory=sidecar_path.parent.name,
                    issue="holdout_or_observation_invalid",
                    severity=IssueSeverity.ERROR,
                    detail=f"{err.field}: {err.message}",
                )
            )

    if cd is None:
        return issues

    project_slug = ""
    bth_toml = project_root / ".bth.toml"
    if bth_toml.is_file():
        with suppress(Exception):
            from bathos.config import load_project_config

            project_slug = load_project_config(bth_toml).slug

    try:
        all_runs = list_runs(cd, project=project_slug or None, limit=10_000)
    except Exception:
        all_runs = []

    # Laundering tripwire (C3): match confirmatory sidecars with no [holdout] against prior exploratory runs.
    exp_by_sha: dict[str, object] = {}
    exp_by_outcomes: dict[str, object] = {}
    db = None
    if catalog_readable(cd):
        with suppress(Exception):
            db = connect_read(cd, read_only=True)
    try:
        for r in all_runs:
            if r.epistemic_class != "exploratory":
                continue
            if r.script_sha256 and r.script_sha256 not in exp_by_sha:
                exp_by_sha[r.script_sha256] = r
            with suppress(Exception):
                raw_sp = r.sidecar_path
                if raw_sp and not Path(raw_sp).is_absolute() and not Path(raw_sp).is_file():
                    cand_sp = project_root / raw_sp
                    if cand_sp.is_file():
                        raw_sp = str(cand_sp)
                sc = resolve_sidecar_for_run(
                    raw_sp,
                    catalog_dir=cd,
                    sidecar_sha256=r.sidecar_sha256 or "",
                    run_id=r.id,
                    db=db,
                )
                if sc is not None and sc.outcomes:
                    sig = canonical_outcomes_signature(sc.outcomes)
                    if sig and sig not in exp_by_outcomes:
                        exp_by_outcomes[sig] = r
    finally:
        if db is not None:
            db.close()

    if exp_by_sha or exp_by_outcomes:
        for sidecar_path in iter_project_sidecars(project_root):
            try:
                sidecar = parse_sidecar(sidecar_path)
            except Exception:
                continue
            if sidecar.kind != SidecarKind.EXPERIMENT:
                continue
            cls = resolve_epistemic_class(sidecar, stem_has_prior_runs=False)
            if cls not in ("confirmatory", "legacy"):
                continue
            if sidecar.holdout is not None or sidecar.derived_from_observation:
                continue

            reasons: list[str] = []
            matched_run = None
            script_sha = _companion_script_sha256(sidecar_path)
            if script_sha and script_sha in exp_by_sha:
                matched_run = exp_by_sha[script_sha]
                reasons.append("script_sha256")
            sig = canonical_outcomes_signature(sidecar.outcomes)
            if sig and sig in exp_by_outcomes:
                if matched_run is None:
                    matched_run = exp_by_outcomes[sig]
                reasons.append("canonicalized [outcomes]")

            if matched_run is not None:
                issues.append(
                    LintIssue(
                        path=sidecar_path,
                        directory=sidecar_path.parent.name,
                        issue="laundering_tripwire",
                        severity=IssueSeverity.WARNING,
                        detail=(
                            f"confirmatory sidecar has no [holdout] and matches prior exploratory run "
                            f"{getattr(matched_run, 'id', '')[:8]} ({', '.join(reasons)}) — possible "
                            "rename-and-retune without holdout (C3)"
                        ),
                    )
                )

    # Same-argv warning (§3.4): confirmatory run argv matches observation source run argv.
    for r in all_runs:
        if r.epistemic_class not in ("confirmatory", "legacy"):
            continue
        meta = read_run_result_metadata(r, cd)
        obs_id = str(meta.get("derived_from_observation") or "")
        if not obs_id:
            continue
        with suppress(Exception):
            obs = get_observation(obs_id, catalog_dir=cd)
            if obs is None or not obs.run_id:
                continue
            src_run = get_run(obs.run_id, cd, allow_cool_fallback=True, include_superseded=True)
            if src_run is not None and list(r.argv or []) == list(src_run.argv or []):
                issues.append(
                    LintIssue(
                        path=cd,
                        directory="catalog",
                        issue="holdout_same_argv",
                        severity=IssueSeverity.WARNING,
                        detail=(
                            f"confirmatory run {r.id[:8]} has identical argv to observation {obs.id} "
                            f"source run {src_run.id[:8]} ({r.argv}) — verify test data is held out"
                        ),
                    )
                )

    return issues


def check_stale_scripts_without_reason(project_root: Path) -> list[LintIssue]:
    """Tier-2: Warn when [status] stale=true lacks a stale_reason (debt: archival design).

    Scans all .bth.toml files in the project. A stale flag with no reason is exactly as
    unjustified as a numeric threshold with no source -- see check_threshold_basis -- and
    it also degrades the pre-registration gate's deny message (prereg.gate_check) into
    something un-actionable for whoever hits it.

    Args:
        project_root: Root directory of the project.

    Returns:
        List of LintIssue objects with severity WARNING.
    """
    issues: list[LintIssue] = []

    for sidecar_path in iter_project_sidecars(project_root):
        try:
            with open(sidecar_path, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            continue

        status = data.get("status", {})
        if not isinstance(status, dict) or not status.get("stale"):
            continue

        if not str(status.get("stale_reason", "")).strip():
            issues.append(
                LintIssue(
                    path=sidecar_path,
                    directory="sidecar",
                    issue="stale_without_reason",
                    severity=IssueSeverity.WARNING,
                    detail=(
                        "[status] stale=true but stale_reason is empty — add a reason so "
                        "the pre-registration gate's deny message is actionable"
                    ),
                )
            )

    return issues


def check_archival_candidates(
    project_root: Path, catalog_dir: Path, project_slug: str | None = None
) -> list[LintIssue]:
    """Tier-2: propose (never act on) stale scripts that have not yet been archived.

    Deliberately narrow v1 scope: flags any sidecar with [status] stale=true (see
    check_stale_scripts_without_reason / prereg.gate_check) that has no corresponding
    archived_items record. This is lint's role in the archival design -- PROPOSE a
    candidate; only an explicit `bth archive-artifact` call (bathos.artifact_archive)
    actually mutates the tree.

    A sidecar counts as "already archived" only if the LATEST archived_items event for
    the covering item is "archived" (not "restored") -- a restored-then-still-stale
    sidecar is a fresh candidate again, not permanently exempt.

    project_slug scopes the archived_items query, mirroring
    artifact_archive._tracked_output_paths_for_script. catalog_dir is commonly one shared
    ~/.bth/catalog/ across all of a researcher's projects (bathos.config), and this repo's
    own scripts/ directory convention (scripts/experiments/verb_noun.py) makes identical
    relative paths across projects likely -- without this filter, an archived record in
    ANY project would silently suppress the warning for a same-path stale script in every
    other project. When project_slug is not available to the caller, the check falls back
    to unscoped (pre-fix) behavior rather than erroring.
    """
    from bathos.sidecar import parse_sidecar

    issues: list[LintIssue] = []

    archived_rel_paths: set[str] = set()
    if catalog_readable(catalog_dir):
        import json

        try:
            con = connect_read(catalog_dir, read_only=True)
            try:
                if project_slug is not None:
                    rows = con.execute(
                        "SELECT id, paths, event, recorded_at FROM archived_items "
                        "WHERE project_slug = ? ORDER BY recorded_at ASC",
                        [project_slug],
                    ).fetchall()
                else:
                    rows = con.execute(
                        "SELECT id, paths, event, recorded_at FROM archived_items "
                        "ORDER BY recorded_at ASC"
                    ).fetchall()
            finally:
                con.close()
            latest_by_id: dict[str, tuple[str, str]] = {}
            for item_id, paths_json, event_name, _recorded_at in rows:
                latest_by_id[item_id] = (event_name, paths_json)  # ASC order -> last wins
            for event_name, paths_json in latest_by_id.values():
                if event_name != "archived":
                    continue
                try:
                    paths = json.loads(paths_json) if paths_json else []
                except (TypeError, ValueError):
                    paths = []
                archived_rel_paths.update(paths)
        except Exception:
            pass  # no archived_items table yet -- every stale sidecar is a candidate

    for sidecar_path in iter_project_sidecars(project_root):
        try:
            sidecar = parse_sidecar(sidecar_path)
        except Exception:
            continue
        if not (sidecar.status and sidecar.status.stale):
            continue

        try:
            rel = str(sidecar_path.relative_to(project_root))
        except ValueError:
            rel = str(sidecar_path)
        if rel in archived_rel_paths:
            continue

        issues.append(
            LintIssue(
                path=sidecar_path,
                directory="sidecar",
                issue="unarchived_stale_script",
                severity=IssueSeverity.WARNING,
                detail=(
                    "[status] stale=true but no archived_items record covers this sidecar — "
                    "run `bth archive-artifact` to migrate it out of working-tree visibility"
                ),
            )
        )

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
    for sidecar_path in iter_project_sidecars(project_root):
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
            issues.append(
                LintIssue(
                    path=sidecar_path,
                    directory="sidecar",
                    issue="todo_in_scaffold",
                    severity=IssueSeverity.WARNING,
                    detail=(
                        "hypothesis contains TODO placeholder — replace with actual hypothesis"
                    ),
                )
            )

        # Check decision fields in outcomes
        outcomes = data.get("outcomes", {})
        for label, outcome in outcomes.items():
            if not isinstance(outcome, dict):
                continue
            decision = outcome.get("decision", "")
            if decision and "TODO" in decision:
                issues.append(
                    LintIssue(
                        path=sidecar_path,
                        directory="sidecar",
                        issue="todo_in_scaffold",
                        severity=IssueSeverity.WARNING,
                        detail=(
                            f"outcome '{label}' decision contains TODO placeholder — "
                            "replace with actual next step"
                        ),
                    )
                )

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
    import tempfile


    if not catalog_readable(catalog_dir):
        return []

    temp_root = str(Path(tempfile.gettempdir()).resolve())
    temp_patterns = list({"/tmp/%", "/var/tmp/%", temp_root + "/%"})

    try:
        db = connect_read(catalog_dir, read_only=True)
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


# argv flags that name a directory a script writes into (output tiering spec 261023, section 9 row 10).
RESUME_DIR_FLAGS = ("--resume-dir", "--out-dir")


def _flag_values(argv: list[str]) -> list[tuple[str, str]]:
    """``(flag, value)`` for every RESUME_DIR_FLAGS occurrence in ``argv``, both ``--flag v`` and ``--flag=v``."""
    out: list[tuple[str, str]] = []
    for i, tok in enumerate(argv):
        for flag in RESUME_DIR_FLAGS:
            if tok == flag and i + 1 < len(argv):
                out.append((flag, argv[i + 1]))
            elif tok.startswith(flag + "="):
                out.append((flag, tok[len(flag) + 1 :]))
    return out


def _as_relpath(value: str, worktree_root: str | None) -> str:
    """POSIX path of ``value`` relative to the run's worktree root when it is under it, else as given (lexical)."""
    import os

    if os.path.isabs(value):
        if worktree_root:
            rel = os.path.relpath(os.path.normpath(value), os.path.normpath(worktree_root))
            if not rel.startswith(".."):
                return Path(rel).as_posix()
        return Path(os.path.normpath(value)).as_posix()
    return Path(os.path.normpath(value)).as_posix()


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _json_list(value: object) -> list:
    import json

    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return list(value) if isinstance(value, list) else []


def check_undeclared_resume_dirs(catalog_dir: Path, project_slug: str = "") -> list[LintIssue]:
    """Flag runs whose argv passes ``--resume-dir`` / ``--out-dir`` a directory outside every declared root.

    Reads only the run record (``argv``, the ``run.started`` sidecar declaration, ``adopted_outputs``), so it works
    whether or not the directory still exists. A directory is covered when it lies under one of the run's declared
    ``[outputs] roots``, or when ``bth outputs adopt`` recorded files under it. Anything else gets no reference hashes,
    so ``bth cold`` cannot copy it and the worktree cannot be removed without losing it (the r6/r7 ``*_cells/``
    case). A relative value is read against the run's worktree root. ``project_slug`` limits the check to that
    project's runs (the catalog is commonly shared); empty checks every run.
    """
    if not catalog_readable(catalog_dir):
        return []
    try:
        db = connect_read(catalog_dir, read_only=True)
    except Exception:
        return []
    try:
        where = "(" + " OR ".join(f"CAST(argv AS VARCHAR) LIKE '%{flag}%'" for flag in RESUME_DIR_FLAGS) + ")"
        params: list[str] = []
        if project_slug:
            where += " AND project_slug = ?"
            params.append(project_slug)
        try:
            rows = db.execute(
                f"SELECT id, command, argv, adopted_outputs FROM runs WHERE {where} ORDER BY id", params
            ).fetchall()
        except duckdb.Error:  # a legacy warm catalog has no adopted_outputs column
            rows = [
                (rid, cmd, argv, None)
                for rid, cmd, argv in db.execute(
                    f"SELECT id, command, argv FROM runs WHERE {where} ORDER BY id", params
                ).fetchall()
            ]
        started: dict[str, tuple[str | None, list]] = {}
        with suppress(duckdb.Error):  # no events table before the run-log cut-over
            for key, wroot, roots in db.execute(
                "SELECT entity_key, worktree_root, json_extract(data, '$.sidecar.outputs.roots') "
                "FROM events WHERE kind = 'run.started'"
            ).fetchall():
                ids = _json_list(key)
                if ids:
                    started[str(ids[0])] = (wroot, _json_list(roots))
    finally:
        db.close()

    issues: list[LintIssue] = []
    for run_id, command, argv, adopted in rows:
        wroot, roots = started.get(run_id, (None, []))
        declared = [_as_relpath(str(r), wroot) for r in roots]
        adopted_rels = [str(e.get("relpath", "")) for e in _json_list(adopted) if isinstance(e, dict)]
        for flag, value in _flag_values([str(t) for t in _json_list(argv)]):
            rel = _as_relpath(value, wroot)
            if any(_under(rel, r) for r in declared) or any(_under(a, rel) for a in adopted_rels if a):
                continue
            issues.append(
                LintIssue(
                    path=catalog_dir,
                    directory="catalog",
                    issue="undeclared_resume_dir",
                    severity=IssueSeverity.WARNING,
                    detail=(
                        f"Run {run_id[:8]} ({(command or '')[:40]!r}) passed {flag} {value}, which is under no "
                        "declared [outputs] root: its files get no reference hashes and bth cold cannot copy "
                        "them. Declare the directory in the sidecar's [outputs] roots, or `bth outputs adopt` "
                        "it for a run that predates the standard"
                    ),
                )
            )
            if len(issues) >= 50:
                return issues
    return issues


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

    from bathos.schema import STAGE_NAME_REGEX

    if not catalog_readable(catalog_dir):
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
        db = connect_read(catalog_dir, read_only=True)
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


def check_baseline_ref_exists(
    project_root: Path,
    db_path: Path,
) -> list[LintIssue]:
    """Tier-2: Validate that baseline_ref values exist in the warm catalog.

    Scans all benchmark sidecars in scripts/benchmarks/ for baseline_ref fields.
    For each non-empty baseline_ref, queries the warm-tier DuckDB catalog using:
      SELECT outcome, started_at FROM runs WHERE id = ? OR id LIKE ? LIMIT 1

    Supports both full UUIDs and short prefixes (e.g., "abc12345%").

    If baseline_ref not found: WARNING
    If baseline_ref found: returns issue with baseline outcome info for audit purposes

    Args:
        project_root: Path to project root.
        db_path: Path to bathos.db warm catalog.

    Returns:
        List of LintIssue objects with severity WARNING (not found) or informational details.
    """

    if not catalog_readable(db_path.parent):
        return []

    scripts_dir = project_root / "scripts" / "benchmarks"
    if not scripts_dir.exists():
        return []

    issues: list[LintIssue] = []

    for sidecar_path in iter_project_sidecars(scripts_dir):
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
            db = connect_read(db_path.parent, read_only=True)
            db.execute("SET TimeZone='UTC'")

            row = db.execute(
                "SELECT outcome, timestamp FROM runs WHERE id = ? OR id LIKE ? LIMIT 1",
                [baseline_ref, baseline_ref + "%"],
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
    # Deliberately unused. AC-06 is specified as a campaign-wide "did you vary
    # anything" smell test over the runs' own grid parameter values, deliberately
    # NOT scoped to the claim's discriminability matrix — that is AC-04/AC-05's
    # job, and those two do consume this data (see claim.py). The parameter is
    # accepted only for signature symmetry across the three gates.
    claim_discriminability: list[dict],  # noqa: ARG001
    campaign_id: str,
    db: duckdb.DuckDBPyConnection,
) -> list[LintIssue]:
    """AC-06: warn if all confirmatory runs in a campaign share identical metadata values.

    Reads ``runs.metadata`` via ``campaign_runs``; it does not read
    ``claim_discriminates``. Not currently wired into any command — ``bth lint
    --campaign`` is post-MVP, so this is exercised by tests only.

    Args:
        claim_discriminability: Discriminability entries from the claim. Accepted
            for signature symmetry with the AC-04/AC-05 gates and intentionally
            unused; see the comment on the parameter.
        campaign_id: Campaign ID to check
        db: DuckDB connection

    Returns:
        List of LintIssue objects with severity WARNING if single-cell pattern detected
    """
    import json

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
        with suppress(json.JSONDecodeError, TypeError):
            metas.append(json.loads(meta_str))

    if len(metas) < 2:
        return issues

    # Find keys present in ALL runs
    common_keys = set(metas[0].keys())
    for m in metas[1:]:
        common_keys &= set(m.keys())

    if not common_keys:
        return issues

    # Check if all runs have the same value for every common key
    uniform_keys = [k for k in common_keys if len({str(m.get(k)) for m in metas}) == 1]

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


def _resolve_project_catalog_dir(project_root: Path) -> Path | None:
    """Resolve a project-scoped catalog directory without leaking ~/.bth/catalog."""
    import os

    env_cat = os.environ.get("BTH_CATALOG_DIR")
    if env_cat:
        p = Path(env_cat)
        return p if p.exists() else None
    local_cat = project_root / ".bth" / "catalog"
    if local_cat.exists():
        return local_cat
    bth_toml = project_root / ".bth.toml"
    if bth_toml.is_file():
        with suppress(Exception):
            from bathos.config import load_project_config

            cfg = load_project_config(bth_toml)
            if cfg.catalog_dir and cfg.catalog_dir.exists():
                return cfg.catalog_dir
    return None


def _lint_single_cell_campaigns(project_root: Path) -> list[LintIssue]:
    """Run `check_single_cell_gate` over confirmatory/sequential campaigns in the project catalog."""
    catalog_dir = _resolve_project_catalog_dir(project_root)
    if catalog_dir is None or not catalog_readable(catalog_dir):
        return []

    issues: list[LintIssue] = []
    try:
        db = connect_read(catalog_dir, read_only=True)
        try:
            rows = db.execute(
                "SELECT id FROM campaigns WHERE mode IN ('confirmation', 'sequential')"
            ).fetchall()
            for (campaign_id,) in rows:
                issues.extend(check_single_cell_gate([], campaign_id, db))
        finally:
            db.close()
    except Exception:
        return []
    return issues


def check_invariants_declared(project_root: Path) -> list[LintIssue]:
    """Tier-2 advisory: warn when confirmatory/benchmark sidecars declare no `[invariants]` (spec 260707 §9)."""
    from bathos.sidecar import SidecarKind, parse_sidecar

    issues: list[LintIssue] = []
    for dir_name in ("experiments", "benchmarks"):
        target_dir = project_root / "scripts" / dir_name
        if not target_dir.exists():
            continue
        for sidecar_path in iter_project_sidecars(target_dir):
            if sidecar_path.name == ".bth.toml":
                continue
            try:
                sidecar = parse_sidecar(sidecar_path)
            except Exception:
                continue
            if sidecar.kind not in (SidecarKind.EXPERIMENT, SidecarKind.BENCHMARK):
                continue
            if sidecar.kind == SidecarKind.EXPERIMENT and sidecar.observe is not None:
                continue
            if sidecar.kind == SidecarKind.EXPERIMENT and sidecar.calibrate is not None:
                continue
            if not sidecar.invariants:
                issues.append(
                    LintIssue(
                        path=sidecar_path,
                        directory=dir_name,
                        issue="missing_invariants",
                        severity=IssueSeverity.WARNING,
                        detail=(
                            "sidecar declares no [invariants] assertions — consider adding numeric "
                            "or differential invariants to fail closed on silent measurement bugs"
                        ),
                    )
                )
    return issues


def check_unmeasured_data_quality_claims(project_root: Path) -> list[LintIssue]:
    """Flag sidecars making prose data-quality/filtering claims without measured counts + invariants (#2662)."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_prose_data_quality_claims

    issues: list[LintIssue] = []
    for sidecar_path in iter_project_sidecars(project_root):
        if sidecar_path.name == ".bth.toml":
            continue
        try:
            sidecar = parse_sidecar(sidecar_path)
        except Exception:
            continue
        for err in validate_prose_data_quality_claims(sidecar):
            issues.append(
                LintIssue(
                    path=sidecar_path,
                    directory=sidecar_path.parent.name,
                    issue="unmeasured_data_quality_claim",
                    severity=IssueSeverity.ERROR,
                    detail=err.message,
                )
            )
    return issues


def check_factor_coverage_lint(project_root: Path) -> list[LintIssue]:
    """Lint sidecars for factor coverage / exclusion declarations and `n_units_informative` (#2200)."""
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_factor_declarations

    issues: list[LintIssue] = []
    for sidecar_path in iter_project_sidecars(project_root):
        if sidecar_path.name == ".bth.toml":
            continue
        try:
            sidecar = parse_sidecar(sidecar_path)
        except Exception:
            continue
        warnings: list[str] = []
        errors = validate_factor_declarations(sidecar, warnings=warnings)
        for err in errors:
            issues.append(
                LintIssue(
                    path=sidecar_path,
                    directory=sidecar_path.parent.name,
                    issue="factor_declaration_invalid",
                    severity=IssueSeverity.ERROR,
                    detail=err.message,
                )
            )
        for warn in warnings:
            issues.append(
                LintIssue(
                    path=sidecar_path,
                    directory=sidecar_path.parent.name,
                    issue="missing_n_units_informative",
                    severity=IssueSeverity.WARNING,
                    detail=warn,
                )
            )
        if sidecar.declared_levels and sidecar.result_schema:
            schema_keys = set(sidecar.result_schema.keys())
            for factor, levels in sidecar.declared_levels.items():
                plural = factor if factor.endswith("s") else f"{factor}s"
                coverage_keys = {
                    "analyzed_levels",
                    "covered_levels",
                    factor,
                    plural,
                    f"{factor}_analyzed",
                    f"analyzed_{factor}",
                    f"per_{factor}",
                }
                all_excluded = all(
                    (f"{factor}.{lv}" in sidecar.excluded_levels and sidecar.excluded_levels[f"{factor}.{lv}"].strip())
                    or (lv in sidecar.excluded_levels and sidecar.excluded_levels[lv].strip())
                    for lv in levels
                )
                if not (schema_keys & coverage_keys) and not all_excluded:
                    issues.append(
                        LintIssue(
                            path=sidecar_path,
                            directory=sidecar_path.parent.name,
                            issue="declared_levels_untracked",
                            severity=IssueSeverity.WARNING,
                            detail=(
                                f"declared_levels.{factor} has no matching coverage field in "
                                "[result_schema] (e.g. analyzed_levels / per_<factor>)"
                            ),
                        )
                    )
    return issues


def check_stepback_reviews(project_root: Path) -> list[LintIssue]:
    """Check stepback review files and sidecar [stepback] sections in project.

    Checks:
    - Review records in .bth/stepback/reviews/: validates JSON syntax, schema compliance,
      and evidence citations count.
    """
    import json

    issues: list[LintIssue] = []
    stepback_reviews_dir = project_root / ".bth" / "stepback" / "reviews"
    if stepback_reviews_dir.exists():
        for p in stepback_reviews_dir.rglob("*.json"):
            if not p.is_file():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                issues.append(
                    LintIssue(
                        path=p,
                        directory=".bth/stepback/reviews",
                        issue="stepback_invalid_json",
                        severity=IssueSeverity.ERROR,
                        detail=f"Invalid JSON: {e}",
                    )
                )
                continue

            rec = data.get("recommendation")
            valid_recs = {"CONTINUE", "PIVOT", "HOLD", "ESCALATE", "CONCLUDE_ABANDON"}
            if rec not in valid_recs:
                issues.append(
                    LintIssue(
                        path=p,
                        directory=".bth/stepback/reviews",
                        issue="stepback_invalid_recommendation",
                        severity=IssueSeverity.ERROR,
                        detail=f"Recommendation {rec!r} not in {valid_recs}",
                    )
                )

            evidence = data.get("cited_evidence", [])
            if not isinstance(evidence, list) or len(evidence) < 2:
                issues.append(
                    LintIssue(
                        path=p,
                        directory=".bth/stepback/reviews",
                        issue="stepback_insufficient_evidence",
                        severity=IssueSeverity.WARNING,
                        detail=f"Review has {len(evidence) if isinstance(evidence, list) else 0} cited evidence items, expected >= 2",
                    )
                )

    return issues


def _iter_parsed_toml_sidecars(project_root: Path) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Yield ``(sidecar_path, data)`` for every parseable ``*.bth.toml`` under ``project_root``."""
    for sidecar_path in iter_project_sidecars(project_root):
        if sidecar_path.name == ".bth.toml":
            continue
        try:
            data = tomllib.loads(sidecar_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            yield sidecar_path, data


def _collect_schema_keys(schema: Any, prefix: str = "") -> set[str]:
    """Collect top-level and dotted leaf names from a raw ``[result_schema]`` dict."""
    if not isinstance(schema, dict):
        return set()
    keys: set[str] = set()
    for key, val in schema.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        keys.add(dotted)
        if isinstance(val, dict):
            keys.update(_collect_schema_keys(val, dotted))
    return keys


def _collect_sidecar_expressions(data: dict[str, Any]) -> list[tuple[str, str]]:
    """Collect ``(location, sql_condition)`` pairs from outcomes, invariants, and sanity."""
    exprs: list[tuple[str, str]] = []
    outcomes = data.get("outcomes")
    if isinstance(outcomes, dict):
        for label, spec in outcomes.items():
            if not isinstance(spec, dict):
                continue
            cond = spec.get("condition")
            if isinstance(cond, str) and cond.strip():
                exprs.append((f"outcomes.{label}.condition", cond))
            adv = spec.get("adversarial_check")
            if isinstance(adv, str) and adv.strip():
                exprs.append((f"outcomes.{label}.adversarial_check", adv))

    invariants = data.get("invariants")
    if isinstance(invariants, dict):
        for name, spec in invariants.items():
            if isinstance(spec, dict) and isinstance(spec.get("condition"), str):
                exprs.append((f"invariants.{name}.condition", spec["condition"]))

    sanity = data.get("sanity")
    if isinstance(sanity, dict):
        assertions = sanity.get("assertions")
        if isinstance(assertions, dict):
            for name, spec in assertions.items():
                if isinstance(spec, dict) and isinstance(spec.get("condition"), str):
                    exprs.append((f"sanity.assertions.{name}.condition", spec["condition"]))
        for name, spec in sanity.items():
            if name in ("argv_suffix", "assertions"):
                continue
            if isinstance(spec, dict) and isinstance(spec.get("condition"), str):
                exprs.append((f"sanity.{name}.condition", spec["condition"]))

    observe = data.get("observe")
    if isinstance(observe, dict):
        obs_sanity = observe.get("sanity")
        if isinstance(obs_sanity, list):
            for idx, item in enumerate(obs_sanity):
                if isinstance(item, str) and item.strip():
                    exprs.append((f"observe.sanity[{idx}]", item))
    return exprs


def _collect_sidecar_prose(data: dict[str, Any]) -> str:
    """Concatenate prose fields (excluding ``outcomes.*.source`` citation metadata)."""
    parts: list[str] = []
    for section_name in ("experiment", "benchmark", "validation", "debug"):
        sec = data.get(section_name)
        if isinstance(sec, dict):
            for field_name in (
                "hypothesis",
                "question",
                "property",
                "reference",
                "symptom",
                "suspected_cause",
                "verification",
            ):
                val = sec.get(field_name)
                if isinstance(val, str) and val.strip():
                    parts.append(val)
    outcomes = data.get("outcomes")
    if isinstance(outcomes, dict):
        for spec in outcomes.values():
            if isinstance(spec, dict):
                for field_name in ("decision", "reasoning"):
                    val = spec.get(field_name)
                    if isinstance(val, str) and val.strip():
                        parts.append(val)
    return " \n ".join(parts)


# ── #3094: Unit of independence ──────────────────────────────────────────────

_REPLICA_UNIT_RE = re.compile(
    r"\b(?:n_?replicas?|replicas?|n_?seeds?|seeds?|pooled_\w+)\b",
    re.IGNORECASE,
)
_REPLICA_PROSE_RE = re.compile(
    r"\b(?:replicas?|replicates?|n_replicas|n_seeds|seed\s+replicates?|"
    r"pool(?:ed|ing)\s+(?:across|replicas?|trajectories|seeds?))\b",
    re.IGNORECASE,
)
_PER_REPLICA_CONVERGENCE_RE = re.compile(
    r"\b(?:per_replica_converg\w*|within_replica_converg\w*|replica_converg\w*|"
    r"all_replicas_converg\w*|every_replica_converg\w*|min_replica_converg\w*|"
    r"per_seed_converg\w*|within_seed_converg\w*|seed_converg\w*|all_seeds_converg\w*)\b",
    re.IGNORECASE,
)
_PER_REPLICA_CONVERGENCE_PROSE_RE = re.compile(
    r"\b(?:per[- ]replica\s+convergence|within[- ]replica\s+convergence|"
    r"each\s+replica\s+(?:must\s+)?converge)\b",
    re.IGNORECASE,
)
_GENERIC_CONVERGED_RE = re.compile(r"\b(?:is_)?converged\b", re.IGNORECASE)
_REPLICA_SPLIT_NULL_RE = re.compile(
    r"\b(?:replica_split_null\w*|split_replica_null\w*|per_replica_null\w*|within_replica_null\w*|"
    r"replica_shuffle_null\w*|within_replica_shuffle\w*|slip_shift_null\w*)\b",
    re.IGNORECASE,
)
_REPLICA_SPLIT_NULL_PROSE_RE = re.compile(
    r"\b(?:replica[- ]split\s+null|per[- ]replica\s+null|within[- ]replica\s+(?:null|shuffle))\b",
    re.IGNORECASE,
)
_REPLICA_SPREAD_RE = re.compile(
    r"\b(?:replica_spread|between_replica_spread|between_replica_std|between_replica_sem|"
    r"replica_std|replica_sem|replica_se|replica_err|replica_ci|"
    r"seed_spread|between_seed_std|seed_std|seed_sem|seed_se)\b",
    re.IGNORECASE,
)
_SEEDS_AS_N_RE = re.compile(
    r"(?:sqrt\s*\(\s*n_(?:seeds|replicas)\s*\)|"
    r"\bn_(?:seeds|replicas)\s*(?:>=|>|<=|<|==|=)\s*\d+|"
    r"\bn_eff\s*=\s*n_(?:seeds|replicas)\b)",
    re.IGNORECASE,
)
_NEGATED_ANTI_PATTERN_RE = re.compile(
    r"\b(?:avoid|no|not|without|drop|dropped|never|instead\s+of)\s+[^.;\n]{0,40}$",
    re.IGNORECASE,
)
_POOLED_CONV_METRIC_RE = re.compile(
    r"\b(?:pooled_\w+|block_bootstrap\w*|ensemble_\w+)\b",
    re.IGNORECASE,
)
_STAT007_WAIVE_RE = re.compile(
    r"\b(?:stat[-_]?007|unit[-_ ]of[-_ ]independence)\b",
    re.IGNORECASE,
)
_STAT008_WAIVE_RE = re.compile(
    r"\b(?:stat[-_]?008|source[-_/ ]batch[-_ ]confound)\b",
    re.IGNORECASE,
)


def _breakdown_waives_rule(tbl: dict[str, Any], rule_re: re.Pattern[str], flag_key: str) -> bool:
    """Return True only if ``[breakdown]`` explicitly waives the given STAT rule."""
    flag_val = tbl.get(flag_key)
    if flag_val is True or (isinstance(flag_val, str) and bool(flag_val.strip())):
        return True
    waive_rules = tbl.get("waive_rules")
    if isinstance(waive_rules, str) and rule_re.search(waive_rules):
        return True
    if isinstance(waive_rules, list) and any(
        isinstance(item, str) and rule_re.search(item) for item in waive_rules
    ):
        return True
    waive = tbl.get("waive")
    return isinstance(waive, str) and bool(rule_re.search(waive))


def _has_unnegated_prose_match(prose: str, pattern: re.Pattern[str]) -> bool:
    for m in pattern.finditer(prose):
        prefix = prose[max(0, m.start() - 48) : m.start()]
        if not _NEGATED_ANTI_PATTERN_RE.search(prefix):
            return True
    return False


def _extract_unit_of_independence(data: dict[str, Any], schema_keys: set[str], prose: str) -> tuple[str, bool]:
    """Return ``(declared_unit, explicitly_waived)`` from sidecar tables."""
    for tbl_name in ("independence", "pooling", "units"):
        tbl = data.get(tbl_name)
        if isinstance(tbl, dict):
            waive = tbl.get("waive")
            if isinstance(waive, str) and waive.strip():
                return "", True
    breakdown_tbl = data.get("breakdown")
    if isinstance(breakdown_tbl, dict) and _breakdown_waives_rule(
        breakdown_tbl, _STAT007_WAIVE_RE, "waive_independence"
    ):
        return "", True

    for tbl_name in ("independence", "pooling", "units", "breakdown", "holdout"):
        tbl = data.get(tbl_name)
        if isinstance(tbl, dict):
            for key in ("unit", "unit_of_independence", "pooling_unit", "bootstrap_unit"):
                val = tbl.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip(), False

    exp = data.get("experiment")
    if isinstance(exp, dict):
        for key in ("unit_of_independence", "pooling_unit", "bootstrap_unit"):
            val = exp.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip(), False

    schema_lower = {k.lower() for k in schema_keys}
    if any("block_bootstrap" in k or "bootstrap_unit" in k or "pooling_unit" in k for k in schema_lower):
        return "block_bootstrap", False
    if re.search(r"\bblock[- ]bootstrap\b", prose, re.IGNORECASE):
        return "block_bootstrap", False
    return "", False


def _non_exchangeable_justified(data: dict[str, Any], prose: str) -> bool:
    for tbl_name in ("independence", "pooling", "units", "experiment"):
        tbl = data.get(tbl_name)
        if isinstance(tbl, dict) and tbl.get("exchangeable") is False:
            return True
    return bool(
        re.search(
            r"\b(?:non[- ]exchangeable|distinct\s+starting|different\s+starting)\b",
            prose,
            re.IGNORECASE,
        )
    )


def assess_unit_of_independence_sidecar(data: dict[str, Any], sidecar_path: Path) -> list[LintIssue]:
    """Evaluate one parsed sidecar dict for STAT-007 unit-of-independence risks (#3094)."""
    schema_keys = _collect_schema_keys(data.get("result_schema"))
    exprs = _collect_sidecar_expressions(data)
    prose = _collect_sidecar_prose(data)
    declared_unit, waived = _extract_unit_of_independence(data, schema_keys, prose)
    if waived:
        return []

    directory = sidecar_path.parent.name or "sidecar"
    schema_joined = " ".join(sorted(schema_keys))
    expr_joined = " ".join(expr for _, expr in exprs)
    has_replica_context = bool(
        _REPLICA_UNIT_RE.search(schema_joined)
        or _REPLICA_UNIT_RE.search(expr_joined)
        or _REPLICA_PROSE_RE.search(prose)
    )

    issues: list[LintIssue] = []

    # 1. Per-replica convergence gates
    conv_hit = _PER_REPLICA_CONVERGENCE_RE.search(schema_joined) or _PER_REPLICA_CONVERGENCE_RE.search(
        expr_joined
    )
    has_pooled_unit = bool(declared_unit) and declared_unit.lower() not in {
        "replica",
        "replicas",
        "seed",
        "seeds",
    }
    has_pooled_conv_expr = has_pooled_unit and any(
        _POOLED_CONV_METRIC_RE.search(expr) for _, expr in exprs
    )
    expr_generic_conv_hit = any(
        _GENERIC_CONVERGED_RE.search(expr)
        and not (has_pooled_unit and _POOLED_CONV_METRIC_RE.search(expr))
        for _, expr in exprs
    )
    schema_generic_conv_hit = (not has_pooled_conv_expr) and any(
        _GENERIC_CONVERGED_RE.search(k) and "pooled" not in k.lower() for k in schema_keys
    )
    generic_conv_hit = bool(
        has_replica_context and (expr_generic_conv_hit or schema_generic_conv_hit)
    )
    if conv_hit or generic_conv_hit or _has_unnegated_prose_match(prose, _PER_REPLICA_CONVERGENCE_PROSE_RE):
        issues.append(
            LintIssue(
                path=sidecar_path,
                directory=directory,
                issue="per_replica_convergence_gate",
                severity=IssueSeverity.WARNING,
                detail=(
                    "sidecar gates on per-replica convergence; simulation replicates are partial "
                    "samples of one ensemble and should be pooled rather than gated individually "
                    "(rule STAT-007)"
                ),
            )
        )

    # 2. Replica-split nulls
    if (
        _REPLICA_SPLIT_NULL_RE.search(schema_joined)
        or _REPLICA_SPLIT_NULL_RE.search(expr_joined)
        or _has_unnegated_prose_match(prose, _REPLICA_SPLIT_NULL_PROSE_RE)
    ):
        issues.append(
            LintIssue(
                path=sidecar_path,
                directory=directory,
                issue="replica_split_null",
                severity=IssueSeverity.WARNING,
                detail=(
                    "sidecar uses a replica-split / within-replica shuffle null; pool exchangeable "
                    "replicas at the true unit of independence or waive with non-exchangeability "
                    "reason (rule STAT-007)"
                ),
            )
        )

    # 3. Replica-spread error bars / seeds treated as n
    has_pooled_se_and_unit = bool(
        declared_unit
        and any("pooled_se" in k.lower() or "block_bootstrap" in k.lower() for k in schema_keys)
        and not _REPLICA_SPREAD_RE.search(expr_joined)
    )
    spread_hit = _REPLICA_SPREAD_RE.search(expr_joined) or (
        _REPLICA_SPREAD_RE.search(schema_joined) and not has_pooled_se_and_unit
    )
    seeds_as_n = bool(
        _SEEDS_AS_N_RE.search(expr_joined)
        and declared_unit.lower() in {"", "seed", "seeds", "replica", "replicas"}
    )
    if spread_hit or seeds_as_n:
        issues.append(
            LintIssue(
                path=sidecar_path,
                directory=directory,
                issue="replica_spread_error_bar",
                severity=IssueSeverity.WARNING,
                detail=(
                    "sidecar uses between-replica/seed spread or seed count as uncertainty/n; "
                    "estimate uncertainty via pooled estimator / block bootstrap at the true "
                    "unit of independence (rule STAT-007)"
                ),
            )
        )

    # 4. Undeclared or non-independent unit when replica/seed units are present
    if has_replica_context and not declared_unit:
        issues.append(
            LintIssue(
                path=sidecar_path,
                directory=directory,
                issue="missing_unit_of_independence",
                severity=IssueSeverity.WARNING,
                detail=(
                    "replica/seed units are present without a declared unit of independence; "
                    "declare [breakdown] unit = '<unit>' (e.g. 'build', 'system') or "
                    "[independence] unit / waive (rule STAT-007)"
                ),
            )
        )
    elif (
        has_replica_context
        and declared_unit.lower() in {"replica", "replicas", "seed", "seeds"}
        and not _non_exchangeable_justified(data, prose)
    ):
        issues.append(
            LintIssue(
                path=sidecar_path,
                directory=directory,
                issue="non_independent_unit",
                severity=IssueSeverity.WARNING,
                detail=(
                    f"declared unit {declared_unit!r} treats replicas/seeds as independent units "
                    "without stating non-exchangeable starting structures (rule STAT-007)"
                ),
            )
        )

    return issues


def check_unit_of_independence(project_root: Path) -> list[LintIssue]:
    """Tier-2 lint for per-replica convergence gates, replica-split nulls, and replica-spread error bars (#3094)."""
    issues: list[LintIssue] = []
    for sidecar_path, data in _iter_parsed_toml_sidecars(project_root):
        issues.extend(assess_unit_of_independence_sidecar(data, sidecar_path))
    return issues


# ── #3095: Source/batch confound check ───────────────────────────────────────

_PROJECTION_OR_CLF_RE = re.compile(
    r"\b(?:pc1|pc2|pca|umap|t_?sne|t-sne|projection|embedding|reduced_space|"
    r"latent_axis|leading_axis|main_axis|origin_collapse|"
    r"auroc|roc_auc|pr_auc|auprc|routing_auroc|classifier|classification)\b",
    re.IGNORECASE,
)
_COVARIATE_RE = re.compile(
    r"\b(?:data_source|source_id|dataset_id|dataset_identity|source_batch|"
    r"batch_id|batch|coverage|missing_residues|pdb_vs_md|sim_vs_pdb|origin_collapse|"
    r"within_source\w*|within_batch\w*|per_source\w*|per_batch\w*|"
    r"pc1_source\w*|pc1_batch\w*|pc1_coverage\w*|axis_source\w*|axis_batch\w*|axis_coverage\w*|"
    r"source_r2|batch_r2|coverage_r2|source_auroc|batch_auroc|sources?)\b",
    re.IGNORECASE,
)
_COVARIATE_PROSE_RE = re.compile(
    r"\b(?:data_source|source_id|dataset_id|dataset_identity|source_batch|"
    r"batch_id|batch|coverage|missing_residues|pdb_vs_md|sim_vs_pdb|origin_collapse|"
    r"within_source\w*|within_batch\w*|per_source\w*|per_batch\w*|"
    r"pc1_source\w*|pc1_batch\w*|pc1_coverage\w*|axis_source\w*|axis_batch\w*|axis_coverage\w*|"
    r"source_r2|batch_r2|coverage_r2|source_auroc|batch_auroc|"
    r"data\s+sources?|multiple\s+sources?|different\s+sources?|across\s+sources?|"
    r"between\s+sources?|source[- ]specific|"
    r"source\s+(?:confound\w*|bias\w*|effect\w*|leakage|label\w*|group\w*|shift\w*|stratifi\w*))\b",
    re.IGNORECASE,
)
_CONFOUND_DIAGNOSTIC_COL_RE = re.compile(
    r"^(?:pc\d+_(?:source|batch|coverage)\w*|axis_(?:source|batch|coverage)\w*|"
    r"(?:source|batch|coverage)_(?:r2|corr|auroc|variance)|"
    r"within_(?:source|batch)_\w+|stratified_\w+|origin_collapse\w*)$",
    re.IGNORECASE,
)
_STRATIFIED_OR_CONFOUND_MITIGATION_RE = re.compile(
    r"\b(?:within_source\w*|within_batch\w*|stratified\w*|per_source\w*|per_batch\w*|"
    r"batch_corrected\w*|harmonized\w*|pc\d+_(?:source|batch|coverage)\w*|"
    r"axis_(?:source|batch|coverage)\w*|origin_collapse\w*|source_auroc|batch_auroc)\b",
    re.IGNORECASE,
)


def assess_source_batch_confound_sidecar(
    data: dict[str, Any], sidecar_path: Path
) -> list[LintIssue]:
    """Evaluate one parsed sidecar dict for STAT-008 source/batch/coverage confounds (#3095)."""
    confounds_tbl = data.get("confounds")
    if isinstance(confounds_tbl, dict):
        waive = confounds_tbl.get("waive")
        if isinstance(waive, str) and waive.strip():
            return []
    breakdown_tbl = data.get("breakdown")
    if isinstance(breakdown_tbl, dict) and _breakdown_waives_rule(
        breakdown_tbl, _STAT008_WAIVE_RE, "waive_confounds"
    ):
        return []

    schema_keys = _collect_schema_keys(data.get("result_schema"))
    exprs = _collect_sidecar_expressions(data)
    prose = _collect_sidecar_prose(data)
    factors = data.get("factors")
    factor_keys = (
        set(factors["declared_levels"].keys())
        if isinstance(factors, dict) and isinstance(factors.get("declared_levels"), dict)
        else set()
    )

    schema_joined = " ".join(sorted(schema_keys))
    expr_joined = " ".join(expr for _, expr in exprs)
    factor_joined = " ".join(sorted(str(k) for k in factor_keys))

    has_proj_or_clf = bool(
        _PROJECTION_OR_CLF_RE.search(schema_joined)
        or _PROJECTION_OR_CLF_RE.search(expr_joined)
        or _PROJECTION_OR_CLF_RE.search(prose)
    )
    if not has_proj_or_clf:
        return []

    has_confounds_tbl = isinstance(confounds_tbl, dict) and bool(confounds_tbl)
    has_covariates = bool(
        has_confounds_tbl
        or _COVARIATE_RE.search(schema_joined)
        or _COVARIATE_RE.search(expr_joined)
        or _COVARIATE_RE.search(factor_joined)
        or _COVARIATE_PROSE_RE.search(prose)
    )
    if not has_covariates:
        return []

    directory = sidecar_path.parent.name or "sidecar"
    issues: list[LintIssue] = []

    # 1. Any confound diagnostic declared in result_schema must be gated in outcomes/invariants/sanity
    diag_cols = sorted(k for k in schema_keys if _CONFOUND_DIAGNOSTIC_COL_RE.match(k))
    if diag_cols:
        referenced_in_gates: set[str] = set()
        for _, expr in exprs:
            referenced_in_gates.update(columns_referenced(expr, diag_cols))
        ungated = [c for c in diag_cols if c not in referenced_in_gates]
        if ungated:
            issues.append(
                LintIssue(
                    path=sidecar_path,
                    directory=directory,
                    issue="confound_diagnostic_ungated",
                    severity=IssueSeverity.WARNING,
                    detail=(
                        f"[result_schema] declares confound/stratification metric(s) {ungated} "
                        "that are never gated in [outcomes], [invariants], or [sanity] "
                        "(rule STAT-008)"
                    ),
                )
            )

    # 2. Projection or classifier with source/batch/coverage covariates must stratify or bound axis confounds
    confound_adjusted = isinstance(confounds_tbl, dict) and bool(
        confounds_tbl.get("stratified_by") or confounds_tbl.get("covariate_adjusted")
    )
    has_mitigation = bool(
        confound_adjusted
        or _STRATIFIED_OR_CONFOUND_MITIGATION_RE.search(expr_joined)
        or _STRATIFIED_OR_CONFOUND_MITIGATION_RE.search(schema_joined)
    )
    if not has_mitigation:
        issues.append(
            LintIssue(
                path=sidecar_path,
                directory=directory,
                issue="unstratified_projection_or_classifier",
                severity=IssueSeverity.WARNING,
                detail=(
                    "projection/classifier sidecar declares source, batch, or coverage context "
                    "without within-stratum metrics (e.g. within_source_auroc) or axis-confound "
                    "gates (e.g. pc1_source_r2, origin_collapse_fraction) (rule STAT-008)"
                ),
            )
        )

    return issues


def check_source_batch_confound(project_root: Path) -> list[LintIssue]:
    """Tier-2 lint for projections or classifiers tracking data source, batch, or coverage (#3095)."""
    issues: list[LintIssue] = []
    for sidecar_path, data in _iter_parsed_toml_sidecars(project_root):
        issues.extend(assess_source_batch_confound_sidecar(data, sidecar_path))
    return issues


def _validate_finite_floats(values: Sequence[float], name: str) -> None:
    for idx, val in enumerate(values):
        if isinstance(val, bool):
            raise ValueError(f"{name}[{idx}] must be a finite number, got {val!r}")
        try:
            finite = math.isfinite(val)
        except TypeError as exc:
            raise ValueError(f"{name}[{idx}] must be a finite number, got {val!r}") from exc
        if not finite:
            raise ValueError(f"{name}[{idx}] must be a finite number, got {val!r}")


def _categorical_eta_squared(values: Sequence[float], labels: Sequence[str]) -> float | None:
    if len(values) != len(labels):
        raise ValueError("values and labels length must match")
    _validate_finite_floats(values, "values")
    if len(values) < 2:
        return None
    mean_all = sum(values) / len(values)
    ss_total = sum((v - mean_all) ** 2 for v in values)
    if ss_total <= 0.0:
        return 0.0
    groups: dict[str, list[float]] = {}
    for v, lbl in zip(values, labels):
        groups.setdefault(lbl, []).append(v)
    if len(groups) < 2:
        return 0.0
    ss_between = sum(len(g) * ((sum(g) / len(g)) - mean_all) ** 2 for g in groups.values())
    return min(1.0, max(0.0, ss_between / ss_total))


def _pearson_r2(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys):
        raise ValueError("xs and ys length must match")
    _validate_finite_floats(xs, "xs")
    _validate_finite_floats(ys, "ys")
    if len(xs) < 2:
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    r = sxy / math.sqrt(sxx * syy)
    return min(1.0, max(0.0, r * r))


def diagnose_projection_axis_confound(
    axis_values: Sequence[float],
    *,
    source: Sequence[str] | None = None,
    batch: Sequence[str] | None = None,
    coverage: Sequence[float] | None = None,
    secondary_axis: Sequence[float] | None = None,
    r2_threshold: float = 0.25,
    origin_eps: float = 1e-3,
    origin_collapse_fraction: float = 0.8,
) -> dict[str, Any]:
    """Diagnose whether a projection axis or classifier score tracks source, batch, or coverage (#3095)."""
    _validate_finite_floats(axis_values, "axis_values")
    if secondary_axis is not None:
        if len(secondary_axis) != len(axis_values):
            raise ValueError("secondary_axis length must match axis_values length")
        _validate_finite_floats(secondary_axis, "secondary_axis")
    if source is not None and len(source) != len(axis_values):
        raise ValueError("source length must match axis_values length")
    if batch is not None and len(batch) != len(axis_values):
        raise ValueError("batch length must match axis_values length")
    if coverage is not None:
        if len(coverage) != len(axis_values):
            raise ValueError("coverage length must match axis_values length")
        _validate_finite_floats(coverage, "coverage")

    reasons: list[str] = []
    source_r2 = _categorical_eta_squared(axis_values, source) if source is not None else None
    batch_r2 = _categorical_eta_squared(axis_values, batch) if batch is not None else None
    coverage_r2 = _pearson_r2(axis_values, coverage) if coverage is not None else None

    if source_r2 is not None and source_r2 >= r2_threshold:
        reasons.append(f"main axis tracks data source (R^2={source_r2:.3f} >= {r2_threshold:.3f})")
    if batch_r2 is not None and batch_r2 >= r2_threshold:
        reasons.append(f"main axis tracks batch (R^2={batch_r2:.3f} >= {r2_threshold:.3f})")
    if coverage_r2 is not None and coverage_r2 >= r2_threshold:
        reasons.append(f"main axis tracks coverage (R^2={coverage_r2:.3f} >= {r2_threshold:.3f})")

    collapsed_groups: list[str] = []
    if axis_values:
        mean_all = sum(axis_values) / len(axis_values)
        spread_all = math.sqrt(sum((v - mean_all) ** 2 for v in axis_values) / len(axis_values))
        if spread_all > origin_eps:
            for cov_name, labels in (("source", source), ("batch", batch)):
                if labels is None or len(labels) != len(axis_values):
                    continue
                grouped_idx: dict[str, list[int]] = {}
                for idx, lbl in enumerate(labels):
                    grouped_idx.setdefault(lbl, []).append(idx)
                for lbl, idxs in sorted(grouped_idx.items()):
                    if not idxs:
                        continue
                    near_zero = 0
                    for idx in idxs:
                        dist = (
                            math.hypot(axis_values[idx], secondary_axis[idx])
                            if secondary_axis is not None
                            else abs(axis_values[idx])
                        )
                        if dist <= origin_eps:
                            near_zero += 1
                    frac = near_zero / len(idxs)
                    if frac >= origin_collapse_fraction:
                        tag = f"{cov_name}:{lbl}"
                        collapsed_groups.append(tag)
                        reasons.append(
                            f"subset {tag!r} collapsed to origin ({near_zero}/{len(idxs)} <= {origin_eps})"
                        )

    return {
        "confounded": bool(reasons),
        "reasons": reasons,
        "source_r2": source_r2,
        "batch_r2": batch_r2,
        "coverage_r2": coverage_r2,
        "collapsed_groups": collapsed_groups,
    }


# ── #3099: Correction sweep ──────────────────────────────────────────────────


def check_correction_sweep(project_root: Path) -> list[LintIssue]:
    """Sweep active sidecars, claims, and specs for stale assumptions from landed corrections (#3099)."""
    from bathos.correction_sweep import sweep_corrections

    report = sweep_corrections(project_root)
    severity_map = {
        "error": IssueSeverity.ERROR,
        "warning": IssueSeverity.WARNING,
        "info": IssueSeverity.INFO,
    }
    issues: list[LintIssue] = []
    for err in report.errors:
        issues.append(
            LintIssue(
                path=project_root / ".bth",
                directory="corrections",
                issue="invalid_correction_registry",
                severity=IssueSeverity.WARNING,
                detail=err,
            )
        )
    for hit in report.hits:
        hint = f" — replace with: {hit.replacement}" if hit.replacement else ""
        issues.append(
            LintIssue(
                path=hit.artifact_path,
                directory=hit.artifact_kind,
                issue="stale_correction_assumption",
                severity=severity_map.get(hit.severity, IssueSeverity.WARNING),
                detail=(
                    f"[{hit.correction_id}] stale assumption {hit.matched_term!r} at "
                    f"{hit.location} ({hit.correction_summary}){hint}"
                ),
            )
        )
    return issues
