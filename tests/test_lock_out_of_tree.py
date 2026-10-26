"""Sidecar lock written as a commit ref, not a working-tree file (spec 261026, P1/T2)."""

from __future__ import annotations

import dataclasses
import hashlib
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from bathos import lock_ref, runner
from bathos.catalog import init_catalog, read_runs
from bathos.init import init_project
from bathos.lock_ref import (
    LockRefError,
    parse_lock_uri,
    read_lock_text,
    write_lock_ref,
)
from bathos.runner import _write_manifest, run_script
from bathos.schema import Run

_SIDECAR = "[experiment]\nhypothesis = 'h'\n"
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
    (path / "scripts" / "x.bth.toml").write_text(_SIDECAR)
    (path / "scripts" / "x.py").write_text("print('hi')\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")
    return path, _git(path, "rev-parse", "HEAD")


def _run(sample_run: Run, git_hash: str) -> Run:
    return dataclasses.replace(sample_run, id=_RUN_ID, git_hash=git_hash)


def _porcelain(repo: Path) -> str:
    return _git(repo, "status", "--porcelain")


def test_ref_lock_leaves_tree_clean_and_body_matches_hash(tmp_path, sample_run):
    repo, sha = _repo(tmp_path / "repo")
    run = _run(sample_run, sha)
    side = repo / "scripts" / "x.bth.toml"

    msha, mpath = _write_manifest(run, side, "a" * 64, tmp_path / "cat", cwd=tmp_path)

    assert _porcelain(repo) == ""
    assert not list((repo / "scripts").glob("*.bth.lock.toml"))
    assert mpath.startswith("git:")
    text = read_lock_text(mpath)
    assert hashlib.sha256(text.encode()).hexdigest() == msha
    parsed = tomllib.loads(text)["manifest"]
    assert parsed["run_id"] == _RUN_ID and parsed["git_sha"] == sha


def test_ref_uses_g19_relpath_and_carrier_parent(tmp_path, sample_run):
    repo, sha = _repo(tmp_path / "repo")
    run = _run(sample_run, sha)
    _write_manifest(run, repo / "scripts" / "x.bth.toml", "a" * 64, tmp_path / "cat", cwd=tmp_path)
    ref = f"refs/bathos/locks/{_RUN_ID}"
    # Path("x.bth.toml").stem is "x.bth": the same name tree mode uses
    rel = f"scripts/x.bth.{_RUN_ID}.bth.lock.toml"
    assert _git(repo, "cat-file", "-e", f"{ref}:{rel}") == ""
    assert _git(repo, "rev-parse", f"{ref}^") == sha
    # carrier = HEAD's tree plus the lock
    assert _git(repo, "show", f"{ref}:scripts/x.py") == "print('hi')"


def test_tree_opt_out_is_unchanged(tmp_path, sample_run):
    repo, sha = _repo(tmp_path / "repo")
    run = _run(sample_run, sha)
    side = repo / "scripts" / "x.bth.toml"

    msha, mpath = _write_manifest(
        run, side, "a" * 64, tmp_path / "cat", lock_location="tree", cwd=tmp_path
    )

    expected = (repo / "scripts" / f"x.bth.{_RUN_ID}.bth.lock.toml").resolve()
    assert mpath == str(expected)
    assert hashlib.sha256(expected.read_bytes()).hexdigest() == msha
    assert _git(repo, "for-each-ref", "refs/bathos/locks") == ""


@pytest.mark.parametrize("hash_", ["", "unknown", "nogit"])
def test_sentinel_hash_falls_back_to_lock_dir(tmp_path, sample_run, hash_):
    repo, _ = _repo(tmp_path / "repo")
    run = _run(sample_run, hash_)
    cat = tmp_path / "cat"

    _, mpath = _write_manifest(run, repo / "scripts" / "x.bth.toml", "a" * 64, cat, cwd=tmp_path)

    assert Path(mpath) == (cat / "locks" / run.project_slug / f"{_RUN_ID}.bth.lock.toml").resolve()
    assert Path(mpath).is_file()
    assert not list((repo / "scripts").glob("*.bth.lock.toml"))
    assert _git(repo, "for-each-ref", "refs/bathos/locks") == ""


def test_no_repo_falls_back(tmp_path, sample_run):
    plain = tmp_path / "plain"
    plain.mkdir()
    side = plain / "x.bth.toml"
    side.write_text(_SIDECAR)
    run = _run(sample_run, "a" * 40)

    _, mpath = _write_manifest(run, side, "a" * 64, tmp_path / "cat", cwd=tmp_path)

    assert str(tmp_path / "cat" / "locks") in mpath
    assert not list(plain.glob("*.bth.lock.toml"))


