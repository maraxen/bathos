"""Archive and manifest of a run's reference set (spec 261023_output-tiering.md section 5.2).

Two things live here:

* :func:`build_archive`: pack the reference set (``reference.reference_hashes``) into
  ``run-<run_id>.tar.zst`` plus ``run-<run_id>.manifest.json`` inside a caller-given staging directory.
  Every file is hashed as it is streamed into the tar and refused, naming its relpath, if it differs from the
  reference sha, so a file modified between an earlier check and the pack cannot slip in. Nothing is written
  outside the staging directory, and the pair appears there only by rename.
* :func:`verify_archive`: check an archive against its manifest without extracting anything. Used before ``warm``
  trusts a fetched copy.

Determinism (the same reference set yields the same archive and manifest bytes, whatever the source mtimes, the
directory it was read from, or the time it is built): members sorted by relpath, every header ``mtime = 0``,
``uid = gid = 0``, empty ``uname``/``gname``, mode ``0644``, PAX tar format, single-threaded zstd level 19 with
no checksum or dictionary id, and a manifest with no wall-clock field.

Interpretation of spec 5.2: it says header ``mtime`` is "the reference event's ts" and also that the output has
no wall-clock dependence. A fixed ``mtime = 0`` satisfies both and is what is built here.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import secrets
import stat
import tarfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import zstandard

from bathos.output_tiers.reference import (
    HASH_CHUNK_BYTES,
    SPECIAL_FILE_IN_OUTPUTS,
    SYMLINK_IN_OUTPUTS,
    OutputTierError,
    ReferenceEntry,
    _check_plain_path,
    check_root_syntax,
    hash_file,
)

MANIFEST_VERSION = 1
CODEC = "zstd-19-st"
ZSTD_LEVEL = 19
TAR_MODE = 0o644

#: Refusal codes (JSON ``error_code`` values). ``outputs_drifted`` is the spec's code for a file that differs
#: from, or is missing against, its reference hash; ``no_reference_hashes`` for an empty set.
NO_REFERENCE_HASHES = "no_reference_hashes"
OUTPUTS_DRIFTED = "outputs_drifted"
ARCHIVE_CONFLICT = "archive_conflict"


def archive_name(run_id: str) -> str:
    return f"run-{run_id}.tar.zst"


def manifest_name(run_id: str) -> str:
    return f"run-{run_id}.manifest.json"


@dataclass(frozen=True)
class BuiltArchive:
    """What :func:`build_archive` committed into the staging directory."""

    archive_path: Path
    manifest_path: Path
    archive_sha256: str
    manifest_sha256: str
    archive_size_bytes: int
    manifest: dict[str, Any]


@dataclass(frozen=True)
class ArchiveVerification:
    """Result of :func:`verify_archive`. ``ok`` only when every list is empty, the shas match, and ``detail`` is
    ``None``. ``unsafe`` holds ``(member_name, reason)``; ``detail`` is set when the archive or manifest could
    not be read at all."""

    ok: bool
    archive_sha256_ok: bool
    missing: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()
    mismatched: tuple[str, ...] = ()
    unsafe: tuple[tuple[str, str], ...] = ()
    detail: str | None = None


# --- build --------------------------------------------------------------------------------------


class _HashingReader:
    """Read exactly ``size`` bytes of ``f``, hashing them. A source that ends early or still has bytes at the end
    changed while it was being packed, which refuses with ``outputs_drifted`` naming ``relpath``."""

    def __init__(self, f: io.BufferedReader, size: int, relpath: str) -> None:
        self._f = f
        self._left = size
        self._relpath = relpath
        self.sha = hashlib.sha256()

    def read(self, n: int = -1) -> bytes:
        want = self._left if n < 0 else min(n, self._left)
        out = bytearray()
        while len(out) < want:
            chunk = self._f.read(want - len(out))
            if not chunk:
                raise OutputTierError(OUTPUTS_DRIFTED, f"{self._relpath} (shrank while packing)")
            out += chunk
        self._left -= len(out)
        self.sha.update(out)
        return bytes(out)

    def finish(self) -> str:
        if self._left or self._f.read(1):
            raise OutputTierError(OUTPUTS_DRIFTED, f"{self._relpath} (changed size while packing)")
        return self.sha.hexdigest()


def _check_relpath(relpath: str) -> None:
    problem = check_root_syntax(relpath)
    if problem is None and PurePosixPath(relpath).as_posix() != relpath:
        problem = "must be a normalised POSIX path"
    if problem:
        raise OutputTierError("invalid_param", f"reference relpath {relpath!r} {problem}")


def _open_regular(root: Path, relpath: str) -> tuple[io.BufferedReader, os.stat_result]:
    """Open ``root/relpath`` for reading only if it is a regular file reached without any symlink."""
    path = root / relpath
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise OutputTierError(OUTPUTS_DRIFTED, f"{relpath} (missing)") from None
    if stat.S_ISLNK(st.st_mode):
        raise OutputTierError(SYMLINK_IN_OUTPUTS, relpath)
    if not stat.S_ISREG(st.st_mode):
        raise OutputTierError(SPECIAL_FILE_IN_OUTPUTS, relpath)
    _check_plain_path(root, path, relpath)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise OutputTierError(SYMLINK_IN_OUTPUTS, relpath) from None
        raise OutputTierError(OUTPUTS_DRIFTED, f"{relpath} (unreadable: {e.strerror})") from None
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise OutputTierError(OUTPUTS_DRIFTED, f"{relpath} (replaced while packing)")
        return os.fdopen(fd, "rb"), fst
    except BaseException:
        os.close(fd)
        raise


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _single_worktree_root(reference: Mapping[str, ReferenceEntry]) -> str:
    roots = {e.worktree_root for e in reference.values() if e.worktree_root}
    if len(roots) != 1:
        raise OutputTierError(
            "invalid_param",
            f"reference set names {len(roots)} worktree roots; pass worktree_root explicitly",
        )
    return next(iter(roots))


def build_archive(
    run_id: str,
    project_slug: str,
    reference: Mapping[str, ReferenceEntry],
    root: str | os.PathLike[str],
    staging: str | os.PathLike[str],
    *,
    worktree_root: str | None = None,
) -> BuiltArchive:
    """Pack ``reference`` (``{relpath: ReferenceEntry}``) from files under ``root`` into ``staging``.

    ``staging`` must be an existing directory; ``run-<run_id>.tar.zst`` and ``.manifest.json`` are written there
    under temporary names and linked into place (manifest first, archive last), so a final name is never
    overwritten (``archive_conflict`` if either exists) and a failed build leaves nothing but the
    partials it removes. ``worktree_root`` is recorded in the manifest as the run's recorded root (not the read
    ``root``, which differs between machines); by default it is the one root the reference entries agree on.

    Raises :class:`OutputTierError`: ``no_reference_hashes`` (empty set), ``outputs_drifted`` (a file is missing
    or its streamed sha256 differs from the reference, detail names the relpath), ``symlink_in_outputs`` /
    ``special_file_in_outputs``, ``archive_conflict``, ``invalid_param``.
    """
    if not reference:
        raise OutputTierError(NO_REFERENCE_HASHES, run_id)
    for rel, entry in reference.items():
        _check_relpath(rel)
        if entry.relpath != rel:
            raise OutputTierError(
                "invalid_param", f"reference key {rel!r} != entry {entry.relpath!r}"
            )
    root_path = Path(root)
    staging_dir = Path(staging)
    if not staging_dir.is_dir():
        raise OutputTierError("invalid_param", f"staging directory does not exist: {staging_dir}")
    recorded_root = worktree_root if worktree_root is not None else _single_worktree_root(reference)

    final_archive = staging_dir / archive_name(run_id)
    final_manifest = staging_dir / manifest_name(run_id)
    for final in (final_archive, final_manifest):
        if os.path.lexists(final):
            raise OutputTierError(ARCHIVE_CONFLICT, f"{final.name} already exists in staging")

    attempt = secrets.token_hex(4)
    tmp_archive = staging_dir / f".{final_archive.name}.partial-{attempt}"
    tmp_manifest = staging_dir / f".{final_manifest.name}.partial-{attempt}"
    files: list[dict[str, Any]] = []
    try:
        cctx = zstandard.ZstdCompressor(
            level=ZSTD_LEVEL,
            threads=0,
            write_checksum=False,
            write_content_size=False,
            write_dict_id=False,
        )
        with open(tmp_archive, "wb") as out:
            with (
                cctx.stream_writer(out, closefd=False) as zw,
                tarfile.open(fileobj=zw, mode="w|", format=tarfile.PAX_FORMAT) as tar,
            ):
                for rel in sorted(reference):
                    entry = reference[rel]
                    f, st = _open_regular(root_path, rel)
                    with f:
                        if entry.size_bytes is not None and entry.size_bytes != st.st_size:
                            raise OutputTierError(OUTPUTS_DRIFTED, f"{rel} (size differs)")
                        info = tarfile.TarInfo(rel)
                        info.size = st.st_size
                        info.mtime = 0
                        info.mode = TAR_MODE
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        info.type = tarfile.REGTYPE
                        reader = _HashingReader(f, st.st_size, rel)
                        tar.addfile(info, reader)
                        sha = reader.finish()
                    if sha != entry.sha256:
                        raise OutputTierError(OUTPUTS_DRIFTED, f"{rel} (sha256 differs)")
                    files.append(
                        {
                            "relpath": rel,
                            "size_bytes": st.st_size,
                            "sha256": sha,
                            "phase": entry.phase,
                            "origin_eid": entry.origin_eid,
                        }
                    )
            out.flush()
            os.fsync(out.fileno())
        archive_sha = hash_file(tmp_archive)
        archive_size = tmp_archive.stat().st_size
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "run_id": run_id,
            "project_slug": project_slug,
            "worktree_root": recorded_root,
            "archive": {
                "name": final_archive.name,
                "sha256": archive_sha,
                "size_bytes": archive_size,
                "codec": CODEC,
            },
            "files": files,
        }
        manifest_bytes = _json_bytes(manifest)
        with open(tmp_manifest, "wb") as mf:
            mf.write(manifest_bytes)
            mf.flush()
            os.fsync(mf.fileno())
        for tmp, final in ((tmp_manifest, final_manifest), (tmp_archive, final_archive)):
            try:
                os.link(tmp, final)  # fails if final exists, never replaces
            except FileExistsError:
                raise OutputTierError(
                    ARCHIVE_CONFLICT, f"{final.name} already exists in staging"
                ) from None
        _fsync_dir(staging_dir)
    finally:
        for tmp in (tmp_archive, tmp_manifest):
            tmp.unlink(missing_ok=True)
    return BuiltArchive(
        archive_path=final_archive,
        manifest_path=final_manifest,
        archive_sha256=archive_sha,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        archive_size_bytes=archive_size,
        manifest=manifest,
    )


# --- verify -------------------------------------------------------------------------------------


def _member_problem(member: tarfile.TarInfo) -> str | None:
    name = member.name
    if not member.isreg():
        return "not a regular file"  # symlink, hardlink, device, fifo, directory
    if name.startswith("/") or "\\" in name:
        return "absolute or non-POSIX name"
    if ".." in PurePosixPath(name).parts:
        return "path traversal"
    return None


def _iter_members(tar: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
    while (member := tar.next()) is not None:
        yield member


def verify_archive(
    archive: str | os.PathLike[str],
    manifest: str | os.PathLike[str],
    *,
    manifest_sha256: str | None = None,
) -> ArchiveVerification:
    """Check ``archive`` against ``manifest`` (both local files) and extract nothing.

    In order: the manifest's own sha256 if ``manifest_sha256`` is given; the archive's sha256 and size against
    ``manifest["archive"]``; then the tar is streamed once and its member set must equal ``manifest["files"]``
    exactly (no extra, missing or duplicate member; no absolute, ``..``, link, device or directory member) and
    each member's sha256 and size must match. Never raises for a bad archive: the result says what was wrong.
    """
    try:
        raw = Path(manifest).read_bytes()
    except OSError as e:
        return ArchiveVerification(False, False, detail=f"manifest unreadable: {e}")
    if manifest_sha256 is not None and hashlib.sha256(raw).hexdigest() != manifest_sha256:
        return ArchiveVerification(False, False, detail="manifest sha256 differs from expected")
    try:
        doc = json.loads(raw)
        info = doc["archive"]
        expected = {f["relpath"]: f for f in doc["files"]}
        if doc.get("manifest_version") != MANIFEST_VERSION or len(expected) != len(doc["files"]):
            raise ValueError("unsupported version or duplicate relpath")
        want_sha, want_size = info["sha256"], info["size_bytes"]
    except (ValueError, KeyError, TypeError) as e:
        return ArchiveVerification(False, False, detail=f"manifest malformed: {e}")

    try:
        sha_ok = hash_file(archive) == want_sha and Path(archive).stat().st_size == want_size
    except OSError as e:
        return ArchiveVerification(False, False, detail=f"archive unreadable: {e}")
    if not sha_ok:
        return ArchiveVerification(
            False, False, detail="archive sha256 or size differs from manifest"
        )

    seen: set[str] = set()
    extra: list[str] = []
    mismatched: list[str] = []
    unsafe: list[tuple[str, str]] = []
    try:
        with (
            open(archive, "rb") as raw_in,
            zstandard.ZstdDecompressor().stream_reader(raw_in) as zr,
            tarfile.open(fileobj=zr, mode="r|") as tar,
        ):
            for member in _iter_members(tar):
                name = member.name
                problem = _member_problem(member)
                if problem:
                    unsafe.append((name, problem))
                    continue
                if name in seen:
                    extra.append(name)  # a second member of the same name
                    continue
                meta = expected.get(name)
                if meta is None:
                    extra.append(name)
                    continue
                seen.add(name)
                h = hashlib.sha256()
                n = 0
                src = tar.extractfile(member)
                assert src is not None
                while chunk := src.read(HASH_CHUNK_BYTES):
                    h.update(chunk)
                    n += len(chunk)
                if h.hexdigest() != meta["sha256"] or n != meta["size_bytes"]:
                    mismatched.append(name)
    except (tarfile.TarError, zstandard.ZstdError, EOFError, OSError) as e:
        return ArchiveVerification(
            False,
            True,
            tuple(sorted(set(expected) - seen)),
            tuple(sorted(extra)),
            tuple(sorted(mismatched)),
            tuple(unsafe),
            detail=f"archive unreadable as tar.zst: {e}",
        )
    missing = tuple(sorted(set(expected) - seen))
    ok = not (missing or extra or mismatched or unsafe)
    return ArchiveVerification(
        ok, True, missing, tuple(sorted(extra)), tuple(sorted(mismatched)), tuple(unsafe)
    )
