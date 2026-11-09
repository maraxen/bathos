"""Targeted test suite for Phase 2 (Findings + Evidence Tiers + SessionStart Hook).

Covers AC-P2-01 through AC-P2-16, DAG cycle detection, and SessionStart hook contract.
Spec: 261106 §4.1 - §4.9.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bathos.campaign_edges import CycleRejectedError
from bathos.catalog import init_catalog, write_run
from bathos.errors import (
    EstimandBoundaryMixedError,
    FindingReopenReasonRequiredError,
    SpikeTierCannotSettleError,
    SupersededTargetNotFoundError,
)
from bathos.findings import (
    create_finding,
    get_finding,
    list_findings,
    parse_finding_file,
)
from bathos.index import connect_read
from bathos.new_experiment import scaffold_experiment
from bathos.provenance import format_prov_json
from bathos.query import list_runs
from bathos.runlog.project_id import assign_project_id, register_main_root
from bathos.schema import Run
from bathos.session_start import generate_session_start_cache
from bathos.sidecar import (
    parse_sidecar,
    resolve_epistemic_class,
    resolve_epistemic_tier,
)
from bathos.supersession import (
    add_supersession_edge,
    get_replaces,
    get_superseded_by,
)
from bathos.validity import ValidityResult, resolve_evidence_tier, resolve_validity


def _make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "README.md").write_text("test repo\n")
    (path / ".bth.toml").write_text(f'[project]\nslug = "{path.name}"\n')
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    assign_project_id(path)
    register_main_root(path)
    return path


def _create_sample_run(
    catalog_dir: Path,
    run_id: str,
    project_slug: str = "repo",
    git_hash: str = "abc1234",
    stage_name: str | None = None,
    epistemic_class: str | None = None,
    metadata: dict | None = None,
    dependency_provenance: dict | None = None,
    parent_run_id: str | None = None,
) -> Run:
    r = Run(
        id=run_id,
        project_slug=project_slug,
        command=f"python run_{run_id[:8]}.py",
        argv=["python", f"run_{run_id[:8]}.py"],
        parent_run_id=parent_run_id,
        git_hash=git_hash,
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        duration_s=1.0,
        metadata=json.dumps(metadata) if metadata else None,
        dependency_provenance=json.dumps(dependency_provenance) if dependency_provenance else None,
        stage_name=stage_name,
        epistemic_class=epistemic_class,
        timestamp=datetime.now(UTC),
    )
    write_run(r, catalog_dir)
    return r


# ---------------------------------------------------------------------------
# AC-P2-01: Spike Tier Sidecar Resolution
# ---------------------------------------------------------------------------
def test_ac_p2_01_spike_tier_resolution(tmp_path: Path):
    sidecar_path = tmp_path / "spike_test.bth.toml"
    sidecar_path.write_text(
        """
        [experiment]
        hypothesis = "Testing throwaway probe assumption"
        stage_name = "spike"
        [result_schema]
        score = "float"
        """
    )
    s = parse_sidecar(sidecar_path)
    assert s.stage_name == "spike"
    assert s.stage_name_declared is True
    assert s.stage_name_raw == "spike"
    assert resolve_epistemic_class(s, stem_has_prior_runs=False) == "spike"
    assert resolve_epistemic_tier(stage_name_declared=True, stage_name_raw="spike") == "spike"
    assert resolve_evidence_tier("spike", "spike") == "spike"


# ---------------------------------------------------------------------------
# AC-P2-02: Spike Tier Cannot Settle Findings
# ---------------------------------------------------------------------------
def test_ac_p2_02_spike_tier_cannot_settle(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    r_spike = _create_sample_run(
        cat_dir, "run_spike_01", stage_name="spike", epistemic_class="spike"
    )

    with pytest.raises(SpikeTierCannotSettleError) as exc_info:
        create_finding(
            question_slug="spike-probe",
            question="Can spike settle?",
            answer="No, it cannot",
            supporting_runs=[r_spike.id],
            status="settled",
            catalog_dir=cat_dir,
            workspace_root=repo,
        )
    assert "Spike runs" in str(exc_info.value)


# ---------------------------------------------------------------------------
# AC-P2-03: Undeclared Stage Displays as "undeclared" on Reads
# ---------------------------------------------------------------------------
def test_ac_p2_03_undeclared_stage_display(tmp_path: Path):
    sidecar_path = tmp_path / "undeclared_test.bth.toml"
    sidecar_path.write_text(
        """
        [experiment]
        hypothesis = "Omitted stage experiment"
        [result_schema]
        val = "float"
        """
    )
    s = parse_sidecar(sidecar_path)
    assert s.stage_name_declared is False
    assert resolve_epistemic_tier(stage_name_declared=False, stage_name_raw=None) == "undeclared"

    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)
    r_und = _create_sample_run(cat_dir, "run_und_01", stage_name=None, epistemic_class="legacy")

    vmap = resolve_validity([r_und.id], catalog_dir=cat_dir)
    assert vmap[r_und.id].evidence_tier == "undeclared"


# ---------------------------------------------------------------------------
# AC-P2-04: Adding Supersession Edge Hides Run from Default Reads
# ---------------------------------------------------------------------------
def test_ac_p2_04_supersession_edge_hides_run(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    r_old = _create_sample_run(cat_dir, "run_old_01")
    r_new = _create_sample_run(cat_dir, "run_new_02")

    edge = add_supersession_edge(
        "run",
        r_old.id,
        "run",
        r_new.id,
        reason="improved methodology",
        catalog_dir=cat_dir,
        workspace_root=repo,
    )
    assert edge.entity_id == r_old.id
    assert edge.superseded_by_id == r_new.id

    vmap = resolve_validity([r_old.id, r_new.id], catalog_dir=cat_dir)
    assert vmap[r_old.id].state == "superseded"
    assert vmap[r_old.id].is_hidden is True
    assert vmap[r_new.id].state == "current"
    assert vmap[r_new.id].is_hidden is False

    # Default list excludes superseded run
    active_runs = list_runs(catalog_dir=cat_dir, include_superseded=False)
    active_ids = [r.id for r in active_runs]
    assert r_old.id not in active_ids
    assert r_new.id in active_ids

    # With include_superseded=True, both are returned
    all_runs = list_runs(catalog_dir=cat_dir, include_superseded=True)
    all_ids = [r.id for r in all_runs]
    assert r_old.id in all_ids
    assert r_new.id in all_ids


# ---------------------------------------------------------------------------
# AC-P2-05: Reopen Guard Across Findings and Experiment Scaffolding
# ---------------------------------------------------------------------------
def test_ac_p2_05_reopen_guard(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    r_conf = _create_sample_run(
        cat_dir, "run_conf_01", stage_name="validation", epistemic_class="confirmatory"
    )

    settled = create_finding(
        question_slug="tev-stability",
        question="Is engineered TEV loop stable at 37C?",
        answer="Yes, RMSD < 1.2A across 50ns NVT",
        supporting_runs=[r_conf.id],
        status="settled",
        catalog_dir=cat_dir,
        workspace_root=repo,
    )
    assert settled.status == "settled"

    # 1. Attempting finding_create on settled question without reopen_reason raises
    with pytest.raises(FindingReopenReasonRequiredError) as exc_info:
        create_finding(
            question_slug="tev-stability",
            question="Is engineered TEV loop stable at 37C?",
            answer="Challenging stability",
            reopen_reason="",
            catalog_dir=cat_dir,
            workspace_root=repo,
        )
    assert "already settled" in str(exc_info.value)
    assert exc_info.value.settled_finding_id == settled.id

    # 2. Supplying reopen_reason starts finding in tentative; predecessor stays settled
    challenger = create_finding(
        question_slug="tev-stability",
        question="Is engineered TEV loop stable at 37C?",
        answer="Re-evaluating under new 37C assay",
        reopen_reason="Higher salt concentration assay",
        catalog_dir=cat_dir,
        workspace_root=repo,
    )
    assert challenger.status == "tentative"
    assert challenger.supersedes == settled.id

    # Settled predecessor remains settled
    reloaded_settled = get_finding(settled.id, catalog_dir=cat_dir, workspace_root=repo)
    assert reloaded_settled.status == "settled"

    # 3. Scaffolding experiment checks settled question
    with pytest.raises(FindingReopenReasonRequiredError):
        scaffold_experiment(
            "retest_tev",
            repo,
            question_slug="tev-stability",
            reopen_reason="",
        )

    # Scaffolding with reopen_reason succeeds
    res = scaffold_experiment(
        "retest_tev",
        repo,
        question_slug="tev-stability",
        reopen_reason="testing new assay conditions",
    )
    assert res.sidecar.exists()
    sidecar_obj = parse_sidecar(res.sidecar)
    assert sidecar_obj.question_slug == "tev-stability"
    assert sidecar_obj.reopen_reason == "testing new assay conditions"


# ---------------------------------------------------------------------------
# AC-P2-06 & AC-P2-14: Dynamic Downgrade Ladder without Mutating Disk
# ---------------------------------------------------------------------------
def test_ac_p2_06_dynamic_downgrade_ladder(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    r1 = _create_sample_run(
        cat_dir, "run_conf_01", stage_name="validation", epistemic_class="confirmatory"
    )
    r2 = _create_sample_run(
        cat_dir, "run_conf_02", stage_name="validation", epistemic_class="confirmatory"
    )

    finding = create_finding(
        question_slug="pdz-affinity",
        question="Is Kd < 150nM?",
        answer="Yes, Kd is 120nM",
        supporting_runs=[r1.id, r2.id],
        status="settled",
        catalog_dir=cat_dir,
        workspace_root=repo,
    )
    assert finding.status == "settled"

    # Void both supporting runs via supersession or defect
    r_dummy = _create_sample_run(cat_dir, "run_dummy")
    add_supersession_edge("run", r1.id, "run", r_dummy.id, "voided", catalog_dir=cat_dir, workspace_root=repo)
    add_supersession_edge("run", r2.id, "run", r_dummy.id, "voided", catalog_dir=cat_dir, workspace_root=repo)

    # Read finding: dynamically resolves to needs-recheck
    active = get_finding(finding.id, catalog_dir=cat_dir, workspace_root=repo)
    assert active.active_status == "needs-recheck"

    # AC-P2-14: Underlying file on disk was NOT modified
    raw_disk_finding = parse_finding_file(finding.file_path)
    assert raw_disk_finding.status == "settled"

    # Vacuous truth guard (Finding M5): open finding with [] stays open
    open_finding = create_finding(
        question_slug="open-untested",
        question="Open question?",
        answer="",
        supporting_runs=[],
        status="open",
        catalog_dir=cat_dir,
        workspace_root=repo,
    )
    active_open = get_finding(open_finding.id, catalog_dir=cat_dir, workspace_root=repo)
    assert active_open.active_status == "open"


# ---------------------------------------------------------------------------
# AC-P2-07: Fast-Path Hook Execution, Subprocess Timing & Context Envelope
# ---------------------------------------------------------------------------
def test_ac_p2_07_session_start_hook_contract(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    r_conf = _create_sample_run(cat_dir, "run_conf", stage_name="validation", epistemic_class="confirmatory")
    create_finding(
        question_slug="settled-q",
        question="Q?",
        answer="Answer A",
        supporting_runs=[r_conf.id],
        status="settled",
        catalog_dir=cat_dir,
        workspace_root=repo,
    )

    # Generate cache
    cache_info = generate_session_start_cache(catalog_dir=cat_dir, workspace_root=repo)
    assert Path(cache_info["path"]).is_file()

    # Subprocess execution of bth-session-start-hook (warm run to avoid one-off page fault cold start)
    hook_bin = shutil.which("bth-session-start-hook")
    hook_file = str(Path(__file__).resolve().parents[1] / "src" / "bathos" / "hooks" / "session_start_hook.py")
    cmd = [hook_bin] if hook_bin else [sys.executable, hook_file]
    env = os.environ.copy()
    env["BTH_WORKSPACE_ROOT"] = str(repo)

    # Warm up Python bytecode execution
    subprocess.run(cmd, cwd=repo, env=env, capture_output=True, check=True)

    t0 = time.perf_counter()
    res = subprocess.run(
        cmd,
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000

    assert res.returncode == 0
    assert elapsed_ms < 100, f"Hook took {elapsed_ms:.2f}ms, exceeds 100ms budget"

    data = json.loads(res.stdout)
    assert "hookSpecificOutput" in data
    assert data["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    additional_context = data["hookSpecificOutput"]["additionalContext"]
    assert len(additional_context.encode("utf-8")) <= 2048
    assert "* SETTLED: 'settled-q'" in additional_context


# ---------------------------------------------------------------------------
# AC-P2-08 & AC-P2-09: SessionStart Ordering & Strict Exclusions
# ---------------------------------------------------------------------------
def test_ac_p2_08_and_09_session_start_ordering_and_exclusions(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    # 1. Defect alert in registry
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True, exist_ok=True)
    (inst_dir / "aminx.toml").write_text(
        """schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "AMINX-2026-001"
