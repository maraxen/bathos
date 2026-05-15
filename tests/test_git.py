import subprocess
from pathlib import Path
import pytest
from bathos.git import capture_git_state, GitState


def test_captures_state_in_git_repo(tmp_path: Path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path,
                   check=True, capture_output=True)
    state = capture_git_state(tmp_path)
    assert len(state.hash) == 40
    assert state.branch in ("main", "master")
    assert state.dirty is False


def test_detects_dirty_state(tmp_path: Path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path,
                   check=True, capture_output=True)
    (tmp_path / "file.txt").write_text("modified")
    state = capture_git_state(tmp_path)
    assert state.dirty is True


def test_returns_sentinel_outside_git_repo(tmp_path: Path):
    state = capture_git_state(tmp_path)
    assert state.hash == "unknown"
    assert state.branch == "unknown"
    assert state.dirty is False
