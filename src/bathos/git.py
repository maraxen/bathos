from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GitState:
    hash: str
    branch: str
    dirty: bool
    dirty_content_id: str | None = None
    provenance_source: str = "git"   # "git" | "myxcel-env" | "myxcel-sidecar" | "none"


_UNKNOWN = GitState(hash="unknown", branch="unknown", dirty=False, provenance_source="none")


def _env_channel(cwd: str | Path) -> dict | None:
    """Read provenance from MYXCEL_PROVENANCE_SCHEMA env vars.

    Returns None if the schema var is absent. Empty string always means null/None.
    Per D5: ignored if cwd is not inside MYXCEL_PROVENANCE_ROOT.
    """
    if "MYXCEL_PROVENANCE_SCHEMA" not in os.environ:
        return None

    # Check D5 guard: cwd must be inside provenance root. Fail CLOSED (reject the channel)
    # when the root is missing/empty -- an unresolvable root can't be verified as covering
    # cwd, so silently accepting would be the unsafe direction (the same posture used
    # everywhere else in this design: malformed sidecar records are rejected, _same_root
    # treats ambiguity as "not same").
    root_str = os.environ.get("MYXCEL_PROVENANCE_ROOT", "")
    if not root_str:
        return None
    try:
        cwd_path = Path(cwd).resolve()
        root_path = Path(root_str).resolve()
        if not cwd_path.is_relative_to(root_path):
            return None
    except (ValueError, OSError):
        return None

    # Read the env channel fields
    return {
        "provenance_status": os.environ.get("MYXCEL_PROVENANCE_STATUS", ""),
        "git_sha": os.environ.get("MYXCEL_GIT_SHA", "") or None,
        "git_branch": os.environ.get("MYXCEL_GIT_BRANCH", "") or None,
        "git_dirty": os.environ.get("MYXCEL_GIT_DIRTY", ""),
        "dirty_content_id": os.environ.get("MYXCEL_GIT_DIRTY_CONTENT_ID", "") or None,
        "root": root_str or None,
    }


def _sidecar_channel(cwd: str | Path) -> dict | None:
    """Ascend from cwd up to 8 levels looking for .myxcel_provenance.json or .git.

    Stops at the first .git (file or directory) or PROVENANCE_FILENAME.
    Returns parsed sidecar record or None if not found.
    """
    cwd_path = Path(cwd).resolve()
    current = cwd_path

    for _ in range(8):
        # Check for .git first (file or directory — worktree layout)
        git_path = current / ".git"
        if git_path.exists():
            return None

        # Check for .myxcel_provenance.json
        sidecar_path = current / ".myxcel_provenance.json"
        if sidecar_path.exists():
            try:
                data = json.loads(sidecar_path.read_text())
                # Validate: must have provenance_status and schema_version must be int
                if (
                    "provenance_status" not in data
                    or "schema_version" not in data
                    or not isinstance(data.get("schema_version"), int)
                ):
                    return None
                # Forward compatibility: accept unknown schema_version with a warning
                if data.get("schema_version", 0) > 1:
                    # In a real implementation, this would be a one-time warning
                    # For now just accept it
                    pass
                return {
                    "provenance_status": data.get("provenance_status"),
                    "git_sha": data.get("git_sha"),
                    "git_branch": data.get("git_branch"),
                    "git_dirty": data.get("git_dirty"),
                    "dirty_content_id": data.get("dirty_content_id"),
                    "root": data.get("provenance_root"),
                }
            except (json.JSONDecodeError, OSError):
                return None

        # Stop at filesystem root
        parent = current.parent
        if parent == current:
            break
        current = parent

    return None


def _same_root(a: str | Path, b: str | Path) -> bool:
    """True iff a and b denote the same directory. Never raises."""
    try:
        if Path(a).samefile(b):
            return True
    except (OSError, ValueError):
        pass
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except (OSError, ValueError):
        return False


