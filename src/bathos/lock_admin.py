"""`bth lock`: administer sidecar locks stored as commit refs (spec 261026 P3).

Five commands over ``refs/bathos/locks/<run_id>``: ``materialize`` (write a ref's lock to its G19 path so the owner can
commit it), ``relocate`` (classify in-tree locks and make sure each has its ref; never deletes), ``untrack``
(``git rm --cached`` for tracked bookkeeping only), ``push`` and ``fetch`` (explicit refspec, so locks can travel).

Conventions shared by all of them: dry-run unless ``apply=True``; a dirty index is refused when a command would stage;
repeating a command changes nothing. Failures raise :class:`LockAdminError` carrying a ``BathosErrorCode``.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import subprocess
import tomllib
from pathlib import Path

from cisternal.provenance.capture import clean_git_env

from bathos.errors import BathosErrorCode
from bathos.git_pin import MANIFEST_RELPATH, ensure_bookkeeping_ignored
from bathos.lock_ref import LOCK_REF_PREFIX, LockRefError, lock_ref_name, write_lock_ref

_GIT_TIMEOUT = 120.0
LOCK_REFSPEC = f"{LOCK_REF_PREFIX}/*:{LOCK_REF_PREFIX}/*"
_LOCK_SUFFIX = ".bth.lock.toml"
# Tracked bookkeeping that `untrack` may remove from the index. Lock files are deliberately absent.
UNTRACKABLE = (MANIFEST_RELPATH.as_posix(), (MANIFEST_RELPATH.parent / ".manifest.lock").as_posix())

UNTRACK_CAVEATS = (
    "G31: after the deletion is committed, fresh checkouts and worktrees start with an empty manifest.jsonl, so runs "
    "whose manifest lines exist only in older checkouts fail clean-run proof (SNAPSHOT_UNPROVEN).",
    "Merging: other checkouts that modified the file hit a modify/delete conflict on merge. Recipe: "
    "cp .bth/refs/manifest.jsonl /tmp/keep && git checkout -- .bth/refs/manifest.jsonl && git merge ... && "
    "mv /tmp/keep .bth/refs/manifest.jsonl",
    "The on-disk file is never touched, and lock files are never untracked.",
)


class LockAdminError(Exception):
    """A `bth lock` command was refused or failed. ``code`` is the BathosErrorCode to report."""

    def __init__(self, code: BathosErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _git(
    root: Path, *args: str, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        env=clean_git_env(),
        input=input_bytes,
        timeout=_GIT_TIMEOUT,
    )


def _text(proc: subprocess.CompletedProcess[bytes]) -> str:
    return (proc.stdout + proc.stderr).decode(errors="replace").strip()


def repo_toplevel(project_root: Path) -> Path:
    """The git toplevel of ``project_root``; ``INVALID_PARAM`` when it is not inside a work tree."""
    try:
        proc = _git(project_root, "rev-parse", "--show-toplevel")
    except OSError as e:
        raise LockAdminError(
            BathosErrorCode.INVALID_PARAM, f"cannot run git in {project_root}: {e}"
        ) from e
    if proc.returncode != 0 or not proc.stdout.strip():
        raise LockAdminError(
            BathosErrorCode.INVALID_PARAM, f"{project_root} is not inside a git work tree"
        )
    return Path(proc.stdout.decode().strip())


def _require_clean_index(root: Path) -> None:
    if _git(root, "diff", "--cached", "--quiet").returncode != 0:
        raise LockAdminError(
            BathosErrorCode.INDEX_DIRTY,
            "the index has staged changes; commit or unstage them first (this command would stage)",
        )


def _ref_exists(root: Path, ref: str) -> bool:
    return _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0


def _blob(root: Path, ref: str, relpath: str) -> bytes | None:
    proc = _git(root, "cat-file", "blob", f"{ref}:{relpath}")
    return proc.stdout if proc.returncode == 0 else None


def _lock_relpath_in_ref(root: Path, run_id: str) -> str | None:
    """The lock's path inside ``refs/bathos/locks/<run_id>``: the one entry named ``*.<run_id>.bth.lock.toml``."""
    ref = lock_ref_name(run_id)
    if not _ref_exists(root, ref):
        return None
    listing = _git(root, "ls-tree", "-r", "--name-only", "-z", ref)
    if listing.returncode != 0:
        return None
    suffix = f".{run_id}{_LOCK_SUFFIX}"
    names = [n for n in listing.stdout.decode().split("\0") if n.endswith(suffix)]
    return names[0] if len(names) == 1 else None


