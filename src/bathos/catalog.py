from __future__ import annotations

import time
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from bathos.schema import Run
from bathos.telemetry import event


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
    target_size = target.stat().st_size
    event("catalog.write_parquet", path=str(target), rows=1, duration_ms=int(duration_ms))


def read_runs(catalog_dir: Path) -> list[Run]:
    """Read all runs from Parquet fragments, sorted by timestamp DESC."""
    runs_dir = catalog_dir / "runs"
    if not runs_dir.exists():
        return []
    parquet_files = list(runs_dir.rglob("run_*.parquet"))
    if not parquet_files:
        return []
    tables = [pq.read_table(f) for f in parquet_files]
    combined = pa.concat_tables(tables, promote_options="permissive")
    order = pc.sort_indices(combined, sort_keys=[("timestamp", "descending")])
    combined = combined.take(order)
    pydict = combined.to_pydict()
    return [Run.from_arrow_row(pydict, i) for i in range(combined.num_rows)]
