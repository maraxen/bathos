"""``bth warm``: restore a run's reference set from a verified cold/mirror copy into a new local directory
(spec 261023_output-tiering.md sections 3.2, 4, 5.2, 5.3, 7 and 9 row 7).

The order is the spec's, and every step is a gate for the next:

1. the target must not exist (``warm_target_exists``) and its parent must be a directory; no myxcel call is made
   before this;
2. take the first ``verified``, non-superseded cold/mirror copy of the run (cold before mirror) and fetch its
   manifest and archive with ``myxcel cold get`` into a bathos-owned temporary directory;
3. :func:`bathos.output_tiers.archive.verify_archive` checks the manifest's sha256 against the copy record and the
   archive's sha256 and size against the manifest BEFORE it opens the tar, then streams the tar once: the member
   set must equal the manifest exactly, with no absolute, ``..``, link, device or directory member;
4. the manifest's file set must equal ``reference_hashes`` exactly (the build asserts the same, section 5.2);
5. extract with ``tarfile`` ``filter="data"`` into the sibling ``<dir>.bth-warm-<attempt8>.partial/`` (never inside
   the target), then hash every extracted file against the manifest AND ``reference_hashes``;
6. publish with ``renameat2(..., RENAME_NOREPLACE)`` through ``ctypes``. A plain ``os.rename`` would replace an
   empty ``<dir>`` created after step 1 (review cycle 2, m3).
7. emit ``output_copy.recorded`` with ``tier = warm``.

Only bathos's own staging directory is ever deleted, and only on a refusal or failure (never a published target).

Interpretations of the spec, each flagged here and in the Step 7 report:

* ``EEXIST`` at the rename deletes bathos's own (verified) staging directory: section 4 keeps staging only for
  ``warm_noreplace_unsupported``, and says "bathos deletes only its own staging directory" for a mismatch. The
  content is re-fetchable and the cold copy is untouched.
* A rename failure other than ``EEXIST`` / ``EINVAL`` / ``ENOSYS`` / ``ENOTSUP`` (no space, permissions, a vanished
  parent) is ``warm_publish_failed``, a code the spec does not list, with the staging kept like the unsupported case.
* The manifest file set must equal ``reference_hashes`` (section 5.2 asserts this at build). A run whose reference
  set grew after the cold copy (a later ``adopt``) therefore refuses ``warm_mismatch`` until it is superseded.
* The fetched archive lives under ``$TMPDIR`` like ``bth cold``'s staging, not next to the target.
* ``--worktree-root`` is accepted for an imported run only, because its recorded root cannot derive relpaths
  (section 3.3). A standard run needs no live worktree to be warmed (it may be gone: "warm from cold instead"),
  and the event is stamped with the run's recorded root whether or not that directory still exists.
* The recorded warm row has ``profile = ""``, ``host`` = this machine's hostname, ``path`` = the absolute target,
  null archive/manifest hashes and size (section 5.3), and carries ``source_copy_id`` of the copy it came from.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import logging
import os
import secrets
import shutil
import socket
import stat
import tarfile
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import zstandard

from bathos.output_tiers import myxcel as mx
from bathos.output_tiers.adopt import _log_cwd
from bathos.output_tiers.archive import archive_name, manifest_name, verify_archive
from bathos.output_tiers.config import load_profile
from bathos.output_tiers.reference import (
    OutputTierError,
    ReferenceEntry,
    hash_file,
    reference_hashes,
)
from bathos.output_tiers.run_context import (
    INVALID_PARAM,
    RunContext,
    _resolve_root,
    open_run,
    require_log_mode,
)
from bathos.runlog.fold_output_copies import KIND_RECORDED, copy_id_for

logger = logging.getLogger(__name__)

WARM_TARGET_EXISTS = "warm_target_exists"
WARM_MISMATCH = "warm_mismatch"
NO_VERIFIED_COPY = "no_verified_copy"
WARM_NOREPLACE_UNSUPPORTED = "warm_noreplace_unsupported"
WARM_PUBLISH_FAILED = "warm_publish_failed"
NO_REFERENCE_HASHES = "no_reference_hashes"
MYXCEL_ERROR = "myxcel_error"

AT_FDCWD = -100
RENAME_NOREPLACE = 1

#: errno values that mean "this kernel or filesystem does not implement RENAME_NOREPLACE".
_UNSUPPORTED_ERRNOS = frozenset({errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP})


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --------------------------------------------------------------------------------------- the rename


def _renameat2_noreplace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """``renameat2(AT_FDCWD, src, AT_FDCWD, dst, RENAME_NOREPLACE)``. Raises :class:`OSError` carrying the
    kernel's errno (``FileExistsError`` for ``EEXIST``). A libc without ``renameat2`` (or no libc to load) raises
    ``OSError(ENOSYS)``."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.renameat2
    except (OSError, AttributeError) as e:
        raise OSError(errno.ENOSYS, f"renameat2 is not available: {e}") from None
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    ctypes.set_errno(0)
    rc = fn(AT_FDCWD, os.fsencode(src), AT_FDCWD, os.fsencode(dst), RENAME_NOREPLACE)
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), os.fsdecode(dst))


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------------------- the copy


