"""Breakdown gate (spec 261026 D3) end to end in LOG mode: a real `bth run` writes `run.started`
with the frozen sidecar declaration, then campaign add / conclude on a claim-bound campaign read
it back through the real resolver (nothing stubbed). The run-time sidecar is recovered from the
event log, so a later edit of the live file cannot change the verdict."""

from __future__ import annotations

import json
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bathos.campaigns import create_campaign
from bathos.mcp import campaign_add_tool, campaign_conclude_tool, claim_register_tool
from bathos.runlog.emit import emit_event
from bathos.runlog.index import connect_read
from bathos.runlog.ingest import run_ingest
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import assign_project_id, register_main_root
from bathos.runner import run_script

from .conftest import make_git_repo, write_bth_toml

_OFFENDING = textwrap.dedent("""
    [experiment]
    hypothesis = "h"
    [outcomes.pass]
    condition = "e_max_placement <= 0.5"
    decision = "go"
    reasoning = "r"
    [outcomes.fail]
    condition = "e_max_placement > 0.5"
    decision = "stop"
    reasoning = "r"
    is_residual = true
    [result_schema]
    e_max_placement = "float"
""")
_SUMMARY_SCHEMA = textwrap.dedent("""
    e_max_placement__worst_unit = "str"
    e_max_placement__second = "float"
    e_max_placement__median = "float"
    e_max_placement__n_units = "int"
""")
_COVERED = (
    '[breakdown]\nunit = "binder"\n[breakdown.aggregates.e_max_placement]\ndirection = "max"\n'
    + _OFFENDING
    + _SUMMARY_SCHEMA
)
_WAIVED = '[breakdown]\nwaive = "calibration; nothing is built on the maximum"\n' + _OFFENDING
_SUMMARY = {
    "e_max_placement__worst_unit": "b17",
    "e_max_placement__second": 0.1,
    "e_max_placement__median": 0.004,
    "e_max_placement__n_units": 24,
}
_CLAIM = """[claim]
headline = "Test claim"
kill_condition = "Outcome != expected"

[[hypotheses]]
id = "H_primary"
label = "Primary hypothesis"

[claim.union_gate]
[[claim.union_gate.clauses]]
id = "C_main"
description = "Main clause"
hypothesis_ids = ["H_primary"]
"""
_SCRIPT = textwrap.dedent("""
    import json, os, sys
    p = os.environ.get("BTH_RESULTS_PATH")
    if p:
        json.dump(json.loads(sys.argv[1]), open(p, "w"))
""")


@pytest.fixture
def log_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    workspace = make_git_repo(tmp_path / "ws")
    write_bth_toml(workspace, slug="proj")
    assign_project_id(workspace)
    register_main_root(workspace)
    (workspace / ".gitignore").write_text("/.bth/log/\n")
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    cutover_marker_path(catalog_dir).write_text(
        json.dumps({"at": "2020-01-01T00:00:00Z", "bathos": "test", "attempt": "t", "segments": []})
    )
    monkeypatch.chdir(workspace)
    return workspace, catalog_dir


def _bound_campaign(workspace: Path, catalog_dir: Path, mode: str = "confirmation") -> str:
    db = connect_read(catalog_dir)
    try:
        campaign_id = create_campaign(
            db, f"c-{mode}", "proj", mode, catalog_dir=catalog_dir, cwd=workspace
        ).id
    finally:
        db.close()
    claim = workspace / "test.claim.toml"
    claim.write_text(_CLAIM)
    result = claim_register_tool(
        str(claim), campaign_id, catalog_dir=str(catalog_dir), workspace_root=str(workspace)
    )
    assert result["ok"] is True, result["error"].replace("\n", " | ")
    return campaign_id


def _real_run(workspace: Path, catalog_dir: Path, sidecar: str, result: dict) -> str:
    """A real `bth run` (no campaign): writes run.started with the sidecar declaration."""
    scripts = workspace / "scripts" / "explore"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "probe.py").write_text(_SCRIPT)
    (scripts / "probe.bth.toml").write_text(sidecar)
    rc = run_script(
        argv=[sys.executable, str(scripts / "probe.py"), json.dumps(result)],
        project_slug="proj",
        catalog_dir=catalog_dir,
        output_paths=[],
        tags=[],
        cwd=workspace,
    )
    assert rc == 0
    run_ingest(catalog_dir)
    db = connect_read(catalog_dir)
    try:
        ids = [r[0] for r in db.execute("SELECT id FROM runs ORDER BY timestamp DESC").fetchall()]
    finally:
        db.close()
    return ids[0]


def _members(catalog_dir: Path, campaign_id: str) -> list[str]:
    run_ingest(catalog_dir)
    db = connect_read(catalog_dir)
    try:
        return [
            r[0]
            for r in db.execute(
                "SELECT run_id FROM campaign_runs WHERE campaign_id = ?", [campaign_id]
            ).fetchall()
        ]
    finally:
        db.close()


