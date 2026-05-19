from __future__ import annotations

import importlib.resources
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path

import bathos


class ExportError(Exception):
    pass


@dataclass
class ExportResult:
    target: Path
    written: bool
    dry_run: bool


_CLAUDE_TARGETS: dict[str, Path] = {
    "user": Path.home() / ".claude" / "skills" / "using-bathos.md",
    "workspace": Path(".claude") / "skills" / "using-bathos.md",
    "system": Path("/etc/claude/skills/using-bathos.md"),
}

_GEMINI_TARGETS: dict[str, Path] = {
    "user": Path.home() / ".gemini" / "skills" / "using-bathos.md",
    "workspace": Path(".gemini") / "skills" / "using-bathos.md",
    "system": Path("/etc/gemini/skills/using-bathos.md"),
}


def resolve_target(tool: str, level: str) -> Path:
    if tool == "claude":
        targets = _CLAUDE_TARGETS
    elif tool == "gemini":
        targets = _GEMINI_TARGETS
    else:
        raise ExportError(f"Unknown tool: {tool!r}. Choose 'claude' or 'gemini'.")

    if level not in targets:
        raise ExportError(f"Unknown level: {level!r}. Choose 'user', 'workspace', or 'system'.")

    return targets[level]


def get_skill_source_path() -> Path:
    """Locate agent_assets/using_bathos/SKILL.md relative to the bathos package."""
    # Try repo layout first (editable install)
    package_dir = Path(bathos.__file__).parent
    candidate = package_dir.parent.parent / "agent_assets" / "using_bathos" / "SKILL.md"
    if candidate.exists():
        return candidate
    # Fallback: installed package data
    try:
        ref = importlib.resources.files("bathos") / "agent_assets" / "using_bathos" / "SKILL.md"
        with importlib.resources.as_file(ref) as p:
            if p.exists():
                return p
    except Exception:
        pass
    raise ExportError(
        "Could not locate agent_assets/using_bathos/SKILL.md. "
        "Ensure bathos is installed in editable mode or package data is complete."
    )


def export_skill(target: Path, dry_run: bool) -> ExportResult:
    source = get_skill_source_path()
    version = getattr(bathos, "__version__", "unknown")
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = f"<!-- bathos v{version} | exported {timestamp} -->\n"
    content = header + source.read_text()

    if dry_run:
        return ExportResult(target=target, written=False, dry_run=True)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return ExportResult(target=target, written=True, dry_run=False)
