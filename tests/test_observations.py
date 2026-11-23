"""Tests for first-class observations (run-backed & run-less), status events, and folding (spec 261015, §3.3 & S3)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from bathos import mcp_auth
from bathos.appraisal import AppraisalValidationError, create_appraisal
from bathos.catalog import write_run
from bathos.cli_cyclopts import app
from bathos.errors import BathosErrorCode
from bathos.index import connect_read
from bathos.mcp import mcp_observe_tool
from bathos.observations import (
    ObservationNotFoundError,
    ObservationRunNotFoundError,
    ObservationValidationError,
    create_observation,
    get_observation,
    get_observation_status_history,
    list_observations,
    update_observation_status,
)
from bathos.runlog.emit import emit_event, unit_of_work
from bathos.runlog.fold_observations import fold_observation
from bathos.runlog.writer import reset_writers_for_test
from bathos.schema import Run
from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()


@pytest.fixture
def obs_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    (home / ".bth").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    reset_writers_for_test()

    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(cat_dir))
    monkeypatch.setenv("BTH_LOG_MODE", "1")

    proj_dir = tmp_path / "project"
    (proj_dir / ".bth").mkdir(parents=True, exist_ok=True)
    (proj_dir / ".bth.toml").write_text(
        f'[project]\nslug = "obs_test"\nroot = "{proj_dir}"\n'
        'id = "bbbbbbbb-1111-2222-3333-444444444444"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BTH_PROJECT_SLUG", "obs_test")
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(proj_dir))
    for var in (
        "CLAUDE_CODE_SESSION_ID",
        "BTH_AGENT_SESSION",
        "BTH_AGENT_MODE",
        "CLAUDE_AGENT",
        "CLAUDE_AGENT_ID",
        "AGENT_NAME",
        "SUBAGENT_ID",
        "ANTIGRAVITY_AGENT",
    ):
        monkeypatch.delenv(var, raising=False)

    token_file = tmp_path / "mcp_token"
    monkeypatch.setenv("BTH_MCP_TOKEN_PATH", str(token_file))
    monkeypatch.setattr(mcp_auth, "_stdio_transport", False)
    token = mcp_auth.get_or_create_token()

    def seed_run(run_id: str = "run_1111222233334444", output_paths: list[str] | None = None) -> Run:
        r = Run(
            id=run_id,
            project_slug="obs_test",
            command="python scripts/experiments/explore_mi.py",
            argv=["python", "scripts/experiments/explore_mi.py"],
            git_hash="deadbeef" * 5,
            git_branch="main",
            git_dirty=False,
            status="completed",
            outcome="observed",
            epistemic_class="exploratory",
            output_paths=output_paths or [],
        )
        with unit_of_work(cat_dir):
            emit_event(
                kind="run.started",
                entity=[r.id],
                data={
                    "id": r.id,
                    "project_slug": r.project_slug,
                    "command": r.command,
                    "git_hash": r.git_hash,
                    "git_branch": r.git_branch,
                    "timestamp": "2026-11-23T10:00:00Z",
                    "status": "running",
                    "epistemic_class": "exploratory",
                },
                cwd=proj_dir,
            )
            emit_event(
                kind="run.finished",
                entity=[r.id],
                data={
                    "id": r.id,
                    "status": "completed",
                    "outcome": "observed",
                    "exit_code": 0,
                    "output_paths": r.output_paths,
                },
                cwd=proj_dir,
            )
        return r

    yield {
        "tmp": tmp_path,
        "cat": cat_dir,
        "proj": proj_dir,
        "token": token,
        "seed_run": seed_run,
    }
    reset_writers_for_test()


def _appraisal_payload(ref: str = "doi:10.1101/obs", **overrides) -> dict:
    base = {
        "ref": ref,
        "locator": "Fig 3B",
        "appraiser": "agent:claude-opus-5-5",
        "read_depth": "methods",
        "scope_claimed": "General allostery across PDZ domains",
        "scope_supported": "PDZ3 (1BE9) single ligand 3x100ns",
        "for": [
            {
                "id": "f1",
                "category": "multi-seed-uncertainty",
                "statement": "Three independent replicas per state with bootstrap intervals shown.",
                "basis": "inspected",
            }
        ],
        "against": [
            {
                "id": "a1",
                "category": "single-source",
                "statement": "No independent third-party replication was found in literature.",
                "basis": "inspected",
                "severity": "minor",
            }
        ],
    }
    base.update(overrides)
    return base


# ── Run-backed Observations ──────────────────────────────────────────────────


def test_run_backed_observation_links_output_sha_and_origin(obs_env, monkeypatch):
    fig = obs_env["proj"] / "outputs" / "mi_heatmap.png"
    fig.parent.mkdir(parents=True, exist_ok=True)
    fig.write_bytes(b"fake-png-bytes-1234")
    expected_sha = hashlib.sha256(b"fake-png-bytes-1234").hexdigest()

    run = obs_env["seed_run"]("run_aaaa1111bbbb2222", output_paths=["outputs/mi_heatmap.png"])

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-obs-agent")
    obs = create_observation(
        run_id="run_aaaa1111",  # prefix lookup
        text="MI hub at H71 in holo only; absent in 2 of 3 seeds",
        figure="mi_heatmap.png",
        tags=["hub", "pdz"],
        project_root=obs_env["proj"],
        catalog_dir=obs_env["cat"],
    )
    assert obs.id.startswith("obs_")
    assert obs.run_id == run.id
    assert obs.output_sha256 == expected_sha
    assert obs.status == "open"
    assert obs.origin == "agent"
    assert obs.agent_session == "sess-obs-agent"
    assert obs.unappraised is False
    assert obs.tags == ("hub", "pdz")

    con = connect_read(obs_env["cat"])
    try:
        row = con.execute(
            "SELECT id, run_id, output_sha256, status, origin FROM observations WHERE id = ?",
            [obs.id],
        ).fetchone()
        assert row == (obs.id, run.id, expected_sha, "open", "agent")
    finally:
        con.close()


def test_run_backed_cool_fallback_in_legacy_mode(obs_env, monkeypatch):
    monkeypatch.setenv("BTH_LOG_MODE", "0")
    fig = obs_env["proj"] / "cool_fig.png"
    fig.write_bytes(b"cool-bytes")
    cool_run = Run(
        id="run_cool99998888",
        project_slug="obs_test",
        command="python explore.py",
        argv=["python", "explore.py"],
        git_hash="cafebabe" * 5,
        git_branch="main",
        git_dirty=False,
        status="completed",
        outcome="observed",
        epistemic_class="exploratory",
        output_paths=[str(fig)],
    )
    write_run(cool_run, obs_env["cat"])

    obs = create_observation(
        run_id="run_cool99998888",
        text="Cool fragment run is observable before bth compact",
        figure=str(fig),
        project_root=obs_env["proj"],
        catalog_dir=obs_env["cat"],
    )
    assert obs.run_id == cool_run.id
    assert obs.output_sha256 == hashlib.sha256(b"cool-bytes").hexdigest()
    assert obs.origin == "human"
    assert (obs_env["cat"] / "observations" / "obs_test" / f"{obs.id}.parquet").is_file()

    # Status update in legacy mode
    update_observation_status(
        obs.id,
        "survived-seed-check",
        reason="checked 5 additional seeds",
        project_root=obs_env["proj"],
        catalog_dir=obs_env["cat"],
    )
    fetched = get_observation(obs.id, catalog_dir=obs_env["cat"])
    assert fetched is not None
    assert fetched.status == "survived-seed-check"


# ── Run-less Observations & Appraisal Gate (C25) ─────────────────────────────


def test_runless_observation_unappraised_and_appraised(obs_env, monkeypatch):
    # Without --appraisal: succeeds with status='open', unappraised=True
    unappr = create_observation(
        ref="doi:10.1101/2024.01.02.123456",
        locator="Fig 3B",
        claim="H71 shows holo-only coupling",
        value_provenance="figure-read",
        project_root=obs_env["proj"],
        catalog_dir=obs_env["cat"],
    )
    assert unappr.status == "open"
    assert unappr.unappraised is True
    assert unappr.locator == "Fig 3B"
    assert unappr.value_provenance == "figure-read"

    # Cannot claim uses_as without an appraisal
    with pytest.raises(ObservationValidationError, match="requires at least one"):
        create_observation(
            ref="doi:10.1101/2024.01.02.123456",
            locator="Fig 3B",
            claim="H71 shows holo-only coupling",
            uses_as="hypothesis-source",
            project_root=obs_env["proj"],
            catalog_dir=obs_env["cat"],
        )

    # Create an appraisal in another session
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-indep-appraiser")
    appr = create_appraisal(
        payload=_appraisal_payload(ref="doi:10.1101/2024.01.02.123456"),
        project_root=obs_env["proj"],
        catalog_dir=obs_env["cat"],
    )

    # Observer in citing session:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-observer")
    # figure-read CANNOT be uses_as='threshold-source' even with independent appraisal
    with pytest.raises(AppraisalValidationError, match="threshold-source"):
        create_observation(
            ref="doi:10.1101/2024.01.02.123456",
            locator="Fig 3B",
            claim="H71 shows holo-only coupling",
            value_provenance="figure-read",
            uses_as="threshold-source",
            appraisal_ids=[appr.id],
            project_root=obs_env["proj"],
            catalog_dir=obs_env["cat"],
        )

    # published + independent appraisal succeeds for threshold-source
    good = create_observation(
        ref="doi:10.1101/2024.01.02.123456",
        locator="Table 2",
        claim="H71 MI = 0.42 nats (SE 0.03)",
        quote="MI(H71, E40) = 0.42 +/- 0.03 nats",
        value_provenance="published",
        disposition="supports",
        bears_on="H1",
        uses_as="threshold-source",
        appraisal_ids=[appr.id],
        project_root=obs_env["proj"],
        catalog_dir=obs_env["cat"],
    )
    assert good.unappraised is False
    assert good.appraisal_ids == (appr.id,)
    assert good.uses_as == "threshold-source"
    assert good.quote_checked is False


# ── Supersedes vs Status Events (C11) ────────────────────────────────────────


def test_supersedes_does_not_conflate_with_status(obs_env):
    run = obs_env["seed_run"]()
    obs1 = create_observation(
        run_id=run.id,
        text="Initial misread: thought H71 peaked at 50ns",
        project_root=obs_env["proj"],
        catalog_dir=obs_env["cat"],
    )
    obs2 = create_observation(
        run_id=run.id,
        text="Corrected: H71 peaks at 120ns after fixing alignment",
        supersedes=obs1.id,
        project_root=obs_env["proj"],
        catalog_dir=obs_env["cat"],
    )
    # Superseding does NOT alter obs1.status
    o1_reloaded = get_observation(obs1.id, catalog_dir=obs_env["cat"])
    assert o1_reloaded is not None
    assert o1_reloaded.status == "open"
    assert o1_reloaded.superseded_by == (obs2.id,)

    # Status events update status independently
    update_observation_status(
        obs2.id,
        "refuted",
        reason="ablation showed artifact from box boundary",
        project_root=obs_env["proj"],
        catalog_dir=obs_env["cat"],
    )
    update_observation_status(
        obs2.id,
        "promoted",
        reason="re-tested on held-out system",
        project_root=obs_env["proj"],
        catalog_dir=obs_env["cat"],
    )
    o2_reloaded = get_observation(obs2.id, catalog_dir=obs_env["cat"])
    assert o2_reloaded is not None
    assert o2_reloaded.status == "promoted"

    hist = get_observation_status_history(obs2.id, catalog_dir=obs_env["cat"])
    assert [h.status for h in hist] == ["refuted", "promoted"]


# ── Negative & Mutation Tests ────────────────────────────────────────────────


def test_observation_negative_controls(obs_env):
    run = obs_env["seed_run"]()

    with pytest.raises(ObservationValidationError, match="not both"):
        create_observation(
            run_id=run.id,
            ref="doi:10.1/x",
            locator="Fig 1",
            text="ambiguous",
            project_root=obs_env["proj"],
            catalog_dir=obs_env["cat"],
        )

    with pytest.raises(ObservationValidationError, match="either 'run_id'"):
        create_observation(
            text="orphan",
            project_root=obs_env["proj"],
            catalog_dir=obs_env["cat"],
        )

    with pytest.raises(ObservationRunNotFoundError):
        create_observation(
            run_id="run_nonexistent",
            text="missing run",
            project_root=obs_env["proj"],
            catalog_dir=obs_env["cat"],
        )

    with pytest.raises(ObservationValidationError, match="locator"):
        create_observation(
            ref="doi:10.1/x",
            claim="missing locator",
            project_root=obs_env["proj"],
            catalog_dir=obs_env["cat"],
        )

    with pytest.raises(ObservationValidationError, match="value_provenance"):
        create_observation(
            ref="doi:10.1/x",
            locator="Fig 1",
            claim="bad prov",
            value_provenance="guessed",
            project_root=obs_env["proj"],
            catalog_dir=obs_env["cat"],
        )

    with pytest.raises(ObservationValidationError, match="does not exist"):
        create_observation(
            ref="doi:10.1/x",
            locator="Fig 1",
            claim="bad appraisal",
            appraisal_ids=["appr_missing"],
            project_root=obs_env["proj"],
            catalog_dir=obs_env["cat"],
        )

    with pytest.raises(ObservationNotFoundError):
        create_observation(
            run_id=run.id,
            text="bad supersedes",
            supersedes="obs_missing",
            project_root=obs_env["proj"],
            catalog_dir=obs_env["cat"],
        )

    obs = create_observation(
        run_id=run.id,
        text="valid obs",
        project_root=obs_env["proj"],
        catalog_dir=obs_env["cat"],
    )
    with pytest.raises(ObservationValidationError, match="Observation status"):
        update_observation_status(
            obs.id,
            "bogus-status",
            project_root=obs_env["proj"],
            catalog_dir=obs_env["cat"],
        )


# ── CLI & MCP Roundtrip ──────────────────────────────────────────────────────


def test_cli_observe_and_mcp_observe_roundtrip(obs_env):
    run = obs_env["seed_run"]("run_cli12345678")

    # Positional CLI syntax from spec §3.3
    res = runner.invoke(
        app,
        [
            "observe",
            run.id,
            "MI hub at H71 in holo only",
            "--tags",
            "hub",
            "--json",
            "--project-root",
            str(obs_env["proj"]),
            "--catalog-dir",
            str(obs_env["cat"]),
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["ok"] is True
    obs_id = data["observation"]["id"]

    # CLI status update + show + list
    st_res = runner.invoke(
        app,
        [
            "observe",
            "--status-of",
            obs_id,
            "--status",
            "refuted",
            "--reason",
            "seed check failed",
            "--json",
            "--project-root",
            str(obs_env["proj"]),
            "--catalog-dir",
            str(obs_env["cat"]),
        ],
    )
    assert st_res.exit_code == 0, st_res.output
    assert json.loads(st_res.output)["status_record"]["status"] == "refuted"

    list_res = runner.invoke(
        app,
        ["observe", "--list", "--json", "--catalog-dir", str(obs_env["cat"])],
    )
    assert list_res.exit_code == 0
    assert len(json.loads(list_res.output)["observations"]) == 1

    # MCP write token gate
    unauth = asyncio.run(
        mcp_observe_tool(
            run_id=run.id,
            text="unauthorized mcp obs",
            project_root=str(obs_env["proj"]),
            catalog_dir=str(obs_env["cat"]),
            token="",
        )
    )
    assert unauth["ok"] is False
    assert unauth["error_code"] == BathosErrorCode.AUTH_ERROR.value

    auth_ok = asyncio.run(
        mcp_observe_tool(
            run_id=run.id,
            text="authorized mcp observation",
            project_root=str(obs_env["proj"]),
            catalog_dir=str(obs_env["cat"]),
            token=obs_env["token"],
        )
    )
    assert auth_ok["ok"] is True
    assert auth_ok["observation"]["origin"] == "agent"
    assert len(list_observations(catalog_dir=obs_env["cat"])) == 2


def test_fold_observation_idempotent_and_ordered():
    created = {
        "v": 1,
        "eid": "eid-c1",
        "kind": "observation.created",
        "entity": ["obs_1"],
        "ts": "2026-11-23T10:00:00Z",
        "project": "p1",
        "data": {"id": "obs_1", "run_id": "r1", "text": "saw hub", "status": "open"},
    }
    st2 = {
        "v": 1,
        "eid": "eid-s2",
        "kind": "observation.status",
        "entity": ["obs_1"],
        "ts": "2026-11-23T10:05:00Z",
        "project": "p1",
        "data": {"of": "obs_1", "status": "promoted"},
    }
    st1 = {
        "v": 1,
        "eid": "eid-s1",
        "kind": "observation.status",
        "entity": ["obs_1"],
        "ts": "2026-11-23T10:02:00Z",
        "project": "p1",
        "data": {"of": "obs_1", "status": "survived-seed-check"},
    }
    row, statuses = fold_observation([st2, created, st1, st2])
    assert row is not None
    assert row["status"] == "promoted"
    assert [s["eid"] for s in statuses] == ["eid-s1", "eid-s2"]
    assert fold_observation([]) == (None, [])
