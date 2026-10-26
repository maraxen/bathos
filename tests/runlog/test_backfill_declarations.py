"""`bth backfill-declarations`: recover claim fields lost before #3717.

Runs recorded before #3717 carry NULL `claim_discriminates` even when their
sidecar declared it, so the Union Gate reports the clause unmapped. The
backfill emits `run.declaration_backfilled`, but only from sidecar bytes that
hash to the run's own recorded `sidecar_sha256`; it never invents a
declaration from a sidecar edited after the run.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from bathos.backfill import BackfillError, backfill_declarations, find_candidates
from bathos.campaigns import add_run_to_campaign, create_campaign
from bathos.claim import run_union_gate
from bathos.mcp import backfill_declarations_tool
from bathos.runlog.emit import emit_event, run_event_data
from bathos.runlog.fold_runs import fold_run
from bathos.runlog.index import connect_read
from bathos.runlog.ingest import run_ingest
from bathos.schema import Run

from .test_reap_folded_status import _folded_run, enable_log_mode, setup_project

SIDECAR = """\
[experiment]
hypothesis = "probe"
claim_discriminates = ["H_a", "H_b"]

[outcomes.pass]
condition = "x > 0"
decision = "go"

[outcomes.fail]
condition = "x <= 0"
decision = "stop"

