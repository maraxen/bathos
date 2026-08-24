"""Cool-tier campaign JSON travels with sync; resolve and review do not need a planted DB row."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from bathos.campaigns import create_campaign, review_campaign
from bathos.catalog import init_catalog, write_run
from bathos.compact import compact
from bathos.config import ProjectConfig
from bathos.schema import Run
from bathos.sync import sync_catalog


@pytest.fixture(autouse=True)
def _no_ssh_mkdir(monkeypatch):
    monkeypatch.setattr("bathos.sync.ensure_remote_catalog_dir", lambda *_a, **_k: None)


def _make_mock_popen(returncode=0, stderr_output="", stdout_output=""):
    mock_proc = MagicMock()
    mock_proc.returncode = returncode
    mock_proc.wait.return_value = returncode
    mock_proc.poll.return_value = None
    mock_proc.stderr = StringIO(stderr_output)
    mock_proc.stdout = StringIO(stdout_output)
    return mock_proc


def test_create_campaign_writes_cool_json(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        campaign = create_campaign(
            db,
            name="solvation-remat",
            project_slug="prolix",
            mode="exploration",
            catalog_dir=tmp_catalog,
        )
    finally:
        db.close()

    path = tmp_catalog / "campaigns" / f"{campaign.id}.json"
    assert path.exists()
    payload = json.loads(path.read_text())
    assert payload["id"] == campaign.id
    assert payload["name"] == "solvation-remat"
    assert payload["project_slug"] == "prolix"
    assert payload["status"] == "open"


def test_resolve_campaign_id_from_json_without_db(tmp_catalog: Path):
    from bathos.campaigns import Campaign, CampaignError, _resolve_campaign_id, write_campaign_cool

    campaign = Campaign(
        id="bd1f47e8-aaaa-bbbb-cccc-ddddeeeeffff",
        project_slug="prolix",
        name="planted",
        mode="exploration",
        status="open",
        started_at="2026-08-21T00:00:00+00:00",
    )
    write_campaign_cool(campaign, tmp_catalog)

    resolved = _resolve_campaign_id(None, "bd1f47e8", catalog_dir=tmp_catalog)
    assert resolved == campaign.id

    with pytest.raises(CampaignError, match="not found"):
        _resolve_campaign_id(None, "deadbeef", catalog_dir=tmp_catalog)


def test_review_after_pull_uses_cool_json_and_parquet(tmp_catalog: Path):
    """No campaign_runs rows: review still works from cool campaign JSON + parquet campaign_id."""
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        campaign = create_campaign(
            db,
            name="after-pull",
            project_slug="prolix",
            mode="exploration",
            catalog_dir=tmp_catalog,
        )
        db.execute("DELETE FROM campaign_runs")
        db.commit()
    finally:
        db.close()

    run = Run(
        project_slug="prolix",
        command="python solvation.py",
        argv=["python", "solvation.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 8, 21, tzinfo=UTC),
        status="completed",
        exit_code=0,
        outcome="pass",
        campaign_id=campaign.id,
    )
    write_run(run, tmp_catalog)

    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        review = review_campaign(db, campaign.id, catalog_dir=tmp_catalog)
    finally:
        db.close()

    assert "error" not in review
    assert review["total_runs"] == 1
    assert review["outcome_distribution"]["pass"] == 1


def test_sync_rsyncs_campaigns_with_update_not_ignore_existing(tmp_path: Path):
    config = ProjectConfig(
        slug="prolix",
        root=Path("/home/user/prolix"),
        remotes={"engaging": {"host": "engaging", "remote_root": "~/projects/prolix"}},
    )
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "runs" / "prolix").mkdir(parents=True)
    (catalog_dir / "campaigns").mkdir()
    (catalog_dir / "campaigns" / "bd1f47e8-aaaa-bbbb-cccc-ddddeeeeffff.json").write_text("{}")

    with patch("bathos.sync.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _make_mock_popen()
        sync_catalog("engaging", config, catalog_dir, pull=False)

    cmds = [c[0][0] for c in mock_popen.call_args_list]
    runs_cmd = next(c for c in cmds if any("runs/prolix/" in str(a) for a in c))
    camp_cmd = next(c for c in cmds if any("campaigns/" in str(a) for a in c))
    assert "--ignore-existing" in runs_cmd
    assert "--update" in camp_cmd
    assert "--ignore-existing" not in camp_cmd
    assert any("engaging:~/projects/prolix/.bth/catalog/campaigns/" in str(a) for a in camp_cmd)


def test_run_script_resolves_campaign_from_json_without_opening_db(tmp_catalog: Path, monkeypatch):
    import sys

    from bathos.campaigns import Campaign, write_campaign_cool
    from bathos.runner import run_script

    init_catalog(tmp_catalog)
    campaign = Campaign(
        id="bd1f47e8-aaaa-bbbb-cccc-ddddeeeeffff",
        project_slug="prolix",
        name="json-only",
        mode="exploration",
        status="open",
        started_at="2026-08-21T00:00:00+00:00",
    )
    write_campaign_cool(campaign, tmp_catalog)
    db_path = tmp_catalog / "bathos.db"
    if db_path.exists():
        db_path.unlink()

    def _boom(*_a, **_k):
        raise AssertionError("duckdb.connect must not be called when cool JSON exists")

    monkeypatch.setattr("duckdb.connect", _boom)

    exit_code = run_script(
        argv=[sys.executable, "-c", "pass"],
        project_slug="prolix",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
        campaign_id="bd1f47e8",
    )
    assert exit_code == 0
    from bathos.catalog import read_runs

    runs = read_runs(tmp_catalog)
    assert runs[0].campaign_id == campaign.id


def test_run_script_opens_campaign_db_read_only_when_json_missing(tmp_catalog: Path, monkeypatch):
    import sys

    from bathos.campaigns import create_campaign
    from bathos.runner import run_script

    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        campaign = create_campaign(db, name="db-only", project_slug="prolix", mode="exploration")
    finally:
        db.close()

    seen: list[bool] = []
    real_connect = duckdb.connect

    def _wrap(*args, **kwargs):
        seen.append(bool(kwargs.get("read_only")))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("duckdb.connect", _wrap)

    exit_code = run_script(
        argv=[sys.executable, "-c", "pass"],
        project_slug="prolix",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
        campaign_id=campaign.id[:8],
    )
    assert exit_code == 0
    assert seen
    assert all(seen)
