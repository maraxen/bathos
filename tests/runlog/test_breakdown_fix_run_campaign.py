# ruff: noqa: F811
"""Remediation group 3: the `bth run --campaign` breakdown gate works in LOG mode, where campaigns
are event-only (the claim binding lives in the folded index, not in `bathos.db`), and a failure to
read the claim binding is advisory but never silent (spec 261026 D3)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

from bathos.catalog import read_runs
from bathos.runner import run_script
from bathos.telemetry import run_advisories_var

from .test_breakdown_gate_log_mode import (  # noqa: F401
    _COVERED,
    _OFFENDING,
    _SCRIPT,
    _bound_campaign,
    log_project,
)


def _run_into(workspace: Path, catalog_dir: Path, sidecar: str, campaign_id: str) -> int:
    scripts = workspace / "scripts" / "explore"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "probe.py").write_text(_SCRIPT)
    (scripts / "probe.bth.toml").write_text(sidecar)
    return run_script(
        argv=[sys.executable, str(scripts / "probe.py"), json.dumps({"e_max_placement": 0.2})],
        project_slug="proj",
        catalog_dir=catalog_dir,
        output_paths=[],
        tags=[],
        cwd=workspace,
        campaign_id=campaign_id,
    )


def test_an_uncovered_aggregate_is_refused_before_compute_in_log_mode(log_project, capsys):
    workspace, catalog_dir = log_project
    campaign_id = _bound_campaign(workspace, catalog_dir)
    assert _run_into(workspace, catalog_dir, _OFFENDING, campaign_id) == 1
    err = capsys.readouterr().err
    assert "breakdown_missing" in err and "e_max_placement" in err
    assert read_runs(catalog_dir) == []


def test_a_covered_sidecar_proceeds_in_log_mode(log_project):
    workspace, catalog_dir = log_project
    campaign_id = _bound_campaign(workspace, catalog_dir)
    assert _run_into(workspace, catalog_dir, _COVERED, campaign_id) == 0


def test_an_unbound_campaign_proceeds_in_log_mode(log_project):
    from bathos.campaigns import create_campaign
    from bathos.runlog.index import connect_read

    workspace, catalog_dir = log_project
    db = connect_read(catalog_dir)
    try:
        unbound = create_campaign(
            db, "free", "proj", "exploration", catalog_dir=catalog_dir, cwd=workspace
        ).id
    finally:
        db.close()
    assert _run_into(workspace, catalog_dir, _OFFENDING, unbound) == 0


def test_an_unreadable_claim_binding_is_advisory_but_loud(log_project, monkeypatch, capsys):
    workspace, catalog_dir = log_project
    campaign_id = _bound_campaign(workspace, catalog_dir)

    def broken(*_args, **_kwargs):
        raise duckdb.IOException("index unreadable")

    monkeypatch.setattr("bathos.campaigns.get_campaign", broken)
    collected: list[dict] = []
    token = run_advisories_var.set(collected)
    try:
        assert _run_into(workspace, catalog_dir, _OFFENDING, campaign_id) == 0  # advisory
    finally:
        run_advisories_var.reset(token)
    err = capsys.readouterr().err
    assert "claim binding" in err
    assert any(a["code"] == "claim_binding_unreadable" for a in collected)


def test_an_unexpected_error_is_not_swallowed(log_project, monkeypatch):
    """Only a read failure is advisory; a bug (here a TypeError) must propagate."""
    import pytest

    workspace, catalog_dir = log_project
    campaign_id = _bound_campaign(workspace, catalog_dir)

    def buggy(*_args, **_kwargs):
        raise TypeError("a bug, not a read failure")

    monkeypatch.setattr("bathos.campaigns.get_campaign", buggy)
    with pytest.raises(TypeError):
        _run_into(workspace, catalog_dir, _OFFENDING, campaign_id)
