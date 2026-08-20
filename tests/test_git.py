import json
import subprocess
from pathlib import Path

import pytest

from bathos.git import capture_git_state


def test_captures_state_in_git_repo(tmp_path: Path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True
    )
    (tmp_path / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    state = capture_git_state(tmp_path)
    assert len(state.hash) == 40
    assert state.branch in ("main", "master")
    assert state.dirty is False
    assert state.provenance_source == "git"


def test_detects_dirty_state(tmp_path: Path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True
    )
    (tmp_path / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "file.txt").write_text("modified")
    state = capture_git_state(tmp_path)
    assert state.dirty is True


def test_returns_sentinel_outside_git_repo(tmp_path: Path):
    state = capture_git_state(tmp_path)
    assert state.hash == "unknown"
    assert state.branch == "unknown"
    assert state.dirty is False
    assert state.provenance_source == "none"


# Test env channel
def test_no_channel_behavior_is_identical_to_legacy(tmp_path: Path):
    """No channel, no repo → ('unknown','unknown',False, 'none')."""
    state = capture_git_state(tmp_path)
    assert state.hash == "unknown"
    assert state.branch == "unknown"
    assert state.dirty is False
    assert state.provenance_source == "none"


def test_env_channel_used_when_no_repo(tmp_path: Path, monkeypatch):
    """Env channel is used when no git repo exists."""
    test_sha = "a" * 40
    monkeypatch.setenv("MYXCEL_PROVENANCE_SCHEMA", "1")
    monkeypatch.setenv("MYXCEL_PROVENANCE_STATUS", "git")
    monkeypatch.setenv("MYXCEL_GIT_SHA", test_sha)
    monkeypatch.setenv("MYXCEL_GIT_BRANCH", "test-branch")
    monkeypatch.setenv("MYXCEL_GIT_DIRTY", "0")
    monkeypatch.setenv("MYXCEL_PROVENANCE_ROOT", str(tmp_path))

    state = capture_git_state(tmp_path)
    assert state.hash == test_sha
    assert state.branch == "test-branch"
    assert state.dirty is False
    assert state.provenance_source == "myxcel-env"


def test_env_channel_ignored_when_cwd_outside_provenance_root(tmp_path: Path, monkeypatch):
    """D5: env channel ignored when cwd not inside MYXCEL_PROVENANCE_ROOT."""
    subdir = tmp_path / "subdir"
    subdir.mkdir()

    test_sha = "a" * 40
    monkeypatch.setenv("MYXCEL_PROVENANCE_SCHEMA", "1")
    monkeypatch.setenv("MYXCEL_PROVENANCE_STATUS", "git")
    monkeypatch.setenv("MYXCEL_GIT_SHA", test_sha)
    monkeypatch.setenv("MYXCEL_GIT_BRANCH", "test-branch")
    monkeypatch.setenv("MYXCEL_GIT_DIRTY", "0")
    # Set provenance root to a different directory
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    monkeypatch.setenv("MYXCEL_PROVENANCE_ROOT", str(other_dir))

    # cwd is subdir, which is not inside other_dir
    state = capture_git_state(subdir)
    # Should fall through to legacy behavior (no repo) → unknown
    assert state.hash == "unknown"
    assert state.provenance_source == "none"


def test_env_channel_rejects_unrecognised_provenance_status(tmp_path: Path, monkeypatch):
    """D4: _env_channel must reject a missing/unrecognised MYXCEL_PROVENANCE_STATUS the
    same way _sidecar_channel rejects an invalid sidecar status -- a garbage or absent
    status must not let whatever git_sha happens to accompany it carry through. Env and
    sidecar are "one schema, one parser" per the spec; this locks the symmetry."""
    monkeypatch.setenv("MYXCEL_PROVENANCE_SCHEMA", "1")
    monkeypatch.setenv("MYXCEL_PROVENANCE_STATUS", "totally-bogus-status")
    monkeypatch.setenv("MYXCEL_GIT_SHA", "9" * 40)
    monkeypatch.setenv("MYXCEL_GIT_BRANCH", "weird-branch")
    monkeypatch.setenv("MYXCEL_GIT_DIRTY", "0")
    monkeypatch.setenv("MYXCEL_PROVENANCE_ROOT", str(tmp_path))

    state = capture_git_state(tmp_path)
    assert state.hash == "unknown"
    assert state.provenance_source == "none"


# Test sidecar channel
def test_sidecar_channel_used_when_no_env(tmp_path: Path):
    """Sidecar channel is used when no env vars set."""
    test_sha = "b" * 40
    sidecar_data = {
        "schema_version": 1,
        "provenance_status": "git",
        "git_sha": test_sha,
        "git_branch": "feature",
        "git_dirty": False,
        "dirty_content_id": None,
        "capture_stage": "push",
        "sync_state": "verified",
        "computed_at": "2026-08-20T14:00:00Z",
        "provenance_root": str(tmp_path),
        "remote": "test",
        "project": "testproj",
        "worktree": None,
        "myxcel_version": "0.1.0",
    }
    sidecar_file = tmp_path / ".myxcel_provenance.json"
    sidecar_file.write_text(json.dumps(sidecar_data))

    state = capture_git_state(tmp_path)
    assert state.hash == test_sha
    assert state.branch == "feature"
    assert state.dirty is False
    assert state.provenance_source == "myxcel-sidecar"


def test_env_channel_beats_sidecar(tmp_path: Path, monkeypatch):
    """Env channel beats sidecar when both present."""
    # Create sidecar
    test_sha_sidecar = "c" * 40
    sidecar_data = {
        "schema_version": 1,
        "provenance_status": "git",
        "git_sha": test_sha_sidecar,
        "git_branch": "sidecar-branch",
        "git_dirty": False,
        "dirty_content_id": None,
        "capture_stage": "push",
        "sync_state": "verified",
        "computed_at": "2026-08-20T14:00:00Z",
        "provenance_root": str(tmp_path),
        "remote": "test",
        "project": "testproj",
        "worktree": None,
        "myxcel_version": "0.1.0",
    }
    sidecar_file = tmp_path / ".myxcel_provenance.json"
    sidecar_file.write_text(json.dumps(sidecar_data))

    # Set env vars with different values
    test_sha_env = "d" * 40
    monkeypatch.setenv("MYXCEL_PROVENANCE_SCHEMA", "1")
    monkeypatch.setenv("MYXCEL_PROVENANCE_STATUS", "git")
    monkeypatch.setenv("MYXCEL_GIT_SHA", test_sha_env)
    monkeypatch.setenv("MYXCEL_GIT_BRANCH", "env-branch")
    monkeypatch.setenv("MYXCEL_GIT_DIRTY", "0")
    monkeypatch.setenv("MYXCEL_PROVENANCE_ROOT", str(tmp_path))

    state = capture_git_state(tmp_path)
    # Env should win
    assert state.hash == test_sha_env
    assert state.branch == "env-branch"
    assert state.provenance_source == "myxcel-env"


def test_real_repo_at_provenance_root_beats_both_channels(tmp_path: Path, monkeypatch):
    """D6: a real repo at provenance root beats both channels."""
    # Create a real git repo
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True
    )
    (tmp_path / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    real_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()

    # Set conflicting env vars
    fake_sha = "e" * 40
    monkeypatch.setenv("MYXCEL_PROVENANCE_SCHEMA", "1")
    monkeypatch.setenv("MYXCEL_PROVENANCE_STATUS", "git")
    monkeypatch.setenv("MYXCEL_GIT_SHA", fake_sha)
    monkeypatch.setenv("MYXCEL_GIT_BRANCH", "fake-branch")
    monkeypatch.setenv("MYXCEL_GIT_DIRTY", "0")
    monkeypatch.setenv("MYXCEL_PROVENANCE_ROOT", str(tmp_path))

    state = capture_git_state(tmp_path)
    # Real repo should win
    assert state.hash == real_sha
    assert state.branch in ("main", "master")
    assert state.provenance_source == "git"


def test_ancestor_repo_at_different_root_does_not_beat_channel(tmp_path: Path, monkeypatch):
    """D6: ancestor repo at different root does NOT beat channel (cluster hijack case)."""
    parent_dir = tmp_path / "parent"
    parent_dir.mkdir()
    project_dir = parent_dir / "project"
    project_dir.mkdir()

    # Create a repo in parent
    subprocess.run(["git", "init"], cwd=parent_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=parent_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=parent_dir, check=True, capture_output=True
    )
    (parent_dir / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=parent_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=parent_dir, check=True, capture_output=True
    )

    # Set env to point to project_dir (different from parent)
    channel_sha = "f" * 40
    monkeypatch.setenv("MYXCEL_PROVENANCE_SCHEMA", "1")
    monkeypatch.setenv("MYXCEL_PROVENANCE_STATUS", "git")
    monkeypatch.setenv("MYXCEL_GIT_SHA", channel_sha)
    monkeypatch.setenv("MYXCEL_GIT_BRANCH", "channel-branch")
    monkeypatch.setenv("MYXCEL_GIT_DIRTY", "0")
    monkeypatch.setenv("MYXCEL_PROVENANCE_ROOT", str(project_dir))

    state = capture_git_state(project_dir)
    # Channel should win (ancestor repo is at different root)
    assert state.hash == channel_sha
    assert state.branch == "channel-branch"
    assert state.provenance_source == "myxcel-env"


def test_sidecar_ascent_stops_at_dot_git_directory(tmp_path: Path):
    """Sidecar ascent stops at .git directory and returns None (stops searching)."""
    # Create git repo in tmp_path with a commit
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True
    )
    (tmp_path / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    # When we search from tmp_path, we should find the .git and stop searching
    state = capture_git_state(tmp_path)
    # Real git repo at tmp_path
    assert state.provenance_source == "git"


def test_sidecar_ascent_stops_at_dot_git_file_in_linked_worktree(tmp_path: Path):
    """Sidecar ascent must stop at a `.git` FILE (linked worktree), not just a `.git`
    directory -- otherwise ascent would climb past the worktree boundary and pick up a
    stale sidecar left at an ancestor (e.g. a shared cluster workspace root), silently
    misattributing provenance. This is the exact worktree edge case (`.git` as a file,
    not a directory) that motivated this whole feature (D3), and the prior
    test_sidecar_ascent_stops_at_dot_git_directory only exercised the directory form."""
    from bathos.git import _sidecar_channel

    main_repo = tmp_path / "main_repo"
    main_repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=main_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=main_repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=main_repo, check=True, capture_output=True,
    )
    (main_repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=main_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=main_repo, check=True, capture_output=True)

    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature", str(worktree)],
        cwd=main_repo, check=True, capture_output=True,
    )
    assert (worktree / ".git").is_file()  # confirms the setup exercises the file case, not dir

    subdir = worktree / "nested" / "deeper"
    subdir.mkdir(parents=True)

    # A sidecar placed OUTSIDE the worktree, at an ancestor -- ascent must never reach it,
    # since it would have to pass through (and stop at) worktree/.git first.
    outer_sidecar = tmp_path / ".myxcel_provenance.json"
    outer_sidecar.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provenance_status": "git",
                "git_sha": "f" * 40,
                "provenance_root": str(tmp_path),
            }
        )
    )

    result = _sidecar_channel(subdir)
    assert result is None


