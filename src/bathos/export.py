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


_MCP_ENTRY = {
    "command": "uv",
    "args": ["run", "--with", "bathos[mcp]", "bth-mcp"],
}


def _claude_mcp_path(level: str) -> Path:
    paths = {
        "user": Path.home() / ".claude" / "mcp.json",
        "workspace": Path(".mcp.json"),
        "system": Path("/etc/claude/mcp.json"),
    }
    if level not in paths:
        raise ExportError(f"Unknown level: {level!r}")
    return paths[level]


def _gemini_settings_path(level: str) -> Path:
    paths = {
        "user": Path.home() / ".gemini" / "settings.json",
        "workspace": Path(".gemini") / "settings.json",
        "system": Path("/etc/gemini/settings.json"),
    }
    if level not in paths:
        raise ExportError(f"Unknown level: {level!r}")
    return paths[level]


def register_mcp(tool: str, level: str, dry_run: bool) -> Path:
    """Merge mcpServers.bathos into the tool's MCP config file."""
    import json

    if tool == "claude":
        target = _claude_mcp_path(level)
    elif tool == "gemini":
        target = _gemini_settings_path(level)
    else:
        raise ExportError(f"Unknown tool: {tool!r}")

    if dry_run:
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {"mcpServers": {}}
    if target.exists():
        try:
            data = json.loads(target.read_text())
        except json.JSONDecodeError:
            data = {"mcpServers": {}}
    data.setdefault("mcpServers", {})["bathos"] = _MCP_ENTRY
    target.write_text(json.dumps(data, indent=2))
    return target


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