[result_schema]
x = "float"
"""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _write_sidecar(repo: Path, text: str = SIDECAR, commit: bool = False) -> Path:
    path = repo / "scripts" / "experiments" / "probe.bth.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if commit:
        subprocess.run(["git", "add", str(path)], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "sidecar"], cwd=repo, check=True)
    return path


def _start(repo: Path, run_id: str, sidecar_path: str, sha: str, **extra) -> None:
    """A pre-#3717 run: the sidecar is recorded by path and hash, the claim field is not."""
    run = Run(
        id=run_id,
        project_slug="repo",
        command="python probe.py",
        argv=["python", "probe.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        status="completed",
        timestamp=datetime.now(UTC),
        sidecar_sha256=sha,
        sidecar_path=sidecar_path,
        **extra,
    )
    emit_event(
        kind="run.started", entity=[run_id], data=run_event_data(run), cwd=repo, hard_fail=True
    )


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    repo = setup_project(tmp_path)
    (repo / ".gitignore").write_text("/.bth/log/\n")
    monkeypatch.chdir(repo)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    return repo, catalog_dir


def test_backfill_from_hash_matched_sidecar_folds_into_runs(project) -> None:
    repo, catalog_dir = project
    path = _write_sidecar(repo)
    _start(repo, "run-1", str(path), _sha(SIDECAR))
    run_ingest(catalog_dir)
    assert _folded_run(catalog_dir, "run-1")["claim_discriminates"] is None

    dry = backfill_declarations(catalog_dir, roots=[repo])
    assert [(c.run_id, c.status, c.resolved_from) for c in dry] == [
        ("run-1", "backfillable", "path")
    ]
    run_ingest(catalog_dir)
    assert _folded_run(catalog_dir, "run-1")["claim_discriminates"] is None  # dry run wrote nothing

    backfill_declarations(catalog_dir, apply=True, roots=[repo])
    run_ingest(catalog_dir)
    assert json.loads(_folded_run(catalog_dir, "run-1")["claim_discriminates"]) == ["H_a", "H_b"]
    # The event lands in the run's own project log.
    logged = [
        json.loads(line)
        for f in (repo / ".bth" / "log").glob("*.jsonl")
        for line in f.read_text().splitlines()
    ]
    assert [
        e["data"]["sidecar_sha256"] for e in logged if e["kind"] == "run.declaration_backfilled"
    ] == [_sha(SIDECAR)]
    # Re-running finds nothing left to do (claim_isolates stays NULL: none declared).
    assert [c.status for c in find_candidates(catalog_dir, roots=[repo])] == ["no_declaration"]


def test_sidecar_edited_after_the_run_is_refused(project) -> None:
    """Negative control: a declaration added after the run must not be backfilled."""
    repo, catalog_dir = project
    original = SIDECAR.replace('claim_discriminates = ["H_a", "H_b"]\n', "")
    path = _write_sidecar(repo)  # the file now declares H_a/H_b ...
    _start(repo, "run-1", str(path), _sha(original))  # ... but the run hashed the original
    run_ingest(catalog_dir)

    result = backfill_declarations(catalog_dir, apply=True, roots=[repo])

    assert [(c.run_id, c.status) for c in result] == [("run-1", "unresolved")]
    run_ingest(catalog_dir)
    assert _folded_run(catalog_dir, "run-1")["claim_discriminates"] is None


def test_original_recovered_from_git_history(project) -> None:
    repo, catalog_dir = project
    path = _write_sidecar(repo, commit=True)
    _start(repo, "run-1", str(path), _sha(SIDECAR))
    path.write_text(SIDECAR.replace("H_b", "H_c"))  # later edit, uncommitted
    run_ingest(catalog_dir)

    (cand,) = backfill_declarations(catalog_dir, apply=True, roots=[repo])

    assert cand.status == "backfillable"
    assert cand.resolved_from == "git"
    assert cand.claim_discriminates == ["H_a", "H_b"]  # the run's version, not the edit
    run_ingest(catalog_dir)
    assert json.loads(_folded_run(catalog_dir, "run-1")["claim_discriminates"]) == ["H_a", "H_b"]


def test_remote_cluster_path_resolves_under_registered_root(project) -> None:
    """Debt #2225: a cluster run records the remote absolute sidecar_path."""
    repo, catalog_dir = project
    _write_sidecar(repo)
    remote = "/orcd/scratch/user/repo/scripts/experiments/probe.bth.toml"
    _start(repo, "run-1", remote, _sha(SIDECAR))
    run_ingest(catalog_dir)

    (cand,) = find_candidates(catalog_dir, roots=[repo])

    assert cand.status == "backfillable"
    assert cand.resolved_from == "registered_root"
    assert cand.resolved_locator == str(repo / "scripts" / "experiments" / "probe.bth.toml")


def test_sidecar_without_a_declaration_is_reported_not_written(project) -> None:
    repo, catalog_dir = project
    text = SIDECAR.replace('claim_discriminates = ["H_a", "H_b"]\n', "")
    path = _write_sidecar(repo, text)
    _start(repo, "run-1", str(path), _sha(text))
    run_ingest(catalog_dir)

    result = backfill_declarations(catalog_dir, apply=True, roots=[repo])

    assert [c.status for c in result] == ["no_declaration"]


def test_fold_never_overrides_a_recorded_value() -> None:
    events = [
        {
            "kind": "run.started",
            "entity": ["r"],
            "ts": "2026-01-01T00:00:00Z",
            "eid": "1",
            "origin": "live",
            "data": {"status": "running", "claim_discriminates": '["H_live"]'},
        },
        {
            "kind": "run.declaration_backfilled",
            "entity": ["r"],
            "ts": "2026-02-01T00:00:00Z",
            "eid": "2",
            "origin": "live",
            "data": {"claim_discriminates": '["H_backfill"]', "claim_isolates": '["C_x"]'},
        },
    ]
    row = fold_run(events)
    assert row["claim_discriminates"] == '["H_live"]'
    assert row["claim_isolates"] == '["C_x"]'  # the NULL field is still filled


def test_union_gate_covers_the_clause_after_backfill(project) -> None:
    repo, catalog_dir = project
    path = _write_sidecar(repo)
    db = connect_read(catalog_dir)
    try:
        campaign_id = create_campaign(
            db, "c", "repo", "confirmation", catalog_dir=catalog_dir, cwd=repo
        ).id
    finally:
        db.close()
    _start(repo, "run-1", str(path), _sha(SIDECAR))  # after the campaign, as confirmation requires
    run_ingest(catalog_dir)
    db = connect_read(catalog_dir)
    try:
        add_run_to_campaign(db, campaign_id, "run-1", catalog_dir=catalog_dir, cwd=repo)
    finally:
        db.close()
    run_ingest(catalog_dir)
    claim = SimpleNamespace(union_gate_clauses=[{"id": "C1", "hypothesis_ids": ["H_a", "H_b"]}])

    def gate() -> tuple[str, list[str]]:
        db = connect_read(catalog_dir)
        try:
            return run_union_gate(db, campaign_id, claim, workspace_root=repo)
        finally:
            db.close()

    assert gate() == ("confounded", ["C1"])
    backfill_declarations(catalog_dir, apply=True, roots=[repo])
    run_ingest(catalog_dir)
    assert gate() == ("covered", [])


def test_refused_outside_log_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = setup_project(tmp_path)
    monkeypatch.chdir(repo)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    with pytest.raises(BackfillError, match="log mode"):
        backfill_declarations(catalog_dir, roots=[repo])
    result = backfill_declarations_tool(catalog_dir=str(catalog_dir))
    assert result["error_code"] == "invalid_param"


def test_tool_reports_counts(project) -> None:
    repo, catalog_dir = project
    path = _write_sidecar(repo)
    _start(repo, "run-1", str(path), _sha(SIDECAR))
    _start(repo, "run-2", "/nowhere/x.bth.toml", "0" * 64)
    run_ingest(catalog_dir)

    result = backfill_declarations_tool(catalog_dir=str(catalog_dir), show="all")

    assert result["counts"] == {"backfillable": 1, "unresolved": 1}
    assert result["backfilled"] == 0
    assert {c["run_id"] for c in result["candidates"]} == {"run-1", "run-2"}


def test_repair_plans_no_warm_rebuild_in_log_mode(tmp_path: Path) -> None:
    """Debt #2819 (7): bathos.db is frozen after cut-over; compact refuses
    force_rebuild there, so repair must not plan a rebuild it cannot apply."""
    from bathos.repair import _actions_from_warm_verify
    from bathos.verify import VerifyResult

    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)
    result = VerifyResult(
        tier="warm",
        ok=False,
        errors=["Database integrity check failed: boom"],
        warnings=[],
        stats={"db_exists": True},
    )

    actions, warnings = _actions_from_warm_verify(result, catalog_dir)

    assert actions == []
    assert any("frozen in log mode" in w for w in warnings)
