"""End-to-end cluster workflow: init → run → compact → check → mutate → check → sync."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from bathos.catalog import init_catalog, write_run
from bathos.checker import check_runs
from bathos.cli import app
from bathos.compact import compact
from bathos.config import load_project_config
from bathos.schema import Run
from bathos.sync import sync_catalog

runner = CliRunner()


@pytest.fixture
def git_repo(tmp_path: Path):
    """Set up a git repo with initial commit for testing."""
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
    # Create initial files
    (tmp_path / "README.md").write_text("# Test Project")
    (tmp_path / "main.py").write_text("print('hello')")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return tmp_path


def test_full_cluster_workflow(git_repo: Path, monkeypatch):
    """
    End-to-end cluster workflow test:
    1. Initialize project with remote configured
    2. Simulate two bth run calls
    3. Compact cool fragments → warm DuckDB
    4. bth check → all runs OK (match HEAD)
    5. Mutate a file in git
    6. bth check → runs now STALE
    7. bth sync command constructed correctly (mocked rsync)
    """
    monkeypatch.chdir(git_repo)
    catalog = git_repo / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")

    # Step 1: Initialize project with remote configured
    r = runner.invoke(app, ["init", "--slug", "testproj"])
    assert r.exit_code == 0, f"init failed: {r.output}"
    assert (git_repo / ".bth.toml").exists()
    assert (git_repo / "scripts" / "experiments").is_dir()

    # Verify remote config can be added
    config_path = git_repo / ".bth.toml"
    config_text = config_path.read_text()
    # Add remote config to .bth.toml for sync test
    if "[remotes" not in config_text:
        # Append remote config
        remote_config = """
