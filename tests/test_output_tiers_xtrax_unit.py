"""Tests for idea-003: accepting ``xtrax``-packed units in ``build_archive`` without unpacking."""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
from pathlib import Path

import pytest
import zstandard

from bathos.output_tiers.archive import build_archive, verify_archive
from bathos.output_tiers.reference import OutputTierError, ReferenceEntry, hash_file
from bathos.output_tiers.unit_pack_reader import (
    PackedUnitInfo,
    PackedUnitMember,
    compute_unit_record_digest,
    digest_of_unit_members,
)

RUN = "1e4596c6-7a55-4595-9083-2091b1719a0b"
WT = "/recorded/worktree"


def _make_xtrax_unit(
    unit_dir: Path,
    members: dict[str, bytes],
    *,
    storage_schema: str = "alynxr.unit_pack/1",
) -> tuple[str, dict]:
    """Create a valid xtrax-packed unit dir (unit.json + members.idx.json + members.tar.zst)."""
    unit_dir.mkdir(parents=True, exist_ok=True)
    member_shas = {rel: hashlib.sha256(data).hexdigest() for rel, data in members.items()}
    content_digest = digest_of_unit_members(member_shas)

    # Build real tar.zst frames (one zstd frame per USTAR member + EOF frame)
    pack_buf = bytearray()
    idx_members: dict[str, dict] = {}
    cctx = zstandard.ZstdCompressor(level=3)
    for rel in sorted(members):
        payload = members[rel]
        ti = tarfile.TarInfo(rel)
        ti.size = len(payload)
        ti.mtime = 0
        ti.mode = 0o644
        hdr = ti.tobuf(tarfile.USTAR_FORMAT)
        pad = (512 - (len(payload) % 512)) % 512
        frame = cctx.compress(hdr + payload + (b"\x00" * pad))
        idx_members[rel] = {
            "offset": len(pack_buf),
            "length": len(frame),
            "header_length": len(hdr),
            "size": len(payload),
            "sha256": member_shas[rel],
        }
        pack_buf.extend(frame)
    pack_buf.extend(cctx.compress(b"\x00" * 1024))

    pack_bytes = bytes(pack_buf)
    (unit_dir / "members.tar.zst").write_bytes(pack_bytes)

    idx_doc = {
        "schema": "xtrax.unit_pack_index/1",
        "format": "tar.zst+frame-index/1",
        "codec": "zstd",
        "packer": "xtrax.pack/1",
        "members": idx_members,
    }
    idx_bytes = (json.dumps(idx_doc, sort_keys=True, indent=1) + "\n").encode("utf-8")
    (unit_dir / "members.idx.json").write_bytes(idx_bytes)

    record = {
        "schema": "xtrax.unit/1",
        "input_digest": "in-" + "a" * 16,
        "input_payload": {"seed": 0},
        "meta": {"unit": unit_dir.name},
        "content_digest": content_digest,
        "run_id": RUN,
        "env": {"python": "3.13"},
        "committed_at": "2026-11-24T00:00:00+00:00",
    }
    record["record_digest"] = compute_unit_record_digest(record)
    record["storage"] = {
        "schema": storage_schema,
        "pack": "members.tar.zst",
        "index": "members.idx.json",
        "format": "tar.zst+frame-index/1",
        "codec": "zstd",
        "pack_sha256": hashlib.sha256(pack_bytes).hexdigest(),
        "index_sha256": hashlib.sha256(idx_bytes).hexdigest(),
        "n_members": len(members),
    }
    (unit_dir / "unit.json").write_text(
        json.dumps(record, sort_keys=True, indent=1) + "\n", encoding="utf-8"
    )
    return content_digest, record


@pytest.mark.parametrize("storage_schema", ["alynxr.unit_pack/1", "xtrax.unit_pack/1"])
def test_build_archive_accepts_xtrax_unit_without_unpacking(
    tmp_path: Path, storage_schema: str
) -> None:
    src, stg = tmp_path / "src", tmp_path / "stg"
    stg.mkdir()

    unit_rel = "outputs/units/u001"
    unit_dir = src / unit_rel
    digest, record = _make_xtrax_unit(
        unit_dir,
        {"fold/scores.json": b'{"auc": 0.91}', "fold/preds.bin": b"\x00\x01\x02"},
        storage_schema=storage_schema,
    )
    loose_rel = "outputs/summary.json"
    (src / loose_rel).write_bytes(b'{"mean_auc": 0.91}')

    ref = {
        unit_rel: ReferenceEntry(unit_rel, digest, None, "run_end", "e1", WT, str(unit_dir)),
        loose_rel: ReferenceEntry(
            loose_rel, hash_file(src / loose_rel), 18, "run_end", "e2", WT, str(src / loose_rel)
        ),
    }

    # Ensure build_archive never unpacks members.tar.zst (even if zstd decompression of inner frames isn't invoked)
    built = build_archive(RUN, "proj", ref, src, stg)

    ver = verify_archive(
        built.archive_path, built.manifest_path, manifest_sha256=built.manifest_sha256
    )
    assert ver.ok, ver

    archived_names = [f["relpath"] for f in built.manifest["files"]]
    assert archived_names == sorted(
        [
            "outputs/summary.json",
            "outputs/units/u001/members.idx.json",
            "outputs/units/u001/members.tar.zst",
            "outputs/units/u001/unit.json",
        ]
    )
    assert len(built.manifest["units"]) == 1
    assert built.manifest["units"][0]["content_digest"] == digest
    assert built.manifest["units"][0]["record_digest"] == record["record_digest"]
    assert built.manifest["units"][0]["storage_schema"] == storage_schema

    # Byte-identical archive when built a second time with different mtimes
    os.utime(unit_dir / "members.tar.zst", (1_800_000_000, 1_800_000_000))
    stg2 = tmp_path / "stg2"
    stg2.mkdir()
    built2 = build_archive(RUN, "proj", ref, src, stg2)
    assert built.archive_sha256 == built2.archive_sha256
    assert built.manifest_sha256 == built2.manifest_sha256


