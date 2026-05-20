from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from bathos.config import ProjectConfig

_SYNC_TIMEOUT_S = 120  # rsync kill switch: fail if no completion within 2 minutes


@dataclass
class SyncResult:
    """Result of a sync operation."""

    transferred: int
    duration_s: float
    remote: str


def sync_catalog(
    remote_name: str, config: ProjectConfig, catalog_dir: Path, pull: bool = False
) -> SyncResult:
    """
    Sync cool-tier catalog to/from remote using rsync.

    Args:
        remote_name: Key in config.remotes (e.g., 'engaging')
        config: ProjectConfig with remotes dict
        catalog_dir: Local catalog directory (e.g., ~/.bth/catalog)
        pull: If False (default), push to remote; if True, pull from remote

    Returns:
        SyncResult with transferred file count, duration, and remote name

    Raises:
        ValueError: if remote_name not in config.remotes
        RuntimeError: if rsync fails
    """
    if remote_name not in config.remotes:
        raise ValueError(f"Remote '{remote_name}' not in config")

    remote_config = config.remotes[remote_name]
    host = remote_config["host"]
    remote_root = remote_config["remote_root"]

    local_runs = catalog_dir / "runs"
    remote_runs = f"{host}:{remote_root}/.bth/catalog/runs/"

    if pull:
        src = remote_runs
        dst = str(local_runs) + "/"
    else:
        src = str(local_runs) + "/"
        dst = remote_runs

    # ConnectTimeout=10: fail in 10s if host is unreachable rather than hanging
    # BatchMode=yes: never prompt for a password — fail immediately instead
    ssh_opts = "ssh -o ConnectTimeout=10 -o BatchMode=yes"

    cmd = [
        "rsync",
        "-az",
        "--partial",
        f"-e{ssh_opts}",
        "--ignore-existing",
        src,
        dst,
    ]

    start_time = time.time()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_SYNC_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"bth sync timed out after {_SYNC_TIMEOUT_S}s — "
            "check that the remote host is reachable and SSH keys are configured"
        )
    duration_s = time.time() - start_time

    if result.returncode != 0:
        raise RuntimeError(f"rsync failed with exit code {result.returncode}: {result.stderr}")

    # Parse transferred count from rsync output
    # rsync outputs something like "Number of regular files transferred: 42"
    transferred = _parse_transferred_count(result.stdout)

    return SyncResult(transferred=transferred, duration_s=duration_s, remote=remote_name)


def _parse_transferred_count(rsync_output: str) -> int:
    """
    Parse number of transferred files from rsync output.

    Returns 0 if count cannot be determined.
    """
    for line in rsync_output.split("\n"):
        if "files transferred" in line:
            # Extract number from lines like "Number of regular files transferred: 42"
            parts = line.split(":")
            if len(parts) > 1:
                try:
                    return int(parts[-1].strip().split()[0])
                except (ValueError, IndexError):
                    pass
    return 0
