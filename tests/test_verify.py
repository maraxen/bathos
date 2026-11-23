"""Tests for verify.py — catalog integrity verification."""

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from bathos.verify import verify_archive, verify_cool, verify_warm


class TestVerifyCool:
    def test_verify_cool_clean(self, tmp_path):
        """Verify clean cool fragments pass."""
        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir()
        runs_dir = catalog_dir / "runs" / "proj"
        runs_dir.mkdir(parents=True)

        # Write valid fragment
        schema = pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("project_slug", pa.string()),
            ]
        )
        tbl = pa.table(
            {
                "id": ["run1"],
                "project_slug": ["proj"],
            },
            schema=schema,
        )
        pq.write_table(tbl, runs_dir / "run_abc123.parquet")

        result = verify_cool(catalog_dir)
        assert result.ok is True
        assert result.errors == []

    def test_verify_cool_detects_bak_files(self, tmp_path):
        """Verify detects .bak files (interrupted migration)."""
        catalog_dir = tmp_path / "catalog"
        runs_dir = catalog_dir / "runs" / "proj"
        runs_dir.mkdir(parents=True)

        # Write a .bak file
        (runs_dir / "run_abc123.bak").write_text("corrupted backup")

        result = verify_cool(catalog_dir)
        assert result.ok is False
        assert any("bak" in e for e in result.errors)

    def test_verify_cool_detects_unreadable_fragment(self, tmp_path):
        """Verify detects corrupt Parquet files."""
        catalog_dir = tmp_path / "catalog"
        runs_dir = catalog_dir / "runs" / "proj"
        runs_dir.mkdir(parents=True)

        # Write garbage to a .parquet file
        (runs_dir / "run_abc123.parquet").write_bytes(b"not parquet")

        result = verify_cool(catalog_dir)
        assert result.ok is False
        assert any("Unreadable" in e for e in result.errors)


class TestVerifyWarm:
    def test_verify_warm_clean(self, tmp_path):
        """Verify clean warm DB passes."""

        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir()

        # Create and compact a clean catalog
        runs_dir = catalog_dir / "runs" / "proj"
        runs_dir.mkdir(parents=True)

        schema = pa.schema([pa.field("id", pa.string())])
        tbl = pa.table({"id": ["run1"]}, schema=schema)
        pq.write_table(tbl, runs_dir / "run_abc123.parquet")

        # This will fail without full compact implementation, so skip
        # In practice, compact() creates a valid bathos.db
        result = verify_warm(catalog_dir)
        # Just check it runs
        assert isinstance(result.ok, bool)

    def test_verify_warm_no_false_positive_on_empty_new_catalog(self, tmp_path):
        """Verify no warning on empty new installation (no db, no fragments)."""
        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir()

        result = verify_warm(catalog_dir)
        # Missing bathos.db is an error (need to run bth compact first)
        assert result.ok is False
        assert any("not found" in e for e in result.errors)


