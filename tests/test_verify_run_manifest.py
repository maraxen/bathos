"""verify_run_manifest over the three forms of manifest_path (spec 261026 P4, T4).

1. a `git:` URI (default lock_location), 2. a plain path that exists (tree mode), 3. a plain path that is
missing but whose lock ref still holds the blob (records relocated by `bth lock relocate`).
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest

from bathos.blast_radius import _MatchContext
from bathos.lock_ref import write_lock_ref
from bathos.prereg import verify_run_manifest, verify_run_manifest_detail
from bathos.runner import _write_manifest
from bathos.schema import Run

_RUN_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("BTH_LOCK_LOCATION", "BTH_LOCK_DIR", "BTH_IGNORE_OUTPUTS"):
        monkeypatch.delenv(var, raising=False)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(path: Path) -> tuple[Path, str]:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t.com")
    _git(path, "config", "user.name", "T")
    (path / "scripts").mkdir()
    (path / "scripts" / "x.bth.toml").write_text("[experiment]\nhypothesis = 'h'\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")
    return path, _git(path, "rev-parse", "HEAD")


def _locked_run(tmp_path: Path, sample_run: Run, **kw) -> tuple[Run, Path]:
    repo, sha = _repo(tmp_path / "repo")
    side = repo / "scripts" / "x.bth.toml"
    run = dataclasses.replace(sample_run, id=_RUN_ID, git_hash=sha, sidecar_path=str(side))
    msha, mpath = _write_manifest(run, side, "a" * 64, tmp_path / "cat", **kw)
    return dataclasses.replace(run, manifest_sha256=msha, manifest_path=mpath), repo


def test_git_uri_verifies(tmp_path, sample_run):
    run, _ = _locked_run(tmp_path, sample_run)
    assert run.manifest_path.startswith("git:")
    assert verify_run_manifest(run) is True
    detail = verify_run_manifest_detail(run)
    assert detail.ok and detail.reason == "ok"


def test_git_uri_wrong_hash_fails(tmp_path, sample_run):
    run, _ = _locked_run(tmp_path, sample_run)
    bad = dataclasses.replace(run, manifest_sha256="0" * 64)
    assert verify_run_manifest(bad) is False
    assert verify_run_manifest_detail(bad).reason == "hash_mismatch"


def test_plain_existing_path_verifies_and_detects_tamper(tmp_path, sample_run):
    run, _ = _locked_run(tmp_path, sample_run, lock_location="tree")
    assert not run.manifest_path.startswith("git:")
    assert verify_run_manifest(run) is True
    Path(run.manifest_path).write_text("tampered\n")
    assert verify_run_manifest(run) is False
    assert verify_run_manifest_detail(run).reason == "hash_mismatch"


def test_relocated_record_verifies_after_file_deleted(tmp_path, sample_run):
    """Form 3: the in-tree lock is gone, the ref (made by `lock relocate`) holds it."""
    run, repo = _locked_run(tmp_path, sample_run, lock_location="tree")
    lock = Path(run.manifest_path)
    rel = lock.resolve().relative_to(repo.resolve()).as_posix()
    write_lock_ref(repo, run.git_hash, run.id, rel, lock.read_bytes())
    lock.unlink()

    assert verify_run_manifest(run) is True


def test_plain_missing_path_without_ref_is_not_found(tmp_path, sample_run):
    run, _ = _locked_run(tmp_path, sample_run, lock_location="tree")
    Path(run.manifest_path).unlink()
    detail = verify_run_manifest_detail(run)
    assert detail.ok is False and detail.reason == "lock_not_found"


def test_git_uri_with_missing_common_dir_found_via_sidecar_ancestor(tmp_path, sample_run):
    """A record synced from another host: the URI's common dir does not exist here, but the
    sidecar's nearest existing ancestor is in a clone that holds the ref."""
    run, _ = _locked_run(tmp_path, sample_run)
    real_common = run.manifest_path[len("git:") :].split("#refs/", 1)[0]
    moved = run.manifest_path.replace(real_common, "/nonexistent/host/repo/.git", 1)
    foreign = dataclasses.replace(run, manifest_path=moved)

    assert verify_run_manifest(foreign) is True


def test_unlocatable_when_no_repo_anywhere(tmp_path, sample_run):
    run, _ = _locked_run(tmp_path, sample_run)
    elsewhere = tmp_path / "plain"
    elsewhere.mkdir()
    real_common = run.manifest_path[len("git:") :].split("#refs/", 1)[0]
    moved = run.manifest_path.replace(real_common, "/nonexistent/host/repo/.git", 1)
    foreign = dataclasses.replace(
        run, manifest_path=moved, sidecar_path=str(elsewhere / "gone" / "x.bth.toml")
    )

    detail = verify_run_manifest_detail(foreign)
    assert verify_run_manifest(foreign) is False
    assert detail.reason == "lock_unlocatable"


def test_plain_missing_path_outside_any_repo_is_unlocatable(tmp_path, sample_run):
    run = dataclasses.replace(
        sample_run,
        id=_RUN_ID,
        manifest_sha256="a" * 64,
        manifest_path=str(tmp_path / "nowhere" / "x.lock.toml"),
    )
    assert verify_run_manifest_detail(run).reason == "lock_unlocatable"


def test_nothing_recorded(sample_run):
    run = dataclasses.replace(sample_run, manifest_sha256="", manifest_path="")
    assert verify_run_manifest(run) is False
    assert verify_run_manifest_detail(run).reason == "no_manifest_recorded"


def test_common_dir_with_hash_sign_verifies(tmp_path, sample_run):
    repo, sha = _repo(tmp_path / "we#ird")
    side = repo / "scripts" / "x.bth.toml"
    run = dataclasses.replace(sample_run, id=_RUN_ID, git_hash=sha, sidecar_path=str(side))
    msha, mpath = _write_manifest(run, side, "a" * 64, tmp_path / "cat")
    assert verify_run_manifest(dataclasses.replace(run, manifest_sha256=msha, manifest_path=mpath))


def test_blast_radius_text_source_reads_git_uri(tmp_path, sample_run):
    """The `manifest text` match source must not silently drop the `git:` form."""
    run, repo = _locked_run(tmp_path, sample_run)
    text = _MatchContext(repo).text_of(run.manifest_path)
    assert text is not None and f'run_id = "{_RUN_ID}"' in text
