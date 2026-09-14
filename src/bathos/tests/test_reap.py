"""Tests for bth catalog reap command."""

import json
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import pyarrow.parquet as pq

from bathos.catalog import read_runs, write_run
from bathos.schema import Run


@pytest.fixture
def temp_catalog(tmp_path):
    """Create a temporary catalog directory."""
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "runs").mkdir()
    return catalog_dir


def create_run(
    run_id: str,
    project_slug: str = "test",
    status: str = "running",
    age_hours: float = 0,
    slurm_job_id: str = "",
    hostname: str = "localhost",
) -> Run:
    """Create a Run object with optional age."""
    run = Run(
        id=run_id,
        project_slug=project_slug,
        command="test cmd",
        argv=["test"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        status=status,
        slurm_job_id=slurm_job_id,
        hostname=hostname,
    )
    # Adjust timestamp to be N hours old
    run.timestamp = datetime.now(UTC) - timedelta(hours=age_hours)
    return run


def test_reap_window_25h_old_running_reaped(temp_catalog):
    """Test (i) - a 25h-old running run is reaped, 23h-old is not."""
    from bathos.reap import reap_runs

    run_25h = create_run("run_25h", age_hours=25, status="running")
    run_23h = create_run("run_23h", age_hours=23, status="running")
    run_30h_completed = create_run("run_30h_done", age_hours=30, status="completed")

    write_run(run_25h, temp_catalog)
    write_run(run_23h, temp_catalog)
    write_run(run_30h_completed, temp_catalog)

    candidates, skipped = reap_runs(
        temp_catalog, older_than_h=24, dry_run=True, apply=False
    )
    assert len(candidates) == 1
    assert candidates[0].id == "run_25h"
    assert len(skipped) == 2  # 23h and 30h_completed


def test_reap_non_running_not_reaped(temp_catalog):
    """Test that non-running status is not reaped."""
    from bathos.reap import reap_runs

    run_old_completed = create_run(
        "run_old_done", age_hours=30, status="completed"
    )
    run_old_failed = create_run("run_old_fail", age_hours=30, status="failed")

    write_run(run_old_completed, temp_catalog)
    write_run(run_old_failed, temp_catalog)

    candidates, skipped = reap_runs(
        temp_catalog, older_than_h=24, dry_run=True, apply=False
    )
    assert len(candidates) == 0
    assert len(skipped) == 2


def test_reap_preserves_all_fields_except_status_metadata(temp_catalog):
    """Test (ii) - all non-status/non-metadata fields preserved after apply."""
    from bathos.reap import reap_runs

    original = create_run("run_test", age_hours=25, status="running")
    original.git_hash = "deadbeef"
    original.outcome = "test_outcome"
    original.tags = ["tag1", "tag2"]

    write_run(original, temp_catalog)

    candidates, _ = reap_runs(
        temp_catalog, older_than_h=24, dry_run=False, apply=True
    )
    assert len(candidates) == 1

    # Read back the reaped run
    reaped = read_runs(temp_catalog)[0]
    assert reaped.status == "abandoned"
    assert reaped.git_hash == "deadbeef"
    assert reaped.outcome == "test_outcome"
    assert reaped.tags == ["tag1", "tag2"]
    # Verify metadata.reaped is persisted
    metadata = json.loads(reaped.metadata)
    assert "reaped" in metadata
    assert metadata["reaped"]["reason"] == "orphan_window_exceeded"
    assert metadata["reaped"]["prior_status"] == "running"
    assert "reaped_at" in metadata["reaped"]
    assert metadata["reaped"]["window_h"] == 24


def test_reap_idempotent(temp_catalog):
    """Test (iii) - running apply twice reaps 0 the second time."""
    from bathos.reap import reap_runs

    run = create_run("run_test", age_hours=25, status="running")
    write_run(run, temp_catalog)

    candidates1, _ = reap_runs(
        temp_catalog, older_than_h=24, dry_run=False, apply=True
    )
    assert len(candidates1) == 1

    candidates2, _ = reap_runs(
        temp_catalog, older_than_h=24, dry_run=False, apply=True
    )
    assert len(candidates2) == 0


def test_reap_dry_run_writes_nothing(temp_catalog):
    """Test (iv) - --dry-run writes nothing."""
    from bathos.reap import reap_runs

    run = create_run("run_test", age_hours=25, status="running")
    write_run(run, temp_catalog)

    fragment_path = temp_catalog / "runs" / "test" / "run_run_test.parquet"
    orig_mtime = fragment_path.stat().st_mtime

    candidates, _ = reap_runs(
        temp_catalog, older_than_h=24, dry_run=True, apply=False
    )
    assert len(candidates) == 1

    # Verify fragment not modified
    assert fragment_path.stat().st_mtime == orig_mtime

    # Verify status still "running"
    runs = read_runs(temp_catalog)
    assert runs[0].status == "running"


def test_reap_enforces_floor(temp_catalog):
    """Test (v) - apply with older_than_h < 24 exits non-zero."""
    from bathos.reap import ReapError, reap_runs

    run = create_run("run_test", age_hours=25, status="running")
    write_run(run, temp_catalog)

    with pytest.raises(ReapError, match="Floor.*24"):
        reap_runs(temp_catalog, older_than_h=23, dry_run=False, apply=True)


def test_reap_sacct_terminal_states(temp_catalog, monkeypatch):
    """Test (vi) - sacct checks terminal states correctly."""
    from bathos.reap import reap_runs

    run_running = create_run("run_running", age_hours=25, status="running", slurm_job_id="1001")
    run_completed = create_run("run_completed", age_hours=25, status="running", slurm_job_id="1002")
    run_failed = create_run("run_failed", age_hours=25, status="running", slurm_job_id="1003")

    write_run(run_running, temp_catalog)
    write_run(run_completed, temp_catalog)
    write_run(run_failed, temp_catalog)

    # Mock sacct to return different states
    def mock_run(cmd, *args, **kwargs):
        if "-X" not in cmd:
            # Verify -X is present
            raise AssertionError("sacct missing -X flag")

        if "1001" in cmd:
            # RUNNING job - should skip
            return subprocess.CompletedProcess(cmd, 0, "RUNNING\n", "")
        elif "1002" in cmd:
            # COMPLETED job - should reap
            return subprocess.CompletedProcess(cmd, 0, "COMPLETED\n", "")
        elif "1003" in cmd:
            # FAILED job - should reap
            return subprocess.CompletedProcess(cmd, 0, "FAILED\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr("subprocess.run", mock_run)

    candidates, skipped = reap_runs(
        temp_catalog, older_than_h=24, dry_run=True, apply=False
    )
    # 1001 should be skipped as nonterminal, 1002 and 1003 reaped
    assert len(candidates) == 2
    assert any(c.id == "run_completed" for c in candidates)
    assert any(c.id == "run_failed" for c in candidates)
    # Check skipped has the running one
    assert any(s[0].id == "run_running" and "slurm_nonterminal" in s[1] for s in skipped)


def test_reap_sacct_error_skips(temp_catalog, monkeypatch):
    """Test sacct error results in skip."""
    from bathos.reap import reap_runs

    run = create_run("run_test", age_hours=25, status="running", slurm_job_id="9999")
    write_run(run, temp_catalog)

    def mock_run(cmd, *args, **kwargs):
        if "9999" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "Error")
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr("subprocess.run", mock_run)

    candidates, skipped = reap_runs(
        temp_catalog, older_than_h=24, dry_run=True, apply=False
    )
    assert len(candidates) == 0
    assert len(skipped) == 1
    assert "sacct_error" in skipped[0][1]


def test_reap_sacct_no_record_old_reaped(temp_catalog, monkeypatch):
    """Test empty sacct output on 337h-old run is reaped."""
    from bathos.reap import reap_runs

    run_30h = create_run("run_30h", age_hours=30, status="running", slurm_job_id="5001")
    run_337h = create_run("run_337h", age_hours=337, status="running", slurm_job_id="5002")

    write_run(run_30h, temp_catalog)
    write_run(run_337h, temp_catalog)

    def mock_run(cmd, *args, **kwargs):
        # Empty output for all (sacct has no record)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("subprocess.run", mock_run)

    candidates, skipped = reap_runs(
        temp_catalog, older_than_h=24, dry_run=True, apply=False
    )
    # 30h should skip as no_record, 337h should reap
    assert len(candidates) == 1
    assert candidates[0].id == "run_337h"
    assert len(skipped) == 1
    assert "sacct_no_record" in skipped[0][1]


def test_reap_same_host_live_process_skipped(temp_catalog, monkeypatch):
    """Test (vii) - same-host live process is skipped."""
    from bathos.reap import reap_runs

    # Get this host's hostname
    import socket
    this_host = socket.gethostname()

    run = create_run(
        "run_test", age_hours=25, status="running", hostname=this_host
    )
    run.command = "sleep"
    run.argv = ["sleep", "100"]

    write_run(run, temp_catalog)

    # Spawn a sleep process that matches
    import os
    pid = os.fork()
    if pid == 0:
        # Child: exec sleep
        os.execvp("sleep", ["sleep", "100"])
    else:
        # Parent: test
        try:
            candidates, skipped = reap_runs(
                temp_catalog, older_than_h=24, dry_run=True, apply=False
            )
            assert len(candidates) == 0
            assert len(skipped) == 1
            assert "same_host_live_process" in skipped[0][1]
        finally:
            # Kill child
            import signal
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)


