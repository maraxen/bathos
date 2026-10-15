"""Shared CLI helpers used by both the Typer surface (`bathos.cli`) and the
hand-written cyclopts CLI-only commands (`bathos.cli_cyclopts`) -- backlog
#4702 Milestone 2's "CLI-only batch". Extracted rather than duplicated so the
final cutover (deleting `bathos.cli`) doesn't strand a second copy.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


def normalise_tags(tags: list[str] | None) -> list[str]:
    """Split each tag on commas, strip whitespace, drop empties (debt #2203).

    ``tags=["a,b,c"]`` (how MCP clients and ``--tags a,b,c`` deliver a
    comma-joined list) must become three tags, not one literal ``"a,b,c"``.
    """
    out: list[str] = []
    for tag in tags or []:
        out.extend(part.strip() for part in str(tag).split(",") if part.strip())
    return out


def normalise_output_paths(paths: list[str] | None) -> list[str]:
    """Expand JSON-list-string entries into their member paths (debt #2217).

    An entry that begins with ``[`` and ``json.loads`` to a list of strings is
    replaced by those strings; anything else is kept verbatim.
    """
    import json

    out: list[str] = []
    for entry in paths or []:
        if isinstance(entry, str) and entry.lstrip().startswith("["):
            try:
                parsed = json.loads(entry)
            except ValueError:
                parsed = None
            if isinstance(parsed, list) and all(isinstance(p, str) for p in parsed):
                out.extend(parsed)
                continue
        out.append(entry)
    return out


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


def require_project_config_path() -> Path:
    """Hard-fail with the same message/stream as require_project_slug(), but
    return the .bth.toml path itself rather than a resolved slug -- for
    commands (remote add/list/remove/test, submit) that need the config path
    or its full ProjectConfig, not just the slug."""
    from bathos.config import find_project_config

    cfg_path = find_project_config()
    if cfg_path is None:
        print("No .bth.toml found. Run `bth init` first.", file=sys.stderr)
        raise SystemExit(1)
    return cfg_path


def parse_since(since: str | None) -> datetime | None:
    """Parse a relative-time filter like '7d' or '24h' into a UTC datetime."""
    if since is None:
        return None
    if since.endswith("d"):
        return datetime.now(UTC) - timedelta(days=float(since[:-1]))
    if since.endswith("h"):
        return datetime.now(UTC) - timedelta(hours=float(since[:-1]))
    return None


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
