"""Regression tests for catalog-sync review findings."""

from __future__ import annotations

import json
from contextlib import suppress
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from bathos.campaigns import (
    Campaign,
    CampaignError,
    _resolve_campaign_id,
    add_run_to_campaign,
    conclude_campaign,
    create_campaign,
    get_campaign,
    ingest_cool_campaigns,
    link_cool_runs_to_campaigns,
    list_campaigns,
    prepare_catalog_for_conclude,
    review_campaign,
    write_campaign_cool,
)
from bathos.catalog import init_catalog, write_run
from bathos.cluster_catalog import ensure_remote_catalog_dir, write_bth_env_sh
from bathos.compact import compact
from bathos.config import ProjectConfig
from bathos.schema import Run
from bathos.sync import sync_catalog


def _make_mock_popen(returncode=0, stderr_output="", stdout_output=""):
    mock_proc = MagicMock()
    mock_proc.returncode = returncode
    mock_proc.wait.return_value = returncode
    mock_proc.poll.return_value = None
    mock_proc.stderr = StringIO(stderr_output)
    mock_proc.stdout = StringIO(stdout_output)
    return mock_proc


def test_ingest_preserves_claim_columns(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        campaign = create_campaign(
            db, name="claimed", project_slug="prolix", mode="confirmation", catalog_dir=tmp_catalog
        )
        db.execute(
            "UPDATE campaigns SET claim_path = ?, claim_sha256 = ?, claim_mode = ? WHERE id = ?",
            [".bth/claims/x.claim.toml", "abc123", "registered", campaign.id],
        )
        db.commit()
        ingest_cool_campaigns(db, tmp_catalog)
        row = db.execute(
            "SELECT claim_path, claim_sha256, claim_mode FROM campaigns WHERE id = ?",
            [campaign.id],
        ).fetchone()
        assert row == (".bth/claims/x.claim.toml", "abc123", "registered")

        payload = json.loads((tmp_catalog / "campaigns" / f"{campaign.id}.json").read_text())
        payload["claim_sha256"] = ""
        payload["claim_path"] = ""
        payload["claim_mode"] = ""
        payload["name"] = "renamed-from-cool"
        (tmp_catalog / "campaigns" / f"{campaign.id}.json").write_text(json.dumps(payload) + "\n")
        ingest_cool_campaigns(db, tmp_catalog)
        row2 = db.execute(
            "SELECT claim_path, claim_sha256, claim_mode, name FROM campaigns WHERE id = ?",
            [campaign.id],
        ).fetchone()
        assert row2[0] == ".bth/claims/x.claim.toml"
        assert row2[1] == "abc123"
        assert row2[2] == "registered"
        assert row2[3] == "renamed-from-cool"
    finally:
        db.close()


def test_ingest_does_not_downgrade_mode(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        campaign = create_campaign(
            db,
            name="confirm",
            project_slug="prolix",
            mode="confirmation",
            catalog_dir=tmp_catalog,
        )
        payload = json.loads((tmp_catalog / "campaigns" / f"{campaign.id}.json").read_text())
        payload["mode"] = "exploration"
        (tmp_catalog / "campaigns" / f"{campaign.id}.json").write_text(json.dumps(payload) + "\n")
        ingest_cool_campaigns(db, tmp_catalog)
        mode = db.execute("SELECT mode FROM campaigns WHERE id = ?", [campaign.id]).fetchone()[0]
        assert mode == "confirmation"
    finally:
        db.close()


def test_compact_fills_sequential_evalue(tmp_catalog: Path, tmp_path: Path):
    from tests.test_campaigns_popper import _write_popper_sidecar

    sidecar = _write_popper_sidecar(tmp_path, null=0.5, alt=0.9, threshold=20.0)
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        campaign = create_campaign(
            db, name="seq", project_slug="prolix", mode="sequential", catalog_dir=tmp_catalog
        )
    finally:
        db.close()

    run = Run(
        project_slug="prolix",
        command="python x.py",
        argv=["python", "x.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 8, 21, tzinfo=UTC),
        status="completed",
        exit_code=0,
        outcome="pass",
        sidecar_path=str(sidecar),
        campaign_id=campaign.id,
    )
    write_run(run, tmp_catalog)
    compact(tmp_catalog)

    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        row = db.execute(
            "SELECT evalue, seq_position FROM campaign_runs WHERE campaign_id = ?",
            [campaign.id],
        ).fetchone()
        assert row is not None
        assert row[0] is not None and row[0] > 0
        assert row[1] == 1
    finally:
        db.close()


def test_resolve_prefix_unions_cool_and_warm(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    prefix = "aa111111"
    id_cool = prefix + "-bbbb-cccc-dddd-000000000001"
    id_warm = prefix + "-bbbb-cccc-dddd-000000000002"

    write_campaign_cool(
        Campaign(
            id=id_cool,
            project_slug="prolix",
            name="cool-only",
            mode="exploration",
            status="open",
            started_at="2026-08-21T00:00:00+00:00",
        ),
        tmp_catalog,
    )
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at) "
            "VALUES (?, 'prolix', 'warm-only', 'exploration', 'open', '2026-08-21T00:00:00+00:00')",
            [id_warm],
        )
        db.commit()
        with pytest.raises(CampaignError, match="Ambiguous"):
            _resolve_campaign_id(db, prefix, catalog_dir=tmp_catalog)
    finally:
        db.close()


def test_review_without_warm_campaign_row(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    cid = "bd1f47e8-aaaa-bbbb-cccc-ddddeeeeffff"
    write_campaign_cool(
        Campaign(
            id=cid,
            project_slug="prolix",
            name="json-only",
            mode="exploration",
            status="open",
            started_at="2026-08-21T00:00:00+00:00",
        ),
        tmp_catalog,
    )
    write_run(
        Run(
            project_slug="prolix",
            command="python x.py",
            argv=["python", "x.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            timestamp=datetime(2026, 8, 21, tzinfo=UTC),
            status="completed",
            exit_code=0,
            outcome="pass",
            campaign_id=cid,
        ),
        tmp_catalog,
    )
    got = get_campaign(None, cid, catalog_dir=tmp_catalog)
    assert got is not None
    assert got.name == "json-only"
    review = review_campaign(None, cid, catalog_dir=tmp_catalog)
    assert "error" not in review
    assert review["total_runs"] == 1


def test_sync_runs_dest_uses_remote_catalog_path(tmp_path: Path):
    config = ProjectConfig(
        slug="prolix",
        root=Path("/home/user/prolix"),
        remotes={
            "engaging": {
                "host": "engaging",
                "remote_root": "~/projects/prolix/.bth/catalog",
            }
        },
    )
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "runs" / "prolix").mkdir(parents=True)

    with (
        patch("bathos.sync.ensure_remote_catalog_dir"),
        patch("bathos.sync.subprocess.Popen") as mock_popen,
    ):
        mock_popen.return_value = _make_mock_popen()
        sync_catalog("engaging", config, catalog_dir, pull=False)

    cmds = [c[0][0] for c in mock_popen.call_args_list]
    runs_cmd = next(c for c in cmds if any("runs/prolix/" in str(a) for a in c))
    assert not any(".bth/catalog/.bth/catalog" in str(a) for a in runs_cmd)
    assert any("~/projects/prolix/.bth/catalog/runs/prolix/" in str(a) for a in runs_cmd)


def test_ensure_remote_catalog_dir_quotes_and_timeout(monkeypatch):
    mock_run = MagicMock(return_value=MagicMock(returncode=0, stderr=""))
    monkeypatch.setattr("bathos.cluster_catalog.subprocess.run", mock_run)
    ensure_remote_catalog_dir("engaging", "~/projects/prolix")
    argv = mock_run.call_args[0][0]
    assert "ConnectTimeout=10" in argv
    remote_cmd = argv[-1]
    assert "mkdir -p --" in remote_cmd
    assert "${HOME}" in remote_cmd or "$HOME" in remote_cmd
    assert "'~/" not in remote_cmd
    assert ".bth/catalog/campaigns" in remote_cmd
    with pytest.raises(ValueError, match="newline"):
        ensure_remote_catalog_dir("engaging\nhost", "~/projects/prolix")


def test_env_helper_bakes_remote_project_root(tmp_path: Path):
    write_bth_env_sh(
        tmp_path,
        slug="prolix",
        project_root_value=tmp_path / ".worktrees" / "feature",
        remote_root="~/projects/prolix",
    )
    env_sh = (tmp_path / "scripts" / "slurm" / "_bth_env.sh").read_text()
    assert 'export BTH_PROJECT_ROOT="${HOME}/projects/prolix"' in env_sh
    assert 'export BTH_WORKSPACE_ROOT="${HOME}/projects/prolix"' in env_sh
    assert str(tmp_path / ".worktrees") not in env_sh


def test_create_and_conclude_write_cool_json(tmp_catalog: Path):
    from bathos.campaigns import conclude_campaign

    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        campaign = create_campaign(
            db,
            name="cool-write",
            project_slug="prolix",
            mode="exploration",
            catalog_dir=tmp_catalog,
        )
        cool = tmp_catalog / "campaigns" / f"{campaign.id}.json"
        assert cool.is_file()
        conclude_campaign(db, campaign.id, "success", "done", catalog_dir=tmp_catalog)
        payload = json.loads(cool.read_text())
        assert payload["status"] == "concluded"
        assert payload["outcome_label"] == "success"
    finally:
        db.close()


def test_mcp_create_writes_cool_json(tmp_catalog: Path):
    from bathos.mcp import campaign_create_tool

    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    out = campaign_create_tool(name="from-mcp", catalog_dir=str(tmp_catalog), project_slug="prolix")
    assert "campaign_id" in out
    cool = tmp_catalog / "campaigns" / f"{out['campaign_id']}.json"
    assert cool.is_file()
    assert json.loads(cool.read_text())["name"] == "from-mcp"


def test_compact_seq_position_oldest_first(tmp_catalog: Path, tmp_path: Path):
    from tests.test_campaigns_popper import _write_popper_sidecar

    sidecar = _write_popper_sidecar(tmp_path, null=0.5, alt=0.9, threshold=20.0)
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        campaign = create_campaign(
            db, name="seq2", project_slug="prolix", mode="sequential", catalog_dir=tmp_catalog
        )
    finally:
        db.close()
    older = Run(
        project_slug="prolix",
        command="python x.py",
        argv=["python", "x.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 8, 1, tzinfo=UTC),
        status="completed",
        exit_code=0,
        outcome="pass",
        sidecar_path=str(sidecar),
        campaign_id=campaign.id,
    )
    newer = Run(
        project_slug="prolix",
        command="python y.py",
        argv=["python", "y.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 8, 21, tzinfo=UTC),
        status="completed",
        exit_code=0,
        outcome="pass",
        sidecar_path=str(sidecar),
        campaign_id=campaign.id,
    )
    write_run(newer, tmp_catalog)
    write_run(older, tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        rows = db.execute(
            "SELECT run_id, seq_position, evalue FROM campaign_runs WHERE campaign_id = ? ORDER BY seq_position",
            [campaign.id],
        ).fetchall()
        assert rows[0][0] == older.id
        assert rows[0][1] == 1
        assert rows[1][0] == newer.id
        assert rows[1][1] == 2
        thresh = db.execute(
            "SELECT stopping_threshold FROM campaigns WHERE id = ?", [campaign.id]
        ).fetchone()[0]
        assert thresh == 20.0
        payload = json.loads((tmp_catalog / "campaigns" / f"{campaign.id}.json").read_text())
        assert payload["stopping_threshold"] == 20.0
    finally:
        db.close()


def test_compact_missing_sidecar_leaves_evalue_null(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        campaign = create_campaign(
            db, name="seq-miss", project_slug="prolix", mode="sequential", catalog_dir=tmp_catalog
        )
    finally:
        db.close()
    write_run(
        Run(
            project_slug="prolix",
            command="python x.py",
            argv=["python", "x.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            timestamp=datetime(2026, 8, 21, tzinfo=UTC),
            status="completed",
            exit_code=0,
            outcome="pass",
            sidecar_path="/no/such/sidecar.bth.toml",
            campaign_id=campaign.id,
        ),
        tmp_catalog,
    )
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        row = db.execute(
            "SELECT evalue FROM campaign_runs WHERE campaign_id = ?", [campaign.id]
        ).fetchone()
        assert row is not None
        assert row[0] is None
    finally:
        db.close()


def test_conclude_json_only_persists(tmp_catalog: Path):
    from bathos.campaigns import conclude_campaign, prepare_catalog_for_conclude

    cid = "cd1f47e8-aaaa-bbbb-cccc-ddddeeeeffff"
    write_campaign_cool(
        Campaign(
            id=cid,
            project_slug="prolix",
            name="json-conclude",
            mode="exploration",
            status="open",
            started_at="2026-08-21T00:00:00+00:00",
        ),
        tmp_catalog,
    )
    write_run(
        Run(
            project_slug="prolix",
            command="python x.py",
            argv=["python", "x.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            timestamp=datetime(2026, 8, 21, tzinfo=UTC),
            status="completed",
            exit_code=0,
            outcome="pass",
            campaign_id=cid,
        ),
        tmp_catalog,
    )
    prepare_catalog_for_conclude(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        conclude_campaign(db, cid, "success", "done", catalog_dir=tmp_catalog)
        row = db.execute(
            "SELECT status, outcome_label FROM campaigns WHERE id = ?", [cid]
        ).fetchone()
        assert row == ("concluded", "success")
    finally:
        db.close()
    payload = json.loads((tmp_catalog / "campaigns" / f"{cid}.json").read_text())
    assert payload["status"] == "concluded"


def test_mcp_review_without_bathos_db(tmp_catalog: Path):
    from bathos.mcp import campaign_review_tool

    cid = "dd1f47e8-aaaa-bbbb-cccc-ddddeeeeffff"
    write_campaign_cool(
        Campaign(
            id=cid,
            project_slug="prolix",
            name="mcp-review",
            mode="exploration",
            status="open",
            started_at="2026-08-21T00:00:00+00:00",
        ),
        tmp_catalog,
    )
    write_run(
        Run(
            project_slug="prolix",
            command="python x.py",
            argv=["python", "x.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            timestamp=datetime(2026, 8, 21, tzinfo=UTC),
            status="completed",
            exit_code=0,
            outcome="pass",
            campaign_id=cid,
        ),
        tmp_catalog,
    )
    out = campaign_review_tool(campaign_id=cid, catalog_dir=str(tmp_catalog))
    assert "error" not in out
    assert out.get("total_runs") == 1


def test_popper_gap_not_no_runs_yet():
    from io import StringIO as _S

    from rich.console import Console

    from bathos.rich_fmt import render_popper_summary

    buf = _S()
    console = Console(file=buf, width=100, color_system=None, force_terminal=False)
    render_popper_summary(
        {
            "mode": "sequential",
            "stopping_threshold": 20.0,
            "threshold_met": False,
            "scripts": [],
            "gap": "evalues_unavailable",
        },
        console=console,
    )
    text = buf.getvalue()
    assert "evalues_unavailable" in text
    assert "NO RUNS YET" not in text


def _run(**kwargs) -> Run:
    defaults = dict(
        project_slug="prolix",
        command="python x.py",
        argv=["python", "x.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 8, 21, tzinfo=UTC),
        status="completed",
        exit_code=0,
        outcome="pass",
    )
    defaults.update(kwargs)
    return Run(**defaults)


def test_prepare_compacts_when_cool_ahead_of_warm(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    write_run(_run(timestamp=datetime(2026, 8, 1, tzinfo=UTC)), tmp_catalog)
    compact(tmp_catalog)
    cid = "ee1f47e8-aaaa-bbbb-cccc-ddddeeeeffff"
    write_campaign_cool(
        Campaign(
            id=cid,
            project_slug="prolix",
            name="ahead",
            mode="confirmation",
            status="open",
            started_at="2026-08-21T00:00:00+00:00",
        ),
        tmp_catalog,
    )
    new_run = _run(campaign_id=cid, timestamp=datetime(2026, 8, 21, tzinfo=UTC))
    write_run(new_run, tmp_catalog)
    prepare_catalog_for_conclude(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        found = db.execute("SELECT 1 FROM runs WHERE id = ?", [new_run.id]).fetchone()
        joined = db.execute(
            """
            SELECT COUNT(*) FROM campaign_runs cr
            INNER JOIN runs r ON cr.run_id = r.id
            WHERE cr.campaign_id = ?
            """,
            [cid],
        ).fetchone()[0]
        assert found is not None
        assert joined >= 1
    finally:
        db.close()


def test_ingest_warm_wins_claim_threshold_and_concluded(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        campaign = create_campaign(
            db,
            name="locked",
            project_slug="prolix",
            mode="sequential",
            catalog_dir=tmp_catalog,
        )
        db.execute(
            "UPDATE campaigns SET claim_sha256 = ?, claim_path = ?, stopping_threshold = ?, "
            "status = 'concluded', concluded_at = ?, outcome_label = 'success', conclusion = 'done' "
            "WHERE id = ?",
            ["warmhash", ".bth/claims/a.toml", 20.0, "2026-08-20T00:00:00+00:00", campaign.id],
        )
        db.commit()
        payload = json.loads((tmp_catalog / "campaigns" / f"{campaign.id}.json").read_text())
        payload["claim_sha256"] = "coolhash"
        payload["claim_path"] = ".bth/claims/b.toml"
        payload["stopping_threshold"] = 1.0
        payload["status"] = "open"
        payload["conclusion"] = None
        payload["name"] = "still-open-name"
        (tmp_catalog / "campaigns" / f"{campaign.id}.json").write_text(json.dumps(payload) + "\n")
        ingest_cool_campaigns(db, tmp_catalog)
        row = db.execute(
            "SELECT claim_sha256, stopping_threshold, status, outcome_label, name FROM campaigns WHERE id = ?",
            [campaign.id],
        ).fetchone()
        assert row[0] == "warmhash"
        assert row[1] == 20.0
        assert row[2] == "concluded"
        assert row[3] == "success"
        assert row[4] == "still-open-name"
    finally:
        db.close()


def test_seq_position_recomputed_after_older_run_arrives(tmp_catalog: Path, tmp_path: Path):
    from tests.test_campaigns_popper import _write_popper_sidecar

    sidecar = _write_popper_sidecar(tmp_path, null=0.5, alt=0.9, threshold=20.0)
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        campaign = create_campaign(
            db, name="seq-rank", project_slug="prolix", mode="sequential", catalog_dir=tmp_catalog
        )
    finally:
        db.close()
    newer = _run(
        campaign_id=campaign.id,
        timestamp=datetime(2026, 8, 21, tzinfo=UTC),
        sidecar_path=str(sidecar),
        command="python new.py",
        argv=["python", "new.py"],
    )
    write_run(newer, tmp_catalog)
    compact(tmp_catalog)
    older = _run(
        campaign_id=campaign.id,
        timestamp=datetime(2026, 8, 1, tzinfo=UTC),
        sidecar_path=str(sidecar),
        command="python old.py",
        argv=["python", "old.py"],
    )
    write_run(older, tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        rows = {
            r[0]: r[1]
            for r in db.execute(
                "SELECT run_id, seq_position FROM campaign_runs WHERE campaign_id = ?",
                [campaign.id],
            ).fetchall()
        }
        assert rows[older.id] == 1
        assert rows[newer.id] == 2
    finally:
        db.close()


def test_compact_raises_on_threshold_mismatch(tmp_catalog: Path, tmp_path: Path):
    from tests.test_campaigns_popper import _write_popper_sidecar

    s20 = _write_popper_sidecar(tmp_path, null=0.5, alt=0.9, threshold=20.0)
    s5 = _write_popper_sidecar(tmp_path, null=0.5, alt=0.9, threshold=5.0)
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        campaign = create_campaign(
            db, name="seq-lock", project_slug="prolix", mode="sequential", catalog_dir=tmp_catalog
        )
    finally:
        db.close()
    write_run(_run(campaign_id=campaign.id, sidecar_path=str(s20)), tmp_catalog)
    compact(tmp_catalog)
    write_run(
        _run(
            campaign_id=campaign.id,
            sidecar_path=str(s5),
            timestamp=datetime(2026, 8, 22, tzinfo=UTC),
            command="python z.py",
            argv=["python", "z.py"],
        ),
        tmp_catalog,
    )
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        with pytest.raises(CampaignError, match="stopping_threshold"):
            from bathos.catalog import read_runs

            link_cool_runs_to_campaigns(
                db,
                read_runs(tmp_catalog),
                catalog_dir=tmp_catalog,
                campaign_id=campaign.id,
            )
    finally:
        db.close()


def test_prepare_and_add_cool_only_campaign(tmp_catalog: Path):
    cid = "ff1f47e8-aaaa-bbbb-cccc-ddddeeeeffff"
    write_campaign_cool(
        Campaign(
            id=cid,
            project_slug="prolix",
            name="add-me",
            mode="exploration",
            status="open",
            started_at="2026-08-21T00:00:00+00:00",
        ),
        tmp_catalog,
    )
    run = _run(campaign_id=None)
    write_run(run, tmp_catalog)
    prepare_catalog_for_conclude(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        add_run_to_campaign(db, cid, run.id, catalog_dir=tmp_catalog)
        row = db.execute(
            "SELECT 1 FROM campaign_runs WHERE campaign_id = ? AND run_id = ?",
            [cid, run.id],
        ).fetchone()
        assert row is not None
    finally:
        db.close()


def test_register_claim_writes_cool_json(tmp_catalog: Path, tmp_path: Path):
    from bathos.claim import register_claim

    claim_path = tmp_path / "test.claim.toml"
    claim_path.write_text(
        """[claim]
headline = "Test claim"
kill_condition = "Outcome != expected"
kill_condition_satisfiable_by_null = false
regime = "param=1.0..2.0"

[[hypotheses]]
id = "H_primary"
label = "Primary hypothesis"
predicted_signature = "metric=100"

[[hypotheses]]
id = "H_null"
label = "Null hypothesis"
predicted_signature = "metric=50"

[[assumptions]]
id = "A1"
label = "Test assumption"

[[confounds]]
id = "C1"
label = "Test confound"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "main"
predicted_outcome = "discriminates"

[claim.union_gate]
[[claim.union_gate.clauses]]
id = "C_main"
description = "Main clause"
hypothesis_ids = ["H_primary", "H_null"]
"""
    )
    cid = "aa1f47e8-aaaa-bbbb-cccc-ddddeeeeffff"
    write_campaign_cool(
        Campaign(
            id=cid,
            project_slug="prolix",
            name="claim-me",
            mode="confirmation",
            status="open",
            started_at="2026-08-21T00:00:00+00:00",
        ),
        tmp_catalog,
    )
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        db.execute("DELETE FROM campaigns WHERE id = ?", [cid])
        db.commit()
        register_claim(Path("test.claim.toml"), cid, db, tmp_path, catalog_dir=tmp_catalog)
        sha = db.execute("SELECT claim_sha256 FROM campaigns WHERE id = ?", [cid]).fetchone()[0]
        assert sha
        payload = json.loads((tmp_catalog / "campaigns" / f"{cid}.json").read_text())
        assert payload["claim_sha256"] == sha
    finally:
        db.close()


def test_register_claim_raises_without_warm_or_json(tmp_catalog: Path, tmp_path: Path):
    from bathos.claim import register_claim

    (tmp_path / "test.claim.toml").write_text("headline = 'x'\n")
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        with pytest.raises(RuntimeError, match="Campaign not found"):
            register_claim(
                Path("test.claim.toml"),
                "00000000-0000-0000-0000-000000000000",
                db,
                tmp_path,
                catalog_dir=tmp_catalog,
            )
    finally:
        db.close()


def test_runner_prefix_ambiguous_with_warm_db(tmp_catalog: Path, capsys):
    import sys

    from bathos.runner import run_script

    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    prefix = "bb111111"
    id_cool = prefix + "-bbbb-cccc-dddd-000000000001"
    id_warm = prefix + "-bbbb-cccc-dddd-000000000002"
    write_campaign_cool(
        Campaign(
            id=id_cool,
            project_slug="prolix",
            name="cool",
            mode="exploration",
            status="open",
            started_at="2026-08-21T00:00:00+00:00",
        ),
        tmp_catalog,
    )
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        db.execute(
            "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at) "
            "VALUES (?, 'prolix', 'warm', 'exploration', 'open', '2026-08-21T00:00:00+00:00')",
            [id_warm],
        )
        db.commit()
    finally:
        db.close()
    code = run_script(
        argv=[sys.executable, "-c", "pass"],
        project_slug="prolix",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
        campaign_id=prefix,
    )
    assert code != 0
    captured = capsys.readouterr()
    assert "Ambiguous" in captured.err


def test_compact_continues_when_other_campaign_mismatches(tmp_catalog: Path, tmp_path: Path):
    from tests.test_campaigns_popper import _write_popper_sidecar

    s20 = _write_popper_sidecar(tmp_path, null=0.5, alt=0.9, threshold=20.0)
    s5 = _write_popper_sidecar(tmp_path, null=0.5, alt=0.9, threshold=5.0)
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        ok = create_campaign(
            db, name="ok-seq", project_slug="prolix", mode="sequential", catalog_dir=tmp_catalog
        )
        bad = create_campaign(
            db, name="bad-seq", project_slug="prolix", mode="sequential", catalog_dir=tmp_catalog
        )
    finally:
        db.close()
    write_run(_run(campaign_id=ok.id, sidecar_path=str(s20), command="python ok.py"), tmp_catalog)
    write_run(_run(campaign_id=bad.id, sidecar_path=str(s20), command="python bad.py"), tmp_catalog)
    compact(tmp_catalog)
    write_run(
        _run(
            campaign_id=bad.id,
            sidecar_path=str(s5),
            timestamp=datetime(2026, 8, 22, tzinfo=UTC),
            command="python bad2.py",
            argv=["python", "bad2.py"],
        ),
        tmp_catalog,
    )
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        ev = db.execute(
            "SELECT evalue FROM campaign_runs WHERE campaign_id = ? AND evalue IS NOT NULL",
            [ok.id],
        ).fetchone()
        assert ev is not None
        conclude_campaign(db, ok.id, "success", "ok", catalog_dir=tmp_catalog)
        with pytest.raises(CampaignError, match="stopping_threshold"):
            from bathos.catalog import read_runs

            link_cool_runs_to_campaigns(
                db, read_runs(tmp_catalog), catalog_dir=tmp_catalog, campaign_id=bad.id
            )
    finally:
        db.close()


def test_seq_position_includes_campaign_add_membership(tmp_catalog: Path, tmp_path: Path):
    from tests.test_campaigns_popper import _write_popper_sidecar

    sidecar = _write_popper_sidecar(tmp_path, null=0.5, alt=0.9, threshold=20.0)
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        campaign = create_campaign(
            db, name="mix", project_slug="prolix", mode="sequential", catalog_dir=tmp_catalog
        )
    finally:
        db.close()
    older = _run(
        campaign_id=campaign.id,
        sidecar_path=str(sidecar),
        timestamp=datetime(2026, 8, 1, tzinfo=UTC),
    )
    newer = _run(
        campaign_id=None,
        sidecar_path=str(sidecar),
        timestamp=datetime(2026, 8, 21, tzinfo=UTC),
        command="python add.py",
        argv=["python", "add.py"],
    )
    write_run(older, tmp_catalog)
    write_run(newer, tmp_catalog)
    prepare_catalog_for_conclude(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        add_run_to_campaign(db, campaign.id, newer.id, catalog_dir=tmp_catalog)
    finally:
        db.close()
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        rows = {
            r[0]: r[1]
            for r in db.execute(
                "SELECT run_id, seq_position FROM campaign_runs WHERE campaign_id = ?",
                [campaign.id],
            ).fetchall()
        }
        assert rows[older.id] == 1
        assert rows[newer.id] == 2
        assert len(rows) == 2
    finally:
        db.close()


def test_get_and_list_overlay_cool_json(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        campaign = create_campaign(
            db, name="stale", project_slug="prolix", mode="confirmation", catalog_dir=tmp_catalog
        )
        db.execute(
            "UPDATE campaigns SET claim_sha256 = ?, claim_path = ? WHERE id = ?",
            ["warmhash", ".bth/claims/a.toml", campaign.id],
        )
        db.commit()
        payload = json.loads((tmp_catalog / "campaigns" / f"{campaign.id}.json").read_text())
        payload["status"] = "concluded"
        payload["name"] = "from-cool"
        payload["claim_sha256"] = "coolhash"
        (tmp_catalog / "campaigns" / f"{campaign.id}.json").write_text(json.dumps(payload) + "\n")
        got = get_campaign(db, campaign.id, catalog_dir=tmp_catalog)
        assert got is not None
        assert got.status == "concluded"
        assert got.name == "from-cool"
        assert got.claim_sha256 == "warmhash"
        listed = list_campaigns(db, catalog_dir=tmp_catalog)
        match = next(c for c in listed if c.id == campaign.id)
        assert match.status == "concluded"
        assert match.claim_sha256 == "warmhash"
    finally:
        db.close()


def test_conclude_after_parquet_ahead(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    write_run(_run(timestamp=datetime(2026, 8, 1, tzinfo=UTC)), tmp_catalog)
    compact(tmp_catalog)
    cid = "cc1f47e8-aaaa-bbbb-cccc-ddddeeeeffff"
    write_campaign_cool(
        Campaign(
            id=cid,
            project_slug="prolix",
            name="ahead-conclude",
            mode="exploration",
            status="open",
            started_at="2026-08-21T00:00:00+00:00",
        ),
        tmp_catalog,
    )
    new_run = _run(campaign_id=cid, timestamp=datetime(2026, 8, 21, tzinfo=UTC))
    write_run(new_run, tmp_catalog)
    prepare_catalog_for_conclude(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        conclude_campaign(db, cid, "success", "done", catalog_dir=tmp_catalog)
        joined = db.execute(
            """
            SELECT COUNT(*) FROM campaign_runs cr
            INNER JOIN runs r ON cr.run_id = r.id
            WHERE cr.campaign_id = ?
            """,
            [cid],
        ).fetchone()[0]
        assert joined >= 1
        status = db.execute("SELECT status FROM campaigns WHERE id = ?", [cid]).fetchone()[0]
        assert status == "concluded"
    finally:
        db.close()


def test_ingest_preserves_warm_negative_check(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        campaign = create_campaign(
            db, name="hedged", project_slug="prolix", mode="exploration", catalog_dir=tmp_catalog
        )
        db.execute(
            "UPDATE campaigns SET negative_check = ? WHERE id = ?",
            ["local-hedge", campaign.id],
        )
        db.commit()
        payload = json.loads((tmp_catalog / "campaigns" / f"{campaign.id}.json").read_text())
        payload["negative_check"] = None
        (tmp_catalog / "campaigns" / f"{campaign.id}.json").write_text(json.dumps(payload) + "\n")
        ingest_cool_campaigns(db, tmp_catalog)
        row = db.execute(
            "SELECT negative_check FROM campaigns WHERE id = ?", [campaign.id]
        ).fetchone()
        assert row[0] == "local-hedge"
        got = get_campaign(db, campaign.id, catalog_dir=tmp_catalog)
        assert got is not None
        assert got.negative_check == "local-hedge"
    finally:
        db.close()


def test_get_campaign_overlay_matches_emit_lookup(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        campaign = create_campaign(
            db,
            name="emit-stale",
            project_slug="prolix",
            mode="exploration",
            catalog_dir=tmp_catalog,
        )
        payload = json.loads((tmp_catalog / "campaigns" / f"{campaign.id}.json").read_text())
        payload["status"] = "concluded"
        (tmp_catalog / "campaigns" / f"{campaign.id}.json").write_text(json.dumps(payload) + "\n")
        got = get_campaign(db, campaign.id, catalog_dir=tmp_catalog)
        assert got is not None
        assert got.status == "concluded"
        listed = list_campaigns(db, project_slug="prolix", status="open", catalog_dir=tmp_catalog)
        assert all(c.id != campaign.id for c in listed)
    finally:
        db.close()


def test_get_campaign_cool_only_without_warm_db(tmp_catalog: Path):
    cid = "dd1f47e8-aaaa-bbbb-cccc-ddddeeeeffff"
    write_campaign_cool(
        Campaign(
            id=cid,
            project_slug="prolix",
            name="cool-only-pm",
            mode="exploration",
            status="open",
            started_at="2026-08-21T00:00:00+00:00",
        ),
        tmp_catalog,
    )
    got = get_campaign(None, cid, catalog_dir=tmp_catalog)
    assert got is not None
    assert got.name == "cool-only-pm"


def test_claim_register_sync_closes_db_on_failure(tmp_catalog: Path, tmp_path: Path):
    from bathos.mcp import _claim_register_sync

    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    with patch("bathos.claim.register_claim", side_effect=RuntimeError("boom")):
        result = _claim_register_sync(
            "test.claim.toml",
            "aa1f47e8-aaaa-bbbb-cccc-ddddeeeeffff",
            tmp_catalog,
            tmp_path,
            force=False,
        )
    assert result["ok"] is False
    con = duckdb.connect(str(tmp_catalog / "bathos.db"), read_only=False)
    try:
        con.execute("SELECT 1").fetchone()
    finally:
        con.close()


def test_check_sha_rejects_path_escape(tmp_path: Path):
    from bathos.claim import check_sha

    workspace = tmp_path / "ws"
    workspace.mkdir()
    secret = tmp_path / "secret.toml"
    secret.write_text("stolen")
    sha = __import__("hashlib").sha256(b"stolen").hexdigest()
    with pytest.raises((RuntimeError, ValueError)):
        check_sha("../secret.toml", sha, workspace)


def test_check_sha_accepts_workspace_relative(tmp_path: Path):
    from bathos.claim import check_sha

    claim = tmp_path / "ok.claim.toml"
    claim.write_text("ok")
    sha = __import__("hashlib").sha256(b"ok").hexdigest()
    check_sha("ok.claim.toml", sha, tmp_path)


def test_conclude_rejects_escaping_claim_path(tmp_catalog: Path, tmp_path: Path):
    import hashlib

    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    claim_body = """[claim]
headline = "Test claim"
kill_condition = "Outcome != expected"
kill_condition_satisfiable_by_null = false
regime = "param=1.0..2.0"

[[hypotheses]]
id = "H_primary"
label = "Primary hypothesis"
predicted_signature = "metric=100"

[[hypotheses]]
id = "H_null"
label = "Null hypothesis"
predicted_signature = "metric=50"

[[assumptions]]
id = "A1"
label = "Test assumption"

[[confounds]]
id = "C1"
label = "Test confound"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "main"
predicted_outcome = "discriminates"

[claim.union_gate]
[[claim.union_gate.clauses]]
id = "C_main"
description = "Main clause"
hypothesis_ids = ["H_primary", "H_null"]
"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    secret = tmp_path / "outside.claim.toml"
    secret.write_text(claim_body)
    sha = hashlib.sha256(secret.read_bytes()).hexdigest()
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        campaign = create_campaign(
            db,
            name="escape-claim",
            project_slug="prolix",
            mode="confirmation",
            catalog_dir=tmp_catalog,
        )
        db.execute(
            "UPDATE campaigns SET claim_path = ?, claim_sha256 = ? WHERE id = ?",
            ["../outside.claim.toml", sha, campaign.id],
        )
        db.commit()
        with pytest.raises(RuntimeError, match="workspace"):
            conclude_campaign(
                db,
                campaign.id,
                "success",
                "done",
                catalog_dir=tmp_catalog,
                workspace_root=workspace,
            )
    finally:
        db.close()


def test_registered_claim_conclude_after_parquet_ahead(tmp_catalog: Path, tmp_path: Path):
    from bathos.claim import register_claim

    claim_path = tmp_path / "test.claim.toml"
    claim_path.write_text(
        """[claim]
headline = "Test claim"
kill_condition = "Outcome != expected"
kill_condition_satisfiable_by_null = false
regime = "param=1.0..2.0"

[[hypotheses]]
id = "H_primary"
label = "Primary hypothesis"
predicted_signature = "metric=100"

[[hypotheses]]
id = "H_null"
label = "Null hypothesis"
predicted_signature = "metric=50"

[[assumptions]]
id = "A1"
label = "Test assumption"

[[confounds]]
id = "C1"
label = "Test confound"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "main"
predicted_outcome = "discriminates"

[claim.union_gate]
[[claim.union_gate.clauses]]
id = "C_main"
description = "Main clause"
hypothesis_ids = ["H_primary", "H_null"]
"""
    )
    init_catalog(tmp_catalog)
    compact(tmp_catalog)
    write_run(_run(timestamp=datetime(2026, 8, 1, tzinfo=UTC)), tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        campaign = create_campaign(
            db,
            name="ahead-claim",
            project_slug="prolix",
            mode="confirmation",
            catalog_dir=tmp_catalog,
        )
        cid = campaign.id
    finally:
        db.close()
    new_run = _run(campaign_id=cid, timestamp=datetime(2026, 8, 21, tzinfo=UTC))
    write_run(new_run, tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        register_claim(Path("test.claim.toml"), cid, db, tmp_path, catalog_dir=tmp_catalog)
    finally:
        db.close()
    prepare_catalog_for_conclude(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        with suppress(Exception):
            conclude_campaign(
                db, cid, "success", "done", catalog_dir=tmp_catalog, workspace_root=tmp_path
            )
        joined = db.execute(
            """
            SELECT COUNT(*) FROM campaign_runs cr
            INNER JOIN runs r ON cr.run_id = r.id
            WHERE cr.campaign_id = ?
            """,
            [cid],
        ).fetchone()[0]
        assert joined >= 1
    finally:
        db.close()
