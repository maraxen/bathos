"""Sealed oracle dump verification, ingest, digest manifest, and loading (WP49b)."""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from bathos.parity.npy_stdlib import canonical_array_digest, decode_npz
from bathos.parity.store import ParityStore, atomic_write_json


class SealedDumpError(RuntimeError):
    """Raised on missing manifest, missing stem, sha mismatch, missing array key, or corrupt dump."""


@dataclass(frozen=True)
class SealedDumpRecord:
    """Verified sealed-dump descriptor and per-member canonical array digest manifest."""

    stem: str
    path: Path | None
    dump_sha256: str
    member_digests: dict[str, str]
    member_shapes: dict[str, tuple[int, ...]]
    member_dtypes: dict[str, str]

    def to_manifest_dict(self) -> dict[str, Any]:
        return {
            "stem": self.stem,
            "dump_sha256": self.dump_sha256,
            "members": {
                k: {
                    "canonical_digest": self.member_digests[k],
                    "dtype": self.member_dtypes[k],
                    "shape": list(self.member_shapes[k]),
                }
                for k in sorted(self.member_digests)
            },
        }


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_dump_digest_manifest(npz_bytes: bytes, *, expected_sha256: str, stem: str = "", path: Path | None = None) -> SealedDumpRecord:
    """Verify *npz_bytes* matches *expected_sha256* and build its canonical array digest manifest."""
    actual = _sha256_bytes(npz_bytes)
    if actual != expected_sha256.lower():
        raise SealedDumpError(
            f"Sealed dump sha256 mismatch{f' ({path})' if path else ''}: "
            f"computed {actual} != pinned {expected_sha256.lower()}"
        )
    try:
        decoded = decode_npz(npz_bytes)
    except Exception as exc:
        raise SealedDumpError(f"Corrupt sealed dump archive ({expected_sha256[:12]}): {exc}") from exc

    member_digests: dict[str, str] = {}
    member_shapes: dict[str, tuple[int, ...]] = {}
    member_dtypes: dict[str, str] = {}
    for key in sorted(decoded):
        raw = decoded[key]
        member_digests[key] = canonical_array_digest(raw.dtype, raw.shape, raw.data)
        member_shapes[key] = raw.shape
        member_dtypes[key] = raw.dtype
    return SealedDumpRecord(
        stem=stem,
        path=path,
        dump_sha256=actual,
        member_digests=member_digests,
        member_shapes=member_shapes,
        member_dtypes=member_dtypes,
    )


def verify_pinned_dump(
    dump_path: Path | str,
    pinned_manifest_path: Path | str,
    stem: str,
) -> SealedDumpRecord:
    """Verify *dump_path* against *pinned_manifest_path* for *stem* and compute member digests.

    Fails loudly if the pinned manifest is absent, *stem* is absent from the manifest,
    the dump file does not exist, or its SHA-256 differs from the pinned value.
    """
    manifest_p = Path(pinned_manifest_path)
    if not manifest_p.is_file():
        raise SealedDumpError(f"Pinned dumps manifest not found: {manifest_p}")
    try:
        pinned = json.loads(manifest_p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SealedDumpError(f"Malformed pinned dumps manifest {manifest_p}: {exc}") from exc
    if not isinstance(pinned, dict):
        raise SealedDumpError(f"Pinned dumps manifest {manifest_p} must be a JSON object")

    expected_sha = pinned.get(stem) or pinned.get(f"{stem}.npz")
    if not isinstance(expected_sha, str) or not expected_sha.strip():
        raise SealedDumpError(
            f"Sealed dump stem {stem!r} is missing from pinned manifest {manifest_p}"
        )

    dump_p = Path(dump_path)
    if not dump_p.is_file():
        raise SealedDumpError(f"Sealed dump file not found: {dump_p}")
    npz_bytes = dump_p.read_bytes()
    return build_dump_digest_manifest(
        npz_bytes,
        expected_sha256=expected_sha.strip(),
        stem=stem,
        path=dump_p,
    )


def ingest_sealed_dump(
    dump_path: Path | str,
    *,
    expected_sha256: str,
    store: ParityStore,
    stem: str = "",
) -> SealedDumpRecord:
    """Verify *dump_path* against *expected_sha256*, store its bytes and member digest manifest."""
    dump_p = Path(dump_path)
    if not dump_p.is_file():
        raise SealedDumpError(f"Sealed dump file not found: {dump_p}")
    npz_bytes = dump_p.read_bytes()
    rec = build_dump_digest_manifest(
        npz_bytes, expected_sha256=expected_sha256, stem=stem or dump_p.stem, path=dump_p
    )
    store.put_bytes(npz_bytes)
    manifest_path = store.root / "sealed_dumps" / f"{rec.dump_sha256}.json"
    atomic_write_json(manifest_path, rec.to_manifest_dict())
    return rec


def load_sealed_dump_manifest(store: ParityStore, dump_sha256: str) -> dict[str, Any]:
    """Load or reconstruct the member canonical-digest manifest for *dump_sha256*."""
    manifest_path = store.root / "sealed_dumps" / f"{dump_sha256.lower()}.json"
    if manifest_path.is_file():
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    try:
        npz_bytes = store.get_bytes(dump_sha256)
    except Exception as exc:
        raise SealedDumpError(f"Failed to load sealed dump {dump_sha256}: {exc}") from exc
    rec = build_dump_digest_manifest(npz_bytes, expected_sha256=dump_sha256)
    payload = rec.to_manifest_dict()
    atomic_write_json(manifest_path, payload)
    return payload


def load_sealed_dump_arrays(store: ParityStore, dump_sha256: str) -> dict[str, np.ndarray]:
    """Verify and load all arrays from sealed dump *dump_sha256* stored in *store*."""
    try:
        npz_bytes = store.get_bytes(dump_sha256)
    except Exception as exc:
        raise SealedDumpError(f"Sealed dump {dump_sha256} unavailable/corrupt: {exc}") from exc
    with np.load(io.BytesIO(npz_bytes), allow_pickle=False) as z:
        return {k: np.asarray(z[k]) for k in sorted(z.files)}


def load_sealed_dump_array(store: ParityStore, dump_sha256: str, key: str) -> np.ndarray:
    """Verify and load one named array *key* from sealed dump *dump_sha256*."""
    arrays = load_sealed_dump_arrays(store, dump_sha256)
    if key not in arrays:
        raise SealedDumpError(f"Key {key!r} missing from sealed dump {dump_sha256[:12]}")
    return arrays[key]
