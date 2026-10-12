"""get_run falls back to a single cool fragment when the warm DuckDB tier misses (debt #2110, partial).

A run finished after the last `bth compact` exists only as `runs/<slug>/run_<id>.parquet`.
The warm-tier lookup must find it, tag it `_source = "cool_unindexed"`, and never glob on
an unsafe id.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from bathos import query
from bathos.catalog import init_catalog, write_run
from bathos.compact import compact
from bathos.query import COOL_UNINDEXED_SOURCE, get_run, run_source
from bathos.schema import Run


@pytest.fixture
def events(monkeypatch) -> list:
    """Record telemetry events emitted by bathos.query."""
    calls: list = []
    monkeypatch.setattr(query, "event", lambda name, **fields: calls.append((name, fields)))
    return calls


def _run(sample_run: Run, run_id: str, slug: str = "testproj") -> Run:
    return replace(sample_run, id=run_id, project_slug=slug)


def _warm_catalog(catalog: Path, sample_run: Run) -> None:
    """Create a catalog whose warm tier (bathos.db) holds run A."""
    init_catalog(catalog)
    write_run(_run(sample_run, "runA"), catalog)
    compact(catalog)
    assert (catalog / "bathos.db").exists()


def test_cool_only_run_found_after_warm_index_exists(tmp_catalog: Path, sample_run: Run, events):
    _warm_catalog(tmp_catalog, sample_run)
    write_run(_run(sample_run, "runB"), tmp_catalog)  # not compacted

    b = get_run("runB", tmp_catalog, allow_cool_fallback=True)
    assert b is not None
    assert b.id == "runB"
    assert run_source(b) == COOL_UNINDEXED_SOURCE
    assert json.loads(b.metadata)["_source"] == COOL_UNINDEXED_SOURCE
    fallback = [f for name, f in events if f.get("query_kind") == "get_cool_fallback"]
    assert len(fallback) == 1
    assert fallback[0]["rows"] == 1


def test_warm_hit_has_no_marker(tmp_catalog: Path, sample_run: Run):
    _warm_catalog(tmp_catalog, sample_run)
    write_run(_run(sample_run, "runB"), tmp_catalog)

    a = get_run("runA", tmp_catalog, allow_cool_fallback=True)
    assert a is not None
    assert run_source(a) is None


def test_unknown_id_returns_none(tmp_catalog: Path, sample_run: Run):
    _warm_catalog(tmp_catalog, sample_run)
    assert get_run("does-not-exist", tmp_catalog, allow_cool_fallback=True) is None


@pytest.mark.parametrize("bad_id", ["*", "/", "..", "run*", "a/b", "", "run.tmp"])
def test_unsafe_id_returns_none_without_glob(
    tmp_catalog: Path, sample_run: Run, monkeypatch, bad_id
):
    _warm_catalog(tmp_catalog, sample_run)

    def _forbidden(_self, *_args, **_kwargs):
        raise AssertionError(f"Path.glob called for unsafe id {bad_id!r}")

    monkeypatch.setattr(Path, "glob", _forbidden)
    assert get_run(bad_id, tmp_catalog, allow_cool_fallback=True) is None


def test_same_id_in_two_slugs_returns_none(tmp_catalog: Path, sample_run: Run, events):
    _warm_catalog(tmp_catalog, sample_run)
    write_run(_run(sample_run, "runC", slug="projx"), tmp_catalog)
    write_run(_run(sample_run, "runC", slug="projy"), tmp_catalog)

    assert get_run("runC", tmp_catalog, allow_cool_fallback=True) is None
    ambiguous = [f for name, f in events if name == "catalog.cool_fallback_ambiguous"]
    assert len(ambiguous) == 1
    assert len(ambiguous[0]["paths"]) == 2


def test_corrupt_fragment_returns_none(tmp_catalog: Path, sample_run: Run):
    _warm_catalog(tmp_catalog, sample_run)
    bad_dir = tmp_catalog / "runs" / "testproj"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "run_runD.parquet").write_bytes(b"not a parquet file")

    assert get_run("runD", tmp_catalog, allow_cool_fallback=True) is None


def test_tmp_fragment_is_ignored(tmp_catalog: Path, sample_run: Run):
    _warm_catalog(tmp_catalog, sample_run)
    slug_dir = tmp_catalog / "runs" / "testproj"
    slug_dir.mkdir(parents=True, exist_ok=True)
    (slug_dir / "run_runE.tmp.parquet").write_bytes(b"in-flight write")

    assert get_run("runE", tmp_catalog, allow_cool_fallback=True) is None


def test_fallback_is_off_by_default(tmp_catalog: Path, sample_run: Run, events):
    """Non-display callers (blast radius, outputs, pin resolution) keep the old
    contract: an uncompacted run is not found, so they never act on a Run with
    empty output_metadata and unapplied postmortem overrides."""
    _warm_catalog(tmp_catalog, sample_run)
    write_run(_run(sample_run, "runF"), tmp_catalog)  # not compacted

    assert get_run("runF", tmp_catalog) is None
    assert not [f for _, f in events if f.get("query_kind") == "get_cool_fallback"]


def test_get_run_tool_returns_notice_for_uncompacted_run(tmp_catalog: Path, sample_run: Run):
    from bathos.mcp import get_run_tool

    _warm_catalog(tmp_catalog, sample_run)
    write_run(_run(sample_run, "runG"), tmp_catalog)  # not compacted

    out = get_run_tool(run_id="runG", catalog_dir=str(tmp_catalog))
    assert out["id"] == "runG"
    assert "not yet compacted" in out["notice"]

    warm = get_run_tool(run_id="runA", catalog_dir=str(tmp_catalog))
    assert warm["id"] == "runA"
    assert "notice" not in warm
