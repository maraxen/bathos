from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECTS_REGISTRY = Path.home() / ".bth" / "projects.toml"


@dataclass
class ProjectConfig:
    slug: str
    root: Path
    catalog_dir: Path = field(default_factory=lambda: Path.home() / ".bth" / "catalog")
    remotes: dict[str, dict] = field(default_factory=dict)
    slurm: dict = field(default_factory=dict)
    sync_filter: str = "project_slug"
    claim: dict = field(default_factory=dict)
    #: [obligations] — per-trigger opt-in for the §5 post-mortem obligation triggers,
    #: plus `enforce`. Lives in .bth.toml so the setting is versioned and reaches SLURM
    #: jobs, which read the same file; a shell-only env var would be honoured locally and
    #: silently skipped on the cluster. Env vars still override (see obligations.py).
    obligations: dict = field(default_factory=dict)


def default_catalog_dir() -> Path:
    return Path.home() / ".bth" / "catalog"


_TRUE = ("1", "true", "yes")
_FALSE = ("0", "false", "no")


def env_override(name: str) -> bool | None:
    """Tri-state read of a boolean env flag: True / False / None (unset or unrecognised).

    A set-but-false value must be able to turn a config-enabled flag OFF, so this cannot
    collapse to a plain truthiness test — otherwise `BTH_X=0` would silently mean "fall
    through to config", i.e. still enabled, which is the opposite of what an override is for.
    An unrecognised value returns None rather than False so garbage is not read as a
    deliberate "off" that beats a considered config setting.
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    return None


def resolve_flag(
    env_name: str,
    section: str,
    key: str,
    workspace_root: Path | str | None = None,
) -> bool:
    """Resolve a boolean gate: env var (both directions) → `.bth.toml [section] key` → False.

    Single implementation so the obligation triggers and the review-coverage gate cannot
    drift apart on the subtle half (the tri-state env read above).

    The config file is the durable home for these settings: a SLURM job reads the same
    `.bth.toml`, whereas a shell-only export is honoured locally and silently skipped on the
    cluster. A malformed or missing config resolves to False — the safe direction for a gate
    that changes verdicts or writes ledger entries — rather than raising mid-run.
    """
    override = env_override(env_name)
    if override is not None:
        return override
    try:
        cfg_path = find_project_config(Path(workspace_root) if workspace_root else None)
        if cfg_path is None:
            return False
        return bool(getattr(load_project_config(cfg_path), section, {}).get(key, False))
    except Exception:
        return False


def find_project_config(start: Path | None = None) -> Path | None:
    if start is None:
        start = Path.cwd()
    for directory in [start, *start.parents]:
        candidate = directory / ".bth.toml"
        if candidate.exists():
            return candidate
    return None


def load_project_config(path: Path) -> ProjectConfig:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    project = data["project"]
    return ProjectConfig(
        slug=project["slug"],
        root=Path(project["root"]).expanduser(),
        catalog_dir=Path(project["catalog_dir"]).expanduser()
        if "catalog_dir" in project
        else default_catalog_dir(),
        remotes=data.get("remotes", {}),
        slurm=data.get("slurm", {}),
        sync_filter=project.get("sync_filter", "project_slug"),
        claim=data.get("claim", {}),
        obligations=data.get("obligations", {}),
    )


def register_project(slug: str, catalog_dir: Path) -> None:
    """Register project in global registry at ~/.bth/projects.toml."""
    try:
        import toml  # type: ignore

        registry: dict = {}
        if PROJECTS_REGISTRY.exists():
            registry = tomllib.loads(PROJECTS_REGISTRY.read_text())
        projects = registry.setdefault("projects", [])
        # Avoid duplicates
        existing_slugs = [p.get("slug") for p in projects]
        if slug not in existing_slugs:
            projects.append({"slug": slug, "catalog_dir": str(catalog_dir)})
        PROJECTS_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        PROJECTS_REGISTRY.write_text(toml.dumps(registry))
    except Exception as e:
        logger.warning(
            f"Failed to register project {slug} in global registry: {e}"
        )  # Registry is best-effort; never block init


def list_registered_projects() -> list[dict]:
    """List all registered projects from global registry."""
    if not PROJECTS_REGISTRY.exists():
        return []
    try:
        return tomllib.loads(PROJECTS_REGISTRY.read_text()).get("projects", [])
    except Exception as e:
        logger.warning(f"Failed to read projects registry {PROJECTS_REGISTRY}: {e}")
        return []
