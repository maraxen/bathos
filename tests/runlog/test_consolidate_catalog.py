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

import fcntl
import os
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

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

    # already_present_differs/unplaceable don't gate needs_review (only
    # conflicts/unplaceable do) -- this stays "ok".
    assert result.status == "ok"
    assert result.copied == 0
    assert result.already_present_differs == ["run-c"]
    assert result.already_present_differs_count == 1
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

    # unplaceable is non-empty -> needs_review (fix 1).
    assert result.status == "needs_review"
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

    # A real conflict pushes status to needs_review -- the additive copy
    # still ran, but this is not a clean result.
    assert result.status == "needs_review"
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
    assert result.skipped_unknown == []
    assert not (dest / "bathos.db").exists()
    assert not (dest / "index.db").exists()
    # dest/writers.lock may now exist (empty) as a side effect of taking the
    # apply-time exclusive lock (fix 3: planning happens inside it) -- but
    # never as a COPY of source's (non-empty, in this test) file content.
    if (dest / "writers.lock").exists():
        assert (dest / "writers.lock").read_text() == ""
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


def test_refuses_when_source_has_cutover_marker(tmp_path: Path):
    """Fix 8: symmetric with the destination-side refusal -- a source that
    is itself already cut over is no longer a legacy catalog to fold in."""
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    source.mkdir(parents=True)
    dest.mkdir(parents=True)
    cutover_marker_path(source).write_text('{"attempt": "x", "segments": []}')

    run = _run("run-i", "proj")
    _write_flat_fragment(source, run)

    result = consolidate_project_catalog(source, dest, apply=True)

    assert result.status == "refused_source_log_mode"
    assert result.copied == 0
    assert not (dest / "runs").exists()


def test_result_is_consolidate_result_instance(tmp_path: Path):
    """Guard against an accidental return-type drift (e.g. a plain dict)."""
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    source.mkdir(parents=True)
    dest.mkdir(parents=True)

    result = consolidate_project_catalog(source, dest, apply=True)

    assert isinstance(result, ConsolidateResult)


# --------------------------------------------------------------------------
# Fix 2: full subtree allowlist (anchors/ledger/blast_radius/archived_items)
# and skipped_unknown for anything else at the top level.
# --------------------------------------------------------------------------


def test_new_flat_subtrees_are_folded_path_matched(tmp_path: Path):
    """Real fragments (via each module's own writer, not raw garbage bytes)
    so the post-apply `bth compact` -- which DOES ingest these four
    subtrees -- succeeds against them, not just a byte-copy check."""
    from bathos.anchor import AnchorRecord, write_anchor_fragment
    from bathos.archived_items import ArchivedItemRecord, write_archived_item_fragment
    from bathos.blast_radius import BlastRadiusRecord
    from bathos.blast_radius import write_ledger_fragment as write_blast_radius_fragment
    from bathos.trust_ledger import TrustLedgerRecord
    from bathos.trust_ledger import write_ledger_fragment as write_trust_ledger_fragment

    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)

    write_anchor_fragment(AnchorRecord(path="fig.png", sha256="a" * 64, kind="figure"), source)
    write_trust_ledger_fragment(
        TrustLedgerRecord(content_hash="b" * 64, from_state="pending", to_state="promoted"),
        source,
    )
    write_blast_radius_fragment(
        BlastRadiusRecord(entity_type="run", entity_id="r1", to_state="affected"), source
    )
    write_archived_item_fragment(
        ArchivedItemRecord(id="i1", project_slug="proj", event="archived"), source
    )

    result = consolidate_project_catalog(source, dest, apply=True)

    assert result.status == "ok"
    for subtree in ("anchors", "ledger", "blast_radius", "archived_items"):
        assert result.per_subtree[subtree]["copied"] == 1
        copied_files = list((dest / subtree).glob("*.parquet"))
        assert len(copied_files) == 1
        source_files = list((source / subtree).glob("*.parquet"))
        assert copied_files[0].read_bytes() == source_files[0].read_bytes()
    assert result.skipped_unknown == []
    # compact() ran (total_copied > 0) and successfully ingested all four --
    # would have raised on invalid/unreadable Parquet.
    assert (dest / "bathos.db").exists()


def test_attestations_already_covered_via_sidecars(tmp_path: Path):
    """Attestations live at sidecars/attestations/ -- already inside the
    existing `sidecars` subtree, so no separate allowlist entry is needed."""
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)

    att_dir = source / "sidecars" / "attestations"
    att_dir.mkdir(parents=True)
    (att_dir / "abc123.attestation.bth.toml").write_text("kind = 'oracle_match'\n")

    result = consolidate_project_catalog(source, dest, apply=True)

    assert result.status == "ok"
    assert (dest / "sidecars" / "attestations" / "abc123.attestation.bth.toml").is_file()
    assert result.skipped_unknown == []


