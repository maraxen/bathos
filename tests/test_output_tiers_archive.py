"""Output tiering step 4: archive builder and verifier (spec 261023_output-tiering.md section 5.2, 9 row 4).

Each test names the wrong implementation it is meant to fail.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest
import zstandard

from bathos.output_tiers.archive import build_archive, manifest_name, verify_archive
from bathos.output_tiers.reference import OutputTierError, ReferenceEntry, hash_file

RUN = "1e4596c6-7a55-4595-9083-2091b1719a0b"
WT = "/recorded/worktree"
FILES = {"outputs/b.bin": b"\x00\x01" * 5000, "outputs/sub/a.txt": b"alpha\n", "z.txt": b"zed"}


def _source(root: Path, mtime: int) -> dict[str, ReferenceEntry]:
    ref = {}
    for i, (rel, data) in enumerate(FILES.items()):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.utime(p, (mtime + i, mtime + i))
        ref[rel] = ReferenceEntry(
            rel,
            hash_file(p),
            len(data),
            "run_end" if i % 2 == 0 else "adopted",
            f"eid{i}",
            WT,
            str(p),
        )
    return ref


def _build(tmp: Path, name: str, mtime: int, order: bool = False):
    src, stg = tmp / f"src_{name}", tmp / f"stg_{name}"
    stg.mkdir()
    ref = _source(src, mtime)
    if order:
        ref = dict(reversed(list(ref.items())))
    return src, stg, ref, build_archive(RUN, "proj", ref, src, stg)


def test_same_set_twice_is_byte_identical(tmp_path):
    """Fails if headers keep source mtime/ordering, or any wall-clock leaks into archive or manifest."""
    _, _, _, a = _build(tmp_path, "a", 1_000_000_000)
    _, _, _, b = _build(tmp_path, "b", 1_700_000_000, order=True)
    assert (
        a.archive_sha256
        == b.archive_sha256
        == hash_file(a.archive_path)
        == hash_file(b.archive_path)
    )
    assert a.manifest_sha256 == b.manifest_sha256
    assert a.manifest_path.read_bytes() == b.manifest_path.read_bytes()


def test_round_trip_and_manifest_matches_reference(tmp_path):
    src, _, ref, built = _build(tmp_path, "a", 1_000_000_000)
    res = verify_archive(
        built.archive_path, built.manifest_path, manifest_sha256=built.manifest_sha256
    )
    assert res.ok and res.archive_sha256_ok and not (res.missing or res.extra or res.mismatched)
    files = json.loads(built.manifest_path.read_bytes())["files"]
    assert [f["relpath"] for f in files] == sorted(ref)  # sorted
    for f in files:
        e = ref[f["relpath"]]
        assert (f["sha256"], f["phase"], f["origin_eid"]) == (e.sha256, e.phase, e.origin_eid)
        assert f["sha256"] == hash_file(src / f["relpath"])  # hash of the real bytes
    with (
        built.archive_path.open("rb") as raw,
        zstandard.ZstdDecompressor().stream_reader(raw) as zr,
        tarfile.open(fileobj=zr, mode="r|") as tar,
    ):
        members = list(tar)
    assert [m.name for m in members] == sorted(ref)
    for m in members:  # normalised headers, independent of the identical-bytes test
        assert (m.mtime, m.uid, m.gid, m.uname, m.gname, m.mode) == (0, 0, 0, "", "", 0o644)


def test_manifest_has_no_wall_clock_keys(tmp_path):
    manifest = _build(tmp_path, "a", 1_000_000_000)[3].manifest
    assert set(manifest) == {
        "manifest_version",
        "run_id",
        "project_slug",
        "worktree_root",
        "archive",
        "files",
    }
    assert set(manifest["archive"]) == {"name", "sha256", "size_bytes", "codec"}
    assert manifest["archive"]["codec"] == "zstd-19-st" and manifest["worktree_root"] == WT
    assert set(manifest["files"][0]) == {"relpath", "size_bytes", "sha256", "phase", "origin_eid"}


def test_one_changed_byte_after_reference_refuses_naming_relpath(tmp_path):
    """Fails if the builder trusts the reference/size instead of hashing the streamed bytes."""
    src, stg = tmp_path / "src", tmp_path / "stg"
    stg.mkdir()
    ref = _source(src, 1_000_000_000)
    target = src / "outputs/sub/a.txt"
    target.write_bytes(b"alphb\n")  # same size, one byte differs
    with pytest.raises(OutputTierError) as ei:
        build_archive(RUN, "proj", ref, src, stg)
    assert ei.value.error_code == "outputs_drifted" and "outputs/sub/a.txt" in ei.value.detail
    assert list(stg.iterdir()) == []  # no partials, no final pair


def test_missing_symlink_and_empty_refuse(tmp_path):
    src, stg = tmp_path / "src", tmp_path / "stg"
    stg.mkdir()
    ref = _source(src, 1_000_000_000)
    with pytest.raises(OutputTierError) as ei:
        build_archive(RUN, "proj", {}, src, stg)
    assert ei.value.error_code == "no_reference_hashes"
    (src / "z.txt").unlink()
    with pytest.raises(OutputTierError) as ei:
        build_archive(RUN, "proj", ref, src, stg)
    assert ei.value.error_code == "outputs_drifted" and "z.txt" in ei.value.detail
    (src / "z.txt").symlink_to(src / "outputs/b.bin")
    with pytest.raises(OutputTierError) as ei:
        build_archive(RUN, "proj", ref, src, stg)
    assert ei.value.error_code == "symlink_in_outputs" and "z.txt" in ei.value.detail
    assert list(stg.iterdir()) == []


def test_existing_final_in_staging_is_never_overwritten(tmp_path):
    src, stg, ref, built = _build(tmp_path, "a", 1_000_000_000)
    before = built.archive_path.read_bytes()
    with pytest.raises(OutputTierError) as ei:
        build_archive(RUN, "proj", ref, src, stg)
    assert ei.value.error_code == "archive_conflict"
    assert built.archive_path.read_bytes() == before
    assert sorted(p.name for p in stg.iterdir()) == sorted(
        [built.archive_path.name, manifest_name(RUN)]
    )


# --- verifier on crafted archives ---------------------------------------------------------------


def _craft(
    tmp: Path, members: list[tuple[str, bytes | str | None, bytes]], listed: dict[str, bytes]
):
    """Write a tar.zst with ``members`` ``(name, kind, data)`` (kind None = regular, 'sym' = symlink to data) and a
    manifest that lists ``listed`` and carries the crafted archive's true sha256, so only the member checks can
    reject it."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, kind, data in members:
            ti = tarfile.TarInfo(name)
            if kind == "sym":
                ti.type, ti.linkname = tarfile.SYMTYPE, data.decode()
                tar.addfile(ti)
            else:
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))
    raw = zstandard.ZstdCompressor(level=3).compress(buf.getvalue())
    arc = tmp / "crafted.tar.zst"
    arc.write_bytes(raw)
    man = tmp / "crafted.manifest.json"
    man.write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "archive": {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)},
                "files": [
                    {"relpath": r, "size_bytes": len(d), "sha256": hashlib.sha256(d).hexdigest()}
                    for r, d in listed.items()
                ],
            }
        )
    )
    return arc, man