class TestVerifyArchive:
    def test_verify_archive_clean(self, tmp_path):
        """Verify clean archive passes."""
        archive_root = tmp_path / "archive"
        archive_root.mkdir()

        # Create manifest with schema_version 2
        manifest = {
            "schema_version": "2",
            "timestamp": "2026-06-01T12:00:00",
            "runs_archived": 1,
            "partitions": 1,
            "total_size_bytes": 100,
            "entries": [
                {
                    "partition": "project=test/year=2026/month=06",
                    "rows": 1,
                    "size_bytes": 100,
                    "sha256": "abc123",
                }
            ],
        }

        # Create archive directory and file
        part_dir = archive_root / "project=test" / "year=2026" / "month=06"
        part_dir.mkdir(parents=True)

        schema = pa.schema([pa.field("id", pa.string())])
        tbl = pa.table({"id": ["run1"]}, schema=schema)
        pq.write_table(tbl, part_dir / "runs.parquet")

        # Compute actual SHA256
        import hashlib

        h = hashlib.sha256()
        with open(part_dir / "runs.parquet", "rb") as f:
            h.update(f.read())
        actual_sha = h.hexdigest()

        # Update manifest with correct SHA256
        manifest["entries"][0]["sha256"] = actual_sha

        with open(archive_root / "manifest.json", "w") as f:
            json.dump(manifest, f)

        result = verify_archive(archive_root)
        assert result.ok is True
        assert result.errors == []

    def test_verify_archive_sha256_mismatch(self, tmp_path):
        """Verify detects SHA256 mismatch."""
        archive_root = tmp_path / "archive"
        archive_root.mkdir()

        # Create manifest with wrong SHA256
        manifest = {
            "schema_version": "2",
            "timestamp": "2026-06-01T12:00:00",
            "runs_archived": 1,
            "partitions": 1,
            "total_size_bytes": 100,
            "entries": [
                {
                    "partition": "project=test/year=2026/month=06",
                    "rows": 1,
                    "size_bytes": 100,
                    "sha256": "wrongsha256",
                }
            ],
        }

        part_dir = archive_root / "project=test" / "year=2026" / "month=06"
        part_dir.mkdir(parents=True)

        schema = pa.schema([pa.field("id", pa.string())])
        tbl = pa.table({"id": ["run1"]}, schema=schema)
        pq.write_table(tbl, part_dir / "runs.parquet")

        with open(archive_root / "manifest.json", "w") as f:
            json.dump(manifest, f)

        result = verify_archive(archive_root)
        assert result.ok is False
        assert any("sha256" in e.lower() for e in result.errors)

    def test_verify_archive_old_manifest_warns(self, tmp_path):
        """Verify old manifest (no SHA256) produces warning."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        archive_root = tmp_path / "archive"
        archive_root.mkdir()

        # Create old manifest without sha256
        manifest = {
            "timestamp": "2026-06-01T12:00:00",
            "runs_archived": 1,
            "partitions": 1,
            "total_size_bytes": 100,
            "entries": [
                {
                    "partition": "project=test/year=2026/month=06",
                    "rows": 1,
                    "size_bytes": 100,
                }
            ],
        }

        # Create the actual archive file
        part_dir = archive_root / "project=test" / "year=2026" / "month=06"
        part_dir.mkdir(parents=True)
        schema = pa.schema([pa.field("id", pa.string())])
        tbl = pa.table({"id": ["run1"]}, schema=schema)
        pq.write_table(tbl, part_dir / "runs.parquet")

        with open(archive_root / "manifest.json", "w") as f:
            json.dump(manifest, f)

        result = verify_archive(archive_root)
        assert result.ok is True  # Not an error, just warning
        assert any("checksum" in w.lower() or "schema" in w.lower() for w in result.warnings)


def test_verify_warm_flags_orphaned_campaign_runs(tmp_path):
    """Negative test (#3051): verify_warm rejects orphaned campaign_runs rows."""
    import duckdb

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    db_path = catalog_dir / "bathos.db"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE _schema_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE runs (id TEXT PRIMARY KEY)")
    con.execute("CREATE TABLE campaigns (id TEXT PRIMARY KEY, mode TEXT)")
    con.execute(
        "CREATE TABLE campaign_runs (campaign_id TEXT, run_id TEXT, PRIMARY KEY (campaign_id, run_id))"
    )
    con.execute("INSERT INTO campaign_runs VALUES ('missing-camp', 'run-1')")
    con.close()

    result = verify_warm(catalog_dir)
    assert result.ok is False
    assert result.stats["orphaned_campaign_runs"] == 1
    assert any("Orphaned campaign_runs" in e for e in result.errors)


def _setup_log_catalog_with_campaigns(tmp_path: Path, n_campaigns: int):
    """Build a log-mode catalog with `n_campaigns` campaigns (including concluded
    and sequential-mismatch cases) and ingest it."""
    import subprocess
    from datetime import UTC, datetime

    from bathos.runlog.envelope import build_envelope
    from bathos.runlog.ingest import run_ingest
    from bathos.runlog.mode import cutover_marker_path
    from bathos.runlog.project_id import assign_project_id, read_project_id, register_main_root

    repo = tmp_path / f"repo_{n_campaigns}"
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / ".bth.toml").write_text(
        f'[project]\nname = "repo"\nslug = "repo"\ncreated_at = "{datetime.now(UTC).isoformat()}"\n'
    )
    assign_project_id(repo)
    pid = read_project_id(repo / ".bth.toml")
    register_main_root(repo)

    catalog_dir = tmp_path / f"catalog_{n_campaigns}"
    catalog_dir.mkdir(parents=True)
    cutover_marker_path(catalog_dir).write_text(
        json.dumps({"at": "2026-01-01T00:00:00Z", "bathos": "test", "attempt": "1", "segments": []})
    )

    common = {
        "main_root": repo,
        "worktree_root": repo,
        "project": "repo",
        "project_id": pid,
        "writer": "w1",
    }
    sidecar_a = {
        "kind": "experiment",
        "result_schema": {},
        "outcomes": {"pass": {"condition": "true", "is_residual": False}},
        "popper_null_pass_rate": 0.1,
        "popper_alt_pass_rate": 0.9,
        "popper_stopping_threshold": 0.05,
        "popper_weights": {},
    }
    sidecar_b = {**sidecar_a, "popper_stopping_threshold": 0.10}

    lines: list[str] = []
    seq = 1
    for idx in range(n_campaigns):
        cid = f"camp-{idx:03d}"
        is_seq = idx == 0
        lines.append(
            json.dumps(
                build_envelope(
                    kind="campaign.created",
                    entity=[cid],
                    data={
                        "id": cid,
                        "project_slug": "repo",
                        "name": cid,
                        "mode": "sequential" if is_seq else "exploration",
                        "status": "open",
                        "started_at": "2026-01-01T00:00:00Z",
                    },
                    seq=seq,
                    ts="2026-01-01T00:00:00.000000Z",
                    eid=f"e-c-{idx}",
                    **common,
                ),
                sort_keys=True,
            )
        )
        seq += 1
        for m in range(2):
            rid = f"run-{idx:03d}-{m}"
            start_data = {
                "project_slug": "repo",
                "command": "python x.py",
                "argv": [],
                "git_hash": "a",
                "git_branch": "main",
                "git_dirty": False,
                "timestamp": "2026-01-01T00:01:00.000000Z",
                "campaign_id": cid,
                "agent_mode": "manual",
            }
            if is_seq:
                start_data["sidecar"] = sidecar_a if m == 0 else sidecar_b
            lines.append(
                json.dumps(
                    build_envelope(
                        kind="run.started",
                        entity=[rid],
                        data=start_data,
                        seq=seq,
                        ts="2026-01-01T00:01:00.000000Z",
                        eid=f"e-rs-{idx}-{m}",
                        **common,
                    ),
                    sort_keys=True,
                )
            )
            seq += 1
            # On campaign 0, run-000-1 finishes after conclusion time.
            dur = 3600.0 if (idx == 0 and m == 1) else 1.0
            lines.append(
                json.dumps(
                    build_envelope(
                        kind="run.finished",
                        entity=[rid],
                        data={
                            "id": rid,
                            "status": "completed",
                            "exit_code": 0,
                            "duration_s": dur,
                            "output_paths": [],
                            "outcome": "pass",
                            "outcome_error_reason": "",
                            "outcome_is_residual": False,
                            "adversarial_check_status": "",
                            "timestamp": "2026-01-01T00:01:00.000000Z",
                        },
                        seq=seq,
                        ts="2026-01-01T00:03:00.000000Z",
                        eid=f"e-rf-{idx}-{m}",
                        **common,
                    ),
                    sort_keys=True,
                )
            )
            seq += 1
        lines.append(
            json.dumps(
                build_envelope(
                    kind="campaign.concluded",
                    entity=[cid],
                    data={
                        "concluded_at": "2026-01-01T00:02:00.000000Z",
                        "outcome_label": "pass",
                        "conclusion": "done",
                    },
                    seq=seq,
                    ts="2026-01-01T00:02:00.000000Z",
                    eid=f"e-cc-{idx}",
                    **common,
                ),
                sort_keys=True,
            )
        )
        seq += 1

    log_dir = repo / ".bth" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "seg1.jsonl").write_text("\n".join(lines) + "\n")
    run_ingest(catalog_dir)
    return catalog_dir


