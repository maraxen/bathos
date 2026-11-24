"""Bitwise and elementwise comparison metrics (`bathos.parity.metrics`, WP49b).

Implements Spec 261123 §3.4 and §12.2 WP49b. Statistical comparisons move to WP49c.
"""

from __future__ import annotations

import hashlib
import math
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


def _nonfinite_pattern_matches(a: np.ndarray, b: np.ndarray, mask: np.ndarray) -> bool:
    if np.iscomplexobj(a) or np.iscomplexobj(b):
        ac = a.astype(np.complex128)
        bc = b.astype(np.complex128)
        return (
            _nonfinite_pattern_matches(ac.real, bc.real, mask)
            and _nonfinite_pattern_matches(ac.imag, bc.imag, mask)
        )
    a_sub = a[mask]
    b_sub = b[mask]
    return bool(
        np.array_equal(np.isnan(a_sub), np.isnan(b_sub))
        and np.array_equal(np.isposinf(a_sub), np.isposinf(b_sub))
        and np.array_equal(np.isneginf(a_sub), np.isneginf(b_sub))
    )


def _exact_int_abs_diff(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, bool]:
    """Return exact non-negative integer differences without float64 truncation.

    Returns ``(diffs, is_uint64_ndarray)``. Uses uint64 arithmetic whenever both arrays
    can be subtracted without overflowing uint64; falls back to Python int objects only
    when mixing negative signed ints with >=2**63 unsigned ints.
    """
    a_u64 = a.dtype == np.uint64
    b_u64 = b.dtype == np.uint64
    if not a_u64 and not b_u64:
        ai = a.astype(np.int64)
        bi = b.astype(np.int64)
        hi = np.maximum(ai, bi).astype(np.uint64)
        lo = np.minimum(ai, bi).astype(np.uint64)
        return hi - lo, True
    if a_u64 and b_u64:
        hi = np.maximum(a, b)
        lo = np.minimum(a, b)
        return hi - lo, True
    # One uint64, one signed/smaller unsigned: check if signed side has negatives while uint64 >= 2**63
    if a_u64:
        u_arr, s_arr = a, b
    else:
        u_arr, s_arr = b, a
    if s_arr.dtype.kind == "u":
        u2 = s_arr.astype(np.uint64)
        return np.maximum(u_arr, u2) - np.minimum(u_arr, u2), True
    # Signed vs uint64
    if not np.any(s_arr < 0):
        u2 = s_arr.astype(np.uint64)
        return np.maximum(u_arr, u2) - np.minimum(u_arr, u2), True
    # Mixed negative signed and uint64: exact Python ints
    flat_diffs = [abs(int(x) - int(y)) for x, y in zip(a.ravel().tolist(), b.ravel().tolist(), strict=True)]
    return np.array(flat_diffs, dtype=object).reshape(a.shape), False


