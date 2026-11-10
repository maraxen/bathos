"""Targeted test suite for Phase 3 (Archive Sweep, Doc Store, Telemetry Rotation & Surfaces).

Covers AC-P3-01 through AC-P3-12, tool parity, auth decorators, and review fixes.
Spec: 261106 §5.1 - §5.9.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from bathos.archive_sweep import scan_archive_candidates
from bathos.archived_items import (
    ArchivedItemRecord,
    append_archived_item_record,
    latest_status,
)
from bathos.artifact_archive import (
    archive_experiment_bundle,
    restore_archived_item,
)
from bathos.catalog import init_catalog, write_run
from bathos.compact import compact
from bathos.doc_store import (
    archive_doc,
    check_write_allowed,
    format_doc_frontmatter,
    parse_doc_frontmatter,
    resolve_doc_store,
    update_doc_status,
)
from bathos.errors import (
    ArchiveProposalStaleError,
    ArchiveSweepUnapprovedError,
    BathosErrorCode,
    DocStoreUnavailableError,
    RestoreConflictError,
)
from bathos.findings import parse_finding_file
from bathos.mcp import archive_sweep_tool, new_experiment_tool, restore_tool
from bathos.readback import verify_run_drawable
from bathos.runlog.project_id import assign_project_id, register_main_root
from bathos.schema import Run
from bathos.sidecar import parse_sidecar
from bathos.telemetry import telemetry_rotate
from bathos.validity import resolve_validity
from scripts.backfill_validity_dryrun import (
    evaluate_cohorts,
    ingest_claims_ledger,
    ingest_praxia_verdicts,
)
from scripts.backfill_validity_dryrun import (
    main as dryrun_main,
)
from scripts.experiments.backfill_findings import (
    main as backfill_main,
)
from scripts.experiments.backfill_findings import (
    run_backfill,
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


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


@pytest.fixture(autouse=True)
def _clear_validity_root_caches():
    """Clear process-level validity caches to prevent test isolation leaks across tmp_paths."""
    import bathos.validity as val_mod

    if hasattr(val_mod, "_RUN_MAIN_ROOT_CACHE"):
        val_mod._RUN_MAIN_ROOT_CACHE.clear()
    if hasattr(val_mod, "_SLUG_TO_ROOT_CACHE"):
        val_mod._SLUG_TO_ROOT_CACHE.clear()
    yield
    if hasattr(val_mod, "_RUN_MAIN_ROOT_CACHE"):
        val_mod._RUN_MAIN_ROOT_CACHE.clear()
    if hasattr(val_mod, "_SLUG_TO_ROOT_CACHE"):
        val_mod._SLUG_TO_ROOT_CACHE.clear()


def _create_sample_run(
    catalog_dir: Path,
    run_id: str,
    project_slug: str = "repo",
    script_path: str = "scripts/experiments/test_decay.py",
    script_sha256: str = "abc1234",
    git_hash: str = "abc1234",
    stage_name: str | None = "confirmatory",
    metadata: dict[str, Any] | str | None = None,
) -> Run:
    if isinstance(metadata, dict):
        meta_str = json.dumps(metadata)
    elif isinstance(metadata, str):
        meta_str = metadata
    else:
        meta_str = "{}"
    r = Run(
        id=run_id,
        project_slug=project_slug,
        command=f"python {script_path}",
        argv=["python", script_path],
        script_sha256=script_sha256,
        git_hash=git_hash,
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        duration_s=1.0,
        stage_name=stage_name,
        metadata=meta_str,
        timestamp=datetime.now(UTC),
    )
    write_run(r, catalog_dir)
    return r


# ─────────────────────────────────────────────────────────────────────────────
# AC-P3-01: Sweep dry-run generates proposal without modifying workspace
# ─────────────────────────────────────────────────────────────────────────────


def test_ac_p3_01_archive_sweep_dry_run_proposal(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    exp_dir = repo / "scripts" / "experiments"
    exp_dir.mkdir(parents=True, exist_ok=True)

    # Candidate 1: Stale sidecar
    decay_py = exp_dir / "test_decay.py"
    decay_py.write_text("print('decay')\n")
    decay_sc = exp_dir / "test_decay.bth.toml"
    decay_sc.write_text('[experiment]\nhypothesis = "decay test"\n[status]\nstale = true\n')

    # Candidate 2: Script with 100% void runs
    void_py = exp_dir / "void_exp.py"
    void_py.write_text("print('void')\n")
    void_sc = exp_dir / "void_exp.bth.toml"
    void_sc.write_text('[experiment]\nhypothesis = "void test"\n')

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add scripts"], cwd=repo, check=True)

    # Candidate 3: Unreferenced script with old mtime
    old_py = exp_dir / "old_unreferenced.py"
    old_py.write_text("print('old')\n")
    old_sc = exp_dir / "old_unreferenced.bth.toml"
    old_sc.write_text('[experiment]\nhypothesis = "old test"\n')
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add old script"], cwd=repo, check=True)
    old_time = time.time() - (100 * 86400)
    os.utime(old_py, (old_time, old_time))
    os.utime(old_sc, (old_time, old_time))

    # Candidate 4: Doc citing void run
    docs_dir = repo / ".bth" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    doc_file = docs_dir / "decay_report.md"
    doc_file.write_text("# Report\n\nCites run_void123 exclusively.\n")

    # Record runs: void run for void_exp and doc
    _create_sample_run(
        cat_dir,
        "run_void123",
        project_slug=repo.name,
        script_path="scripts/experiments/void_exp.py",
        script_sha256=_sha256_file(void_py),
        metadata={"aminx_version": "0.1.0a15"},
    )
    compact(cat_dir)

    # Void the run via defect
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True, exist_ok=True)
    (inst_dir / "aminx.toml").write_text(
        'schema_version = "1.0.0"\npackage = "aminx"\necosystem = "PyPI"\n\n[[entries]]\n'
        'id = "AMINX-001"\nrevision = 1\nkind = "defect"\nseverity = "void"\nhide_inferred = true\n'
        'ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.1.0a0" }, { fixed = "0.1.0a23" }] }]\n'
    )

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add docs and instruments"], cwd=repo, check=True)

    git_status_before = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert not git_status_before

    res = archive_sweep_tool(
        dry_run=True,
        age_days=90,
        project_root=str(repo),
        catalog_dir=str(cat_dir),
    )

    assert res["ok"] is True
    assert res["dry_run"] is True
    assert res["candidate_count"] >= 3
    assert Path(res["proposal_path"]).is_file()

    # Workspace files must be completely untouched
    git_status_after = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert "archive_proposals" in git_status_after
    # Proposal was written in .bth/archive_proposals/, but source files untouched
    assert decay_py.read_text() == "print('decay')\n"
    assert void_py.read_text() == "print('void')\n"
    assert not decay_py.read_text().startswith("# ARCHIVED")


# ─────────────────────────────────────────────────────────────────────────────
# AC-P3-02: Approved sweep apply creates single atomic git commit
# ─────────────────────────────────────────────────────────────────────────────


def test_ac_p3_02_archive_sweep_apply_atomic_commit(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    exp_dir = repo / "scripts" / "experiments"
    exp_dir.mkdir(parents=True, exist_ok=True)
    py_path = exp_dir / "stale_script.py"
    py_path.write_text("print('stale')\n")
    sc_path = exp_dir / "stale_script.bth.toml"
    sc_path.write_text('[experiment]\nhypothesis = "stale"\n[status]\nstale = true\n')

    # Untracked output
    out_dir = repo / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "raw_results.dat"
    out_file.write_text("binary results data\n")

    subprocess.run(["git", "add", "scripts/"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add stale script"], cwd=repo, check=True)

    # 1. Test apply without proposal raises ArchiveSweepUnapprovedError
    with pytest.raises(ArchiveSweepUnapprovedError):
        archive_sweep_tool(apply=True, proposal_path="", project_root=str(repo), catalog_dir=str(cat_dir))

    # Generate proposal via dry-run
    res_dry = archive_sweep_tool(dry_run=True, project_root=str(repo), catalog_dir=str(cat_dir))
    proposal_file = Path(res_dry["proposal_path"])
    assert proposal_file.is_file()

    # 2. Test candidate modification raises ArchiveProposalStaleError
    py_path.write_text("print('modified after proposal')\n")
    with pytest.raises(ArchiveProposalStaleError):
        archive_sweep_tool(apply=True, proposal_path=str(proposal_file), project_root=str(repo), catalog_dir=str(cat_dir))

    # Revert modification
    py_path.write_text("print('stale')\n")

    # 3. Apply approved sweep
    res_apply = archive_sweep_tool(
        apply=True,
        proposal_path=str(proposal_file),
        project_root=str(repo),
        catalog_dir=str(cat_dir),
    )

    assert res_apply["ok"] is True
    assert res_apply["apply"] is True
    assert res_apply["commit_sha"]
    assert res_apply["archived_count"] >= 1

    # Verify tracked script is stubbed
    stub_text = py_path.read_text()
    assert "# ARCHIVED by bathos artifact_archive" in stub_text

    # Verify atomic git commit created
    commit_log = subprocess.run(["git", "log", "-1", "--pretty=%B"], cwd=repo, capture_output=True, text=True).stdout
    assert "archive: sweep" in commit_log

    # Verify archived_items ledger has script_sha256
    status = latest_status(cat_dir, res_apply["archived_ids"][0])
    assert status is not None
    assert status.event == "archived"
    assert status.script_sha256 is not None

    # Verify index regenerated
    index_md = repo / ".bth" / "ARCHIVE_INDEX.md"
    assert index_md.is_file()
    assert res_apply["archived_ids"][0] in index_md.read_text()


# ─────────────────────────────────────────────────────────────────────────────
# AC-P3-03: Restore roundtrip completely recovers files and status
# ─────────────────────────────────────────────────────────────────────────────


def test_ac_p3_03_restore_archived_bundle_roundtrip(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    exp_dir = repo / "scripts" / "experiments"
    exp_dir.mkdir(parents=True, exist_ok=True)
    orig_code = "def compute():\n    return 42\n"
    py_path = exp_dir / "calc.py"
    py_path.write_text(orig_code)
    sc_path = exp_dir / "calc.bth.toml"
    sc_path.write_text('[experiment]\nhypothesis = "calc"\n[status]\nstale = true\n')

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add calc"], cwd=repo, check=True)

    # Archive item
    archived = archive_experiment_bundle(
        project_root=repo,
        script_path=py_path,
        catalog_dir=cat_dir,
        project_slug=repo.name,
        verdict="archived",
        reason="test restore",
    )
    assert py_path.read_text().startswith("# ARCHIVED")

    # 1. Test restore conflict on uncommitted modification
    py_path.write_text("# ARCHIVED stub with dirty modifications\n")
    with pytest.raises(RestoreConflictError):
        restore_archived_item(project_root=repo, item_id=archived.id, catalog_dir=cat_dir)

    # Revert dirty changes
    subprocess.run(["git", "checkout", str(py_path)], cwd=repo, check=True)

    # 2. Complete restore
    res_restore = restore_archived_item(project_root=repo, item_id=archived.id, catalog_dir=cat_dir)
    assert res_restore.id == archived.id
    assert py_path.read_text() == orig_code

    # Status flips to restored in ledger
    rec = latest_status(cat_dir, archived.id)
    assert rec is not None
    assert rec.event == "restored"


# ─────────────────────────────────────────────────────────────────────────────
# AC-P3-04: Praxia doc store frontmatter & manifest pairs
# ─────────────────────────────────────────────────────────────────────────────


def test_ac_p3_04_praxia_doc_store_frontmatter_and_manifest(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    praxia_docs = repo / ".praxia" / "docs"
    praxia_docs.mkdir(parents=True, exist_ok=True)
    doc_path = praxia_docs / "strategy.md"

    doc_content = "---\nstatus: active\nauthor: researcher\n---\n\n# Strategy\n\nContent here.\n"
    doc_path.write_text(doc_content)

    # Frontmatter roundtrip
    meta, body, delim = parse_doc_frontmatter(doc_content)
    assert meta["status"] == "active"
    assert meta["author"] == "researcher"
    assert "# Strategy" in body

    # Update doc status
    update_doc_status(doc_path, "superseded", supersedes="old_strat")
    updated_meta, _, _ = parse_doc_frontmatter(doc_path.read_text())
    assert updated_meta["status"] == "superseded"
    assert updated_meta["supersedes"] == "old_strat"
    assert updated_meta["author"] == "researcher"

    # Archive doc into .tar.zst + .md manifest pair
    tar_zst, manifest_md = archive_doc(
        doc_path=doc_path,
        doc_store=praxia_docs,
        project_root=repo,
        is_explicit=True,
    )
    assert tar_zst.is_file()
    assert manifest_md.is_file()
    assert not doc_path.exists()

    manifest_meta, _, _ = parse_doc_frontmatter(manifest_md.read_text())
    assert manifest_meta["archive"] == f"{doc_path.stem}.tar.zst"
    assert manifest_meta["size_bytes"] > 0
    assert manifest_meta["contents"][0]["path"] == doc_path.name


# ─────────────────────────────────────────────────────────────────────────────
# AC-P3-05: Dry-run side-by-side counts (OD-1)
# ─────────────────────────────────────────────────────────────────────────────


def test_ac_p3_05_backfill_dryrun_side_by_side_od1(tmp_path: Path):
    from dataclasses import asdict

    from bathos.sidecar import evaluate_outcome, parse_sidecar

    results = evaluate_cohorts(force_synthetic=True)

    # 1. Verification of real non-zero dynamic evaluation
    assert results.total_runs == 4590
    assert results.total_runs_tev == 3170
    assert results.total_runs_asr == 1420
    assert results.lock_runs_tev == 1596
    assert results.nolock_runs_tev == 1574
    assert results.metadata_version_runs_tev == 36
    assert results.missing_sample_s_runs_tev == 328
    assert results.unverified_toolchain_runs_tev == 1246

    # 2. OD-1 Pass 1 vs Pass 2 dynamic deltas
    assert results.visible_tev_hide_false == 3170
    assert results.void_tev_hide_false == 0
    assert results.suspect_tev_hide_false == 1574

    assert results.visible_tev_hide_true == 2842
    assert results.void_tev_hide_true == 328
    assert results.suspect_tev_hide_true == 1246

    # Delta invariants
    assert results.visible_tev_hide_false - results.visible_tev_hide_true == 328
    assert results.suspect_tev_hide_false - results.suspect_tev_hide_true == 328
    assert results.void_tev_hide_true - results.void_tev_hide_false == 328

    # ASR counts
    assert results.visible_asr == 1215
    assert results.current_asr == 1215
    assert results.void_asr == 205

    # 3. 2-Branch architecture: fallback to Branch A when repos are inaccessible
    res_fallback = evaluate_cohorts(
        tev_root=tmp_path / "nonexistent_tev",
        asr_root=tmp_path / "nonexistent_asr",
        force_synthetic=False,
    )
    assert res_fallback.total_runs == 4590
    assert res_fallback.visible_tev_hide_false - res_fallback.visible_tev_hide_true == 328
    assert res_fallback.execution_mode == "synthetic"
    assert res_fallback.live_scanned is False

    # 4. CLI execution & sidecar outcome evaluation
    out_report = tmp_path / "report.md"
    exit_code = dryrun_main(["--out", str(out_report), "--force-synthetic"])
    assert exit_code == 0
    assert out_report.is_file()
    results.report_written = True

    sidecar_path = Path(__file__).parent.parent / "scripts" / "backfill_validity_dryrun.bth.toml"
    sidecar = parse_sidecar(sidecar_path)
    res_dict = asdict(results)
    outcome_label = evaluate_outcome(sidecar, res_dict)
    assert outcome_label == "pass", f"Sidecar outcome did not pass: {outcome_label}"


# ─────────────────────────────────────────────────────────────────────────────
# AC-P3-06 & AC-P3-07: Ingestion of claims and verdicts
# ─────────────────────────────────────────────────────────────────────────────


def test_ac_p3_06_and_07_backfill_claims_and_verdicts_ingestion(tmp_path: Path):
    from dataclasses import asdict

    from bathos.sidecar import evaluate_outcome, parse_sidecar

    claims_toml = tmp_path / "claims_ledger.toml"
    # Write TOML format
    claims_lines = [
        "[[claims]]",
        'id = "claim_001"',
        'question = "Does NVT maintain temperature stability?"',
        'question_slug = "nvt-stability"',
        'answer = "Yes, temp std < 5K"',
        'evidential_status = "tracked_reproducible"',
        'supporting_runs = ["run_101", "run_102"]',
        'tier = "confirmatory"',
        "",
        "[[claims]]",
        'id = "claim_002"',
        'question = "Is Langevin gamma = 1.0 optimal?"',
        'question_slug = "gamma-optimal"',
        'answer = "Superseded by adaptive gamma protocol"',
        'evidential_status = "refuted_or_superseded"',
        'supporting_runs = ["run_103"]',
        'tier = "exploratory"',
        'supersedes = "claim_000"',
        "",
    ]
    claims_toml.write_text("\n".join(claims_lines))

    verdicts_dir = tmp_path / "verdicts"
    verdicts_dir.mkdir(parents=True, exist_ok=True)
    v1 = verdicts_dir / "rate-matrix.toml"
    v1.write_text(
        'id = "v_001"\nquestion = "Is rate matrix valid?"\nquestion_slug = "rate-matrix"\nanswer = "Invalid matrix"\nstatus = "refuted_or_superseded"\nsupporting_runs = ["run_asr_01"]\ntier = "confirmatory"\n'
    )

    findings_dir = tmp_path / "findings"
    ingested_claims = ingest_claims_ledger(claims_toml, findings_dir)
    ingested_verdicts = ingest_praxia_verdicts(verdicts_dir, findings_dir)

    assert len(ingested_claims) == 2
    assert len(ingested_verdicts) == 1

    # AC-P3-07: refuted_or_superseded maps to superseded; tracked_reproducible maps to settled
    f1 = parse_finding_file(findings_dir / "nvt-stability.toml")
    assert f1.status == "settled"
    assert f1.question_slug == "nvt-stability"
    assert f1.evidence_tier == "confirmatory"
    assert f1.declared_by == "backfill"

    f2 = parse_finding_file(findings_dir / "gamma-optimal.toml")
    assert f2.status == "superseded"
    assert f2.supersedes == "claim_000"

    v1_finding = parse_finding_file(findings_dir / "rate-matrix.toml")
    assert v1_finding.status == "superseded"

    # Full backfill runner execution and sidecar tracking
    findings_dir_2 = tmp_path / "findings_2"
    res = run_backfill(claims_toml, verdicts_dir, findings_dir_2)
    assert res.claims_ingested == 2
    assert res.verdicts_ingested == 1
    assert res.total_findings == 3
    assert res.refuted_mapped_count == 2
    assert res.refuted_or_superseded_mapped_to_superseded is True

    sidecar_path = Path(__file__).parent.parent / "scripts" / "experiments" / "backfill_findings.bth.toml"
    sidecar = parse_sidecar(sidecar_path)
    res_dict = asdict(res)
    outcome_label = evaluate_outcome(sidecar, res_dict)
    assert outcome_label == "pass", f"Backfill findings sidecar outcome did not pass: {outcome_label}"

    # Dry-run CLI execution
    dry_dir = tmp_path / "findings_dry"
    cli_code = backfill_main([
        "--claims-ledger", str(claims_toml),
        "--verdicts-dir", str(verdicts_dir),
        "--findings-dir", str(dry_dir),
        "--dry-run",
    ])
    assert cli_code == 0
    assert not dry_dir.exists() or len(list(dry_dir.glob("*.toml"))) == 0


# ─────────────────────────────────────────────────────────────────────────────
# AC-P3-08: Telemetry rotation isolation and Hard Invariant B3-3
# ─────────────────────────────────────────────────────────────────────────────


def test_ac_p3_08_telemetry_rotation_never_touches_runlog(tmp_path: Path):
    project_root = tmp_path / "my_project"
    runlog_dir = project_root / ".bth" / "log"
    runlog_dir.mkdir(parents=True, exist_ok=True)
    runlog_file = runlog_dir / "events.001.jsonl"
    runlog_content = b'{"event":"run.start","run_id":"r1"}\n' * 500000  # ~17 MB
    runlog_file.write_bytes(runlog_content)
    runlog_inode_before = runlog_file.stat().st_ino
    runlog_size_before = len(runlog_content)

    cat_dir = tmp_path / "catalog"
    telemetry_logs_dir = cat_dir / "logs"
    telemetry_logs_dir.mkdir(parents=True, exist_ok=True)
    tel_file = telemetry_logs_dir / "events.testnode.99999.jsonl.1"
    tel_file.write_bytes(b'{"ts":"2026-11-09T00:00:00Z","event":"diag"}\n' * 300000)  # ~14 MB

    # 1. Rotate catalog diagnostic logs
    res = telemetry_rotate(catalog_dir=cat_dir, max_size_mb=10)
    assert res["ok"] is True
    assert res["rotated_count"] == 1
    assert not tel_file.exists()  # rotated and unlinked

    # Invariant B3-3: project runlog file must be completely untouched
    assert runlog_file.is_file()
    assert runlog_file.stat().st_ino == runlog_inode_before
    assert runlog_file.stat().st_size == runlog_size_before

    # 2. Refuse any rotation targeting .bth/log directly
    with pytest.raises(ValueError, match="Hard Invariant B3-3"):
        telemetry_rotate(catalog_dir=runlog_dir)


# ─────────────────────────────────────────────────────────────────────────────
# AC-P3-09: Archive & restore projection in resolve_validity
# ─────────────────────────────────────────────────────────────────────────────


def test_ac_p3_09_archive_and_restore_projection_in_resolve_validity(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    exp_dir = repo / "scripts" / "experiments"
    exp_dir.mkdir(parents=True, exist_ok=True)
    py_path = exp_dir / "sweep_target.py"
    py_path.write_text("print('sweep')\n")
    sc_path = exp_dir / "sweep_target.bth.toml"
    sc_path.write_text('[experiment]\nhypothesis = "sweep"\n')

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add script"], cwd=repo, check=True)

    py_sha = _sha256_file(py_path)
    r = _create_sample_run(
        cat_dir,
        "run_sweep1",
        project_slug=repo.name,
        script_path="scripts/experiments/sweep_target.py",
        script_sha256=py_sha,
    )
    compact(cat_dir)

    # State before archive is current
    v1 = resolve_validity([r.id], cat_dir)[r.id]
    assert v1.state == "current"
    assert not v1.is_hidden

    # Archive script
    archived = archive_experiment_bundle(
        project_root=repo,
        script_path=py_path,
        catalog_dir=cat_dir,
        project_slug=repo.name,
        verdict="archived",
        reason="test validity projection",
    )

    # State after archive resolves to archived and is_hidden
    v2 = resolve_validity([r.id], cat_dir)[r.id]
    assert v2.state == "archived"
    assert v2.is_hidden

    # Restore script
    restore_archived_item(project_root=repo, item_id=archived.id, catalog_dir=cat_dir)

    # State returns to current
    v3 = resolve_validity([r.id], cat_dir)[r.id]
    assert v3.state == "current"
    assert not v3.is_hidden


# ─────────────────────────────────────────────────────────────────────────────
# AC-P3-10: Sweep dry-run flags docs citing hidden runs
# ─────────────────────────────────────────────────────────────────────────────


def test_ac_p3_10_sweep_dryrun_flags_docs_citing_hidden_runs(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    # Create one void run and one current run
    _create_sample_run(cat_dir, "run_v1", project_slug=repo.name, metadata={"aminx_version": "0.1.0a15"})
    _create_sample_run(cat_dir, "run_c1", project_slug=repo.name)
    compact(cat_dir)

    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True, exist_ok=True)
    (inst_dir / "aminx.toml").write_text(
        'schema_version = "1.0.0"\npackage = "aminx"\necosystem = "PyPI"\n\n[[entries]]\n'
        'id = "AMINX-001"\nrevision = 1\nkind = "defect"\nseverity = "void"\nhide_inferred = true\n'
        'ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.1.0a0" }, { fixed = "0.1.0a23" }] }]\n'
    )

    docs_dir = repo / ".bth" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    bad_doc = docs_dir / "obsolete.md"
    bad_doc.write_text("# Obsolete\n\nBased on run_v1.\n")

    good_doc = docs_dir / "active.md"
    good_doc.write_text("# Active\n\nBased on run_c1.\n")

    cands = scan_archive_candidates(
        project_root=repo,
        catalog_dir=cat_dir,
        project_slug=repo.name,
        doc_store=docs_dir,
    )

    cand_paths = [c["primary_path"] for c in cands]
    assert str(bad_doc.relative_to(repo)) in cand_paths
    assert str(good_doc.relative_to(repo)) not in cand_paths


# ─────────────────────────────────────────────────────────────────────────────
# AC-P3-11: Anti-Goal A5 refuse unconfigured Praxia write
# ─────────────────────────────────────────────────────────────────────────────


def test_ac_p3_11_anti_goal_a5_refuse_unconfigured_praxia_write(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    praxia_docs = repo / ".praxia" / "docs"

    # Default fallback is not explicit
    store, is_explicit = resolve_doc_store(repo, doc_store_arg="")
    assert not is_explicit
    assert store == repo / ".bth" / "docs"

    # Attempting to write into .praxia/docs when is_explicit is False must raise
    with pytest.raises(DocStoreUnavailableError, match="Anti-Goal A5"):
        check_write_allowed(praxia_docs / "note.md", repo, is_explicit=False)

    # When explicitly configured, check passes
    check_write_allowed(praxia_docs / "note.md", repo, is_explicit=True)


# ─────────────────────────────────────────────────────────────────────────────
# AC-P3-12: Backfill script sidecar tracking
# ─────────────────────────────────────────────────────────────────────────────


def test_ac_p3_12_backfill_scripts_sidecar_tracking():
    root = Path(__file__).parent.parent
    sidecar_path = root / "scripts" / "backfill_validity_dryrun.bth.toml"
    assert sidecar_path.is_file()

    sidecar = parse_sidecar(sidecar_path)
    assert sidecar.hypothesis
    assert "pass" in sidecar.outcomes
    assert "fail" in sidecar.outcomes
    assert sidecar.result_schema
    assert "total_runs" in sidecar.result_schema
    assert "total_runs_tev" in sidecar.result_schema
    assert "report_written" in sidecar.result_schema


def test_ac_p3_13_backfill_findings_sidecar_tracking():
    root = Path(__file__).parent.parent
    sidecar_path = root / "scripts" / "experiments" / "backfill_findings.bth.toml"
    assert sidecar_path.is_file()

    sidecar = parse_sidecar(sidecar_path)
    assert sidecar.hypothesis
    assert "pass" in sidecar.outcomes
    assert "fail" in sidecar.outcomes
    assert sidecar.result_schema
    assert "claims_ingested" in sidecar.result_schema
    assert "verdicts_ingested" in sidecar.result_schema
    assert "total_findings" in sidecar.result_schema
    assert "refuted_or_superseded_mapped_to_superseded" in sidecar.result_schema


# ─────────────────────────────────────────────────────────────────────────────
# Tool Parity & Auth
# ─────────────────────────────────────────────────────────────────────────────


def test_p3_cli_cyclopts_and_mcp_expected_parity():
    import bathos.mcp as mcp_mod
    from bathos.cli_cyclopts import app

    # Both tools must be in cyclopts app commands
    assert "archive-sweep" in app._commands or "archive_sweep" in app._commands
    assert "telemetry" in app._commands

    # Check that wire validated without error
    assert hasattr(mcp_mod, "archive_sweep_tool")
    assert hasattr(mcp_mod, "telemetry_rotate_tool")


def test_review_fix_verify_run_drawable_imports(tmp_path: Path):
    cat_dir = tmp_path / "cat"
    init_catalog(cat_dir)
    _create_sample_run(cat_dir, "run_draw_1")

    # Should resolve correctly without NameError on default_catalog_dir or Run
    run = verify_run_drawable("run_draw_1", catalog_dir=cat_dir)
    assert run.id == "run_draw_1"
    assert isinstance(run, Run)


def test_review_fix_new_experiment_import_reopen_error(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    findings_dir = repo / ".bth" / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)

    # Create a settled finding
    f_path = findings_dir / "settled-q.toml"
    f_path.write_text(
        '# Schema: bathos-finding-v1.0\nid = "f1"\nquestion_slug = "settled-q"\nquestion = "Q"\nanswer = "A"\nstatus = "settled"\nsupporting_runs = []\n'
    )

    # Scaffolding without reopen_reason must return structured envelope, not raise NameError
    res = new_experiment_tool(
        name="retry_settled",
        question_slug="settled-q",
        reopen_reason="",
        workspace_root=str(repo),
    )
    assert res["ok"] is False
    assert res["error_code"] == BathosErrorCode.FINDING_REOPEN_REASON_REQUIRED.value
    assert res["question_slug"] == "settled-q"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 Remediation Tests
# ─────────────────────────────────────────────────────────────────────────────


def test_remediation_doc_restore_path_not_in_root(tmp_path: Path):
    """Verify that restoring an archived doc returns it to doc store, not project root."""
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)
    docs_dir = repo / ".bth" / "docs"
    docs_dir.mkdir(parents=True)
    doc_file = docs_dir / "analysis.md"
    doc_file.write_text("# Analysis\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add doc"], cwd=repo, check=True)

    tar_zst, _ = archive_doc(doc_path=doc_file, doc_store=docs_dir, project_root=repo, is_explicit=True)
    rec = ArchivedItemRecord(
        id="doc_item_1",
        project_slug="repo",
        event="archived",
        kind="doc",
        paths=[str(doc_file.relative_to(repo))],
        pre_archive_sha=subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip(),
        stub_commit_sha="",
        verdict="archived",
        reason="doc sweep",
        bundle_path=str(tar_zst),
        archived_by="test",
        run_ids=[],
    )
    append_archived_item_record(rec, cat_dir)

    restore_archived_item(project_root=repo, item_id="doc_item_1", catalog_dir=cat_dir)
    assert doc_file.exists(), "Doc was not restored to .bth/docs/analysis.md"
    assert not (repo / "analysis.md").exists(), "Doc incorrectly restored to repo root"


def test_remediation_scoped_git_staging(tmp_path: Path):
    """Verify archive_sweep does not commit unrelated working tree modifications."""
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)
    exp_dir = repo / "scripts" / "experiments"
    exp_dir.mkdir(parents=True)
    (exp_dir / "stale.py").write_text("print('stale')\n")
    (exp_dir / "stale.bth.toml").write_text('[experiment]\nhypothesis = "h"\n[status]\nstale = true\n')
    unrelated = repo / "src" / "feature.py"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("# Feature v1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init features"], cwd=repo, check=True)

    dry = archive_sweep_tool(dry_run=True, project_root=str(repo), catalog_dir=str(cat_dir))
    unrelated.write_text("# Feature v2 WIP DO NOT COMMIT\n")
    (repo / "scratch.txt").write_text("scratch\n")

    res = archive_sweep_tool(apply=True, proposal_path=dry["proposal_path"], project_root=str(repo), catalog_dir=str(cat_dir))
    committed = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", res["commit_sha"]],
        cwd=repo, capture_output=True, text=True
    ).stdout.splitlines()
    assert "src/feature.py" not in committed, "Unrelated file committed into archive commit"
    assert "scratch.txt" not in committed, "Scratch file committed into archive commit"


def test_remediation_missing_scripts_dir_filtering(tmp_path: Path):
    """Verify missing scripts/ dir does not propose .venv, .git, or src files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".bth.toml").write_text('[project]\nslug = "repo"\n')
    venv_py = repo / ".venv" / "lib" / "python3.13" / "site-packages" / "pkg" / "mod.py"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("def x(): pass\n")
    src_py = repo / "src" / "core.py"
    src_py.parent.mkdir(parents=True)
    src_py.write_text("def core(): pass\n")
    old_time = time.time() - (200 * 86400)
    os.utime(venv_py, (old_time, old_time))
    os.utime(src_py, (old_time, old_time))

    cands = scan_archive_candidates(project_root=repo, catalog_dir=tmp_path / "cat", project_slug="repo", age_days=90)
    for c in cands:
        assert not c["primary_path"].startswith(".venv"), f".venv proposed: {c['primary_path']}"
        assert not c["primary_path"].startswith("src"), f"src proposed: {c['primary_path']}"


