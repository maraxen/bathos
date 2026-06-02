from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from bathos.catalog import read_runs
from bathos.schema import CURRENT_SCHEMA_VERSION, Run
from bathos.telemetry import event

logger = logging.getLogger(__name__)


class CorruptDatabaseError(RuntimeError):
    """Raised when bathos.db cannot be opened or fails a post-connect check.

    Attributes:
        db_path: Path to the database file that failed the check.
    """

    def __init__(self, message: str, db_path: Path | None = None) -> None:
        super().__init__(message)
        self.db_path = db_path


class CompactionLockedError(RuntimeError):
    """Raised when compact() cannot acquire the advisory lock."""

    pass


def _collect_output_metadata(output_path: str) -> dict:
    """Collect metadata about an output file.

    Returns dict with:
    - status: "present", "missing", or "unreadable"
    - size_bytes: file size (0 if missing/unreadable)
    - mtime_unix: modification time (Unix timestamp)
    - sha256: file hash (None if >100MB or unreadable)
    """
    path = Path(output_path)

    try:
        if not path.exists():
            return {"status": "missing", "size_bytes": 0}

        stat = path.stat()
        size_bytes = stat.st_size
        mtime_unix = stat.st_mtime

        # Skip SHA256 for large files (>100MB)
        sha256_hash = None
        if size_bytes < 100 * 1024 * 1024:
            try:
                h = hashlib.sha256()
                with open(path, "rb") as f:
                    while chunk := f.read(8192):
                        h.update(chunk)
                sha256_hash = h.hexdigest()
            except Exception:
                sha256_hash = None

        return {
            "status": "present",
            "size_bytes": size_bytes,
            "mtime_unix": mtime_unix,
            "sha256": sha256_hash,
        }
    except (PermissionError, OSError):
        return {"status": "unreadable", "size_bytes": 0}


@dataclass
class CompactResult:
    """Result of a compact operation."""

    ingested: int
    skipped: int
    duration_s: float


# Migration registry: transforms Run objects from older schema versions to current
MIGRATIONS: dict[str, Callable[[dict], dict]] = {}


def _migrate_v0(run_dict: dict) -> dict:
    """Migrate v0 fragment (missing schema_version) to v1."""
    # v0 runs have no schema_version; set to "1" for compatibility
    run_dict["schema_version"] = "1"
    return run_dict


def _migrate_v1(run_dict: dict) -> dict:
    """Migrate v1 fragment (no hostname) to v2.

    v1 fragments have schema_version='1' but no hostname field.
    This migration adds hostname (defaults to "") and updates version.
    """
    run_dict["hostname"] = ""
    run_dict["schema_version"] = "2"  # hardcoded: v1→v2 always
    return run_dict


def _migrate_v2(run_dict: dict) -> dict:
    """Migrate v2 fragment (no agentic integrity fields) to v3."""
    run_dict["sidecar_sha256"] = ""
    run_dict["sidecar_path"] = ""
    run_dict["parent_run_id"] = ""
    run_dict["agent_mode"] = ""
    run_dict["sidecar_mode"] = ""
    run_dict["outcome_is_residual"] = False
    run_dict["skill_sha256"] = ""
    run_dict["campaign_id"] = ""
    run_dict["schema_version"] = "3"
    return run_dict


def _migrate_v3(run_dict: dict) -> dict:
    """Migrate v3 fragment to v4 by adding postmortem fields."""
    run_dict["script_sha256"] = ""
    run_dict["postmortem_status"] = "unassigned"
    run_dict["postmortem_override"] = "none"
    run_dict["postmortem_verdict_override"] = "none"
    run_dict["postmortem_author"] = ""
    run_dict["postmortem_path"] = ""
    run_dict["postmortem_hypothesis_status"] = "unassigned"
    run_dict["postmortem_has_anomalies"] = False
    run_dict["postmortem_summary"] = ""
    run_dict["postmortem_asset_links"] = "{}"
    run_dict["schema_version"] = "4"
    return run_dict


