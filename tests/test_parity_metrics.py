"""Tests for WP49b: bitwise/elementwise metrics, sealed-dump loader, and input store."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import numpy as np
import pytest

from bathos.parity.input_store import InputStoreError, put_input_bytes, read_and_verify_input
from bathos.parity.metrics import compare_bitwise, compare_within_tolerance, get_backend_identity
from bathos.parity.npy_stdlib import encode_npz
from bathos.parity.sealed_dump import (
    SealedDumpError,
    ingest_sealed_dump,
    load_sealed_dump_array,
    load_sealed_dump_manifest,
    verify_pinned_dump,
)
from bathos.parity.store import ParityStore


class TestBitwiseAndElementwiseMetrics:
    def test_hand_computed_within_tolerance_and_asymmetry(self) -> None:
        # b is reference: bound = atol + rtol * abs(b)
        b = np.array([1.0, 10.0], dtype=np.float32)
        a = np.array([1.25, 11.25], dtype=np.float32)
        # atol=0.2, rtol=0.1 => bounds = [0.3, 1.2]; exact diffs = [0.25, 1.25] -> index 1 fails
        res_bad = compare_within_tolerance(a, b, atol=0.2, rtol=0.1)
        assert not res_bad.within_tolerance
        assert res_bad.n_exceeding == 1
        assert res_bad.max_abs_diff == pytest.approx(1.25, abs=1e-6)
        assert res_bad.mean_abs_diff == pytest.approx(0.75, abs=1e-6)

        # Swap a and b: when ref=11.25, bound at idx 1 = 0.2 + 1.125 = 1.325 >= 1.25 -> passes!
        res_swapped = compare_within_tolerance(b, a, atol=0.2, rtol=0.1)
        assert res_swapped.within_tolerance
        assert res_swapped.n_exceeding == 0

    def test_nonfinite_handling(self) -> None:
        a = np.array([1.0, np.nan], dtype=np.float32)
        b = np.array([1.0, 2.0], dtype=np.float32)
        res = compare_within_tolerance(a, b, atol=1e-3, rtol=1e-3)
        assert not res.within_tolerance
        assert res.nonfinite_mismatch is True

        # Matching nonfinite mask (bug_compatible style) compares finite subset cleanly
        b2 = np.array([1.0005, np.nan], dtype=np.float32)
        res2 = compare_within_tolerance(a, b2, atol=1e-3, rtol=0.0)
        assert res2.within_tolerance

    def test_nc_plus_zero_vs_minus_zero_bitwise_unequal(self) -> None:
        a = np.array([0.0, 1.0], dtype=np.float32)
        b = np.array([-0.0, 1.0], dtype=np.float32)
        res = compare_bitwise(a, b)
        assert not res.equal
        assert res.n_differing_bytes == 1

    def test_nc_nan_payloads_differing_bitwise_unequal(self) -> None:
        nan1 = np.frombuffer(b"\x01\x00\xc0\x7f", dtype="<f4").copy()
        nan2 = np.frombuffer(b"\x02\x00\xc0\x7f", dtype="<f4").copy()
        res = compare_bitwise(nan1, nan2)
        assert not res.equal
        assert res.n_differing_bytes >= 1

        # Same payload bit pattern is equal
        nan1_copy = np.array([struct.unpack("<f", b"\x01\x00\xc0\x7f")[0]], dtype="<f4")
        assert compare_bitwise(nan1, nan1_copy).equal

    def test_backend_identity(self) -> None:
        ident = get_backend_identity()
        assert ident["name"] == "numpy"
        assert len(ident["file_sha256"]) == 64


class TestSealedDumpAndInputStore:
    def test_verify_ingest_and_load_sealed_dump(self, tmp_path: Path) -> None:
        arr = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
        npz_bytes = encode_npz({"scores": arr})
        dump_sha = hashlib.sha256(npz_bytes).hexdigest()
        dump_path = tmp_path / "oracle.npz"
        dump_path.write_bytes(npz_bytes)
        manifest_path = tmp_path / "pinned.json"
        manifest_path.write_text(json.dumps({"oracle": dump_sha}), encoding="utf-8")

        rec = verify_pinned_dump(dump_path, manifest_path, "oracle")
        assert rec.dump_sha256 == dump_sha
        assert "scores" in rec.member_digests

        store = ParityStore(tmp_path / "store")
        ingest_sealed_dump(dump_path, expected_sha256=dump_sha, store=store, stem="oracle")
        loaded = load_sealed_dump_array(store, dump_sha, "scores")
        np.testing.assert_array_equal(loaded, arr)
        digest_manifest = load_sealed_dump_manifest(store, dump_sha)
        assert digest_manifest["members"]["scores"]["canonical_digest"] == rec.member_digests["scores"]

    def test_nc_missing_stem_in_pinned_manifest_raises(self, tmp_path: Path) -> None:
        npz_bytes = encode_npz({"scores": np.array([1.0], dtype=np.float32)})
        dump_path = tmp_path / "oracle.npz"
        dump_path.write_bytes(npz_bytes)
        manifest_path = tmp_path / "pinned.json"
        manifest_path.write_text(json.dumps({"other_stem": "0" * 64}), encoding="utf-8")

        with pytest.raises(SealedDumpError, match="missing from pinned manifest"):
            verify_pinned_dump(dump_path, manifest_path, "oracle")

    def test_nc_edited_dump_sha_raises(self, tmp_path: Path) -> None:
        npz_bytes = encode_npz({"scores": np.array([1.0], dtype=np.float32)})
        dump_path = tmp_path / "oracle.npz"
        dump_path.write_bytes(npz_bytes)
        manifest_path = tmp_path / "pinned.json"
        manifest_path.write_text(json.dumps({"oracle": "f" * 64}), encoding="utf-8")

        with pytest.raises(SealedDumpError, match="sha256 mismatch"):
            verify_pinned_dump(dump_path, manifest_path, "oracle")

    def test_input_store_read_and_corruption(self, tmp_path: Path) -> None:
        store = ParityStore(tmp_path / "store")
        desc = put_input_bytes(store, b"PDBATOMDATA", dtype="pdb", declared_span="1ubq:1-76")
        assert desc.present is True
        assert read_and_verify_input(store, desc.sha256) == b"PDBATOMDATA"

        # Missing or corrupted input raises InputStoreError
        with pytest.raises(InputStoreError):
            read_and_verify_input(store, "0" * 64)
        store.blob_path(desc.sha256).write_bytes(b"TAMPERED")
        with pytest.raises(InputStoreError):
            read_and_verify_input(store, desc.sha256)