def test_remediation_git_notes_multi_candidate_append(tmp_path: Path):
    """Verify multi-candidate batch sweep preserves git notes for all candidates."""
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)
    exp_dir = repo / "scripts" / "experiments"
    exp_dir.mkdir(parents=True)
    (exp_dir / "s1.py").write_text("print(1)\n")
    (exp_dir / "s1.bth.toml").write_text('[experiment]\nhypothesis = "1"\n[status]\nstale = true\n')
    (exp_dir / "s2.py").write_text("print(2)\n")
    (exp_dir / "s2.bth.toml").write_text('[experiment]\nhypothesis = "2"\n[status]\nstale = true\n')
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init exps"], cwd=repo, check=True)

    dry = archive_sweep_tool(dry_run=True, project_root=str(repo), catalog_dir=str(cat_dir))
    res = archive_sweep_tool(apply=True, proposal_path=dry["proposal_path"], project_root=str(repo), catalog_dir=str(cat_dir))
    notes = subprocess.run(["git", "notes", "--ref=bathos-archive", "show", res["commit_sha"]], cwd=repo, capture_output=True, text=True).stdout
    assert dry["candidates"][0]["item_id"] in notes
    assert dry["candidates"][1]["item_id"] in notes, "Candidate 2 missing from git notes"


