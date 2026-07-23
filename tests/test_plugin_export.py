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

    mcp = json.loads((out / ".mcp.json").read_text())
    assert "bathos" in mcp["mcpServers"]


def test_export_plugin_bundle_dry_run_does_not_write(tmp_path):
    from bathos.plugin_export import export_plugin_bundle
    out = tmp_path / "plugin-dist"
    export_plugin_bundle(surface="claude", out=out, dry_run=True)
    assert not out.exists()
