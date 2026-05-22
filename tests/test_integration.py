"""End-to-end: init → run → ls → show → find → compact."""

import sys
from pathlib import Path

from typer.testing import CliRunner

from bathos.catalog import init_catalog, read_runs
from bathos.cli import app
from bathos.compact import compact
from bathos.query import run_sql

runner = CliRunner()


def test_full_workflow(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "intproj")

    # Lower threshold so 2 runs triggers banner; test banner appearance before/after compact
    import bathos.compact

    monkeypatch.setattr(bathos.compact, "COMPACTION_THRESHOLD", 1)

    # 1. init
    r = runner.invoke(app, ["init", "--slug", "intproj"])
    assert r.exit_code == 0
    assert (tmp_path / ".bth.toml").exists()
    assert (tmp_path / "scripts" / "experiments").is_dir()

    # 2. run a passing script
    r = runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])
    assert r.exit_code == 0

    # 3. run a failing script
    r = runner.invoke(app, ["run", sys.executable, "--", "-c", "raise SystemExit(1)"])
    assert r.exit_code == 1

    # 4a. ls shows both runs and banner BEFORE compact (threshold now 1, so 2 fragments triggers it)
    r = runner.invoke(app, ["ls"])
    assert r.exit_code == 0
    assert "intproj" in r.output
    lines = [line for line in r.output.splitlines() if "intproj" in line]
    assert len(lines) == 2
    # Banner should appear now since fragment count (2) > threshold (1)
    assert "uncompacted" in r.output

    # 5. find by status
    r = runner.invoke(app, ["find", "--status", "failed"])
    assert r.exit_code == 0
    assert "failed" in r.output

    # 6. show run detail
    init_catalog(catalog)
    runs = read_runs(catalog)
    run_id = runs[0].id
    r = runner.invoke(app, ["show", run_id])
    assert r.exit_code == 0
    # Rich panels show "Execution" header; full UUID not shown (short ID only)
    assert "Execution" in r.output

    # 7. sql escape hatch (cool tier; runs are in per-project subdirs)
    glob = str(catalog / "runs" / "intproj" / "run_*.parquet")
    r = runner.invoke(app, ["sql", f"SELECT count(*) FROM read_parquet('{glob}')"])
    assert r.exit_code == 0
    assert "2" in r.output

    # 8. compact into warm tier
    result = compact(catalog)
    assert result.ingested == 2
    assert result.skipped == 0
    assert (catalog / "bathos.db").exists()

    # 8b. verify banner logic: should_compact returns False after warm DB exists
    from bathos.compact import should_compact

    assert not should_compact(catalog), (
        "Banner should not appear after compact (warm DB now exists)"
    )

    # 9. verify warm tier has both runs via SQL
    rows = run_sql("SELECT count(*) FROM runs", catalog)
    assert len(rows) == 1
    assert rows[0][0] == 2