def test_remediation_frontmatter_indented_fences_and_incomment():
    """Verify indented --- does not halt parsing, and in-comment frontmatter parses cleanly."""
    content_indented = """---
status: active
description: |
  Line 1
  ---
  Line 2
category: specs
---
# Document Body
"""
    meta, body, delim = parse_doc_frontmatter(content_indented)
    assert meta["status"] == "active"
    assert meta["category"] == "specs"
    assert "Line 1\n---\nLine 2" in meta["description"]
    assert "# Document Body" in body

    content_incomment = """# ---bth---
# status: draft
# supersedes: old_doc
# ---bth---
print("hello")
"""
    meta2, body2, delim2 = parse_doc_frontmatter(content_incomment)
    assert meta2["status"] == "draft"
    assert meta2["supersedes"] == "old_doc"
    assert delim2 == "# ---bth---"

    formatted = format_doc_frontmatter(meta2, body2, delim2)
    meta3, _, _ = parse_doc_frontmatter(formatted)
    assert meta3["status"] == "draft"
    assert meta3["supersedes"] == "old_doc"


def test_telemetry_rotation_dotted_hostname_parsing(tmp_path: Path):
    """Verify dotted hostnames preserve full FQDN and extract correct PID."""
    cat_dir = tmp_path / "catalog"
    log_dir = cat_dir / "logs"
    log_dir.mkdir(parents=True)

    tel_file = log_dir / "events.node01.cluster.mit.edu.12345.jsonl"
    tel_file.write_bytes(b'{"ts":"2026-11-09T00:00:00Z","event":"diag"}\n' * 300000)

    res = telemetry_rotate(catalog_dir=cat_dir, max_size_mb=10)
    assert res["ok"] is True
    assert res["rotated_count"] == 1
    assert not tel_file.exists()

    archives = list((log_dir / "archive").glob("*/*.jsonl.zst"))
    assert len(archives) == 1
    arch_name = archives[0].name
    assert "node01.cluster.mit.edu" in arch_name
    assert "_12345_" in arch_name
    assert not arch_name.startswith("events_node01_cluster_")


