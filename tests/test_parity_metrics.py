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
        assert res_bad.max_rel_diff == pytest.approx(0.25, abs=1e-6)
        assert res_bad.mean_abs_diff == pytest.approx(0.75, abs=1e-6)

        # Swap a and b: when ref=11.25, bound at idx 1 = 0.2 + 1.125 = 1.325 >= 1.25 -> passes!
        res_swapped = compare_within_tolerance(b, a, atol=0.2, rtol=0.1)
        assert res_swapped.within_tolerance
        assert res_swapped.n_exceeding == 0

    def test_nan_inf_negative_tolerances_rejected(self) -> None:
        a = np.array([0.0], dtype=np.float64)
        b = np.array([1e9], dtype=np.float64)
        for bad_atol, bad_rtol in (
            (float("nan"), 0.0),
            (0.0, float("nan")),
            (float("inf"), 0.0),
            (0.0, float("inf")),
            (-1e-6, 0.0),
            (0.0, -1e-6),
        ):
            with pytest.raises(ValueError, match="finite and non-negative"):
                compare_within_tolerance(a, b, atol=bad_atol, rtol=bad_rtol)

    def test_int64_uint64_exact_and_complex_modulus(self) -> None:
        # int64 > 2**53 must not truncate to float64
        i_a = np.array([2**53 + 1], dtype=np.int64)
        i_b = np.array([2**53], dtype=np.int64)
        res_i = compare_within_tolerance(i_a, i_b, atol=0.0, rtol=0.0)
        assert not res_i.within_tolerance
        assert res_i.max_abs_diff == 1.0
        assert compare_within_tolerance(i_a, i_b, atol=1.0, rtol=0.0).within_tolerance

        u_a = np.array([2**63 + 1], dtype=np.uint64)
        u_b = np.array([2**63], dtype=np.uint64)
        assert not compare_within_tolerance(u_a, u_b, atol=0.5, rtol=0.0).within_tolerance
        assert compare_within_tolerance(u_a, u_b, atol=1.0, rtol=0.0).within_tolerance

        # complex64/complex128 must compare imaginary part via modulus
        c_a = np.array([1.0 + 1000.0j], dtype=np.complex128)
        c_b = np.array([1.0 + 0.0j], dtype=np.complex128)
        res_c = compare_within_tolerance(c_a, c_b, atol=1e-4, rtol=1e-4)
        assert not res_c.within_tolerance
        assert res_c.max_abs_diff == pytest.approx(1000.0)

        c_close = np.array([1.0 + 1e-6j], dtype=np.complex64)
        c_ref = np.array([1.0 + 0.0j], dtype=np.complex64)
        assert compare_within_tolerance(c_close, c_ref, atol=1e-4, rtol=0.0).within_tolerance

    def test_nonfinite_handling(self) -> None:
        a = np.array([1.0, np.nan], dtype=np.float32)
        b = np.array([1.0, 2.0], dtype=np.float32)
        res = compare_within_tolerance(a, b, atol=1e-3, rtol=1e-3)
        assert not res.within_tolerance
        assert res.nonfinite_mismatch is True

        # +inf vs -inf and nan vs +inf must fail nonfinite_mismatch
        assert compare_within_tolerance(
            np.array([np.inf], dtype=np.float32),
            np.array([-np.inf], dtype=np.float32),
            atol=0.0,
            rtol=0.0,
        ).nonfinite_mismatch is True
        assert compare_within_tolerance(
            np.array([np.nan], dtype=np.float32),
            np.array([np.inf], dtype=np.float32),
            atol=0.0,
            rtol=0.0,
        ).nonfinite_mismatch is True

        # Empty and all-nonfinite arrays are degenerate and must NEVER pass
        empty = np.array([], dtype=np.float32)
        res_empty = compare_within_tolerance(empty, empty, atol=0.0, rtol=0.0)
        assert not res_empty.within_tolerance
        assert res_empty.degenerate is True
        assert res_empty.max_abs_diff is None
        all_nan = np.array([np.nan, np.inf], dtype=np.float32)
        res_nan = compare_within_tolerance(all_nan, all_nan, atol=0.0, rtol=0.0)
        assert not res_nan.within_tolerance
        assert res_nan.degenerate is True
        assert res_nan.max_abs_diff is None

        # Matching nonfinite mask (bug_compatible style) compares finite subset cleanly
        b2 = np.array([1.0005, np.nan], dtype=np.float32)
        res2 = compare_within_tolerance(a, b2, atol=1e-3, rtol=0.0)
        assert res2.within_tolerance

    def test_bitwise_dtype_and_shape_mismatch(self) -> None:
        a = np.array([1.0, 2.0], dtype=np.float32)
        b_dtype = np.array([1.0, 2.0], dtype=np.float64)
        b_shape = np.array([[1.0], [2.0]], dtype=np.float32)
        r1 = compare_bitwise(a, b_dtype)
        assert not r1.equal and "dtype mismatch" in r1.reason
        r2 = compare_bitwise(a, b_shape)
        assert not r2.equal and "shape mismatch" in r2.reason

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

        # All-nonfinite bitwise is degenerate (equal=False, n_differing_bytes=0); with a finite element same payload is equal
        nan1_copy = np.array([struct.unpack("<f", b"\x01\x00\xc0\x7f")[0]], dtype="<f4")
        res_all_nan = compare_bitwise(nan1, nan1_copy)
        assert not res_all_nan.equal and res_all_nan.degenerate is True and res_all_nan.n_differing_bytes == 0
        mix1 = np.array([1.0, nan1[0]], dtype="<f4")
        mix1_copy = np.array([1.0, nan1_copy[0]], dtype="<f4")
        assert compare_bitwise(mix1, mix1_copy).equal

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

    def test_sealed_dump_edge_cases_and_tampering(self, tmp_path: Path) -> None:
        arr = np.array([1.0, 2.0], dtype=np.float32)
        npz_bytes = encode_npz({"scores": arr})
        dump_sha = hashlib.sha256(npz_bytes).hexdigest()
        dump_path = tmp_path / "oracle.npz"
        dump_path.write_bytes(npz_bytes)

        store = ParityStore(tmp_path / "store")
        ingest_sealed_dump(dump_path, expected_sha256=dump_sha, store=store, stem="oracle")

        # Missing array key raises SealedDumpError
        with pytest.raises(SealedDumpError, match="missing from sealed dump"):
            load_sealed_dump_array(store, dump_sha, "nonexistent")

        # Traversal or non-hex dump_sha256 rejected
        with pytest.raises(SealedDumpError, match="Invalid dump_sha256"):
            load_sealed_dump_manifest(store, "../../evil")

        # Tampered cached manifest rejected
        m_path = store.root / "sealed_dumps" / f"{dump_sha}.json"
        m_obj = json.loads(m_path.read_text(encoding="utf-8"))
        m_obj["members"]["scores"]["canonical_digest"] = "0" * 64
        m_path.write_text(json.dumps(m_obj), encoding="utf-8")
        with pytest.raises(SealedDumpError, match="do not match verified blob"):
            load_sealed_dump_manifest(store, dump_sha)

        # Corrupt zip stored as sealed dump raises SealedDumpError
        bad_bytes = b"not a zip archive"
        bad_sha = store.put_bytes(bad_bytes)
        with pytest.raises(SealedDumpError):
            load_sealed_dump_array(store, bad_sha, "scores")
