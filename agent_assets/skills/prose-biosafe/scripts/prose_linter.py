"""prose_linter.py — Python prose linter for classifier-safe terminology.

Detects three classes of issues in biology-context prose:

  1. TERM     — single flagged term (e.g. "gain-of-function")
  2. COMPOUND — two trigger terms within a configurable line window
  3. DENSITY  — flagged-term density above threshold (prioritizes files
                 where many small issues co-occur — the real classifier target)

Usage (standalone):
    python scripts/prose_linter.py [PATH] [--window N] [--density-threshold F]

Usage (as library):
    from scripts.prose_linter import lint_path, ProseIssue

Integrates with bathos linter.py via check_prose_biosafe(project_root).
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterator


# ── enums & data classes ──────────────────────────────────────────────────────

class IssueKind(str, Enum):
    TERM = "term"          # single flagged token
    COMPOUND = "compound"  # two trigger terms within proximity window
    DENSITY = "density"    # high flagged-term density across a file


@dataclass
class ProseIssue:
    path: Path
    line: int               # 1-indexed; 0 = file-level (for DENSITY)
    col: int                # 1-indexed; 0 if not applicable
    kind: IssueKind
    rule_id: str            # e.g. "dead-bio", "gain-of-function", "compound:virus+design"
    matched: str            # the actual text that triggered
    suggestion: str         # preferred replacement
    context: str = ""       # surrounding line text (truncated)

    def __str__(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line else str(self.path)
        return (
            f"[{self.kind.value.upper()}] {loc}  rule={self.rule_id}\n"
            f"  matched: {self.matched!r}\n"
            f"  suggest: {self.suggestion}\n"
            f"  context: {self.context!r}"
        )


# ── rule registry ─────────────────────────────────────────────────────────────
# Each TERM rule: (rule_id, pattern, suggestion, allowlist_pattern | None)
# Patterns are case-insensitive. allowlist_pattern exempts matching lines.

@dataclass
class TermRule:
    rule_id: str
    pattern: re.Pattern
    suggestion: str
    allowlist: re.Pattern | None = None   # lines matching this are skipped


@dataclass
class CompoundRule:
    rule_id: str
    pattern_a: re.Pattern
    pattern_b: re.Pattern
    suggestion: str
    allowlist: re.Pattern | None = None
    # optional: if BOTH need to be in window AND a third anchor present
    anchor: re.Pattern | None = None


def _ci(s: str) -> re.Pattern:
    return re.compile(s, re.IGNORECASE)


def _allow(*patterns: str) -> re.Pattern:
    return re.compile("|".join(patterns), re.IGNORECASE)


TERM_RULES: list[TermRule] = [
    TermRule(
        rule_id="dead-bio",
        pattern=_ci(
            r"\bdead\s+(control|variant|construct|protease|enzyme)"
            r"|catalytically\s+dead"
            r"|parent-dead"
        ),
        suggestion='Use "catalytically inactive control/variant" or "negative control"',
        allowlist=_allow(
            r"dead code", r"dead gitlink", r"dead.end", r"dead knob",
            r"confirmed.dead", r"write.only.dead", r"deadline",
        ),
    ),
    TermRule(
        rule_id="bio-variant-term",
        pattern=_ci(r"\bmutant\b"),
        suggestion='Use "variant" (modern genetics style; "mutant" is deprecated in major journals)',
        allowlist=_allow(
            r"double-mutant", r"single-mutant", r"mutant.coverage", r"mutant_coverage",
        ),
    ),
    TermRule(
        rule_id="neo-substrate-framing",
        pattern=_ci(r"\bgain[- ]of[- ]function\b"),
        suggestion='Use "neo-functional" or "neo-substrate" (describes the actual assay axis)',
    ),
    TermRule(
        rule_id="evolution-verb",
        pattern=_ci(r"\bout.evolv"),
        suggestion='Use "outpaced" or "out-competed"',
    ),
    TermRule(
        rule_id="viral-origin",
        pattern=_ci(r"tobacco\s+etch\s+virus|TEV\s*[\./,]?\s*virus|virus\s*[\./,]?\s*TEV"),
        suggestion='Omit viral origin; use "EC 3.4.22.44" or just "TEV protease"',
        allowlist=_allow(r"bibliography", r"citation", r"doi\.org", r"10\.\d{4}", r"et al"),
    ),
    TermRule(
        rule_id="structural-perturbation",
        pattern=_ci(r"\bdestabili[sz]"),
        suggestion='Use "perturbed" or "structurally modified"',
    ),
    TermRule(
        rule_id="specificity-framing",
        pattern=_ci(r"\benhance\b.{0,40}\bactivit(?:y|ies)\b.{0,80}\bprotease\b"
                    r"|\bprotease\b.{0,80}\benhance\b.{0,40}\bactivit(?:y|ies)\b"),
        suggestion='Use "broaden specificity" or "shift substrate scope"',
    ),
    TermRule(
        rule_id="perturbation-scale",
        pattern=_ci(
            r"(dose|dosing)\s+(series|scale).*substitut"
            r"|substitut.*(dose|dosing)\s+(series|scale)"
            r"|core\s+damag"
        ),
        suggestion='Use "designed-modification series" or "internal perturbation"',
        allowlist=_allow(r"dosing.*drug", r"dose.*response.*drug"),
    ),
    TermRule(
        rule_id="directed-evolution-framing",
        pattern=_ci(r"\bdirected\s+evolution\b"),
        suggestion='Use "continuous selection" or the specific platform name (PACE/PRANCE)',
        allowlist=_allow(
            r"bibliography", r"citation", r"doi\.org", r"10\.\d{4}", r"et al",
            r"Esvelt", r"Dickinson", r"Packer", r"DeBenedictis", r"Badran",
            r"^\s*>",   # blockquote — direct quote from paper
            r"^\s*\|",  # table cell — likely bibliography
        ),
    ),
    TermRule(
        rule_id="substrate-scope-framing",
        pattern=_ci(r"\bhost\s+range\b"),
        suggestion='Use "substrate scope" or "AP specificity" in enzyme-design context',
        allowlist=_allow(r"phage.*host.*biology", r"infection.*host.*range"),
    ),
    TermRule(
        rule_id="catalytic-efficiency",
        pattern=_ci(r"\bpotency\b"),
        suggestion='Use "catalytic efficiency" or "kcat/Km" — "potency" is pharmacology register',
        allowlist=_allow(r"drug.*potency", r"inhibitor.*potency"),
    ),
]


# ── compound rules ────────────────────────────────────────────────────────────
# Fires when pattern_a and pattern_b both appear within WINDOW lines of each other.

COMPOUND_RULES: list[CompoundRule] = [
    CompoundRule(
        rule_id="compound:viral-origin+design",
        pattern_a=_ci(r"\bvirus\b"),
        pattern_b=_ci(r"\b(design|engineer|modif)\b"),
        suggestion='Omit viral origin; describe the enzyme by EC number or function',
    ),
    CompoundRule(
        rule_id="compound:neo-substrate+protease",
        pattern_a=_ci(r"\bgain[- ]of[- ]function\b"),
        pattern_b=_ci(r"\bprotease\b"),
        suggestion='Replace "gain-of-function" with "neo-substrate" or "shifted specificity"',
    ),
    CompoundRule(
        rule_id="compound:evolution+protease+select",
        pattern_a=_ci(r"\b(direct|continu).{0,20}(evolut|select)\b"),
        pattern_b=_ci(r"\bprotease\b"),
        suggestion='Frame as "continuous selection of [enzyme] variants" specifying the engineering platform',
        allowlist=_allow(r"bibliography", r"citation", r"doi\.org", r"10\.\d{4}", r"et al"),
    ),
    CompoundRule(
        rule_id="compound:bio-variant-term+activity",
        pattern_a=_ci(r"\bmutant\b"),
        pattern_b=_ci(r"\bactivit(?:y|ies)\b"),
        suggestion='Replace "mutant" with "variant" — mutant+activity is core gain-of-function language',
        allowlist=_allow(r"double-mutant", r"single-mutant"),
    ),
    CompoundRule(
        rule_id="compound:structural-perturbation+core",
        pattern_a=_ci(r"\bdestabili[sz]"),
        pattern_b=_ci(r"\b(core|buried|scaffold|active.site)\b"),
        suggestion='Use "core_perturbed" or "structural-core modified"',
    ),
]


# ── file selection ─────────────────────────────────────────────────────────────

_SCAN_SUFFIXES = {".md", ".py", ".toml", ".rst", ".txt"}

_SKIP_DIRS = {
    ".git", ".venv", "__pycache__", "node_modules",
    ".ruff_cache", ".pytest_cache", ".fastembed_cache",
    "dist", "wheels",
}

# Paths whose content is exclusively bibliographies / direct quotes — scan but
# apply allowlists aggressively; don't skip entirely (density matters).
_QUOTE_HEAVY_GLOBS = ["*bibliography*", "*reference*", "*literature*"]


def _is_allowlisted(line: str, rule: TermRule) -> bool:
    if rule.allowlist is None:
        return False
    return bool(rule.allowlist.search(line))


def _iter_files(root: Path) -> Iterator[Path]:
    for p in root.rglob("*"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.is_file() and p.suffix in _SCAN_SUFFIXES:
            yield p


# ── core lint logic ────────────────────────────────────────────────────────────

def lint_file(
    path: Path,
    window: int = 6,
    density_threshold: float = 0.03,
) -> list[ProseIssue]:
    """Lint a single file for prose issues.

    Args:
        path: File to lint.
        window: Number of lines to search for compound co-occurrence.
        density_threshold: Fraction of lines with any flag above which
            a DENSITY issue fires (0.03 = 3%).

    Returns:
        List of ProseIssue, sorted by line number.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []

    lines = text.splitlines()
    issues: list[ProseIssue] = []
    flagged_lines: set[int] = set()  # 0-indexed lines that have any term hit

    # ── pass 1: TERM rules ───────────────────────────────────────────────────
    for rule in TERM_RULES:
        for lineno, raw in enumerate(lines):
            if _is_allowlisted(raw, rule):
                continue
            for m in rule.pattern.finditer(raw):
                flagged_lines.add(lineno)
                issues.append(ProseIssue(
                    path=path,
                    line=lineno + 1,
                    col=m.start() + 1,
                    kind=IssueKind.TERM,
                    rule_id=rule.rule_id,
                    matched=m.group(0),
                    suggestion=rule.suggestion,
                    context=raw.strip()[:120],
                ))

    # ── pass 2: COMPOUND rules (sliding window) ───────────────────────────────
    for crule in COMPOUND_RULES:
        # Collect line indices where each pattern fires
        lines_a: list[int] = []
        lines_b: list[int] = []
        for i, raw in enumerate(lines):
            if crule.pattern_a.search(raw):
                lines_a.append(i)
            if crule.pattern_b.search(raw):
                lines_b.append(i)

        # For every a-hit, check if any b-hit is within [i-window, i+window]
        bi = 0
        for ai in lines_a:
            # advance bi past lines that are too early
            while bi < len(lines_b) and lines_b[bi] < ai - window:
                bi += 1
            for bj in range(bi, len(lines_b)):
                if lines_b[bj] > ai + window:
                    break
                # Check allowlist if any
                anchor_line = lines[ai]
                if crule.allowlist is not None and crule.allowlist.search(anchor_line):
                    continue

                flagged_lines.add(ai)
                issues.append(ProseIssue(
                    path=path,
                    line=ai + 1,
                    col=0,
                    kind=IssueKind.COMPOUND,
                    rule_id=crule.rule_id,
                    matched=f"line {ai+1} + line {lines_b[bj]+1}",
                    suggestion=crule.suggestion,
                    context=lines[ai].strip()[:120],
                ))
                break  # one compound issue per a-hit is enough

    # ── pass 3: DENSITY ────────────────────────────────────────────────────────
    if lines:
        density = len(flagged_lines) / len(lines)
        if density >= density_threshold:
            issues.append(ProseIssue(
                path=path,
                line=0,
                col=0,
                kind=IssueKind.DENSITY,
                rule_id="density",
                matched=f"{len(flagged_lines)} flagged lines / {len(lines)} total ({density:.1%})",
                suggestion=(
                    "High flagged-term density — prioritize this file for a "
                    "terminology pass; co-occurrence in dense context is the "
                    "primary classifier trigger"
                ),
                context="",
            ))

    issues.sort(key=lambda x: (x.line, x.kind.value))
    return issues