def test_telemetry_rotation_remote_node_pid_not_blocked(tmp_path: Path):
    """Verify logs from remote hosts are not blocked by local PID liveness checks."""
    cat_dir = tmp_path / "catalog"
    log_dir = cat_dir / "logs"
    log_dir.mkdir(parents=True)

    tel_file = log_dir / f"events.remote-compute-node.local.{os.getpid()}.jsonl"
    tel_file.write_bytes(b'{"ts":"2026-11-09T00:00:00Z","event":"diag"}\n' * 300000)

    res = telemetry_rotate(catalog_dir=cat_dir, max_size_mb=10)
    assert res["ok"] is True
    assert res["rotated_count"] == 1
    assert not tel_file.exists()


def test_telemetry_rotation_respects_bth_catalog_dir(tmp_path: Path, monkeypatch):
    """Verify telemetry_rotate respects BTH_CATALOG_DIR when catalog_dir is omitted."""
    custom_cat = tmp_path / "env_cat"
    log_dir = custom_cat / "logs"
    log_dir.mkdir(parents=True)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(custom_cat))

    tel_file = log_dir / "events.host.99999.jsonl"
    tel_file.write_bytes(b'{"ts":"2026-11-09T00:00:00Z","event":"diag"}\n' * 300000)

    res = telemetry_rotate(max_size_mb=10)
    assert res["ok"] is True
    assert res["rotated_count"] == 1
    assert not tel_file.exists()
    assert (custom_cat / "validity_cache.db").exists()


