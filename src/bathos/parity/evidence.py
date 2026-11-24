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
)
from bathos.parity.store import ParityStore, atomic_write_json

CELL_EVIDENCE_SCHEMA_VERSION = "cell_evidence.v1"
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class CellEvidenceError(ValueError):
    """Raised when ``cell_evidence.v1`` writing or ingest validation fails."""


def _store_array_bundle(
    store: ParityStore,
    arrays: Mapping[str, Any],
) -> tuple[str, dict[str, dict[str, Any]]]:
    if not arrays:
        raise CellEvidenceError("At least one array is required to build a cell-evidence bundle")
    coerced: dict[str, RawArray] = {}
    descriptors: dict[str, dict[str, Any]] = {}
    for name in sorted(arrays):
        raw = coerce_to_raw_array(arrays[name])
        coerced[name] = raw
        descriptors[name] = {
            "member_name": f"{name}.npy",
            "dtype": raw.dtype,
            "shape": list(raw.shape),
            "canonical_digest": canonical_array_digest(raw.dtype, raw.shape, raw.data),
        }
    npz_bytes = encode_npz(coerced)
    bundle_sha = store.put_bytes(npz_bytes)
    return bundle_sha, descriptors


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
        "record_id": str(record_id),
        "cell_id": str(cell_id),
        "run_id": str(run_id),
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

    index_target = parity_store.root / "evidence" / str(record_id) / str(run_id) / f"{cell_id}.json"
    atomic_write_json(index_target, {"manifest_sha256": manifest_sha, **manifest})
    return manifest_sha, manifest


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
        if not isinstance(val, str) or not val.strip():
            errors.append(f"missing or empty required field {key!r}")

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
    if not isinstance(ours_arrays, Mapping) or not ours_arrays:
        errors.append("missing or empty 'ours_arrays'")

    adapter = manifest.get("adapter")
    if not isinstance(adapter, Mapping) or not adapter.get("name"):
        errors.append("missing adapter.name")

    if store is not None and isinstance(ours_bundle, str) and _HEX64_RE.match(ours_bundle) and isinstance(ours_arrays, Mapping):
        try:
            npz_bytes = store.get_bytes(ours_bundle)
            decoded = decode_npz(npz_bytes)
        except Exception as exc:
            errors.append(f"failed to load ours_bundle_sha256 {ours_bundle}: {exc}")
            decoded = {}
        for arr_name, meta in ours_arrays.items():
            if not isinstance(meta, Mapping):
                errors.append(f"ours_arrays.{arr_name} must be a mapping")
                continue
            if arr_name not in decoded:
                errors.append(f"ours_arrays.{arr_name} missing from .npz bundle")
                continue
            raw = decoded[arr_name]
            recomputed = canonical_array_digest(raw.dtype, raw.shape, raw.data)
            if recomputed != meta.get("canonical_digest"):
                errors.append(
                    f"ours_arrays.{arr_name} digest mismatch: manifest={meta.get('canonical_digest')}, computed={recomputed}"
                )

    return errors


def load_cell_evidence_for_record(store: ParityStore, record_id: str) -> list[dict[str, Any]]:
    """Load all indexed ``cell_evidence.v1`` manifests for *record_id* in deterministic order."""
    ev_dir = store.root / "evidence" / record_id
    if not ev_dir.is_dir():
        return []
    manifests: list[dict[str, Any]] = []
    for json_file in sorted(ev_dir.glob("*/*.json")):
        if json_file.name.startswith("."):
            continue
        manifests.append(json.loads(json_file.read_text(encoding="utf-8")))
    return manifests