def _add(catalog_dir: Path, campaign_id: str, run_id: str) -> dict:
    return campaign_add_tool(run_id=run_id, campaign_id=campaign_id, catalog_dir=str(catalog_dir))


def test_add_refused_without_breakdown(log_project):
    workspace, catalog_dir = log_project
    campaign_id = _bound_campaign(workspace, catalog_dir)
    run_id = _real_run(workspace, catalog_dir, _OFFENDING, {"e_max_placement": 0.2})
    result = _add(catalog_dir, campaign_id, run_id)
    assert result["ok"] is False
    assert result["error_code"] == "breakdown_missing"
    assert "e_max_placement" in result["error"]
    assert _members(catalog_dir, campaign_id) == []


def test_add_accepted_with_breakdown(log_project):
    workspace, catalog_dir = log_project
    campaign_id = _bound_campaign(workspace, catalog_dir)
    run_id = _real_run(workspace, catalog_dir, _COVERED, {"e_max_placement": 0.2, **_SUMMARY})
    result = _add(catalog_dir, campaign_id, run_id)
    assert result["ok"] is True, result["error"].replace("\n", " | ")
    assert _members(catalog_dir, campaign_id) == [run_id]


def test_add_refused_when_breakdown_declared_but_columns_missing(log_project):
    workspace, catalog_dir = log_project
    campaign_id = _bound_campaign(workspace, catalog_dir)
    run_id = _real_run(workspace, catalog_dir, _COVERED, {"e_max_placement": 0.2})
    result = _add(catalog_dir, campaign_id, run_id)
    assert result["error_code"] == "breakdown_missing"
    assert "e_max_placement__worst_unit" in result["error"]


def test_add_accepted_with_run_time_waiver(log_project):
    workspace, catalog_dir = log_project
    campaign_id = _bound_campaign(workspace, catalog_dir)
    run_id = _real_run(workspace, catalog_dir, _WAIVED, {"e_max_placement": 0.2})
    assert _add(catalog_dir, campaign_id, run_id)["ok"] is True


def test_post_hoc_edit_of_the_live_sidecar_does_not_change_the_verdict(log_project):
    """The frozen declaration, not the live file, is the run-time sidecar in log mode."""
    workspace, catalog_dir = log_project
    campaign_id = _bound_campaign(workspace, catalog_dir)
    run_id = _real_run(workspace, catalog_dir, _OFFENDING, {"e_max_placement": 0.2})
    (workspace / "scripts" / "explore" / "probe.bth.toml").write_text(_WAIVED)
    result = _add(catalog_dir, campaign_id, run_id)
    assert result["error_code"] == "breakdown_missing"  # not accepted, not "unverifiable"


def _inject_member(workspace: Path, campaign_id: str, run_id: str) -> None:
    """A member that reached the campaign without the add gate (as a SLURM / link path does)."""
    emit_event(
        kind="campaign.run_added",
        entity=[campaign_id, run_id],
        data={"campaign_id": campaign_id, "run_id": run_id, "evalue": None, "seq_position": None},
        cwd=workspace,
        hard_fail=True,
    )
    assert datetime.now(UTC)  # (event time is stamped by emit_event)


def _conclude(workspace: Path, catalog_dir: Path, campaign_id: str) -> dict:
    return campaign_conclude_tool(
        campaign_id=campaign_id,
        outcome_label="pass",
        conclusion="c",
        catalog_dir=str(catalog_dir),
        workspace_root=str(workspace),
    )


def test_conclude_refused_for_a_member_without_breakdown(log_project):
    workspace, catalog_dir = log_project
    campaign_id = _bound_campaign(workspace, catalog_dir)
    run_id = _real_run(workspace, catalog_dir, _OFFENDING, {"e_max_placement": 0.2})
    _inject_member(workspace, campaign_id, run_id)
    result = _conclude(workspace, catalog_dir, campaign_id)
    assert result.get("error_code") == "breakdown_missing", result


def test_conclude_not_refused_for_covered_and_waived_members(log_project):
    workspace, catalog_dir = log_project
    for sidecar, result_payload in (
        (_COVERED, {"e_max_placement": 0.2, **_SUMMARY}),
        (_WAIVED, {"e_max_placement": 0.2}),
    ):
        campaign_id = _bound_campaign(workspace, catalog_dir)
        run_id = _real_run(workspace, catalog_dir, sidecar, result_payload)
        _inject_member(workspace, campaign_id, run_id)
        result = _conclude(workspace, catalog_dir, campaign_id)
        assert result.get("error_code") != "breakdown_missing", result
        assert result.get("error_code") != "sidecar_unverifiable", result