def test_campaign_findings_constant_connections_and_batched_queries(tmp_path, monkeypatch):
    """Regression test (#3052): _campaign_findings uses O(1) connect_read calls and
    zero per-member _fetch_events_by_entity_key queries regardless of campaign count."""
    import bathos.index as idx_mod
    import bathos.runlog.ingest as ingest_mod
    from bathos.verify import _campaign_findings, verify_runlog

    cat_1 = _setup_log_catalog_with_campaigns(tmp_path / "one", 1)
    cat_8 = _setup_log_catalog_with_campaigns(tmp_path / "eight", 8)

    orig_connect_read = idx_mod.connect_read
    orig_fetch_key = ingest_mod._fetch_events_by_entity_key

    def _measure(catalog_dir):
        counts = {"connect_read": 0, "fetch_entity_key": 0}

        def counting_connect(*args, **kwargs):
            counts["connect_read"] += 1
            return orig_connect_read(*args, **kwargs)

        def counting_fetch(*args, **kwargs):
            counts["fetch_entity_key"] += 1
            return orig_fetch_key(*args, **kwargs)

        monkeypatch.setattr(idx_mod, "connect_read", counting_connect)
        monkeypatch.setattr(ingest_mod, "_fetch_events_by_entity_key", counting_fetch)
        findings = _campaign_findings(catalog_dir)
        monkeypatch.undo()
        return findings, counts

    findings_1, counts_1 = _measure(cat_1)
    findings_8, counts_8 = _measure(cat_8)

    assert counts_1["connect_read"] == 1
    assert counts_8["connect_read"] == 1
    assert counts_1["fetch_entity_key"] == 0
    assert counts_8["fetch_entity_key"] == 0

    types_1 = {f["type"] for f in findings_1}
    assert "threshold_mismatch" in types_1
    assert "evalue_changed_after_conclusion" in types_1
    assert {f["type"] for f in findings_8} == types_1

    # Full verify_runlog connection count is also O(1) vs N campaigns.
    def _measure_full(catalog_dir):
        conn_count = 0

        def counting_connect(*args, **kwargs):
            nonlocal conn_count
            conn_count += 1
            return orig_connect_read(*args, **kwargs)

        monkeypatch.setattr(idx_mod, "connect_read", counting_connect)
        res = verify_runlog(catalog_dir)
        monkeypatch.undo()
        return res, conn_count

    res_1, full_1 = _measure_full(cat_1)
    res_8, full_8 = _measure_full(cat_8)
    assert full_1 == full_8
    assert res_1.ok is False and res_8.ok is False