def lock_materialize(project_root: Path, run_id: str, apply: bool = False) -> dict:
    """Write ``refs/bathos/locks/<run_id>``'s lock to its G19 path, byte-identical to the ref's blob.

    The owner then commits it, so praxia G19/G31 see it unchanged. Never overwrites a file with different
    content (``INVALID_PARAM``). Does not stage anything, so no index check.
    """
    root = repo_toplevel(project_root)
    relpath = _lock_relpath_in_ref(root, run_id)
    if relpath is None:
        raise LockAdminError(
            BathosErrorCode.LOCK_NOT_FOUND, f"no lock ref {lock_ref_name(run_id)} in {root}"
        )
    body = _blob(root, lock_ref_name(run_id), relpath)
    if body is None:
        raise LockAdminError(
            BathosErrorCode.LOCK_NOT_FOUND, f"{lock_ref_name(run_id)} has no blob at {relpath}"
        )
    target = root / relpath
    present = target.is_file()
    if present and target.read_bytes() != body:
        raise LockAdminError(
            BathosErrorCode.INVALID_PARAM,
            f"{relpath} already exists with different content; not overwriting",
        )
    result = {
        "apply": apply,
        "run_id": run_id,
        "relpath": relpath,
        "path": str(target),
        "sha256": hashlib.sha256(body).hexdigest(),
        "already_present": present,
        "would_write": not present,
        "written": False,
    }
    if apply and not present:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        result["written"] = True
    return result


def _run_id_of(relpath: str) -> str | None:
    name = Path(relpath).name
    if not name.endswith(_LOCK_SUFFIX):
        return None
    head = name[: -len(_LOCK_SUFFIX)]
    run_id = head.rsplit(".", 1)[-1]
    return run_id if re.fullmatch(r"[0-9A-Za-z-]+", run_id) and "." in head else None


def _catalog_sha(catalog_dir: Path | None, run_id: str) -> str | None:
    if catalog_dir is None:
        return None
    try:
        from bathos.query import get_run

        run = get_run(run_id, catalog_dir, allow_cool_fallback=True)
    except Exception:
        return None
    return (run.manifest_sha256 or None) if run is not None else None


def lock_relocate(project_root: Path, catalog_dir: Path | None = None, apply: bool = False) -> dict:
    """Classify in-tree locks and, on apply, make sure each has ``refs/bathos/locks/<run_id>`` holding that blob.

    * **Tracked** locks are ``keep (committed evidence)``: never deletion candidates (praxia G19 reads them).
    * **Untracked** locks whose sha256 equals a catalog run's ``manifest_sha256`` are ``safe to delete``.
    * Any other untracked lock is ``unmatched`` and is left alone.

    Never deletes anything. An existing ref holding a different blob is reported as ``ref_conflict`` and not
    replaced.
    """
    root = repo_toplevel(project_root)
    tracked = {
        n
        for n in _git(root, "ls-files", "-z", "--", f"*{_LOCK_SUFFIX}").stdout.decode().split("\0")
        if n
    }
    untracked = {
        n
        for n in _git(
            root, "ls-files", "-z", "--others", "--exclude-standard", "--", f"*{_LOCK_SUFFIX}"
        )
        .stdout.decode()
        .split("\0")
        if n
    }
    entries: list[dict] = []
    for relpath in sorted(tracked | untracked):
        is_tracked = relpath in tracked
        entry: dict = {"relpath": relpath, "tracked": is_tracked, "run_id": _run_id_of(relpath)}
        path = root / relpath
        try:
            body = path.read_bytes()
        except OSError:
            entry.update(action="unreadable", ref="skipped")
            entries.append(entry)
            continue
        sha = hashlib.sha256(body).hexdigest()
        entry["sha256"] = sha
        run_id = entry["run_id"]
        if is_tracked:
            entry["action"] = "keep (committed evidence)"
        elif run_id and _catalog_sha(catalog_dir, run_id) == sha:
            entry["action"] = "safe to delete"
        else:
            entry["action"] = "unmatched"
        entry["ref"] = _ensure_ref(root, run_id, relpath, body, apply)
        entries.append(entry)
    return {
        "apply": apply,
        "locks": entries,
        "safe_to_delete": [e["relpath"] for e in entries if e["action"] == "safe to delete"],
        "kept": [e["relpath"] for e in entries if e["action"].startswith("keep")],
        "deleted": [],
    }


