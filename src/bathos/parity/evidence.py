"""Cell-evidence contract (`cell_evidence.v1`) and stdlib writer `write_cell_evidence()` (WP49).

CRITICAL: like ``npy_stdlib.py``, this module MUST NEVER import `numpy`, `jax`, or `torch`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bathos.parity.npy_stdlib import (
    RawArray,
    canonical_array_digest,
    coerce_to_raw_array,
    decode_npz,
    encode_npz,
    normalize_dtype_str,
)
from bathos.parity.store import ParityStore, atomic_write_json, validate_safe_store_segment

CELL_EVIDENCE_SCHEMA_VERSION = "cell_evidence.v1"
_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")


class CellEvidenceError(ValueError):
    """Raised when ``cell_evidence.v1`` writing or ingest validation fails."""


def _normalize_array_key(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise CellEvidenceError(f"Array name must be a non-empty string, got {name!r}")
    norm = name[:-4] if name.endswith(".npy") else name
    if not norm or not norm.strip():
        raise CellEvidenceError(f"Array name {name!r} is empty after stripping '.npy'")
    return norm


def _store_array_bundle(
    store: ParityStore,
    arrays: Mapping[str, Any],
) -> tuple[str, dict[str, dict[str, Any]]]:
    if not arrays:
        raise CellEvidenceError("At least one array is required to build a cell-evidence bundle")
    coerced: dict[str, RawArray] = {}
    descriptors: dict[str, dict[str, Any]] = {}
    for raw_name in sorted(arrays):
        norm_name = _normalize_array_key(raw_name)
        if norm_name in coerced:
            raise CellEvidenceError(
                f"Duplicate array key after .npy normalization: {raw_name!r} collides with {norm_name!r}"
            )
        raw = coerce_to_raw_array(arrays[raw_name])
        coerced[norm_name] = raw
        descriptors[norm_name] = {
            "member_name": f"{norm_name}.npy",
            "dtype": raw.dtype,
            "shape": list(raw.shape),
            "canonical_digest": canonical_array_digest(raw.dtype, raw.shape, raw.data),
        }
    npz_bytes = encode_npz(coerced)
    bundle_sha = store.put_bytes(npz_bytes)
    return bundle_sha, descriptors


def _safe_evidence_dir(store: ParityStore, record_id: str) -> Path:
    rec_seg = validate_safe_store_segment(record_id, "record_id")
    ev_root = (store.root / "evidence").resolve()
    rec_dir = (ev_root / rec_seg).resolve()
    if ev_root not in rec_dir.parents:
        raise CellEvidenceError(f"record_id escapes evidence directory: {record_id!r}")
    return rec_dir


def _safe_evidence_index_path(store: ParityStore, record_id: str, run_id: str, cell_id: str) -> Path:
    rec_dir = _safe_evidence_dir(store, record_id)
    run_seg = validate_safe_store_segment(run_id, "run_id")
    cell_seg = validate_safe_store_segment(cell_id, "cell_id")
    target = (rec_dir / run_seg / f"{cell_seg}.json").resolve()
    if rec_dir not in target.parents:
        raise CellEvidenceError(f"Evidence path escapes record directory: {record_id!r}/{run_id!r}/{cell_id!r}")
    return target


def write_cell_evidence(
    *,
    store: ParityStore | Path | str,
    record_id: str,
    cell_id: str,
    run_id: str,
    ours_arrays: Mapping[str, Any],
    input_sha256: Mapping[str, Sequence[str]],
    reference_arrays: Mapping[str, Any] | None = None,
    reference_dump_sha256: str | None = None,
    reference_dump_keys: Mapping[str, str] | None = None,
    adapter: Mapping[str, str] | None = None,
    compared_calls: list[dict[str, Any]] | None = None,
    witnesses: dict[str, Any] | None = None,
    ref_env_blob_sha256: str | None = None,
    input_transforms: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Write cell arrays (`.npz`) and a validated ``cell_evidence.v1`` manifest to *store*.

    Runs purely on Python's standard library (no NumPy required).
    Returns ``(manifest_sha256, manifest_dict)``.
    """
    parity_store = store if isinstance(store, ParityStore) else ParityStore(store)
    safe_record_id = validate_safe_store_segment(record_id, "record_id")
    safe_cell_id = validate_safe_store_segment(cell_id, "cell_id")
    safe_run_id = validate_safe_store_segment(run_id, "run_id")
    index_target = _safe_evidence_index_path(parity_store, safe_record_id, safe_run_id, safe_cell_id)

    ours_bundle_sha, ours_desc = _store_array_bundle(parity_store, ours_arrays)

    ref_bundle_sha: str | None = None
    ref_desc: dict[str, dict[str, Any]] | None = None
    if reference_arrays is not None:
        ref_bundle_sha, ref_desc = _store_array_bundle(parity_store, reference_arrays)

    norm_inputs = {
        "ours": [str(x) for x in input_sha256.get("ours", [])],
        "reference": [str(x) for x in input_sha256.get("reference", [])],
    }

    manifest: dict[str, Any] = {
        "schema_version": CELL_EVIDENCE_SCHEMA_VERSION,
        "record_id": safe_record_id,
        "cell_id": safe_cell_id,
        "run_id": safe_run_id,
        "input_sha256": norm_inputs,
        "ours_bundle_sha256": ours_bundle_sha,
        "ours_arrays": ours_desc,
        "adapter": dict(adapter) if adapter is not None else {"name": "bathos.stdlib", "version": "1.0.0"},
        "created_at": datetime.now(UTC).isoformat(),
    }
    if ref_bundle_sha is not None:
        manifest["reference_bundle_sha256"] = ref_bundle_sha
    if ref_desc is not None:
        manifest["reference_arrays"] = ref_desc
    if reference_dump_sha256 is not None:
        manifest["reference_dump_sha256"] = str(reference_dump_sha256)
    if reference_dump_keys is not None:
        manifest["reference_dump_keys"] = {str(k): str(v) for k, v in reference_dump_keys.items()}
    if input_transforms is not None:
        manifest["input_transforms"] = list(input_transforms)
    if compared_calls is not None:
        manifest["compared_calls"] = list(compared_calls)
    if witnesses is not None:
        manifest["witnesses"] = dict(witnesses)
    if ref_env_blob_sha256 is not None:
        manifest["ref_env_blob_sha256"] = str(ref_env_blob_sha256)

    errors = validate_cell_evidence(manifest, store=parity_store)
    if errors:
        raise CellEvidenceError("Invalid cell_evidence.v1 manifest: " + "; ".join(errors))

    manifest_bytes = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")
    manifest_sha = parity_store.put_bytes(manifest_bytes)

    atomic_write_json(index_target, {"manifest_sha256": manifest_sha, **manifest})
    return manifest_sha, manifest


