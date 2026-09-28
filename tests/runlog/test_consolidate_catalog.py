"""`bth migrate --consolidate-catalog` / `consolidate_project_catalog()`
(debt #1998, spec v39): additively folds a foreign per-project `catalog_dir`
into the catalog being migrated, without ever touching the source.

Uses the same isolation as the rest of `tests/runlog/` (`isolated_home`,
autouse from `conftest.py`) -- HOME lives under `tmp_path`, never the real
`~/.bth/`. These tests build `source`/`dest` catalog directories directly
under `tmp_path` (not through the registry) since `consolidate_project_
catalog()` takes explicit paths and does not consult the registry itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from bathos.catalog import write_run
from bathos.query import run_sql
from bathos.runlog.migrate import ConsolidateResult, consolidate_project_catalog
from bathos.runlog.mode import cutover_marker_path
from bathos.schema import Run

BASE_TS = datetime(2020, 1, 1, tzinfo=UTC)


def _run(run_id: str, slug: str, *, command: str = "python x.py") -> Run:
    return Run(
        id=run_id,
        project_slug=slug,
        command=command,
        argv=["python", "x.py"],
        git_hash="a" * 40,
        git_branch="main",
        git_dirty=False,
        timestamp=BASE_TS,
        duration_s=1.0,
        exit_code=0,
        status="completed",
    )


def _write_flat_fragment(catalog_dir: Path, run: Run) -> Path:
    """Write a run fragment directly under `catalog_dir/runs/` with no
    project-slug subdir -- the flat legacy layout the 260927 refinement
    describes (asr: 322 fragments sitting flat in a foreign catalog's
    `runs/`)."""
    runs_dir = catalog_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    target = runs_dir / f"run_{run.id}.parquet"
    pq.write_table(run.to_arrow(), target)
    return target


# --------------------------------------------------------------------------
# Runs: flat-layout matching, new fragments, differing bytes
# --------------------------------------------------------------------------


def test_flat_fragment_already_present_under_dest_slug_not_copied(tmp_path: Path):
    """A flat-layout source fragment whose same run id already lives at
    dest/runs/<slug>/run_<id>.parquet is recognised as already present --
    matched by filename (run id) anywhere under dest/runs/, not by path --
    and is NOT copied."""
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"

    run = _run("run-a", "proj")
    write_run(run, dest)  # canonical layout: dest/runs/proj/run_run-a.parquet
    _write_flat_fragment(source, run)  # same run, flat: source/runs/run_run-a.parquet

    result = consolidate_project_catalog(source, dest, apply=True)

    assert result.status == "ok"
    assert result.per_subtree["runs"]["copied"] == 0
    assert result.copied == 0
    # Only the one canonical fragment written by write_run() exists in dest.
    dest_fragments = sorted((dest / "runs").rglob("run_*.parquet"))
    assert dest_fragments == [dest / "runs" / "proj" / "run_run-a.parquet"]


def test_flat_fragment_new_run_lands_under_dest_slug(tmp_path: Path):
    """A new run id, flat in source, lands at dest/runs/<project_slug>/ --
    the slug read off the fragment's OWN `project_slug` column, not its
    source-relative path (which is flat -- there is no slug to inherit from
    the path)."""
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)

    run = _run("run-b", "newproj")
    _write_flat_fragment(source, run)

    result = consolidate_project_catalog(source, dest, apply=True)

    assert result.status == "ok"
    assert result.per_subtree["runs"]["copied"] == 1
    assert result.copied == 1
    dest_path = dest / "runs" / "newproj" / "run_run-b.parquet"
    assert dest_path.is_file()
    assert dest_path.read_bytes() == (source / "runs" / "run_run-b.parquet").read_bytes()


def test_same_run_id_different_bytes_not_overwritten_reported_informational(tmp_path: Path):
    """Same run id present in both, but with different bytes: NOT a
    conflict for runs -- reported as `already_present_differs`, and dest's
    file is left untouched (dest always wins)."""
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"

    dest_run = _run("run-c", "proj", command="python dest.py")
    write_run(dest_run, dest)
    dest_bytes_before = (dest / "runs" / "proj" / "run_run-c.parquet").read_bytes()

    source_run = _run("run-c", "proj", command="python source.py")
    _write_flat_fragment(source, source_run)

    result = consolidate_project_catalog(source, dest, apply=True)

    assert result.status == "ok"
    assert result.copied == 0
    assert result.already_present_differs == 1
    assert result.per_subtree["runs"]["already_present_differs"] == 1
    assert result.conflicts == 0
    assert result.conflict_paths == []
    # Dest's own fragment is byte-for-byte unchanged.
    assert (dest / "runs" / "proj" / "run_run-c.parquet").read_bytes() == dest_bytes_before
    # No stray copy landed anywhere else in dest.
    dest_fragments = sorted((dest / "runs").rglob("run_*.parquet"))
    assert dest_fragments == [dest / "runs" / "proj" / "run_run-c.parquet"]


def test_run_with_no_slug_and_flat_parent_is_unplaceable(tmp_path: Path):
    """A fragment with no readable `project_slug` column, sitting directly
    under `runs/` (so there is no parent-dir name to fall back to either),
    is reported as unplaceable and left uncopied."""
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)

    runs_dir = source / "runs"
    runs_dir.mkdir(parents=True)
    # A parquet file with no project_slug column at all.
    import pyarrow as pa

    table = pa.table({"id": ["run-z"], "command": ["x"]})
    pq.write_table(table, runs_dir / "run_run-z.parquet")

    result = consolidate_project_catalog(source, dest, apply=True)

    assert result.status == "ok"
    assert result.copied == 0
    assert result.unplaceable == ["runs/run_run-z.parquet"]
    assert not (dest / "runs").exists() or list((dest / "runs").rglob("run_*.parquet")) == []


# --------------------------------------------------------------------------
# Path-matched subtrees: campaigns/submits/reaped/sidecars
# --------------------------------------------------------------------------


def test_path_matched_subtree_copies_missing_skips_identical_reports_conflict(tmp_path: Path):
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    (source / "campaigns").mkdir(parents=True)
    (dest / "campaigns").mkdir(parents=True)

    # Missing in dest -> copied.
    (source / "campaigns" / "new_camp.json").write_text('{"id": "new"}')
    # Identical in both -> skipped (already_present).
    (source / "campaigns" / "same_camp.json").write_text('{"id": "same"}')
    (dest / "campaigns" / "same_camp.json").write_text('{"id": "same"}')
    # Different bytes at the same path -> conflict, not overwritten.
    (source / "campaigns" / "diff_camp.json").write_text('{"id": "source-version"}')
    (dest / "campaigns" / "diff_camp.json").write_text('{"id": "dest-version"}')

    result = consolidate_project_catalog(source, dest, apply=True)

    assert result.status == "ok"
    assert result.per_subtree["campaigns"] == {
        "copied": 1,
        "already_present": 1,
        "conflicts": 1,
    }
    assert result.conflicts == 1
    assert result.conflict_paths == ["campaigns/diff_camp.json"]
    assert (dest / "campaigns" / "new_camp.json").read_text() == '{"id": "new"}'
    # Conflict left untouched -- dest's own version survives.
    assert (dest / "campaigns" / "diff_camp.json").read_text() == '{"id": "dest-version"}'


def test_ignores_bathos_db_and_non_allowlisted_dirs(tmp_path: Path):
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)

    (source / "bathos.db").parent.mkdir(parents=True, exist_ok=True)
    (source / "bathos.db").write_text("not a real db, just a marker")
    (source / "index.db").write_text("marker")
    (source / "writers.lock").write_text("")
    (source / "logs").mkdir()
    (source / "logs" / "whatever.log").write_text("marker")
    (source / "remote-runs" / "engaging").mkdir(parents=True)
    (source / "remote-runs" / "engaging" / "run_x.parquet").write_text("marker")

    result = consolidate_project_catalog(source, dest, apply=True)

    assert result.status == "ok"
    assert result.copied == 0
    assert not (dest / "bathos.db").exists()
    assert not (dest / "index.db").exists()
    assert not (dest / "writers.lock").exists()
    assert not (dest / "logs").exists()
    assert not (dest / "remote-runs").exists()


# --------------------------------------------------------------------------
# Never touches source; dry-run writes nothing
# --------------------------------------------------------------------------


def test_never_modifies_source(tmp_path: Path):
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)

    run = _run("run-d", "proj")
    _write_flat_fragment(source, run)
    (source / "campaigns").mkdir()
    (source / "campaigns" / "c.json").write_text("{}")

    def _fingerprint(root: Path) -> dict[str, tuple[int, bytes]]:
        return {
            str(p.relative_to(root)): (p.stat().st_size, p.read_bytes())
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }

    before = _fingerprint(source)
    consolidate_project_catalog(source, dest, apply=True)
    after = _fingerprint(source)

    assert before == after


def test_dry_run_writes_nothing(tmp_path: Path):
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)

    run = _run("run-e", "proj")
    _write_flat_fragment(source, run)
    (source / "campaigns").mkdir()
    (source / "campaigns" / "c.json").write_text("{}")

    result = consolidate_project_catalog(source, dest, apply=False)

    assert result.status == "ok"
    assert result.applied is False
    assert result.copied == 2  # one run fragment + one campaign file, planned but not written
    dest_files = [p for p in dest.rglob("*") if p.is_file()]
    assert dest_files == []


# --------------------------------------------------------------------------
# After apply, the copied run is visible in the warm tier
# --------------------------------------------------------------------------


def test_apply_compacts_and_run_becomes_visible_in_warm_tier(tmp_path: Path):
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)

    run = _run("run-f", "proj")
    _write_flat_fragment(source, run)

    result = consolidate_project_catalog(source, dest, apply=True)

    assert result.status == "ok"
    assert result.copied == 1
    assert (dest / "bathos.db").exists()
    rows = run_sql("SELECT id FROM runs WHERE id = 'run-f'", dest)
    assert rows == [("run-f",)]


def test_dry_run_does_not_compact(tmp_path: Path):
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)

    run = _run("run-g", "proj")
    _write_flat_fragment(source, run)

    consolidate_project_catalog(source, dest, apply=False)

    assert not (dest / "bathos.db").exists()


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_refuses_when_dest_has_cutover_marker(tmp_path: Path):
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)
    cutover_marker_path(dest).write_text('{"attempt": "x", "segments": []}')

    run = _run("run-h", "proj")
    _write_flat_fragment(source, run)

    result = consolidate_project_catalog(source, dest, apply=True)

    assert result.status == "refused_log_mode"
    assert result.copied == 0
    # Never touched dest's runs/ either.
    assert not (dest / "runs").exists()


def test_refuses_when_source_missing(tmp_path: Path):
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)

    result = consolidate_project_catalog(tmp_path / "does-not-exist", dest, apply=True)

    assert result.status == "source_missing"


def test_result_is_consolidate_result_instance(tmp_path: Path):
    """Guard against an accidental return-type drift (e.g. a plain dict)."""
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    source.mkdir(parents=True)
    dest.mkdir(parents=True)

    result = consolidate_project_catalog(source, dest, apply=True)

    assert isinstance(result, ConsolidateResult)
