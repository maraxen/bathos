"""A fragment schema upgrade after cut-over (261025).

A plain `bth migrate` on a log-mode catalog rewrote every frozen cool fragment
to the current schema (new default-valued columns, `schema_version` bumped).
Each fragment's import hash changed, so `bth verify` reported all 6,964 as
`legacy_write_after_cutover` and failed. Now `bth migrate` refuses that write
in log mode, and verify reports a schema-only rewrite as an advisory
`legacy_schema_upgrade_after_cutover` while a real value change still fails.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bathos.catalog import write_run
from bathos.migrate import migrate_catalog
from bathos.runlog.ingest import run_ingest
from bathos.runlog.migrate import import_legacy_post_cutover
from bathos.schema import Run
from bathos.verify import verify_runlog

from .test_verify_runlog import enable_log_mode, setup_project

_DROPPED = ("dependency_provenance", "seed", "stdout_sha256")


def _old_schema_fragment(catalog_dir: Path) -> Path:
    run = Run(
        id="run-old",
        project_slug="repo",
        command="test",
        argv=["test"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        status="completed",
        hostname="h1",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    write_run(run, catalog_dir)
    (frag,) = (catalog_dir / "runs").rglob("run_*.parquet")
    tbl = pq.read_table(frag)
    tbl = tbl.drop_columns(list(_DROPPED))
    idx = tbl.schema.get_field_index("schema_version")
    tbl = tbl.set_column(idx, "schema_version", pa.array(["9"] * len(tbl), type=pa.string()))
    pq.write_table(tbl, frag)
    return frag


@pytest.fixture
def imported_old_fragment(tmp_path: Path) -> tuple[Path, Path]:
    setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    frag = _old_schema_fragment(catalog_dir)
    enable_log_mode(catalog_dir)
    assert import_legacy_post_cutover(catalog_dir).status == "imported"
    run_ingest(catalog_dir)
    return catalog_dir, frag


def _types(result) -> list[str]:
    return sorted(f["type"] for f in result.stats["findings"] if f["type"].startswith("legacy_"))


def test_migrate_refuses_to_rewrite_fragments_in_log_mode(imported_old_fragment) -> None:
    catalog_dir, frag = imported_old_fragment
    before = frag.read_bytes()

    with pytest.raises(ValueError, match="refused in log mode"):
        migrate_catalog(catalog_dir)
    assert migrate_catalog(catalog_dir, dry_run=True).migrated == 1  # inspection still works
    assert frag.read_bytes() == before


def test_schema_only_rewrite_is_advisory(imported_old_fragment, monkeypatch) -> None:
    catalog_dir, _frag = imported_old_fragment
    assert _types(verify_runlog(catalog_dir)) == []  # baseline: imported, unchanged

    # Reproduce the 261025 pass: an older bathos with no log-mode guard.
    monkeypatch.setattr("bathos.runlog.mode.is_log_mode", lambda *_a, **_k: False)
    assert migrate_catalog(catalog_dir).migrated == 1
    monkeypatch.undo()

    result = verify_runlog(catalog_dir)
    assert _types(result) == ["legacy_schema_upgrade_after_cutover"]
    assert not any("legacy_" in e for e in result.errors)
    assert any("1 frozen cool fragment" in w for w in result.warnings)


def test_value_change_after_schema_rewrite_still_fails(imported_old_fragment, monkeypatch) -> None:
    """Negative control: the advisory class must not swallow a real data change."""
    catalog_dir, frag = imported_old_fragment
    monkeypatch.setattr("bathos.runlog.mode.is_log_mode", lambda *_a, **_k: False)
    migrate_catalog(catalog_dir)
    monkeypatch.undo()
    tbl = pq.read_table(frag)
    idx = tbl.schema.get_field_index("hostname")
    pq.write_table(tbl.set_column(idx, "hostname", pa.array(["h2"], type=pa.string())), frag)

    result = verify_runlog(catalog_dir)

    assert _types(result) == ["legacy_write_after_cutover"]
    assert not result.ok