def _migrate_v4(run_dict: dict) -> dict:
    """Migrate v4 fragment to v5 by adding manifest and outcome_error fields."""
    run_dict["manifest_sha256"] = ""
    run_dict["manifest_path"] = ""
    run_dict["outcome_error_reason"] = ""
    run_dict["adversarial_check_status"] = ""
    run_dict["schema_version"] = "5"
    return run_dict


def _migrate_v5(run_dict: dict) -> dict:
    """Migrate v5 fragment to v6 — no new run-level fields; version stamp only."""
    run_dict["schema_version"] = "6"
    return run_dict


MIGRATIONS["0"] = _migrate_v0
MIGRATIONS["1"] = _migrate_v1
MIGRATIONS["2"] = _migrate_v2
MIGRATIONS["3"] = _migrate_v3
MIGRATIONS["4"] = _migrate_v4
MIGRATIONS["5"] = _migrate_v5


_RUNS_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    project_slug TEXT,
    command TEXT,
    argv TEXT[],
    git_hash TEXT,
    git_branch TEXT,
    git_dirty BOOLEAN,
    timestamp TIMESTAMP WITH TIME ZONE,
    duration_s DOUBLE,
    exit_code INTEGER,
    status TEXT,
    output_paths TEXT[],
    tags TEXT[],
    schema_version TEXT,
    slurm_job_id TEXT,
    hostname TEXT,
    metadata TEXT,
    outcome TEXT,
    output_metadata TEXT,
    sidecar_sha256 TEXT,
    sidecar_path TEXT,
    parent_run_id TEXT,
    agent_mode TEXT,
    sidecar_mode TEXT,
    outcome_is_residual BOOLEAN,
    skill_sha256 TEXT,
    campaign_id TEXT,
    script_sha256 TEXT,
    postmortem_status TEXT,
    postmortem_override TEXT,
    postmortem_verdict_override TEXT,
    postmortem_author TEXT,
    postmortem_path TEXT,
    postmortem_hypothesis_status TEXT,
    postmortem_has_anomalies BOOLEAN,
    postmortem_summary TEXT,
    postmortem_asset_links TEXT,
    manifest_sha256 TEXT,
    manifest_path TEXT,
    outcome_error_reason TEXT,
    adversarial_check_status TEXT
)
"""

_CAMPAIGNS_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    project_slug TEXT NOT NULL,
    name TEXT NOT NULL,
    mode TEXT NOT NULL,
    question TEXT,
    hypothesis TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    concluded_at TEXT,
    conclusion TEXT,
    outcome_label TEXT,
    parent_campaign_id TEXT,
    stopping_threshold REAL
)
"""

_CAMPAIGN_RUNS_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS campaign_runs (
    campaign_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    evalue REAL CHECK (evalue IS NULL OR evalue > 0),
    seq_position INTEGER,
    PRIMARY KEY (campaign_id, run_id)
)
"""

_AMENDMENTS_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS amendments (
    run_id TEXT NOT NULL,
    amended_at TEXT NOT NULL,
    old_sidecar_sha256 TEXT,
    new_sidecar_sha256 TEXT,
    reason TEXT NOT NULL
)
"""


COMPACTION_THRESHOLD = 50  # Public constant for compaction trigger threshold


def _fragment_count(catalog_dir: Path) -> int:
    """Count cool fragments in catalog directory."""
    runs_dir = catalog_dir / "runs"
    if not runs_dir.exists():
        return 0
    return len(list(runs_dir.rglob("run_*.parquet")))


def should_compact(catalog_dir: Path) -> bool:
    """Return True if fragment count > COMPACTION_THRESHOLD and warm DB missing.

    This indicates the user should run `bth compact` to improve query performance.

    Args:
        catalog_dir: Path to catalog root

    Returns:
        True if compaction is recommended, False otherwise
    """
    if (catalog_dir / "bathos.db").exists():
        return False
    return _fragment_count(catalog_dir) > COMPACTION_THRESHOLD


