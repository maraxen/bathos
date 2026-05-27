from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from bathos.telemetry import event


@dataclass
class ArchiveResult:
    """Result of an archive operation."""

    runs_archived: int
    partitions_created: int
    archive_size_bytes: int
    manifest_path: Path
    duration_s: float


def archive(
    catalog_dir: Path,
    archive_root: Path | None = None,
    project_slug: str | None = None,
    dry_run: bool = False,
) -> ArchiveResult:
    """Export warm-tier runs to cold-tier partitioned Parquet.

    Strategy: Partition by project_slug, year, month for efficient bulk queries.

    Args:
        catalog_dir: Path to catalog root
        archive_root: Root for archive (default ~/.bth/archive/)
        project_slug: Filter to specific project (default: all)
        dry_run: If True, show what would be archived without writing

    Returns:
        ArchiveResult with counts, size, manifest path, duration
    """
    start_time = time.time()

    if archive_root is None:
        archive_root = Path.home() / ".bth" / "archive"

    if not dry_run:
        archive_root.mkdir(parents=True, exist_ok=True)

    # Check for warm DB
    db_path = catalog_dir / "bathos.db"
    if not db_path.exists():
        raise RuntimeError("No warm catalog. Run `bth compact` first.")

    # Read runs from warm DB
    con = duckdb.connect(str(db_path), read_only=True)

    query = "SELECT * FROM runs ORDER BY id"
    params = []
    if project_slug:
        query = "SELECT * FROM runs WHERE project_slug = ? ORDER BY id"
        params = [project_slug]

    results = con.execute(query, params).to_arrow_table()
    con.close()

    if len(results) == 0:
        duration_s = time.time() - start_time
        return ArchiveResult(
            runs_archived=0,
            partitions_created=0,
            archive_size_bytes=0,
            manifest_path=archive_root / "manifest.json",
            duration_s=duration_s,
        )

    # Partition by project, year, month
    partitions: dict[tuple[str, int, str], list[int]] = {}

    timestamp_col = results.column("timestamp")
    project_col = results.column("project_slug")

    for i in range(len(results)):
        # Extract project and timestamp
        project = project_col[i].as_py()
        ts = timestamp_col[i].as_py()

        # Handle both int (microseconds) and datetime
        dt = datetime.fromtimestamp(ts / 1000000.0) if isinstance(ts, int) else ts

        year = dt.year
        month = f"{dt.month:02d}"

        key = (project, year, month)
        if key not in partitions:
            partitions[key] = []
        partitions[key].append(i)

    # Write partitions
    total_archived = 0
    partitions_created = 0
    total_size = 0
    manifest_entries = []

    for (project, year, month), indices in sorted(partitions.items()):
        partition_path = archive_root / f"project={project}" / f"year={year}" / f"month={month}"
        output_file = partition_path / "runs.parquet"

        partition_start_time = time.time()

        if not dry_run:
            partition_path.mkdir(parents=True, exist_ok=True)

            # Filter results to just these rows
            indices_array = pa.array(indices)
            mask = pa.compute.is_in(pa.array(list(range(len(results)))), indices_array)
            filtered = results.filter(mask)

            # Write atomically: write to temp, then rename
            temp_file = output_file.parent / f".{output_file.name}.tmp"
            pq.write_table(filtered, str(temp_file))
            temp_file.rename(output_file)

            file_size = output_file.stat().st_size
            total_size += file_size
        else:
            file_size = 0

        partition_duration_ms = (time.time() - partition_start_time) * 1000

        # Emit telemetry: archive.export per partition
        partition_key = f"project={project}/year={year}/month={month}"
        event(
            "archive.export",
            partition=partition_key,
            rows=len(indices),
            duration_ms=partition_duration_ms,
        )

        manifest_entries.append(
            {
                "partition": f"project={project}/year={year}/month={month}",
                "rows": len(indices),
                "size_bytes": file_size if not dry_run else 0,
            }
        )

        total_archived += len(indices)
        partitions_created += 1

    # Write manifest
    manifest_path = archive_root / "manifest.json"
    if not dry_run:
        manifest = {
            "timestamp": datetime.now(UTC).isoformat(),
            "runs_archived": total_archived,
            "partitions": partitions_created,
            "total_size_bytes": total_size,
            "entries": manifest_entries,
        }
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

    duration_s = time.time() - start_time

    return ArchiveResult(
        runs_archived=total_archived,
        partitions_created=partitions_created,
        archive_size_bytes=total_size,
        manifest_path=manifest_path,
        duration_s=duration_s,
    )
