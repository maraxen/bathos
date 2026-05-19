from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import tomlkit

from bathos.config import ProjectConfig


@dataclass
class TestResult:
    success: bool
    latency_ms: float | None  # None when success is False
    error: str  # empty string when success is True


def list_remotes(config: ProjectConfig) -> list[tuple[str, str, str]]:
    """List all remotes from config.

    Args:
        config: ProjectConfig instance

    Returns:
        List of tuples (name, host, remote_root) sorted alphabetically by name.
    """
    remotes_list = []
    for name, remote_config in config.remotes.items():
        host = remote_config.get("host")
        remote_root = remote_config.get("remote_root")
        remotes_list.append((name, host, remote_root))

    # Sort by name (first element of tuple)
    remotes_list.sort(key=lambda x: x[0])
    return remotes_list


def add_remote(config_path: Path, name: str, host: str, path: str) -> None:
    """Add a remote to the config file.

    Args:
        config_path: Path to .bth.toml
        name: Remote name
        host: Remote hostname
        path: Remote path

    Raises:
        FileNotFoundError: If config_path doesn't exist
        ValueError: If remote name already exists
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    # Load the TOML file
    with open(config_path, "r") as f:
        doc = tomlkit.load(f)

    # Check if remotes section exists, if not create it
    if "remotes" not in doc:
        doc["remotes"] = tomlkit.table()

    # Check if remote already exists
    if name in doc["remotes"]:
        raise ValueError(f"Remote '{name}' already exists")

    # Create the remote entry with host and remote_root in that order
    remote_entry = tomlkit.table()
    remote_entry["host"] = host
    remote_entry["remote_root"] = path

    doc["remotes"][name] = remote_entry

    # Atomic write: write to tmp file first, then replace
    tmp_path = config_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        tomlkit.dump(doc, f)

    tmp_path.replace(config_path)


def remove_remote(config_path: Path, name: str) -> None:
    """Remove a remote from the config file.

    Args:
        config_path: Path to .bth.toml
        name: Remote name to remove

    Raises:
        FileNotFoundError: If config_path doesn't exist
        ValueError: If remote name not found
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    # Load the TOML file
    with open(config_path, "r") as f:
        doc = tomlkit.load(f)

    # Check if remotes section exists and remote exists
    if "remotes" not in doc or name not in doc["remotes"]:
        raise ValueError(f"Remote '{name}' not found")

    # Remove the remote
    del doc["remotes"][name]

    # If remotes is now empty, remove the entire remotes section
    if len(doc["remotes"]) == 0:
        del doc["remotes"]

    # Atomic write: write to tmp file first, then replace
    tmp_path = config_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        tomlkit.dump(doc, f)

    tmp_path.replace(config_path)


def test_remote(config: ProjectConfig, name: str) -> TestResult:
    """Test SSH connectivity to a remote.

    Args:
        config: ProjectConfig instance
        name: Remote name to test

    Returns:
        TestResult with success status and latency/error

    Raises:
        ValueError: If remote name not found
    """
    if name not in config.remotes:
        raise ValueError(f"Remote '{name}' not found")

    remote_config = config.remotes[name]
    host = remote_config["host"]

    # Build SSH command
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        host,
        "echo", "ok"
    ]

    try:
        # Time the SSH call
        start_time = time.time()
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10
        )
        elapsed_ms = (time.time() - start_time) * 1000

        # Check if successful
        if result.returncode == 0 and result.stdout.strip() == "ok":
            return TestResult(success=True, latency_ms=elapsed_ms, error="")
        else:
            # Return stderr if available, otherwise stdout
            error_msg = result.stderr.strip() or result.stdout.strip()
            return TestResult(success=False, latency_ms=None, error=error_msg)

    except subprocess.TimeoutExpired:
        return TestResult(
            success=False,
            latency_ms=None,
            error="Connection timed out after 10s"
        )
