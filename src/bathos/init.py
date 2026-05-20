from __future__ import annotations

import importlib.resources
from pathlib import Path

from bathos.catalog import init_catalog

SCRIPT_DIRS = [
    "scripts/experiments",
    "scripts/analysis",
    "scripts/validation",
    "scripts/benchmarks",
    "scripts/data",
    "scripts/slurm",
    "scripts/debug",
    "scripts/explore",
    "scripts/scratch",
]

_BTH_TOML_TEMPLATE = """\
[project]
slug = "{slug}"
root = "{root}"
"""

_GITIGNORE_ENTRY = "scripts/scratch/\n"


def _load_env_sh_template() -> str:
    pkg = importlib.resources.files("bathos") / "templates" / "_bth_env.sh"
    return pkg.read_text(encoding="utf-8")


def init_project(
    project_root: Path,
    slug: str,
    catalog_dir: Path,
    remote: str | None = None,
    slurm_partition: str | None = None,
) -> None:
    # Script directories
    for d in SCRIPT_DIRS:
        (project_root / d).mkdir(parents=True, exist_ok=True)

    # .bth.toml
    toml_path = project_root / ".bth.toml"
    content = _BTH_TOML_TEMPLATE.format(slug=slug, root=str(project_root))
    if remote:
        host, remote_root = remote.split(":", 1)
        content += f'\n[remotes.{host}]\nhost = "{host}"\nremote_root = "{remote_root}"\n'
    if slurm_partition:
        content += f'\n[slurm]\npartition = "{slurm_partition}"\n'
    toml_path.write_text(content)

    # scripts/slurm/_bth_env.sh
    template = _load_env_sh_template()
    env_sh = template.format(slug=slug, root=str(project_root), catalog_dir=str(catalog_dir))
    (project_root / "scripts" / "slurm" / "_bth_env.sh").write_text(env_sh)

    # .gitignore
    gitignore = project_root / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    if _GITIGNORE_ENTRY.strip() not in existing:
        with open(gitignore, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(_GITIGNORE_ENTRY)

    # Catalog
    init_catalog(catalog_dir)

    # Register in global project registry
    from bathos.config import register_project

    register_project(slug=slug, catalog_dir=catalog_dir)