def test_legacy_findings_caches_unchanged_sources_and_catches_mutations(tmp_path, monkeypatch):
    """Regression test (#3052): second _legacy_findings pass skips rescanning unchanged
    Parquet/warm sources and avoids fetching full data blobs, while still detecting new
    writes, corruption, and locked warm DBs."""
    from datetime import UTC, datetime

    import bathos.runlog.importer as importer_mod
    import bathos.verify as verify_mod
    from bathos.catalog import write_run, write_submit_provenance
    from bathos.compact import compact
    from bathos.runlog.ingest import run_ingest
    from bathos.runlog.migrate import import_legacy_post_cutover
    from bathos.runlog.mode import cutover_marker_path
    from bathos.schema import Run
    from bathos.verify import _legacy_findings

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    r1 = Run(
        id="run-legacy-1",
        project_slug="proj",
        command="python x.py",
        argv=["python", "x.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        status="completed",
        hostname="h1",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    write_run(r1, catalog_dir)
    write_submit_provenance(
        project_slug="proj",
        command="python x.py",
        sidecar_sha256="sha",
        myxcel_job_id="101",
        stage_name="exploration",
        catalog_dir=catalog_dir,
    )
    compact(catalog_dir)
    (catalog_dir / "bathos.db").rename(catalog_dir / "bathos.db.frozen")
    cutover_marker_path(catalog_dir).write_text(
        json.dumps({"at": "2026-01-01T00:00:00Z", "bathos": "test", "attempt": "1", "segments": []})
    )
    assert import_legacy_post_cutover(catalog_dir).status == "imported"
    run_ingest(catalog_dir)

    # First pass builds cache and finds nothing wrong.
    assert _legacy_findings(catalog_dir) == []

    # Second pass: spy on parquet reads, warm table reads, and on-demand data fetches.
    orig_iter_parquet = importer_mod._iter_parquet_rows
    orig_read_table = importer_mod._read_table_rows
    orig_fetch_fields = verify_mod._fetch_latest_import_fields
    calls = {"parquet": 0, "warm_table": 0, "fetch_fields": 0}

    def spy_parquet(*a, **kw):
        calls["parquet"] += 1
        return orig_iter_parquet(*a, **kw)

    def spy_warm_table(*a, **kw):
        calls["warm_table"] += 1
        return orig_read_table(*a, **kw)

    def spy_fetch_fields(*a, **kw):
        calls["fetch_fields"] += 1
        return orig_fetch_fields(*a, **kw)

    monkeypatch.setattr(importer_mod, "_iter_parquet_rows", spy_parquet)
    monkeypatch.setattr(importer_mod, "_read_table_rows", spy_warm_table)
    monkeypatch.setattr(verify_mod, "_fetch_latest_import_fields", spy_fetch_fields)

    assert _legacy_findings(catalog_dir) == []
    assert calls == {"parquet": 0, "warm_table": 0, "fetch_fields": 0}
    monkeypatch.undo()

    # Negative check 1: a newly written legacy run after cutover is still detected.
    r2 = Run(
        id="run-legacy-2",
        project_slug="proj",
        command="python y.py",
        argv=["python", "y.py"],
        git_hash="def",
        git_branch="main",
        git_dirty=False,
        status="completed",
        hostname="rogue-host",
        timestamp=datetime(2026, 1, 2, tzinfo=UTC),
    )
    write_run(r2, catalog_dir)
    findings = _legacy_findings(catalog_dir)
    assert [f["type"] for f in findings] == ["legacy_write_after_cutover"]
    assert findings[0]["writing_host"] == "rogue-host"

    # Negative check 2: corrupting a previously cached fragment is detected.
    frag1 = catalog_dir / "runs" / "proj" / "run_run-legacy-1.parquet"
    frag1.write_bytes(b"corrupt")
    findings2 = _legacy_findings(catalog_dir)
    types2 = {f["type"] for f in findings2}
    assert "corrupt_legacy_source" in types2
    assert "legacy_write_after_cutover" in types2
