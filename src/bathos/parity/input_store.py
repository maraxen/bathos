"""Input-store read and verification API (WP49b)."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from bathos.parity.store import ParityStore


class InputStoreError(RuntimeError):
    """Raised when an input blob in the parity store is missing or fails verification."""


@dataclass(frozen=True)
class InputDescriptor:
    """Descriptor for an input artifact cited by ``cell_evidence.v1`` or T3 manifest."""

    sha256: str
    dtype: str | None = None
    shape: tuple[int, ...] | None = None
    declared_span: str | None = None
    present: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "dtype": self.dtype,
            "shape": list(self.shape) if self.shape is not None else None,
            "declared_span": self.declared_span,
            "status": "present" if self.present else "absent",
        }


def put_input_bytes(
    store: ParityStore,
    data: bytes,
    *,
    dtype: str | None = None,
    shape: Sequence[int] | None = None,
    declared_span: str | None = None,
) -> InputDescriptor:
    """Store raw input bytes content-addressed in *store* and return its descriptor."""
    digest = store.put_bytes(data)
    return InputDescriptor(
        sha256=digest,
        dtype=dtype,
        shape=tuple(int(d) for d in shape) if shape is not None else None,
        declared_span=declared_span,
        present=True,
    )


def read_and_verify_input(store: ParityStore, expected_sha256: str) -> bytes:
    """Read input bytes for *expected_sha256* from *store* and verify content digest."""
    try:
        data = store.get_bytes(expected_sha256)
    except Exception as exc:
        raise InputStoreError(f"Input {expected_sha256} unavailable or corrupt: {exc}") from exc
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256.lower():
        raise InputStoreError(
            f"Input digest mismatch for {expected_sha256}: got {actual}"
        )
    return data
