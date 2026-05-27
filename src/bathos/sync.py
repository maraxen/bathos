from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from bathos.config import ProjectConfig
from bathos.telemetry import event

_SYNC_TIMEOUT_S = 120  # rsync kill switch: fail if no completion within 2 minutes
_RSYNC_STALL_SECONDS = 30  # Stall detection: no progress for N seconds


@dataclass
class SyncResult:
    """Result of a sync operation."""

    transferred: int
    duration_s: float
    remote: str
    filtered: int = 0


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

    project_slug = config.slug
    sync_filter = getattr(config, "sync_filter", "project_slug")

    if sync_filter == "project_slug":
        local_runs = catalog_dir / "runs" / project_slug
        remote_runs = f"{host}:{remote_root}/.bth/catalog/runs/{project_slug}/"
        # Count filtered runs (total in catalog minus this project's runs)
        runs_root = catalog_dir / "runs"
        if not pull and runs_root.exists():
            total = len(list(runs_root.rglob("run_*.parquet")))
            project_count = len(list(local_runs.glob("run_*.parquet"))) if local_runs.exists() else 0
            filtered = total - project_count
        else:
            filtered = 0
    else:
        local_runs = catalog_dir / "runs"
        remote_runs = f"{host}:{remote_root}/.bth/catalog/runs/"
        filtered = 0

    if not pull:
        local_runs.mkdir(parents=True, exist_ok=True)

    if pull:
        src = remote_runs
        dst = str(local_runs) + "/"
    else:
        src = str(local_runs) + "/"
        dst = remote_runs

    # ConnectTimeout=10: fail in 10s if host is unreachable rather than hanging
    # BatchMode=yes: never prompt for a password — fail immediately instead
    ssh_opts = "ssh -o ConnectTimeout=10 -o BatchMode=yes"

    # Build rsync command with --info=progress2 for streaming progress
    cmd = [
        "rsync",
        "-az",
        "--partial",
        f"-e{ssh_opts}",
        "--ignore-existing",
        "--info=progress2",
        src,
        dst,
    ]

    # Determine direction (push vs pull)
    direction = "pull" if pull else "push"

    # Emit telemetry: sync.rsync_start
    event(
        "sync.rsync_start",
        direction=direction,
        remote=remote_name,
        src=src,
        dst=dst,
        filters="project_slug" if sync_filter == "project_slug" else "none",
    )

    start_time = time.time()
    t0_ns = time.monotonic_ns()
    last_progress_ts = time.monotonic()
    total_bytes = 0
    total_files = 0
    proc = None
    stdout_output = ""

    try:
        proc = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )

        # Watchdog thread to detect hangs
        def watchdog():
            try:
                proc.wait(timeout=_SYNC_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:
                # Ignore other exceptions (e.g., if proc is already dead)
                pass

        watchdog_thread = threading.Thread(target=watchdog, daemon=True)
        watchdog_thread.start()

        # Stream-read stderr for progress2 format and stdout for completion info
        for line in iter(proc.stderr.readline, ""):
            if not line:
                break
            line = line.strip()

            # Parse progress2 format: "   1,234 100%    1.23MB/s    0:00:01 (xfr#1, to-chk=0/1)"
            m = re.match(
                r"\s*([\d,]+)\s+(\d+)%\s+([\d.]+\S*)\s+\S+",
                line,
            )
            if m:
                bytes_str = m.group(1).replace(",", "")
                pct_str = m.group(2)
                rate_str = m.group(3)

                try:
                    bytes_xfr = int(bytes_str)
                    pct = int(pct_str)
                    total_bytes = bytes_xfr
                    last_progress_ts = time.monotonic()

                    event(
                        "sync.rsync_progress",
                        bytes_transferred=bytes_xfr,
                        pct=pct,
                        xfer_rate=rate_str,
                    )
                except (ValueError, IndexError):
                    pass
            else:
                # Check for stall: no progress line for N seconds while proc alive
                elapsed_since = (time.monotonic() - last_progress_ts) * 1000
                if (
                    elapsed_since > (_RSYNC_STALL_SECONDS * 1000)
                    and proc.poll() is None
                ):
                    event(
                        "sync.rsync_stall",
                        elapsed_since_progress_ms=int(elapsed_since),
                    )
                    # Reset so we don't spam stall events
                    last_progress_ts = time.monotonic()

        # Read any remaining stdout (e.g., final stats)
        remaining_stdout = proc.stdout.read()
        if remaining_stdout:
            stdout_output = remaining_stdout

        # Wait for process to complete
        try:
            exit_code = proc.wait()
        except subprocess.TimeoutExpired:
            # Watchdog timed out, process was killed
            proc.kill()
            try:
                proc.wait()
            except subprocess.TimeoutExpired:
                # Already timed out, can't wait further
                pass
            raise

    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
            try:
                proc.wait()
            except subprocess.TimeoutExpired:
                pass
        duration_s = time.time() - start_time
        event(
            "sync.rsync_end",
            exit_code=-1,
            duration_ms=duration_s * 1000,
            bytes_transferred=total_bytes,
            files_transferred=total_files,
        )
        raise RuntimeError(
            f"bth sync timed out after {_SYNC_TIMEOUT_S}s — "
            "check that the remote host is reachable and SSH keys are configured"
        )
    except Exception as e:
        duration_s = time.time() - start_time
        event(
            "sync.rsync_end",
            exit_code=-1,
            duration_ms=duration_s * 1000,
            bytes_transferred=total_bytes,
            files_transferred=total_files,
        )
        raise

    duration_s = time.time() - start_time
    duration_ms = duration_s * 1000

    # Parse transferred count from stdout if we didn't get it from progress2
    if total_bytes == 0:
        transferred = _parse_transferred_count(stdout_output)
    else:
        transferred = total_bytes

    # Emit telemetry: sync.rsync_end
    event(
        "sync.rsync_end",
        exit_code=exit_code,
        duration_ms=duration_ms,
        bytes_transferred=total_bytes,
        files_transferred=total_files,
    )

    if exit_code != 0:
        raise RuntimeError(f"rsync failed with exit code {exit_code}")

    return SyncResult(transferred=transferred, duration_s=duration_s, remote=remote_name, filtered=filtered)


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
