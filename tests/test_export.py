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
