"""Tests for Exploratory-mode contract S4 ([holdout], --from-observation) & S5 (calibration run sources, SESOI-vs-MDE)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bathos import mcp_auth
from bathos.appraisal import create_appraisal
from bathos.catalog import init_catalog, read_runs, write_run
from bathos.cli_cyclopts import app
from bathos.linter import (
    IssueSeverity,
    check_calibration_sources,
    check_holdout_and_observation_lint,
    lint_project,
)
from bathos.mcp import mcp_run_tool, new_experiment_tool, validate_sidecar_tool
from bathos.new_experiment import scaffold_experiment
from bathos.observations import (
    ObservationNotFoundError,
    create_observation,
    get_observation,
)
from bathos.runlog.emit import emit_event, unit_of_work
from bathos.runlog.writer import reset_writers_for_test
from bathos.runner import run_script
from bathos.schema import Run
from bathos.sidecar import (
    HoldoutSpec,
    Sidecar,
    SidecarError,
    holdout_rank,
    parse_sidecar,
)
from bathos.validate import validate_sidecar
from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()


@pytest.fixture
def s4_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
        f'[project]\nslug = "s4_test"\nroot = "{proj_dir}"\n'
        'id = "cccccccc-1111-2222-3333-444444444444"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BTH_PROJECT_SLUG", "s4_test")
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

    def seed_run(
        run_id: str,
        *,
        epistemic_class: str = "exploratory",
        stage_name: str = "exploration",
        outcome: str = "observed",
        exit_code: int = 0,
        argv: list[str] | None = None,
        script_sha256: str = "",
        sidecar_path: str = "",
        metadata: dict | None = None,
        output_paths: list[str] | None = None,
        timestamp: datetime | None = None,
    ) -> Run:
        ts = timestamp or datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)
        meta_str = json.dumps(metadata or {})
        r = Run(
            id=run_id,
            project_slug="s4_test",
            command=" ".join(argv or ["python", "scripts/experiments/explore_mi.py"]),
            argv=argv or ["python", "scripts/experiments/explore_mi.py"],
            git_hash="deadbeef" * 5,
            git_branch="main",
            git_dirty=False,
            timestamp=ts,
            status="completed" if exit_code == 0 else "failed",
            exit_code=exit_code,
            outcome=outcome,
            epistemic_class=epistemic_class,
            stage_name=stage_name,
            script_sha256=script_sha256,
            sidecar_path=sidecar_path,
            output_paths=output_paths or [],
            metadata=meta_str,
        )
        with unit_of_work(cat_dir):
            emit_event(
                kind="run.started",
                entity=[r.id],
                data={
                    "id": r.id,
                    "project_slug": r.project_slug,
                    "command": r.command,
                    "argv": r.argv,
                    "git_hash": r.git_hash,
                    "git_branch": r.git_branch,
                    "timestamp": ts.isoformat(),
                    "status": "running",
                    "epistemic_class": epistemic_class,
                    "stage_name": stage_name,
                    "script_sha256": script_sha256,
                    "sidecar_path": sidecar_path,
                },
                cwd=proj_dir,
            )
            emit_event(
                kind="run.finished",
                entity=[r.id],
                data={
                    "id": r.id,
                    "status": r.status,
                    "outcome": outcome,
                    "exit_code": exit_code,
                    "output_paths": r.output_paths,
                    "metadata": meta_str,
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


def _appraisal_payload(
    ref: str = "doi:10.1101/s4",
    *,
    severity: str = "minor",
    read_depth: str = "methods",
    for_basis: str = "inspected",
) -> dict:
    return {
        "ref": ref,
        "locator": "Fig 3B",
        "appraiser": "agent:claude-opus-5-5",
        "read_depth": read_depth,
        "scope_claimed": "General allostery across PDZ domains",
        "scope_supported": "PDZ3 (1BE9) single ligand 3x100ns",
        "for": [
            {
                "id": "f1",
                "category": "multi-seed-uncertainty",
                "statement": "Three independent replicas per state with bootstrap intervals shown.",
                "basis": for_basis,
            }
        ],
        "against": [
            {
                "id": "a1",
                "category": "single-source",
                "statement": "No independent third-party replication was found in literature.",
                "basis": "inspected",
                "severity": severity,
            }
        ],
    }


def _write_sidecar(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


# ── 1. HoldoutSpec parsing, rank & declaration roundtrip ─────────────────────


def test_parse_holdout_and_provenance_roundtrip(tmp_path: Path):
    sc_path = _write_sidecar(
        tmp_path / "confirm_x.bth.toml",
        """
        [experiment]
        stage_name = "validation"
        hypothesis = "H71 hub replicates on held-out builds"

        [provenance]
        derived_from_observation = "obs_abc123"
        track_packages = ["numpy"]

        [holdout]
        kind = "held_out_split"
        unit = "system"
        split_manifest = "data/split.json"
        description = "Pre-committed system split"

        [outcomes.pass]
        condition = "metric > 0.5"
        decision = "accept"
        reasoning = "above SESOI"
        source = "sesoi:0.5:minimum biologically meaningful delta"

        [outcomes.fail]
        condition = "metric <= 0.5"
        decision = "reject"
        reasoning = "residual"
        is_residual = true

        [result_schema]
        metric = "float"
        """,
    )
    sc = parse_sidecar(sc_path)
    assert sc.derived_from_observation == "obs_abc123"
    assert sc.holdout == HoldoutSpec(
        kind="held_out_split",
        unit="system",
        split_manifest="data/split.json",
        description="Pre-committed system split",
        attestation="",
    )
    assert holdout_rank(sc.holdout) == 2
    assert holdout_rank(sc.holdout, split_timing_ok=False) == 0
    assert holdout_rank(HoldoutSpec(kind="new_seeds")) == 0
    assert holdout_rank(HoldoutSpec(kind="new_builds")) == 1
    assert holdout_rank(HoldoutSpec(kind="held_out_split", unit="seed")) == 0
    assert holdout_rank(HoldoutSpec(kind="held_out_split", unit="build")) == 1
    assert holdout_rank(HoldoutSpec(kind="new_system")) == 3
    assert holdout_rank(HoldoutSpec(kind="literature_prediction")) == 3

    rehydrated = Sidecar.from_declaration(json.loads(json.dumps(sc.to_declaration())))
    assert rehydrated.derived_from_observation == "obs_abc123"
    assert rehydrated.holdout == sc.holdout

    bad_path = _write_sidecar(
        tmp_path / "bad_holdout.bth.toml",
        """
        [experiment]
        stage_name = "validation"
        hypothesis = "H"
        holdout = "not-a-table"
        """,
    )
    bad_path.write_text(
        '[experiment]\nstage_name = "validation"\nhypothesis = "H"\nholdout = "not-a-table"\n'
    )
    # Top-level holdout="not-a-table" must raise SidecarError
    bad_top = tmp_path / "bad_top.bth.toml"
    bad_top.write_text('holdout = "nope"\n[experiment]\nstage_name = "validation"\nhypothesis = "H"\n')
    with pytest.raises(SidecarError, match="\\[holdout\\] must be a table"):
        parse_sidecar(bad_top)


# ── 2. Validation rules for [holdout] & observation promotion ────────────────


def test_validate_holdout_negative_controls(s4_env, monkeypatch):
    proj = s4_env["proj"]
    cat = s4_env["cat"]
    exp_run = s4_env["seed_run"]("run_exp111122223333")
    run_obs = create_observation(
        run_id=exp_run.id,
        text="H71 hub in holo",
        project_root=proj,
        catalog_dir=cat,
    )
    unappr_obs = create_observation(
        ref="doi:10.1101/unappr",
        locator="Fig 1",
        claim="unappraised literature claim",
        value_provenance="figure-read",
        project_root=proj,
        catalog_dir=cat,
    )

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-indep")
    appr_ok = create_appraisal(
        payload=_appraisal_payload(ref="doi:10.1101/ok"),
        project_root=proj,
        catalog_dir=cat,
    )
    appr_contested = create_appraisal(
        payload=_appraisal_payload(ref="doi:10.1101/contested", severity="major"),
        project_root=proj,
        catalog_dir=cat,
    )
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-author")
    obs_contrast_only = create_observation(
        ref="doi:10.1101/ok",
        locator="Fig 2",
        claim="contrast context only",
        uses_as="contrast-context",
        appraisal_ids=[appr_ok.id],
        project_root=proj,
        catalog_dir=cat,
    )
    obs_contested = create_observation(
        ref="doi:10.1101/contested",
        locator="Fig 2",
        claim="contested claim",
        appraisal_ids=[appr_contested.id],
        project_root=proj,
        catalog_dir=cat,
    )

    base_outcomes = """
        [outcomes.pass]
        condition = "metric > 0.5"
        decision = "accept"
        reasoning = "above SESOI"
        source = "sesoi:0.5:meaningful"

        [outcomes.fail]
        condition = "metric <= 0.5"
        decision = "reject"
        reasoning = "residual"
        is_residual = true

        [result_schema]
        metric = "float"
    """

    # Missing [holdout] when derived_from_observation is set -> error
    p_no_holdout = _write_sidecar(
        proj / "no_holdout.bth.toml",
        f'[experiment]\nstage_name = "validation"\nhypothesis = "H"\n'
        f'[provenance]\nderived_from_observation = "{run_obs.id}"\n{base_outcomes}',
    )
    r = validate_sidecar(parse_sidecar(p_no_holdout), sidecar_path=p_no_holdout, catalog_dir=cat)
    assert not r.ok and any(e.field == "holdout" for e in r.errors)

    # Frame / time splits forbidden (C9)
    for bad_kind in ("frame_split", "time_split"):
        p_bad = _write_sidecar(
            proj / f"{bad_kind}.bth.toml",
            f'[experiment]\nstage_name = "validation"\nhypothesis = "H"\n'
            f'[provenance]\nderived_from_observation = "{run_obs.id}"\n'
            f'[holdout]\nkind = "{bad_kind}"\n{base_outcomes}',
        )
        r = validate_sidecar(parse_sidecar(p_bad), sidecar_path=p_bad, catalog_dir=cat)
        assert not r.ok and any(e.field == "holdout.kind" for e in r.errors)

    for bad_unit in ("frame", "time"):
        p_bad_u = _write_sidecar(
            proj / f"split_{bad_unit}.bth.toml",
            f'[experiment]\nstage_name = "validation"\nhypothesis = "H"\n'
            f'[provenance]\nderived_from_observation = "{run_obs.id}"\n'
            f'[holdout]\nkind = "held_out_split"\nunit = "{bad_unit}"\n{base_outcomes}',
        )
        r = validate_sidecar(parse_sidecar(p_bad_u), sidecar_path=p_bad_u, catalog_dir=cat)
        assert not r.ok and any(e.field == "holdout.unit" for e in r.errors)

    # held_out_split with unit='seed' ranks 0 and cannot promote (C9 / §8)
    p_seed_split = _write_sidecar(
        proj / "seed_split.bth.toml",
        f'[experiment]\nstage_name = "validation"\nhypothesis = "H"\n'
        f'[provenance]\nderived_from_observation = "{run_obs.id}"\n'
        f'[holdout]\nkind = "held_out_split"\nunit = "seed"\n{base_outcomes}',
    )
    r = validate_sidecar(parse_sidecar(p_seed_split), sidecar_path=p_seed_split, catalog_dir=cat)
    assert not r.ok and any("ranks 0" in e.message for e in r.errors)

    # new_seeds on confirmatory fails, but passes on exploratory kill-test
    p_new_seeds_conf = _write_sidecar(
        proj / "new_seeds_conf.bth.toml",
        f'[experiment]\nstage_name = "validation"\nhypothesis = "H"\n'
        f'[provenance]\nderived_from_observation = "{run_obs.id}"\n'
        f'[holdout]\nkind = "new_seeds"\n{base_outcomes}',
    )
    r = validate_sidecar(parse_sidecar(p_new_seeds_conf), sidecar_path=p_new_seeds_conf, catalog_dir=cat)
    assert not r.ok and any("ranks 0" in e.message for e in r.errors)

    p_new_seeds_exp = _write_sidecar(
        proj / "new_seeds_exp.bth.toml",
        f'[experiment]\nstage_name = "exploration"\nquestion = "kill test"\n'
        f'[observe]\noutputs = ["results.json"]\n'
        f'[provenance]\nderived_from_observation = "{run_obs.id}"\n'
        f'[holdout]\nkind = "new_seeds"\n[result_schema]\nmetric = "float"\n',
    )
    r = validate_sidecar(parse_sidecar(p_new_seeds_exp), sidecar_path=p_new_seeds_exp, catalog_dir=cat)
    assert r.ok, r.errors

    # literature_prediction on run-backed observation fails (C8)
    p_lit_run = _write_sidecar(
        proj / "lit_run.bth.toml",
        f'[experiment]\nstage_name = "validation"\nhypothesis = "H"\n'
        f'[provenance]\nderived_from_observation = "{run_obs.id}"\n'
        f'[holdout]\nkind = "literature_prediction"\n{base_outcomes}',
    )
    r = validate_sidecar(parse_sidecar(p_lit_run), sidecar_path=p_lit_run, catalog_dir=cat)
    assert not r.ok and any("literature_prediction" in e.message for e in r.errors)

    # Unappraised, contrast-context, and contested observations cannot promote
    for bad_obs, match in (
        (unappr_obs, "unappraised"),
        (obs_contrast_only, "contrast-context"),
        (obs_contested, "below 'hypothesis-source'"),
    ):
        p_obs = _write_sidecar(
            proj / f"bad_{bad_obs.id}.bth.toml",
            f'[experiment]\nstage_name = "validation"\nhypothesis = "H"\n'
            f'[provenance]\nderived_from_observation = "{bad_obs.id}"\n'
            f'[holdout]\nkind = "literature_prediction"\n{base_outcomes}',
        )
        r = validate_sidecar(parse_sidecar(p_obs), sidecar_path=p_obs, catalog_dir=cat)
        assert not r.ok and any(match in e.message for e in r.errors)

    # Self-sourcing: citing the exploratory run itself as threshold source fails
    p_self_src = _write_sidecar(
        proj / "self_src.bth.toml",
        f'[experiment]\nstage_name = "validation"\nhypothesis = "H"\n'
        f'[provenance]\nderived_from_observation = "{run_obs.id}"\n'
        f'[holdout]\nkind = "new_builds"\n'
        f'[outcomes.pass]\ncondition = "metric > 0.5"\ndecision = "go"\nreasoning = "r"\n'
        f'source = "run:{exp_run.id[:8]}:metric"\n'
        f'[outcomes.fail]\ncondition = "metric <= 0.5"\ndecision = "no"\nreasoning = "r"\n'
        f'is_residual = true\n[result_schema]\nmetric = "float"\n',
    )
    r = validate_sidecar(parse_sidecar(p_self_src), sidecar_path=p_self_src, catalog_dir=cat)
    assert not r.ok and any("optimistic selection bias" in e.message for e in r.errors)


# ── 3. Split timing (C10) ────────────────────────────────────────────────────


def test_split_timing_c10_git_and_run_provenance(s4_env):
    proj = s4_env["proj"]
    cat = s4_env["cat"]

    # Initialize git repo in proj
    subprocess.run(["git", "init"], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=proj, check=True)

    early_split = proj / "splits" / "early_split.json"
    early_split.parent.mkdir(parents=True, exist_ok=True)
    early_split.write_text('{"systems": ["1BE9"]}\n')
    env_early = os.environ.copy()
    env_early["GIT_AUTHOR_DATE"] = "2026-11-10T10:00:00Z"
    env_early["GIT_COMMITTER_DATE"] = "2026-11-10T10:00:00Z"
    subprocess.run(["git", "add", "splits/early_split.json"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-m", "early split"], cwd=proj, env=env_early, check=True, capture_output=True)

    late_split = proj / "splits" / "late_split.json"
    late_split.write_text('{"systems": ["1BE9"]}\n')
    env_late = os.environ.copy()
    env_late["GIT_AUTHOR_DATE"] = "2026-11-22T10:00:00Z"
    env_late["GIT_COMMITTER_DATE"] = "2026-11-22T10:00:00Z"
    subprocess.run(["git", "add", "splits/late_split.json"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-m", "late split"], cwd=proj, env=env_late, check=True, capture_output=True)

    untracked_split = proj / "splits" / "untracked.json"
    untracked_split.write_text('{"systems": ["1BE9"]}\n')

    # Source run at 2026-11-20; even if obs.created_at is later (2026-11-23), C10 compares against source run!
    src_run = s4_env["seed_run"](
        "run_src_timing_1111",
        timestamp=datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC),
    )
    obs = create_observation(
        run_id=src_run.id,
        text="Observed pattern on train split",
        project_root=proj,
        catalog_dir=cat,
    )

    # Also test split emitted by an earlier bth run
    run_split_file = proj / "outputs" / "run_split.json"
    run_split_file.parent.mkdir(parents=True, exist_ok=True)
    run_split_file.write_text('{"systems": ["2PDZ"]}\n')
    s4_env["seed_run"](
        "run_split_gen_0000",
        epistemic_class="confirmatory",
        stage_name="production",
        outcome="pass",
        output_paths=["outputs/run_split.json"],
        timestamp=datetime(2026, 11, 15, 8, 0, 0, tzinfo=UTC),
    )

    def _make_split_sidecar(name: str, manifest: str) -> Path:
        return _write_sidecar(
            proj / "scripts" / "experiments" / f"{name}.bth.toml",
            f"""
            [experiment]
            stage_name = "validation"
            hypothesis = "Replicates on held-out systems"
            novel = true

            [provenance]
            derived_from_observation = "{obs.id}"

            [holdout]
            kind = "held_out_split"
            unit = "system"
            split_manifest = "{manifest}"

            [outcomes.pass]
            condition = "metric > 0.5"
            decision = "accept"
            reasoning = "above SESOI"
            source = "sesoi:0.5:minimum meaningful effect"

            [outcomes.fail]
            condition = "metric <= 0.5"
            decision = "reject"
            reasoning = "residual"
            is_residual = true

            [result_schema]
            metric = "float"
            """,
        )

    # Early git commit passes
    p_early = _make_split_sidecar("confirm_early", "splits/early_split.json")
    r_early = validate_sidecar(parse_sidecar(p_early), sidecar_path=p_early, catalog_dir=cat)
    assert r_early.ok, r_early.errors

    # Earlier bth run output passes
    p_run_gen = _make_split_sidecar("confirm_rungen", "outputs/run_split.json")
    r_run_gen = validate_sidecar(parse_sidecar(p_run_gen), sidecar_path=p_run_gen, catalog_dir=cat)
    assert r_run_gen.ok, r_run_gen.errors

    # Late git commit (after source run, though before obs.created_at!) fails -> ranks 0
    p_late = _make_split_sidecar("confirm_late", "splits/late_split.json")
    r_late = validate_sidecar(parse_sidecar(p_late), sidecar_path=p_late, catalog_dir=cat)
    assert not r_late.ok and any("split ranks 0" in e.message for e in r_late.errors)

    # Untracked file (neither git nor bth run) fails -> ranks 0
    p_untracked = _make_split_sidecar("confirm_untracked", "splits/untracked.json")
    r_untracked = validate_sidecar(parse_sidecar(p_untracked), sidecar_path=p_untracked, catalog_dir=cat)
    assert not r_untracked.ok and any("split ranks 0" in e.message for e in r_untracked.errors)


# ── 4. Scaffolding (--from-observation) ──────────────────────────────────────


def test_scaffold_from_observation_cli_and_mcp(s4_env, monkeypatch):
    proj = s4_env["proj"]
    cat = s4_env["cat"]
    monkeypatch.chdir(proj)

    exp_run = s4_env["seed_run"](
        "run_exp_scaffold_1",
        metadata={"metric": 0.88, "n_samples": 50},
    )
    run_obs = create_observation(
        run_id=exp_run.id,
        text="H71 shows holo-only coupling at 0.88",
        project_root=proj,
        catalog_dir=cat,
    )
    unappr = create_observation(
        ref="doi:10.1101/unappr_scaf",
        locator="Fig 1",
        claim="unappraised",
        project_root=proj,
        catalog_dir=cat,
    )
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-indep")
    appr = create_appraisal(
        payload=_appraisal_payload(ref="doi:10.1101/ok_scaf"),
        project_root=proj,
        catalog_dir=cat,
    )
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-author")
    lit_obs = create_observation(
        ref="doi:10.1101/ok_scaf",
        locator="Fig 3B",
        claim="H71 coupling holds across builds",
        value_provenance="published",
        uses_as="hypothesis-source",
        appraisal_ids=[appr.id],
        project_root=proj,
        catalog_dir=cat,
    )

    # CLI new-experiment --from-observation --holdout-kind new_builds
    cli_res = runner.invoke(
        app,
        [
            "new-experiment",
            "confirm_h71_hub",
            "--from-observation",
            run_obs.id,
            "--holdout-kind",
            "new_builds",
            "--catalog-dir",
            str(cat),
        ],
    )
    assert cli_res.exit_code == 0, cli_res.output
    sc_path = proj / "scripts" / "experiments" / "confirm_h71_hub.bth.toml"
    sc = parse_sidecar(sc_path)
    assert sc.stage_name == "validation"
    assert sc.derived_from_observation == run_obs.id
    assert sc.holdout == HoldoutSpec(kind="new_builds")
    assert "H71 shows holo-only coupling" in sc.hypothesis
    assert "Observed exploratory metrics" in sc_path.read_text()

    # Rank-0 new_seeds scaffolds exploratory kill test
    kill_res = scaffold_experiment(
        "kill_h71_seeds",
        proj,
        from_observation=run_obs.id,
        holdout_kind="new_seeds",
        catalog_dir=cat,
    )
    assert kill_res.mode == "explore"
    assert kill_res.epistemic_class == "exploratory"
    assert kill_res.needs_filling == []
    kill_sc = parse_sidecar(kill_res.sidecar)
    assert kill_sc.holdout == HoldoutSpec(kind="new_seeds")

    # Rank-0 with mode='confirm' is refused
    with pytest.raises(ValueError, match="rank-0 holdout"):
        scaffold_experiment(
            "bad_kill_confirm",
            proj,
            mode="confirm",
            from_observation=run_obs.id,
            holdout_kind="new_seeds",
            catalog_dir=cat,
        )

    # Run-less appraised obs defaults to literature_prediction
    lit_out = new_experiment_tool(
        "confirm_lit_pred",
        workspace_root=str(proj),
        from_observation=lit_obs.id,
        catalog_dir=str(cat),
    )
    assert lit_out["ok"] is True
    assert lit_out["holdout_kind"] == "literature_prediction"
    assert lit_out["mode"] == "confirm"

    # Unappraised obs refused cleanly via MCP tool without writing files
    bad_out = new_experiment_tool(
        "bad_unappr",
        workspace_root=str(proj),
        from_observation=unappr.id,
        catalog_dir=str(cat),
    )
    assert bad_out["ok"] is False
    assert "unappraised" in bad_out["error"]
    assert not (proj / "scripts" / "experiments" / "bad_unappr.py").exists()

    # Negative: observation not found
    with pytest.raises(ObservationNotFoundError):
        scaffold_experiment("missing_obs", proj, from_observation="obs_doesnotexist", catalog_dir=cat)


# ── 5. Runner linkage, enforcement & status promotion ────────────────────────


def test_runner_observation_linkage_and_promotion(s4_env, monkeypatch):
    proj = s4_env["proj"]
    cat = s4_env["cat"]
    monkeypatch.chdir(proj)

    exp_run = s4_env["seed_run"]("run_exp_promo_1")
    obs = create_observation(
        run_id=exp_run.id,
        text="Hub persists in holo",
        project_root=proj,
        catalog_dir=cat,
    )
    obs_kill = create_observation(
        run_id=exp_run.id,
        text="Seed kill check target",
        project_root=proj,
        catalog_dir=cat,
    )
    unappr = create_observation(
        ref="doi:10.1101/unappr_run",
        locator="Table 1",
        claim="unappraised claim",
        project_root=proj,
        catalog_dir=cat,
    )

    marker = proj / "should_not_exist.txt"
    bad_script = proj / "scripts" / "experiments" / "refused_promo.py"
    bad_script.parent.mkdir(parents=True, exist_ok=True)
    bad_script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    _write_sidecar(
        proj / "scripts" / "experiments" / "refused_promo.bth.toml",
        f"""
        [experiment]
        stage_name = "validation"
        hypothesis = "Test unappraised"
        novel = true

        [provenance]
        derived_from_observation = "{unappr.id}"

        [holdout]
        kind = "literature_prediction"

        [outcomes.pass]
        condition = "metric > 0.5"
        decision = "go"
        reasoning = "r"
        source = "sesoi:0.5:meaningful"

        [outcomes.fail]
        condition = "metric <= 0.5"
        decision = "stop"
        reasoning = "r"
        is_residual = true

        [result_schema]
        metric = "float"
        """,
    )
    # Pre-execution refusal must prevent subprocess execution
    rc_bad = run_script(
        argv=[sys.executable, str(bad_script)],
        project_slug="s4_test",
        catalog_dir=cat,
        output_paths=[],
        tags=[],
        cwd=proj,
    )
    assert rc_bad == 1
    assert not marker.exists()

    # Confirmatory run with [holdout] kind='new_builds' via --from-observation promotes obs
    good_script = proj / "scripts" / "experiments" / "confirm_promo.py"
    good_script.write_text(
        textwrap.dedent(
            """
            import json, os
            from pathlib import Path
            Path(os.environ["BTH_RESULTS_PATH"]).write_text(json.dumps({"metric": 0.9}))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    _write_sidecar(
        proj / "scripts" / "experiments" / "confirm_promo.bth.toml",
        """
        [experiment]
        stage_name = "validation"
        hypothesis = "Hub replicates on new builds"
        novel = true

        [holdout]
        kind = "new_builds"

        [outcomes.pass]
        condition = "metric > 0.5"
        decision = "accept"
        reasoning = "above SESOI"
        source = "sesoi:0.5:minimum meaningful effect"

        [outcomes.fail]
        condition = "metric <= 0.5"
        decision = "reject"
        reasoning = "residual"
        is_residual = true

        [result_schema]
        metric = "float"
        """,
    )

    # CLI bth run --from-observation <obs.id>
    cli_run = runner.invoke(
        app,
        [
            "run",
            "--project-slug",
            "s4_test",
            "--catalog-dir",
            str(cat),
            "--from-observation",
            obs.id,
            str(good_script),
        ],
    )
    assert cli_run.exit_code == 0, cli_run.output
    run_payload, _ = json.JSONDecoder().raw_decode(cli_run.output.lstrip())
    assert run_payload["outcome"] == "pass"

    obs_after = get_observation(obs.id, catalog_dir=cat)
    assert obs_after is not None
    assert obs_after.status == "promoted"

    # Conflicting --from-observation vs [provenance].derived_from_observation fails closed
    rc_conflict = run_script(
        argv=[sys.executable, str(good_script)],
        project_slug="s4_test",
        catalog_dir=cat,
        output_paths=[],
        tags=[],
        cwd=proj,
        from_observation=obs.id,
    )
    assert rc_conflict == 0  # sidecar had no derived_from_observation, so no conflict
    # Now add obs_kill to sidecar and pass obs.id -> conflict
    _write_sidecar(
        proj / "scripts" / "experiments" / "confirm_promo.bth.toml",
        f"""
        [experiment]
        stage_name = "validation"
        hypothesis = "Hub replicates on new builds"
        novel = true

        [provenance]
        derived_from_observation = "{obs_kill.id}"

        [holdout]
        kind = "new_builds"

        [outcomes.pass]
        condition = "metric > 0.5"
        decision = "accept"
        reasoning = "above SESOI"
        source = "sesoi:0.5:minimum meaningful effect"

        [outcomes.fail]
        condition = "metric <= 0.5"
        decision = "reject"
        reasoning = "residual"
        is_residual = true

        [result_schema]
        metric = "float"
        """,
    )
    rc_mismatch = run_script(
        argv=[sys.executable, str(good_script)],
        project_slug="s4_test",
        catalog_dir=cat,
        output_paths=[],
        tags=[],
        cwd=proj,
        from_observation=obs.id,
    )
    assert rc_mismatch == 1

    # Rank-0 exploratory kill test NEVER promotes observation status
    kill_script = proj / "scripts" / "experiments" / "kill_seeds.py"
    kill_script.write_text(good_script.read_text(), encoding="utf-8")
    _write_sidecar(
        proj / "scripts" / "experiments" / "kill_seeds.bth.toml",
        f"""
        [experiment]
        stage_name = "exploration"
        question = "Does hub survive new seeds?"

        [observe]
        outputs = ["results.json"]

        [provenance]
        derived_from_observation = "{obs_kill.id}"

        [holdout]
        kind = "new_seeds"

        [result_schema]
        metric = "float"
        """,
    )
    mcp_res = asyncio.run(
        mcp_run_tool(
            script_path=str(kill_script),
            project_slug="s4_test",
            catalog_dir=str(cat),
            token=s4_env["token"],
        )
    )
    assert mcp_res["exit_code"] == 0
    assert mcp_res["outcome"] == "observed"
    obs_kill_after = get_observation(obs_kill.id, catalog_dir=cat)
    assert obs_kill_after is not None
    assert obs_kill_after.status == "open"