def _ensure_ref(root: Path, run_id: str | None, relpath: str, body: bytes, apply: bool) -> str:
    if not run_id:
        return "skipped (no run id in name)"
    ref = lock_ref_name(run_id)
    if _ref_exists(root, ref):
        existing = _blob(root, ref, relpath)
        return "present" if existing == body else "ref_conflict"
    if not apply:
        return "would create"
    parsed: dict = {}
    with contextlib.suppress(UnicodeDecodeError, tomllib.TOMLDecodeError):
        parsed = tomllib.loads(body.decode("utf-8")).get("manifest", {})
    git_sha = str(parsed.get("git_sha", ""))
    try:
        write_lock_ref(root, git_sha, run_id, relpath, body)
    except (LockRefError, OSError, subprocess.SubprocessError) as e:
        raise LockAdminError(BathosErrorCode.LOCK_REF_WRITE_FAILED, f"{ref}: {e}") from e
    return "created"


def lock_untrack(project_root: Path, apply: bool = False) -> dict:
    """``git rm --cached`` the tracked ``manifest.jsonl`` / ``.manifest.lock`` only, then re-run the ignores.

    The on-disk files are never touched and lock files are excluded. The result is a staged deletion for the
    owner to commit. Dry-run and apply both return the G31 and merge caveats. Refuses a dirty index on apply.
    """
    root = repo_toplevel(project_root)
    tracked = [
        p for p in UNTRACKABLE if _git(root, "ls-files", "--error-unmatch", "--", p).returncode == 0
    ]
    result: dict = {
        "apply": apply,
        "tracked": tracked,
        "untracked_paths": [],
        "ignored": {},
        "caveats": list(UNTRACK_CAVEATS),
    }
    if not apply:
        return result
    if tracked:
        _require_clean_index(root)
        proc = _git(root, "rm", "--cached", "-q", "--", *tracked)
        if proc.returncode != 0:
            raise LockAdminError(BathosErrorCode.INTERNAL, f"git rm --cached failed: {_text(proc)}")
        result["untracked_paths"] = tracked
    result["ignored"] = ensure_bookkeeping_ignored(root)
    return result


def _remote_check(root: Path, remote: str) -> None:
    if _git(root, "remote", "get-url", remote).returncode != 0:
        raise LockAdminError(BathosErrorCode.INVALID_PARAM, f"no git remote named {remote!r}")


def lock_push(project_root: Path, remote: str = "origin", apply: bool = False) -> dict:
    """Push ``refs/bathos/locks/*`` to ``remote`` with an explicit refspec (dry-run unless ``apply``)."""
    root = repo_toplevel(project_root)
    _remote_check(root, remote)
    args = ["push", *([] if apply else ["--dry-run"]), remote, LOCK_REFSPEC]
    proc = _git(root, *args)
    if proc.returncode != 0:
        raise LockAdminError(BathosErrorCode.INVALID_PARAM, f"git push failed: {_text(proc)}")
    return {"apply": apply, "remote": remote, "refspec": LOCK_REFSPEC, "output": _text(proc)}


def lock_fetch(project_root: Path, remote: str = "origin", apply: bool = False) -> dict:
    """Fetch ``refs/bathos/locks/*`` from ``remote`` (dry-run lists them with ``ls-remote``)."""
    root = repo_toplevel(project_root)
    _remote_check(root, remote)
    if apply:
        proc = _git(root, "fetch", remote, LOCK_REFSPEC)
    else:
        proc = _git(root, "ls-remote", remote, f"{LOCK_REF_PREFIX}/*")
    if proc.returncode != 0:
        raise LockAdminError(BathosErrorCode.INVALID_PARAM, f"git fetch failed: {_text(proc)}")
    return {"apply": apply, "remote": remote, "refspec": LOCK_REFSPEC, "output": _text(proc)}