revision = 1
kind = "defect"
severity = "void"
description = "S > 1 invalid"
ranges = [
  { type = "ECOSYSTEM", events = [{ fixed = "0.1.0a23" }] }
]
"""
    )

    # 2. Confirmatory run & settled finding (post-defect toolchain version)
    r_conf = _create_sample_run(
        cat_dir,
        "run_conf_ok",
        stage_name="validation",
        epistemic_class="confirmatory",
        dependency_provenance={"aminx_version": "0.2.0"},
    )
    create_finding(
        question_slug="settled-item",
        question="Is it settled?",
        answer="Confirmed true",
        supporting_runs=[r_conf.id],
        status="settled",
        catalog_dir=cat_dir,
        workspace_root=repo,
    )

    # 3. Needs-recheck finding
    r_void = _create_sample_run(
        cat_dir,
        "run_void_src",
        stage_name="validation",
        epistemic_class="confirmatory",
        dependency_provenance={"aminx_version": "0.2.0"},
    )
    create_finding(
        question_slug="recheck-item",
        question="Needs recheck?",
        answer="Answer old",
        supporting_runs=[r_void.id],
        status="settled",
        catalog_dir=cat_dir,
        workspace_root=repo,
    )
    r_repl = _create_sample_run(cat_dir, "run_repl")
    add_supersession_edge("run", r_void.id, "run", r_repl.id, "voided", catalog_dir=cat_dir, workspace_root=repo)

    # 4. Open finding
    create_finding(
        question_slug="open-item",
        question="Open question?",
        answer="",
        status="open",
        catalog_dir=cat_dir,
        workspace_root=repo,
    )

    # 5. Spike run & finding (must be excluded from session-start, AC-P2-09)
    r_spike = _create_sample_run(cat_dir, "run_spike", stage_name="spike", epistemic_class="spike")
    create_finding(
        question_slug="spike-item",
        question="Spike question?",
        answer="Probe",
        supporting_runs=[r_spike.id],
        status="tentative",
        catalog_dir=cat_dir,
        workspace_root=repo,
    )

    cache_res = generate_session_start_cache(catalog_dir=cat_dir, workspace_root=repo)
    text = cache_res["hookSpecificOutput"]["additionalContext"]

    # Verify Ordering (AC-P2-08):
    # Alert (! ALERT:) -> Needs-Recheck (? NEEDS RECHECK:) -> Settled (* SETTLED:) -> Open (o OPEN:)
    alert_pos = text.find("! ALERT:")
    recheck_pos = text.find("? NEEDS RECHECK:")
    settled_pos = text.find("* SETTLED:")
    open_pos = text.find("o OPEN:")

    assert alert_pos != -1, "Defect alert missing"
    assert recheck_pos != -1, "Needs-recheck item missing"
    assert settled_pos != -1, "Settled item missing"
    assert open_pos != -1, "Open item missing"

    assert alert_pos < recheck_pos < settled_pos < open_pos, "SessionStart output not in priority order"

    # AC-P2-09: Strictly 0 spike items in cache
    assert "spike-item" not in text


# ---------------------------------------------------------------------------
# AC-P2-10: finding_list Hides Superseded Findings
# ---------------------------------------------------------------------------
def test_ac_p2_10_finding_list_include_superseded(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    r1 = _create_sample_run(cat_dir, "run_1", stage_name="validation", epistemic_class="confirmatory")
    r2 = _create_sample_run(cat_dir, "run_2", stage_name="validation", epistemic_class="confirmatory")

    f1 = create_finding(
        question_slug="pred-finding",
        question="Initial finding",
        answer="A1",
        supporting_runs=[r1.id],
        status="settled",
        catalog_dir=cat_dir,
        workspace_root=repo,
    )
    f2 = create_finding(
        question_slug="succ-finding",
        question="Successor finding",
        answer="A2",
        supporting_runs=[r2.id],
        status="settled",
        supersedes=f1.id,
        reopen_reason="Newer data",
        catalog_dir=cat_dir,
        workspace_root=repo,
    )

    # Without include_superseded: f1 is hidden, f2 is visible
    active_findings = list_findings(include_superseded=False, catalog_dir=cat_dir, workspace_root=repo)
    active_ids = [f.id for f in active_findings]
    assert f1.id not in active_ids
    assert f2.id in active_ids

    # With include_superseded=True: both are listed
    all_findings = list_findings(include_superseded=True, catalog_dir=cat_dir, workspace_root=repo)
    all_ids = [f.id for f in all_findings]
    assert f1.id in all_ids
    assert f2.id in all_ids


# ---------------------------------------------------------------------------
# AC-P2-11: W3C PROV-JSON Invalidation and Revision
# ---------------------------------------------------------------------------
def test_ac_p2_11_prov_json_invalidation_and_revision():
    now = datetime.now(UTC)
    r_valid = Run(
        id="run_valid_1111",
        project_slug="p",
        command="cmd",
        argv=["cmd"],
        git_hash="g",
        git_branch="main",
        git_dirty=False,
        timestamp=now,
    )
    r_void = Run(
        id="run_void_2222",
        project_slug="p",
        command="cmd",
        argv=["cmd"],
        git_hash="g",
        git_branch="main",
        git_dirty=False,
        timestamp=now,
    )
    r_super = Run(
        id="run_super_3333",
        project_slug="p",
        command="cmd",
        argv=["cmd"],
        git_hash="g",
        git_branch="main",
        git_dirty=False,
        timestamp=now,
    )
    r_repl = Run(
        id="run_repl_4444",
        project_slug="p",
        command="cmd",
        argv=["cmd"],
        git_hash="g",
        git_branch="main",
        git_dirty=False,
        timestamp=now,
    )

    validity_map = {
        r_valid.id: ValidityResult(
            run_id=r_valid.id,
            state="current",
            reason=None,
            replacement_id=None,
            version_source="recorded",
            evidence_tier="confirmatory",
            is_hidden=False,
        ),
        r_void.id: ValidityResult(
            run_id=r_void.id,
            state="void",
            reason="AMINX-2026-001",
            replacement_id=None,
            version_source="recorded",
            evidence_tier="confirmatory",
            is_hidden=True,
        ),
        r_super.id: ValidityResult(
            run_id=r_super.id,
            state="superseded",
            reason="superseded by replacement",
            replacement_id=r_repl.id,
            version_source="recorded",
            evidence_tier="confirmatory",
            is_hidden=True,
        ),
        r_repl.id: ValidityResult(
            run_id=r_repl.id,
            state="current",
            reason=None,
            replacement_id=None,
            version_source="recorded",
            evidence_tier="confirmatory",
            is_hidden=False,
        ),
    }

    prov = format_prov_json(
        [r_valid, r_void, r_super, r_repl],
        validity_map=validity_map,
    )

    # 1. wasInvalidatedBy for void run
    assert "wasInvalidatedBy" in prov
    assert len(prov["wasInvalidatedBy"]) == 1
    inv = next(iter(prov["wasInvalidatedBy"].values()))
    assert inv["prov:entity"] == f"bth:run_{r_void.id[:8]}"
    assert inv["bth:validity_state"] == "void"
    assert inv["bth:reason"] == "AMINX-2026-001"

    # 2. wasRevisionOf for superseded run
    assert "wasRevisionOf" in prov
    assert len(prov["wasRevisionOf"]) == 1
    rev = next(iter(prov["wasRevisionOf"].values()))
    assert rev["prov:olderEntity"] == f"bth:run_{r_super.id[:8]}"
    assert rev["prov:newerEntity"] == f"bth:run_{r_repl.id[:8]}"


# ---------------------------------------------------------------------------
# AC-P2-12: Non-Canonical Stage Aliases ("confirmatory", "power")
# ---------------------------------------------------------------------------
def test_ac_p2_12_stage_aliases(tmp_path: Path):
    s_conf = tmp_path / "conf.bth.toml"
    s_conf.write_text(
        """
        [experiment]
        hypothesis = "Confirmatory alias test"
        stage_name = "confirmatory"
        [result_schema]
        v = "float"
        """
    )
    parsed_conf = parse_sidecar(s_conf)
    assert parsed_conf.stage_name == "validation"
    assert parsed_conf.stage_name_raw == "confirmatory"
    assert resolve_epistemic_tier(stage_name_declared=True, stage_name_raw="confirmatory") == "confirmatory"

    s_power = tmp_path / "power.bth.toml"
    s_power.write_text(
        """
        [experiment]
        hypothesis = "Power calibration test"
        stage_name = "power"
        [result_schema]
        v = "float"
        """
    )
    parsed_power = parse_sidecar(s_power)
    assert parsed_power.stage_name == "validation"
    assert parsed_power.stage_name_raw == "power"
    assert resolve_epistemic_tier(stage_name_declared=True, stage_name_raw="power") == "confirmatory"


# ---------------------------------------------------------------------------
# AC-P2-13: Bidirectional Supersession Lookup
# ---------------------------------------------------------------------------
def test_ac_p2_13_supersession_bidirectional(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    r_pre = _create_sample_run(cat_dir, "run_pre_1")
    r_post = _create_sample_run(cat_dir, "run_post_2")

    add_supersession_edge(
        "run",
        r_pre.id,
        "run",
        r_post.id,
        reason="newer iteration",
        catalog_dir=cat_dir,
        workspace_root=repo,
    )

    replaces = get_replaces("run", r_post.id, catalog_dir=cat_dir)
    assert len(replaces) == 1
    assert replaces[0]["entity_id"] == r_pre.id

    superseded_by = get_superseded_by("run", r_pre.id, catalog_dir=cat_dir)
    assert len(superseded_by) == 1
    assert superseded_by[0]["superseded_by_id"] == r_post.id


# ---------------------------------------------------------------------------
# AC-P2-15: Hook Registration Parity Check
# ---------------------------------------------------------------------------
def test_ac_p2_15_hook_registration_parity():
    repo_root = Path(__file__).parent.parent
    manifest_path = repo_root / ".praxia" / "manifest.toml"
    hooks_json_path = repo_root / "plugin" / "hooks" / "hooks.json"

    assert manifest_path.is_file(), "manifest.toml missing"
    assert hooks_json_path.is_file(), "hooks.json missing"

    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert 'event    = "SessionStart"' in manifest_text
    assert 'script   = "bth-session-start-hook"' in manifest_text

    hooks_data = json.loads(hooks_json_path.read_text(encoding="utf-8"))
    assert "SessionStart" in hooks_data["hooks"]
    session_start_hooks = hooks_data["hooks"]["SessionStart"]
    found = any(
        any(h.get("command") == "bth-session-start-hook" for h in entry.get("hooks", []))
        for entry in session_start_hooks
    )
    assert found, "bth-session-start-hook not registered in plugin/hooks/hooks.json"


# ---------------------------------------------------------------------------
# AC-P2-16: OD-6 Estimand Boundary Mixed Refusal
# ---------------------------------------------------------------------------
def test_ac_p2_16_estimand_boundary_mixed_refusal(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    # Declare an estimand_change defect
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True, exist_ok=True)
    (inst_dir / "aminx.toml").write_text(
        """schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "AMINX-2026-002"