def test_runner_legacy_mode_records_holdout_verified_false(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BTH_LOG_MODE", "0")
    cat = tmp_path / "cat"
    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    init_catalog(cat)

    exp_run = Run(
        id="run_legacy_exp_1",
        project_slug="leg",
        command="python explore.py",
        argv=["python", "explore.py"],
        git_hash="a" * 40,
        git_branch="main",
        git_dirty=False,
        status="completed",
        outcome="observed",
        epistemic_class="exploratory",
    )
    write_run(exp_run, cat)
    obs = create_observation(
        run_id=exp_run.id,
        text="Legacy obs",
        project_root=proj,
        catalog_dir=cat,
    )

    sc_dir = proj / "scripts" / "experiments"
    sc_dir.mkdir(parents=True)
    script = sc_dir / "confirm_leg.py"
    script.write_text(
        "import json, os; from pathlib import Path; "
        "Path(os.environ['BTH_RESULTS_PATH']).write_text(json.dumps({'metric': 1.0}))\n",
        encoding="utf-8",
    )
    _write_sidecar(
        sc_dir / "confirm_leg.bth.toml",
        f"""
        [experiment]
        stage_name = "validation"
        hypothesis = "H"
        novel = true
        [provenance]
        derived_from_observation = "{obs.id}"
        [holdout]
        kind = "new_builds"
        [outcomes.pass]
        condition = "metric > 0.5"
        decision = "go"
        reasoning = "r"
        source = "sesoi:0.5:why"
        [outcomes.fail]
        condition = "metric <= 0.5"
        decision = "stop"
        reasoning = "r"
        is_residual = true
        [result_schema]
        metric = "float"
        """,
    )
    rc = run_script(
        argv=[sys.executable, str(script)],
        project_slug="leg",
        catalog_dir=cat,
        output_paths=[],
        tags=[],
        cwd=proj,
    )
    assert rc == 0
    runs = [r for r in read_runs(cat) if r.id != exp_run.id]
    assert len(runs) == 1
    meta = json.loads(runs[0].metadata)
    assert meta["holdout_verified"] is False
    assert meta["derived_from_observation"] == obs.id
    assert meta["holdout"]["kind"] == "new_builds"
    assert meta["holdout"]["rank"] == 1
    obs_promoted = get_observation(obs.id, catalog_dir=cat)
    assert obs_promoted is not None
    assert obs_promoted.status == "promoted"


# ── 6. Laundering tripwire (C3), same-argv, and S5 calibration lint ──────────


def test_lint_laundering_tripwire_and_same_argv(s4_env):
    proj = s4_env["proj"]
    cat = s4_env["cat"]
    import hashlib

    exp_dir = proj / "scripts" / "experiments"
    exp_dir.mkdir(parents=True, exist_ok=True)
    exp_script = exp_dir / "explore_hub.py"
    exp_script.write_text("print('hi')\n", encoding="utf-8")
    exp_sha = hashlib.sha256(exp_script.read_bytes()).hexdigest()

    exp_sidecar = _write_sidecar(
        exp_dir / "explore_hub.bth.toml",
        """
        [experiment]
        stage_name = "exploration"
        question = "Is there a hub?"

        [observe]
        outputs = ["results.json"]

        [outcomes.hub]
        condition = "score > 0.7"
        decision = "inspect"
        reasoning = "triage"
        source = "provisional:eyeball"

        [outcomes.rest]
        condition = "score <= 0.7"
        decision = "ignore"
        reasoning = "residual"
        is_residual = true

        [result_schema]
        score = "float"
        """,
    )
    exp_run = s4_env["seed_run"](
        "run_exp_launder_1",
        epistemic_class="exploratory",
        stage_name="exploration",
        argv=["python", "scripts/experiments/shared_cmd.py", "--seed", "1"],
        script_sha256=exp_sha,
        sidecar_path=str(exp_sidecar),
    )

    # Renamed confirmatory script copying BOTH script bytes AND canonicalized [outcomes], with NO [holdout]
    renamed_script = exp_dir / "confirm_renamed.py"
    renamed_script.write_text("print('hi')\n", encoding="utf-8")
    renamed_sc = _write_sidecar(
        exp_dir / "confirm_renamed.bth.toml",
        """
        [experiment]
        stage_name = "validation"
        hypothesis = "Hub exists"
        novel = true

        [outcomes.hub]
        condition = "score > 0.7"
        decision = "inspect"
        reasoning = "reframed"
        source = "sesoi:0.7:claimed"

        [outcomes.rest]
        condition = "score <= 0.7"
        decision = "ignore"
        reasoning = "residual"
        is_residual = true

        [result_schema]
        score = "float"
        """,
    )
    # Also test canonicalized-[outcomes]-only match with different script bytes
    retuned_script = exp_dir / "confirm_outcomes_copy.py"
    retuned_script.write_text("print('different script')\n", encoding="utf-8")
    _write_sidecar(
        exp_dir / "confirm_outcomes_copy.bth.toml",
        renamed_sc.read_text(),
    )

    # Same-argv confirmatory run
    obs = create_observation(
        run_id=exp_run.id,
        text="Hub at 0.7",
        project_root=proj,
        catalog_dir=cat,
    )
    s4_env["seed_run"](
        "run_conf_same_argv",
        epistemic_class="confirmatory",
        stage_name="validation",
        outcome="pass",
        argv=["python", "scripts/experiments/shared_cmd.py", "--seed", "1"],
        metadata={"derived_from_observation": obs.id, "holdout_verified": False},
    )

    issues = check_holdout_and_observation_lint(proj, catalog_dir=cat)
    launder_hits = [i for i in issues if i.issue == "laundering_tripwire"]
    assert len(launder_hits) == 2
    assert all(i.severity == IssueSeverity.WARNING for i in launder_hits)
    assert any("script_sha256" in i.detail for i in launder_hits)
    assert any("canonicalized [outcomes]" in i.detail for i in launder_hits)

    same_argv_hits = [i for i in issues if i.issue == "holdout_same_argv"]
    assert len(same_argv_hits) == 1
    assert same_argv_hits[0].severity == IssueSeverity.WARNING

    # Adding [holdout] clears the laundering warning on confirm_renamed
    _write_sidecar(
        renamed_sc,
        renamed_sc.read_text() + '\n[provenance]\nderived_from_observation = "' + obs.id + '"\n[holdout]\nkind = "new_builds"\n',
    )
    issues_after = check_holdout_and_observation_lint(proj, catalog_dir=cat)
    assert len([i for i in issues_after if i.issue == "laundering_tripwire"]) == 1


def test_s5_calibration_run_source_and_sesoi_vs_mde(s4_env):
    proj = s4_env["proj"]
    cat = s4_env["cat"]
    exp_dir = proj / "scripts" / "experiments"
    exp_dir.mkdir(parents=True, exist_ok=True)

    # Seed calibration runs (good, crashed, invalid_measurement, missing field) and an exploratory run
    cal_sc = _write_sidecar(
        exp_dir / "cal_noise.bth.toml",
        """
        [experiment]
        stage_name = "calibration"
        [calibrate]
        quantity = "spike-in MDE and noise floor"
        [result_schema]
        noise_floor = "float"
        mde = "float"
        """,
    )
    cal_good = s4_env["seed_run"](
        "calgood_11112222",
        epistemic_class="calibrating",
        stage_name="calibration",
        outcome="observed",
        exit_code=0,
        sidecar_path=str(cal_sc),
        metadata={"noise_floor": 0.12, "mde": 0.35},
    )
    cal_bad_exit = s4_env["seed_run"](
        "calbad_22223333",
        epistemic_class="calibrating",
        stage_name="calibration",
        outcome="error",
        exit_code=1,
        metadata={"noise_floor": 0.1},
    )
    cal_invalid_meas = s4_env["seed_run"](
        "calinv_33334444",
        epistemic_class="calibrating",
        stage_name="calibration",
        outcome="invalid_measurement",
        exit_code=0,
        metadata={"noise_floor": 0.1},
    )
    exp_noncal = s4_env["seed_run"](
        "expnoncal_44445555",
        epistemic_class="exploratory",
        stage_name="exploration",
        outcome="observed",
        exit_code=0,
        metadata={"noise_floor": 0.1},
    )

    # Valid calibration source + SESOI >= MDE (0.5 >= 0.35) -> clean
    ok_sc = _write_sidecar(
        exp_dir / "confirm_cal_ok.bth.toml",
        f"""
        [experiment]
        stage_name = "validation"
        hypothesis = "Signal clears noise floor and SESOI"
        novel = true

        [outcomes.pass]
        condition = "delta > 0.5"
        decision = "accept"
        reasoning = "above SESOI"
        source = "sesoi:0.5:biologically meaningful"

        [outcomes.marginal]
        condition = "delta > 0.24 AND delta <= 0.5"
        decision = "marginal"
        reasoning = "above 2x noise floor"
        source = "run:{cal_good.id[:8]}:noise_floor"

        [outcomes.fail]
        condition = "delta <= 0.24"
        decision = "reject"
        reasoning = "residual"
        is_residual = true

        [result_schema]
        delta = "float"
        """,
    )
    issues_ok = check_calibration_sources(proj, catalog_dir=cat)
    assert issues_ok == [], issues_ok
    assert validate_sidecar(parse_sidecar(ok_sc), sidecar_path=ok_sc, catalog_dir=cat).ok

    # Underpowered SESOI (0.2 < MDE 0.35) -> WARNING underpowered_vs_sesoi (C21)
    _write_sidecar(
        ok_sc,
        ok_sc.read_text().replace("sesoi:0.5:biologically meaningful", "sesoi:0.2:too small"),
    )
    issues_under = check_calibration_sources(proj, catalog_dir=cat)
    assert [i.issue for i in issues_under] == ["underpowered_vs_sesoi"]
    assert issues_under[0].severity == IssueSeverity.WARNING

    # Negative controls: missing run, non-calibrating run, crashed/invalid_measurement, missing field
    bad_cases = [
        ("run:run_doesnotexist:noise_floor", "calibration_run_not_found"),
        (f"run:{exp_noncal.id[:8]}:noise_floor", "non_calibrating_run_source"),
        (f"run:{cal_bad_exit.id[:8]}:noise_floor", "calibration_run_unusable"),
        (f"run:{cal_invalid_meas.id[:8]}:noise_floor", "calibration_run_unusable"),
        (f"run:{cal_good.id[:8]}:nonexistent_field", "calibration_field_missing"),
    ]
    for src_val, expected_issue in bad_cases:
        _write_sidecar(
            exp_dir / "confirm_cal_bad.bth.toml",
            f"""
            [experiment]
            stage_name = "validation"
            hypothesis = "H"
            novel = true

            [outcomes.pass]
            condition = "delta > 0.5"
            decision = "go"
            reasoning = "r"
            source = "{src_val}"

            [outcomes.fail]
            condition = "delta <= 0.5"
            decision = "stop"
            reasoning = "r"
            is_residual = true

            [result_schema]
            delta = "float"
            """,
        )
        all_issues = lint_project(proj)
        hits = [i for i in all_issues if i.issue == expected_issue]
        assert hits, f"Expected {expected_issue} for {src_val}, got {[i.issue for i in all_issues]}"
        assert hits[0].severity == IssueSeverity.ERROR

    # validate_sidecar_tool surfaces catalog-backed errors too
    val_out = validate_sidecar_tool(str(exp_dir / "confirm_cal_bad.bth.toml"))
    assert val_out["validation_ok"] is False
    assert "nonexistent_field" in val_out["error"]
