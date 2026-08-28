import json

import pytest


def test_export_plugin_bundle_unknown_surface():
    from bathos.plugin_export import PluginExportError, export_plugin_bundle

    with pytest.raises(PluginExportError, match="Unknown surface"):
        export_plugin_bundle(surface="bogus", out=None, dry_run=True)


def test_export_plugin_bundle_writes_real_claude_bundle(tmp_path):
    import bathos
    from bathos.plugin_export import export_plugin_bundle

    out = tmp_path / "plugin-dist"
    result = export_plugin_bundle(surface="claude", out=out, dry_run=False)
    assert result.dry_run is False

    plugin_json = out / ".claude-plugin" / "plugin.json"
    assert plugin_json.exists()
    data = json.loads(plugin_json.read_text())
    assert data["name"] == "bathos"
    # Version comes from the installed package, not manifest.toml's own
    # (hand-maintained, easily stale) version field.
    assert data["version"] == bathos.__version__

    assert (out / "agents" / "experiment-runner.md").exists()
    assert (out / "skills" / "using-bathos" / "SKILL.md").exists()
    assert (out / "skills" / "bathos-cluster" / "SKILL.md").exists()
    assert (out / "skills" / "bathos-campaigns" / "SKILL.md").exists()
    assert (out / "skills" / "bathos-literature-parity" / "SKILL.md").exists()
    assert (out / "skills" / "bathos-mcp" / "SKILL.md").exists()

    mcp = json.loads((out / ".mcp.json").read_text())
    assert "bathos" in mcp["mcpServers"]


def test_export_plugin_bundle_dry_run_does_not_write(tmp_path):
    from bathos.plugin_export import export_plugin_bundle

    out = tmp_path / "plugin-dist"
    export_plugin_bundle(surface="claude", out=out, dry_run=True)
    assert not out.exists()


def test_export_plugin_bundle_writes_real_cursor_bundle(tmp_path):
    from bathos.plugin_export import export_plugin_bundle

    out = tmp_path / "plugin-dist-cursor"
    result = export_plugin_bundle(surface="cursor", out=out, dry_run=False)
    assert result.dry_run is False
    assert (out / ".cursor-plugin" / "plugin.json").exists()
    assert (out / "skills" / "using-bathos" / "SKILL.md").exists()
    assert (out / "skills" / "bathos-cluster" / "SKILL.md").exists()


def test_export_plugin_bundle_writes_real_antigravity_bundle(tmp_path):
    """Antigravity's emitter (non-rust-parity mode) has no 'agents' concept --
    it only emits plugin.json, skills/, hook scripts, and mcp_config.json.
    That's a real surface capability gap in cisternal's AntigravityEmitter,
    not a bathos integration bug -- assert what it actually emits rather than
    mirroring the claude/cursor agents assertions."""
    import json

    from bathos.plugin_export import export_plugin_bundle

    out = tmp_path / "plugin-dist-antigravity"
    result = export_plugin_bundle(surface="antigravity", out=out, dry_run=False)
    assert result.dry_run is False
    assert (out / "plugin.json").exists()
    assert (out / "mcp_config.json").exists()
    assert (out / "skills" / "using-bathos" / "SKILL.md").exists()

    plugin_json = json.loads((out / "plugin.json").read_text())
    assert plugin_json["name"] == "bathos"
