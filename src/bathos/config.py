from __future__ import annotations

import logging
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


def default_catalog_dir() -> Path:
    return Path.home() / ".bth" / "catalog"


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
        catalog_dir=Path(project["catalog_dir"]).expanduser() if "catalog_dir" in project else default_catalog_dir(),
        remotes=data.get("remotes", {}),
        slurm=data.get("slurm", {}),
        sync_filter=project.get("sync_filter", "project_slug"),
        claim=data.get("claim", {}),
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
        logger.warning(f"Failed to register project {slug} in global registry: {e}")  # Registry is best-effort; never block init


def list_registered_projects() -> list[dict]:
    """List all registered projects from global registry."""
    if not PROJECTS_REGISTRY.exists():
        return []
    try:
        return tomllib.loads(PROJECTS_REGISTRY.read_text()).get("projects", [])
    except Exception as e:
        logger.warning(f"Failed to read projects registry {PROJECTS_REGISTRY}: {e}")
        return []