def _validate_array_descriptors(
    arrays_map: Any,
    *,
    field_label: str,
    bundle_sha: Any,
    store: ParityStore | None,
    errors: list[str],
) -> None:
    if not isinstance(arrays_map, Mapping) or not arrays_map:
        errors.append(f"missing or empty '{field_label}'")
        return
    for arr_name, meta in arrays_map.items():
        if not isinstance(arr_name, str) or not arr_name.strip():
            errors.append(f"{field_label} contains empty array name {arr_name!r}")
            continue
        if not isinstance(meta, Mapping):
            errors.append(f"{field_label}.{arr_name} must be a mapping")
            continue
        mname = meta.get("member_name")
        if not isinstance(mname, str) or not mname.strip():
            errors.append(f"{field_label}.{arr_name}.member_name must be a non-empty string")
        elif mname != f"{arr_name}.npy":
            errors.append(
                f"{field_label}.{arr_name}.member_name mismatch: expected {f'{arr_name}.npy'!r}, got {mname!r}"
            )
        cdig = meta.get("canonical_digest")
        if not isinstance(cdig, str) or not _HEX64_RE.match(cdig):
            errors.append(f"{field_label}.{arr_name}.canonical_digest must be a 64-hex string")
        dtype_val = meta.get("dtype")
        if not isinstance(dtype_val, str) or not dtype_val.strip():
            errors.append(f"{field_label}.{arr_name}.dtype must be a non-empty string")
        else:
            try:
                normalize_dtype_str(dtype_val)
            except ValueError as exc:
                errors.append(f"{field_label}.{arr_name}.dtype invalid: {exc}")
        shape_val = meta.get("shape")
        if (
            not isinstance(shape_val, Sequence)
            or isinstance(shape_val, (str, bytes))
            or any(isinstance(d, bool) or not isinstance(d, int) or d < 0 for d in shape_val)
        ):
            errors.append(f"{field_label}.{arr_name}.shape must be a list of non-negative integers")

    if store is not None and isinstance(bundle_sha, str) and _HEX64_RE.match(bundle_sha):
        try:
            npz_bytes = store.get_bytes(bundle_sha)
            decoded = decode_npz(npz_bytes)
        except Exception as exc:
            errors.append(f"failed to load {field_label} bundle {bundle_sha}: {exc}")
            return
        extra_members = sorted(set(decoded.keys()) - {str(k) for k in arrays_map})
        if extra_members:
            errors.append(
                f"{field_label} bundle {bundle_sha} contains undeclared member(s): {extra_members}"
            )
        for arr_name, meta in arrays_map.items():
            if not isinstance(meta, Mapping):
                continue
            if arr_name not in decoded:
                errors.append(f"{field_label}.{arr_name} missing from .npz bundle")
                continue
            raw = decoded[arr_name]
            recomputed = canonical_array_digest(raw.dtype, raw.shape, raw.data)
            if recomputed != meta.get("canonical_digest"):
                errors.append(
                    f"{field_label}.{arr_name} digest mismatch: manifest={meta.get('canonical_digest')}, computed={recomputed}"
                )
            dtype_val = meta.get("dtype")
            if isinstance(dtype_val, str):
                try:
                    if normalize_dtype_str(dtype_val) != raw.dtype:
                        errors.append(
                            f"{field_label}.{arr_name} dtype mismatch: manifest={dtype_val!r}, bundle={raw.dtype!r}"
                        )
                except ValueError:
                    pass
            shape_val = meta.get("shape")
            if (
                isinstance(shape_val, Sequence)
                and not isinstance(shape_val, (str, bytes))
                and list(raw.shape) != list(shape_val)
            ):
                errors.append(
                    f"{field_label}.{arr_name} shape mismatch: manifest={list(shape_val)}, bundle={list(raw.shape)}"
                )


