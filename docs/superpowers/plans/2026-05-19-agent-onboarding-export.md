# Agent Onboarding & `bth export` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `bth export` to distribute the using-bathos skill to Claude Code and Gemini CLI at user/workspace/system level, and write integration reference docs for both platforms.

**Architecture:** New `src/bathos/export.py` module handles path resolution and file copy with a version/timestamp header. The SKILL.md source is located via `importlib.resources` from the installed package's `agent_assets/` directory (or a fallback to the repo root). CLI `export` subcommand wraps this. Integration docs are static markdown files in `agent_assets/docs/`.

**Tech Stack:** Python 3.12, Typer, importlib.resources, pathlib, uv

**Independence:** This plan can execute in parallel with Plan B (historical migration). It does not depend on Plan A's sidecar feature.

---

## File Map

| Action | Path |
|--------|------|
| Create | `src/bathos/export.py` |
| Modify | `src/bathos/cli.py` |
| Create | `agent_assets/docs/claude-code-integration.md` |
| Create | `agent_assets/docs/gemini-cli-integration.md` |
| Create | `tests/test_export.py` |

---

## Task 1: Create `export.py` module

**Files:**
- Create: `src/bathos/export.py`
- Create: `tests/test_export.py`

- [ ] **Step 1.1: Write failing tests**

Create `tests/test_export.py`:

```python
from pathlib import Path
import pytest


def test_get_skill_source_path_returns_existing_file():
    from bathos.export import get_skill_source_path
    p = get_skill_source_path()
    assert p.exists(), f"Skill source not found at {p}"
    assert p.name == "SKILL.md"


def test_export_skill_writes_to_target(tmp_path):
    from bathos.export import export_skill
    target = tmp_path / "skills" / "using-bathos.md"
    result = export_skill(target=target, dry_run=False)
    assert result.written is True
    assert target.exists()
    content = target.read_text()
    assert "bathos" in content.lower()


def test_export_skill_dry_run_does_not_write(tmp_path):
    from bathos.export import export_skill
    target = tmp_path / "skills" / "using-bathos.md"
    result = export_skill(target=target, dry_run=True)
    assert result.written is False
    assert not target.exists()


def test_export_skill_stamps_version_header(tmp_path):
    from bathos.export import export_skill
    target = tmp_path / "using-bathos.md"
    export_skill(target=target, dry_run=False)
    first_line = target.read_text().splitlines()[0]
    assert first_line.startswith("<!-- bathos")


def test_resolve_target_claude_user():
    from bathos.export import resolve_target
    t = resolve_target(tool="claude", level="user")
    assert "claude" in str(t).lower()
    assert t.name == "using-bathos.md"


def test_resolve_target_gemini_workspace():
    from bathos.export import resolve_target
    t = resolve_target(tool="gemini", level="workspace")
    assert ".gemini" in str(t)
    assert t.name == "using-bathos.md"


def test_resolve_target_invalid_tool():
    from bathos.export import resolve_target, ExportError
    with pytest.raises(ExportError, match="Unknown tool"):
        resolve_target(tool="vscode", level="user")


def test_resolve_target_invalid_level():
    from bathos.export import resolve_target, ExportError
    with pytest.raises(ExportError, match="Unknown level"):
        resolve_target(tool="claude", level="global")
```

- [ ] **Step 1.2: Run tests to verify they fail**

```bash
cd /home/marielle/projects/bathos
uv run pytest tests/test_export.py -v
```

Expected: `ERROR` — `ModuleNotFoundError: No module named 'bathos.export'`

- [ ] **Step 1.3: Create `src/bathos/export.py`**

```python
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
```

- [ ] **Step 1.4: Run tests to verify they pass**

```bash
uv run pytest tests/test_export.py -v
```

Expected: `8 passed`

- [ ] **Step 1.5: Run full suite**

```bash
uv run pytest -x -q
```

Expected: all pass

- [ ] **Step 1.5a: Verify pyproject.toml includes agent_assets as package data**

```bash
grep -A5 "\[tool.hatch.build.targets.wheel\]" /home/marielle/projects/bathos/pyproject.toml
```

