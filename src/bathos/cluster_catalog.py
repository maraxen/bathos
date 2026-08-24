"""Canonical cluster catalog path: {remote_root}/.bth/catalog.

Local catalogs may live at ~/.bth/catalog. Cluster jobs and bth sync must
share the project tree under remote_root, never the home catalog.
"""

from __future__ import annotations

import importlib.resources
import os
import re
import shlex
import subprocess
from pathlib import Path


class CatalogIdentityError(ValueError):
    """Job BTH_CATALOG_DIR does not match the remote rsync destination."""


def remote_catalog_path(remote_root: str) -> str:
    """Return `{remote_root}/.bth/catalog` without collapsing ``~``."""
    root = remote_root.rstrip("/")
    if root.endswith("/.bth/catalog") or root.endswith(".bth/catalog"):
        return root
    return f"{root}/.bth/catalog"


def cluster_catalog_export(remote_root: str | None) -> str:
    """Value for `export BTH_CATALOG_DIR=...` in `_bth_env.sh` (unquoted)."""
    if not remote_root:
        return "${BTH_PROJECT_ROOT}/.bth/catalog"
    path = remote_catalog_path(remote_root)
    if path.startswith("~/"):
        return "${HOME}/" + path[2:]
    if path == "~":
        return "${HOME}"
    return path


def cluster_root_export(remote_root: str) -> str:
    """Shell value for BTH_PROJECT_ROOT / BTH_WORKSPACE_ROOT from remote_root."""
    path = remote_root.rstrip("/")
    if path.endswith("/.bth/catalog"):
        path = path[: -len("/.bth/catalog")]
    elif path.endswith(".bth/catalog"):
        path = path[: -len(".bth/catalog")].rstrip("/")
    if path.startswith("~/"):
        return "${HOME}/" + path[2:]
    if path == "~":
        return "${HOME}"
    return path


def write_bth_env_sh(
    project_root: Path,
    *,
    slug: str,
    project_root_value: Path | str | None = None,
    remote_root: str | None = None,
) -> Path:
    """Write `scripts/slurm/_bth_env.sh` with a cluster-safe catalog export."""
    if remote_root:
        root = cluster_root_export(remote_root)
    else:
        root = str(project_root_value if project_root_value is not None else project_root)
    template = (importlib.resources.files("bathos") / "templates" / "_bth_env.sh").read_text(
        encoding="utf-8"
    )
    env_sh = template.format(
        slug=slug,
        root=root,
        catalog_dir=cluster_catalog_export(remote_root),
    )
    dest = project_root / "scripts" / "slurm" / "_bth_env.sh"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(env_sh)
    return dest


def _parse_export(text: str, name: str) -> str | None:
    pattern = rf"export\s+{re.escape(name)}=([^\n]+)"
    m = re.search(pattern, text)
    if not m:
        return None
    return m.group(1).strip().strip("'").strip('"')


def _normalize_catalog_path(
    value: str,
    *,
    home: str,
    project_root: str | None = None,
) -> str:
    v = value.strip().strip("'").strip('"')
    v = v.replace("${HOME}", home).replace("$HOME", home)
    if project_root is not None:
        pr = str(project_root)
        v = v.replace("${BTH_PROJECT_ROOT}", pr).replace("$BTH_PROJECT_ROOT", pr)
    if v.startswith("~/"):
        v = str(Path(home) / v[2:])
    elif v == "~":
        v = home
    return os.path.normpath(v)


def check_env_catalog_matches_remote(project_root: Path, remote_root: str) -> None:
    """Raise CatalogIdentityError if `_bth_env.sh` catalog ≠ `{remote_root}/.bth/catalog`."""
    env_path = project_root / "scripts" / "slurm" / "_bth_env.sh"
    if not env_path.exists():
        raise CatalogIdentityError(
            f"missing {env_path}: cluster jobs will not set BTH_CATALOG_DIR to "
            f"{remote_catalog_path(remote_root)}"
        )
    text = env_path.read_text()
    exported = _parse_export(text, "BTH_CATALOG_DIR")
    if not exported:
        raise CatalogIdentityError(
            f"{env_path} does not export BTH_CATALOG_DIR (jobs will write ~/.bth/catalog; "
            f"bth sync uses {remote_catalog_path(remote_root)})"
        )
    baked_root = _parse_export(text, "BTH_PROJECT_ROOT")
    home = str(Path.home())
    actual = _normalize_catalog_path(exported, home=home, project_root=baked_root)
    expected = _normalize_catalog_path(
        cluster_catalog_export(remote_root),
        home=home,
        project_root=baked_root,
    )
    if actual != expected:
        raise CatalogIdentityError(
            f"BTH_CATALOG_DIR in {env_path} is {exported!r}, which resolves to {actual}; "
            f"bth sync uses {expected} ({remote_catalog_path(remote_root)}). "
            "Regenerate scripts/slurm/_bth_env.sh (bth remote add / bth init) so jobs "
            "and sync share one catalog."
        )


def ensure_remote_catalog_dir(host: str, remote_root: str) -> None:
    """`mkdir -p` the remote cool catalog so rsync pull is not error 11."""
    if "\n" in host or "\r" in host:
        raise ValueError("host must not contain a newline")
    if "\n" in remote_root or "\r" in remote_root:
        raise ValueError("remote_root must not contain a newline")
    dest = remote_catalog_path(remote_root) + "/campaigns"
    if dest.startswith("/~/"):
        dest = dest[1:]
    if dest.startswith("~/"):
        remote_cmd = f"mkdir -p -- ${{HOME}}/{shlex.quote(dest[2:])}"
    else:
        remote_cmd = f"mkdir -p -- {shlex.quote(dest)}"
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "--", remote_cmd],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or f"ssh mkdir failed for {host}:{dest}")
