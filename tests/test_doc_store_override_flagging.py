"""Tests for tech_debt #3494: postmortem override -> decision doc flagging."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bathos.catalog import init_catalog, write_run
from bathos.cli_cyclopts import app
from bathos.doc_store import (
    extract_doc_citations,
    find_overridden_decision_docs,
    parse_doc_frontmatter,
)
from bathos.errors import DocStoreUnavailableError
from bathos.findings import create_finding, find_findings_with_overridden_runs
from bathos.mcp import check_tool, lint_tool, postmortem_validate_tool
from bathos.schema import Run
from bathos.supersession import find_stale_or_superseded_docs
from bathos.validity import resolve_postmortem_overrides
from tests._cyclopts_runner import CyclopticRunner

cli_runner = CyclopticRunner()


def _init_project(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{repo}"\n')
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    cat_dir = repo / ".bth" / "catalog"
    init_catalog(cat_dir)
    return repo, cat_dir


def _git_head(repo: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def _write_postmortem(
    repo: Path,
    *,
    run_id: str = "",
    campaign_id: str = "",
    verdict_override: str = "fail",
    hypothesis_status: str = "refuted",
    status: str = "submitted",
    summary: str = "Thermostat drift invalidated the original pass verdict.",
    extra_toml: str = "",
) -> Path:
    pm_dir = repo / ".bth" / "postmortems"
    pm_dir.mkdir(parents=True, exist_ok=True)
    if run_id:
        pm_path = pm_dir / f"run_{run_id[:8]}.bth.postmortem.toml"
        id_line = f'run_id = "{run_id}"'
    else:
        pm_path = pm_dir / f"campaign_{campaign_id}.bth.postmortem.toml"
        id_line = f'campaign_id = "{campaign_id}"'
    pm_path.write_text(
        f"""{id_line}
format = 2

[postmortem]
hypothesis_status = "{hypothesis_status}"
summary = "{summary}"
verdict_override = "{verdict_override}"
status = "{status}"
author = "researcher"
{extra_toml}
""",
        encoding="utf-8",
    )
    return pm_path


def test_extract_doc_citations_frontmatter_and_body():
    doc = """---
status: active
run_id: 11111111-2222-3333-4444-555555555555
cited_campaigns:
  - camp-alpha
acknowledged_overrides:
  - 99999999-aaaa-bbbb-cccc-dddddddddddd
---

# Decision