If `artifacts` or `include` does not list `agent_assets/`, add it. The section should read:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/bathos"]
artifacts = ["agent_assets/**"]
```

Edit `pyproject.toml` to add the `artifacts` line if missing, then commit it alongside the module.

- [ ] **Step 1.6: Commit**

```bash
git add src/bathos/export.py tests/test_export.py pyproject.toml
git commit -m "feat(export): add export_skill module for Claude Code / Gemini CLI skill distribution"
```

---

## Task 2: Add `bth export` CLI command

**Files:**
- Modify: `src/bathos/cli.py`

- [ ] **Step 2.1: Write failing CLI test**

Add to `tests/test_cli.py`:

```python
def test_export_dry_run_claude_user(runner, tmp_path, monkeypatch):
    """bth export --tool claude --level user --dry-run prints target path without writing."""
    from bathos.cli import app
    result = runner.invoke(app, ["export", "--tool", "claude", "--level", "user", "--dry-run"])
    assert result.exit_code == 0
    assert "claude" in result.output.lower()
    assert "dry-run" in result.output.lower() or "dry run" in result.output.lower()


def test_export_writes_file(runner, tmp_path, monkeypatch):
    """bth export --tool claude --level workspace writes skill to .claude/skills/."""
    monkeypatch.chdir(tmp_path)
    from bathos.cli import app
    result = runner.invoke(app, ["export", "--tool", "claude", "--level", "workspace"])
    assert result.exit_code == 0
    target = tmp_path / ".claude" / "skills" / "using-bathos.md"
    assert target.exists()