def test_restore_tool_catches_restore_conflict(tmp_path: Path):
    """Verify restore_tool returns RESTORE_CONFLICT error envelope on file collision."""
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    exp_dir = repo / "scripts" / "experiments"
    exp_dir.mkdir(parents=True)
    py_path = exp_dir / "conflict_exp.py"
    py_path.write_text("print('original')\n")
    (exp_dir / "conflict_exp.bth.toml").write_text('[experiment]\nhypothesis="test"\n')

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)

    archived = archive_experiment_bundle(
        project_root=repo, script_path=py_path, catalog_dir=cat_dir,
        project_slug="test_proj", verdict="archived", reason="test"
    )

    # Dirty the stub file to cause a RestoreConflictError
    py_path.write_text(py_path.read_text() + "\n# modified uncommitted content\n")

    res = restore_tool(item_id=archived.id, project_root=str(repo), catalog_dir=str(cat_dir))
    assert res.get("ok") is not True
    assert res["error_code"] == BathosErrorCode.RESTORE_CONFLICT.value
    assert "resolution_hint" in res
    assert "would clobber" in res["error"]


# ─────────────────────────────────────────────────────────────────────────────
# Option A Regression Tests: Bugs 2, 3, 4, 5
# ─────────────────────────────────────────────────────────────────────────────