def test_build_archive_rejects_tampered_xtrax_unit(tmp_path: Path) -> None:
    def _setup(name: str):
        src = tmp_path / f"src_{name}"
        stg = tmp_path / f"stg_{name}"
        stg.mkdir(parents=True)
        unit_rel = "outputs/units/u001"
        digest, _ = _make_xtrax_unit(src / unit_rel, {"a.txt": b"hello"})
        ref = {
            unit_rel: ReferenceEntry(
                unit_rel, digest, None, "run_end", "e1", WT, str(src / unit_rel)
            )
        }
        return src, stg, unit_rel, ref

    # 1. Tampered record_digest in unit.json
    src, stg, unit_rel, ref = _setup("rec")
    doc = json.loads((src / unit_rel / "unit.json").read_text())
    doc["meta"]["tamper"] = True
    (src / unit_rel / "unit.json").write_text(json.dumps(doc))
    with pytest.raises(OutputTierError) as ei:
        build_archive(RUN, "proj", ref, src, stg)
    assert ei.value.error_code == "outputs_drifted"
    assert "record_digest" in ei.value.detail

    # 2. Tampered members.idx.json
    src, stg, unit_rel, ref = _setup("idx")
    (src / unit_rel / "members.idx.json").write_bytes(b"{}")
    with pytest.raises(OutputTierError) as ei:
        build_archive(RUN, "proj", ref, src, stg)
    assert ei.value.error_code == "outputs_drifted"
    assert "index_sha256" in ei.value.detail

    # 3. Tampered members.tar.zst (caught by streaming pack_sha256 check)
    src, stg, unit_rel, ref = _setup("pack")
    raw = bytearray((src / unit_rel / "members.tar.zst").read_bytes())
    raw[0] ^= 0xFF
    (src / unit_rel / "members.tar.zst").write_bytes(bytes(raw))
    with pytest.raises(OutputTierError) as ei:
        build_archive(RUN, "proj", ref, src, stg)
    assert ei.value.error_code == "outputs_drifted"
    assert "members.tar.zst" in ei.value.detail

    # 4. ReferenceEntry content_digest mismatch
    src, stg, unit_rel, ref = _setup("ref")
    bad_ref = {
        unit_rel: ReferenceEntry(unit_rel, "0" * 64, None, "run_end", "e1", WT, str(src / unit_rel))
    }
    with pytest.raises(OutputTierError) as ei:
        build_archive(RUN, "proj", bad_ref, src, stg)
    assert ei.value.error_code == "outputs_drifted"
    assert "content_digest differs" in ei.value.detail

    # 5. Plain non-unit directory still refused as special_file_in_outputs
    src, stg, unit_rel, _ = _setup("plaindir")
    (src / "outputs/plain_dir").mkdir(parents=True)
    (src / "outputs/plain_dir/file.txt").write_bytes(b"hi")
    plain_ref = {
        "outputs/plain_dir": ReferenceEntry(
            "outputs/plain_dir", "0" * 64, None, "run_end", "e1", WT, str(src / "outputs/plain_dir")
        )
    }
    with pytest.raises(OutputTierError) as ei:
        build_archive(RUN, "proj", plain_ref, src, stg)
    assert ei.value.error_code == "special_file_in_outputs"


def test_build_archive_accepts_swapped_unit_pack_reader(tmp_path: Path) -> None:
    """build_archive delegates all packed-unit format handling to UnitPackReader."""
    src, stg = tmp_path / "src_swap", tmp_path / "stg_swap"
    stg.mkdir()
    unit_rel = "outputs/custom_unit"
    (src / unit_rel).mkdir(parents=True)
    blob = src / unit_rel / "payload.bin"
    blob.write_bytes(b"custom-packed-bytes")
    blob_sha = hash_file(blob)

    class _CustomReader:
        def is_unit_dir(self, root_path: Path, rel: str) -> bool:
            return (root_path / rel / "payload.bin").is_file()

        def read_unit(
            self, root_path: Path, rel: str, expected_content_digest: str
        ) -> PackedUnitInfo:
            payload = (root_path / rel / "payload.bin").read_bytes()
            return PackedUnitInfo(
                relpath=rel,
                content_digest=expected_content_digest,
                record_digest="custom-rec-digest",
                storage_schema="custom.pack/1",
                n_members=1,
                members=(PackedUnitMember(f"{rel}/payload.bin", blob_sha, len(payload)),),
            )

    ref = {
        unit_rel: ReferenceEntry(unit_rel, "digest-123", None, "run_end", "e1", WT, str(src / unit_rel))
    }
    built = build_archive(RUN, "proj", ref, src, stg, unit_reader=_CustomReader())
    ver = verify_archive(
        built.archive_path, built.manifest_path, manifest_sha256=built.manifest_sha256
    )
    assert ver.ok
    assert [f["relpath"] for f in built.manifest["files"]] == [f"{unit_rel}/payload.bin"]
    assert built.manifest["units"][0]["storage_schema"] == "custom.pack/1"