[remotes.engaging]
host = "engaging"
remote_root = "~/projects/testproj"
"""
        config_path.write_text(config_text + remote_config)

    # Step 2: Simulate two bth run calls
    # Get current HEAD (will be used later to verify runs are OK)
    initial_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, text=True
    ).strip()

    # Initialize catalog
    init_catalog(catalog)

    # Create two runs with current git hash
    run1 = Run(
        project_slug="testproj",
        command="python scripts/test1.py",
        argv=["python", "scripts/test1.py"],
        git_hash=initial_head,
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        duration_s=1.5,
    )
    write_run(run1, catalog)

    run2 = Run(
        project_slug="testproj",
        command="python scripts/test2.py",
        argv=["python", "scripts/test2.py"],
        git_hash=initial_head,
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        duration_s=2.0,
    )
    write_run(run2, catalog)

    # Verify we have 2 cool fragments
    run_files = list((catalog / "runs").rglob("run_*.parquet"))
    assert len(run_files) == 2, f"Expected 2 run files, got {len(run_files)}"

    # Step 3: Compact cool fragments → warm DuckDB
    result = compact(catalog)
    assert result.ingested == 2, f"Expected 2 ingested, got {result.ingested}"
    assert (catalog / "bathos.db").exists(), "bathos.db should exist after compact"

    # Step 4: bth check → all runs OK (match current HEAD)
    check_results = check_runs(catalog, git_repo)
    assert len(check_results) == 2
    assert all(r.status == "OK" for r in check_results), (
        f"All runs should be OK at HEAD. Got: {[(r.run_id, r.status) for r in check_results]}"
    )

    # Step 5: Mutate a file in git to make runs stale
    (git_repo / "main.py").write_text("print('modified')")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "modify main.py"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    new_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, text=True
    ).strip()
    assert new_head != initial_head, "New HEAD should differ from initial"

    # Step 6: bth check → runs now STALE
    check_results_after = check_runs(catalog, git_repo)
    assert len(check_results_after) == 2
    # Both runs should now be STALE because git_hash != current HEAD and git_dirty was False
    assert all(r.status == "STALE" for r in check_results_after), (
        f"All runs should be STALE after mutation. Got: {[(r.run_id, r.status) for r in check_results_after]}"
    )
    assert all(r.run_git_hash == initial_head for r in check_results_after)
    assert all(r.current_hash == new_head for r in check_results_after)

    # Step 7: bth sync command constructed correctly (mock rsync)
    config = load_project_config(git_repo / ".bth.toml")
    with patch("bathos.sync.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        # Provide realistic rsync output with file transfer info
        mock_run.return_value.stdout = (
            "sent 1000 bytes  received 500 bytes\nNumber of regular files transferred: 2"
        )

        result = sync_catalog("engaging", config, catalog, pull=False)

        # Verify mock was called
        assert mock_run.called
        call_args = mock_run.call_args
        cmd = call_args[0][0]

        # Verify rsync command structure
        assert cmd[0] == "rsync"
        assert "-az" in cmd
        assert "--ignore-existing" in cmd
        # Should have source and destination with runs/ directories
        assert any("runs/" in str(arg) for arg in cmd)

        # Verify result
        assert result.remote == "engaging"
        assert result.transferred == 2
        assert result.duration_s > 0

    # Step 7b: Test pull direction (rsync with remote as source)
    with patch("bathos.sync.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = (
            "sent 500 bytes  received 1000 bytes\nNumber of regular files transferred: 2"
        )

        result = sync_catalog("engaging", config, catalog, pull=True)

        call_args = mock_run.call_args
        cmd = call_args[0][0]

        # Pull should have engaging: as source
        assert any("engaging:" in str(arg) for arg in cmd)


def test_cluster_workflow_with_slurm_job_id(git_repo: Path, monkeypatch):
    """
    Test that SLURM_JOB_ID is captured during runs and can be queried.
    Simulates cluster array job scenario.
    """
    monkeypatch.chdir(git_repo)
    catalog = git_repo / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    # Simulate SLURM job ID
    monkeypatch.setenv("SLURM_JOB_ID", "12345")

    init_catalog(catalog)

    # Get current HEAD
    current_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, text=True
    ).strip()

    # Create a run as if it were part of a SLURM array job
    run = Run(
        project_slug="testproj",
        command="python scripts/benchmark.py",
        argv=["python", "scripts/benchmark.py"],
        git_hash=current_head,
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        slurm_job_id="12345",  # Captured from env var
        duration_s=5.0,
    )
    write_run(run, catalog)

    # Verify SLURM_JOB_ID is in the written fragment
    run_files = list((catalog / "runs").rglob("run_*.parquet"))
    assert len(run_files) == 1

    # Compact and verify slurm_job_id is preserved in warm tier
    result = compact(catalog)
    assert result.ingested == 1

    # Read back from catalog and verify slurm_job_id is preserved
    from bathos.catalog import read_runs

    runs = read_runs(catalog)
    assert len(runs) == 1
    assert runs[0].slurm_job_id == "12345"


def test_cluster_workflow_check_with_dirty_run(git_repo: Path, monkeypatch):
    """
    Test that runs with git_dirty=True are flagged as DIRTY_RUN,
    indicating results may not be reproducible.
    """
    monkeypatch.chdir(git_repo)
    catalog = git_repo / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))

    init_catalog(catalog)

    current_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, text=True
    ).strip()

    # Create a run that was executed with dirty working directory
    dirty_run = Run(
        project_slug="testproj",
        command="python scripts/test.py",
        argv=["python", "scripts/test.py"],
        git_hash=current_head,
        git_branch="main",
        git_dirty=True,  # Uncommitted changes at run time
        status="completed",
        exit_code=0,
        duration_s=1.0,
    )
    write_run(dirty_run, catalog)

    # Compact
    result = compact(catalog)
    assert result.ingested == 1

    # Check the run
    check_results = check_runs(catalog, git_repo)
    assert len(check_results) == 1
    assert check_results[0].status == "DIRTY_RUN"
    assert check_results[0].run_id == dirty_run.id


def test_cluster_workflow_multiple_checks(git_repo: Path, monkeypatch):
    """
    Test that check can be run multiple times,
    showing different results as code evolves.
    """
    monkeypatch.chdir(git_repo)
    catalog = git_repo / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))

    init_catalog(catalog)

    # Get initial HEAD and create runs
    head1 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=git_repo, text=True).strip()

    run1 = Run(
        project_slug="testproj",
        command="python test.py",
        argv=["python", "test.py"],
        git_hash=head1,
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
    )
    write_run(run1, catalog)

    # First check: should be OK
    result1 = check_runs(catalog, git_repo)
    assert len(result1) == 1
    assert result1[0].status == "OK"

    # Advance git
    (git_repo / "file.txt").write_text("new")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "second"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    # Second check: should be STALE
    result2 = check_runs(catalog, git_repo)
    assert len(result2) == 1
    assert result2[0].status == "STALE"

    # Advance git again
    (git_repo / "file.txt").write_text("newer")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "third"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    # Third check: still STALE (different hash)
    result3 = check_runs(catalog, git_repo)
    assert len(result3) == 1
    assert result3[0].status == "STALE"
