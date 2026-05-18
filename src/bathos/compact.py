from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb

from bathos.catalog import read_runs
from bathos.schema import Run


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
    run_dict["schema_version"] = "2"
    return run_dict


MIGRATIONS["0"] = _migrate_v0
MIGRATIONS["1"] = _migrate_v1


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
    output_metadata TEXT
)
"""


COMPACTION_THRESHOLD = 50  # Public constant for compaction trigger threshold


def _fragment_count(catalog_dir: Path) -> int:
    """Count cool fragments in catalog directory."""
    runs_dir = catalog_dir / "runs"
    if not runs_dir.exists():
        return 0
    return len(list(runs_dir.glob("run_*.parquet")))


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


def compact(catalog_dir: Path) -> CompactResult:
    """Ingest all cool fragments into bathos.db DuckDB database.

    - Snapshots file list at start (ignores fragments written after snapshot)
    - Inserts new runs into DuckDB `runs` table; skips any runs already present (keyed on `id`)
    - Tracks warm-tier schema version in `_schema_meta` table
    - Does NOT remove cool fragments after ingest (safe default)

    Args:
        catalog_dir: Path to catalog root (contains runs/ and bathos.db target)

    Returns:
        CompactResult with ingested count, skipped count, and duration
    """
    start_time = time.time()

    # Read all runs from cool fragments (read_runs snapshots file list internally)
    cool_runs = read_runs(catalog_dir)

    # Open DuckDB connection
    db_path = catalog_dir / "bathos.db"
    con = duckdb.connect(str(db_path))

    # Initialize schema meta table if it doesn't exist
    con.execute("CREATE TABLE IF NOT EXISTS _schema_meta (key TEXT PRIMARY KEY, value TEXT)")

    # Initialize runs table if it doesn't exist
    con.execute(_RUNS_TABLE_SCHEMA)

    # Track ingested and skipped counts
    ingested = 0
    skipped = 0

    # Ingest each run
    for run in cool_runs:
        # Check if run already exists in DuckDB
        existing = con.execute("SELECT id FROM runs WHERE id = ?", [run.id]).fetchall()

        if existing:
            skipped += 1
            continue

        # Apply migrations if needed
        run = _apply_migrations(run)

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
                output_paths, tags, schema_version, slurm_job_id, hostname, metadata, outcome, output_metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                None,  # outcome is not set during compact
                output_metadata_json,
            ],
        )

        ingested += 1

    # Update schema_meta table
    con.execute(
        "INSERT OR REPLACE INTO _schema_meta (key, value) VALUES (?, ?)",
        ["warm_version", "2"],
    )

    # Commit and close
    con.close()

    duration_s = time.time() - start_time

    return CompactResult(
        ingested=ingested,
        skipped=skipped,
        duration_s=duration_s,
    )
