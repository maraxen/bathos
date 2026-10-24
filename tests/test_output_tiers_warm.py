"""Output tiering step 7: ``bth warm`` (spec 261023_output-tiering.md sections 3.2, 4, 5.2, 5.3, 7 and 9 row 7).

Built on the Step 6 harness (a real log-mode catalog and a fake ``myxcel cold`` that simulates a remote filesystem):
a valid cold copy is written by ``bth cold`` itself, then hostile or broken archives are planted over it on the
fake remote together with a matching ``output_copy.recorded`` event, so each refusal is reached through the real
gates and not around them. Each test names the broken implementation it guards against.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import io
import json
import os
import socket
import tarfile
from pathlib import Path

import pytest
import zstandard

from bathos.cli_cyclopts import app
from bathos.mcp import mcp_warm_outputs_tool, warm_outputs_tool
from bathos.output_tiers import warm as warm_module
from bathos.output_tiers.reference import OutputTierError
from bathos.output_tiers.warm import warm_outputs
from bathos.runlog.envelope import build_envelope
from tests._cyclopts_runner import CyclopticRunner
from tests.test_output_tiers_adopt import RID
from tests.test_output_tiers_cold import (  # noqa: F401  (fixtures)
    COLD,
    MIRROR,
    ColdWorld,
    _fresh_probe_cache,
    cw,
)

FUTURE = "2099-01-01T00:00:00.000000Z"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --------------------------------------------------------------------------------------------------
# helpers


class WarmWorld:
    """A finished run with a valid verified cold copy, a fresh parent directory to warm into, and a planter."""

    def __init__(self, cold_world: ColdWorld, tmp_path: Path) -> None:
        self.cw = cold_world
        self.parent = tmp_path / "warm-parent"
        self.parent.mkdir()
        self.target = self.parent / "restored"
        self.shas = cold_world.standard_run()
        cold_world.ingest()
        cold_world.cold()
        self._n = 0

    def warm(self, to: Path | None = None, **kw):
        kw.setdefault("runner", self.cw.fake)
        return warm_outputs(self.cw.w.catalog, RID[:8], str(to or self.target), **kw)

    def listing(self) -> list[str]:
        return sorted(os.listdir(self.parent))

    def copy_row(self, tier: str = "cold") -> dict:
        (row,) = [r for r in self.cw.where()["copies"] if r["tier"] == tier]
        return row

    def reference_files(self) -> dict[str, bytes]:
        return {rel: (self.cw.w.root / rel).read_bytes() for rel in self.shas}

    def plant(
        self,
        members: list[tuple[str, str, bytes | str]],
        manifest_files: dict[str, bytes] | None = None,
    ) -> None:
        """Replace the cold pair on the fake remote with an archive of ``members`` (``(name, kind, payload)``,
        kind ``reg`` or ``symlink``) and a manifest listing ``manifest_files`` (default: the run's real files),
        both internally consistent, then record a newer ``output_copy.recorded`` carrying their hashes, so the
        copy record, manifest and archive all agree and only the hostile content is wrong."""
        files = self.reference_files() if manifest_files is None else manifest_files
        buf = io.BytesIO()
        with (
            zstandard.ZstdCompressor(level=3).stream_writer(buf, closefd=False) as zw,
            tarfile.open(fileobj=zw, mode="w|", format=tarfile.PAX_FORMAT) as tar,
        ):
            for name, kind, payload in members:
                info = tarfile.TarInfo(name)
                info.mode = 0o644
                if kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = str(payload)
                    tar.addfile(info)
                else:
                    data = payload if isinstance(payload, bytes) else str(payload).encode()
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
        archive_bytes = buf.getvalue()
        manifest = {
            "manifest_version": 1,
            "run_id": RID,
            "project_slug": "repo",
            "worktree_root": str(self.cw.w.root),
            "archive": {
                "name": f"run-{RID}.tar.zst",
                "sha256": sha(archive_bytes),
                "size_bytes": len(archive_bytes),
                "codec": "zstd-19-st",
            },
            "files": [
                {
                    "relpath": rel,
                    "size_bytes": len(data),
                    "sha256": sha(data),
                    "phase": "run_end",
                    "origin_eid": "x",
                }
                for rel, data in sorted(files.items())
            ],
        }
        manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode()
        m_name, a_name = self.cw.final_names()
        self.cw.fake.remote(COLD, f"{self.cw.dest()}/{m_name}").write_bytes(manifest_bytes)
        self.cw.fake.remote(COLD, f"{self.cw.dest()}/{a_name}").write_bytes(archive_bytes)
        self.record(
            archive_sha256=sha(archive_bytes),
            manifest_sha256=sha(manifest_bytes),
            archive_size_bytes=len(archive_bytes),
        )

    def record(self, **changes) -> None:
        """Append a newer ``recorded`` event for the cold copy with ``changes`` applied (a later event wins)."""
        row = self.copy_row("cold")
        data = {
            k: row[k]
            for k in (
                "tier",
                "profile",
                "host",
                "path",
                "config_layer",
                "archive_sha256",
                "manifest_sha256",
                "archive_size_bytes",
                "state",
                "verified_at",
                "recorded_at",
            )
        }
        data.update(changes)
        self.event("output_copy.recorded", row["copy_id"], data)

    def event(self, kind: str, copy_id: str, data: dict) -> None:
        self._n += 1
        env = build_envelope(
            kind=kind,
            entity=[RID, copy_id],
            data=data,
            main_root=self.cw.w.repo,
            worktree_root=self.cw.w.root,
            project="repo",
            project_id=self.cw.w.pid,
            writer="w1",
            seq=500 + self._n,
            ts=FUTURE.replace("2099-01-01", f"2099-01-{self._n:02d}"),
            eid=f"e-warm-{self._n}",
        )
        self.cw.w._lines.append(json.dumps(env, sort_keys=True) + "\n")
        self.cw.ingest()


@pytest.fixture
def ww(cw: ColdWorld, tmp_path: Path) -> WarmWorld:  # noqa: F811
    return WarmWorld(cw, tmp_path)


def _code(excinfo) -> str:
    return excinfo.value.error_code


def _good_members(ww: WarmWorld) -> list[tuple[str, str, bytes | str]]:
    return [(rel, "reg", data) for rel, data in sorted(ww.reference_files().items())]


# --------------------------------------------------------------------------------------------------
# the happy path


def test_warm_restores_every_file_leaves_no_staging_and_records_a_folded_warm_copy(ww: WarmWorld):
    """Files equal the reference hashes, the parent holds only the target (no `.partial`), and the event folds
    into `output_copies` as a verified warm row beside the cold row. An implementation that leaves its staging
    behind, writes into the target in place, or emits nothing fails one of these."""
    result = ww.warm()

    assert result["ok"] and result["tier"] == "warm" and result["state"] == "verified"
    assert ww.listing() == ["restored"]
    got = {
        p.relative_to(ww.target).as_posix(): sha(p.read_bytes())
        for p in ww.target.rglob("*")
        if p.is_file()
    }
    assert got == ww.shas
    assert result["n_files"] == 2 and result["total_bytes"] == sum(
        len(d) for d in ww.reference_files().values()
    )

    rows = {r["tier"]: r for r in ww.cw.where()["copies"]}
    assert set(rows) == {"cold", "warm"}
    warm = rows["warm"]
    assert warm["profile"] == "" and warm["host"] == socket.gethostname()
    assert warm["path"] == str(ww.target)
    assert warm["state"] == "verified" and warm["verified_at"] and warm["superseded_at"] is None
    assert warm["archive_sha256"] is None and warm["manifest_sha256"] is None
    assert warm["archive_size_bytes"] is None
    assert warm["copy_id"] == result["copy_id"] != rows["cold"]["copy_id"]
    (ev,) = [e for e in ww.cw.copy_events("output_copy.recorded") if e["data"]["tier"] == "warm"]
    assert ev["data"]["source_copy_id"] == rows["cold"]["copy_id"]
    assert ev["entity"] == [RID, warm["copy_id"]]

    from bathos.verify import verify_runlog

    findings = verify_runlog(ww.cw.w.catalog).stats["findings"]
    assert not [f for f in findings if f["type"] == "output_copies_fold_mismatch"]


def test_a_warm_row_is_never_used_as_the_source_for_the_next_warm(ww: WarmWorld):
    """Only cold/mirror rows hold an archive. Without the tier filter the second warm would try to fetch the
    first warm's directory path from myxcel."""
    ww.warm()
    gets_before = len(ww.cw.fake.calls_named("get"))

    ww.warm(ww.parent / "second")

    new_gets = ww.cw.fake.calls_named("get")[gets_before:]
    assert len(new_gets) == 2 and all(g[0] == COLD and "bathos-cold/" in g[1] for g in new_gets)


def test_the_first_verified_copy_wins_and_a_failed_cold_falls_through_to_the_mirror(
    cw: ColdWorld,  # noqa: F811
    tmp_path: Path,
):
    cw.standard_run()
    cw.ingest()
    cw.cold(mirror=MIRROR)
    parent = tmp_path / "p"
    parent.mkdir()
    cw.fake.calls.clear()  # the cold write made its own gets (resume checks)

    warm_outputs(cw.w.catalog, RID[:8], str(parent / "a"), runner=cw.fake)
    assert {g[0] for g in cw.fake.calls_named("get")} == {COLD}  # cold before mirror

    (cold_row,) = [r for r in cw.where()["copies"] if r["tier"] == "cold"]
    ww = WarmWorld.__new__(WarmWorld)
    ww.cw, ww._n = cw, 0
    ww.event(
        "output_copy.verified", cold_row["copy_id"], {"state": "failed", "verified_at": FUTURE}
    )
    cw.fake.calls.clear()
    warm_outputs(cw.w.catalog, RID[:8], str(parent / "b"), runner=cw.fake)
    assert {g[0] for g in cw.fake.calls_named("get")} == {MIRROR}


# --------------------------------------------------------------------------------------------------
# refusals before anything is fetched


def test_an_existing_target_refuses_with_zero_myxcel_calls_and_is_left_alone(ww: WarmWorld):
    """The existence check precedes the fetch: an implementation that fetches first, or that merges into an
    existing directory, fails (calls were made, or the sentinel is gone)."""
    ww.target.mkdir()
    (ww.target / "mine.txt").write_text("keep")
    calls = len(ww.cw.fake.calls)

    with pytest.raises(OutputTierError) as e:
        ww.warm()

    assert _code(e) == "warm_target_exists"
    assert len(ww.cw.fake.calls) == calls
    assert (ww.target / "mine.txt").read_text() == "keep" and ww.listing() == ["restored"]
    assert len([x for x in ww.cw.where()["copies"] if x["tier"] == "warm"]) == 0


def test_an_existing_plain_file_or_dangling_symlink_at_the_target_also_refuses(ww: WarmWorld):
    ww.target.write_text("file")
    with pytest.raises(OutputTierError) as e:
        ww.warm()
    assert _code(e) == "warm_target_exists"
    ww.target.unlink()

    ww.target.symlink_to(ww.parent / "nowhere")  # exists() is False for this; lexists is not
    with pytest.raises(OutputTierError) as e2:
        ww.warm()
    assert _code(e2) == "warm_target_exists" and ww.target.is_symlink()


def test_a_missing_parent_directory_is_refused_before_any_myxcel_call(ww: WarmWorld):
    calls = len(ww.cw.fake.calls)
    with pytest.raises(OutputTierError) as e:
        ww.warm(ww.parent / "no" / "such" / "restored")
    assert _code(e) == "invalid_param" and len(ww.cw.fake.calls) == calls
    assert ww.listing() == []


def test_no_verified_copy_refuses_and_fetches_nothing(cw: ColdWorld, tmp_path: Path):  # noqa: F811
    cw.standard_run()
    cw.ingest()  # never written to cold
    parent = tmp_path / "p"
    parent.mkdir()

    with pytest.raises(OutputTierError) as e:
        warm_outputs(cw.w.catalog, RID[:8], str(parent / "x"), runner=cw.fake)

    assert _code(e) == "no_verified_copy"
    assert cw.fake.calls_named("get") == [] and os.listdir(parent) == []


def test_a_failed_or_superseded_copy_is_not_a_source(ww: WarmWorld):
    row = ww.copy_row("cold")
    ww.cw.fake.calls.clear()  # the cold write made its own gets (resume checks)
    ww.event("output_copy.verified", row["copy_id"], {"state": "failed", "verified_at": FUTURE})
    with pytest.raises(OutputTierError) as e:
        ww.warm()
    assert _code(e) == "no_verified_copy"

    ww.event("output_copy.verified", row["copy_id"], {"state": "verified", "verified_at": FUTURE})
    ww.event(
        "output_copy.superseded", row["copy_id"], {"superseded_at": FUTURE, "renamed_to": None}
    )
    with pytest.raises(OutputTierError) as e2:
        ww.warm()
    assert _code(e2) == "no_verified_copy"
    assert ww.cw.fake.calls_named("get") == [] and ww.listing() == []


# --------------------------------------------------------------------------------------------------
# hostile and broken archives: warm_mismatch, nothing outside staging


def _evil_cases(ww: WarmWorld) -> dict[str, list[tuple[str, str, bytes | str]]]:
    good = _good_members(ww)
    return {
        "traversal": [*good, ("../evil.txt", "reg", b"pwned")],
        "absolute": [*good, ("/tmp/bth-warm-absolute-evil.txt", "reg", b"pwned")],
        "extra_member": [*good, ("out/extra.txt", "reg", b"not in the manifest")],
        "symlink_member": [*good, ("out/link", "symlink", "/etc/passwd")],
    }


@pytest.mark.parametrize("case", ["traversal", "absolute", "extra_member", "symlink_member"])
@pytest.mark.parametrize("bypass_precheck", [False, True], ids=["precheck", "extract_filter_only"])
def test_hostile_archives_are_warm_mismatch_and_nothing_is_written_outside_staging(
    ww: WarmWorld,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
    bypass_precheck: bool,
):
    """A `../`, absolute, extra or symlink member refuses `warm_mismatch`; the target is never created, the staging
    directory is gone, and the parent's listing is exactly what it was. With the pre-extract member check bypassed
    the `data` filter and the post-extract set check must still catch each one (defence in depth): an extraction
    with a permissive filter writes `evil.txt` into the parent's parent and fails the `not exists` assertion."""
    ww.plant(_evil_cases(ww)[case])
    outside = [tmp_path / "evil.txt", Path("/tmp/bth-warm-absolute-evil.txt")]
    for p in outside:
        p.unlink(missing_ok=True)
    before = (ww.listing(), sorted(os.listdir(tmp_path)))
    if bypass_precheck:
        from bathos.output_tiers.archive import ArchiveVerification

        monkeypatch.setattr(
            warm_module, "verify_archive", lambda *_a, **_k: ArchiveVerification(True, True)
        )

    with pytest.raises(OutputTierError) as e:
        ww.warm()

    assert _code(e) == "warm_mismatch"
    assert not any(p.exists() for p in outside)
    assert (ww.listing(), sorted(os.listdir(tmp_path))) == before == ([], before[1])
    assert not ww.target.exists()
    assert [x["tier"] for x in ww.cw.where()["copies"]] == ["cold"]  # no warm event


def test_a_missing_member_is_warm_mismatch(ww: WarmWorld):
    ww.plant(_good_members(ww)[:1])  # manifest still lists both files
    with pytest.raises(OutputTierError) as e:
        ww.warm()
    assert _code(e) == "warm_mismatch" and "missing" in e.value.detail
    assert ww.listing() == []


def test_a_member_whose_bytes_differ_from_the_manifest_is_warm_mismatch(ww: WarmWorld):
    files = ww.reference_files()
    rel = sorted(files)[0]
    ww.plant(
        [(r, "reg", b"TAMPERED" if r == rel else d) for r, d in sorted(files.items())],
        manifest_files=files,
    )
    with pytest.raises(OutputTierError) as e:
        ww.warm()
    assert _code(e) == "warm_mismatch" and rel in e.value.detail
    assert ww.listing() == []


def test_a_manifest_and_archive_that_agree_with_each_other_but_not_with_the_run_are_refused(
    ww: WarmWorld,
):
    """Self-consistent but wrong content (the run's reference hash is the authority, section 3.3): the manifest
    names different bytes than the run recorded."""
    files = ww.reference_files()
    rel = sorted(files)[0]
    forged = {**files, rel: b"FORGED but consistent"}
    ww.plant([(r, "reg", d) for r, d in sorted(forged.items())], manifest_files=forged)
    with pytest.raises(OutputTierError) as e:
        ww.warm()
    assert _code(e) == "warm_mismatch" and "reference" in e.value.detail and rel in e.value.detail
    assert ww.listing() == []


def test_a_manifest_missing_a_reference_file_is_refused(ww: WarmWorld):
    files = ww.reference_files()
    keep = {k: v for k, v in files.items() if k != sorted(files)[0]}
    ww.plant([(r, "reg", d) for r, d in sorted(keep.items())], manifest_files=keep)
    with pytest.raises(OutputTierError) as e:
        ww.warm()
    assert _code(e) == "warm_mismatch" and "not in the manifest" in e.value.detail


def test_the_extracted_files_are_hashed_against_reference_hashes_not_only_the_manifest(
    ww: WarmWorld, monkeypatch: pytest.MonkeyPatch
):
    """With the manifest-vs-reference pre-check neutralised, the post-extraction pass must still compare each
    extracted file with `reference_hashes`; dropping that comparison lets forged content publish."""
    files = ww.reference_files()
    rel = sorted(files)[0]
    forged = {**files, rel: b"FORGED but consistent"}
    ww.plant([(r, "reg", d) for r, d in sorted(forged.items())], manifest_files=forged)
    monkeypatch.setattr(
        warm_module,
        "_check_manifest_against_reference",
        lambda manifest, _ref: {
            f["relpath"]: f for f in json.loads(manifest.read_bytes())["files"]
        },
    )
    with pytest.raises(OutputTierError) as e:
        ww.warm()
    assert _code(e) == "warm_mismatch" and "reference hash" in e.value.detail
    assert ww.listing() == [] and not ww.target.exists()


# --------------------------------------------------------------------------------------------------
# both sha256s are checked before the tar is opened


def _no_tar(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    opened: list[object] = []

    def boom(*a, **k):
        opened.append((a, k))
        raise AssertionError("the tar was opened before the sha256 gate")

    monkeypatch.setattr(tarfile, "open", boom)
    return opened


def test_a_tampered_archive_is_refused_before_the_tar_is_opened(
    ww: WarmWorld, monkeypatch: pytest.MonkeyPatch
):
    """One flipped byte in the fetched archive: the archive sha256 (against the manifest) refuses; an
    implementation that opens the tar first (or only checks member hashes) opens it and fails the assertion."""
    _m, a_name = ww.cw.final_names()
    remote = ww.cw.fake.remote(COLD, f"{ww.cw.dest()}/{a_name}")
    data = bytearray(remote.read_bytes())
    data[len(data) // 2] ^= 0xFF
    remote.write_bytes(bytes(data))
    opened = _no_tar(monkeypatch)

    with pytest.raises(OutputTierError) as e:
        ww.warm()

    assert _code(e) == "warm_mismatch" and "sha256" in e.value.detail
    assert opened == [] and ww.listing() == []


def test_a_manifest_that_differs_from_the_copy_record_is_refused_before_the_tar_is_opened(
    ww: WarmWorld, monkeypatch: pytest.MonkeyPatch
):
    m_name, _a = ww.cw.final_names()
    remote = ww.cw.fake.remote(COLD, f"{ww.cw.dest()}/{m_name}")
    remote.write_bytes(remote.read_bytes() + b" ")
    opened = _no_tar(monkeypatch)

    with pytest.raises(OutputTierError) as e:
        ww.warm()

    assert _code(e) == "warm_mismatch" and "manifest sha256" in e.value.detail
    assert opened == [] and ww.listing() == []


def test_myxcel_get_failure_is_myxcel_error_and_writes_nothing(ww: WarmWorld):
    ww.cw.fake.fail("get", "host unreachable")
    with pytest.raises(OutputTierError) as e:
        ww.warm()
    assert _code(e) == "myxcel_error" and "host unreachable" in e.value.detail
    assert ww.listing() == []


# --------------------------------------------------------------------------------------------------
# publishing: renameat2 with RENAME_NOREPLACE


def test_the_real_renameat2_refuses_to_replace_an_empty_or_full_directory(tmp_path: Path):
    """The kernel call itself: onto an empty directory (which `os.rename` would silently replace) it raises
    FileExistsError and leaves both directories untouched; onto nothing it renames."""
    src, empty, full, fresh = (tmp_path / n for n in ("src", "empty", "full", "fresh"))
    src.mkdir()
    (src / "f").write_text("x")
    empty.mkdir()
    full.mkdir()
    (full / "keep").write_text("k")
    ino = empty.stat().st_ino

    with pytest.raises(FileExistsError):
        warm_module._renameat2_noreplace(src, empty)
    with pytest.raises(FileExistsError):
        warm_module._renameat2_noreplace(src, full)
    assert (
        (src / "f").read_text() == "x" and empty.stat().st_ino == ino and not any(empty.iterdir())
    )
    assert (full / "keep").read_text() == "k"

    warm_module._renameat2_noreplace(src, fresh)
    assert (fresh / "f").read_text() == "x" and not src.exists()


def test_an_empty_target_created_after_the_existence_check_is_not_replaced(
    ww: WarmWorld, monkeypatch: pytest.MonkeyPatch
):
    """Review cycle 2 m3. An empty `<dir>` appears between the check and the rename: the result is
    `warm_target_exists`, the empty directory is the same inode and still empty, the staging directory is
    deleted, and no warm event is recorded. A plain `os.rename` replaces the empty directory and fails this."""
    real = warm_module._renameat2_noreplace
    seen: dict[str, int] = {}

    def racing(src, dst):
        os.mkdir(dst)  # somebody else wins the race
        seen["ino"] = os.stat(dst).st_ino
        real(src, dst)

    monkeypatch.setattr(warm_module, "_renameat2_noreplace", racing)

    with pytest.raises(OutputTierError) as e:
        ww.warm()

    assert _code(e) == "warm_target_exists"
    assert os.stat(ww.target).st_ino == seen["ino"] and os.listdir(ww.target) == []
    assert ww.listing() == ["restored"]  # staging deleted, nothing else left
    assert [x["tier"] for x in ww.cw.where()["copies"]] == ["cold"]


@pytest.mark.parametrize("err", [errno.EINVAL, errno.ENOSYS])
def test_a_filesystem_without_the_flag_is_noreplace_unsupported_and_keeps_the_staging(
    ww: WarmWorld, monkeypatch: pytest.MonkeyPatch, err: int
):
    """The verified copy is kept for the owner (section 4): exactly one `.partial` directory remains, it holds
    every file, the target was never created, and no warm event claims otherwise."""

    def unsupported(_src, dst):
        raise OSError(err, os.strerror(err), str(dst))

    monkeypatch.setattr(warm_module, "_renameat2_noreplace", unsupported)

    with pytest.raises(OutputTierError) as e:
        ww.warm()

    assert _code(e) == "warm_noreplace_unsupported"
    (left,) = ww.listing()
    assert left.startswith("restored.bth-warm-") and left.endswith(".partial")
    assert left in e.value.detail
    kept = ww.parent / left
    assert {
        p.relative_to(kept).as_posix(): sha(p.read_bytes()) for p in kept.rglob("*") if p.is_file()
    } == ww.shas
    assert not ww.target.exists()
    assert [x["tier"] for x in ww.cw.where()["copies"]] == ["cold"]


def test_a_libc_without_renameat2_is_noreplace_unsupported(
    ww: WarmWorld, monkeypatch: pytest.MonkeyPatch
):
    class NoSym:
        def __getattr__(self, name):
            raise AttributeError(name)

    monkeypatch.setattr(warm_module.ctypes, "CDLL", lambda *_a, **_k: NoSym())
    with pytest.raises(OutputTierError) as e:
        ww.warm()
    assert _code(e) == "warm_noreplace_unsupported" and len(ww.listing()) == 1


def test_any_other_rename_failure_keeps_the_staging_and_is_warm_publish_failed(
    ww: WarmWorld, monkeypatch: pytest.MonkeyPatch
):
    def denied(_src, dst):
        raise PermissionError(errno.EACCES, "Permission denied", str(dst))

    monkeypatch.setattr(warm_module, "_renameat2_noreplace", denied)
    with pytest.raises(OutputTierError) as e:
        ww.warm()
    assert _code(e) == "warm_publish_failed"
    assert len(ww.listing()) == 1 and ww.listing()[0].endswith(".partial")


def test_an_interrupted_extraction_deletes_only_its_own_staging(
    ww: WarmWorld, monkeypatch: pytest.MonkeyPatch
):
    """A bystander directory with a similar name survives; the run's own staging does not."""
    bystander = ww.parent / "restored.bth-warm-feedface.partial"
    bystander.mkdir()
    (bystander / "mine").write_text("keep")

    def die(_archive, staging):
        (staging / "half").write_text("partial")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(warm_module, "_extract", die)
    with pytest.raises(OSError):
        ww.warm()
    assert ww.listing() == [bystander.name] and (bystander / "mine").read_text() == "keep"


# --------------------------------------------------------------------------------------------------
# wiring: CLI and MCP


def test_cli_warm_restores_and_a_second_run_refuses_with_the_code(ww: WarmWorld):
    runner = CyclopticRunner()
    base = ["warm", "--run-id", RID, "--catalog-dir", str(ww.cw.w.catalog)]

    ok = runner.invoke(app, [*base, "--to", str(ww.target)])
    assert ok.exit_code == 0, ok.output
    payload = json.loads(ok.stdout[ok.stdout.index("{") :])
    assert payload["path"] == str(ww.target) and payload["n_files"] == 2
    assert ww.listing() == ["restored"]

    again = runner.invoke(app, [*base, "--to", str(ww.target)])
    assert again.exit_code == 1 and "warm_target_exists" in again.stderr

    missing = runner.invoke(app, [*base])
    assert missing.exit_code != 0


def test_mcp_wrapper_returns_the_error_envelope_and_the_success_dict(ww: WarmWorld):
    ww.target.mkdir()
    refused = asyncio.run(
        mcp_warm_outputs_tool(run_id=RID, to=str(ww.target), catalog_dir=str(ww.cw.w.catalog))
    )
    assert refused["error_code"] == "warm_target_exists" and refused["resolution_hint"]
    assert refused["error"].startswith("warm_target_exists:")

    ww.target.rmdir()
    ok = warm_outputs_tool(run_id=RID, to=str(ww.target), catalog_dir=str(ww.cw.w.catalog))
    assert ok["ok"] and ok["tier"] == "warm"

    assert "error" in warm_outputs_tool(run_id=RID, catalog_dir=str(ww.cw.w.catalog))
    assert "error" in warm_outputs_tool(to=str(ww.parent / "z"), catalog_dir=str(ww.cw.w.catalog))