def _legacy_git_shellout(cwd: str | Path) -> GitState | None:
    """Original capture_git_state behavior. Returns None on any failure."""
    try:
        hash_ = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=cwd, text=True, stderr=subprocess.DEVNULL
        ).strip()
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=cwd,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
        return GitState(hash=hash_, branch=branch, dirty=dirty, provenance_source="git")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _git_shellout_with_toplevel(cwd: str | Path) -> tuple[str, str, bool, str] | None:
    """Git shellout returning (hash, branch, dirty, toplevel) or None on failure."""
    try:
        hash_ = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=cwd, text=True, stderr=subprocess.DEVNULL
        ).strip()
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=cwd,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
        toplevel = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return (hash_, branch, dirty, toplevel)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def capture_git_state(cwd: Path = Path.cwd()) -> GitState:
    """Capture git provenance from multiple channels with defined precedence.

    D6 precedence:
    1. Check env and sidecar channels; if neither present, use legacy git or _UNKNOWN
    2. If a channel exists and a real repo exists at the same root, use the real repo
    3. Otherwise use the channel

    Never raises (C6).
    """
    try:
        cwd_str = str(cwd)

        # Try env and sidecar channels, tracking which one actually supplied `prov` --
        # re-deriving the source later from ambient os.environ presence (rather than which
        # call returned non-None) mislabels the sidecar as "myxcel-env" whenever D5 rejects
        # the env channel (cwd outside MYXCEL_PROVENANCE_ROOT) while MYXCEL_PROVENANCE_SCHEMA
        # is still set in the environment.
        env_prov = _env_channel(cwd_str)
        if env_prov is not None:
            prov = env_prov
            prov_source = "myxcel-env"
        else:
            prov = _sidecar_channel(cwd_str)
            prov_source = "myxcel-sidecar"

        if prov is None:
            # No channel: use legacy shellout
            result = _legacy_git_shellout(cwd_str)
            return result if result is not None else _UNKNOWN

        # We have a channel. Check if a real repo exists at the same root.
        git_info = _git_shellout_with_toplevel(cwd_str)
        if git_info is not None:
            hash_, branch, dirty, toplevel = git_info
            if _same_root(toplevel, prov.get("root") or ""):
                # Real repo at same root wins. dirty_content_id is intentionally NOT
                # borrowed from `prov` here -- prov may describe an earlier (possibly
                # drifted) tree state than the one this fresh shellout just observed, so
                # pairing a fresh dirty flag with a stale content id would be internally
                # inconsistent (D3's invariant). Bathos has no local equivalent of
                # myxcel's tree-OID algorithm to compute a fresh one, so None (unknown) is
                # the safe value here, not a guess.
                return GitState(
                    hash=hash_,
                    branch=branch,
                    dirty=dirty,
                    dirty_content_id=None,
                    provenance_source="git",
                )

        # Use the channel
        status = prov.get("provenance_status", "")
        git_sha = prov.get("git_sha")
        git_branch = prov.get("git_branch")
        git_dirty_value = prov.get("git_dirty", "")
        # Handle both string ("1"/"0"/empty) and boolean (from sidecar JSON)
        if isinstance(git_dirty_value, bool):
            dirty = git_dirty_value
        elif isinstance(git_dirty_value, str):
            dirty = git_dirty_value == "1" if git_dirty_value else False
        else:
            dirty = False

        # Map status to hash
        if status == "nogit":
            git_sha = "nogit"
        elif status == "unavailable" or not git_sha:
            git_sha = "unknown"

        return GitState(
            hash=git_sha or "unknown",
            branch=git_branch or "unknown",
            dirty=dirty,
            dirty_content_id=prov.get("dirty_content_id"),
            provenance_source=prov_source,
        )
    except Exception:
        # Never raise (C6)
        return _UNKNOWN


def paths_changed_since(sha: str, paths: list[str], cwd: Path = Path.cwd()) -> bool:
    """True if any of `paths` differs between `sha` and the working tree.

    Compares the given commit against the index+worktree (no --cached), so this
    also catches uncommitted edits, not just committed-since-sha changes.

    Fail-safe: a blank sha/paths, or any git error (unknown sha, not a repo),
    counts as "changed" — never silently reports unchanged on an error.
    """
    if not sha or not paths:
        return True
    try:
        result = subprocess.run(
            ["git", "diff", "--quiet", sha, "--", *paths],
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return True
    return result.returncode != 0
