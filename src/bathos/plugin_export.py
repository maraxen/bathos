"""Export bathos as a real agent-surface plugin bundle via cisternal.

Distinct from bathos.export (UNCHANGED — do not touch), which imperatively
writes a single skill file + merges MCP config directly into a caller's
~/.claude.json. This module instead builds a self-contained, portable plugin
bundle (.claude-plugin/, agents/, skills/, .mcp.json) from the wired
"bathos" MCP tool registry plus .praxia/manifest.toml's declared skills/
agents/hooks, by shelling out to `cisternal assets export`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import bathos

SUPPORTED_SURFACES = ("claude", "cursor", "copilot", "antigravity")


class PluginExportError(Exception):
    pass


@dataclass
class PluginExportResult:
    surface: str
    out: Path
    dry_run: bool
    stdout: str


def _find_repo_root() -> Path:
    """Locate the repo root containing .praxia/manifest.toml.

    Mirrors bathos.export.get_skill_source_path's editable-install
    assumption: this only works against a source checkout, not an installed
    wheel, since the manifest and agent_assets/ are dev-time artifacts.
    """
    package_dir = Path(bathos.__file__).parent
    candidate = package_dir.parent.parent
    if (candidate / ".praxia" / "manifest.toml").exists():
        return candidate
    raise PluginExportError(
        "Could not locate .praxia/manifest.toml relative to the installed "
        "bathos package. Plugin export requires a source checkout (editable "
        "install), not an installed wheel."
    )


def export_plugin_bundle(
    surface: str,
    out: Path,
    dry_run: bool = False,
) -> PluginExportResult:
    """Export the wired "bathos" registry + manifest assets as a plugin bundle.

    Runs `cisternal assets export` against .praxia/manifest.toml and the
    "bathos" MCP tool registry (populated by importing bathos.mcp), passing
    the installed bathos version explicitly so the bundle never drifts from
    manifest.toml's own (hand-maintained, easily stale) version field.
    """
    if surface not in SUPPORTED_SURFACES:
        raise PluginExportError(
            f"Unknown surface {surface!r}. Choose one of: {', '.join(SUPPORTED_SURFACES)}."
        )

    # Check the running interpreter's own bin/ dir first — bth and cisternal
    # are console scripts in the same venv, but that venv's bin/ is often
    # not on PATH unless explicitly activated (e.g. when bth is invoked via
    # its own venv-relative path rather than an activated shell).
    venv_candidate = Path(sys.executable).parent / "cisternal"
    cisternal_bin = str(venv_candidate) if venv_candidate.exists() else shutil.which("cisternal")
    if cisternal_bin is None:
        raise PluginExportError(
            "cisternal CLI not found (checked the current interpreter's bin/ "
            "dir and PATH). Plugin export requires the 'cisternal' console "
            "script (installed alongside the cisternal dependency)."
        )

    repo_root = _find_repo_root()
    manifest_path = repo_root / ".praxia" / "manifest.toml"

    cmd = [
        cisternal_bin,
        "assets",
        "export",
        "--manifest",
        str(manifest_path),
        "--registry",
        "bathos",
        "--import",
        "bathos.mcp",
        "--surface",
        surface,
        "--name",
        "bathos",
        "--version",
        bathos.__version__,
    ]
    if dry_run:
        cmd.append("--dry-run")
    else:
        cmd.extend(["--out", str(out)])

    proc = subprocess.run(
        cmd,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    # cisternal assets export always exits 0 (never-raise convention) and
    # reports problems via stderr warnings instead — treat any stderr output
    # as a hard failure here, since a correctly configured manifest.toml
    # should produce none (a warning usually means an asset silently got
    # dropped from the bundle, e.g. an unreadable path).
    if proc.returncode != 0 or proc.stderr.strip():
        raise PluginExportError(
            f"cisternal assets export reported problems (exit {proc.returncode}):\n{proc.stderr}"
        )

    return PluginExportResult(
        surface=surface,
        out=out,
        dry_run=dry_run,
        stdout=proc.stdout,
    )
