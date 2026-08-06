import json

import pytest


def test_export_plugin_bundle_unknown_surface():
    from bathos.plugin_export import PluginExportError, export_plugin_bundle

    with pytest.raises(PluginExportError, match="Unknown surface"):
        export_plugin_bundle(surface="bogus", out=None, dry_run=True)


CISTERNAL_PATH_BUG = (
    "bth export --surface is genuinely broken by an upstream path-resolution disagreement, "
    "and these tests are correctly red — see backlog #4078. cisternal resolves a manifest's "
    "declared asset paths against the MANIFEST'S OWN DIRECTORY "
    "(cisternal/assets/manifest.py:25, `self._root = self._manifest_path.parent`), so "
    "`.praxia/manifest.toml` + `agent_assets/skills/...` resolves to "
    "`.praxia/agent_assets/skills/...`, which does not exist. praxia resolves the SAME "
    "manifest against the repo root — parent-of-parent — deliberately and with a comment "
    "saying so (praxia-workflows/src/plugin_cli.rs:400-405). bathos's manifest paths are "
    "correct for praxia and wrong for cisternal, and no single path string satisfies both, "
    "so this cannot be fixed in this repo: rewriting the manifest to `../agent_assets/...` "
    "would fix cisternal and break praxia. strict=True on purpose — if cisternal changes its "
    "resolution these must fail loudly rather than silently start passing."
)


@pytest.mark.xfail(strict=True, reason=CISTERNAL_PATH_BUG)
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


@pytest.mark.xfail(strict=True, reason=CISTERNAL_PATH_BUG)
def test_export_plugin_bundle_dry_run_does_not_write(tmp_path):
    from bathos.plugin_export import export_plugin_bundle

    out = tmp_path / "plugin-dist"
    export_plugin_bundle(surface="claude", out=out, dry_run=True)
    assert not out.exists()
