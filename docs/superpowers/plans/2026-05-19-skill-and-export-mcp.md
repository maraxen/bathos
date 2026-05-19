# SKILL.md Update + Export MCP Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update `agent_assets/using_bathos/SKILL.md` to accurately reflect the v0.2 MCP tool set, and extend `bth export` to register the bathos MCP server in the tool's config file alongside the skill.

**Architecture:** Two changes. (1) Rewrite SKILL.md Section 9 to show all shipped MCP tools with correct v0.2 status. (2) Extend `export.py` with a `register_mcp()` function that merges `mcpServers.bathos` into the correct config file per tool/level, then call it from `export_cmd`.

**Tech Stack:** Python 3.12, json (stdlib), pathlib, Typer, uv

---

## File Map

| Action | Path |
|--------|------|
| Modify | `agent_assets/using_bathos/SKILL.md` |
| Modify | `src/bathos/export.py` |
| Modify | `src/bathos/cli.py` (export_cmd) |
| Modify | `tests/test_export.py` |

---

## MCP Config Locations Reference

| Tool | Level | Config file | Key |
|------|-------|-------------|-----|
| Claude Code | user | `~/.claude/mcp.json` | `mcpServers` |
| Claude Code | workspace | `.mcp.json` in CWD | `mcpServers` |
| Claude Code | system | `/etc/claude/mcp.json` | `mcpServers` |
| Gemini CLI | user | `~/.gemini/settings.json` | `mcpServers` |
| Gemini CLI | workspace | `.gemini/settings.json` in CWD | `mcpServers` |
| Gemini CLI | system | `/etc/gemini/settings.json` | `mcpServers` |

## MCP Server Entry (both tools)

```json
{
  "command": "uv",
  "args": ["run", "--with", "bathos[mcp]", "bth-mcp"]
}
```

---

## Task 1: Update SKILL.md Section 9 (MCP Tool Reference)

**Files:**
- Modify: `agent_assets/using_bathos/SKILL.md`

- [ ] **Step 1.1: Locate and replace Section 9**

Find the section starting with `# SECTION 9: MCP Tool Reference` (around line 757). Replace the entire section (through the `### Planned` table) with:

```markdown
# SECTION 9: MCP Tool Reference (v0.2 — Fully Shipped)

**Status:** FastMCP server ships with bathos. Start with `bth-mcp` (stdio transport). Register automatically via `bth export --tool claude --level user` (writes skill + wires MCP server).

### All Shipped Tools ✅

| MCP Tool | Arguments | Return | CLI Equivalent |
|----------|-----------|--------|----------------|
| `list_runs` | `catalog_dir, limit, since, status` | `{runs: [], count: int}` | `bth ls` |
| `find_runs` | `catalog_dir, project, since, status, tag, output_file` | `{runs: [], count: int}` | `bth find` |
| `get_run` | `run_id, catalog_dir` | `{run: {id, command, git_hash, outcome, ...}}` | `bth show` |
| `run_sql` | `query, catalog_dir` | `{rows: []}` | `bth sql` |
| `init_project` | `project_root, slug, remote, slurm_partition` | `{success: bool, msg: str}` | `bth init` |
| `run` | `script_path, args, project_slug, catalog_dir, output_paths, tags` | `{run_id: str, exit_code: int}` | `bth run` |
| `compact` | `catalog_dir` | `{ingested: int, skipped: int}` | `bth compact` |
| `archive` | `project, archive_dir, dry_run, catalog_dir` | `{runs: int, partitions: int}` | `bth archive` |
| `check` | `catalog_dir, project_root, status_filter` | `{results: [], total: int}` | `bth check` |
| `sync` | `remote, pull, catalog_dir` | `{transferred: int, duration_s: float}` | `bth sync` |

### Registration

```bash
# Claude Code — user level (all projects)
bth export --tool claude --level user

# Claude Code — workspace level (this project only)
bth export --tool claude --level workspace

