"""Campaign-scoped postmortem retrieval and scaffolding.

The `campaign_id` field added for §8b objection 4 was effectively write-only: both retrieval
surfaces (`bth postmortem show`, `postmortem_get`) matched on `pm.run_id`, and both scaffold
surfaces were hard-keyed to a Run row via `find_run_for_scaffold`. So a campaign-scoped
postmortem could be written and validated but never retrieved by id, and never scaffolded.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from bathos.catalog import init_catalog, write_run
from bathos.cli import app
from bathos.compact import compact
from bathos.schema import Run

runner = CliRunner()


def _campaign_catalog(tmp_path: Path):
    """A warm catalog holding one campaign and one member run."""
    from bathos.campaigns import add_run_to_campaign, create_campaign

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    init_catalog(catalog_dir)
    run = Run(
        project_slug="testproj",
        command="python scripts/experiments/run_nvt.py",
        argv=["python", "scripts/experiments/run_nvt.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
    )
    write_run(run, catalog_dir)
    compact(catalog_dir)
    db = duckdb.connect(str(catalog_dir / "bathos.db"))
    try:
        # exploration: a confirmation campaign refuses member runs whose timestamp predates
        # its creation, and the run has to exist first here. Mode is irrelevant to scaffolding.
        campaign = create_campaign(db, "c", "testproj", "exploration")
        add_run_to_campaign(db, campaign.id, run.id)
        db.commit()
    finally:
        db.close()
    return catalog_dir, campaign, run


# ── the shared lookup ───────────────────────────────────────────────────────


def test_find_postmortem_locates_a_campaign_scoped_writeup(tmp_path: Path):
    from bathos.postmortem import find_postmortem

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "c1.bth.postmortem.toml").write_text(
        '[postmortem]\ncampaign_id = "c1"\nhypothesis_status = "refuted"\n', encoding="utf-8"
    )

    assert find_postmortem(ws, campaign_id="c1") is not None
    # The two id namespaces stay separate: a campaign id must not resolve as a run id.
    assert find_postmortem(ws, run_id="c1") is None


def test_find_postmortem_requires_exactly_one_id(tmp_path: Path):
    from bathos.postmortem import find_postmortem

    for kwargs in ({}, {"run_id": "r", "campaign_id": "c"}):
        with pytest.raises(ValueError, match="exactly one"):
            find_postmortem(tmp_path, **kwargs)


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_show_retrieves_a_campaign_scoped_postmortem(tmp_path: Path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(ws))
    monkeypatch.chdir(ws)
    init_catalog(catalog_dir)

    (ws / "camp.bth.postmortem.toml").write_text(
        '[postmortem]\ncampaign_id = "camp7"\nhypothesis_status = "refuted"\n'
        'summary = "the effect vanished under the control arm"\n',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["postmortem", "show", "--campaign-id", "camp7"])
    assert result.exit_code == 0, result.output
    assert "camp7" in result.output
    assert "the effect vanished" in result.output


def test_cli_show_rejects_both_ids(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["postmortem", "show", "r1", "--campaign-id", "c1"])
    assert result.exit_code != 0
    assert "exactly one" in result.output


def test_cli_scaffold_writes_a_campaign_template_with_obligation_checklist(
    tmp_path: Path, monkeypatch
):
    from bathos.obligations import open_obligation
    from bathos.postmortem import parse_postmortem

    catalog_dir, campaign, run = _campaign_catalog(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(ws))
    monkeypatch.chdir(ws)

    ob_campaign = open_obligation(ws, "campaign", campaign.id, "campaign_confounded")
    ob_run = open_obligation(ws, "run", run.id, "outcome_failed")
    open_obligation(ws, "run", "unrelated-run", "outcome_failed")

    result = runner.invoke(app, ["postmortem", "scaffold", "--campaign-id", campaign.id])
    assert result.exit_code == 0, result.output

    pm_path = ws / ".bth" / "postmortems" / f"campaign_{campaign.id}.bth.postmortem.toml"
    assert pm_path.exists()
    body = pm_path.read_text()

    # Obligations for the campaign AND its member runs are listed; unrelated ones are not.
    assert ob_campaign.obligation_id in body
    assert ob_run.obligation_id in body
    assert "unrelated-run" not in body

    # Listed as a COMMENTED checklist — a scaffold must not discharge the ledger by default.
    pm = parse_postmortem(pm_path)
    assert pm.campaign_id == campaign.id
    assert pm.run_id == ""
    assert pm.discharges == []


def test_cli_scaffold_rejects_an_unknown_campaign(tmp_path: Path, monkeypatch):
    catalog_dir, _campaign, _run = _campaign_catalog(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(ws))
    monkeypatch.chdir(ws)

    result = runner.invoke(app, ["postmortem", "scaffold", "--campaign-id", "no-such-campaign"])
    assert result.exit_code != 0
    assert "Campaign not found" in result.output


# ── MCP mirror ──────────────────────────────────────────────────────────────


def test_mcp_campaign_scaffold_and_get_mirror_the_cli(tmp_path: Path, monkeypatch):
    import asyncio

    from bathos import mcp
    from bathos.mcp_auth import get_or_create_token

    catalog_dir, campaign, _run = _campaign_catalog(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.delenv("BTH_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("BTH_MCP_TOKEN_PATH", str(tmp_path / "mcp_token"))
    token = get_or_create_token()

    res = asyncio.run(
        mcp.postmortem_scaffold(
            campaign_id=campaign.id,
            catalog_dir=str(catalog_dir),
            workspace_root=str(ws),
            token=token,
        )
    )
    assert not res.get("error"), res
    assert res["campaign_id"] == campaign.id
    assert Path(res["path"]).exists()

    got = asyncio.run(mcp.postmortem_get(campaign_id=campaign.id, workspace_root=str(ws)))
    assert not got.get("error"), got
    assert got["campaign_id"] == campaign.id
    assert got["run_id"] == ""

    # Exactly-one enforcement on both tools, returned as an error dict rather than raising.
    assert "error" in asyncio.run(mcp.postmortem_get(workspace_root=str(ws)))
    assert "error" in asyncio.run(
        mcp.postmortem_get(run_id="r", campaign_id="c", workspace_root=str(ws))
    )
    assert "error" in asyncio.run(
        mcp.postmortem_scaffold(catalog_dir=str(catalog_dir), workspace_root=str(ws), token=token)
    )