def test_sidecar_ascent_is_bounded_to_eight_levels(tmp_path: Path):
    """Sidecar ascent is bounded to 8 levels."""
    # Create deeply nested directory beyond 8 levels
    deep_dir = tmp_path
    for i in range(9):
        deep_dir = deep_dir / f"level{i}"
    deep_dir.mkdir(parents=True)

    # Create sidecar at tmp_path (would be 9 levels up from deep_dir)
    sidecar_data = {
        "schema_version": 1,
        "provenance_status": "git",
        "git_sha": "1" * 40,
        "git_branch": "test",
        "git_dirty": False,
        "dirty_content_id": None,
        "capture_stage": "push",
        "sync_state": "verified",
        "computed_at": "2026-08-20T14:00:00Z",
        "provenance_root": str(tmp_path),
        "remote": "test",
        "project": "testproj",
        "worktree": None,
        "myxcel_version": "0.1.0",
    }
    sidecar_file = tmp_path / ".myxcel_provenance.json"
    sidecar_file.write_text(json.dumps(sidecar_data))

    state = capture_git_state(deep_dir)
    # Should not find sidecar (too deep)
    assert state.hash == "unknown"


def test_status_nogit_maps_to_hash_nogit(tmp_path: Path, monkeypatch):
    """D4: status='nogit' maps to hash='nogit'."""
    monkeypatch.setenv("MYXCEL_PROVENANCE_SCHEMA", "1")
    monkeypatch.setenv("MYXCEL_PROVENANCE_STATUS", "nogit")
    monkeypatch.setenv("MYXCEL_GIT_SHA", "")  # empty → None
    monkeypatch.setenv("MYXCEL_GIT_BRANCH", "")
    monkeypatch.setenv("MYXCEL_GIT_DIRTY", "")
    monkeypatch.setenv("MYXCEL_PROVENANCE_ROOT", str(tmp_path))

    state = capture_git_state(tmp_path)
    assert state.hash == "nogit"
    assert state.provenance_source == "myxcel-env"