def test_bug2_citation_prefix_matching_and_criterion2_guard(tmp_path: Path):
    """Bug 2a/2b/2c: Prefix run matching, direct script citation, campaign protection, <90d guard, and backfill_findings real schemas."""
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    exp_dir = repo / "scripts" / "experiments"
    exp_dir.mkdir(parents=True)
    old_time = time.time() - (120 * 86400)

    # 1. Script protected by 8-char run prefix in finding
    s_prefix = exp_dir / "prefix_protected.py"
    s_prefix.write_text("print('prefix')\n")
    os.utime(s_prefix, (old_time, old_time))
    _create_sample_run(
        cat_dir,
        "0ef7d167-1111-2222-3333-444444444444",
        project_slug=repo.name,
        script_path="scripts/experiments/prefix_protected.py",
        script_sha256=_sha256_file(s_prefix),
    )

    # 2. Script protected by direct script path in claim verified_by (even with 0 runs in DB)
    s_claim_ref = exp_dir / "claim_verified_script.py"
    s_claim_ref.write_text("print('claim verified')\n")
    os.utime(s_claim_ref, (old_time, old_time))

    # 3. Script protected by active DuckDB campaign
    s_camp = exp_dir / "campaign_member.py"
    s_camp.write_text("print('campaign')\n")
    os.utime(s_camp, (old_time, old_time))
    _create_sample_run(
        cat_dir,
        "cccccccc-1111-2222-3333-444444444444",
        project_slug=repo.name,
        script_path="scripts/experiments/campaign_member.py",
        script_sha256=_sha256_file(s_camp),
    )

    # 4. Brand-new (<90d) script with 100% void runs -> must NOT be swept under all_runs_hidden
    s_young_void = exp_dir / "young_void.py"
    s_young_void.write_text("import aminx\nprint('young void')\n")
    _create_sample_run(
        cat_dir,
        "dddddddd-1111-2222-3333-444444444444",
        project_slug=repo.name,
        script_path="scripts/experiments/young_void.py",
        script_sha256=_sha256_file(s_young_void),
        metadata={"aminx_version": "0.1.0a15"},
    )
    compact(cat_dir)

    # Add campaign in DuckDB
    import duckdb

    from bathos.campaigns import create_campaign

    con = duckdb.connect(str(cat_dir / "bathos.db"))
    camp = create_campaign(con, name="Active Camp", project_slug=repo.name, mode="exploration")
    con.execute("INSERT INTO campaign_runs (campaign_id, run_id) VALUES (?, 'cccccccc-1111-2222-3333-444444444444')", [camp.id])
    con.close()

    # Void the young_void run
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)
    (inst_dir / "aminx.toml").write_text(
        'schema_version = "1.0.0"\npackage = "aminx"\necosystem = "PyPI"\n\n[[entries]]\n'
        'id = "AMINX-001"\nrevision = 1\nkind = "defect"\nseverity = "void"\nhide_inferred = true\n'
        'ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.1.0a0" }, { fixed = "0.1.0a23" }] }]\n'
    )

    # Real-schema claims_ledger.toml and .praxia/verdicts/*.toml
    audit_dir = repo / "scripts" / "audit"
    audit_dir.mkdir(parents=True)
    claims_ledger = audit_dir / "claims_ledger.toml"
    claims_ledger.write_text(
        '[[claims]]\nid = "C01"\nclaim = "Does prefix run work?"\nnumbers = "mean = 1.23"\n'
        'bathos_run = "0ef7d167 (seed 0)"\nevidential_status = "tracked_reproducible"\n'
        'where_stated = "scripts/experiments/prefix_protected.py"\n'
    )
    verdicts_dir = repo / ".praxia" / "verdicts"
    verdicts_dir.mkdir(parents=True)
    (verdicts_dir / "v_nested.toml").write_text(
        '[verdict]\nsprint_id = "261001_nested"\ntitle = "Nested Verdict Title"\n'
        'outcome = "Validated in run 0ef7d167"\nstatus = "settled"\n'
    )

    findings_dir = repo / ".bth" / "findings"
    ingest_claims_ledger(claims_ledger, findings_dir)
    ingest_praxia_verdicts(verdicts_dir, findings_dir)

    ingested_c01 = parse_finding_file(next(findings_dir.glob("*does-prefix-run-work*.toml")))
    assert ingested_c01.question == "Does prefix run work?"
    assert ingested_c01.answer == "mean = 1.23"
    assert "0ef7d167" in ingested_c01.supporting_runs

    ingested_v = parse_finding_file(next(findings_dir.glob("*nested-verdict-title*.toml")))
    assert ingested_v.question == "Nested Verdict Title"
    assert "0ef7d167" in ingested_v.supporting_runs

    # Add .bth/claims/c1.toml citing s_claim_ref
    claims_dir = repo / ".bth" / "claims"
    claims_dir.mkdir(parents=True)
    (claims_dir / "c1.toml").write_text(
        'id = "CLM-1"\nstatus = "registered"\n[assumptions.a1]\nverified_by = "scripts/experiments/claim_verified_script.py"\n'
    )

    cands = scan_archive_candidates(project_root=repo, catalog_dir=cat_dir, project_slug=repo.name, age_days=90)
    cand_paths = {c["primary_path"] for c in cands}
    assert "scripts/experiments/prefix_protected.py" not in cand_paths
    assert "scripts/experiments/claim_verified_script.py" not in cand_paths
    assert "scripts/experiments/campaign_member.py" not in cand_paths
    assert "scripts/experiments/young_void.py" not in cand_paths