GOOD = {"a.txt": b"aaa"}


@pytest.mark.parametrize(
    ("members", "listed", "field"),
    [
        ([("a.txt", None, b"aaa")], GOOD, None),  # control: the crafting helper itself is sound
        ([("a.txt", "sym", b"/etc/passwd")], GOOD, "unsafe"),
        ([("a.txt", None, b"aaa"), ("extra.txt", None, b"x")], GOOD, "extra"),
        ([("a.txt", None, b"aaa"), ("../evil", None, b"x")], {**GOOD, "../evil": b"x"}, "unsafe"),
        (
            [("a.txt", None, b"aaa"), ("/abs/evil", None, b"x")],
            {**GOOD, "/abs/evil": b"x"},
            "unsafe",
        ),
        ([], GOOD, "missing"),
        ([("a.txt", None, b"AAA")], GOOD, "mismatched"),  # same size, other bytes
        ([("a.txt", None, b"aaa"), ("a.txt", None, b"aaa")], GOOD, "extra"),  # duplicate member
    ],
    ids=["control", "symlink", "extra", "dotdot", "absolute", "missing", "content", "duplicate"],
)
def test_verifier_rejects_crafted_archives(tmp_path, members, listed, field):
    res = verify_archive(*_craft(tmp_path, members, listed))
    if field is None:
        assert res.ok
        return
    assert not res.ok and getattr(res, field), res


def test_verifier_rejects_modified_archive_before_reading_it(tmp_path):
    built = _build(tmp_path, "a", 1_000_000_000)[3]
    raw = bytearray(built.archive_path.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    built.archive_path.write_bytes(bytes(raw))
    res = verify_archive(built.archive_path, built.manifest_path)
    assert not res.ok and not res.archive_sha256_ok