def test_sidecar_outside_repo_falls_back(tmp_path, sample_run, monkeypatch):
    repo, sha = _repo(tmp_path / "repo")
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr("bathos.lock_config._show_toplevel", lambda _d: other)

    _, mpath = _write_manifest(
        _run(sample_run, sha),
        repo / "scripts" / "x.bth.toml",
        "a" * 64,
        tmp_path / "cat",
        cwd=tmp_path,
    )

    assert str(tmp_path / "cat" / "locks") in mpath


def test_lock_dir_none_disables_fallback_non_autonomous(tmp_path, sample_run, monkeypatch):
    monkeypatch.setenv("BTH_LOCK_DIR", "none")
    repo, _ = _repo(tmp_path / "repo")
    run = _run(sample_run, "unknown")

    assert _write_manifest(
        run, repo / "scripts" / "x.bth.toml", "a" * 64, tmp_path / "cat", cwd=tmp_path
    ) == (
        "",
        "",
    )


@pytest.mark.parametrize("slug", ["../../escape", "/abs/slug", "a/b", "..", ""])
def test_fallback_confines_hostile_project_slug(tmp_path, sample_run, slug):
    repo, _ = _repo(tmp_path / "repo")
    cat = tmp_path / "cat"
    run = dataclasses.replace(_run(sample_run, "unknown"), project_slug=slug)

    _, mpath = _write_manifest(run, repo / "scripts" / "x.bth.toml", "a" * 64, cat, cwd=tmp_path)

    assert Path(mpath).resolve().is_relative_to((cat / "locks").resolve())
    assert not (tmp_path / "escape").exists()


def test_no_lock_written_is_not_silent(tmp_path, sample_run, monkeypatch, capsys):
    monkeypatch.setenv("BTH_LOCK_DIR", "none")
    repo, _ = _repo(tmp_path / "repo")
    run = _run(sample_run, "unknown")

    assert _write_manifest(
        run, repo / "scripts" / "x.bth.toml", "a" * 64, tmp_path / "cat", cwd=tmp_path
    ) == ("", "")

    err = capsys.readouterr().err
    assert "no lock was written" in err and "sentinel_hash" in err


def test_lock_dir_none_is_fatal_when_autonomous(tmp_path, sample_run, monkeypatch):
    monkeypatch.setenv("BTH_LOCK_DIR", "none")
    repo, _ = _repo(tmp_path / "repo")
    run = dataclasses.replace(_run(sample_run, "unknown"), agent_mode="autonomous")

    with pytest.raises(RuntimeError):
        _write_manifest(
            run, repo / "scripts" / "x.bth.toml", "a" * 64, tmp_path / "cat", cwd=tmp_path
        )


def test_ref_write_failure_falls_back_and_records_ref_error(tmp_path, sample_run, monkeypatch):
    repo, sha = _repo(tmp_path / "repo")

    def boom(*_a, **_k):
        raise LockRefError("injected")

    monkeypatch.setattr(runner, "write_lock_ref", boom)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(runner, "event", lambda name, **kw: events.append((name, kw)))

    _, mpath = _write_manifest(
        _run(sample_run, sha),
        repo / "scripts" / "x.bth.toml",
        "a" * 64,
        tmp_path / "cat",
        cwd=tmp_path,
    )

    assert str(tmp_path / "cat" / "locks") in mpath
    name, data = next(e for e in events if e[0] == "run.lock_written")
    assert data["location"] == "fallback"
    assert "injected" in data["ref_error"]


def test_lock_written_event_for_ref(tmp_path, sample_run, monkeypatch):
    repo, sha = _repo(tmp_path / "repo")
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(runner, "event", lambda name, **kw: events.append((name, kw)))

    _write_manifest(
        _run(sample_run, sha),
        repo / "scripts" / "x.bth.toml",
        "a" * 64,
        tmp_path / "cat",
        cwd=tmp_path,
    )

    _, data = next(e for e in events if e[0] == "run.lock_written")
    assert data["location"] == "ref" and data["parentless"] is False and data["ref_error"] is None
    assert data["layers"]["lock_location"] == "default"