def lint_path(
    root: Path,
    window: int = 6,
    density_threshold: float = 0.03,
    skip_worktrees: bool = True,
) -> list[ProseIssue]:
    """Lint all eligible files under root.

    Args:
        root: Directory to scan (or a single file).
        window: Proximity window for COMPOUND rules (lines).
        density_threshold: DENSITY trigger fraction.
        skip_worktrees: If True, skip .claude/worktrees/ subtrees.

    Returns:
        All ProseIssue objects across the tree, sorted by (path, line).
    """
    if root.is_file():
        return lint_file(root, window=window, density_threshold=density_threshold)

    all_issues: list[ProseIssue] = []
    for p in _iter_files(root):
        if skip_worktrees and ".claude/worktrees" in str(p):
            continue
        all_issues.extend(lint_file(p, window=window, density_threshold=density_threshold))

    all_issues.sort(key=lambda x: (str(x.path), x.line))
    return all_issues


# ── bathos integration ─────────────────────────────────────────────────────────

def check_prose_biosafe(
    project_root: Path,
    window: int = 6,
    density_threshold: float = 0.03,
) -> list:
    """bathos linter.py integration point.

    Returns a list of bathos LintIssue objects compatible with bathos.linter.
    Import this function from bathos/linter.py to wire it in.

    Example in bathos/linter.py::

        from scripts.prose_linter import check_prose_biosafe
        issues.extend(check_prose_biosafe(project_root))
    """
    try:
        from bathos.linter import IssueSeverity, LintIssue
    except ImportError:
        return []

    prose_issues = lint_path(project_root, window=window, density_threshold=density_threshold)
    result = []
    for pi in prose_issues:
        severity = IssueSeverity.WARNING
        if pi.kind == IssueKind.DENSITY:
            severity = IssueSeverity.INFO
        if pi.rule_id in ("neo-substrate-framing", "compound:neo-substrate+protease"):
            severity = IssueSeverity.ERROR  # highest-risk rule → escalate

        loc = f":{pi.line}" if pi.line else ""
        result.append(LintIssue(
            path=pi.path,
            directory=str(pi.path.parent.relative_to(project_root)),
            issue=pi.rule_id,
            severity=severity,
            detail=(
                f"matched={pi.matched!r}  "
                f"suggest: {pi.suggestion}  "
                f"context: {pi.context[:80]!r}"
            ),
        ))
    return result


