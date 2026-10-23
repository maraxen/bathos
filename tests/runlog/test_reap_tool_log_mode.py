"""`reap --apply` on a log-mode catalog (MCP/CLI `reap_tool`).

Regression: `reap_tool` called `reconcile_warm_tier` after every apply. In log
mode `bathos.db` is the frozen pre-cutover copy (it still exists), so the guard
`if not db_path.exists(): return` did not fire and `compact(force_rebuild=True)`
raised "force_rebuild is not supported in log mode" -- AFTER `reap_runs` had
already appended every `run.reaped` event. The caller saw `internal` and a
manual-recovery hint although the reap itself had succeeded.
"""

from __future__ import annotations

import duckdb

from bathos.mcp import reap_tool
from bathos.reap import reconcile_warm_tier
from bathos.runlog.ingest import run_ingest

from .test_reap_folded_status import (
    _folded_run,
    _make_run,
    _start,
    enable_log_mode,
    setup_project,
)


def _freeze_warm_db(catalog_dir) -> bytes:
    """A frozen pre-cutover warm DB, as the real catalog keeps it."""
    db_path = catalog_dir / "bathos.db"
    duckdb.connect(str(db_path)).close()
    return db_path.read_bytes()


def test_reap_tool_apply_succeeds_in_log_mode_with_frozen_warm_db(tmp_path, monkeypatch):
    repo = setup_project(tmp_path)
    monkeypatch.chdir(repo)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    frozen = _freeze_warm_db(catalog_dir)

    _start(repo, _make_run("run-orphan", age_hours=25))
    run_ingest(catalog_dir)

    result = reap_tool(catalog_dir=str(catalog_dir), older_than_h=24, dry_run=False, apply=True)

    assert "error" not in result, result
    assert result["candidate_ids"] == ["run-orphan"]
    assert result["reaped"] == 1

    run_ingest(catalog_dir)
    assert _folded_run(catalog_dir, "run-orphan")["status"] == "abandoned"

    # The frozen warm DB is untouched: no rebuild, no stray backup beside it.
    assert (catalog_dir / "bathos.db").read_bytes() == frozen
    assert list(catalog_dir.glob("bathos.db.bak-*")) == []


def test_reconcile_warm_tier_is_noop_in_log_mode(tmp_path):
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    frozen = _freeze_warm_db(catalog_dir)

    reconcile_warm_tier(catalog_dir)  # must not raise

    assert (catalog_dir / "bathos.db").read_bytes() == frozen
    assert list(catalog_dir.glob("bathos.db.bak-*")) == []