def test_parentless_when_hash_absent(tmp_path):
    repo, _ = _repo(tmp_path / "repo")
    absent = "1" * 40
    body = b"lock \xe2\x80\x94 body\n"
    rel = "scripts/x.r.bth.lock.toml"

    result = write_lock_ref(repo, absent, "rid", rel, body)

    assert result.parentless is True
    ref = "refs/bathos/locks/rid"
    assert _git(repo, "rev-list", "--count", ref) == "1"
    assert read_lock_text(result.uri).encode() == body


def test_common_dir_containing_hash_sign(tmp_path, sample_run):
    repo, sha = _repo(tmp_path / "we#ird:dir")
    run = _run(sample_run, sha)

    _, mpath = _write_manifest(
        run, repo / "scripts" / "x.bth.toml", "a" * 64, tmp_path / "cat", cwd=tmp_path
    )

    common, ref, rel = parse_lock_uri(mpath, run.id)
    assert "we#ird:dir" in str(common) and ref == f"refs/bathos/locks/{_RUN_ID}"
    assert rel.endswith(".bth.lock.toml")
    # run-id-free parse agrees
    assert parse_lock_uri(mpath) == (common, ref, rel)
    assert tomllib.loads(read_lock_text(mpath, run.id))["manifest"]["run_id"] == _RUN_ID


def test_parse_lock_uri_rejects_non_uri():
    with pytest.raises(ValueError):
        parse_lock_uri("/plain/path")
    with pytest.raises(ValueError):
        parse_lock_uri("git:/x/.git")


def test_read_lock_text_plain_path(tmp_path):
    f = tmp_path / "a.lock.toml"
    f.write_text("x = 1\n")
    assert read_lock_text(str(f)) == "x = 1\n"


def test_readback_mismatch_is_a_write_failure(tmp_path, monkeypatch):
    repo, sha = _repo(tmp_path / "repo")
    real = lock_ref._git

    def tampered(*args, **kw):
        proc = real(*args, **kw)
        if args and args[0] == "cat-file" and args[1] == "blob":
            proc.stdout = b"tampered"
        return proc

    monkeypatch.setattr(lock_ref, "_git", tampered)
    with pytest.raises(LockRefError, match="read-back"):
        write_lock_ref(repo, sha, "rid", "scripts/x.lock", b"body\n")
    # No dangling ref holding the wrong bytes is left behind.
    monkeypatch.undo()
    assert _git(repo, "for-each-ref", "refs/bathos/locks") == ""


def test_existing_lock_ref_is_never_overwritten(tmp_path):
    repo, sha = _repo(tmp_path / "repo")
    first = write_lock_ref(repo, sha, "rid", "scripts/x.lock", b"first\n")
    with pytest.raises(LockRefError, match="could not create"):
        write_lock_ref(repo, sha, "rid", "scripts/x.lock", b"second\n")
    assert _git(repo, "rev-parse", "refs/bathos/locks/rid") == first.commit
    assert read_lock_text(first.uri).encode() == b"first\n"


def _enforced_project(tmp_path: Path):
    repo = tmp_path / "repo"
    catalog = tmp_path / "catalog"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "T")
    init_project(repo, slug="p", catalog_dir=catalog)
    scripts = repo / "scripts" / "experiments"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "run_x.py").write_text("print('hi')\n")
    (scripts / "run_x.bth.toml").write_text(
        "[experiment]\nhypothesis = 'h'\n[outcomes.pass]\ncondition = 'x == 1'\ndecision = 'good'\n"
        "reasoning = 'r'\n[outcomes.fallback]\ncondition = '1==1'\ndecision = 'o'\nreasoning = 'c'\n"
        "is_residual = true\n[result_schema]\nx = 'float'\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "scripts")
    init_catalog(catalog)
    return repo, catalog, scripts / "run_x.py"


def test_bth_run_leaves_no_lock_in_tree_and_hash_matches_record(tmp_path):
    repo, catalog, script = _enforced_project(tmp_path)

    rc = run_script(
        argv=[sys.executable, str(script)],
        project_slug="p",
        catalog_dir=catalog,
        output_paths=[],
        tags=[],
        cwd=repo,
    )

    assert rc == 0
    assert not list(repo.rglob("*.bth.lock.toml"))
    run = read_runs(catalog)[-1]
    assert run.manifest_path.startswith("git:")
    assert (
        hashlib.sha256(read_lock_text(run.manifest_path, run.id).encode()).hexdigest()
        == run.manifest_sha256
    )
    assert _git(repo, "rev-parse", f"refs/bathos/locks/{run.id}^") == run.git_hash
