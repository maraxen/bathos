from __future__ import annotations
from pathlib import Path
import pyarrow.parquet as pq
import duckdb

from bathos.schema import Run, RUN_SCHEMA


def init_catalog(catalog_dir: Path) -> None:
    (catalog_dir / "runs").mkdir(parents=True, exist_ok=True)


def write_run(run: Run, catalog_dir: Path) -> None:
    """Write (or overwrite) a run record atomically."""
    runs_dir = catalog_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    target = runs_dir / f"run_{run.id}.parquet"
    tmp = runs_dir / f"run_{run.id}.tmp.parquet"
    pq.write_table(run.to_arrow(), tmp)
    tmp.rename(target)  # atomic on POSIX


def read_runs(catalog_dir: Path) -> list[Run]:
    """Read all runs, deduplicating by id (latest write wins)."""
    runs_dir = catalog_dir / "runs"
    if not runs_dir.exists():
        return []
    parquet_files = list(runs_dir.glob("run_*.parquet"))
    if not parquet_files:
        return []
    # One file per run id (write_run overwrites in-place), no dedup needed.
    # duckdb reads multiple files efficiently via glob.
    glob_pattern = str(runs_dir / "run_*.parquet")
    con = duckdb.connect()
    result = con.execute(
        f"SELECT * FROM read_parquet('{glob_pattern}') ORDER BY timestamp DESC"
    ).fetchall()
    columns = [desc[0] for desc in con.description]
    pydict = {col: [row[i] for row in result] for i, col in enumerate(columns)}
    return [Run.from_arrow_row(pydict, i) for i in range(len(result))]