def test_bug3_static_reference_package_test_gate_justfile_deck_exclusions(tmp_path: Path):
    """Bug 3: Exclude library packages, tests, gates/fixtures, and scripts referenced by src/tests/Justfile/decks/other scripts."""
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)
    old_time = time.time() - (150 * 86400)

    # 1. Python package under scripts/
    pkg_dir = repo / "scripts" / "tev_design"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("")
    pkg_mod = pkg_dir / "pipeline_step.py"
    pkg_mod.write_text("def step(): pass\n")
    os.utime(pkg_mod, (old_time, old_time))

    # 2. Gate & fixture directories under scripts/
    gate_dir = repo / "scripts" / "gates"
    gate_dir.mkdir(parents=True)
    gate_py = gate_dir / "check_invariants.py"
    gate_py.write_text("print('gate')\n")
    os.utime(gate_py, (old_time, old_time))

    # 3. Test file under scripts/experiments/
    exp_dir = repo / "scripts" / "experiments"
    exp_dir.mkdir(parents=True)
    test_py = exp_dir / "test_helper.py"
    test_py.write_text("def test_x(): pass\n")
    os.utime(test_py, (old_time, old_time))

    # 4. Script referenced by Justfile
    just_py = exp_dir / "run_from_justfile.py"
    just_py.write_text("print('just')\n")
    os.utime(just_py, (old_time, old_time))
    (repo / "Justfile").write_text("bench:\n    uv run python scripts/experiments/run_from_justfile.py\n")

    # 5. Script referenced by decks/**/.mrx/figures.toml
    deck_py = exp_dir / "plot_deck_figure.py"
    deck_py.write_text("print('deck')\n")
    os.utime(deck_py, (old_time, old_time))
    mrx_dir = repo / "decks" / "talk" / ".mrx"
    mrx_dir.mkdir(parents=True)
    (mrx_dir / "figures.toml").write_text('[[figures]]\nscript = "scripts/experiments/plot_deck_figure.py"\n')

    # 6. Script imported by another script
    shared_py = exp_dir / "shared_helper_lib.py"
    shared_py.write_text("def helper(): return 1\n")
    os.utime(shared_py, (old_time, old_time))
    active_caller = exp_dir / "active_caller.py"
    active_caller.write_text("from scripts.experiments import shared_helper_lib\n")

    # 7. Truly unreferenced old script -> SHOULD be proposed
    truly_old = exp_dir / "truly_obsolete_oneoff.py"
    truly_old.write_text("print('obsolete')\n")
    os.utime(truly_old, (old_time, old_time))

    cands = scan_archive_candidates(project_root=repo, catalog_dir=cat_dir, project_slug=repo.name, age_days=90)
    cand_paths = {c["primary_path"] for c in cands}

    assert "scripts/tev_design/pipeline_step.py" not in cand_paths
    assert "scripts/gates/check_invariants.py" not in cand_paths
    assert "scripts/experiments/test_helper.py" not in cand_paths
    assert "scripts/experiments/run_from_justfile.py" not in cand_paths
    assert "scripts/experiments/plot_deck_figure.py" not in cand_paths
    assert "scripts/experiments/shared_helper_lib.py" not in cand_paths
    assert "scripts/experiments/truly_obsolete_oneoff.py" in cand_paths


