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
    # Create-only: the old value is the zero oid, so an existing ref is never overwritten. Run ids are fresh uuids, so
    # a collision means something is wrong (a rerun of the same id, a forged ref); failing back to the lock_dir
    # fallback is safer than silently replacing evidence. (cisternal's update_ref takes no old value, so the call is
    # made directly.) sha256-object-format repos have 64-char oids, so this also fails there and falls back.
    created = _git("update-ref", ref, commit, "0" * 40, cwd=root)
    if created.returncode != 0:
        raise LockRefError(
            f"could not create {ref} -> {commit}: {created.stderr.decode(errors='replace').strip()}"
        )
    back = _git("cat-file", "blob", f"{ref}:{relpath}", cwd=root)
    if (
        back.returncode != 0
        or hashlib.sha256(back.stdout).hexdigest() != hashlib.sha256(body).hexdigest()
    ):
        # Do not leave a ref that holds the wrong bytes: delete it, but only if it still points at our commit.
        _git("update-ref", "-d", ref, commit, cwd=root)
        raise LockRefError(f"read-back of {ref}:{relpath} did not match the written body")
    return LockRefResult(
        uri=lock_uri(_common_dir(root), run_id, relpath),
        ref=ref,
        commit=commit,
        parentless=not has_parent,
    )


def _cat_blob(git_dir: Path, ref: str, relpath: str) -> bytes | None:
    proc = _git("--git-dir", str(git_dir), "cat-file", "blob", f"{ref}:{relpath}", cwd=Path.cwd())
    return proc.stdout if proc.returncode == 0 else None


def _repo_near(path: Path) -> tuple[Path, Path] | None:
    """``(common_dir, toplevel)`` of the repo holding the nearest EXISTING ancestor of ``path``, else None."""
    probe = path
    while not probe.exists():
        if probe.parent == probe:
            return None
        probe = probe.parent
    if probe.is_file():
        probe = probe.parent
    try:
        top = _git("rev-parse", "--show-toplevel", cwd=probe)
    except OSError:
        return None
    if top.returncode != 0 or not top.stdout.strip():
        return None
    root = Path(top.stdout.decode().strip())
    try:
        return _common_dir(root), root
    except (LockRefError, OSError):
        return None


@dataclass(frozen=True)
class LockLookup:
    """Result of locating a run's lock: its bytes, or why none was found.

    ``reason`` is ``ok``, ``lock_unlocatable`` (no repo could be found to look in) or ``lock_not_found``
    (a repo was found but holds neither the file nor the ref).
    """

    data: bytes | None
    reason: str
    source: str = ""


def locate_lock_bytes(
    manifest_path: str, run_id: str | None = None, sidecar_path: str = ""
) -> LockLookup:
    """Find a run's lock in any of the three forms of ``manifest_path`` (spec 261026 P4).

    1. ``git:`` URI: read from its common dir; if that dir is absent here (a record synced from another host) or
       lacks the ref, retry ``refs/bathos/locks/<run_id>:<relpath from the URI>`` in the repo holding the nearest
       existing ancestor of ``sidecar_path`` (``Run`` has no script path, B15).
    2. A plain path that exists: read it.
    3. A plain path that is missing: locate the repo from the nearest existing ancestor of the path and try
       ``refs/bathos/locks/<run_id>:<path relative to that repo's root>``. This verifies records relocated by
       ``bth lock relocate`` after their files are deleted.
    """
    if manifest_path.startswith(URI_SCHEME):
        common, ref, relpath = parse_lock_uri(manifest_path, run_id)
        if common.is_dir():
            data = _cat_blob(common, ref, relpath)
            if data is not None:
                return LockLookup(data, "ok", f"uri:{common}")
        found = _repo_near(Path(sidecar_path)) if sidecar_path else None
        if found is None:
            return LockLookup(None, "lock_not_found" if common.is_dir() else "lock_unlocatable")
        data = _cat_blob(found[0], ref, relpath)
        if data is None:
            return LockLookup(None, "lock_not_found")
        return LockLookup(data, "ok", f"sidecar-repo:{found[0]}")

    path = Path(manifest_path)
    try:
        if path.is_file():
            return LockLookup(path.read_bytes(), "ok", f"file:{path}")
    except OSError:
        pass
    found = _repo_near(path)
    if found is None or run_id is None:
        return LockLookup(None, "lock_unlocatable")
    common, root = found
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        rel = (probe.resolve() / path.relative_to(probe)).relative_to(root.resolve()).as_posix()
    except ValueError:
        return LockLookup(None, "lock_unlocatable")
    data = _cat_blob(common, lock_ref_name(run_id), rel)
    if data is None:
        return LockLookup(None, "lock_not_found")
    return LockLookup(data, "ok", f"relocated:{common}")


def read_lock_bytes(manifest_path: str, run_id: str | None = None, sidecar_path: str = "") -> bytes:
    """Bytes of a lock named by ``run.manifest_path`` in any form (see :func:`locate_lock_bytes`).

    Uses ``cat-file blob``, never text-mode ``show``. Raises ``FileNotFoundError`` when it cannot be found.
    """
    lookup = locate_lock_bytes(manifest_path, run_id, sidecar_path)
    if lookup.data is None:
        raise FileNotFoundError(f"{manifest_path}: {lookup.reason}")
    return lookup.data


def read_lock_text(manifest_path: str, run_id: str | None = None, sidecar_path: str = "") -> str:
    """The single text resolver for ``run.manifest_path`` in any form (``git:`` URI or plain path)."""
    return read_lock_bytes(manifest_path, run_id, sidecar_path).decode("utf-8")
