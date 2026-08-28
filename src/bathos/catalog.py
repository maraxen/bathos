from __future__ import annotations

import importlib.metadata
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from bathos.schema import Run
from bathos.telemetry import event

logger = logging.getLogger(__name__)


@dataclass
class CorruptFragment:
    """A cool-tier Parquet fragment that could not be read.

    Attributes:
        path: Path to the unreadable fragment.
        error: str(exception) captured while trying to read it.
    """

    path: Path
    error: str


def init_catalog(catalog_dir: Path) -> None:
    (catalog_dir / "runs").mkdir(parents=True, exist_ok=True)


def write_run(run: Run, catalog_dir: Path) -> None:
    """Write (or overwrite) a run record atomically."""
    runs_dir = catalog_dir / "runs" / run.project_slug
    runs_dir.mkdir(parents=True, exist_ok=True)
    target = runs_dir / f"run_{run.id}.parquet"
    tmp = runs_dir / f"run_{run.id}.tmp.parquet"

    t_start = time.monotonic()
    pq.write_table(run.to_arrow(), tmp)
    tmp.rename(target)  # atomic on POSIX
    duration_ms = (time.monotonic() - t_start) * 1000

    # Emit telemetry event
    event("catalog.write_parquet", path=str(target), rows=1, duration_ms=int(duration_ms))


def read_runs(catalog_dir: Path) -> list[Run]:
    """Read all runs from Parquet fragments, sorted by timestamp DESC.

    Corrupt or unreadable fragments (e.g. a cool-tier Parquet file truncated by
    a killed SLURM job) are skipped rather than aborting the whole read -- see
    read_runs_report() for a variant that also returns what was skipped and
    why, for operator-facing reporting (bth compact, bth migrate --dry-run).
    A skip is always logged at WARNING, so it is never silent even for the 14+
    call sites that only want the list[Run].
    """
    runs, _ = read_runs_report(catalog_dir)
    return runs


def read_runs_report(catalog_dir: Path) -> tuple[list[Run], list[CorruptFragment]]:
    """Read all runs from Parquet fragments, sorted by timestamp DESC.

    Returns (runs, corrupt_fragments). One corrupt fragment in one project must
    never abort catalog maintenance for every project (260827_bathos-catalog-
    robustness): each fragment is read individually and a read failure is
    recorded and skipped instead of propagating out of this function.
    """
    runs_dir = catalog_dir / "runs"
    if not runs_dir.exists():
        return [], []
    parquet_files = sorted(runs_dir.rglob("run_*.parquet"))
    if not parquet_files:
        return [], []

    tables: list[pa.Table] = []
    corrupt: list[CorruptFragment] = []
    for f in parquet_files:
        try:
            tables.append(pq.read_table(f))
        except Exception as exc:
            logger.warning("Skipping corrupt cool-tier fragment %s: %s", f, exc)
            corrupt.append(CorruptFragment(path=f, error=str(exc)))

    if corrupt:
        event(
            "catalog.corrupt_fragments_skipped",
            count=len(corrupt),
            paths=[str(c.path) for c in corrupt],
        )

    if not tables:
        return [], corrupt

    combined = pa.concat_tables(tables, promote_options="permissive")
    order = pc.sort_indices(combined, sort_keys=[("timestamp", "descending")])
    combined = combined.take(order)
    pydict = combined.to_pydict()
    runs = [Run.from_arrow_row(pydict, i) for i in range(combined.num_rows)]
    return runs, corrupt


def write_submit_provenance(
    project_slug: str,
    command: str,
    sidecar_sha256: str,
    myxcel_job_id: str,
    stage_name: str,
    catalog_dir: Path,
) -> None:
    """Write submit-provenance record atomically.

    Args:
        project_slug: Project slug (e.g., 'myproject').
        command: Script path token (e.g., 'scripts/experiments/foo.py').
        sidecar_sha256: Hash of located sidecar file (or empty string if none).
        myxcel_job_id: SLURM job ID returned by myxcel submit.
        stage_name: Stage name from sidecar [experiment].stage_name (default 'exploration').
        catalog_dir: Catalog directory path.

    Writes atomically to ~/.bth/catalog/submits/<project_slug>/<timestamp>_submit.parquet.
    """
    submit_schema = pa.schema(
        [
            pa.field("project_slug", pa.string()),
            pa.field("command", pa.string()),
            pa.field("sidecar_sha256", pa.string()),
            pa.field("bth_submit_version", pa.string()),
            pa.field("submitted_at", pa.timestamp("us", tz="UTC")),
            pa.field("myxcel_job_id", pa.string()),
            pa.field("stage_name", pa.string()),
        ]
    )

    submit_dir = catalog_dir / "submits" / project_slug
    submit_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(UTC)
    ts_str = ts.strftime("%Y%m%dT%H%M%S%f")  # Include microseconds for uniqueness
    tmp_path = submit_dir / f"{ts_str}_submit.parquet.tmp"
    final_path = submit_dir / f"{ts_str}_submit.parquet"

    t_start = time.monotonic()
    table = pa.table(
        {
            "project_slug": [project_slug],
            "command": [command],
            "sidecar_sha256": [sidecar_sha256 or ""],
            "bth_submit_version": [importlib.metadata.version("bathos")],
            "submitted_at": pa.array([ts], type=pa.timestamp("us", tz="UTC")),
            "myxcel_job_id": [myxcel_job_id or ""],
            "stage_name": [stage_name or "exploration"],
        },
        schema=submit_schema,
    )
    pq.write_table(table, str(tmp_path))
    tmp_path.rename(final_path)  # atomic on POSIX
    duration_ms = (time.monotonic() - t_start) * 1000

    # Emit telemetry event
    event(
        "catalog.write_submit_provenance",
        path=str(final_path),
        rows=1,
        duration_ms=int(duration_ms),
    )