def test_status_unavailable_maps_to_hash_unknown(tmp_path: Path, monkeypatch):
    """D4: status='unavailable' maps to hash='unknown'."""
    monkeypatch.setenv("MYXCEL_PROVENANCE_SCHEMA", "1")
    monkeypatch.setenv("MYXCEL_PROVENANCE_STATUS", "unavailable")
    monkeypatch.setenv("MYXCEL_GIT_SHA", "")
    monkeypatch.setenv("MYXCEL_GIT_BRANCH", "")
    monkeypatch.setenv("MYXCEL_GIT_DIRTY", "")
    monkeypatch.setenv("MYXCEL_PROVENANCE_ROOT", str(tmp_path))

    state = capture_git_state(tmp_path)
    assert state.hash == "unknown"
    assert state.provenance_source == "myxcel-env"


def test_malformed_sidecar_falls_through(tmp_path: Path):
    """Malformed sidecar (bad JSON, missing status, non-int schema) falls through."""
    # Bad JSON
    sidecar_file = tmp_path / ".myxcel_provenance.json"
    sidecar_file.write_text("{invalid json")

    state = capture_git_state(tmp_path)
    assert state.hash == "unknown"
    assert state.provenance_source == "none"

    # Missing provenance_status
    sidecar_file.write_text(json.dumps({"schema_version": 1}))
    state = capture_git_state(tmp_path)
    assert state.hash == "unknown"

    # Non-int schema_version
    sidecar_file.write_text(
        json.dumps({"schema_version": "1", "provenance_status": "git"})
    )
    state = capture_git_state(tmp_path)
    assert state.hash == "unknown"

    # Present but unrecognised provenance_status (D4's enum is exactly
    # "git" | "nogit" | "unavailable" -- anything else must be rejected, not passed
    # through with whatever git_sha it happened to carry).
    sidecar_file.write_text(
        json.dumps({"schema_version": 1, "provenance_status": "totally-bogus-status", "git_sha": "9" * 40})
    )
    state = capture_git_state(tmp_path)
    assert state.hash == "unknown"
    assert state.provenance_source == "none"


