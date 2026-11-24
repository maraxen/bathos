"""Tests for WP49: cell_evidence.v1 contract, pure-stdlib .npy/.npz writer, canonical digests."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

from bathos.parity.evidence import (
    load_cell_evidence_for_record,
    validate_cell_evidence,
    write_cell_evidence,
)
from bathos.parity.npy_stdlib import (
    RawArray,
    canonical_array_digest,
    canonical_array_digest_from_npy,
    decode_npy,
    decode_npz,
    encode_npy,
    encode_npz,
)
from bathos.parity.store import ParityStore


class TestCellEvidenceStdlib:
    def test_acceptance_write_cell_evidence_runs_without_numpy(self, tmp_path: Path) -> None:
        """Execute write_cell_evidence() in a fresh Python subprocess where numpy import is blocked."""
        store_dir = tmp_path / "store"
        src_dir = Path(__file__).resolve().parent.parent / "src"
        script = f"""
import sys
from pathlib import Path

class BlockNumpy:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "numpy" or fullname.startswith("numpy."):
            raise ImportError("blocked numpy in stdlib test")
        return None

sys.meta_path.insert(0, BlockNumpy())
sys.path.insert(0, {str(src_dir)!r})

from bathos.parity.evidence import write_cell_evidence
from bathos.parity.npy_stdlib import RawArray

arr = RawArray.from_sequence([[1.5, -2.25], [3.0, 0.5]], dtype="<f4")
sha, manifest = write_cell_evidence(
    store=Path({str(store_dir)!r}),
    record_id="demo-v1",
    cell_id="0123456789abcdef",
    run_id="run-1",
    ours_arrays={{"scores": arr}},
    input_sha256={{"ours": ["a" * 64], "reference": ["b" * 64]}},
)
print(manifest["ours_bundle_sha256"])
"""
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
        )
        bundle_sha = proc.stdout.strip().splitlines()[-1]
        store = ParityStore(store_dir)
        npz_bytes = store.get_bytes(bundle_sha)
        with np.load(io.BytesIO(npz_bytes)) as loaded:
            arr = loaded["scores"]
            assert arr.dtype == np.float32
            assert arr.shape == (2, 2)
            np.testing.assert_array_equal(arr, np.array([[1.5, -2.25], [3.0, 0.5]], dtype=np.float32))

    def test_nc_missing_input_sha256_rejected_on_ingest(self, tmp_path: Path) -> None:
        store = ParityStore(tmp_path / "store")
        arr = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        _sha, manifest = write_cell_evidence(
            store=store,
            record_id="demo-v1",
            cell_id="cell-1",
            run_id="run-1",
            ours_arrays={"scores": arr},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        assert validate_cell_evidence(manifest, store=store) == []

        broken = dict(manifest)
        del broken["input_sha256"]
        errors = validate_cell_evidence(broken, store=store)
        assert any("input_sha256" in e for e in errors)

        broken2 = json.loads(json.dumps(manifest))
        broken2["input_sha256"]["reference"] = []
        errors2 = validate_cell_evidence(broken2, store=store)
        assert any("input_sha256.reference" in e for e in errors2)

    def test_nc_same_canonical_digest_in_npy_and_npz(self) -> None:
        arr = np.arange(12, dtype=np.float32).reshape(3, 4)
        npy_bytes = encode_npy(arr)
        npz_bytes = encode_npz({"tensor": arr})

        digest_direct = canonical_array_digest(arr.dtype.str, arr.shape, arr.tobytes("C"))
        digest_npy = canonical_array_digest_from_npy(npy_bytes)
        decoded_npz = decode_npz(npz_bytes)
        digest_npz = canonical_array_digest(
            decoded_npz["tensor"].dtype, decoded_npz["tensor"].shape, decoded_npz["tensor"].data
        )
        assert digest_direct == digest_npy == digest_npz

        # Verify numpy's own np.save produces the exact same canonical digest via decode_npy
        buf = io.BytesIO()
        np.save(buf, arr, allow_pickle=False)
        decoded_np = decode_npy(buf.getvalue())
        assert canonical_array_digest(decoded_np.dtype, decoded_np.shape, decoded_np.data) == digest_direct

    def test_big_endian_array_normalized_to_little_endian_digest(self) -> None:
        arr_le = np.array([1.0, 2.0, -3.5], dtype="<f4")
        arr_be = arr_le.astype(">f4")
        assert canonical_array_digest_from_npy(encode_npy(arr_be)) == canonical_array_digest_from_npy(
            encode_npy(arr_le)
        )

    def test_load_cell_evidence_for_record(self, tmp_path: Path) -> None:
        store = ParityStore(tmp_path / "store")
        arr = np.zeros((2, 3), dtype=np.float64)
        write_cell_evidence(
            store=store,
            record_id="rec-1",
            cell_id="c1",
            run_id="r1",
            ours_arrays={"out": arr},
            input_sha256={"ours": ["c" * 64], "reference": ["d" * 64]},
        )
        loaded = load_cell_evidence_for_record(store, "rec-1")
        assert len(loaded) == 1
        assert loaded[0]["cell_id"] == "c1"
        assert isinstance(RawArray(dtype="<f8", shape=(2, 3), data=arr.tobytes("C")), RawArray)

    def test_numpy_in_pyproject_dependencies(self) -> None:
        pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        assert '"numpy>=' in pyproject
