"""The `bth plugin` sub-app and its packaged bundle snapshot.

`bth plugin install claude` reads src/bathos/agent_plugin.json when bathos is
installed from a wheel (no .praxia/manifest.toml on disk). It must match the
manifest; after editing the manifest or any skill/agent it lists, regenerate:

    uv run cisternal assets snapshot --out src/bathos/agent_plugin.json
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _exit_code(app, argv: list[str]) -> int:
    try:
        app(argv)
    except SystemExit as exc:
        return exc.code or 0
    return 0


def test_packaged_snapshot_matches_manifest() -> None:
    from cisternal.cli import app

    argv = [
        "assets", "snapshot", "--check",
        "--manifest", str(ROOT / ".praxia" / "manifest.toml"),
        "--out", str(ROOT / "src" / "bathos" / "agent_plugin.json"),
    ]
    assert _exit_code(app, argv) == 0, "agent_plugin.json is stale; see module docstring"


def test_committed_repo_bundle_is_current() -> None:
    """plugin/ + .claude-plugin/marketplace.json make the repo installable from GitHub
    (`/plugin marketplace add maraxen/bathos`); regenerate with `bth plugin repo-bundle`."""
    from bathos.cli_cyclopts import app

    assert _exit_code(app, ["plugin", "repo-bundle", "--check"]) == 0, (
        "plugin/ is stale; run `bth plugin repo-bundle` and commit the result"
    )


def test_repo_bundle_mcp_server_is_self_contained() -> None:
    """A GitHub install has no bathos package; the server must launch via uvx."""
    import json

    import bathos

    mcp = json.loads((ROOT / "plugin" / ".mcp.json").read_text(encoding="utf-8"))
    server = mcp["mcpServers"]["bathos"]
    assert server["command"] == "uvx"
    assert server["args"] == ["--from", f"bathos=={bathos.__version__}", "bth-mcp"]


def test_plugin_subapp_is_mounted(capsys: pytest.CaptureFixture[str]) -> None:
    from bathos.cli_cyclopts import app

    assert _exit_code(app, ["plugin", "info"]) == 0
    assert "plugin:      bathos" in capsys.readouterr().out