def test_unknown_top_level_entry_is_reported_not_silently_dropped(tmp_path: Path):
    """`harness_runs/` and `quarantine/` are real catalog subtrees that
    exist today but are out of scope for consolidation -- they must show up
    in skipped_unknown, never be silently ignored (fix 2)."""
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)

    (source / "harness_runs").mkdir(parents=True)
    (source / "harness_runs" / "x.json").write_text("{}")
    (source / "quarantine").mkdir(parents=True)
    (source / "quarantine" / "y.parquet").write_bytes(b"x")

    result = consolidate_project_catalog(source, dest, apply=True)

    assert result.status == "ok"
    assert result.skipped_unknown == ["harness_runs", "quarantine"]
    assert not (dest / "harness_runs").exists()
    assert not (dest / "quarantine").exists()


def test_needs_review_on_unplaceable_even_with_zero_conflicts(tmp_path: Path):
    """Fix 1: unplaceable alone (no conflicts) is still enough to force
    needs_review -- not just conflicts."""
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)

    runs_dir = source / "runs"
    runs_dir.mkdir(parents=True)
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table({"id": ["run-z"], "command": ["x"]})
    pq.write_table(table, runs_dir / "run_run-z.parquet")

    result = consolidate_project_catalog(source, dest, apply=True)

    assert result.status == "needs_review"
    assert result.conflicts == 0
    assert result.unplaceable == ["runs/run_run-z.parquet"]


def test_needs_review_on_dry_run_too(tmp_path: Path):
    """The needs_review classification applies to dry runs identically, not
    just a real apply."""
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    (source / "campaigns").mkdir(parents=True)
    (dest / "campaigns").mkdir(parents=True)
    (source / "campaigns" / "c.json").write_text("source-version")
    (dest / "campaigns" / "c.json").write_text("dest-version")

    result = consolidate_project_catalog(source, dest, apply=False)

    assert result.status == "needs_review"
    assert result.applied is False


# --------------------------------------------------------------------------
# Fix 4: temp/dotfile names are never read as content, in any subtree.
# --------------------------------------------------------------------------


def test_temp_and_dotfile_names_skipped_in_runs(tmp_path: Path):
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)

    runs_dir = source / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "run_run-tmp1.tmp.parquet").write_bytes(b"in-flight")
    (runs_dir / ".run_run-tmp2.parquet").write_bytes(b"hidden")

    result = consolidate_project_catalog(source, dest, apply=True)

    assert result.status == "ok"
    assert result.copied == 0
    if (dest / "runs").exists():
        assert list((dest / "runs").rglob("*")) == []


def test_temp_and_dotfile_names_skipped_in_path_matched_subtree(tmp_path: Path):
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)

    camp_dir = source / "campaigns"
    camp_dir.mkdir(parents=True)
    (camp_dir / "c.json.tmp").write_text("in-flight")
    (camp_dir / ".hidden.json").write_text("hidden")
    (camp_dir / "real.parquet.tmp").write_bytes(b"in-flight-2")

    result = consolidate_project_catalog(source, dest, apply=True)

    assert result.status == "ok"
    assert result.copied == 0
    assert not (dest / "campaigns" / "c.json.tmp").exists()
    assert not (dest / "campaigns" / ".hidden.json").exists()


def test_temp_named_dest_fragment_not_treated_as_existing(tmp_path: Path):
    """A `.tmp.parquet` sitting in dest/runs/ (another writer's in-flight
    file) must not be indexed as "this run id already exists" -- the source
    fragment for that same id is still copied."""
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    (dest / "runs").mkdir(parents=True)
    (dest / "runs" / "run_run-j.tmp.parquet").write_bytes(b"someone-elses-in-flight-write")

    run = _run("run-j", "proj")
    _write_flat_fragment(source, run)

    result = consolidate_project_catalog(source, dest, apply=True)

    assert result.status == "ok"
    assert result.copied == 1
    assert (dest / "runs" / "proj" / "run_run-j.parquet").is_file()


# --------------------------------------------------------------------------
# Fix 5 + 6: within-source duplicate run ids, byte-compared, differs list.
# --------------------------------------------------------------------------


def test_within_source_duplicate_identical_bytes_copies_once(tmp_path: Path):
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)

    run = _run("run-k", "proj")
    runs_dir = source / "runs"
    runs_dir.mkdir(parents=True)
    # Flat copy and slug-subdir copy of the SAME bytes, both new to dest.
    pq.write_table(run.to_arrow(), runs_dir / "run_run-k.parquet")
    (runs_dir / "other").mkdir()
    pq.write_table(run.to_arrow(), runs_dir / "other" / "run_run-k.parquet")

    result = consolidate_project_catalog(source, dest, apply=True)

    assert result.status == "ok"
    assert result.copied == 1
    assert result.already_present == 1
    assert result.already_present_differs == []
    dest_fragments = list((dest / "runs").rglob("run_*.parquet"))
    assert len(dest_fragments) == 1