def test_reap_warm_tier_update_list_runs(temp_catalog):
    """Test (viii) - list_runs reports abandoned after apply on warm tier."""
    from bathos.compact import compact
    from bathos.query import list_runs
    from bathos.reap import reap_runs

    run = create_run("run_test", age_hours=25, status="running")
    write_run(run, temp_catalog)

    # Build warm tier
    compact(temp_catalog)

    # Verify run shows as running before reap
    runs_before = list_runs(temp_catalog)
    assert len(runs_before) == 1
    assert runs_before[0].status == "running"

    # Reap
    reap_runs(temp_catalog, older_than_h=24, dry_run=False, apply=True)

    # Verify warm tier is reconciled
    runs_after = list_runs(temp_catalog)
    assert len(runs_after) == 1
    assert runs_after[0].status == "abandoned"


def test_reap_warm_tier_disagreement_reconciled(temp_catalog):
    """Test (ix) - disagreement between tiers reconciled by reap."""
    from bathos.compact import compact
    from bathos.query import list_runs
    from bathos.reap import reap_runs

    # Create a run that's completed in cool tier
    run_completed = create_run("run_test", age_hours=25, status="completed")
    write_run(run_completed, temp_catalog)

    # Build warm tier (status will be completed)
    compact(temp_catalog)

    # Manually edit warm tier to disagree: change status to running in DB
    import duckdb
    db_path = temp_catalog / "bathos.db"
    con = duckdb.connect(str(db_path))
    con.execute("UPDATE runs SET status = 'running' WHERE id = 'run_test'")
    con.close()

    # Verify disagreement exists
    runs_warm_before = list_runs(temp_catalog)
    assert len(runs_warm_before) == 1
    assert runs_warm_before[0].status == "running"  # warm says running

    # Cool tier still says completed
    cool_runs = read_runs(temp_catalog)
    assert len(cool_runs) == 1
    assert cool_runs[0].status == "completed"

    # Now reap should reconcile (completed is NOT reaped, but warm should be updated)
    reap_runs(temp_catalog, older_than_h=24, dry_run=False, apply=True)

    # Check that list_runs (warm path) reports completed (reconciled)
    runs_after = list_runs(temp_catalog)
    assert len(runs_after) == 1
    assert runs_after[0].status == "completed"


