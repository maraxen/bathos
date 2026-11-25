"""Tests for WP49: cell_evidence.v1 contract, pure-stdlib .npy/.npz writer, canonical digests."""

from __future__ import annotations

import importlib.resources
import io
import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import numpy as np
import pytest

from bathos.parity.evidence import (
    CellEvidenceError,
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
        _sha, manifest = write_cell_evidence(
            store=store,
            record_id="rec-1",
            cell_id="c1",
            run_id="r1",
            ours_arrays={"out": arr},
            reference_arrays={"out": arr},
            input_sha256={"ours": ["c" * 64], "reference": ["d" * 64]},
        )
        loaded = load_cell_evidence_for_record(store, "rec-1")
        assert len(loaded) == 1
        assert loaded[0]["cell_id"] == "c1"
        decoded_bundle = decode_npz(store.get_bytes(manifest["ours_bundle_sha256"]))
        assert decoded_bundle["out"] == RawArray(dtype="<f8", shape=(2, 3), data=arr.tobytes("C"))
        ce_schema = json.loads(
            importlib.resources.files("bathos").joinpath("schemas", "cell_evidence.v1.json").read_text(encoding="utf-8")
        )
        jsonschema.validate(instance=manifest, schema=ce_schema)

    def test_nc_tampered_ours_and_reference_digests_and_missing_blobs(self, tmp_path: Path) -> None:
        store = ParityStore(tmp_path / "store")
        arr = np.array([1.0, 2.0], dtype=np.float32)
        _sha, manifest = write_cell_evidence(
            store=store,
            record_id="rec-tamper",
            cell_id="c1",
            run_id="r1",
            ours_arrays={"y": arr},
            reference_arrays={"y": arr},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        assert validate_cell_evidence(manifest, store=store) == []

        # Tamper ours digest
        bad_ours = json.loads(json.dumps(manifest))
        bad_ours["ours_arrays"]["y"]["canonical_digest"] = "0" * 64
        assert any("ours_arrays.y digest mismatch" in e for e in validate_cell_evidence(bad_ours, store=store))

        # Tamper reference_bundle_sha256 / reference_arrays digest
        bad_ref = json.loads(json.dumps(manifest))
        bad_ref["reference_arrays"]["y"]["canonical_digest"] = "0" * 64
        assert any("reference_arrays.y digest mismatch" in e for e in validate_cell_evidence(bad_ref, store=store))

        bad_ref_blob = json.loads(json.dumps(manifest))
        bad_ref_blob["reference_bundle_sha256"] = "f" * 64
        assert any("failed to load reference_arrays bundle" in e for e in validate_cell_evidence(bad_ref_blob, store=store))

        # Missing adapter.version and invalid reference_dump_sha256
        bad_meta = json.loads(json.dumps(manifest))
        del bad_meta["adapter"]["version"]
        bad_meta["reference_dump_sha256"] = "not-hex"
        errs = validate_cell_evidence(bad_meta, store=store)
        assert any("adapter.version" in e for e in errs)
        assert any("reference_dump_sha256" in e for e in errs)

        # Tampered index JSON manifest_sha256 detected on load_cell_evidence_for_record
        idx_file = store.root / "evidence" / "rec-tamper" / "r1" / "c1.json"
        idx_obj = json.loads(idx_file.read_text(encoding="utf-8"))
        idx_obj["ours_arrays"]["y"]["canonical_digest"] = "0" * 64
        idx_file.write_text(json.dumps(idx_obj), encoding="utf-8")
        with pytest.raises(CellEvidenceError, match="does not match content-addressed manifest"):
            load_cell_evidence_for_record(store, "rec-tamper")

    def test_nc_path_traversal_and_npy_key_normalization(self, tmp_path: Path) -> None:
        store = ParityStore(tmp_path / "store")
        arr = np.array([1.0], dtype=np.float32)

        for bad_kwargs in (
            {"record_id": "../../escape", "cell_id": "c1", "run_id": "r1"},
            {"record_id": "rec-1", "cell_id": "../c1", "run_id": "r1"},
            {"record_id": "rec-1", "cell_id": "c1", "run_id": "/tmp/evil"},
        ):
            with pytest.raises(ValueError):
                write_cell_evidence(
                    store=store,
                    ours_arrays={"y": arr},
                    input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
                    **bad_kwargs,
                )
        with pytest.raises(ValueError):
            load_cell_evidence_for_record(store, "../escape")

        # ".npy" suffix key normalizes cleanly; duplicate "y" + "y.npy" rejected
        _sha, m_npy = write_cell_evidence(
            store=store,
            record_id="rec-npy",
            cell_id="c1",
            run_id="r1",
            ours_arrays={"y.npy": arr},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        assert "y" in m_npy["ours_arrays"]
        assert m_npy["ours_arrays"]["y"]["member_name"] == "y.npy"
        assert validate_cell_evidence(m_npy, store=store) == []

        with pytest.raises(CellEvidenceError, match="Duplicate array key"):
            write_cell_evidence(
                store=store,
                record_id="rec-npy",
                cell_id="c2",
                run_id="r1",
                ours_arrays={"y": arr, "y.npy": arr},
                input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
            )
        with pytest.raises(ValueError, match="Duplicate array member"):
            encode_npz({"y": arr, "y.npy": arr})

    def test_npy_stdlib_edge_cases_and_invalid_input_sha(self, tmp_path: Path) -> None:
        # 0-d scalar & empty array
        scalar = np.array(3.5, dtype=np.float64)
        empty = np.array([], dtype=np.float32)
        for arr in (scalar, empty):
            raw = decode_npy(encode_npy(arr))
            assert raw.shape == arr.shape
            assert canonical_array_digest(raw.dtype, raw.shape, raw.data) == canonical_array_digest(
                arr.dtype.str, arr.shape, arr.tobytes("C")
            )

        # Fortran order rejected
        f_arr = np.asfortranarray(np.arange(6, dtype=np.float32).reshape(2, 3))
        buf = io.BytesIO()
        np.save(buf, f_arr, allow_pickle=False)
        with pytest.raises(ValueError, match="Fortran-ordered"):
            decode_npy(buf.getvalue())

        # Invalid input_sha256 hex rejected
        store = ParityStore(tmp_path / "store")
        with pytest.raises(CellEvidenceError, match="invalid sha256 digest"):
            write_cell_evidence(
                store=store,
                record_id="rec-bad-inp",
                cell_id="c1",
                run_id="r1",
                ours_arrays={"y": empty},
                input_sha256={"ours": ["not-64-hex"], "reference": ["b" * 64]},
            )

    def test_numpy_in_pyproject_dependencies(self) -> None:
        pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        assert '"numpy>=' in pyproject


class TestCellEvidenceV1:
    def test_item4_reject_manifest_with_wrong_record_id_or_cell_id(self, tmp_path: Path) -> None:
        store = ParityStore(tmp_path / "store")
        arr = np.array([1.0, 2.0], dtype=np.float32)

        # Case 1: manifest carrying another record_id placed inside evidence/<record_id>/<run_id>/<cell_id>.json
        _sha, foreign_manifest = write_cell_evidence(
            store=store,
            record_id="other-record-v1",
            cell_id="cell-a",
            run_id="r1",
            ours_arrays={"scores": arr},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        foreign_idx = store.root / "evidence" / "other-record-v1" / "r1" / "cell-a.json"
        dest_dir = store.root / "evidence" / "target-record-v1" / "r1"
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "cell-a.json").write_text(foreign_idx.read_text(encoding="utf-8"), encoding="utf-8")
        with pytest.raises(CellEvidenceError, match="record_id"):
            load_cell_evidence_for_record(store, "target-record-v1")

        # Case 2: filename cell_id differs from JSON cell_id
        store2 = ParityStore(tmp_path / "store2")
        write_cell_evidence(
            store=store2,
            record_id="rec-mismatch",
            cell_id="real-cell",
            run_id="r1",
            ours_arrays={"scores": arr},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        real_idx = store2.root / "evidence" / "rec-mismatch" / "r1" / "real-cell.json"
        (store2.root / "evidence" / "rec-mismatch" / "r1" / "other-cell.json").write_text(
            real_idx.read_text(encoding="utf-8"), encoding="utf-8"
        )
        with pytest.raises(CellEvidenceError, match="cell_id"):
            load_cell_evidence_for_record(store2, "rec-mismatch")

        # Case 3: filename run_id differs from JSON run_id
        store3 = ParityStore(tmp_path / "store3")
        write_cell_evidence(
            store=store3,
            record_id="rec-run-mm",
            cell_id="c1",
            run_id="r-real",
            ours_arrays={"scores": arr},
            input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
        )
        r_idx = store3.root / "evidence" / "rec-run-mm" / "r-real" / "c1.json"
        wrong_run_dir = store3.root / "evidence" / "rec-run-mm" / "r-other"
        wrong_run_dir.mkdir(parents=True, exist_ok=True)
        (wrong_run_dir / "c1.json").write_text(r_idx.read_text(encoding="utf-8"), encoding="utf-8")
        with pytest.raises(CellEvidenceError, match="run_id"):
            load_cell_evidence_for_record(store3, "rec-run-mm")