def validate_cell_evidence(
    manifest: Mapping[str, Any],
    *,
    store: ParityStore | None = None,
) -> list[str]:
    """Validate a ``cell_evidence.v1`` manifest and optionally recompute stored digests."""
    errors: list[str] = []
    if manifest.get("schema_version") != CELL_EVIDENCE_SCHEMA_VERSION:
        errors.append(f"schema_version must be {CELL_EVIDENCE_SCHEMA_VERSION!r}")
    for key in ("record_id", "cell_id", "run_id"):
        val = manifest.get(key)
        try:
            validate_safe_store_segment(val, key)
        except ValueError as exc:
            errors.append(str(exc))

    inp = manifest.get("input_sha256")
    if not isinstance(inp, Mapping):
        errors.append("missing required field 'input_sha256'")
    else:
        for side in ("ours", "reference"):
            digests = inp.get(side)
            if not isinstance(digests, Sequence) or isinstance(digests, (str, bytes)) or not digests:
                errors.append(f"input_sha256.{side} must be a non-empty list of sha256 digests")
            else:
                for d in digests:
                    if not isinstance(d, str) or not _HEX64_RE.match(d):
                        errors.append(f"invalid sha256 digest in input_sha256.{side}: {d!r}")

    ours_bundle = manifest.get("ours_bundle_sha256")
    if not isinstance(ours_bundle, str) or not _HEX64_RE.match(ours_bundle):
        errors.append("missing or invalid 'ours_bundle_sha256'")

    ours_arrays = manifest.get("ours_arrays")
    _validate_array_descriptors(
        ours_arrays,
        field_label="ours_arrays",
        bundle_sha=ours_bundle,
        store=store,
        errors=errors,
    )

    ref_bundle = manifest.get("reference_bundle_sha256")
    ref_arrays = manifest.get("reference_arrays")
    if ref_bundle is not None:
        if not isinstance(ref_bundle, str) or not _HEX64_RE.match(ref_bundle):
            errors.append("invalid 'reference_bundle_sha256': must be 64-hex string or null")
        if ref_arrays is None:
            errors.append("reference_arrays is required when reference_bundle_sha256 is set")
        else:
            _validate_array_descriptors(
                ref_arrays,
                field_label="reference_arrays",
                bundle_sha=ref_bundle,
                store=store,
                errors=errors,
            )
    elif ref_arrays is not None:
        errors.append("reference_bundle_sha256 is required when reference_arrays is set")

    for opt_hex_key in ("reference_dump_sha256", "ref_env_blob_sha256"):
        opt_val = manifest.get(opt_hex_key)
        if opt_val is not None and (not isinstance(opt_val, str) or not _HEX64_RE.match(opt_val)):
            errors.append(f"invalid '{opt_hex_key}': must be 64-hex string or null")

    adapter = manifest.get("adapter")
    if not isinstance(adapter, Mapping):
        errors.append("missing required field 'adapter'")
    else:
        if not isinstance(adapter.get("name"), str) or not str(adapter["name"]).strip():
            errors.append("missing adapter.name")
        if not isinstance(adapter.get("version"), str) or not str(adapter["version"]).strip():
            errors.append("missing adapter.version")

    return errors