Based on Run abcdef12 and run:deadbeef12345678 and campaign:camp-beta and finding:pdz-kd.
"""
    cit = extract_doc_citations(doc)
    assert "11111111-2222-3333-4444-555555555555" in cit.run_ids
    assert "abcdef12" in cit.run_ids
    assert "deadbeef12345678" in cit.run_ids
    assert set(cit.campaign_ids) == {"camp-alpha", "camp-beta"}
    assert "pdz-kd" in cit.finding_ids
    assert cit.doc_status == "active"
    assert cit.acknowledged_overrides == ("99999999-aaaa-bbbb-cccc-dddddddddddd",)


def test_postmortem_override_flags_citing_decision_doc(tmp_path: Path):
    repo, cat_dir = _init_project(tmp_path)
    run_id = "11111111-2222-3333-4444-555555555555"
    run = Run(
        id=run_id,
        project_slug="testproj",
        command="python run_sim.py",
        argv=["python", "run_sim.py"],
        git_hash=_git_head(repo),
        git_branch="main",
        git_dirty=False,
        status="completed",
        outcome="pass",
    )
    write_run(run, cat_dir)

    docs_dir = repo / ".bth" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    doc_path = docs_dir / "use_thermostat_v1.md"
    doc_path.write_text(
        f"---\nstatus: active\nrun_id: {run_id}\n---\n\n# Decision\n\nShip v1 based on Run {run_id[:8]}.\n",
        encoding="utf-8",
    )

    # Negative control before override: no doc flagged
    assert find_overridden_decision_docs(repo, cat_dir) == []

    # Record a submitted postmortem override
    pm_path = _write_postmortem(repo, run_id=run_id, verdict_override="fail", hypothesis_status="refuted")

    hits = find_overridden_decision_docs(repo, cat_dir)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.doc_path == ".bth/docs/use_thermostat_v1.md"
    assert hit.cited_kind == "run"
    assert hit.target_id == run_id
    assert hit.verdict_override == "fail"
    assert hit.hypothesis_status == "refuted"
    assert hit.postmortem_path.endswith(pm_path.name)

    # validity + supersession integration
    v_overrides = resolve_postmortem_overrides([run_id], catalog_dir=cat_dir, workspace_root=repo)
    assert run_id in v_overrides
    assert v_overrides[run_id][0]["verdict_override"] == "fail"

    stale = find_stale_or_superseded_docs(repo, cat_dir)
    assert len(stale) == 1
    assert stale[0]["doc_path"] == ".bth/docs/use_thermostat_v1.md"


def test_draft_and_non_override_postmortem_do_not_flag_docs(tmp_path: Path):
    repo, cat_dir = _init_project(tmp_path)
    run_id = "22222222-3333-4444-5555-666666666666"
    docs_dir = repo / ".bth" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "decision.md").write_text(
        f"---\nstatus: active\n---\n\nWe rely on run:{run_id[:8]}.\n",
        encoding="utf-8",
    )

    # Draft override must NOT flag
    pm_path = _write_postmortem(
        repo, run_id=run_id, verdict_override="fail", hypothesis_status="refuted", status="draft"
    )
    assert find_overridden_decision_docs(repo, cat_dir) == []

    # Submitted non-override (verdict_override="none", hypothesis_status="held") must NOT flag
    pm_path.unlink()
    _write_postmortem(
        repo, run_id=run_id, verdict_override="none", hypothesis_status="held", status="submitted"
    )
    assert find_overridden_decision_docs(repo, cat_dir) == []


def test_campaign_postmortem_and_amendment_and_finding_flag_docs(tmp_path: Path):
    repo, cat_dir = _init_project(tmp_path)
    run_id = "33333333-4444-5555-6666-777777777777"
    run = Run(
        id=run_id,
        project_slug="testproj",
        command="python exp.py",
        argv=["python", "exp.py"],
        git_hash=_git_head(repo),
        git_branch="main",
        git_dirty=False,
        status="completed",
        outcome="pass",
        stage_name="validation",
        epistemic_class="confirmatory",
    )
    write_run(run, cat_dir)

    create_finding(
        question_slug="kd-under-150",
        question="Is Kd < 150?",
        answer="Yes",
        supporting_runs=[run_id],
        status="settled",
        catalog_dir=cat_dir,
        workspace_root=repo,
    )

    docs_dir = repo / ".bth" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "camp_doc.md").write_text(
        "---\nstatus: active\ncampaign_id: camp-123456\n---\n\nAdopted campaign:camp-123456.\n",
        encoding="utf-8",
    )
    (docs_dir / "finding_doc.md").write_text(
        "---\nstatus: active\n---\n\nRelies on finding:kd-under-150.\n",
        encoding="utf-8",
    )

    _write_postmortem(
        repo,
        campaign_id="camp-123456",
        verdict_override="inconclusive",
        hypothesis_status="inconclusive",
        extra_toml=f"""
