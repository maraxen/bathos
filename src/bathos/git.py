from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import subprocess


@dataclass
class GitState:
    hash: str
    branch: str
    dirty: bool


_UNKNOWN = GitState(hash="unknown", branch="unknown", dirty=False)


def capture_git_state(cwd: Path = Path.cwd()) -> GitState:
    try:
        hash_ = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=cwd, text=True, stderr=subprocess.DEVNULL
        ).strip()
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, text=True, stderr=subprocess.DEVNULL,
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=cwd, text=True, stderr=subprocess.DEVNULL,
            ).strip()
        )
        return GitState(hash=hash_, branch=branch, dirty=dirty)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return _UNKNOWN