def compare_within_tolerance(
    ours: np.ndarray,
    ref: np.ndarray,
    *,
    atol: float,
    rtol: float,
) -> ElementwiseComparisonResult:
    """Compare ``abs(a - b) <= atol + rtol * abs(b)`` with ``b`` the reference side (§3.4)."""
    if isinstance(atol, bool) or isinstance(rtol, bool):
        raise ValueError(f"atol and rtol must be finite non-negative floats (got atol={atol}, rtol={rtol})")
    atol_f = float(atol)
    rtol_f = float(rtol)
    if not (math.isfinite(atol_f) and math.isfinite(rtol_f)) or atol_f < 0 or rtol_f < 0:
        raise ValueError(f"atol and rtol must be finite and non-negative (got atol={atol}, rtol={rtol})")
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
            atol=atol_f,
            rtol=rtol_f,
            dtype_ours=a.dtype.str,
            dtype_ref=b.dtype.str,
            shape_ours=a.shape,
            shape_ref=b.shape,
            reason=f"shape mismatch: ours={a.shape} vs ref={b.shape}",
        )

    is_complex = np.iscomplexobj(a) or np.iscomplexobj(b)
    is_exact_int = (
        not is_complex
        and a.dtype.kind in ("i", "u", "b")
        and b.dtype.kind in ("i", "u", "b")
    )

    if is_exact_int:
        if b.size == 0:
            return ElementwiseComparisonResult(
                within_tolerance=True,
                max_abs_diff=0.0,
                max_rel_diff=0.0,
                mean_abs_diff=0.0,
                n_exceeding=0,
                n_elements=0,
                atol=atol_f,
                rtol=rtol_f,
                dtype_ours=a.dtype.str,
                dtype_ref=b.dtype.str,
                shape_ours=a.shape,
                shape_ref=b.shape,
            )
        diffs, is_u64 = _exact_int_abs_diff(a, b)
        b_abs_float = np.abs(b.astype(np.float64))
        bound = atol_f + rtol_f * b_abs_float
        if is_u64:
            max_u64_float = float(2**64)
            floor_b = np.floor(bound)
            # For integer d >= 0 and real B >= 0: d > B iff d > floor(B).
            never_exceed = ~np.isfinite(bound) | (floor_b >= max_u64_float)
            safe_floor = np.where(never_exceed, 0.0, np.maximum(floor_b, 0.0)).astype(np.uint64)
            exceed = (~never_exceed) & (diffs > safe_floor)
        else:
            b_list = b.ravel().tolist()
            d_list = diffs.ravel().tolist()
            exceed_list = [
                d > math.floor(atol_f + rtol_f * abs(int(bv)))
                if math.isfinite(atol_f + rtol_f * abs(int(bv)))
                else False
                for d, bv in zip(d_list, b_list, strict=True)
            ]
            exceed = np.array(exceed_list, dtype=bool)
        n_exceed = int(np.count_nonzero(exceed))
        abs_diff_f = diffs.astype(np.float64)
        nonzero_b = b_abs_float > 0
        rel_diff = np.where(nonzero_b, abs_diff_f / np.where(nonzero_b, b_abs_float, 1.0), 0.0)
        return ElementwiseComparisonResult(
            within_tolerance=(n_exceed == 0),
            max_abs_diff=float(np.max(abs_diff_f)),
            max_rel_diff=float(np.max(rel_diff)),
            mean_abs_diff=float(np.mean(abs_diff_f)),
            n_exceeding=n_exceed,
            n_elements=int(b.size),
            atol=atol_f,
            rtol=rtol_f,
            dtype_ours=a.dtype.str,
            dtype_ref=b.dtype.str,
            shape_ours=a.shape,
            shape_ref=b.shape,
            reason="" if n_exceed == 0 else f"{n_exceed} element(s) exceed tolerance band",
        )

    a_cmp = a.astype(np.complex128) if is_complex else a.astype(np.float64)
    b_cmp = b.astype(np.complex128) if is_complex else b.astype(np.float64)
    fin_a = np.isfinite(a_cmp)
    fin_b = np.isfinite(b_cmp)
    if not np.array_equal(fin_a, fin_b):
        n_bad = int(np.count_nonzero(fin_a != fin_b))
        return ElementwiseComparisonResult(
            within_tolerance=False,
            max_abs_diff=None,
            max_rel_diff=None,
            mean_abs_diff=None,
            n_exceeding=n_bad,
            n_elements=int(b_cmp.size),
            atol=atol_f,
            rtol=rtol_f,
            dtype_ours=a.dtype.str,
            dtype_ref=b.dtype.str,
            shape_ours=a.shape,
            shape_ref=b.shape,
            nonfinite_mismatch=True,
            reason=f"non-finite mask mismatch on {n_bad} element(s)",
        )

    nonfin_mask = ~fin_b
    if np.any(nonfin_mask) and not _nonfinite_pattern_matches(a_cmp, b_cmp, nonfin_mask):
        return ElementwiseComparisonResult(
            within_tolerance=False,
            max_abs_diff=None,
            max_rel_diff=None,
            mean_abs_diff=None,
            n_exceeding=int(np.count_nonzero(nonfin_mask)),
            n_elements=int(b_cmp.size),
            atol=atol_f,
            rtol=rtol_f,
            dtype_ours=a.dtype.str,
            dtype_ref=b.dtype.str,
            shape_ours=a.shape,
            shape_ref=b.shape,
            nonfinite_mismatch=True,
            reason="non-finite values differ between NaN/+inf/-inf",
        )

    if b_cmp.size == 0:
        return ElementwiseComparisonResult(
            within_tolerance=True,
            max_abs_diff=0.0,
            max_rel_diff=0.0,
            mean_abs_diff=0.0,
            n_exceeding=0,
            n_elements=0,
            atol=atol_f,
            rtol=rtol_f,
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
            n_elements=int(b_cmp.size),
            atol=atol_f,
            rtol=rtol_f,
            dtype_ours=a.dtype.str,
            dtype_ref=b.dtype.str,
            shape_ours=a.shape,
            shape_ref=b.shape,
        )

    a_fin = a_cmp[fin_b]
    b_fin = b_cmp[fin_b]
    abs_diff = np.abs(a_fin - b_fin)
    abs_b = np.abs(b_fin)
    bound = atol_f + rtol_f * abs_b
    exceed = abs_diff > bound
    n_exceed = int(np.count_nonzero(exceed))
    nonzero_b = abs_b > 0
    rel_diff = np.where(nonzero_b, abs_diff / np.where(nonzero_b, abs_b, 1.0), 0.0)

    return ElementwiseComparisonResult(
        within_tolerance=(n_exceed == 0),
        max_abs_diff=float(np.max(abs_diff)),
        max_rel_diff=float(np.max(rel_diff)),
        mean_abs_diff=float(np.mean(abs_diff)),
        n_exceeding=n_exceed,
        n_elements=int(b_cmp.size),
        atol=atol_f,
        rtol=rtol_f,
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