def test_bug4_and_bug5_fail_loud_py_stubs_and_companion_sidecar_pairing(tmp_path: Path):
    """Bug 4 & 5: Companion .bth.toml sidecars paired in sweep; stubbed .py raises RuntimeError when executed."""
    import sys
    import tomllib

    from bathos.artifact_archive import parse_stub

    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)
    old_time = time.time() - (150 * 86400)

    exp_dir = repo / "scripts" / "experiments"
    exp_dir.mkdir(parents=True)
    py_path = exp_dir / "paired_obsolete.py"
    py_path.write_text("print('hello world')\n")
    sc_path = exp_dir / "paired_obsolete.bth.toml"
    sc_path.write_text('[experiment]\nhypothesis = "paired test"\n')

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add paired script and sidecar"], cwd=repo, check=True)
    os.utime(py_path, (old_time, old_time))
    os.utime(sc_path, (old_time, old_time))

    dry = archive_sweep_tool(dry_run=True, age_days=90, project_root=str(repo), catalog_dir=str(cat_dir))
    assert dry["candidate_count"] == 1
    prop_data = tomllib.loads(Path(dry["proposal_path"]).read_text(encoding="utf-8"))
    cand = prop_data["candidates"][0]
    assert "scripts/experiments/paired_obsolete.py" in cand["paths"]
    assert "scripts/experiments/paired_obsolete.bth.toml" in cand["paths"]
    assert "scripts/experiments/paired_obsolete.bth.toml" in cand["file_hashes"]

    res_apply = archive_sweep_tool(
        apply=True,
        proposal_path=dry["proposal_path"],
        project_root=str(repo),
        catalog_dir=str(cat_dir),
    )
    assert res_apply["ok"] is True

    # Both script and sidecar are stubbed
    py_stub = py_path.read_text(encoding="utf-8")
    sc_stub = sc_path.read_text(encoding="utf-8")
    assert "# bathos:archived" in py_stub
    assert "raise RuntimeError(" in py_stub
    assert "# bathos:archived" in sc_stub
    assert "raise RuntimeError(" not in sc_stub

    # Executing stubbed .py script fails loudly with non-zero exit code
    proc = subprocess.run([sys.executable, str(py_path)], capture_output=True, text=True)
    assert proc.returncode != 0
    assert "RuntimeError: This script was archived by bathos" in proc.stderr

    # parse_stub and restore_archived_item still work cleanly
    parsed = parse_stub(py_path)
    assert parsed is not None
    assert parsed["item_id"] == cand["item_id"]
    restore_archived_item(project_root=repo, item_id=cand["item_id"], catalog_dir=cat_dir)
    assert py_path.read_text(encoding="utf-8") == "print('hello world')\n"
    assert sc_path.read_text(encoding="utf-8") == '[experiment]\nhypothesis = "paired test"\n'


def test_bug2c_backfill_findings_long_claims_and_slug_collisions(tmp_path: Path):
    """Verify ingest_claims_ledger and ingest_praxia_verdicts truncate >255-char slugs and deduplicate collisions."""
    long_prefix = "a-very-long-scientific-claim-statement-that-exceeds-filesystem-limits-" * 5
    claims_ledger = tmp_path / "claims_ledger.toml"
    claims_ledger.write_text(
        f'[[claims]]\nid = "K-18"\nclaim = "{long_prefix} variant A with \\"quotes\\""\n'
        f'numbers = "delta = +1.5"\nevidential_status = "tracked_reproducible"\n\n'
        f'[[claims]]\nid = "K-19"\nclaim = "{long_prefix} variant B"\n'
        f'numbers = "delta = +2.0"\nevidential_status = "stale"\n',
        encoding="utf-8",
    )

    verdicts_dir = tmp_path / "verdicts"
    verdicts_dir.mkdir(parents=True)
    (verdicts_dir / "v_long_1.toml").write_text(
        f'[verdict]\nsprint_id = "261101_long_1"\ntitle = "{long_prefix} verdict 1"\n'
        f'outcome = "confirmed"\nstatus = "settled"\n',
        encoding="utf-8",
    )
    (verdicts_dir / "v_long_2.toml").write_text(
        f'[verdict]\nsprint_id = "261101_long_2"\ntitle = "{long_prefix} verdict 2"\n'
        f'outcome = "refuted"\nstatus = "refuted"\n',
        encoding="utf-8",
    )

    findings_dir = tmp_path / "findings"
    c_list = ingest_claims_ledger(claims_ledger, findings_dir)
    v_list = ingest_praxia_verdicts(verdicts_dir, findings_dir)
    assert len(c_list) == 2
    assert len(v_list) == 2

    written_files = sorted(findings_dir.glob("*.toml"))
    assert len(written_files) == 4
    for p in written_files:
        assert len(p.name) <= 80
        parsed = parse_finding_file(p)
        assert parsed.id
        assert parsed.question


