"""Export bathos as a real agent-surface plugin bundle via cisternal.

Distinct from bathos.export (UNCHANGED — do not touch), which imperatively
writes a single skill file + merges MCP config directly into a caller's
~/.claude.json. This module instead builds a self-contained, portable plugin
bundle (.claude-plugin/, agents/, skills/, .mcp.json) from the wired
"bathos" MCP tool registry plus .praxia/manifest.toml's declared skills/
agents/hooks, calling cisternal's native, in-process Python API directly
(cisternal.assets.load.load_asset_report -> cisternal.export emitters ->
cisternal.export.write_bundle) — cisternal documents this as public API in
cisternal/{assets,export}/__init__.py, so there's no need to shell out to
the cisternal CLI and parse its stderr for warnings.

Note on manifest path resolution: cisternal's ManifestAssetSource resolves a
manifest's declared asset paths relative to the manifest FILE's own
*grandparent* directory (tuned for its own convention of
<plugin_root>/.praxia/manifest.toml), matching praxia's own repo-root-relative
resolution of the same manifest. Requires cisternal>=0.1.1a4: earlier versions
resolved from the manifest file's single parent instead, which is why
.praxia/manifest.toml's asset paths used to carry a `../` prefix as a
workaround (removed when this module was ported to the native API).
"""

from __future__ import annotations

import importlib
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
    files: tuple[str, ...]


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

    Imports bathos.mcp (populating the "bathos" cisternal tool registry via
    its @cisternal.tool side-effects), then loads .praxia/manifest.toml
    against that registry and emits it for the selected surface — in-process,
    passing the installed bathos version explicitly so the bundle never
    drifts from manifest.toml's own (hand-maintained, easily stale) version
    field.
    """
    if surface not in SUPPORTED_SURFACES:
        raise PluginExportError(
            f"Unknown surface {surface!r}. Choose one of: {', '.join(SUPPORTED_SURFACES)}."
        )

    from cisternal.assets.bundle import BundleMetadata
    from cisternal.assets.load import load_asset_report
    from cisternal.export import get_emitter, write_bundle

    # Triggers bathos.mcp's @cisternal.tool registration side-effects (mirrors
    # `cisternal assets export --import bathos.mcp`); sys.modules caching
    # means an already-imported bathos.mcp is a no-op here, matching the CLI.
    importlib.import_module("bathos.mcp")

    repo_root = _find_repo_root()
    manifest_path = repo_root / ".praxia" / "manifest.toml"

    # Load once to pick up the manifest-declared description, then reload
    # with bathos's own name/version overriding the manifest's — mirrors
    # cisternal's own CLI (cisternal.cli._load_export_bundle).
    preload = load_asset_report(manifest=manifest_path, registry="bathos")
    metadata = BundleMetadata(
        name="bathos",
        version=bathos.__version__,
        description=preload.bundle.metadata.description,
    )
    report = load_asset_report(manifest=manifest_path, registry="bathos", metadata=metadata)

    # load_asset_report never raises; a warning usually means an asset
    # silently got dropped from the bundle, and a conflict means two assets
    # collided on the same name — both are hard failures for us.
    if report.warnings or report.conflicts:
        raise PluginExportError(
            "cisternal reported problems loading the manifest:\n"
            f"warnings: {report.warnings}\nconflicts: {report.conflicts}"
        )

    emitter = get_emitter(surface)
    if emitter is None:
        raise PluginExportError(f"cisternal has no emitter registered for surface {surface!r}.")

    files = emitter.emit(report.bundle)
    write_result = write_bundle(files, out, dry_run=dry_run)

    return PluginExportResult(
        surface=surface,
        out=out,
        dry_run=dry_run,
        files=tuple(path for path, _sha256 in write_result.files),
    )
