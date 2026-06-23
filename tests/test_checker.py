import subprocess
from pathlib import Path

import pytest

from bathos.catalog import init_catalog, write_run
from bathos.checker import check_runs
from bathos.schema import Run


@pytest.fixture
def git_repo(tmp_path: Path):
    """Set up a git repo for testing."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "file.txt").write_text("initial")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return tmp_path


def test_check_flags_stale_run(tmp_catalog: Path, git_repo: Path, monkeypatch):
    """Run with hash != HEAD and dirty=False should be flagged STALE."""
    monkeypatch.chdir(git_repo)
    init_catalog(tmp_catalog)

    # Create a run with an old git hash
    stale_run = Run(
        project_slug="testproj",
        command="python test.py",
        argv=["python", "test.py"],
        git_hash="deadbeef0000",
        git_branch="main",
        git_dirty=False,
        status="completed",
    )
    write_run(stale_run, tmp_catalog)

    # Check the runs
    results = check_runs(tmp_catalog, git_repo)

    assert len(results) == 1
    assert results[0].status == "STALE"
    assert results[0].run_id == stale_run.id
    assert results[0].run_git_hash == "deadbeef0000"


def test_check_ok_for_current_run(tmp_catalog: Path, git_repo: Path, monkeypatch):
    """Run with hash == HEAD should be flagged OK."""
    monkeypatch.chdir(git_repo)
    init_catalog(tmp_catalog)

    # Get current HEAD
    current_hash = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, text=True
    ).strip()

    # Create a run with current git hash
    current_run = Run(
        project_slug="testproj",
        command="python test.py",
        argv=["python", "test.py"],
        git_hash=current_hash,
        git_branch="main",
        git_dirty=False,
        status="completed",
    )
    write_run(current_run, tmp_catalog)

    # Check the runs
    results = check_runs(tmp_catalog, git_repo)

    assert len(results) == 1
    assert results[0].status == "OK"
    assert results[0].run_id == current_run.id


def test_check_flags_dirty_run(tmp_catalog: Path, git_repo: Path, monkeypatch):
    """Run with git_dirty=True should be flagged DIRTY_RUN."""
    monkeypatch.chdir(git_repo)
    init_catalog(tmp_catalog)

    # Create a run with dirty=True
    dirty_run = Run(
        project_slug="testproj",
        command="python test.py",
        argv=["python", "test.py"],
        git_hash="anyhashatall",
        git_branch="main",
        git_dirty=True,
        status="completed",
    )
    write_run(dirty_run, tmp_catalog)

    # Check the runs
    results = check_runs(tmp_catalog, git_repo)

    assert len(results) == 1
    assert results[0].status == "DIRTY_RUN"
    assert results[0].run_id == dirty_run.id


def test_check_unknown_outside_git(tmp_catalog: Path, monkeypatch):
    """Run with git_hash='unknown' should be flagged UNKNOWN_CODE."""
    # Don't set up a git repo, so capture_git_state returns unknown
    tmp_path = tmp_catalog.parent.parent  # get a temp directory
    monkeypatch.chdir(tmp_path)
    init_catalog(tmp_catalog)

    # Create a run with unknown hash
    unknown_run = Run(
        project_slug="testproj",
        command="python test.py",
        argv=["python", "test.py"],
        git_hash="unknown",
        git_branch="unknown",
        git_dirty=False,
        status="completed",
    )
    write_run(unknown_run, tmp_catalog)

    # Check the runs
    results = check_runs(tmp_catalog, tmp_path)

    assert len(results) == 1
    assert results[0].status == "UNKNOWN_CODE"
    assert results[0].run_id == unknown_run.id


def test_check_uses_cool_backend(tmp_catalog: Path, git_repo: Path, monkeypatch):
    """check_runs should work with cool tier (no warm DB)."""
    monkeypatch.chdir(git_repo)
    init_catalog(tmp_catalog)

    current_hash = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, text=True
    ).strip()

    run1 = Run(
        project_slug="testproj",
        command="python test1.py",
        argv=["python", "test1.py"],
        git_hash=current_hash,
        git_branch="main",
        git_dirty=False,
    )
    run2 = Run(
        project_slug="testproj",
        command="python test2.py",
        argv=["python", "test2.py"],
        git_hash="olddeadbeef",
        git_branch="main",
        git_dirty=False,
    )
    write_run(run1, tmp_catalog)
    write_run(run2, tmp_catalog)

    results = check_runs(tmp_catalog, git_repo)

    assert len(results) == 2
    assert sum(1 for r in results if r.status == "OK") == 1
    assert sum(1 for r in results if r.status == "STALE") == 1


def test_check_status_filter(tmp_catalog: Path, git_repo: Path, monkeypatch):
    """check_runs should support filtering by status."""
    monkeypatch.chdir(git_repo)
    init_catalog(tmp_catalog)

    # Create multiple runs with different statuses
    stale_run = Run(
        project_slug="testproj",
        command="python test1.py",
        argv=["python", "test1.py"],
        git_hash="deadbeef0000",
        git_branch="main",
        git_dirty=False,
    )
    dirty_run = Run(
        project_slug="testproj",
        command="python test2.py",
        argv=["python", "test2.py"],
        git_hash="anyhashatall",
        git_branch="main",
        git_dirty=True,
    )
    write_run(stale_run, tmp_catalog)
    write_run(dirty_run, tmp_catalog)

    # Filter by STALE
    results = check_runs(tmp_catalog, git_repo, status_filter="STALE")
    assert len(results) == 1
    assert results[0].status == "STALE"

    # Filter by DIRTY_RUN
    results = check_runs(tmp_catalog, git_repo, status_filter="DIRTY_RUN")
    assert len(results) == 1
    assert results[0].status == "DIRTY_RUN"


def test_check_result_has_required_fields(tmp_catalog: Path, git_repo: Path, monkeypatch):
    """CheckResult should have all required fields."""
    monkeypatch.chdir(git_repo)
    init_catalog(tmp_catalog)

    current_hash = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, text=True
    ).strip()

    run = Run(
        project_slug="testproj",
        command="python test.py",
        argv=["python", "test.py"],
        git_hash=current_hash,
        git_branch="main",
        git_dirty=False,
    )
    write_run(run, tmp_catalog)

    results = check_runs(tmp_catalog, git_repo)

    assert len(results) == 1
    result = results[0]
    assert hasattr(result, "run_id")
    assert hasattr(result, "status")
    assert hasattr(result, "run_git_hash")
    assert hasattr(result, "current_hash")
    assert result.current_hash == current_hash


def test_check_output_files_present(tmp_path: Path):
    """Verify check_output_files detects present files."""
    from bathos.checker import check_output_files

    output_file = tmp_path / "result.json"
    output_file.write_text('{"success": true}')

    run = Run(
        project_slug="test",
        command="test",
        argv=["test"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        output_paths=[str(output_file)],
    )

    results = check_output_files(run)

    assert len(results) == 1
    assert results[0].path == str(output_file)
    assert results[0].status == "present"
    assert results[0].size_bytes > 0


def test_check_output_files_missing():
    """Verify check_output_files detects missing files."""
    from bathos.checker import check_output_files

    run = Run(
        project_slug="test",
        command="test",
        argv=["test"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        output_paths=["/nonexistent/result.json"],
    )

    results = check_output_files(run)

    assert len(results) == 1
    assert results[0].status == "missing"
    assert results[0].size_bytes == 0


def test_check_output_files_multiple(tmp_path: Path):
    """Verify checking multiple output files."""
    from bathos.checker import check_output_files

    file1 = tmp_path / "results.json"
    file2 = tmp_path / "metrics.csv"
    file3 = "/nonexistent/missing.txt"

    file1.write_text("{}")
    file2.write_text("a,b,c\n1,2,3")

    run = Run(
        project_slug="test",
        command="test",
        argv=["test"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        output_paths=[str(file1), str(file2), file3],
    )

    results = check_output_files(run)

    assert len(results) == 3
    assert results[0].status == "present"
    assert results[1].status == "present"
    assert results[2].status == "missing"


def test_check_output_sha_drift_ok(tmp_path: Path):
    """Verify check_output_sha_drift reports OK when hashes match."""
    import json

    from bathos.checker import check_output_sha_drift
    from bathos.compact import _collect_output_metadata

    output_file = tmp_path / "parity_verdict.md"
    output_file.write_text("# PARITY")

    meta = _collect_output_metadata(str(output_file))
    output_metadata = json.dumps([{"path": str(output_file), **meta}])

    run = Run(
        project_slug="test",
        command="test",
        argv=["test"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        output_metadata=output_metadata,
    )

    results = check_output_sha_drift(run)

    assert len(results) == 1
    assert results[0].status == "OK"
    assert results[0].recorded_sha256 == results[0].current_sha256


def test_check_output_sha_drift_detects_mutation(tmp_path: Path):
    """Verify check_output_sha_drift reports DRIFT after file mutation."""
    import json

    from bathos.checker import check_output_sha_drift, output_metadata_has_sha_drift
    from bathos.compact import _collect_output_metadata

    output_file = tmp_path / "parity_verdict.md"
    output_file.write_text("# PARITY")

    meta = _collect_output_metadata(str(output_file))
    output_metadata = json.dumps([{"path": str(output_file), **meta}])

    output_file.write_text("# MODIFIED")

    run = Run(
        project_slug="test",
        command="test",
        argv=["test"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        output_metadata=output_metadata,
    )

    results = check_output_sha_drift(run)

    assert len(results) == 1
    assert results[0].status == "DRIFT"
    assert output_metadata_has_sha_drift(output_metadata)