[[amendments]]
analysis_run_id = "aaaa1111"
amends_run_id = "{run_id}"
kind = "reanalysis"
effect = "verdict_changed"
note = "Reanalysis reversed original run"
""",
    )

    hits = find_overridden_decision_docs(repo, cat_dir)
    doc_paths = {h.doc_path for h in hits}
    assert ".bth/docs/camp_doc.md" in doc_paths
    assert ".bth/docs/finding_doc.md" in doc_paths

    affected_findings = find_findings_with_overridden_runs(catalog_dir=cat_dir, workspace_root=repo)
    assert len(affected_findings) == 1
    assert affected_findings[0]["question_slug"] == "kd-under-150"


def test_superseded_and_acknowledged_docs_are_skipped_and_flag_docs_annotates(tmp_path: Path):
    repo, cat_dir = _init_project(tmp_path)
    run_id = "44444444-5555-6666-7777-888888888888"
    _write_postmortem(repo, run_id=run_id, verdict_override="fail", hypothesis_status="refuted")

    docs_dir = repo / ".bth" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    active_doc = docs_dir / "active_doc.md"
    active_doc.write_text(
        f"---\nstatus: active\nrun_id: {run_id}\n---\n\n# Active\n",
        encoding="utf-8",
    )
    superseded_doc = docs_dir / "old_doc.md"
    superseded_doc.write_text(
        f"---\nstatus: superseded\nrun_id: {run_id}\n---\n\n# Old\n",
        encoding="utf-8",
    )
    acked_doc = docs_dir / "acked_doc.md"
    acked_doc.write_text(
        f"---\nstatus: active\nrun_id: {run_id}\nacknowledged_overrides:\n  - {run_id}\n---\n\n# Acked\n",
        encoding="utf-8",
    )

    hits = find_overridden_decision_docs(repo, cat_dir, flag_docs=True)
    assert [h.doc_path for h in hits] == [".bth/docs/active_doc.md"]

    meta, _, _ = parse_doc_frontmatter(active_doc.read_text(encoding="utf-8"))
    assert meta["stale"] is True
    assert run_id in meta["overridden_runs"]
    assert "verdict_override='fail'" in meta["stale_reason"]

    # include_superseded=True still surfaces superseded docs when requested
    all_hits = find_overridden_decision_docs(repo, cat_dir, include_superseded=True)
    assert {h.doc_path for h in all_hits} == {
        ".bth/docs/active_doc.md",
        ".bth/docs/old_doc.md",
    }


def test_flag_docs_respects_anti_goal_a5_for_praxia_docs(tmp_path: Path):
    repo, cat_dir = _init_project(tmp_path)
    run_id = "55555555-6666-7777-8888-999999999999"
    _write_postmortem(repo, run_id=run_id, verdict_override="fail", hypothesis_status="refuted")

    praxia_decisions = repo / ".praxia" / "docs" / "decisions"
    praxia_decisions.mkdir(parents=True, exist_ok=True)
    praxia_doc = praxia_decisions / "261123_decision.md"
    praxia_doc.write_text(
        f"---\nstatus: active\nrun_id: {run_id}\n---\n\n# Decision\n",
        encoding="utf-8",
    )

    # Read-only detection works without touching .praxia/docs
    hits = find_overridden_decision_docs(repo, cat_dir, flag_docs=False)
    assert len(hits) == 1

    # Writing to .praxia/docs without explicit doc_store must be refused (Anti-Goal A5)
    with pytest.raises(DocStoreUnavailableError):
        find_overridden_decision_docs(repo, cat_dir, flag_docs=True)


def test_check_lint_and_postmortem_validate_surface_flagged_docs(tmp_path: Path, monkeypatch):
    repo, cat_dir = _init_project(tmp_path)
    run_id = "66666666-7777-8888-9999-aaaaaaaaaaaa"
    run = Run(
        id=run_id,
        project_slug="testproj",
        command="python run_sim.py",
        argv=["python", "run_sim.py"],
        git_hash=_git_head(repo),
        git_branch="main",
        git_dirty=False,
        status="completed",
        outcome="pass",
    )
    write_run(run, cat_dir)

    docs_dir = repo / ".bth" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "decision.md").write_text(
        f"---\nstatus: active\nrun_id: {run_id}\n---\n\nRelies on Run {run_id[:8]}.\n",
        encoding="utf-8",
    )

    pm_path = _write_postmortem(repo, run_id=run_id, verdict_override="fail", hypothesis_status="refuted")

    monkeypatch.chdir(repo)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(cat_dir))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(repo))

    # 1. postmortem_validate_tool surfaces flagged_docs + warning
    pm_res = postmortem_validate_tool(
        path=str(pm_path),
        workspace_root=str(repo),
        catalog_dir=str(cat_dir),
    )
    assert pm_res["validation_ok"] is True
    assert len(pm_res["flagged_docs"]) == 1
    assert any("stale_decision_doc" in w for w in pm_res["warnings"])

    # 2. check_tool + `bth check` CLI exit 1 when decision doc cites overridden run
    chk = check_tool(catalog_dir=str(cat_dir), project_root=str(repo))
    assert chk["flagged_doc_count"] == 1
    assert "error" in chk

    cli_chk = cli_runner.invoke(app, ["check", "--catalog-dir", str(cat_dir), "--project-root", str(repo)])
    assert cli_chk.exit_code == 1
    assert "postmortem-overridden" in cli_chk.output

    # 3. lint_tool + `bth lint` CLI exit 1 with overridden_citation issue
    lnt = lint_tool(project_root=str(repo))
    assert lnt["ok"] is False
    assert any(i["issue"] == "overridden_citation" for i in lnt["issues"])

    cli_lnt = cli_runner.invoke(app, ["lint", "--project-root", str(repo)])
    assert cli_lnt.exit_code == 1
    assert "error" in cli_lnt.output.lower()