def test_reap_revert_restores_status(temp_catalog):
    """Test (x) - revert restores running status and preserves other fields."""
    from bathos.reap import reap_runs

    original = create_run("run_test", age_hours=25, status="running")
    original.git_hash = "deadbeef"
    write_run(original, temp_catalog)

    # Reap it
    reap_runs(temp_catalog, older_than_h=24, dry_run=False, apply=True)

    # Verify it's abandoned
    reaped = read_runs(temp_catalog)[0]
    assert reaped.status == "abandoned"

    # Revert
    reap_runs(temp_catalog, older_than_h=24, dry_run=False, apply=True, revert=True, revert_ids=["run_test"])

    # Verify restored
    restored = read_runs(temp_catalog)[0]
    assert restored.status == "running"
    assert restored.git_hash == "deadbeef"


def test_reap_status_only_selector_fails_red(temp_catalog):
    """Test negative control - status-only selector without window should fail."""
    # This test should fail if someone implements a reaper that only looks at status
    # without checking the time window
    from bathos.reap import reap_runs

    run_23h = create_run("run_23h", age_hours=23, status="running")
    run_25h = create_run("run_25h", age_hours=25, status="running")

    write_run(run_23h, temp_catalog)
    write_run(run_25h, temp_catalog)

    # If implementation only checks status without time window, it would reap both
    candidates, _ = reap_runs(
        temp_catalog, older_than_h=24, dry_run=True, apply=False
    )
    # Must only reap 25h, not 23h
    assert len(candidates) == 1
    assert candidates[0].id == "run_25h"


def test_reap_warm_tier_step_required(temp_catalog):
    """Test negative control (viii) - without warm tier reconciliation, list_runs shows stale."""
    from bathos.compact import compact
    from bathos.query import list_runs
    from bathos.reap import reap_runs

    run = create_run("run_test", age_hours=25, status="running")
    write_run(run, temp_catalog)

    # Build warm tier
    compact(temp_catalog)

    # Reap WITHOUT warm tier reconciliation (this test ensures the plan's requirement)
    # This would fail if reap doesn't update the warm tier
    reap_runs(temp_catalog, older_than_h=24, dry_run=False, apply=True)

    # Verify list_runs reports abandoned (from the warm tier)
    runs = list_runs(temp_catalog)
    assert len(runs) == 1
    # If warm-tier reconciliation is missing, this would still show "running"
    assert runs[0].status == "abandoned", "warm tier not reconciled"