# ── CLI ────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Prose linter: detect classifier-adjacent terminology in biology-context text.\n"
            "Reports TERM (single-flag), COMPOUND (proximity co-occurrence), "
            "and DENSITY (high flag density) issues."
        )
    )
    p.add_argument("path", nargs="?", default=".", help="File or directory to scan (default: cwd)")
    p.add_argument("--window", type=int, default=6,
                   help="Proximity window for COMPOUND detection (lines, default 6)")
    p.add_argument("--density-threshold", type=float, default=0.03,
                   help="Flagged-line fraction threshold for DENSITY issues (default 0.03)")
    p.add_argument("--summary", action="store_true",
                   help="Print per-file density summary table instead of full issue list")
    p.add_argument("--no-skip-worktrees", action="store_true",
                   help="Also scan .claude/worktrees/ subtrees")
    p.add_argument("--only-density", action="store_true",
                   help="Only emit DENSITY issues — useful for quick prioritization")
    return p


def _print_summary(issues: list[ProseIssue]) -> None:
    from collections import Counter, defaultdict

    by_file: dict[Path, list[ProseIssue]] = defaultdict(list)
    for iss in issues:
        by_file[iss.path].append(iss)

    # Sort by density (DENSITY issue score) descending, then by term-count
    def sort_key(items: list[ProseIssue]):
        density_issues = [i for i in items if i.kind == IssueKind.DENSITY]
        return (
            -float(density_issues[0].matched.split("(")[1].rstrip("%)").strip()) if density_issues else 0,
            -len(items),
        )

    print(f"\n{'FILE':<60} {'TERMS':>6} {'COMPOUND':>8} {'DENSITY':>8}")
    print("─" * 86)
    for path, file_issues in sorted(by_file.items(), key=lambda kv: sort_key(kv[1])):
        terms = sum(1 for i in file_issues if i.kind == IssueKind.TERM)
        compounds = sum(1 for i in file_issues if i.kind == IssueKind.COMPOUND)
        density_str = ""
        for i in file_issues:
            if i.kind == IssueKind.DENSITY:
                density_str = i.matched.split("(")[1].rstrip(")")
                break
        print(f"{str(path):<60} {terms:>6} {compounds:>8} {density_str:>8}")


def main() -> int:
    args = _build_parser().parse_args()
    root = Path(args.path)

    issues = lint_path(
        root,
        window=args.window,
        density_threshold=args.density_threshold,
        skip_worktrees=not args.no_skip_worktrees,
    )

    if args.only_density:
        issues = [i for i in issues if i.kind == IssueKind.DENSITY]

    if args.summary:
        _print_summary(issues)
    else:
        for iss in issues:
            print(iss)
            print()

    if not issues:
        print("✓ No classifier-adjacent prose issues found.")
        return 0

    term_count = sum(1 for i in issues if i.kind == IssueKind.TERM)
    compound_count = sum(1 for i in issues if i.kind == IssueKind.COMPOUND)
    density_count = sum(1 for i in issues if i.kind == IssueKind.DENSITY)
    print(
        f"\n✗ {len(issues)} total issues  "
        f"({term_count} term, {compound_count} compound, {density_count} density)"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