def _apply_migrations(run: Run) -> Run:
    """Apply schema migrations to a run, chaining through all versions.

    Migrations are applied sequentially: v0→v1→v2→...
    Each migration updates the schema_version field, which determines
    the next migration to apply.
    """
    run_dict = asdict(run)

    # Default v0 fragments (pre-schema_version) to "0"
    current_version = run_dict.get("schema_version") or "0"

    # Chain migrations until we reach the latest version
    while current_version in MIGRATIONS:
        run_dict = MIGRATIONS[current_version](run_dict)
        current_version = run_dict.get("schema_version")

    return Run(**run_dict)


def _open_db(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Open bathos.db, detecting corruption at connect and post-connect.

    Raises:
        CorruptDatabaseError: If the file header is unreadable (IOException at
            connect time) or if _schema_meta is inaccessible after a successful
            connect (structural corruption).
    """
    try:
        con = duckdb.connect(str(db_path))
    except duckdb.IOException as exc:
        raise CorruptDatabaseError(
            f"DuckDB could not open {db_path}: {exc}",
            db_path=db_path,
        ) from exc

    # Post-connect structural check: _schema_meta must be accessible in any
    # valid bathos.db that has been through at least one compact().
    if db_path.exists() and db_path.stat().st_size > 0:
        try:
            con.execute("SELECT COUNT(*) FROM _schema_meta").fetchone()
        except Exception as exc:
            con.close()
            raise CorruptDatabaseError(
                f"DuckDB opened {db_path} but _schema_meta is inaccessible: {exc}",
                db_path=db_path,
            ) from exc

    return con


def compact(catalog_dir: Path, force_rebuild: bool = False) -> CompactResult:
    """Ingest all cool fragments into bathos.db DuckDB database.

    - Snapshots file list at start (ignores fragments written after snapshot)
    - Inserts new runs into DuckDB `runs` table; skips any runs already present (keyed on `id`)
    - Tracks warm-tier schema version in `_schema_meta` table
    - Does NOT remove cool fragments after ingest (safe default)

    Args:
        catalog_dir: Path to catalog root (contains runs/ and bathos.db target)
        force_rebuild: If True, remove existing bathos.db before compacting (for recovery from corruption)

    Returns:
        CompactResult with ingested count, skipped count, and duration
    """
    start_time = time.time()

    # Count cool files at start for telemetry
    cool_files = _fragment_count(catalog_dir)

    # Read all runs from cool fragments (read_runs snapshots file list internally)
    cool_runs = read_runs(catalog_dir)

    # Parse all postmortems in workspace
    from bathos.config import find_project_config, load_project_config
    from bathos.postmortem import parse_postmortem

    postmortem_map = {}
    config_path = find_project_config(Path.cwd())
    if config_path:
        project_config = load_project_config(config_path)
        workspace_root = project_config.root
    else:
        workspace_root = Path.cwd()

    if workspace_root.exists():
        for pm_file in workspace_root.rglob("*.bth.postmortem.toml"):
            try:
                pm = parse_postmortem(pm_file)
                if pm.status != "draft":
                    rel_path = str(pm_file.relative_to(workspace_root))
                    postmortem_map[pm.run_id] = (pm, rel_path)
            except Exception as e:
                logger.warning(f"Skipping postmortem parse: {pm_file}: {e}")

    # Get warm-tier row count before compaction (if DB exists)
    warm_rows_before = 0
    db_path = catalog_dir / "bathos.db"
    if db_path.exists() and force_rebuild:
        db_path.unlink()
    if db_path.exists():
        temp_con = duckdb.connect(str(db_path), read_only=True)
        try:
            warm_rows_before = temp_con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        except Exception:
            warm_rows_before = 0
        finally:
            temp_con.close()

    # Emit compact start event
    event("catalog.compact_start", cool_files=cool_files, warm_rows_before=warm_rows_before)

    # Open DuckDB connection (time the connect call to detect lock waits)
    t_connect = time.monotonic()
    con = duckdb.connect(str(db_path))
    waited_ms = (time.monotonic() - t_connect) * 1000
    if waited_ms > 500:
        event("catalog.duckdb_lock_wait", waited_ms=int(waited_ms), db_path=str(db_path))

    # Initialize schema meta table if it doesn't exist
    con.execute("CREATE TABLE IF NOT EXISTS _schema_meta (key TEXT PRIMARY KEY, value TEXT)")

    # Initialize schema migrations audit table if it doesn't exist
    con.execute("""
        CREATE TABLE IF NOT EXISTS _schema_migrations (
            warm_version TEXT NOT NULL,
            migrated_at TIMESTAMPTZ DEFAULT now(),
            notes TEXT
        )
    """)

    # Initialize runs table if it doesn't exist
    con.execute(_RUNS_TABLE_SCHEMA)

    # Initialize campaign tables if they don't exist
    con.execute(_CAMPAIGNS_TABLE_SCHEMA)
    con.execute(_CAMPAIGN_RUNS_TABLE_SCHEMA)
    con.execute(_AMENDMENTS_TABLE_SCHEMA)

    # Idempotent column additions for POPPER (handles existing warm DBs)
    for _alter_sql in [
        "ALTER TABLE campaign_runs ADD COLUMN IF NOT EXISTS evalue REAL",
        "ALTER TABLE campaign_runs ADD COLUMN IF NOT EXISTS seq_position INTEGER",
        "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS stopping_threshold REAL",
    ]:
        try:
            con.execute(_alter_sql)
        except Exception:
            pass

    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_campaigns_mode_status ON campaigns (mode, status)"
    )

    # Track ingested and skipped counts
    ingested = 0
    skipped = 0

    # Ingest each run
    for run in cool_runs:
        # Check if run already exists in DuckDB
        existing = con.execute("SELECT id FROM runs WHERE id = ?", [run.id]).fetchall()

        if existing:
            skipped += 1
            if run.id in postmortem_map:
                pm, rel_path = postmortem_map[run.id]
                postmortem_verdict_override = pm.verdict_override
                postmortem_has_anomalies = any(v and str(v).lower() != "none" for v in getattr(pm, "anomalies", {}).values())
                
                curr_outcome = con.execute("SELECT outcome FROM runs WHERE id = ?", [run.id]).fetchone()[0] or ""
                outcome = postmortem_verdict_override if postmortem_verdict_override != "none" else curr_outcome
                
                con.execute(
                    """
                    UPDATE runs SET
                        outcome = ?,
                        postmortem_status = ?,
                        postmortem_override = ?,
                        postmortem_verdict_override = ?,
                        postmortem_author = ?,
                        postmortem_path = ?,
                        postmortem_hypothesis_status = ?,
                        postmortem_has_anomalies = ?,
                        postmortem_summary = ?,
                        postmortem_asset_links = ?
                    WHERE id = ?
                    """,
                    [
                        outcome,
                        pm.status,
                        pm.verdict_override,
                        pm.verdict_override,
                        pm.author,
                        rel_path,
                        pm.hypothesis_status,
                        postmortem_has_anomalies,
                        pm.summary,
                        json.dumps(pm.asset_links),
                        run.id
                    ]
                )
            continue

        # Apply migrations if needed
        run = _apply_migrations(run)

        # Apply postmortem updates to run object if present
        if run.id in postmortem_map:
            pm, rel_path = postmortem_map[run.id]
            run.postmortem_status = pm.status
            run.postmortem_override = pm.verdict_override
            run.postmortem_verdict_override = pm.verdict_override
            run.postmortem_author = pm.author
            run.postmortem_path = rel_path
            run.postmortem_hypothesis_status = pm.hypothesis_status
            run.postmortem_has_anomalies = any(v and str(v).lower() != "none" for v in getattr(pm, "anomalies", {}).values())
            run.postmortem_summary = pm.summary
            run.postmortem_asset_links = json.dumps(pm.asset_links)
            if pm.verdict_override != "none":
                run.outcome = pm.verdict_override

        # Collect output metadata
        output_metadata = []
        if run.output_paths:
            for output_path in run.output_paths:
                meta = _collect_output_metadata(output_path)
                output_metadata.append({"path": output_path, **meta})

        # Serialize metadata to JSON
        output_metadata_json = json.dumps(output_metadata) if output_metadata else "[]"

        # Insert into DuckDB
        con.execute(
            """
            INSERT INTO runs (
                id, project_slug, command, argv, git_hash, git_branch,
                git_dirty, timestamp, duration_s, exit_code, status,
                output_paths, tags, schema_version, slurm_job_id, hostname, metadata, outcome, output_metadata,
                sidecar_sha256, sidecar_path, parent_run_id, agent_mode, sidecar_mode, outcome_is_residual, skill_sha256, campaign_id,
                script_sha256, postmortem_status, postmortem_override, postmortem_verdict_override, postmortem_author, postmortem_path,
                postmortem_hypothesis_status, postmortem_has_anomalies, postmortem_summary, postmortem_asset_links
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run.id,
                run.project_slug,
                run.command,
                run.argv,
                run.git_hash,
                run.git_branch,
                run.git_dirty,
                run.timestamp,
                run.duration_s,
                run.exit_code,
                run.status,
                run.output_paths,
                run.tags,
                run.schema_version,
                run.slurm_job_id,
                run.hostname,
                run.metadata,
                run.outcome or None,  # preserve evaluated outcome label from cool fragment
                output_metadata_json,
                run.sidecar_sha256,
                run.sidecar_path,
                run.parent_run_id,
                run.agent_mode,
                run.sidecar_mode,
                run.outcome_is_residual,
                run.skill_sha256,
                run.campaign_id,
                run.script_sha256,
                run.postmortem_status,
                run.postmortem_override,
                run.postmortem_verdict_override,
                run.postmortem_author,
                run.postmortem_path,
                run.postmortem_hypothesis_status,
                run.postmortem_has_anomalies,
                run.postmortem_summary,
                run.postmortem_asset_links,
            ],
        )

        ingested += 1

    # Populate campaign_runs from runs with campaign_id set
    for run in cool_runs:
        if run.campaign_id:
            con.execute("""
                INSERT INTO campaign_runs (campaign_id, run_id)
                VALUES (?, ?)
                ON CONFLICT DO NOTHING
            """, [run.campaign_id, run.id])

    # Update schema_meta table
    con.execute(
        "INSERT OR REPLACE INTO _schema_meta (key, value) VALUES (?, ?)",
        ["warm_version", CURRENT_SCHEMA_VERSION],
    )

    # Record migration in audit table
    con.execute(
        "INSERT INTO _schema_migrations (warm_version, notes) VALUES (?, ?)",
        [CURRENT_SCHEMA_VERSION, "compact"],
    )

    # Commit and close
    con.close()

    duration_s = time.time() - start_time
    duration_ms = duration_s * 1000

    # Get final warm-tier row count for telemetry
    warm_rows_after = 0
    try:
        temp_con = duckdb.connect(str(db_path), read_only=True)
        warm_rows_after = temp_con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        temp_con.close()
    except Exception:
        warm_rows_after = 0

    # Emit compact end event
    event(
        "catalog.compact_end",
        cool_files=cool_files,
        warm_rows_before=warm_rows_before,
        warm_rows_after=warm_rows_after,
        duration_ms=int(duration_ms),
    )

    return CompactResult(
        ingested=ingested,
        skipped=skipped,
        duration_s=duration_s,
    )