def _first_verified_copy(catalog_dir: Path, run_id: str) -> dict[str, Any]:
    """The first ``verified``, non-superseded cold or mirror row of ``run_id`` (cold before mirror, then oldest
    first). Warm rows are never a source: they hold no archive."""
    from bathos.index import OUTPUT_COPIES_COLUMNS, connect_read

    con = connect_read(catalog_dir)
    try:
        cols = ", ".join(OUTPUT_COPIES_COLUMNS)
        row = con.execute(
            f"SELECT {cols} FROM output_copies WHERE run_id = ? AND state = 'verified' "  # noqa: S608
            "AND superseded_at IS NULL AND tier IN ('cold', 'mirror') "
            "ORDER BY (tier = 'cold') DESC, recorded_at, copy_id LIMIT 1",
            [run_id],
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise OutputTierError(
            NO_VERIFIED_COPY,
            f"run {run_id} has no verified, non-superseded cold or mirror copy; "
            "see `bth outputs where` and `bth verify --tier outputs`",
        )
    return dict(zip(OUTPUT_COPIES_COLUMNS, row, strict=True))


def _fetch(prof_name: str, rel: str, scratch: Path, label: str, runner: mx.Runner | None) -> Path:
    target = scratch / label
    target.mkdir()
    got = mx.get(prof_name, rel, str(target), runner=runner)
    if got.status != "ok" or not got.local_path:
        raise OutputTierError(
            MYXCEL_ERROR, f"cannot fetch {rel} from {prof_name}: {got.message or got.status}"
        )
    return Path(got.local_path)


def _describe(res: Any) -> str:
    parts: list[str] = []
    if res.detail:
        parts.append(res.detail)
    for label, names in (
        ("missing", res.missing),
        ("extra", res.extra),
        ("mismatched", res.mismatched),
    ):
        if names:
            parts.append(f"{label}: {', '.join(names[:5])}" + (" ..." if len(names) > 5 else ""))
    if res.unsafe:
        parts.append("unsafe: " + ", ".join(f"{n} ({why})" for n, why in res.unsafe[:5]))
    return "; ".join(parts) or "archive does not match its manifest"


def _check_manifest_against_reference(
    manifest_path: Path, reference: Mapping[str, ReferenceEntry]
) -> dict[str, dict[str, Any]]:
    """``{relpath: manifest file entry}``, after requiring the manifest's file set, sha256s and sizes to equal
    ``reference`` exactly. (``verify_archive`` has already parsed this manifest successfully.)"""
    files = {f["relpath"]: f for f in json.loads(manifest_path.read_bytes())["files"]}
    problems: list[str] = []
    for rel in sorted(set(reference) - set(files)):
        problems.append(f"{rel} (in the reference set, not in the manifest)")
    for rel in sorted(set(files) - set(reference)):
        problems.append(f"{rel} (in the manifest, not in the reference set)")
    for rel in sorted(set(files) & set(reference)):
        ref = reference[rel]
        if files[rel]["sha256"] != ref.sha256 or (
            ref.size_bytes is not None and files[rel]["size_bytes"] != ref.size_bytes
        ):
            problems.append(f"{rel} (manifest differs from the run's reference hash)")
    if problems:
        shown = "; ".join(problems[:5]) + (
            f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
        )
        raise OutputTierError(WARM_MISMATCH, f"manifest does not equal the reference set: {shown}")
    return files


def _extract(archive: Path, staging: Path) -> None:
    """Stream the (already verified) archive into ``staging`` with the ``data`` filter. Any extraction problem
    is a mismatch: the filter's refusals (absolute names, links, traversal) are the second line behind
    ``verify_archive``."""
    try:
        with (
            open(archive, "rb") as raw,
            zstandard.ZstdDecompressor().stream_reader(raw) as zr,
            tarfile.open(fileobj=zr, mode="r|") as tar,
        ):
            tar.extractall(staging, filter="data")
    except (tarfile.TarError, zstandard.ZstdError, EOFError) as e:
        raise OutputTierError(WARM_MISMATCH, f"extraction refused: {e}") from None


def _staged_files(staging: Path) -> dict[str, Path]:
    """``{relpath: path}`` of every file under ``staging``. A symlink or special file anywhere is a mismatch."""
    out: dict[str, Path] = {}
    stack = [staging]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as it:
            for entry in it:
                path = Path(entry.path)
                mode = os.lstat(path).st_mode
                if stat.S_ISDIR(mode):
                    stack.append(path)
                elif stat.S_ISREG(mode):
                    out[path.relative_to(staging).as_posix()] = path
                else:
                    raise OutputTierError(
                        WARM_MISMATCH,
                        f"{path.relative_to(staging).as_posix()} extracted as a link or special file",
                    )
    return out


def _check_staged(
    staging: Path,
    files: Mapping[str, Mapping[str, Any]],
    reference: Mapping[str, ReferenceEntry],
) -> int:
    """Hash every extracted file against the manifest AND ``reference_hashes``; the extracted set must be
    exactly the manifest's. Returns the byte total."""
    on_disk = _staged_files(staging)
    if set(on_disk) != set(files):
        diff = sorted(set(on_disk) ^ set(files))
        raise OutputTierError(
            WARM_MISMATCH, f"extracted file set differs from the manifest: {', '.join(diff[:5])}"
        )
    total = 0
    for rel in sorted(on_disk):
        path = on_disk[rel]
        sha = hash_file(path)
        size = path.stat().st_size
        meta = files[rel]
        if sha != meta["sha256"] or size != meta["size_bytes"]:
            raise OutputTierError(
                WARM_MISMATCH, f"{rel} (extracted file differs from the manifest)"
            )
        if sha != reference[rel].sha256:
            raise OutputTierError(
                WARM_MISMATCH, f"{rel} (extracted file differs from the reference hash)"
            )
        total += size
    return total


def _publish(staging: Path, target: Path) -> OutputTierError | None:
    """Rename ``staging`` to ``target`` without replacing anything. Returns ``None`` on success. Raises
    :class:`OutputTierError` when the caller should delete its staging directory (``warm_target_exists``).
    *Returns* the refusal when the staging directory must be kept for the owner (the kernel cannot do it, or the
    rename failed for another reason): the caller raises it after it has decided not to clean up."""
    try:
        _renameat2_noreplace(staging, target)
    except FileExistsError:
        raise OutputTierError(
            WARM_TARGET_EXISTS,
            f"{target} appeared while the copy was being checked; it was left untouched",
        ) from None
    except OSError as e:
        if e.errno == errno.EEXIST:
            raise OutputTierError(
                WARM_TARGET_EXISTS,
                f"{target} appeared while the copy was being checked; it was left untouched",
            ) from None
        if e.errno in _UNSUPPORTED_ERRNOS:
            return OutputTierError(
                WARM_NOREPLACE_UNSUPPORTED,
                f"this kernel or filesystem cannot rename without replacing ({e.strerror or e}); "
                f"the verified copy is kept at {staging} for you to move into place by hand",
            )
        return OutputTierError(
            WARM_PUBLISH_FAILED,
            f"cannot rename {staging} to {target}: {e.strerror or e}; "
            f"the verified copy is kept at {staging}",
        )
    return None


# --------------------------------------------------------------------------------------- entry point


def warm_outputs(
    catalog_dir: Path,
    run_prefix: str,
    to: str,
    *,
    worktree_root: str | None = None,
    runner: mx.Runner | None = None,
    config_dir: Path | None = None,
) -> dict[str, Any]:
    """``bth warm``: restore one run's reference set into the new directory ``to``. See the module docstring.

    Raises :class:`OutputTierError` (``warm_target_exists``, ``warm_mismatch``, ``no_verified_copy``,
    ``warm_noreplace_unsupported``, ``warm_publish_failed``, ``myxcel_error``, ``no_reference_hashes``,
    ``invalid_param``, ...), :class:`~bathos.cluster.MyxcelCapabilityGapError` for an old myxcel, and
    :class:`~bathos.output_tiers.config.OutputTiersConfigError` for a bad profile."""
    from bathos.runlog.emit import emit_event, unit_of_work

    if not to or not str(to).strip():
        raise OutputTierError(INVALID_PARAM, "--to <dir> is required")
    target = Path(os.path.abspath(os.path.expanduser(to)))
    require_log_mode(catalog_dir)

    with unit_of_work(catalog_dir):
        ctx: RunContext = open_run(catalog_dir, run_prefix, need_root=False)
        imported_root: str | None = None
        run_root: Path | None = None
        try:
            run_root, _source, imported_root = _resolve_root(ctx.events, worktree_root)
        except OutputTierError:
            if ctx.imported:
                raise  # worktree_unknown: an imported run's recorded root cannot derive relpaths
        if os.path.lexists(target):
            raise OutputTierError(
                WARM_TARGET_EXISTS, f"{target} already exists; warm writes a new directory only"
            )
        if not target.parent.is_dir():
            raise OutputTierError(INVALID_PARAM, f"the parent of {target} is not a directory")

        ref = reference_hashes(ctx.events, imported_root=imported_root)
        if not ref:
            raise OutputTierError(NO_REFERENCE_HASHES, ctx.run_id)
        copy = _first_verified_copy(catalog_dir, ctx.run_id)
        if not copy["manifest_sha256"]:
            raise OutputTierError(
                WARM_MISMATCH, f"copy {copy['copy_id']} records no manifest sha256"
            )
        prof = load_profile(str(copy["profile"]), config_dir)
        dest = prof.relative_destination(ctx.project_slug, ctx.run_id)
        mx.probe_cold_capability(runner)

        attempt = secrets.token_hex(4)
        staging = target.parent / f"{target.name}.bth-warm-{attempt}.partial"
        keep = False
        with tempfile.TemporaryDirectory(prefix="bth-warm-") as tmp:
            scratch = Path(tmp)
            manifest = _fetch(
                prof.name, f"{dest}/{manifest_name(ctx.run_id)}", scratch, "manifest", runner
            )
            archive = _fetch(
                prof.name, f"{dest}/{archive_name(ctx.run_id)}", scratch, "archive", runner
            )
            # Both sha256s are checked inside verify_archive before it opens the tar.
            verdict = verify_archive(archive, manifest, manifest_sha256=copy["manifest_sha256"])
            if not verdict.ok:
                raise OutputTierError(WARM_MISMATCH, _describe(verdict))
            files = _check_manifest_against_reference(manifest, ref)

            staging.mkdir()  # exclusive: a collision with a leftover is an error, never a merge
            try:
                _extract(archive, staging)
                total_bytes = _check_staged(staging, files, ref)
                _fsync_dir(staging)
                kept = _publish(staging, target)
                if kept is not None:
                    keep = True
                    raise kept
            finally:
                if not keep and staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
        with contextlib.suppress(OSError):
            _fsync_dir(target.parent)

        now = _now_iso()
        host = socket.gethostname()
        copy_id = copy_id_for(ctx.run_id, host, "", str(target))
        data = {
            "tier": "warm",
            "profile": "",
            "host": host,
            "path": str(target),
            "config_layer": "explicit --to",
            "archive_sha256": None,
            "manifest_sha256": None,
            "archive_size_bytes": None,
            "state": "verified",
            "verified_at": now,
            "recorded_at": now,
            "source_copy_id": copy["copy_id"],
            "source_tier": copy["tier"],
            "attempt": attempt,
            "n_files": len(files),
            "total_bytes": total_bytes,
            "project_slug": ctx.project_slug,
        }
        out = emit_event(
            kind=KIND_RECORDED,
            entity=[ctx.run_id, copy_id],
            data=data,
            cwd=_log_cwd(ctx, None),
            hard_fail=True,
            worktree_root=run_root,
        )
        eid = (out.envelope or {}).get("eid")
    return {
        "ok": True,
        "run_id": ctx.run_id,
        "project_slug": ctx.project_slug,
        "path": str(target),
        "tier": "warm",
        "state": "verified",
        "copy_id": copy_id,
        "n_files": len(files),
        "total_bytes": total_bytes,
        "source": {
            "copy_id": copy["copy_id"],
            "tier": copy["tier"],
            "profile": copy["profile"],
            "host": copy["host"],
            "path": copy["path"],
        },
        "eid": eid,
    }


__all__ = [
    "NO_VERIFIED_COPY",
    "WARM_MISMATCH",
    "WARM_NOREPLACE_UNSUPPORTED",
    "WARM_PUBLISH_FAILED",
    "WARM_TARGET_EXISTS",
    "warm_outputs",
]
