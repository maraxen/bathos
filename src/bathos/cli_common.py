"""Shared CLI helpers used by both the Typer surface (`bathos.cli`) and the
hand-written cyclopts CLI-only commands (`bathos.cli_cyclopts`) -- backlog
#4702 Milestone 2's "CLI-only batch". Extracted rather than duplicated so the
final cutover (deleting `bathos.cli`) doesn't strand a second copy.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def catalog_dir() -> Path:
    override = os.environ.get("BTH_CATALOG_DIR")
    if override:
        return Path(override)
    from bathos.config import default_catalog_dir, find_project_config, load_project_config

    cfg_path = find_project_config()
    if cfg_path is not None:
        return load_project_config(cfg_path).catalog_dir
    return default_catalog_dir()


def require_project_slug() -> str:
    slug_env = os.environ.get("BTH_PROJECT_SLUG")
    if slug_env:
        return slug_env
    from bathos.config import find_project_config, load_project_config

    cfg_path = find_project_config()
    if cfg_path is None:
        print("No .bth.toml found. Run `bth init` first.", file=sys.stderr)
        raise SystemExit(1)
    return load_project_config(cfg_path).slug


def soft_project_slug() -> str | None:
    """Best-effort project slug lookup that returns None instead of exiting.

    Used by commands like `lint` that must keep working on a project with no
    BTH_PROJECT_SLUG/.bth.toml at all (unlike `run`/`archive-artifact`, which require one).
    """
    slug_env = os.environ.get("BTH_PROJECT_SLUG")
    if slug_env:
        return slug_env
    from bathos.config import find_project_config, load_project_config

    cfg_path = find_project_config()
    if cfg_path is None:
        return None
    return load_project_config(cfg_path).slug
