from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import tomllib


@dataclass
class ProjectConfig:
    slug: str
    root: Path
    remotes: dict[str, dict] = field(default_factory=dict)
    slurm: dict = field(default_factory=dict)


def default_catalog_dir() -> Path:
    return Path.home() / ".bth" / "catalog"


def find_project_config(start: Path = Path.cwd()) -> Path | None:
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
        root=Path(project["root"]),
        remotes=data.get("remotes", {}),
        slurm=data.get("slurm", {}),
    )
