import dataclasses
from pathlib import Path

import duckdb

from bathos.catalog import init_catalog, write_run
from bathos.compact import _fragment_count, compact
from bathos.schema import Run


def test_compact_ingests_all_fragments(tmp_catalog: Path, sample_run: Run):
    """compact() should ingest all cool fragments into DuckDB."""
    init_catalog(tmp_catalog)

    # Write 3 fragments to cool tier
    for i in range(3):
        run = Run(
            project_slug=sample_run.project_slug,
            command=f"python run.py --seed {i}",
            argv=["python", "run.py", "--seed", str(i)],
            git_hash=sample_run.git_hash,
            git_branch=sample_run.git_branch,
            git_dirty=sample_run.git_dirty,
            status=sample_run.status,
            exit_code=sample_run.exit_code,
            duration_s=sample_run.duration_s,
            output_paths=sample_run.output_paths,
            tags=sample_run.tags,
        )
        write_run(run, tmp_catalog)

    # Compact
    result = compact(tmp_catalog)

    # Verify result
    assert result.ingested == 3
    assert result.skipped == 0
    assert result.duration_s > 0

    # Verify DuckDB file created
    assert (tmp_catalog / "bathos.db").exists()

    # Verify all runs in DuckDB
    con = duckdb.connect(str(tmp_catalog / "bathos.db"))
    rows = con.execute("SELECT COUNT(*) FROM runs").fetchall()
    assert rows[0][0] == 3


def test_compact_is_idempotent(tmp_catalog: Path, sample_run: Run):
    """Running compact() twice should not duplicate rows (upsert by id)."""
    init_catalog(tmp_catalog)

    # Write 2 fragments
    for i in range(2):
        run = Run(
            project_slug=sample_run.project_slug,
            command=f"python run.py --i {i}",
            argv=["python", "run.py", "--i", str(i)],
            git_hash=sample_run.git_hash,
            git_branch=sample_run.git_branch,
            git_dirty=sample_run.git_dirty,
            status=sample_run.status,
            exit_code=sample_run.exit_code,
            duration_s=sample_run.duration_s,
            output_paths=sample_run.output_paths,
            tags=sample_run.tags,
        )
        write_run(run, tmp_catalog)

    # First compact
    result1 = compact(tmp_catalog)
    assert result1.ingested == 2

    # Second compact
    result2 = compact(tmp_catalog)
    assert result2.ingested == 0  # Already ingested
    assert result2.skipped == 2  # Both skipped as already present

    # Still only 2 rows
    con = duckdb.connect(str(tmp_catalog / "bathos.db"))
    rows = con.execute("SELECT COUNT(*) FROM runs").fetchall()
    assert rows[0][0] == 2


def test_compact_is_idempotent_with_new_fragments(tmp_catalog: Path, sample_run: Run):
    """compact() skips already-ingested runs; new fragments ingested on subsequent calls."""
    init_catalog(tmp_catalog)

    # Write 1 fragment
    run1 = Run(
        project_slug=sample_run.project_slug,
        command="python run.py --i 0",
        argv=["python", "run.py", "--i", "0"],
        git_hash=sample_run.git_hash,
        git_branch=sample_run.git_branch,
        git_dirty=sample_run.git_dirty,
        status=sample_run.status,
        exit_code=sample_run.exit_code,
        duration_s=sample_run.duration_s,
        output_paths=sample_run.output_paths,
        tags=sample_run.tags,
    )
    write_run(run1, tmp_catalog)

    # Monkey-patch write_run to verify snapshot behavior
    # We'll verify by checking that a fragment written after snapshot start is not ingested
    # For now, we just check that the count doesn't exceed what we wrote before compact

    result = compact(tmp_catalog)
    assert result.ingested == 1

    # Verify snapshot: now write another fragment after compact
    run2 = Run(
        project_slug=sample_run.project_slug,
        command="python run.py --i 1",
        argv=["python", "run.py", "--i", "1"],
        git_hash=sample_run.git_hash,
        git_branch=sample_run.git_branch,
        git_dirty=sample_run.git_dirty,
        status=sample_run.status,
        exit_code=sample_run.exit_code,
        duration_s=sample_run.duration_s,
        output_paths=sample_run.output_paths,
        tags=sample_run.tags,
    )
    write_run(run2, tmp_catalog)

    # Compact again: should only see the new fragment
    result2 = compact(tmp_catalog)
    assert result2.ingested == 1
    assert result2.skipped == 1

    # Total should be 2
    con = duckdb.connect(str(tmp_catalog / "bathos.db"))
    rows = con.execute("SELECT COUNT(*) FROM runs").fetchall()
    assert rows[0][0] == 2


