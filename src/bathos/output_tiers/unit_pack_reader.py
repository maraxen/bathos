"""Interim swappable reader for ``xtrax``-packed units (``alynxr.unit_pack/1``, ``idea-003``).

All format-specific ``unit.json`` / ``members.idx.json`` / ``alynxr.unit_pack/1``
verification lives behind :class:`UnitPackReader` so that ``archive.py`` has no
direct knowledge of the pack format and the reader can later be repointed to a
dependency-free core in ``cisternal``.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from bathos.output_tiers.reference import (
    SPECIAL_FILE_IN_OUTPUTS,
    SYMLINK_IN_OUTPUTS,
    OutputTierError,
    _check_plain_path,
    check_root_syntax,
)

OUTPUTS_DRIFTED = "outputs_drifted"

UNIT_RECORD_FILENAME = "unit.json"
DEFAULT_PACK_FILE = "members.tar.zst"
DEFAULT_PACK_INDEX = "members.idx.json"
STORAGE_SCHEMAS = frozenset({"alynxr.unit_pack/1", "xtrax.unit_pack/1"})
PACK_FORMATS = frozenset({"tar.zst+frame-index/1", "alynxr.unit_pack/1"})

_CORE_FIELDS = (
    "input_digest",
    "input_payload",
    "meta",
    "content_digest",
    "run_id",
    "env",
    "committed_at",
)
_CORE_STR_FIELDS = ("input_digest", "content_digest", "run_id", "committed_at")
_CORE_DICT_FIELDS = ("input_payload", "meta", "env")


@dataclass(frozen=True)
class PackedUnitMember:
    """One raw file inside a packed unit that should be streamed into the cold archive as-is."""

    relpath: str
    expected_sha256: str | None = None
    expected_size: int | None = None


@dataclass(frozen=True)
class PackedUnitInfo:
    """Verified packed-unit descriptor returned by a :class:`UnitPackReader`."""

    relpath: str
    content_digest: str
    record_digest: str
    storage_schema: str
    n_members: int
    members: tuple[PackedUnitMember, ...]


class UnitPackReader(Protocol):
    """Swappable interface for verifying and enumerating packed unit directories."""

    def is_unit_dir(self, root_path: Path, rel: str) -> bool:
        """Return ``True`` if ``root_path / rel`` looks like a packed unit directory."""
        ...

    def read_unit(
        self, root_path: Path, rel: str, expected_content_digest: str
    ) -> PackedUnitInfo:
        """Verify the packed unit at ``root_path / rel`` without unpacking inner frames."""
        ...


def _normalize_unit_value(value: Any) -> Any:
    """JSON-safe, canonically ordered, NFC-normalized value matching ``xtrax.pack``."""
    if isinstance(value, dict):
        return {str(k): _normalize_unit_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_unit_value(v) for v in value]
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ValueError(f"record value of unsupported type {type(value).__name__}")


def compute_unit_record_digest(record: Mapping[str, Any]) -> str:
    """Compute the immutable ``record_digest`` over ``unit.json``'s core fields."""
    core = _normalize_unit_value({k: record.get(k) for k in _CORE_FIELDS})
    text = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unit_record_problem(record: Mapping[str, Any]) -> str | None:
    """Return why ``record`` is not a well-formed ``unit.json`` whose digest verifies; else ``None``."""
    for field_name in (*_CORE_STR_FIELDS, "record_digest"):
        value = record.get(field_name)
        if not isinstance(value, str) or (not value and field_name != "run_id"):
            return f"record lacks {field_name}"
    for field_name in _CORE_DICT_FIELDS:
        if not isinstance(record.get(field_name), dict):
            return f"record field {field_name} is not an object"
    try:
        if compute_unit_record_digest(record) != record["record_digest"]:
            return "record_digest does not match the record's core"
    except (ValueError, UnicodeDecodeError) as exc:
        return str(exc)
    return None


