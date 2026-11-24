"""Bitwise and elementwise comparison metrics (`bathos.parity.metrics`, WP49b).

Implements Spec 261123 §3.4 and §12.2 WP49b. Statistical comparisons move to WP49c.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


@dataclass(frozen=True)
class BitwiseComparisonResult:
    """Result of raw-byte bitwise equality check between ``ours`` and ``ref``."""

    equal: bool
    dtype_ours: str
    dtype_ref: str
    shape_ours: tuple[int, ...]
    shape_ref: tuple[int, ...]
    n_differing_bytes: int
    n_bytes: int
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "equal": self.equal,
            "dtype_ours": self.dtype_ours,
            "dtype_ref": self.dtype_ref,
            "shape_ours": list(self.shape_ours),
            "shape_ref": list(self.shape_ref),
            "n_differing_bytes": self.n_differing_bytes,
            "n_bytes": self.n_bytes,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ElementwiseComparisonResult:
    """Result of ``abs(a - b) <= atol + rtol * abs(b)`` with ``b`` as reference."""

    within_tolerance: bool
    max_abs_diff: float | None
    max_rel_diff: float | None
    mean_abs_diff: float | None
    n_exceeding: int
    n_elements: int
    atol: float
    rtol: float
    dtype_ours: str
    dtype_ref: str
    shape_ours: tuple[int, ...]
    shape_ref: tuple[int, ...]
    nonfinite_mismatch: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "within_tolerance": self.within_tolerance,
            "max_abs_diff": self.max_abs_diff,
            "max_rel_diff": self.max_rel_diff,
            "mean_abs_diff": self.mean_abs_diff,
            "n_exceeding": self.n_exceeding,
            "n_elements": self.n_elements,
            "atol": self.atol,
            "rtol": self.rtol,
            "dtype_ours": self.dtype_ours,
            "dtype_ref": self.dtype_ref,
            "shape_ours": list(self.shape_ours),
            "shape_ref": list(self.shape_ref),
            "nonfinite_mismatch": self.nonfinite_mismatch,
            "reason": self.reason,
        }


def _le_c_contiguous(arr: np.ndarray) -> np.ndarray:
    arr = np.asarray(arr)
    if arr.dtype.byteorder == ">":
        arr = arr.astype(arr.dtype.newbyteorder("<"), copy=True)
    return np.ascontiguousarray(arr)


def compare_bitwise(ours: np.ndarray, ref: np.ndarray) -> BitwiseComparisonResult:
    """Compare raw little-endian C-order bytes of ``ours`` and ``ref``.

    Requires identical dtype and shape; ``+0.0`` and ``-0.0`` are unequal,
    and differing NaN bit payloads are unequal (§3.4).
    """
    a = _le_c_contiguous(ours)
    b = _le_c_contiguous(ref)
    if a.dtype != b.dtype:
        return BitwiseComparisonResult(
            equal=False,
            dtype_ours=a.dtype.str,
            dtype_ref=b.dtype.str,
            shape_ours=a.shape,
            shape_ref=b.shape,
            n_differing_bytes=0,
            n_bytes=max(a.nbytes, b.nbytes),
            reason=f"dtype mismatch: ours={a.dtype.str} vs ref={b.dtype.str}",
        )
    if a.shape != b.shape:
        return BitwiseComparisonResult(
            equal=False,
            dtype_ours=a.dtype.str,
            dtype_ref=b.dtype.str,
            shape_ours=a.shape,
            shape_ref=b.shape,
            n_differing_bytes=0,
            n_bytes=max(a.nbytes, b.nbytes),
            reason=f"shape mismatch: ours={a.shape} vs ref={b.shape}",
        )
    a_bytes = np.frombuffer(a.tobytes("C"), dtype=np.uint8)
    b_bytes = np.frombuffer(b.tobytes("C"), dtype=np.uint8)
    diff_mask = a_bytes != b_bytes
    n_diff = int(np.count_nonzero(diff_mask))
    return BitwiseComparisonResult(
        equal=(n_diff == 0),
        dtype_ours=a.dtype.str,
        dtype_ref=b.dtype.str,
        shape_ours=a.shape,
        shape_ref=b.shape,
        n_differing_bytes=n_diff,
        n_bytes=int(b_bytes.size),
        reason="" if n_diff == 0 else f"{n_diff} of {b_bytes.size} byte(s) differ",
    )


def compare_within_tolerance(
    ours: np.ndarray,
    ref: np.ndarray,
    *,
    atol: float,
    rtol: float,
) -> ElementwiseComparisonResult:
    """Compare ``abs(a - b) <= atol + rtol * abs(b)`` with ``b`` the reference side (§3.4)."""
    if atol < 0 or rtol < 0:
        raise ValueError(f"atol and rtol must be non-negative (got atol={atol}, rtol={rtol})")
    a = np.asarray(ours)
    b = np.asarray(ref)
    if a.shape != b.shape:
        return ElementwiseComparisonResult(
            within_tolerance=False,
            max_abs_diff=None,
            max_rel_diff=None,
            mean_abs_diff=None,
            n_exceeding=int(max(a.size, b.size)),
            n_elements=int(b.size),
            atol=float(atol),
            rtol=float(rtol),
            dtype_ours=a.dtype.str,
            dtype_ref=b.dtype.str,
            shape_ours=a.shape,
            shape_ref=b.shape,
            reason=f"shape mismatch: ours={a.shape} vs ref={b.shape}",
        )

    a64 = a.astype(np.float64)
    b64 = b.astype(np.float64)
    fin_a = np.isfinite(a64)
    fin_b = np.isfinite(b64)
    if not np.array_equal(fin_a, fin_b):
        n_bad = int(np.count_nonzero(fin_a != fin_b))
        return ElementwiseComparisonResult(
            within_tolerance=False,
            max_abs_diff=None,
            max_rel_diff=None,
            mean_abs_diff=None,
            n_exceeding=n_bad,
            n_elements=int(b64.size),
            atol=float(atol),
            rtol=float(rtol),
            dtype_ours=a.dtype.str,
            dtype_ref=b.dtype.str,
            shape_ours=a.shape,
            shape_ref=b.shape,
            nonfinite_mismatch=True,
            reason=f"non-finite mask mismatch on {n_bad} element(s)",
        )

    # Check exact non-finite pattern (NaN vs +inf vs -inf) when any non-finite present
    nonfin_mask = ~fin_b
    if np.any(nonfin_mask):
        nan_match = np.array_equal(np.isnan(a64[nonfin_mask]), np.isnan(b64[nonfin_mask]))
        posinf_match = np.array_equal(np.isposinf(a64[nonfin_mask]), np.isposinf(b64[nonfin_mask]))
        neginf_match = np.array_equal(np.isneginf(a64[nonfin_mask]), np.isneginf(b64[nonfin_mask]))
        if not (nan_match and posinf_match and neginf_match):
            return ElementwiseComparisonResult(
                within_tolerance=False,
                max_abs_diff=None,
                max_rel_diff=None,
                mean_abs_diff=None,
                n_exceeding=int(np.count_nonzero(nonfin_mask)),
                n_elements=int(b64.size),
                atol=float(atol),
                rtol=float(rtol),
                dtype_ours=a.dtype.str,
                dtype_ref=b.dtype.str,
                shape_ours=a.shape,
                shape_ref=b.shape,
                nonfinite_mismatch=True,
                reason="non-finite values differ between NaN/+inf/-inf",
            )

    if b64.size == 0:
        return ElementwiseComparisonResult(
            within_tolerance=True,
            max_abs_diff=0.0,
            max_rel_diff=0.0,
            mean_abs_diff=0.0,
            n_exceeding=0,
            n_elements=0,
            atol=float(atol),
            rtol=float(rtol),
            dtype_ours=a.dtype.str,
            dtype_ref=b.dtype.str,
            shape_ours=a.shape,
            shape_ref=b.shape,
        )

    if not np.any(fin_b):
        return ElementwiseComparisonResult(
            within_tolerance=True,
            max_abs_diff=0.0,
            max_rel_diff=0.0,
            mean_abs_diff=0.0,
            n_exceeding=0,
            n_elements=int(b64.size),
            atol=float(atol),
            rtol=float(rtol),
            dtype_ours=a.dtype.str,
            dtype_ref=b.dtype.str,
            shape_ours=a.shape,
            shape_ref=b.shape,
        )

    a_fin = a64[fin_b]
    b_fin = b64[fin_b]
    abs_diff = np.abs(a_fin - b_fin)
    bound = float(atol) + float(rtol) * np.abs(b_fin)
    exceed = abs_diff > bound
    n_exceed = int(np.count_nonzero(exceed))
    nonzero_b = np.abs(b_fin) > 0
    rel_diff = np.where(nonzero_b, abs_diff / np.where(nonzero_b, np.abs(b_fin), 1.0), 0.0)

    return ElementwiseComparisonResult(
        within_tolerance=(n_exceed == 0),
        max_abs_diff=float(np.max(abs_diff)),
        max_rel_diff=float(np.max(rel_diff)),
        mean_abs_diff=float(np.mean(abs_diff)),
        n_exceeding=n_exceed,
        n_elements=int(b64.size),
        atol=float(atol),
        rtol=float(rtol),
        dtype_ours=a.dtype.str,
        dtype_ref=b.dtype.str,
        shape_ours=a.shape,
        shape_ref=b.shape,
        reason="" if n_exceed == 0 else f"{n_exceed} element(s) exceed tolerance band",
    )


def get_backend_identity() -> dict[str, str]:
    """Return comparison-backend identity metadata recorded in `grade_inputs`."""
    file_path = Path(__file__).resolve()
    file_sha = hashlib.sha256(file_path.read_bytes()).hexdigest()
    return {
        "name": "numpy",
        "distribution": "bathos",
        "numpy_version": str(np.__version__),
        "file_sha256": file_sha,
    }
