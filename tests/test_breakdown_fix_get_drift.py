"""Remediation group 9 (spec 261026 D5): `postmortem_get_tool` finds the run for its drift check
through `query.lookup_run`, so a prefix run id (as scaffolds and the examples use) is checked too."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from bathos.catalog import init_catalog, write_run
from bathos.compact import compact
from bathos.mcp import postmortem_get_tool
from bathos.schema import Run

_FULL = "a1b2c3d4-0000-0000-0000-000000000001"


def _setup(tmp_catalog: Path, tmp_path: Path, monkeypatch, *, dirty: bool, compacted: bool):
    init_catalog(tmp_catalog)
    run = Run(
        id=_FULL,
        project_slug="p",
        command="python x.py",
        argv=["python", "x.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=dirty,
        timestamp=datetime(2026, 5, 1, tzinfo=UTC),
        status="completed",
        exit_code=0,
    )
    write_run(run, tmp_catalog)
    if compacted:
        compact(tmp_catalog)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_catalog))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "x.py.pm.bth.postmortem.toml").write_text(
        'run_id = "a1b2c3d4"\n[postmortem]\nhypothesis_status = "inconclusive"\n'
        'status = "submitted"\n'
    )
    return ws


@pytest.mark.parametrize("compacted", [True, False])
def test_a_prefix_run_id_still_gets_the_drift_check(tmp_catalog, tmp_path, monkeypatch, compacted):
    ws = _setup(tmp_catalog, tmp_path, monkeypatch, dirty=True, compacted=compacted)
    result = postmortem_get_tool(run_id="a1b2c3d4", workspace_root=str(ws))
    assert "Validation failed" in result["error"]
    assert "git_dirty" in result["error"]


def test_a_clean_prefix_run_returns_the_postmortem(tmp_catalog, tmp_path, monkeypatch):
    ws = _setup(tmp_catalog, tmp_path, monkeypatch, dirty=False, compacted=True)
    result = postmortem_get_tool(run_id="a1b2c3d4", workspace_root=str(ws))
    # the workspace is not a git checkout, so no code-drift error can arise either
    assert "error" not in result, result
    assert result["run_id"] == "a1b2c3d4"