def digest_of_unit_members(shas: Mapping[str, str]) -> str:
    """``content_digest`` over uncompressed member sha256 pairs, matching ``xtrax.pack``."""
    pairs = [[rel, shas[rel]] for rel in sorted(shas)]
    text = json.dumps(pairs, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_plain_regular_bytes(root: Path, relpath: str) -> bytes:
    """Read a regular file under ``root`` without following any symlink."""
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
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise OutputTierError(SYMLINK_IN_OUTPUTS, relpath) from None
        raise OutputTierError(OUTPUTS_DRIFTED, f"{relpath} (unreadable: {exc.strerror})") from None
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise OutputTierError(OUTPUTS_DRIFTED, f"{relpath} (replaced while packing)")
        with os.fdopen(fd, "rb") as fh:
            return fh.read()
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise


class InterimUnitPackReader:
    """Interim local reader for ``xtrax`` / ``alynxr.unit_pack/1`` directories."""

    def is_unit_dir(self, root_path: Path, rel: str) -> bool:
        """True if ``root_path / rel`` contains ``unit.json``."""
        return os.path.lexists(root_path / rel / UNIT_RECORD_FILENAME)

    def read_unit(
        self, root_path: Path, rel: str, expected_content_digest: str
    ) -> PackedUnitInfo:
        """Verify ``unit.json`` and ``members.idx.json`` without unpacking ``members.tar.zst``."""
        unit_rel = f"{rel}/{UNIT_RECORD_FILENAME}"
        raw_unit = _read_plain_regular_bytes(root_path, unit_rel)
        try:
            record = json.loads(raw_unit.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise OutputTierError(
                OUTPUTS_DRIFTED, f"{rel} (unreadable {UNIT_RECORD_FILENAME}: {exc})"
            ) from None
        if not isinstance(record, dict):
            raise OutputTierError(
                OUTPUTS_DRIFTED, f"{rel} ({UNIT_RECORD_FILENAME} is not a JSON object)"
            )

        problem = unit_record_problem(record)
        if problem is not None:
            raise OutputTierError(OUTPUTS_DRIFTED, f"{rel} ({problem})")

        storage = record.get("storage")
        if not isinstance(storage, dict) or not storage.get("pack"):
            raise OutputTierError(OUTPUTS_DRIFTED, f"{rel} (unit has no packed storage metadata)")
        storage_schema = storage.get("schema")
        storage_format = storage.get("format")
        if storage_schema is not None and storage_schema not in STORAGE_SCHEMAS:
            raise OutputTierError(
                OUTPUTS_DRIFTED, f"{rel} (unsupported unit storage schema {storage_schema!r})"
            )
        if storage_schema is None and storage_format not in PACK_FORMATS:
            raise OutputTierError(
                OUTPUTS_DRIFTED,
                f"{rel} (missing unit storage schema for format {storage_format!r})",
            )
        if storage_format is not None and storage_format not in PACK_FORMATS:
            raise OutputTierError(
                OUTPUTS_DRIFTED, f"{rel} (unsupported unit pack format {storage_format!r})"
            )

        pack_name = storage.get("pack", DEFAULT_PACK_FILE)
        index_name = storage.get("index", DEFAULT_PACK_INDEX)
        for kind, fname in (("pack", pack_name), ("index", index_name)):
            if not isinstance(fname, str) or check_root_syntax(fname) is not None or "/" in fname:
                raise OutputTierError(
                    OUTPUTS_DRIFTED, f"{rel} (invalid storage.{kind} filename {fname!r})"
                )
        if pack_name in (UNIT_RECORD_FILENAME, index_name) or index_name == UNIT_RECORD_FILENAME:
            raise OutputTierError(
                OUTPUTS_DRIFTED,
                f"{rel} (conflicting storage filenames pack={pack_name!r}, index={index_name!r})",
            )
        pack_sha = storage.get("pack_sha256")
        index_sha = storage.get("index_sha256")
        if not isinstance(pack_sha, str) or not pack_sha:
            raise OutputTierError(OUTPUTS_DRIFTED, f"{rel} (storage lacks pack_sha256)")
        if not isinstance(index_sha, str) or not index_sha:
            raise OutputTierError(OUTPUTS_DRIFTED, f"{rel} (storage lacks index_sha256)")

        index_rel = f"{rel}/{index_name}"
        raw_idx = _read_plain_regular_bytes(root_path, index_rel)
        if hashlib.sha256(raw_idx).hexdigest() != index_sha:
            raise OutputTierError(
                OUTPUTS_DRIFTED, f"{rel} ({index_name} differs from index_sha256)"
            )
        try:
            idx_doc = json.loads(raw_idx.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise OutputTierError(
                OUTPUTS_DRIFTED, f"{rel} (invalid {index_name} JSON: {exc})"
            ) from None
        members = idx_doc.get("members") if isinstance(idx_doc, dict) else None
        if not isinstance(members, dict):
            raise OutputTierError(OUTPUTS_DRIFTED, f"{rel} ({index_name} missing members mapping)")

        n_members = storage.get("n_members")
        if n_members is not None and (
            isinstance(n_members, bool) or not isinstance(n_members, int) or n_members != len(members)
        ):
            raise OutputTierError(
                OUTPUTS_DRIFTED,
                f"{rel} ({index_name} member count {len(members)} != storage.n_members {n_members})",
            )

        excluded = {UNIT_RECORD_FILENAME, pack_name, index_name}
        member_shas: dict[str, str] = {}
        for mrel, meta in members.items():
            if (
                not isinstance(mrel, str)
                or check_root_syntax(mrel) is not None
                or mrel in excluded
                or not isinstance(meta, dict)
                or not isinstance(meta.get("sha256"), str)
                or not meta["sha256"]
            ):
                raise OutputTierError(
                    OUTPUTS_DRIFTED, f"{rel} (bad member entry {mrel!r} in {index_name})"
                )
            member_shas[mrel] = meta["sha256"]

        computed_content_digest = digest_of_unit_members(member_shas)
        if computed_content_digest != record["content_digest"]:
            raise OutputTierError(
                OUTPUTS_DRIFTED, f"{rel} (content_digest does not match {index_name} members)"
            )
        if expected_content_digest != record["content_digest"]:
            raise OutputTierError(OUTPUTS_DRIFTED, f"{rel} (content_digest differs)")

        pack_rel = f"{rel}/{pack_name}"
        return PackedUnitInfo(
            relpath=rel,
            content_digest=record["content_digest"],
            record_digest=record["record_digest"],
            storage_schema=str(storage_schema or storage_format),
            n_members=len(members),
            members=(
                PackedUnitMember(index_rel, index_sha, None),
                PackedUnitMember(pack_rel, pack_sha, None),
                PackedUnitMember(unit_rel, hashlib.sha256(raw_unit).hexdigest(), len(raw_unit)),
            ),
        )


DEFAULT_UNIT_PACK_READER: UnitPackReader = InterimUnitPackReader()