def test_full_workflow_140_141_142(tmp_path: Path, monkeypatch):
    """End-to-end integration test: schema v2, compact, archive, check, find.

    Workflow:
    1. Create v1 run with output file
    2. Compact (v1→v2 migration + output enrichment)
    3. Archive to cold tier
    4. Check runs (git-drift + output verification)
    5. Find with output-file filter

    Tests for tasks #140 (schema versioning), #141 (archive),
    #142 (results management / output filtering).
    """
    import json
    import subprocess

    from bathos.archive import archive
    from bathos.catalog import init_catalog, read_runs, write_run
    from bathos.checker import check_runs
    from bathos.compact import compact
    from bathos.query import _filter_runs_by_output_file, find_runs
    from bathos.schema import Run

    # Setup
    catalog_dir = tmp_path / ".bth" / "catalog"
    catalog_dir.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

    # Initialize catalog
    init_catalog(catalog_dir)

    # Create output files
    output_file1 = tmp_path / "analysis_result.json"
    output_file2 = tmp_path / "metrics.json"
    output_file1.write_text('{"success": true, "score": 0.95}')
    output_file2.write_text('{"latency_ms": 42, "throughput": 1000}')

    # 1. Create two runs (simulate v1 and v2 schemas)
    run1 = Run(
        id="run-v1-001",
        project_slug="prolix",
        command="simulate",
        argv=["simulate", "--nsteps=100"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        duration_s=10.5,
        output_paths=[str(output_file1)],
        tags=["simulation"],
    )
    run1.schema_version = "1"  # Simulate v1 fragment

    run2 = Run(
        id="run-v2-001",
        project_slug="prolix",
        command="analyze",
        argv=["analyze", "--model=gnn"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        duration_s=5.2,
        output_paths=[str(output_file2)],
        tags=["analysis"],
    )
    # run2 defaults to v2

    write_run(run1, catalog_dir)
    write_run(run2, catalog_dir)

    # 2. Compact (triggers v1→v2 migration + output metadata enrichment)
    compact_result = compact(catalog_dir)
    assert compact_result.ingested == 2, "Should ingest 2 runs"
    assert compact_result.skipped == 0, "First compact should have no skipped"

    # Verify runs are in warm DuckDB (warm_runs from read_runs are cool-tier still)
    # The migrations happen on the DuckDB side, not in memory
    assert (catalog_dir / "bathos.db").exists(), "Warm DB should be created"

    # Verify cool tier still has runs (unchanged)
    cool_runs = read_runs(catalog_dir)
    assert len(cool_runs) == 2, "Should have 2 runs in cool tier"

    # Verify runs can be queried via DuckDB (which applies migrations)
    from bathos.query import list_runs

    warm_runs = list_runs(catalog_dir)
    assert len(warm_runs) == 2, "Should have 2 runs via warm tier"
    from bathos.schema import CURRENT_SCHEMA_VERSION
    for run in warm_runs:
        # After migration, all runs should be at current schema version in the database
        assert run.schema_version == CURRENT_SCHEMA_VERSION, f"Run {run.id} should be at current schema version in warm DB after compact"
        assert hasattr(run, "hostname"), "Run should have hostname field"

    # Verify output metadata was collected
    # (At minimum, runs should be readable with output_paths intact)
    run1_loaded = [r for r in warm_runs if r.id == "run-v1-001"][0]
    run2_loaded = [r for r in warm_runs if r.id == "run-v2-001"][0]
    assert len(run1_loaded.output_paths) > 0, "Run1 should have output paths"
    assert len(run2_loaded.output_paths) > 0, "Run2 should have output paths"

    # 3. Archive runs to cold tier
    archive_dir = tmp_path / "archive"
    archive_result = archive(catalog_dir, archive_root=archive_dir)
    assert archive_result.runs_archived == 2, "Should archive 2 runs"
    assert archive_result.partitions_created == 1, "Should create 1 partition (same project/date)"
    assert archive_result.manifest_path.exists(), "Manifest should exist"

    # Verify partitions exist
    partition = list((archive_dir / "project=prolix").glob("year=*/month=*/runs.parquet"))
    assert len(partition) == 1, "Should have 1 partition file"

    # Verify manifest content
    manifest = json.loads(archive_result.manifest_path.read_text())
    assert manifest["runs_archived"] == 2, "Manifest should record 2 archived runs"
    assert manifest["partitions"] == 1, "Manifest should record 1 partition"
    assert len(manifest["entries"]) == 1, "Manifest should have 1 partition entry"
    assert manifest["entries"][0]["rows"] == 2, "Partition should contain 2 rows"

    # 4. Check runs (git-drift + output verification)
    # First initialize git repo
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

    # Get current HEAD hash
    head_hash = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()

    # Create new runs with current git hash
    run3 = Run(
        id="run-check-001",
        project_slug="prolix",
        command="validate",
        argv=["validate", "--data=test"],
        git_hash=head_hash,
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        duration_s=3.1,
        output_paths=[str(output_file1)],
    )
    write_run(run3, catalog_dir)
    compact(catalog_dir)  # Re-compact to ingest new run

    check_results = check_runs(catalog_dir, tmp_path)
    # Should have runs from both old and new compactions
    assert len(check_results) >= 1, "Should check at least 1 run"
    # At least one should be OK (the run with current HEAD)
    statuses = {r.status for r in check_results}
    assert "OK" in statuses or "UNKNOWN_CODE" in statuses, (
        f"Should have OK or UNKNOWN_CODE status, got {statuses}"
    )

    # 5. Find with output-file filter
    all_runs = find_runs(catalog_dir)
    assert len(all_runs) >= 2, "Should find at least 2 runs"

    json_runs = _filter_runs_by_output_file(all_runs, pattern="*result*.json")
    assert len(json_runs) >= 1, "Should find runs with *result*.json pattern"

    csv_runs = _filter_runs_by_output_file(all_runs, pattern="*.csv")
    assert len(csv_runs) == 0, "Should find no .csv files"

    # Verify all runs have required fields
    for run in all_runs:
        assert run.id, "Run should have id"
        assert run.project_slug == "prolix", "Run should be from prolix project"
        assert run.status in ["completed", "failed", "running"], "Status should be valid"

    # 6. Verify final state
    assert catalog_dir.joinpath("bathos.db").exists(), "Warm DB should exist"
    assert archive_dir.exists(), "Archive directory should exist"
    assert archive_result.manifest_path.exists(), "Archive manifest should exist"

    # 7. Idempotency check: compact again should skip already-ingested
    compact_result2 = compact(catalog_dir)
    # Since we already compacted after adding run3, it's also already ingested
    # The second compact should skip all 3 runs
    assert compact_result2.skipped == 3, "Should skip all already-ingested runs on re-compact"
    assert compact_result2.ingested == 0, "Should ingest no new runs on re-compact"