revision = 1
kind = "estimand_change"
severity = "unverifiable"
description = "Algorithm scale shift"
ranges = [
  { type = "ECOSYSTEM", events = [{ introduced = "0.2.0" }] }
]
"""
    )

    r_epoch1 = _create_sample_run(
        cat_dir,
        "run_ep1",
        stage_name="validation",
        epistemic_class="confirmatory",
        dependency_provenance={"aminx_version": "0.1.5"},
    )
    r_epoch2 = _create_sample_run(
        cat_dir,
        "run_ep2",
        stage_name="validation",
        epistemic_class="confirmatory",
        dependency_provenance={"aminx_version": "0.2.5"},
    )

    # Refused without allow_mixed_estimand
    with pytest.raises(EstimandBoundaryMixedError):
        create_finding(
            question_slug="cross-epoch-finding",
            question="Can we mix epochs?",
            answer="Attempting",
            supporting_runs=[r_epoch1.id, r_epoch2.id],
            status="settled",
            allow_mixed_estimand="",
            catalog_dir=cat_dir,
            workspace_root=repo,
        )

    # Allowed with justification
    settled = create_finding(
        question_slug="cross-epoch-finding",
        question="Can we mix epochs?",
        answer="Mixed with justification",
        supporting_runs=[r_epoch1.id, r_epoch2.id],
        status="settled",
        allow_mixed_estimand="Affine transform calibrates pre and post epoch measurements",
        catalog_dir=cat_dir,
        workspace_root=repo,
    )
    assert settled.status == "settled"

    # Verify audit event in acknowledgment_ledger
    con = connect_read(cat_dir, missing="empty")
    try:
        row = con.execute(
            "SELECT reason FROM acknowledgment_ledger WHERE entity_id = ?", [settled.id]
        ).fetchone()
        assert row is not None
        assert "Affine transform" in row[0]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# DAG Cycle Rejection & Target Validation
# ---------------------------------------------------------------------------
def test_supersession_cycle_rejection(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    r_a = _create_sample_run(cat_dir, "run_a")
    r_b = _create_sample_run(cat_dir, "run_b")
    r_c = _create_sample_run(cat_dir, "run_c")

    # Target not found
    with pytest.raises(SupersededTargetNotFoundError):
        add_supersession_edge("run", r_a.id, "run", "run_nonexistent", "reason", catalog_dir=cat_dir, workspace_root=repo)

    # Self-loop A -> A
    with pytest.raises(CycleRejectedError):
        add_supersession_edge("run", r_a.id, "run", r_a.id, "self-loop", catalog_dir=cat_dir, workspace_root=repo)

    # A -> B succeeds
    add_supersession_edge("run", r_a.id, "run", r_b.id, "A superseded by B", catalog_dir=cat_dir, workspace_root=repo)

    # B -> A creates cycle
    with pytest.raises(CycleRejectedError):
        add_supersession_edge("run", r_b.id, "run", r_a.id, "B superseded by A", catalog_dir=cat_dir, workspace_root=repo)

    # B -> C succeeds
    add_supersession_edge("run", r_b.id, "run", r_c.id, "B superseded by C", catalog_dir=cat_dir, workspace_root=repo)

    # C -> A creates transitive cycle
    with pytest.raises(CycleRejectedError):
        add_supersession_edge("run", r_c.id, "run", r_a.id, "C superseded by A", catalog_dir=cat_dir, workspace_root=repo)


# ---------------------------------------------------------------------------
# SessionStart Missing Cache Exits 0 Cleanly
# ---------------------------------------------------------------------------
def test_session_start_hook_missing_cache_exits_zero(tmp_path: Path):
    clean_repo = _make_git_repo(tmp_path / "clean_repo")
    env = os.environ.copy()
    env["BTH_WORKSPACE_ROOT"] = str(clean_repo)

    hook_bin = shutil.which("bth-session-start-hook")
    hook_file = str(Path(__file__).resolve().parents[1] / "src" / "bathos" / "hooks" / "session_start_hook.py")
    cmd = [hook_bin] if hook_bin else [sys.executable, hook_file]
    res = subprocess.run(
        cmd,
        cwd=clean_repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert res.stdout.strip() == ""
