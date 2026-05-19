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


def test_register_mcp_claude_user_creates_mcp_json(tmp_path, monkeypatch):
    """register_mcp writes mcpServers.bathos into ~/.claude.json."""
    from bathos.export import register_mcp
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    register_mcp(tool="claude", level="user", dry_run=False)
    mcp_path = tmp_path / ".claude.json"
    assert mcp_path.exists()
    import json
    data = json.loads(mcp_path.read_text())
    assert "bathos" in data["mcpServers"]
    assert data["mcpServers"]["bathos"]["command"] == "uv"


def test_register_mcp_merges_existing_servers(tmp_path, monkeypatch):
    """register_mcp preserves existing mcpServers entries."""
    import json
    from bathos.export import register_mcp
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    mcp_path = tmp_path / ".claude.json"
    mcp_path.write_text(json.dumps({"mcpServers": {"other": {"command": "npx"}}}))
    register_mcp(tool="claude", level="user", dry_run=False)
    data = json.loads(mcp_path.read_text())
    assert "other" in data["mcpServers"]
    assert "bathos" in data["mcpServers"]


def test_register_mcp_gemini_merges_settings(tmp_path, monkeypatch):
    """register_mcp merges into ~/.gemini/settings.json preserving other keys."""
    import json
    from bathos.export import register_mcp
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    settings_path = tmp_path / ".gemini" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"theme": "dark", "mcpServers": {}}))
    register_mcp(tool="gemini", level="user", dry_run=False)
    data = json.loads(settings_path.read_text())
    assert data["theme"] == "dark"
    assert "bathos" in data["mcpServers"]


def test_register_mcp_dry_run_does_not_write(tmp_path, monkeypatch):
    """register_mcp dry_run=True does not write any file."""
    from bathos.export import register_mcp
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    register_mcp(tool="claude", level="user", dry_run=True)
    assert not (tmp_path / ".claude.json").exists()


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