def load_cell_evidence_for_record(store: ParityStore, record_id: str) -> list[dict[str, Any]]:
    """Load and verify all indexed ``cell_evidence.v1`` manifests for *record_id*."""
    safe_record_id = validate_safe_store_segment(record_id, "record_id")
    ev_dir = _safe_evidence_dir(store, safe_record_id)
    if not ev_dir.is_dir():
        return []
    manifests: list[dict[str, Any]] = []
    for json_file in sorted(ev_dir.glob("*/*.json")):
        if json_file.name.startswith("."):
            continue
        resolved = json_file.resolve()
        if ev_dir not in resolved.parents:
            raise CellEvidenceError(f"Evidence index file escapes record directory: {json_file}")
        try:
            idx_obj = json.loads(json_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise CellEvidenceError(f"Malformed evidence index JSON {json_file}: {exc}") from exc
        if not isinstance(idx_obj, dict):
            raise CellEvidenceError(f"Evidence index {json_file} must be a JSON object")
        m_sha = idx_obj.get("manifest_sha256")
        if not isinstance(m_sha, str) or not _HEX64_RE.match(m_sha):
            raise CellEvidenceError(f"Evidence index {json_file} missing valid manifest_sha256")
        try:
            blob_bytes = store.get_bytes(m_sha)
            blob_obj = json.loads(blob_bytes.decode("utf-8"))
        except Exception as exc:
            raise CellEvidenceError(
                f"Evidence manifest blob {m_sha} for {json_file} missing or corrupt: {exc}"
            ) from exc
        idx_body = {k: v for k, v in idx_obj.items() if k != "manifest_sha256"}
        if blob_obj != idx_body:
            raise CellEvidenceError(
                f"Evidence index {json_file} does not match content-addressed manifest {m_sha}"
            )
        expected_run_id = json_file.parent.name
        expected_cell_id = json_file.stem
        if idx_obj.get("record_id") != safe_record_id:
            raise CellEvidenceError(
                f"Evidence index {json_file} record_id {idx_obj.get('record_id')!r} does not match {safe_record_id!r}"
            )
        if idx_obj.get("run_id") != expected_run_id:
            raise CellEvidenceError(
                f"Evidence index {json_file} run_id {idx_obj.get('run_id')!r} does not match directory {expected_run_id!r}"
            )
        if idx_obj.get("cell_id") != expected_cell_id:
            raise CellEvidenceError(
                f"Evidence index {json_file} cell_id {idx_obj.get('cell_id')!r} does not match filename {expected_cell_id!r}"
            )
        manifests.append(idx_obj)
    manifests.sort(key=lambda m: (str(m.get("created_at", "")), str(m.get("run_id", "")), str(m.get("cell_id", ""))))
    return manifests