def test_future_schema_version_is_accepted_with_warning(tmp_path: Path):
    """Forward compatibility: future schema_version accepted, AND warns once (D2's
    "accepts the record... and warns once" -- the acceptance half alone isn't the full
    contract)."""
    sidecar_data = {
        "schema_version": 99,  # Future version
        "provenance_status": "git",
        "git_sha": "3" * 40,
        "git_branch": "test",
        "git_dirty": False,
        "dirty_content_id": None,
        "capture_stage": "push",
        "sync_state": "verified",
        "computed_at": "2026-08-20T14:00:00Z",
        "provenance_root": str(tmp_path),
        "remote": "test",
        "project": "testproj",
        "worktree": None,
        "myxcel_version": "0.1.0",
    }
    sidecar_file = tmp_path / ".myxcel_provenance.json"
    sidecar_file.write_text(json.dumps(sidecar_data))

    with pytest.warns(UserWarning, match="schema_version"):
        state = capture_git_state(tmp_path)
    assert state.hash == "3" * 40
    assert state.provenance_source == "myxcel-sidecar"


def test_dirty_content_id_reaches_gitstate(tmp_path: Path, monkeypatch):
    """dirty_content_id from channel reaches GitState."""
    content_id = "tree:abcd1234abcd1234abcd1234abcd1234abcd1234"
    monkeypatch.setenv("MYXCEL_PROVENANCE_SCHEMA", "1")
    monkeypatch.setenv("MYXCEL_PROVENANCE_STATUS", "git")
    monkeypatch.setenv("MYXCEL_GIT_SHA", "4" * 40)
    monkeypatch.setenv("MYXCEL_GIT_BRANCH", "test")
    monkeypatch.setenv("MYXCEL_GIT_DIRTY", "1")
    monkeypatch.setenv("MYXCEL_GIT_DIRTY_CONTENT_ID", content_id)
    monkeypatch.setenv("MYXCEL_PROVENANCE_ROOT", str(tmp_path))

    state = capture_git_state(tmp_path)
    assert state.dirty_content_id == content_id