```

- [ ] **Step 2.2: Run tests to verify they fail**

```bash
uv run pytest tests/test_cli.py::test_export_dry_run_claude_user -v
```

Expected: `FAILED` — `No such command 'export'`

- [ ] **Step 2.3: Add `export` command to `cli.py`**

Add this command to `src/bathos/cli.py` (after the `sql` command):

```python
@app.command("export")
def export_cmd(
    tool: str = typer.Option("claude", "--tool", "-t", help="Target tool: claude or gemini"),
    level: str = typer.Option("user", "--level", "-l", help="Install level: user, workspace, or system"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would happen without writing"),
):
    """Export the using-bathos skill to a code tool (Claude Code or Gemini CLI)."""
    from bathos.export import export_skill, resolve_target, ExportError

    try:
        target = resolve_target(tool=tool, level=level)
    except ExportError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    result = export_skill(target=target, dry_run=dry_run)

    if dry_run:
        typer.echo(f"Dry run — would write skill to: {result.target}")
    else:
        typer.echo(f"Exported using-bathos skill to: {result.target}")
```

- [ ] **Step 2.4: Run tests to verify they pass**

```bash
uv run pytest tests/test_cli.py::test_export_dry_run_claude_user tests/test_cli.py::test_export_writes_file -v
```

Expected: `2 passed`

- [ ] **Step 2.5: Run full suite**

```bash
uv run pytest -x -q
```

Expected: all pass

- [ ] **Step 2.6: Commit**

```bash
git add src/bathos/cli.py tests/test_cli.py
git commit -m "feat(cli): add bth export command for skill distribution"
```

---

## Task 3: Write integration docs

**Files:**
- Create: `agent_assets/docs/claude-code-integration.md`
- Create: `agent_assets/docs/gemini-cli-integration.md`

- [ ] **Step 3.1: Create Claude Code integration doc**

```bash
mkdir -p /home/marielle/projects/bathos/agent_assets/docs
```

Create `agent_assets/docs/claude-code-integration.md`:

```markdown
# bathos Integration — Claude Code

bathos provides a skill (`using-bathos`) that teaches Claude Code how to:
- Track experiments via `bth run`
- Query results via `bth ls`, `bth find`, `bth sql`
- Validate runs via `bth check` _(coming in v0.2 — not yet available)_
- Decide when to dispatch vs. run CLI directly

## Install

```bash
# User-level (available in all projects)
bth export --tool claude --level user

# Workspace-level (current project only)
bth export --tool claude --level workspace
```

This writes `using-bathos.md` to `~/.claude/skills/` or `.claude/skills/` respectively.

## Verify

```bash
ls ~/.claude/skills/using-bathos.md
```

The skill is loaded automatically at session start in Claude Code. In a new session, the agent will have bathos commands available in its routing table.

## What the skill teaches agents

- **When to call `bth run`** vs. executing scripts directly
- **How to create sidecars** (`.bth.toml`) for pre-registration
- **Outcome evaluation** — how `bth ls` OUTCOME column maps to pass/marginal/fail
- **SLURM integration** — using `_bth_env.sh` in batch scripts
- **Cross-project queries** — `bth sql` across all projects

## MCP tool reference (if FastMCP is enabled)

| MCP Tool | CLI equivalent | Status |
|----------|---------------|--------|
| `run_script` | `bth run` | ✅ Available |
| `list_runs` | `bth ls` | ✅ Available |
| `get_run` | `bth show <id>` | ✅ Available |
| `find_runs` | `bth find` | ✅ Available |
| `run_sql` | `bth sql` | ✅ Available |
| `compact_catalog` | `bth compact` | ✅ Available |
| `check_runs` | `bth check` | 🔜 Coming in v0.2 |
| `archive_runs` | `bth archive` | 🔜 Coming in v0.2 |

## Update

```bash
bth export --tool claude --level user
```

Re-running overwrites with the latest skill version (stamped with bathos version and timestamp in first line).
```

- [ ] **Step 3.2: Create Gemini CLI integration doc**

Create `agent_assets/docs/gemini-cli-integration.md`:

```markdown
# bathos Integration — Gemini CLI

bathos provides a skill (`using-bathos`) that teaches Gemini CLI how to:
- Track experiments via `bth run`
- Query results via `bth ls`, `bth find`, `bth sql`
- Validate runs via `bth check` _(coming in v0.2 — not yet available)_
- Decide when to dispatch vs. run CLI directly

## Install

```bash
# User-level (available in all projects)
bth export --tool gemini --level user

# Workspace-level (current project only)
bth export --tool gemini --level workspace
```

This writes `using-bathos.md` to `~/.gemini/skills/` or `.gemini/skills/` respectively.

## Verify

```bash
ls ~/.gemini/skills/using-bathos.md
```

Gemini CLI loads skills from these paths at session start via its skill discovery mechanism.

## What the skill teaches agents

- **When to call `bth run`** vs. executing scripts directly
- **How to create sidecars** (`.bth.toml`) for pre-registration
- **Outcome evaluation** — how `bth ls` OUTCOME column maps to pass/marginal/fail
- **SLURM integration** — using `_bth_env.sh` in batch scripts
- **Cross-project queries** — `bth sql` across all projects

## Tool name mapping

Gemini CLI tools use different names from Claude Code. The using-bathos skill is written for Claude Code tool names. For Gemini CLI, the following equivalents apply:

| Skill tool name | Gemini CLI equivalent |
|-----------------|-----------------------|
| `Bash` | `run_shell_command` |
| `Read` | `read_file` |
| `Edit` | `replace_in_file` |
| `Write` | `write_file` |

The SKILL.md uses Claude Code names; adapt accordingly when the Gemini CLI skill loader doesn't auto-translate.

## Update

```bash
bth export --tool gemini --level user
```

Re-running overwrites with the latest skill version.
```

- [ ] **Step 3.3: Commit docs**

```bash
git add agent_assets/docs/
git commit -m "docs(agent): add Claude Code and Gemini CLI integration guides for using-bathos skill"
```

---

## Final Verification

- [ ] **Run full test suite**

```bash
uv run pytest -v --tb=short 2>&1 | tail -5
```

Expected: all pass

- [ ] **Smoke test export**

```bash
cd /tmp
bth export --tool claude --level workspace --dry-run
```

Expected: `Dry run — would write skill to: .claude/skills/using-bathos.md`

- [ ] **Test real export**

```bash
cd /tmp && mkdir bth_export_test && cd bth_export_test
bth export --tool claude --level workspace
cat .claude/skills/using-bathos.md | head -3
```

Expected: first line is `<!-- bathos v... | exported ... -->` followed by skill content
