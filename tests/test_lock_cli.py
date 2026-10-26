"""`bth lock` group: materialize / relocate / untrack / push / fetch (spec 261026 P3, T5).

Core in `bathos.lock_admin`; CLI via the cyclopts app; MCP mirrors take a REQUIRED `project_root`, `apply`, and a
write token. Commands are dry-run by default, refuse a dirty index when they would stage, and are idempotent.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from bathos import lock_admin, mcp_auth
from bathos.catalog import init_catalog, write_run
from bathos.cli_cyclopts import app
from bathos.errors import RESOLUTION_HINTS, BathosErrorCode
from bathos.lock_admin import (
    LockAdminError,
    lock_fetch,
    lock_materialize,
    lock_push,
    lock_relocate,
    lock_untrack,
)
from bathos.lock_ref import read_lock_bytes
from bathos.runner import _write_manifest
from bathos.schema import Run
from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()
_RUN_ID = "11111111-2222-3333-4444-555555555555"
_RUN_ID2 = "99999999-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("BTH_LOCK_LOCATION", "BTH_LOCK_DIR", "BTH_IGNORE_OUTPUTS"):
        monkeypatch.delenv(var, raising=False)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _json(output: str) -> dict:
    return json.loads(output[output.index("{") :])


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "T")
    (r / "scripts" / "x.bth.toml").write_text("[experiment]\nhypothesis = 'h'\n")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "init")
    return r


def _run(sample_run: Run, repo: Path, run_id: str = _RUN_ID) -> Run:
    return dataclasses.replace(
        sample_run, id=run_id, git_hash=_git(repo, "rev-parse", "HEAD"), project_slug="proj"
    )


def _lock(sample_run, repo, tmp_path, *, location: str, run_id: str = _RUN_ID):
    run = _run(sample_run, repo, run_id)
    sha, path = _write_manifest(
        run,
        repo / "scripts" / "x.bth.toml",
        "a" * 64,
        tmp_path / "cat",
        lock_location=location,
    )
    return dataclasses.replace(run, manifest_sha256=sha, manifest_path=path)


def _relpath(run_id: str = _RUN_ID) -> str:
    return f"scripts/x.bth.{run_id}.bth.lock.toml"


# -- materialize ---------------------------------------------------------------------------------


def test_materialize_dry_run_is_default_and_writes_nothing(repo, sample_run, tmp_path):
    _lock(sample_run, repo, tmp_path, location="git")
    result = lock_materialize(repo, _RUN_ID)
    assert result["apply"] is False and result["would_write"] is True
    assert result["relpath"] == _relpath()
    assert not (repo / _relpath()).exists()


def test_materialize_apply_is_byte_identical_and_g19_parseable(repo, sample_run, tmp_path):
    run = _lock(sample_run, repo, tmp_path, location="git")
    result = lock_materialize(repo, _RUN_ID, apply=True)
    target = repo / _relpath()
    assert result["written"] is True and target.is_file()
    blob = read_lock_bytes(run.manifest_path, _RUN_ID)
    assert target.read_bytes() == blob
    assert hashlib.sha256(blob).hexdigest() == run.manifest_sha256
    manifest = tomllib.loads(target.read_text())["manifest"]  # what praxia G19 parses
    for key in ("run_id", "sidecar_sha256", "git_sha", "sidecar_path"):
        assert key in manifest
    assert manifest["run_id"] == _RUN_ID
    # committable without `add -f`: lock files are not ignored
    _git(repo, "add", _relpath())
    assert _git(repo, "diff", "--cached", "--name-only") == _relpath()


def test_materialize_is_idempotent(repo, sample_run, tmp_path):
    _lock(sample_run, repo, tmp_path, location="git")
    lock_materialize(repo, _RUN_ID, apply=True)
    again = lock_materialize(repo, _RUN_ID, apply=True)
    assert again["written"] is False and again["already_present"] is True


def test_materialize_refuses_to_overwrite_different_content(repo, sample_run, tmp_path):
    _lock(sample_run, repo, tmp_path, location="git")
    (repo / _relpath()).write_text("something else\n")
    with pytest.raises(LockAdminError) as ei:
        lock_materialize(repo, _RUN_ID, apply=True)
    assert ei.value.code is BathosErrorCode.INVALID_PARAM
    assert (repo / _relpath()).read_text() == "something else\n"


def test_materialize_unknown_run_is_lock_not_found(repo):
    with pytest.raises(LockAdminError) as ei:
        lock_materialize(repo, "no-such-run")
    assert ei.value.code is BathosErrorCode.LOCK_NOT_FOUND


# -- relocate ------------------------------------------------------------------------------------


def _catalog_with(run: Run, tmp_path: Path) -> Path:
    cat = tmp_path / "cat"
    init_catalog(cat)
    write_run(run, cat)
    return cat


def test_relocate_never_lists_tracked_lock_as_deletable(repo, sample_run, tmp_path):
    run = _lock(sample_run, repo, tmp_path, location="tree", run_id=_RUN_ID)
    _git(repo, "add", _relpath())
    _git(repo, "commit", "-qm", "commit lock (G19 evidence)")
    cat = _catalog_with(run, tmp_path)

    result = lock_relocate(repo, cat)

    by_path = {e["relpath"]: e for e in result["locks"]}
    entry = by_path[_relpath()]
    assert entry["tracked"] is True
    assert entry["action"] == "keep (committed evidence)"
    assert result["safe_to_delete"] == []


def test_relocate_marks_matching_untracked_lock_safe_and_never_deletes(repo, sample_run, tmp_path):
    run = _lock(sample_run, repo, tmp_path, location="tree")
    cat = _catalog_with(run, tmp_path)

    dry = lock_relocate(repo, cat)
    assert dry["apply"] is False
    assert [e["relpath"] for e in dry["locks"] if e["action"] == "safe to delete"] == [_relpath()]
    assert _git(repo, "for-each-ref", "refs/bathos/locks") == ""  # dry-run created nothing

    applied = lock_relocate(repo, cat, apply=True)
    assert applied["safe_to_delete"] == [_relpath()]
    assert (repo / _relpath()).is_file()  # never deletes
    ref_blob = _git(repo, "show", f"refs/bathos/locks/{_RUN_ID}:{_relpath()}")
    assert ref_blob == (repo / _relpath()).read_text().rstrip("\n")


def test_relocate_unmatched_untracked_is_not_safe(repo, sample_run, tmp_path):
    run = _lock(sample_run, repo, tmp_path, location="tree")
    cat = _catalog_with(dataclasses.replace(run, manifest_sha256="0" * 64), tmp_path)
    result = lock_relocate(repo, cat)
    assert result["safe_to_delete"] == []
    assert result["locks"][0]["action"] != "safe to delete"


def test_relocate_is_idempotent(repo, sample_run, tmp_path):
    run = _lock(sample_run, repo, tmp_path, location="tree")
    cat = _catalog_with(run, tmp_path)
    lock_relocate(repo, cat, apply=True)
    tip = _git(repo, "rev-parse", f"refs/bathos/locks/{_RUN_ID}")
    lock_relocate(repo, cat, apply=True)
    assert _git(repo, "rev-parse", f"refs/bathos/locks/{_RUN_ID}") == tip


# -- untrack -------------------------------------------------------------------------------------


def _track_manifest(repo: Path) -> Path:
    manifest = repo / ".bth" / "refs" / "manifest.jsonl"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"run_id": "r1"}\n')
    _git(repo, "add", "-f", ".bth/refs/manifest.jsonl")
    _git(repo, "commit", "-qm", "legacy tracked manifest")
    manifest.write_text('{"run_id": "r1"}\n{"run_id": "r2"}\n')  # uncommitted local append
    return manifest


def test_untrack_dry_run_default_changes_nothing_and_prints_caveats(repo):
    _track_manifest(repo)
    result = lock_untrack(repo)
    assert result["apply"] is False
    assert result["tracked"] == [".bth/refs/manifest.jsonl"]
    assert _git(repo, "diff", "--cached", "--name-only") == ""
    text = " ".join(result["caveats"])
    assert "G31" in text and "SNAPSHOT_UNPROVEN" in text
    assert "modify/delete" in text


def test_untrack_apply_stages_deletion_keeps_disk_file_and_ignores(repo):
    manifest = _track_manifest(repo)
    before = manifest.read_bytes()

    result = lock_untrack(repo, apply=True)

    assert manifest.read_bytes() == before  # on-disk file never touched
    assert _git(repo, "diff", "--cached", "--name-only") == ".bth/refs/manifest.jsonl"
    assert "G31" in " ".join(result["caveats"])
    assert result["ignored"]["/.bth/refs/manifest.jsonl"] is True
    _git(repo, "commit", "-qm", "untrack")
    assert _git(repo, "status", "--porcelain") == ""  # not reappearing as untracked


def test_untrack_never_touches_locks(repo, sample_run, tmp_path):
    _lock(sample_run, repo, tmp_path, location="tree")
    _git(repo, "add", _relpath())
    _git(repo, "commit", "-qm", "lock")
    result = lock_untrack(repo, apply=True)
    assert result["tracked"] == []
    assert _git(repo, "ls-files", _relpath()) == _relpath()


def test_untrack_refuses_dirty_index(repo):
    _track_manifest(repo)
    (repo / "other.txt").write_text("x\n")
    _git(repo, "add", "other.txt")
    with pytest.raises(LockAdminError) as ei:
        lock_untrack(repo, apply=True)
    assert ei.value.code is BathosErrorCode.INDEX_DIRTY
    assert _git(repo, "ls-files", ".bth/refs/manifest.jsonl") == ".bth/refs/manifest.jsonl"


def test_untrack_is_idempotent(repo):
    _track_manifest(repo)
    lock_untrack(repo, apply=True)
    _git(repo, "commit", "-qm", "untrack")
    again = lock_untrack(repo, apply=True)
    assert again["tracked"] == []


# -- push / fetch --------------------------------------------------------------------------------


@pytest.fixture
def remote_pair(repo, sample_run, tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(repo, "remote", "add", "origin", str(bare))
    _lock(sample_run, repo, tmp_path, location="git")
    return bare


def test_push_dry_run_then_apply_then_fetch(repo, remote_pair, tmp_path):
    dry = lock_push(repo)
    assert dry["apply"] is False
    assert _git(remote_pair, "for-each-ref", "refs/bathos/locks") == ""

    lock_push(repo, apply=True)
    assert _RUN_ID in _git(remote_pair, "for-each-ref", "refs/bathos/locks")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(remote_pair), str(clone)], check=True)
    assert _git(clone, "for-each-ref", "refs/bathos/locks") == ""
    planned = lock_fetch(clone)
    assert planned["apply"] is False and _RUN_ID in planned["output"]
    assert _git(clone, "for-each-ref", "refs/bathos/locks") == ""
    lock_fetch(clone, apply=True)
    assert _RUN_ID in _git(clone, "for-each-ref", "refs/bathos/locks")


def test_push_without_remote_is_an_error(repo):
    with pytest.raises(LockAdminError):
        lock_push(repo, remote="nope", apply=True)


# -- CLI -----------------------------------------------------------------------------------------


def test_cli_materialize_dry_run_then_apply(repo, sample_run, tmp_path, monkeypatch):
    _lock(sample_run, repo, tmp_path, location="git")
    monkeypatch.chdir(repo)  # --project-root defaults to the git toplevel of cwd
    dry = runner.invoke(app, ["lock", "materialize", "--run-id", _RUN_ID])
    assert dry.exit_code == 0, dry.output
    assert _json(dry.output)["would_write"] is True
    assert not (repo / _relpath()).exists()
    applied = runner.invoke(app, ["lock", "materialize", "--run-id", _RUN_ID, "--apply"])
    assert applied.exit_code == 0, applied.output
    assert (repo / _relpath()).is_file()


def test_cli_untrack_prints_caveat(repo, monkeypatch):
    _track_manifest(repo)
    monkeypatch.chdir(repo)
    result = runner.invoke(app, ["lock", "untrack"])
    assert result.exit_code == 0, result.output
    assert "G31" in result.output


def test_cli_error_exits_nonzero_with_message(repo, monkeypatch):
    monkeypatch.chdir(repo)
    result = runner.invoke(app, ["lock", "materialize", "--run-id", "missing"])
    assert result.exit_code != 0
    assert "no lock ref" in result.output


def test_error_envelope_has_code_and_hint(repo):
    from bathos.mcp import lock_materialize_tool

    payload = lock_materialize_tool(run_id="missing", project_root=str(repo))
    assert payload["ok"] is False
    assert payload["error_code"] == BathosErrorCode.LOCK_NOT_FOUND.value
    assert payload["resolution_hint"]


# -- MCP mirrors ---------------------------------------------------------------------------------


@pytest.fixture
def token_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BTH_MCP_TOKEN_PATH", str(tmp_path / "mcp_token"))
    monkeypatch.setattr(mcp_auth, "_stdio_transport", False)
    monkeypatch.delenv("BTH_MCP_REQUIRE_TOKEN", raising=False)
    return mcp_auth.get_or_create_token()


# Token gating of the five write tools is covered by tests/test_mcp_auth.py (WRITE_TOOLS).


def test_mcp_project_root_is_required(token_env):
    from bathos import mcp

    result = asyncio.run(mcp.mcp_lock_untrack_tool(project_root="", token=token_env))
    assert result["error_code"] == BathosErrorCode.INVALID_PARAM.value


def test_mcp_apply_flag_and_dry_run_default(repo, token_env):
    from bathos import mcp

    _track_manifest(repo)
    dry = asyncio.run(mcp.mcp_lock_untrack_tool(project_root=str(repo), token=token_env))
    assert dry["apply"] is False
    assert _git(repo, "diff", "--cached", "--name-only") == ""
    done = asyncio.run(
        mcp.mcp_lock_untrack_tool(project_root=str(repo), apply=True, token=token_env)
    )
    assert done["apply"] is True
    assert _git(repo, "diff", "--cached", "--name-only") == ".bth/refs/manifest.jsonl"


# -- error codes ---------------------------------------------------------------------------------


def test_new_error_codes_have_hints():
    for code in (
        BathosErrorCode.LOCK_NOT_FOUND,
        BathosErrorCode.INDEX_DIRTY,
        BathosErrorCode.LOCK_REF_WRITE_FAILED,
    ):
        assert RESOLUTION_HINTS[code]


def test_lock_admin_error_is_registered():
    from bathos.errors import EXCEPTION_TO_CODE

    assert "LockAdminError" in EXCEPTION_TO_CODE
    assert lock_admin.LockAdminError.__name__ == "LockAdminError"
