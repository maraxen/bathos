"""Regressions for two log-mode cutover leftovers (sprint 144, task 261016_debt-sprint-cutover).

debt #2814: ``run_<id>.tmp.parquet`` matches the ``run_*.parquet`` glob but is an in-flight
write-then-rename temp, never a fragment. Every cool-tier scan goes through
``catalog.iter_run_fragments``.

debt #2752: DuckDB connections have no ``.closed`` attribute. The old ``if not con.closed``
guard raised AttributeError out of the ``finally`` block of the warm-tier gate helpers, which
silently dropped them onto the cool-tier scan.
"""

from pathlib import Path

from bathos.catalog import init_catalog, iter_run_fragments, write_run
from bathos.compact import _fragment_count, compact
from bathos.prereg import check_first_of_kind, check_sidecar_drift
from bathos.schema import Run


def test_iter_run_fragments_skips_tmp_and_honours_depth(tmp_catalog: Path, sample_run: Run):
    init_catalog(tmp_catalog)
    write_run(sample_run, tmp_catalog)
    runs_dir = tmp_catalog / "runs"
    real = next(runs_dir.rglob("run_*.parquet"))

    (runs_dir / "run_inflight.tmp.parquet").write_bytes(b"x")
    nested = runs_dir / "slug"
    nested.mkdir()
    (nested / "run_inflight2.tmp.parquet").write_bytes(b"x")

    assert iter_run_fragments(runs_dir) == [real]
    flat = iter_run_fragments(runs_dir, recursive=False)
    assert flat == ([real] if real.parent == runs_dir else [])


def test_fragment_count_ignores_tmp_fragments(tmp_catalog: Path, sample_run: Run):
    """An in-flight temp must not inflate the count that drives ``should_compact``."""
    init_catalog(tmp_catalog)
    write_run(sample_run, tmp_catalog)
    (tmp_catalog / "runs" / "run_inflight.tmp.parquet").write_bytes(b"x")

    assert _fragment_count(tmp_catalog) == 1


def _warm_catalog_with_run(tmp_path: Path, script: Path, **run_kwargs) -> Path:
    """Catalog whose run is compacted into the warm DuckDB tier, so the gate helpers take
    their warm branch (the ``finally: con.close()`` path) rather than the cool fallback."""
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    write_run(
        Run(
            project_slug="test",
            command=f"python {script}",
            argv=["python", str(script)],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            **run_kwargs,
        ),
        catalog_dir,
    )
    compact(catalog_dir)
    return catalog_dir


def test_check_first_of_kind_warm_tier_closes_connection(tmp_path: Path, caplog):
    script = tmp_path / "run_nvt.py"
    script.touch()
    catalog_dir = _warm_catalog_with_run(tmp_path, script)

    assert check_first_of_kind(script, catalog_dir, "abc123") is False
    assert check_first_of_kind(script, catalog_dir, "other") is True
    # The cool-tier fallback gives the same answer, so assert the warm path itself did not
    # blow up: the failure is only visible as this warning.
    assert "Warm tier gate check failed" not in caplog.text


def test_check_sidecar_drift_warm_tier_closes_connection(tmp_path: Path, caplog):
    script = tmp_path / "run_nvt.py"
    script.touch()
    catalog_dir = _warm_catalog_with_run(tmp_path, script, sidecar_sha256="aaa")

    assert check_sidecar_drift(script, catalog_dir, "aaa") is False
    assert check_sidecar_drift(script, catalog_dir, "bbb") is True
    assert "Sidecar drift check failed" not in caplog.text