def test_capture_git_state_never_raises_with_nonexistent_prov_root(tmp_path: Path, monkeypatch):
    """C6: capture_git_state never raises even when prov.root doesn't exist locally.

    The env channel is rejected by the D5 guard (cwd not inside root), but we don't raise.
    Instead we fall through to legacy behavior (no repo → unknown).
    """
    nonexistent = "/nonexistent/path/that/does/not/exist"
    monkeypatch.setenv("MYXCEL_PROVENANCE_SCHEMA", "1")
    monkeypatch.setenv("MYXCEL_PROVENANCE_STATUS", "git")
    monkeypatch.setenv("MYXCEL_GIT_SHA", "5" * 40)
    monkeypatch.setenv("MYXCEL_GIT_BRANCH", "test")
    monkeypatch.setenv("MYXCEL_GIT_DIRTY", "0")
    monkeypatch.setenv("MYXCEL_PROVENANCE_ROOT", nonexistent)

    # Should not raise. D5 guard rejects env channel (cwd not inside root),
    # so we fall through to legacy behavior → unknown
    state = capture_git_state(tmp_path)
    assert state.hash == "unknown"
    assert state.provenance_source == "none"


def test_env_channel_rejected_by_d5_falls_through_to_sidecar_with_correct_source(
    tmp_path: Path, monkeypatch
):
    """provenance_source must reflect which channel actually supplied the data, not just
    ambient env-var presence. Sets MYXCEL_PROVENANCE_SCHEMA (so it's present in os.environ)
    but points MYXCEL_PROVENANCE_ROOT at a DIFFERENT directory than cwd, so D5 rejects the
    env channel -- while a valid sidecar sits at cwd. The result must come from the sidecar
    AND be labeled "myxcel-sidecar", not mislabeled "myxcel-env" from the stale environ
    presence check."""
    other_dir = tmp_path / "other"
    other_dir.mkdir()

    env_sha = "1" * 40
    monkeypatch.setenv("MYXCEL_PROVENANCE_SCHEMA", "1")
    monkeypatch.setenv("MYXCEL_PROVENANCE_STATUS", "git")
    monkeypatch.setenv("MYXCEL_GIT_SHA", env_sha)
    monkeypatch.setenv("MYXCEL_GIT_BRANCH", "env-branch")
    monkeypatch.setenv("MYXCEL_GIT_DIRTY", "0")
    # Points at a directory that is NOT an ancestor of tmp_path -> D5 rejects.
    monkeypatch.setenv("MYXCEL_PROVENANCE_ROOT", str(other_dir))

    sidecar_sha = "2" * 40
    sidecar_data = {
        "schema_version": 1,
        "provenance_status": "git",
        "git_sha": sidecar_sha,
        "git_branch": "sidecar-branch",
        "git_dirty": False,
        "dirty_content_id": None,
        "capture_stage": "push",
        "sync_state": "verified",
        "computed_at": "2026-08-20T14:00:00Z",
        "provenance_root": str(tmp_path),
        "remote": "test",
        "project": "testproj",
        "worktree": None,
        "myxcel_version": "0.1.0",
    }
    (tmp_path / ".myxcel_provenance.json").write_text(json.dumps(sidecar_data))

    state = capture_git_state(tmp_path)
    assert state.hash == sidecar_sha
    assert state.branch == "sidecar-branch"
    assert state.provenance_source == "myxcel-sidecar"