def test_within_source_duplicate_differing_bytes_reported_not_dropped(tmp_path: Path):
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)

    run_a = _run("run-l", "proj", command="python a.py")
    run_b = _run("run-l", "proj", command="python b.py")
    runs_dir = source / "runs"
    runs_dir.mkdir(parents=True)
    pq.write_table(run_a.to_arrow(), runs_dir / "run_run-l.parquet")
    (runs_dir / "other").mkdir()
    pq.write_table(run_b.to_arrow(), runs_dir / "other" / "run_run-l.parquet")

    result = consolidate_project_catalog(source, dest, apply=True)

    # First occurrence copied, second reported via already_present_differs
    # rather than silently dropped (fix 5) -- never a conflict for runs.
    assert result.copied == 1
    assert result.already_present_differs == ["run-l"]
    assert result.already_present_differs_count == 1
    assert result.conflicts == 0


# --------------------------------------------------------------------------
# Fix 7: project_slug sanitization.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad_slug", ["..", ".", "a/b", "a\\b"])
def test_unsafe_project_slug_is_unplaceable(tmp_path: Path, bad_slug: str):
    """A fragment whose OWN `project_slug` column holds an unsafe value
    (fix 7) is reported unplaceable rather than trusted as a path
    component -- flat layout, so `_read_run_project_slug` reads the
    (truthy, non-empty) column value directly rather than falling back to
    the parent directory name."""
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)

    run = _run("run-m", bad_slug)
    _write_flat_fragment(source, run)

    result = consolidate_project_catalog(source, dest, apply=True)

    assert result.status == "needs_review"
    assert result.copied == 0
    assert result.unplaceable == ["runs/run_run-m.parquet"]


# --------------------------------------------------------------------------
# Fix 3: TOCTOU -- a file that appears at dest between planning and the
# per-file rename is never clobbered.
# --------------------------------------------------------------------------


def test_late_arriving_dest_run_fragment_not_clobbered(tmp_path, monkeypatch):
    import bathos.runlog.migrate as migrate_mod

    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)

    run = _run("run-n", "proj")
    _write_flat_fragment(source, run)

    orig_link = migrate_mod.os.link
    calls = {"n": 0}

    def racing_link(src, dst, *, follow_symlinks=True):  # noqa: ARG001 -- matches os.link signature
        if calls["n"] == 0:
            calls["n"] += 1
            # Simulate a concurrent external writer landing at dest first,
            # with DIFFERENT bytes -- our own os.link now genuinely
            # collides, exactly as it would in a real race.
            Path(dst).write_bytes(b"raced-in-externally-different-bytes")
        return orig_link(src, dst)

    monkeypatch.setattr(migrate_mod.os, "link", racing_link)

    result = consolidate_project_catalog(source, dest, apply=True)

    # Never clobbered -- the racing writer's bytes survive.
    dest_path = dest / "runs" / "proj" / "run_run-n.parquet"
    assert dest_path.read_bytes() == b"raced-in-externally-different-bytes"
    # Reclassified as already_present_differs, not silently lost and not a
    # conflict (runs never conflict -- dest always wins).
    assert result.copied == 0
    assert result.already_present_differs == ["run-n"]
    assert result.conflicts == 0
    assert result.status == "ok"


def test_late_arriving_dest_path_matched_file_reclassified_as_conflict(tmp_path, monkeypatch):
    import bathos.runlog.migrate as migrate_mod

    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    (source / "campaigns").mkdir(parents=True)
    dest.mkdir(parents=True)
    (source / "campaigns" / "c.json").write_text("source-version")

    orig_link = migrate_mod.os.link
    calls = {"n": 0}

    def racing_link(src, dst, *, follow_symlinks=True):  # noqa: ARG001 -- matches os.link signature
        if calls["n"] == 0:
            calls["n"] += 1
            Path(dst).write_bytes(b"raced-in-dest-version")
        return orig_link(src, dst)

    monkeypatch.setattr(migrate_mod.os, "link", racing_link)

    result = consolidate_project_catalog(source, dest, apply=True)

    dest_path = dest / "campaigns" / "c.json"
    assert dest_path.read_bytes() == b"raced-in-dest-version"
    assert result.copied == 0
    assert result.conflicts == 1
    assert result.conflict_paths == ["campaigns/c.json"]
    assert result.status == "needs_review"


def test_apply_plans_inside_writers_lock(tmp_path, monkeypatch):
    """Fix 3: an apply's plan is built while the destination's writers lock
    is already held -- assert the lock is held during `_build_consolidation_plan`."""
    import bathos.runlog.migrate as migrate_mod

    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)
    run = _run("run-o", "proj")
    _write_flat_fragment(source, run)

    lock_path = dest / "writers.lock"
    seen_locked = {"value": None}
    orig_build = migrate_mod._build_consolidation_plan

    def spying_build(src, dst):
        # A held exclusive flock means a second, non-blocking exclusive
        # attempt from another fd fails immediately.
        fd = None
        try:
            fd = os.open(lock_path, os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            seen_locked["value"] = False
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            seen_locked["value"] = True
        finally:
            if fd is not None:
                os.close(fd)
        return orig_build(src, dst)

    monkeypatch.setattr(migrate_mod, "_build_consolidation_plan", spying_build)

    consolidate_project_catalog(source, dest, apply=True)

    assert seen_locked["value"] is True
