"""Per-run sidecar lock stored as a commit under ``refs/bathos/locks/<run_id>`` (spec 261026, P1/P4).

A run's lock used to be a new untracked file beside the sidecar. In ``lock_location = "git"`` mode it is instead
a *lock carrier* commit: ``HEAD``'s tree plus the lock at its G19 path (``<sidecar dir>/<stem>.<run_id>.bth.lock.toml``),
parented on the run's ``git_hash`` (parentless when that commit is absent, as in a shallow clone). It is built with a
temporary per-run index, so no shared state and no working-tree write; it is not a WIP snapshot.

``run.manifest_path`` then holds ``git:<common-dir>#refs/bathos/locks/<run_id>:<relpath>``. The URI is parsed by the
fixed marker ``#refs/bathos/locks/<run_id>:`` and never split on ``#`` or ``:``, since the common dir may contain
both. ``read_lock_text`` is the single reader for either form of ``manifest_path``.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cisternal.provenance.capture import clean_git_env

from bathos.git_pin import update_ref

LOCK_REF_PREFIX = "refs/bathos/locks"
URI_SCHEME = "git:"
_GIT_TIMEOUT = 60.0
# commit-tree refuses to run without an identity, and a run must not fail because none is configured.
_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "bathos-lock",
    "GIT_AUTHOR_EMAIL": "bathos-lock@localhost",
    "GIT_COMMITTER_NAME": "bathos-lock",
    "GIT_COMMITTER_EMAIL": "bathos-lock@localhost",
}
_MARKER_RE = re.compile(r"#refs/bathos/locks/([^:/#]+):")


class LockRefError(RuntimeError):
    """The lock commit-ref could not be written or did not read back identically."""


@dataclass(frozen=True)
class LockRefResult:
    uri: str
    ref: str
    commit: str
    parentless: bool


def _git(
    *args: str, cwd: Path, env_extra: dict[str, str] | None = None, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    # The temp index is set AFTER clean_git_env, which strips GIT_INDEX_FILE.
    env = {**clean_git_env(), **(env_extra or {})}
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        env=env,
        input=input_bytes,
        timeout=_GIT_TIMEOUT,
    )


def _ok(proc: subprocess.CompletedProcess[bytes], what: str) -> str:
    if proc.returncode != 0:
        raise LockRefError(f"{what} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout.decode().strip()


def lock_ref_name(run_id: str) -> str:
    return f"{LOCK_REF_PREFIX}/{run_id}"


def lock_uri(common_dir: Path | str, run_id: str, relpath: str) -> str:
    return f"{URI_SCHEME}{common_dir}#{lock_ref_name(run_id)}:{relpath}"


def parse_lock_uri(uri: str, run_id: str | None = None) -> tuple[Path, str, str]:
    """``(common_dir, ref, relpath)`` of a ``git:`` URI, parsed by the fixed run-id marker.

    With ``run_id`` the marker is exact and the last occurrence wins (the common dir may hold ``#``). Without it
    the last ``#refs/bathos/locks/<id>:`` match is taken. Raises ``ValueError`` when the marker is absent.
    """
    if not uri.startswith(URI_SCHEME):
        raise ValueError(f"not a git: lock URI: {uri!r}")
    body = uri[len(URI_SCHEME) :]
    if run_id is not None:
        marker = f"#{lock_ref_name(run_id)}:"
        at = body.rfind(marker)
        if at < 0:
            raise ValueError(f"lock URI {uri!r} lacks the marker {marker!r}")
        return Path(body[:at]), lock_ref_name(run_id), body[at + len(marker) :]
    matches = list(_MARKER_RE.finditer(body))
    if not matches:
        raise ValueError(f"lock URI {uri!r} lacks a #{LOCK_REF_PREFIX}/<run_id>: marker")
    m = matches[-1]
    return Path(body[: m.start()]), lock_ref_name(m.group(1)), body[m.end() :]


def _common_dir(root: Path) -> Path:
    absolute = _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=root)
    if absolute.returncode == 0 and absolute.stdout.strip():
        return Path(absolute.stdout.decode().strip())
    # git < 2.31 (B14): no --path-format; the relative answer is relative to the root.
    relative = Path(
        _ok(_git("rev-parse", "--git-common-dir", cwd=root), "rev-parse --git-common-dir")
    )
    return (relative if relative.is_absolute() else root / relative).resolve()


def write_lock_ref(
    root: Path, git_hash: str, run_id: str, relpath: str, body: bytes
) -> LockRefResult:
    """Write the lock carrier commit, point the ref at it, and read it back byte-for-byte.

    Raises :class:`LockRefError` on any failure, including a read-back whose sha256 differs from ``body``.
    """
    has_parent = _git("cat-file", "-e", f"{git_hash}^{{commit}}", cwd=root).returncode == 0
    with tempfile.TemporaryDirectory(prefix="bathos-lock-index-") as tmp:
        idx = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if has_parent:
            _ok(_git("read-tree", git_hash, cwd=root, env_extra=idx), "read-tree")
        else:
            _ok(_git("read-tree", "--empty", cwd=root, env_extra=idx), "read-tree --empty")
        blob = _ok(_git("hash-object", "-w", "--stdin", cwd=root, input_bytes=body), "hash-object")
        _ok(
            _git(
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob},{relpath}",
                cwd=root,
                env_extra=idx,
            ),
            "update-index",
        )
        tree = _ok(_git("write-tree", cwd=root, env_extra=idx), "write-tree")
        parent_args = ["-p", git_hash] if has_parent else []
        commit = _ok(
            _git(
                "commit-tree",
                tree,
                *parent_args,
                "-m",
                f"bathos lock {run_id}",
                cwd=root,
                env_extra={**idx, **_IDENTITY_ENV},
            ),
            "commit-tree",
        )
    ref = lock_ref_name(run_id)
    if not update_ref(ref, commit, root, root=root):
        raise LockRefError(f"could not create {ref} -> {commit}")
    back = _git("cat-file", "blob", f"{ref}:{relpath}", cwd=root)
    if (
        back.returncode != 0
        or hashlib.sha256(back.stdout).hexdigest() != hashlib.sha256(body).hexdigest()
    ):
        raise LockRefError(f"read-back of {ref}:{relpath} did not match the written body")
    return LockRefResult(
        uri=lock_uri(_common_dir(root), run_id, relpath),
        ref=ref,
        commit=commit,
        parentless=not has_parent,
    )


def read_lock_bytes(manifest_path: str, run_id: str | None = None) -> bytes:
    """Bytes of a lock named by a ``git:`` URI or a plain path. ``cat-file blob``, never text-mode ``show``."""
    if manifest_path.startswith(URI_SCHEME):
        common, ref, relpath = parse_lock_uri(manifest_path, run_id)
        proc = _git(
            "--git-dir", str(common), "cat-file", "blob", f"{ref}:{relpath}", cwd=Path.cwd()
        )
        if proc.returncode != 0:
            raise FileNotFoundError(
                f"{manifest_path}: {proc.stderr.decode(errors='replace').strip()}"
            )
        return proc.stdout
    return Path(manifest_path).read_bytes()


def read_lock_text(manifest_path: str, run_id: str | None = None) -> str:
    """The single text resolver for ``run.manifest_path`` in either form (``git:`` URI or plain path)."""
    return read_lock_bytes(manifest_path, run_id).decode("utf-8")
