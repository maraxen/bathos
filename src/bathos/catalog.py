from __future__ import annotations

import importlib.metadata
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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


def iter_run_fragments(runs_dir: Path, *, recursive: bool = True) -> list[Path]:
    """Sorted `run_*.parquet` fragments under `runs_dir`, excluding in-flight temps.

    `run_<id>.tmp.parquet` is a write-then-rename temp (or a stale remote pull) that
    also matches the `run_*.parquet` glob; counting or reading one reports a run that
    does not exist yet. Every cool-tier scan goes through here (debt #2814).
    """
    pattern = runs_dir.rglob if recursive else runs_dir.glob
    return sorted(f for f in pattern("run_*.parquet") if not f.name.endswith(".tmp.parquet"))


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
    parquet_files = iter_run_fragments(runs_dir)
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


SUBMIT_SCHEMA = pa.schema(
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


@dataclass
class SubmitCompactionResult:
    """Summary of a submit-provenance prune/compaction pass."""

    projects_processed: int = 0
    files_before: int = 0
    files_after: int = 0
    rows_kept: int = 0
    rows_pruned: int = 0
    corrupt_skipped: int = 0
    dry_run: bool = False


def write_submit_provenance(
    project_slug: str,
    command: str,
    sidecar_sha256: str,
    myxcel_job_id: str,
    stage_name: str,
    catalog_dir: Path,
    *,
    submit_id: str | None = None,
    cwd: Path | None = None,
) -> None:
    """Write submit-provenance record atomically.

    Args:
        project_slug: Project slug (e.g., 'myproject').
        command: Script path token (e.g., 'scripts/experiments/foo.py').
        sidecar_sha256: Hash of located sidecar file (or empty string if none).
        myxcel_job_id: SLURM job ID returned by myxcel submit.
        stage_name: Stage name from sidecar [experiment].stage_name (default 'exploration').
        catalog_dir: Catalog directory path.
        submit_id: Entity key for the `submit.recorded` event (delivery step 2b,
            AC-25). No legacy caller threads one through, so a fresh uuid4 is
            minted when omitted -- this is a NEW identifier, not derived from
            anything legacy code already tracks.
        cwd: Working directory used to resolve the runlog project/root when the
            flag is on. Defaults to `Path.cwd()` (via `resolve_log_root`).

    Writes atomically to ~/.bth/catalog/submits/<project_slug>/<timestamp>_submit.parquet.
    Delivery step 2b (AC-25): behind the runlog flag, emits `submit.recorded`
    instead of writing the Parquet fragment; flag off performs only this
    legacy write, unchanged.
    """
    from bathos.runlog.emit import emit_or_legacy, unit_of_work

    with unit_of_work(catalog_dir):

        def _legacy_write() -> None:
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
                schema=SUBMIT_SCHEMA,
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

        emit_or_legacy(
            kind="submit.recorded",
            entity=[submit_id or str(uuid.uuid4())],
            data={
                "project_slug": project_slug,
                "command": command,
                "sidecar_sha256": sidecar_sha256 or "",
                "bth_submit_version": importlib.metadata.version("bathos"),
                "myxcel_job_id": myxcel_job_id or "",
                # AC-25 site description: "includes slurm_job_id" -- myxcel submit's
                # job id IS the SLURM job id for this cluster backend.
                "slurm_job_id": myxcel_job_id or "",
                "stage_name": stage_name or "exploration",
            },
            legacy_write=_legacy_write,
            cwd=cwd,
        )


def _iter_submit_fragments(proj_dir: Path) -> list[Path]:
    return sorted(
        f
        for f in proj_dir.glob("**/*.parquet")
        if not f.name.endswith(".tmp") and not f.name.endswith(".tmp.parquet")
    )


def compact_submit_provenance(
    catalog_dir: Path,
    *,
    project_slug: str | None = None,
    max_age_days: int | None = None,
    dry_run: bool = False,
) -> SubmitCompactionResult:
    """Compact and optionally prune per-submit Parquet fragments under `submits/<slug>/`.

    Merges many single-row `*_submit.parquet` files per project into one
    `<ts>_compacted_submit.parquet` file written atomically, and optionally
    prunes records older than `max_age_days` (based on `submitted_at`).
    Corrupt fragments are logged and left untouched.
    """
    if max_age_days is not None and max_age_days < 0:
        raise ValueError("max_age_days must be >= 0")

    result = SubmitCompactionResult(dry_run=dry_run)
    submits_dir = catalog_dir / "submits"
    if not submits_dir.is_dir():
        return result

    if project_slug is not None:
        candidate = submits_dir / project_slug
        proj_dirs = [candidate] if candidate.is_dir() else []
    else:
        proj_dirs = sorted(d for d in submits_dir.iterdir() if d.is_dir())

    cutoff = datetime.now(UTC) - timedelta(days=max_age_days) if max_age_days is not None else None

    for proj_dir in proj_dirs:
        files = _iter_submit_fragments(proj_dir)
        if not files:
            continue
        result.projects_processed += 1
        result.files_before += len(files)

        tables: list[pa.Table] = []
        readable_files: list[Path] = []
        for f in files:
            try:
                tables.append(pq.read_table(str(f)))
                readable_files.append(f)
            except Exception as exc:
                logger.warning("Skipping corrupt submit fragment %s: %s", f, exc)
                result.corrupt_skipped += 1

        if not tables:
            result.files_after += len(files)
            continue

        combined = pa.concat_tables(tables, promote_options="permissive")
        kept = combined
        pruned_here = 0
        if cutoff is not None and "submitted_at" in combined.column_names and combined.num_rows > 0:
            ts_col = combined.column("submitted_at")
            if pa.types.is_timestamp(ts_col.type):
                ts_utc = pc.cast(ts_col, pa.timestamp("us", tz="UTC"))
                cutoff_scalar = pa.scalar(cutoff, type=pa.timestamp("us", tz="UTC"))
                mask = pc.fill_null(pc.greater_equal(ts_utc, cutoff_scalar), True)
                kept = combined.filter(mask)
                pruned_here = combined.num_rows - kept.num_rows

        result.rows_kept += kept.num_rows
        result.rows_pruned += pruned_here

        # Already compacted (1 readable file) and nothing pruned: leave untouched.
        if len(readable_files) == 1 and pruned_here == 0:
            result.files_after += len(files)
            continue

        if kept.num_rows > 0 and "submitted_at" in kept.column_names:
            order = pc.sort_indices(kept, sort_keys=[("submitted_at", "ascending")])
            kept = kept.take(order)

        corrupt_count = len(files) - len(readable_files)
        if dry_run:
            result.files_after += corrupt_count + (1 if kept.num_rows > 0 else 0)
            continue

        new_path: Path | None = None
        if kept.num_rows > 0:
            ts_str = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            tmp_path = proj_dir / f"{ts_str}_compacted_submit.parquet.tmp"
            new_path = proj_dir / f"{ts_str}_compacted_submit.parquet"
            pq.write_table(kept, str(tmp_path))
            tmp_path.rename(new_path)
            result.files_after += corrupt_count + 1
        else:
            result.files_after += corrupt_count

        for old_file in readable_files:
            if new_path is not None and old_file == new_path:
                continue
            old_file.unlink(missing_ok=True)

    if not dry_run and result.projects_processed > 0 and (
        result.files_before != result.files_after or result.rows_pruned > 0
    ):
        event(
            "catalog.compact_submit_provenance",
            projects=result.projects_processed,
            files_before=result.files_before,
            files_after=result.files_after,
            rows_kept=result.rows_kept,
            rows_pruned=result.rows_pruned,
        )

    return result
