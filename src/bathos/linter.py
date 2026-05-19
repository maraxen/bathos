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
            if script.name.startswith(".") or script.is_dir():
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
