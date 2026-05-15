from __future__ import annotations
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable
import time
import duckdb

from bathos.catalog import read_runs
from bathos.schema import Run


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


MIGRATIONS["0"] = _migrate_v0


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
    metadata TEXT,
    outcome TEXT
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
    """Apply schema migrations to a run if needed."""
    schema_version = run.schema_version or "0"
    if schema_version not in MIGRATIONS:
        return run

    # Convert to dict, apply migration, reconstruct
    run_dict = asdict(run)
    run_dict.update(MIGRATIONS[schema_version](run_dict))
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
    con.execute(
        "CREATE TABLE IF NOT EXISTS _schema_meta (key TEXT PRIMARY KEY, value TEXT)"
    )

    # Initialize runs table if it doesn't exist
    con.execute(_RUNS_TABLE_SCHEMA)

    # Track ingested and skipped counts
    ingested = 0
    skipped = 0

    # Ingest each run
    for run in cool_runs:
        # Check if run already exists in DuckDB
        existing = con.execute(
            "SELECT id FROM runs WHERE id = ?", [run.id]
        ).fetchall()

        if existing:
            skipped += 1
            continue

        # Apply migrations if needed
        run = _apply_migrations(run)

        # Insert into DuckDB
        con.execute(
            """
            INSERT INTO runs (
                id, project_slug, command, argv, git_hash, git_branch,
                git_dirty, timestamp, duration_s, exit_code, status,
                output_paths, tags, schema_version, slurm_job_id, metadata, outcome
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                run.metadata,
                None,  # outcome is not set during compact
            ],
        )

        ingested += 1

    # Update schema_meta table
    con.execute(
        "INSERT OR REPLACE INTO _schema_meta (key, value) VALUES (?, ?)",
        ["warm_version", "1"],
    )

    # Commit and close
    con.close()

    duration_s = time.time() - start_time

    return CompactResult(
        ingested=ingested,
        skipped=skipped,
        duration_s=duration_s,
    )
