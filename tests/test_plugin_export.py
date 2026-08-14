import json

import pytest


def test_export_plugin_bundle_unknown_surface():
    from bathos.plugin_export import PluginExportError, export_plugin_bundle

    with pytest.raises(PluginExportError, match="Unknown surface"):
        export_plugin_bundle(surface="bogus", out=None, dry_run=True)


CISTERNAL_VERSION_OVERRIDE_BUG = (
    "cisternal's CompositeAssetSource.load() (assets/composite.py) computes a "
    "registry_meta from the caller-supplied --version/--name override, but "
    "then discards it when assembling the final bundle, using "
    "manifest_report.bundle.metadata (i.e. manifest.toml's own, often-stale "
    "plugin.version field) unconditionally. bathos passes --version explicitly "
    "specifically to avoid drifting from manifest.toml's hand-maintained "
    "version, and it silently doesn't work. Fixed upstream at "
    "maraxen/cisternal@a6005d8, shipping in cisternal v0.1.1a3 "
    "(maraxen/cisternal#23) — this repo's pyproject.toml already pins "
    ">=0.1.1a3; remove this xfail once that version is installed. "
    "(The prior path-resolution bug tracked as backlog #4078 — cisternal "
    "resolving manifest asset paths against the manifest's own directory — "
    "is already fixed on the bathos side: .praxia/manifest.toml's "
    "[[plugin.skills]]/[[plugin.snippets]]/[[plugin.agents]] paths now carry "
    "the `../` prefix cisternal's resolution needs.)"
)


@pytest.mark.xfail(strict=True, reason=CISTERNAL_VERSION_OVERRIDE_BUG)
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