# Gemini CLI — user level
bth export --tool gemini --level user
```

Both skill and MCP server are registered in one command. The MCP entry:
- **Claude Code:** written to `~/.claude/mcp.json` (user) or `.mcp.json` in CWD (workspace)
- **Gemini CLI:** merged into `~/.gemini/settings.json` (user) or `.gemini/settings.json` (workspace)
```

- [ ] **Step 1.2: Commit**

```bash
git add agent_assets/using_bathos/SKILL.md
git commit -m "docs(skill): update MCP reference to v0.2 full ship status"
```

---

## Task 2: Add `register_mcp()` to `export.py`

**Files:**
- Modify: `src/bathos/export.py`
- Modify: `tests/test_export.py`

- [ ] **Step 2.1: Write failing tests**

Add to `tests/test_export.py`:

```python
def test_register_mcp_claude_user_creates_mcp_json(tmp_path, monkeypatch):
    """register_mcp writes mcpServers.bathos into ~/.claude/mcp.json."""
    from bathos.export import register_mcp
    fake_home = tmp_path
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    register_mcp(tool="claude", level="user", dry_run=False)
    mcp_path = fake_home / ".claude" / "mcp.json"
    assert mcp_path.exists()
    import json
    data = json.loads(mcp_path.read_text())
    assert "bathos" in data["mcpServers"]
    assert data["mcpServers"]["bathos"]["command"] == "uv"


def test_register_mcp_merges_existing_servers(tmp_path, monkeypatch):
    """register_mcp preserves existing mcpServers entries."""
    import json
    from bathos.export import register_mcp
    fake_home = tmp_path
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    mcp_path = fake_home / ".claude" / "mcp.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text(json.dumps({"mcpServers": {"other": {"command": "npx"}}}))
    register_mcp(tool="claude", level="user", dry_run=False)
    data = json.loads(mcp_path.read_text())
    assert "other" in data["mcpServers"]
    assert "bathos" in data["mcpServers"]


def test_register_mcp_gemini_merges_settings(tmp_path, monkeypatch):
    """register_mcp merges into ~/.gemini/settings.json preserving other keys."""
    import json
    from bathos.export import register_mcp
    fake_home = tmp_path
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    settings_path = fake_home / ".gemini" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"theme": "dark", "mcpServers": {}}))
    register_mcp(tool="gemini", level="user", dry_run=False)
    data = json.loads(settings_path.read_text())
    assert data["theme"] == "dark"
    assert "bathos" in data["mcpServers"]


def test_register_mcp_dry_run_does_not_write(tmp_path, monkeypatch):
    """register_mcp dry_run=True does not write any file."""
    from bathos.export import register_mcp
    fake_home = tmp_path
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    register_mcp(tool="claude", level="user", dry_run=True)
    assert not (fake_home / ".claude" / "mcp.json").exists()


def test_register_mcp_workspace_uses_cwd(tmp_path, monkeypatch):
    """register_mcp workspace level writes to CWD-relative path."""
    import json
    from bathos.export import register_mcp
    monkeypatch.chdir(tmp_path)
    register_mcp(tool="claude", level="workspace", dry_run=False)
    mcp_path = tmp_path / ".mcp.json"
    assert mcp_path.exists()
    data = json.loads(mcp_path.read_text())
    assert "bathos" in data["mcpServers"]
```

- [ ] **Step 2.2: Run tests to confirm they fail**

```bash
cd /home/marielle/projects/bathos
uv run pytest tests/test_export.py::test_register_mcp_claude_user_creates_mcp_json -v
```

Expected: `FAILED` — `ImportError: cannot import name 'register_mcp'`

- [ ] **Step 2.3: Add `register_mcp()` to `src/bathos/export.py`**

Add after the `export_skill()` function:

```python
_MCP_ENTRY = {
    "command": "uv",
    "args": ["run", "--with", "bathos[mcp]", "bth-mcp"],
}

_CLAUDE_MCP_PATHS: dict[str, Path] = {
    "user": Path.home() / ".claude" / "mcp.json",
    "workspace": Path(".mcp.json"),
    "system": Path("/etc/claude/mcp.json"),
}

_GEMINI_SETTINGS_PATHS: dict[str, Path] = {
    "user": Path.home() / ".gemini" / "settings.json",
    "workspace": Path(".gemini") / "settings.json",
    "system": Path("/etc/gemini/settings.json"),
}


def register_mcp(tool: str, level: str, dry_run: bool) -> Path:
    """Merge mcpServers.bathos into the tool's MCP config file.

    Returns the target path (written or would-be-written).
    """
    import json

    if tool == "claude":
        if level not in _CLAUDE_MCP_PATHS:
            raise ExportError(f"Unknown level: {level!r}")
        target = _CLAUDE_MCP_PATHS[level]
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

    elif tool == "gemini":
        if level not in _GEMINI_SETTINGS_PATHS:
            raise ExportError(f"Unknown level: {level!r}")
        target = _GEMINI_SETTINGS_PATHS[level]
        if dry_run:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if target.exists():
            try:
                data = json.loads(target.read_text())
            except json.JSONDecodeError:
                data = {}
        data.setdefault("mcpServers", {})["bathos"] = _MCP_ENTRY
        target.write_text(json.dumps(data, indent=2))
        return target

    else:
        raise ExportError(f"Unknown tool: {tool!r}")
```

**Important:** The `_CLAUDE_MCP_PATHS` and `_GEMINI_SETTINGS_PATHS` dicts use `Path.home()` at module load time. To make them work with `monkeypatch.setattr("pathlib.Path.home", ...)` in tests, convert them to functions instead:

```python
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
```

And use these in `register_mcp()` instead of the dicts.

- [ ] **Step 2.4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_export.py -v
```

Expected: all pass

- [ ] **Step 2.5: Wire `register_mcp()` into `export_cmd` in `cli.py`**

In `src/bathos/cli.py`, update `export_cmd` to call `register_mcp` after `export_skill`:

```python
@app.command("export")
def export_cmd(
    tool: str = typer.Option("claude", "--tool", "-t", help="Target tool: claude or gemini"),
    level: str = typer.Option("user", "--level", "-l", help="Install level: user, workspace, or system"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would happen without writing"),
):
    """Export the using-bathos skill and register MCP server for a code tool."""
    from bathos.export import export_skill, resolve_target, register_mcp, ExportError

    try:
        target = resolve_target(tool=tool, level=level)
    except ExportError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    result = export_skill(target=target, dry_run=dry_run)
    mcp_target = register_mcp(tool=tool, level=level, dry_run=dry_run)

    if dry_run:
        typer.echo(f"Dry run — would write skill to:  {result.target}")
        typer.echo(f"Dry run — would register MCP at: {mcp_target}")
    else:
        typer.echo(f"Exported skill to:    {result.target}")
        typer.echo(f"Registered MCP at:   {mcp_target}")
```

- [ ] **Step 2.6: Run full suite**

```bash
uv run pytest -q
```

Expected: all pass (207+)

- [ ] **Step 2.7: Smoke test**

```bash
uv run bth export --tool claude --level workspace --dry-run
```

Expected:
```
Dry run — would write skill to:  .claude/skills/using-bathos.md
Dry run — would register MCP at: .mcp.json
```

- [ ] **Step 2.8: Commit**

```bash
git add src/bathos/export.py src/bathos/cli.py tests/test_export.py
git commit -m "feat(export): register MCP server in tool config alongside skill export"
```

---

## Final Verification

- [ ] **Re-export to both tools**

```bash
uv run bth export --tool claude --level user
uv run bth export --tool gemini --level user
```

Verify `~/.claude/mcp.json` and `~/.gemini/settings.json` both contain `mcpServers.bathos`.

```bash
python3 -c "import json; d=json.load(open('${HOME}/.claude/mcp.json')); print(d['mcpServers']['bathos'])"
python3 -c "import json; d=json.load(open('${HOME}/.gemini/settings.json')); print(d['mcpServers']['bathos'])"
```
