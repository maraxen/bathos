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
id = "{project_id}"
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
    from bathos.runlog.project_id import mint_project_id

    toml_path = project_root / ".bth.toml"
    content = _BTH_TOML_TEMPLATE.format(
        slug=slug, root=str(project_root), project_id=mint_project_id()
    )
    remote_root = None
    if remote:
        host, remote_root = remote.split(":", 1)
        content += f'\n[remotes.{host}]\nhost = "{host}"\nremote_root = "{remote_root}"\n'
    if slurm_partition:
        content += f'\n[slurm]\npartition = "{slurm_partition}"\n'
    toml_path.write_text(content)

    from bathos.cluster_catalog import write_bth_env_sh

    write_bth_env_sh(
        project_root,
        slug=slug,
        project_root_value=project_root,
        remote_root=remote_root,
    )

    # .gitignore
    gitignore = project_root / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    if _GITIGNORE_ENTRY.strip() not in existing:
        with open(gitignore, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(_GITIGNORE_ENTRY)

    # D3: .bth/log/ must be gitignored before any run can append to it.
    from bathos.runlog.resolve import ensure_log_ignored

    ensure_log_ignored(project_root)

    # Catalog
    init_catalog(catalog_dir)

    # Register in global project registry
    from bathos.config import register_project

    register_project(slug=slug, catalog_dir=catalog_dir)


def assign_id_to_existing_project(project_root: Path) -> tuple[str, bool]:
    """`bth init --assign-id`: retrofit a `[project] id` onto an existing
    project's `.bth.toml` without redoing the rest of `init_project` (script
    dirs, .gitignore, catalog, env.sh). Returns `(project_id, minted)`.
    """
    from bathos.runlog.project_id import assign_project_id

    result = assign_project_id(project_root)
    return result.project_id, result.minted
