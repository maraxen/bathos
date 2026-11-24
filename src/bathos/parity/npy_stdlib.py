"""Pure-stdlib `.npy` / `.npz` encoder, decoder, and canonical array digest (WP49).

CRITICAL: this module MUST NEVER import `numpy`, `jax`, or `torch` (Spec 261123 §8.2, A66, A69).
A script or black-box runner without NumPy installed uses this module to emit the exact
little-endian `.npy` and `.npz` format that `numpy.load()` reads back byte-for-byte identically.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import struct
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_NPY_MAGIC = b"\x93NUMPY"

# Little-endian canonical numpy dtype.str -> (itemsize, struct format char)
DTYPE_SPECS: dict[str, tuple[int, str]] = {
    "|b1": (1, "?"),
    "|u1": (1, "B"),
    "|i1": (1, "b"),
    "<u2": (2, "H"),
    "<i2": (2, "h"),
    "<u4": (4, "I"),
    "<i4": (4, "i"),
    "<u8": (8, "Q"),
    "<i8": (8, "q"),
    "<f2": (2, "e"),
    "<f4": (4, "f"),
    "<f8": (8, "d"),
}

_FRIENDLY_DTYPE_ALIASES: dict[str, str] = {
    "bool": "|b1",
    "bool_": "|b1",
    "b1": "|b1",
    "|b1": "|b1",
    "uint8": "|u1",
    "u1": "|u1",
    "|u1": "|u1",
    "int8": "|i1",
    "i1": "|i1",
    "|i1": "|i1",
    "uint16": "<u2",
    "u2": "<u2",
    "<u2": "<u2",
    "=u2": "<u2",
    "int16": "<i2",
    "i2": "<i2",
    "<i2": "<i2",
    "=i2": "<i2",
    "uint32": "<u4",
    "u4": "<u4",
    "<u4": "<u4",
    "=u4": "<u4",
    "int32": "<i4",
    "i4": "<i4",
    "<i4": "<i4",
    "=i4": "<i4",
    "uint64": "<u8",
    "u8": "<u8",
    "<u8": "<u8",
    "=u8": "<u8",
    "int64": "<i8",
    "i8": "<i8",
    "<i8": "<i8",
    "=i8": "<i8",
    "float16": "<f2",
    "f2": "<f2",
    "<f2": "<f2",
    "=f2": "<f2",
    "float32": "<f4",
    "f4": "<f4",
    "<f4": "<f4",
    "=f4": "<f4",
    "float64": "<f8",
    "f8": "<f8",
    "<f8": "<f8",
    "=f8": "<f8",
}

_BIG_TO_LITTLE: dict[str, str] = {
    ">u2": "<u2",
    ">i2": "<i2",
    ">u4": "<u4",
    ">i4": "<i4",
    ">u8": "<u8",
    ">i8": "<i8",
    ">f2": "<f2",
    ">f4": "<f4",
    ">f8": "<f8",
}


def normalize_dtype_str(descr: str) -> str:
    """Normalize a dtype name or numpy ``dtype.str`` to little-endian canonical ``dtype.str``."""
    if descr in _FRIENDLY_DTYPE_ALIASES:
        return _FRIENDLY_DTYPE_ALIASES[descr]
    if descr in _BIG_TO_LITTLE:
        return _BIG_TO_LITTLE[descr]
    if descr in ("<b1", "=b1", ">b1"):
        return "|b1"
    if descr in ("<u1", "=u1", ">u1"):
        return "|u1"
    if descr in ("<i1", "=i1", ">i1"):
        return "|i1"
    raise ValueError(f"Unsupported or unknown numeric dtype descriptor: {descr!r}")


def _swap_endianness(data: bytes, itemsize: int) -> bytes:
    if itemsize <= 1:
        return data
    out = bytearray(len(data))
    for i in range(0, len(data), itemsize):
        out[i : i + itemsize] = data[i : i + itemsize][::-1]
    return bytes(out)


@dataclass(frozen=True)
class RawArray:
    """Pure-stdlib container representing a C-contiguous little-endian array."""

    dtype: str
    shape: tuple[int, ...]
    data: bytes

    def __post_init__(self) -> None:
        norm_dtype = normalize_dtype_str(self.dtype)
        object.__setattr__(self, "dtype", norm_dtype)
        itemsize, _fmt = DTYPE_SPECS[norm_dtype]
        expected_len = itemsize
        for dim in self.shape:
            if dim < 0:
                raise ValueError(f"Negative dimension in shape {self.shape}")
            expected_len *= dim
        if len(self.data) != expected_len:
            raise ValueError(
                f"Array byte length {len(self.data)} does not match shape {self.shape} "
                f"and dtype {norm_dtype} (expected {expected_len})"
            )

    @classmethod
    def from_sequence(cls, values: Any, dtype: str = "<f4") -> RawArray:
        """Build a :class:`RawArray` from nested Python sequences without NumPy."""
        norm_dtype = normalize_dtype_str(dtype)
        _itemsize, fmt = DTYPE_SPECS[norm_dtype]
        shape, flat = _flatten_sequence(values)
        packed = b"".join(struct.pack(f"<{fmt}", v) for v in flat)
        return cls(dtype=norm_dtype, shape=shape, data=packed)


def _flatten_sequence(obj: Any) -> tuple[tuple[int, ...], list[Any]]:
    if not isinstance(obj, (list, tuple)):
        return (), [obj]
    if not obj:
        return (0,), []
    first_shape, _ = _flatten_sequence(obj[0])
    flat: list[Any] = []
    for item in obj:
        sub_shape, sub_flat = _flatten_sequence(item)
        if sub_shape != first_shape:
            raise ValueError(f"Ragged nested sequence: expected subshape {first_shape}, got {sub_shape}")
        flat.extend(sub_flat)
    return (len(obj), *first_shape), flat


def coerce_to_raw_array(array_like: Any) -> RawArray:
    """Convert a :class:`RawArray`, NumPy/JAX ndarray, or ``(dtype, shape, bytes)`` into :class:`RawArray`."""
    if isinstance(array_like, RawArray):
        return array_like
    if isinstance(array_like, tuple) and len(array_like) == 3:
        dtype_s, shape_s, raw_s = array_like
        return RawArray(dtype=str(dtype_s), shape=tuple(int(d) for d in shape_s), data=bytes(raw_s))

    # Duck-type NumPy / JAX / array objects without importing numpy
    dtype_obj = getattr(array_like, "dtype", None)
    shape_obj = getattr(array_like, "shape", None)
    tobytes_fn = getattr(array_like, "tobytes", None)
    if dtype_obj is not None and shape_obj is not None and callable(tobytes_fn):
        raw_descr = getattr(dtype_obj, "str", None) or str(dtype_obj)
        norm_dtype = normalize_dtype_str(str(raw_descr))
        shape = tuple(int(d) for d in shape_obj)
        try:
            raw_bytes = bytes(tobytes_fn("C"))
        except TypeError:
            raw_bytes = bytes(tobytes_fn())
        if str(raw_descr).startswith(">"):
            itemsize, _fmt = DTYPE_SPECS[norm_dtype]
            raw_bytes = _swap_endianness(raw_bytes, itemsize)
        return RawArray(dtype=norm_dtype, shape=shape, data=raw_bytes)

    raise TypeError(
        f"Cannot encode object of type {type(array_like).__name__} without NumPy: pass RawArray or ndarray"
    )


def canonical_array_digest(dtype_str: str, shape: Sequence[int], raw_le_c_bytes: bytes) -> str:
    """Compute canonical sha256 over ``(little-endian dtype.str, canonical JSON shape, LE C bytes)``."""
    norm_dtype = normalize_dtype_str(dtype_str)
    shape_list = [int(d) for d in shape]
    shape_bytes = json.dumps(shape_list, separators=(",", ":")).encode("utf-8")
    h = hashlib.sha256()
    h.update(norm_dtype.encode("utf-8"))
    h.update(b"\x00")
    h.update(shape_bytes)
    h.update(b"\x00")
    h.update(raw_le_c_bytes)
    return h.hexdigest()


def encode_npy(array_like: Any) -> bytes:
    """Encode an array as a standard `.npy` v1.0 or v2.0 byte payload without NumPy."""
    raw = coerce_to_raw_array(array_like)
    header_dict = f"{{'descr': {raw.dtype!r}, 'fortran_order': False, 'shape': {raw.shape!r}, }}"
    header_bytes = header_dict.encode("latin1") + b"\n"

    # v1.0: magic (6) + version (2) + hdr_len (2) = 10 prefix bytes; pad to multiple of 64
    prefix_len_v1 = 10
    total_v1 = prefix_len_v1 + len(header_bytes)
    pad_v1 = (-total_v1) % 64
    if len(header_bytes) + pad_v1 <= 65535:
        padded_hdr = header_bytes[:-1] + (b" " * pad_v1) + b"\n"
        return _NPY_MAGIC + struct.pack("<BBH", 1, 0, len(padded_hdr)) + padded_hdr + raw.data

    # v2.0 fallback for huge headers
    prefix_len_v2 = 12
    total_v2 = prefix_len_v2 + len(header_bytes)
    pad_v2 = (-total_v2) % 64
    padded_hdr = header_bytes[:-1] + (b" " * pad_v2) + b"\n"
    return _NPY_MAGIC + struct.pack("<BBI", 2, 0, len(padded_hdr)) + padded_hdr + raw.data


def decode_npy(data: bytes) -> RawArray:
    """Decode a `.npy` byte stream into a little-endian C-order :class:`RawArray` without NumPy."""
    if not data.startswith(_NPY_MAGIC) or len(data) < 10:
        raise ValueError("Invalid .npy header: missing NUMPY magic")
    major, _minor = struct.unpack("<BB", data[6:8])
    if major == 1:
        (hdr_len,) = struct.unpack("<H", data[8:10])
        hdr_start = 10
    elif major in (2, 3):
        if len(data) < 12:
            raise ValueError("Truncated .npy v2/v3 header")
        (hdr_len,) = struct.unpack("<I", data[8:12])
        hdr_start = 12
    else:
        raise ValueError(f"Unsupported .npy format version: {major}")

    hdr_end = hdr_start + hdr_len
    if len(data) < hdr_end:
        raise ValueError("Truncated .npy header bytes")
    encoding = "utf-8" if major == 3 else "latin1"
    header = ast.literal_eval(data[hdr_start:hdr_end].decode(encoding).strip())
    if not isinstance(header, dict):
        raise ValueError("Malformed .npy header dictionary")
    if header.get("fortran_order"):
        raise ValueError("Fortran-ordered .npy arrays are not supported in stdlib canonical decoder")

    try:
        raw_descr = str(header["descr"])
        raw_shape = header["shape"]
    except KeyError as exc:
        raise ValueError(f"Malformed .npy header: missing required key {exc}") from exc
    norm_dtype = normalize_dtype_str(raw_descr)
    shape = tuple(int(d) for d in raw_shape)
    payload = data[hdr_end:]
    if raw_descr.startswith(">"):
        itemsize, _fmt = DTYPE_SPECS[norm_dtype]
        payload = _swap_endianness(payload, itemsize)
    return RawArray(dtype=norm_dtype, shape=shape, data=payload)


def canonical_array_digest_from_npy(npy_bytes: bytes) -> str:
    """Decode `.npy` bytes and return its canonical array digest."""
    arr = decode_npy(npy_bytes)
    return canonical_array_digest(arr.dtype, arr.shape, arr.data)


def encode_npz(arrays: Mapping[str, Any], *, compress: bool = True) -> bytes:
    """Encode named arrays into a deterministic `.npz` zip archive without NumPy."""
    buf = io.BytesIO()
    compression = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    fixed_dt = (2026, 1, 1, 0, 0, 0)
    seen_members: set[str] = set()
    with zipfile.ZipFile(buf, mode="w", compression=compression) as zf:
        for key in sorted(arrays):
            member_name = key if key.endswith(".npy") else f"{key}.npy"
            if member_name in seen_members:
                raise ValueError(f"Duplicate array member name in .npz archive: {member_name!r}")
            seen_members.add(member_name)
            zi = zipfile.ZipInfo(filename=member_name, date_time=fixed_dt)
            zi.compress_type = compression
            zf.writestr(zi, encode_npy(arrays[key]))
    return buf.getvalue()


def decode_npz(data: bytes) -> dict[str, RawArray]:
    """Decode a `.npz` archive into ``{name_without_npy: RawArray}`` without NumPy."""
    out: dict[str, RawArray] = {}
    with zipfile.ZipFile(io.BytesIO(data), mode="r") as zf:
        for info in zf.infolist():
            name = info.filename
            key = name[:-4] if name.endswith(".npy") else name
            out[key] = decode_npy(zf.read(name))
    return out