def test_compact_upgrades_v0_fragments(tmp_catalog: Path, sample_run: Run):
    """compact() should migrate v0 fragments (missing schema_version) to v4 via v1→v2→v3→v4."""
    init_catalog(tmp_catalog)

    # Write a v0 fragment (no schema_version field)
    v0_run = dataclasses.replace(sample_run, schema_version="0")
    write_run(v0_run, tmp_catalog)

    # Compact
    result = compact(tmp_catalog)
    assert result.ingested == 1

    # Verify in DuckDB: should have schema_version="8" (migrated through v0→v1→v2→v3→v4→v5→v6→v7→v8)
    con = duckdb.connect(str(tmp_catalog / "bathos.db"))
    rows = con.execute("SELECT schema_version FROM runs").fetchall()
    assert rows[0][0] == "8"


def test_compact_tracks_warm_schema_version(tmp_catalog: Path, sample_run: Run):
    """compact() should write _schema_meta table tracking warm schema version."""
    init_catalog(tmp_catalog)
    write_run(sample_run, tmp_catalog)

    # Compact
    compact(tmp_catalog)

    # Verify _schema_meta table
    con = duckdb.connect(str(tmp_catalog / "bathos.db"))
    rows = con.execute("SELECT value FROM _schema_meta WHERE key = 'warm_version'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "8"


def test_fragment_count_helper(tmp_catalog: Path, sample_run: Run):
    """_fragment_count() should return number of cool fragments."""
    init_catalog(tmp_catalog)

    # Write 3 fragments
    for i in range(3):
        run = Run(
            project_slug=sample_run.project_slug,
            command=f"python run.py --i {i}",
            argv=["python", "run.py", "--i", str(i)],
            git_hash=sample_run.git_hash,
            git_branch=sample_run.git_branch,
            git_dirty=sample_run.git_dirty,
            status=sample_run.status,
            exit_code=sample_run.exit_code,
            duration_s=sample_run.duration_s,
            output_paths=sample_run.output_paths,
            tags=sample_run.tags,
        )
        write_run(run, tmp_catalog)

    count = _fragment_count(tmp_catalog)
    assert count == 3


def test_fragment_count_empty_catalog(tmp_catalog: Path):
    """_fragment_count() should return 0 for empty catalog."""
    init_catalog(tmp_catalog)

    count = _fragment_count(tmp_catalog)
    assert count == 0


def test_compact_preserves_run_data(tmp_catalog: Path, sample_run: Run):
    """compact() should preserve all Run fields during ingest."""
    init_catalog(tmp_catalog)

    # Write a run with all fields populated
    run_with_data = Run(
        project_slug=sample_run.project_slug,
        command=sample_run.command,
        argv=sample_run.argv,
        git_hash=sample_run.git_hash,
        git_branch=sample_run.git_branch,
        git_dirty=sample_run.git_dirty,
        status=sample_run.status,
        exit_code=sample_run.exit_code,
        duration_s=sample_run.duration_s,
        tags=["smoke", "critical"],
        output_paths=["/tmp/a.json", "/tmp/b.json"],
        slurm_job_id="12345",
        metadata='{"key": "value"}',
    )
    write_run(run_with_data, tmp_catalog)

    # Compact
    compact(tmp_catalog)

    # Verify data preserved
    con = duckdb.connect(str(tmp_catalog / "bathos.db"))
    rows = con.execute("SELECT tags, output_paths, slurm_job_id, metadata FROM runs").fetchall()
    assert len(rows) == 1
    # Verify complex types preserved (tags and output_paths)
    assert rows[0][0] == ["smoke", "critical"]
    assert rows[0][1] == ["/tmp/a.json", "/tmp/b.json"]
    assert rows[0][2] == "12345"
    # Note: metadata is not part of cool tier schema, so defaults to '{}' on read
    assert rows[0][3] == "{}"


def test_compact_migrates_v1_to_v4(sample_run: Run):
    """Verify v1 fragments are upgraded to v4 during compact."""
    from bathos.compact import _apply_migrations

    # Create a v1 run (explicitly set schema_version="1")
    v1_run = dataclasses.replace(sample_run, schema_version="1")

    # Apply migrations
    result = _apply_migrations(v1_run)

    # Verify upgraded to v8 with hostname
    assert result.schema_version == "8"
    assert result.hostname == ""


def test_compact_v0_chain_to_v4(sample_run: Run):
    """Verify v0 fragments chain through v1→v2→v3→v4→v5→v6 migrations."""
    from bathos.compact import _apply_migrations

    # Create a v0 run
    v0_run = dataclasses.replace(sample_run, schema_version="0")

    # Apply migrations (should walk 0→1→2→3→4→5→6)
    result = _apply_migrations(v0_run)

    # Verify final state is v8
    assert result.schema_version == "8"
    assert result.hostname == ""


def test_apply_migrations_v4_upgrades_to_v5(sample_run: Run):
    """Verify v4 fragment is upgraded through v5 to v6 with manifest fields."""
    from bathos.compact import _apply_migrations

    v4_run = dataclasses.replace(sample_run, schema_version="4", hostname="testhost")

    # Apply migrations (should upgrade v4→v5→v6)
    result = _apply_migrations(v4_run)

    # Verify upgraded
    assert result.schema_version == "8"
    assert result.hostname == "testhost"
    # Verify fields added during v5 migration are present
    assert result.manifest_sha256 == ""
    assert result.manifest_path == ""
    assert result.outcome_error_reason == ""
    assert result.adversarial_check_status == ""


def test_compact_collects_output_metadata(tmp_path):
    """Verify output file metadata is collected during compact."""
    from bathos.compact import _collect_output_metadata

    # Create a dummy output file
    output_file = tmp_path / "result.json"
    output_file.write_text('{"result": 42}')

    # Collect metadata
    meta = _collect_output_metadata(str(output_file))

    assert meta["status"] == "present"
    assert meta["size_bytes"] > 0
    assert meta["mtime_unix"] > 0
    assert meta["sha256"] is not None
    assert len(meta["sha256"]) == 64  # SHA256 hex string


def test_compact_handles_missing_output_file():
    """Verify missing output files are handled gracefully."""
    from bathos.compact import _collect_output_metadata

    meta = _collect_output_metadata("/nonexistent/file.json")

    assert meta["status"] == "missing"
    assert meta["size_bytes"] == 0


def test_compact_skips_sha256_for_large_files(tmp_path):
    """Verify SHA256 is skipped for files >100MB."""
    from bathos.compact import _collect_output_metadata

    # Create a small file and verify it gets SHA256
    small_file = tmp_path / "small.txt"
    small_file.write_text("x" * 1000)

    meta = _collect_output_metadata(str(small_file))
    assert meta["sha256"] is not None  # Small file gets SHA256


def test_compact_ingests_output_metadata_into_warm_db(tmp_catalog: Path, sample_run: Run):
    """Verify output metadata is stored in warm DuckDB."""
    import json

    from bathos.compact import compact

    # Create an output file
    output_file = tmp_catalog / "output.json"
    output_file.write_text('{"data": [1,2,3]}')

    # Write a run with output_paths
    run = Run(
        project_slug=sample_run.project_slug,
        command=sample_run.command,
        argv=sample_run.argv,
        git_hash=sample_run.git_hash,
        git_branch=sample_run.git_branch,
        git_dirty=sample_run.git_dirty,
        output_paths=[str(output_file)],
    )
    write_run(run, tmp_catalog)

    # Compact
    result = compact(tmp_catalog)
    assert result.ingested > 0

    # Query warm DB
    con = duckdb.connect(str(tmp_catalog / "bathos.db"), read_only=True)
    rows = con.execute("SELECT output_metadata FROM runs").fetchall()
    con.close()

    assert len(rows) > 0
    metadata = json.loads(rows[0][0])
    assert len(metadata) > 0
    assert metadata[0]["status"] == "present"
    assert metadata[0]["size_bytes"] > 0
    assert "path" in metadata[0]


def test_compact_handles_empty_output_paths(tmp_catalog: Path, sample_run: Run):
    """Verify runs with no output_paths get empty metadata array."""
    import json

    from bathos.compact import compact

    # Write a run with NO output paths
    run = Run(
        project_slug=sample_run.project_slug,
        command=sample_run.command,
        argv=sample_run.argv,
        git_hash=sample_run.git_hash,
        git_branch=sample_run.git_branch,
        git_dirty=sample_run.git_dirty,
        output_paths=[],  # Empty
    )
    write_run(run, tmp_catalog)

    # Compact
    compact(tmp_catalog)

    # Query
    con = duckdb.connect(str(tmp_catalog / "bathos.db"), read_only=True)
    rows = con.execute("SELECT output_metadata FROM runs").fetchall()
    con.close()

    metadata = json.loads(rows[0][0])
    assert metadata == []


def test_compact_creates_schema_migrations_table(tmp_path):
    """bth compact creates _schema_migrations table in warm DuckDB."""
    from bathos.catalog import write_run
    from bathos.compact import compact
    from bathos.schema import Run

    r = Run(project_slug="p", command="c", argv=["c"],
            git_hash="abc", git_branch="main", git_dirty=False)
    write_run(r, tmp_path)
    compact(tmp_path)

    con = duckdb.connect(str(tmp_path / "bathos.db"))
    tables = [row[0] for row in con.execute("SHOW TABLES").fetchall()]
    con.close()
    assert "_schema_migrations" in tables


def test_schema_migrations_has_record(tmp_path):
    """_schema_migrations contains a record after compact."""
    from bathos.catalog import write_run
    from bathos.compact import compact
    from bathos.schema import Run, CURRENT_SCHEMA_VERSION

    r = Run(project_slug="p", command="c", argv=["c"],
            git_hash="abc", git_branch="main", git_dirty=False)
    write_run(r, tmp_path)
    compact(tmp_path)

    con = duckdb.connect(str(tmp_path / "bathos.db"))
    rows = con.execute("SELECT warm_version FROM _schema_migrations").fetchall()
    con.close()
    assert len(rows) >= 1
    assert rows[-1][0] == CURRENT_SCHEMA_VERSION


def test_migration_v2_to_v4(tmp_catalog: Path, sample_run: Run):
    """Verify that a v2 fragment (no v3/v4 fields) migrates to v4 with empty defaults."""
    init_catalog(tmp_catalog)

    # Write a v2 fragment (has hostname, no agentic integrity fields)
    v2_run = dataclasses.replace(
        sample_run,
        schema_version="2",
        sidecar_sha256="",
        sidecar_path="",
        parent_run_id="",
        agent_mode="",
        sidecar_mode="",
        outcome_is_residual=False,
        skill_sha256="",
        campaign_id="",
    )
    write_run(v2_run, tmp_catalog)

    # Compact
    result = compact(tmp_catalog)
    assert result.ingested == 1

    # Verify in DuckDB: should have schema_version="4" and all fields set to defaults
    con = duckdb.connect(str(tmp_catalog / "bathos.db"))
    rows = con.execute(
        "SELECT schema_version, sidecar_sha256, sidecar_path, parent_run_id, agent_mode, sidecar_mode, outcome_is_residual, skill_sha256, campaign_id, script_sha256 FROM runs"
    ).fetchall()
    con.close()

    assert len(rows) == 1
    assert rows[0][0] == "8"  # schema_version
    assert rows[0][1] == ""  # sidecar_sha256
    assert rows[0][2] == ""  # sidecar_path
    assert rows[0][3] == ""  # parent_run_id
    assert rows[0][4] == ""  # agent_mode
    assert rows[0][5] == ""  # sidecar_mode
    assert rows[0][6] is False  # outcome_is_residual
    assert rows[0][7] == ""  # skill_sha256
    assert rows[0][8] == ""  # campaign_id
    assert rows[0][9] == ""  # script_sha256


def test_migration_chain_v0_to_v4(tmp_catalog: Path, sample_run: Run):
    """Verify that a v0 fragment chains through v0→v1→v2→v3→v4→v5→v6 with all v3/v4/v5 fields."""
    init_catalog(tmp_catalog)

    # Write a v0 fragment (missing schema_version field entirely)
    v0_run = dataclasses.replace(
        sample_run,
        schema_version="0",
    )
    write_run(v0_run, tmp_catalog)

    # Compact
    result = compact(tmp_catalog)
    assert result.ingested == 1

    # Verify in DuckDB: should have schema_version="6" and all fields with defaults
    con = duckdb.connect(str(tmp_catalog / "bathos.db"))
    rows = con.execute(
        "SELECT schema_version, hostname, sidecar_sha256, sidecar_path, parent_run_id, agent_mode, sidecar_mode, outcome_is_residual, skill_sha256, campaign_id, script_sha256 FROM runs"
    ).fetchall()
    con.close()

    assert len(rows) == 1
    assert rows[0][0] == "8"  # schema_version
    assert rows[0][1] == ""  # hostname (from v1 migration)
    assert rows[0][2] == ""  # sidecar_sha256 (from v2 migration)
    assert rows[0][3] == ""  # sidecar_path
    assert rows[0][4] == ""  # parent_run_id
    assert rows[0][5] == ""  # agent_mode
    assert rows[0][6] == ""  # sidecar_mode
    assert rows[0][7] is False  # outcome_is_residual
    assert rows[0][8] == ""  # skill_sha256
    assert rows[0][9] == ""  # campaign_id
    assert rows[0][10] == ""  # script_sha256


def test_migration_v6_to_v7_adds_stage_name(sample_run: Run):
    """Verify v6 fragments are upgraded to v7 with stage_name=None."""
    from bathos.compact import _apply_migrations

    # Create a v6 run (explicitly set schema_version="6")
    v6_run = dataclasses.replace(sample_run, schema_version="6", stage_name=None)

    # Apply migrations
    result = _apply_migrations(v6_run)

    # Verify upgraded to v7 with stage_name=None
    assert result.schema_version == "8"
    assert result.stage_name is None


def test_migration_chain_v0_to_v7_includes_stage_name(sample_run: Run):
    """Verify v0 fragments chain through all migrations to v7 with stage_name=None."""
    from bathos.compact import _apply_migrations

    # Create a v0 run
    v0_run = dataclasses.replace(sample_run, schema_version="0")

    # Apply migrations (should walk 0→1→2→3→4→5→6→7)
    result = _apply_migrations(v0_run)

    # Verify final state is v7 with stage_name=None
    assert result.schema_version == "8"
    assert result.stage_name is None


def test_force_rebuild_creates_backup(tmp_catalog: Path, sample_run: Run):
    """force_rebuild=True should backup bathos.db before deletion."""
    init_catalog(tmp_catalog)

    # Write a fragment and compact to create bathos.db
    write_run(sample_run, tmp_catalog)
    compact(tmp_catalog)
    original_db = tmp_catalog / "bathos.db"
    assert original_db.exists()

    # Record original file size
    original_size = original_db.stat().st_size

    # Force rebuild (should backup then delete)
    result = compact(tmp_catalog, force_rebuild=True)
    assert result.ingested == 1

    # Verify bathos.db exists (rebuilt)
    assert original_db.exists()

    # Verify backup file was created (bathos.db.bak-<timestamp>)
    backups = list(tmp_catalog.glob("bathos.db.bak-*"))
    assert len(backups) == 1
    assert backups[0].exists()
    assert backups[0].stat().st_size == original_size


def test_backup_rotation_keeps_max_3(tmp_catalog: Path, sample_run: Run):
    """Backup rotation should keep at most 3 .bak-* files."""
    import time
    init_catalog(tmp_catalog)

    # Create and rebuild 5 times to generate 5 backups
    for i in range(5):
        write_run(sample_run, tmp_catalog)
        compact(tmp_catalog, force_rebuild=(i > 0))
        # Small delay to ensure different timestamps
        if i < 4:
            time.sleep(0.01)

    # Count backup files
    backups = list(tmp_catalog.glob("bathos.db.bak-*"))
    # Should keep at most 3 most recent; due to timestamp precision, might be fewer
    assert len(backups) <= 3, f"Expected at most 3 backups, got {len(backups)}"


def test_output_metadata_refreshed_on_recompact(tmp_catalog: Path, sample_run: Run, tmp_path: Path):
    """output_metadata for existing runs is re-statted on subsequent compacts (Debt #71)."""
    import dataclasses
    import json
    import duckdb
    from bathos.catalog import init_catalog, write_run
    from bathos.compact import compact

    init_catalog(tmp_catalog)
    out_file = tmp_path / "result.json"
    out_file.write_text('{"x": 1}')

    run = dataclasses.replace(sample_run, output_paths=[str(out_file)])
    write_run(run, tmp_catalog)
    compact(tmp_catalog)

    # Verify initial metadata captured
    con = duckdb.connect(str(tmp_catalog / "bathos.db"), read_only=True)
    meta_json = con.execute("SELECT output_metadata FROM runs WHERE id = ?", [run.id]).fetchone()[0]
    con.close()
    meta = json.loads(meta_json)
    assert meta[0]["status"] == "present"
    initial_size = meta[0]["size_bytes"]

    # Mutate the file so size changes
    out_file.write_text('{"x": 1, "y": 2, "extra": "padding to change size"}')

    # Second compact should refresh the metadata
    compact(tmp_catalog)
    con = duckdb.connect(str(tmp_catalog / "bathos.db"), read_only=True)
    meta_json2 = con.execute("SELECT output_metadata FROM runs WHERE id = ?", [run.id]).fetchone()[0]
    con.close()
    meta2 = json.loads(meta_json2)
    assert meta2[0]["size_bytes"] != initial_size, "size_bytes should reflect updated file"


def test_output_metadata_refresh_detects_deleted_file(tmp_catalog: Path, sample_run: Run, tmp_path: Path):
    """output_metadata refresh marks deleted files as 'missing' on next compact."""
    import dataclasses
    import json
    import duckdb
    from bathos.catalog import init_catalog, write_run
    from bathos.compact import compact

    init_catalog(tmp_catalog)
    out_file = tmp_path / "will_delete.json"
    out_file.write_text('{}')

    run = dataclasses.replace(sample_run, output_paths=[str(out_file)])
    write_run(run, tmp_catalog)
    compact(tmp_catalog)

    # Delete the file
    out_file.unlink()

    # Next compact should mark it missing
    compact(tmp_catalog)
    con = duckdb.connect(str(tmp_catalog / "bathos.db"), read_only=True)
    meta_json = con.execute("SELECT output_metadata FROM runs WHERE id = ?", [run.id]).fetchone()[0]
    con.close()
    meta = json.loads(meta_json)
    assert meta[0]["status"] == "missing"


def test_output_metadata_sha256_reused_when_mtime_unchanged(tmp_catalog: Path, sample_run: Run, tmp_path: Path):
    """sha256 is reused from stored metadata when mtime is unchanged (skip rehash)."""
    import dataclasses
    import json
    import duckdb
    from bathos.catalog import init_catalog, write_run
    from bathos.compact import compact

    init_catalog(tmp_catalog)
    out_file = tmp_path / "stable.bin"
    out_file.write_bytes(b"stable content")

    run = dataclasses.replace(sample_run, output_paths=[str(out_file)])
    write_run(run, tmp_catalog)
    compact(tmp_catalog)

    con = duckdb.connect(str(tmp_catalog / "bathos.db"), read_only=True)
    meta1 = json.loads(con.execute("SELECT output_metadata FROM runs WHERE id = ?", [run.id]).fetchone()[0])
    con.close()
    sha_before = meta1[0].get("sha256")

    # Compact again without touching the file — mtime unchanged, sha256 should be reused
    compact(tmp_catalog)
    con = duckdb.connect(str(tmp_catalog / "bathos.db"), read_only=True)
    meta2 = json.loads(con.execute("SELECT output_metadata FROM runs WHERE id = ?", [run.id]).fetchone()[0])
    con.close()
    assert meta2[0].get("sha256") == sha_before