def test_env_channel_rejects_when_provenance_root_is_empty(tmp_path: Path, monkeypatch):
    """D5 must fail CLOSED (reject the env channel) when MYXCEL_PROVENANCE_ROOT is
    missing/empty -- an unresolvable root can't be verified as covering cwd, so the safe
    direction is to discard the channel, not accept it unconditionally."""
    monkeypatch.setenv("MYXCEL_PROVENANCE_SCHEMA", "1")
    monkeypatch.setenv("MYXCEL_PROVENANCE_STATUS", "git")
    monkeypatch.setenv("MYXCEL_GIT_SHA", "3" * 40)
    monkeypatch.setenv("MYXCEL_GIT_BRANCH", "test")
    monkeypatch.setenv("MYXCEL_GIT_DIRTY", "0")
    monkeypatch.delenv("MYXCEL_PROVENANCE_ROOT", raising=False)

    state = capture_git_state(tmp_path)
    # No env channel (rejected), no sidecar, no real repo -> legacy shellout -> unknown.
    assert state.hash == "unknown"
    assert state.provenance_source == "none"


def test_real_repo_wins_does_not_borrow_stale_dirty_content_id(tmp_path: Path, monkeypatch):
    """D6/D3: when a real repo at the provenance root wins, dirty_content_id must NOT be
    borrowed from the channel -- the channel's dirty_content_id may describe an earlier
    (possibly drifted) tree state than the one the fresh git shellout just observed, and
    bathos has no local equivalent of myxcel's tree-OID algorithm to compute a fresh one."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True,
    )
    (tmp_path / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    monkeypatch.setenv("MYXCEL_PROVENANCE_SCHEMA", "1")
    monkeypatch.setenv("MYXCEL_PROVENANCE_STATUS", "git")
    monkeypatch.setenv("MYXCEL_GIT_SHA", "4" * 40)
    monkeypatch.setenv("MYXCEL_GIT_BRANCH", "fake-branch")
    monkeypatch.setenv("MYXCEL_GIT_DIRTY", "1")
    monkeypatch.setenv("MYXCEL_GIT_DIRTY_CONTENT_ID", "tree:" + "a" * 40)
    monkeypatch.setenv("MYXCEL_PROVENANCE_ROOT", str(tmp_path))

    state = capture_git_state(tmp_path)
    assert state.provenance_source == "git"
    assert state.dirty_content_id is None


def test_same_root_falls_back_to_realpath_when_samefile_raises(tmp_path: Path):
    """D6: _same_root must fall back to a realpath comparison when `samefile` cannot be
    computed at all (raises, rather than returning False) -- the case a stat-identity check
    genuinely cannot answer, e.g. one side of the comparison doesn't exist on disk (a path
    computed from a symlink target or a bind-mount alias before the mount is live). This
    exercises the actual fallback trigger: `Path.samefile()` raises FileNotFoundError/OSError
    when either path is missing, at which point only the second (`os.path.realpath`) try
    block runs -- realpath resolves purely lexically and does not require either path to
    exist, so two syntactically-different-but-equivalent paths (one with a `..` segment)
    still compare equal via the fallback even though `samefile` could never have answered."""
    from bathos.git import _same_root

    real_dir = tmp_path / "real"
    real_dir.mkdir()

    # The intermediate component "nonexistent_dir" does not exist, so the OS cannot
    # traverse the literal path -> Path(missing).samefile(real_dir) raises
    # FileNotFoundError, forcing the realpath fallback. os.path.realpath resolves the
    # ".." segment lexically without requiring the path to exist, landing on real_dir.
    missing_but_equivalent = tmp_path / "nonexistent_dir" / ".." / "real"
    assert not missing_but_equivalent.exists()  # confirms samefile() would raise here
    assert _same_root(str(missing_but_equivalent), str(real_dir)) is True

    missing_and_different = tmp_path / "nonexistent_dir" / ".." / "nonexistent_target"
    assert _same_root(str(missing_and_different), str(real_dir)) is False


def test_same_root_treats_oserror_as_not_same(tmp_path: Path, monkeypatch):
    """D6: samefile raising OSError is treated as 'not same', no exception escapes."""
    subdir = tmp_path / "subdir"
    subdir.mkdir()

    # Set env channel with root pointing to subdir (so D5 guard passes)
    # but no real repo at subdir
    monkeypatch.setenv("MYXCEL_PROVENANCE_SCHEMA", "1")
    monkeypatch.setenv("MYXCEL_PROVENANCE_STATUS", "git")
    monkeypatch.setenv("MYXCEL_GIT_SHA", "6" * 40)
    monkeypatch.setenv("MYXCEL_GIT_BRANCH", "test")
    monkeypatch.setenv("MYXCEL_GIT_DIRTY", "0")
    monkeypatch.setenv("MYXCEL_PROVENANCE_ROOT", str(subdir))

    # Should not raise and should use channel
    state = capture_git_state(subdir)
    assert state.hash == "6" * 40
    assert state.provenance_source == "myxcel-env"


def test_empty_string_env_vars_become_none(tmp_path: Path, monkeypatch):
    """Empty string in env vars is treated as None/null."""
    monkeypatch.setenv("MYXCEL_PROVENANCE_SCHEMA", "1")
    monkeypatch.setenv("MYXCEL_PROVENANCE_STATUS", "git")
    monkeypatch.setenv("MYXCEL_GIT_SHA", "7" * 40)
    monkeypatch.setenv("MYXCEL_GIT_BRANCH", "")  # empty
    monkeypatch.setenv("MYXCEL_GIT_DIRTY", "")   # empty
    monkeypatch.setenv("MYXCEL_GIT_DIRTY_CONTENT_ID", "")  # empty
    monkeypatch.setenv("MYXCEL_PROVENANCE_ROOT", str(tmp_path))

    state = capture_git_state(tmp_path)
    assert state.hash == "7" * 40
    assert state.branch == "unknown"  # empty string → None → "unknown"
    assert state.dirty is False  # empty string → False
    assert state.dirty_content_id is None


def test_sidecar_with_nonzero_git_dirty(tmp_path: Path):
    """git_dirty=true in sidecar maps to dirty=True in GitState."""
    sidecar_data = {
        "schema_version": 1,
        "provenance_status": "git",
        "git_sha": "8" * 40,
        "git_branch": "test",
        "git_dirty": True,
        "dirty_content_id": "tree:abcd1234abcd1234abcd1234abcd1234abcd1234",
        "capture_stage": "push",
        "sync_state": "verified",
        "computed_at": "2026-08-20T14:00:00Z",
        "provenance_root": str(tmp_path),
        "remote": "test",
        "project": "testproj",
        "worktree": None,
        "myxcel_version": "0.1.0",
    }
    sidecar_file = tmp_path / ".myxcel_provenance.json"
    sidecar_file.write_text(json.dumps(sidecar_data))

    state = capture_git_state(tmp_path)
    assert state.dirty is True
    assert state.dirty_content_id == "tree:abcd1234abcd1234abcd1234abcd1234abcd1234"
