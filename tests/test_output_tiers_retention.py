"""Tests for retention / citation-time cold-archive guard (#3495)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bathos.output_tiers.adopt import hash_outputs
from bathos.output_tiers.cold import cold_outputs, where_outputs
from bathos.output_tiers.reference import OutputTierError
from bathos.output_tiers.retention import (
    COLD_COPY_UNVERIFIED,
    UNARCHIVED_GITIGNORED,
    UNARCHIVED_GITIGNORED_OUTPUTS,
    UNPINNED_GITIGNORED,
    assess_paths_retention,
    check_citation_retention,
    guard_citation_retention,
)
from tests._fake_myxcel import FakeMyxcel
from tests.test_output_tiers_adopt import RID, World


def _git_init_with_ignore(root: Path, ignore_lines: str = "/outputs/\n") -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / ".gitignore").write_text(ignore_lines, encoding="utf-8")


def test_assess_paths_retention_flags_unpinned_and_unarchived_gitignored(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init_with_ignore(repo, "/outputs/\n")
    (repo / "outputs").mkdir()
    (repo / "outputs" / "metrics.json").write_text("{}")
    (repo / "tracked.json").write_text("{}")

    # Gitignored, unpinned, no cold copy -> flags both UNPINNED_GITIGNORED and UNARCHIVED_GITIGNORED
    rep = assess_paths_retention(
        ["outputs/metrics.json", "tracked.json"],
        reference_paths=[],
        worktree_root=repo,
        has_verified_cold_copy=False,
        run_id="run-1",
        decision_relevant=True,
    )
    assert rep.ok is False
    assert rep.gitignored_paths == ("outputs/metrics.json",)
    assert rep.unpinned_paths == ("outputs/metrics.json",)
    assert rep.unarchived_paths == ("outputs/metrics.json",)
    assert UNPINNED_GITIGNORED in rep.flag_codes
    assert UNARCHIVED_GITIGNORED in rep.flag_codes
    assert len(rep.warnings) == 2

    # Reference pin added, still no cold copy -> UNARCHIVED_GITIGNORED only
    rep_pinned = assess_paths_retention(
        ["outputs/metrics.json"],
        reference_paths=["outputs/metrics.json"],
        worktree_root=repo,
        has_verified_cold_copy=False,
        run_id="run-1",
        decision_relevant=True,
    )
    assert rep_pinned.ok is False
    assert rep_pinned.unpinned_paths == ()
    assert rep_pinned.flag_codes == (UNARCHIVED_GITIGNORED,)

    # Verified cold copy + reference pin -> ok!
    rep_cold = assess_paths_retention(
        ["outputs/metrics.json"],
        reference_paths=["outputs/metrics.json"],
        worktree_root=repo,
        has_verified_cold_copy=True,
        run_id="run-1",
        decision_relevant=True,
    )
    assert rep_cold.ok is True
    assert rep_cold.flag_codes == ()
    assert rep_cold.warnings == ()

    # Tracked-only outputs -> ok even without cold copy
    rep_tracked = assess_paths_retention(
        ["tracked.json"],
        reference_paths=[],
        worktree_root=repo,
        has_verified_cold_copy=False,
        run_id="run-1",
        decision_relevant=True,
    )
    assert rep_tracked.ok is True
    assert rep_tracked.gitignored_paths == ()


def test_assess_paths_retention_unverified_cold_copy_flagged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init_with_ignore(repo, "/outputs/\n")
    (repo / "outputs").mkdir()
    (repo / "outputs" / "res.json").write_text("{}")

    rep = assess_paths_retention(
        ["outputs/res.json"],
        reference_paths=["outputs/res.json"],
        worktree_root=repo,
        has_verified_cold_copy=False,
        has_unverified_cold_copy=True,
        run_id="run-2",
        decision_relevant=True,
    )
    assert rep.ok is False
    assert rep.flag_codes == (UNARCHIVED_GITIGNORED, COLD_COPY_UNVERIFIED)


def test_check_and_guard_citation_retention_e2e_with_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path)
    _git_init_with_ignore(w.root, "/out/\n")
    monkeypatch.setenv("BTH_COLD_PROFILE", "coldprof")
    cfg = tmp_path / "myxcel-cfg"
    cfg.mkdir()
    (cfg / "coldprof.toml").write_text('host = "coldhost"\ncold_root = "/cold"\n')
    monkeypatch.setattr("bathos.output_tiers.config.myxcel_config_dir", lambda: cfg)
    fake = FakeMyxcel(tmp_path / "remote")

    w.write("out/result.json", b'{"loss": 0.1}')
    w.started(RID, declared=True)
    w.run_end([w.entry("out/result.json", phase="run_end")], run_id=RID)
    w.finished(RID)
    w.ingest()

    # Before cold archive: guard warns and strict=True refuses
    rep = check_citation_retention(w.catalog, RID[:8])
    assert rep.ok is False
    assert rep.decision_relevant is True
    assert "outcome:pass" in rep.decision_reasons
    assert rep.gitignored_paths == ("out/result.json",)
    assert UNARCHIVED_GITIGNORED in rep.flag_codes

    with pytest.raises(OutputTierError) as ei:
        guard_citation_retention(w.catalog, RID[:8], strict=True)
    assert ei.value.error_code == UNARCHIVED_GITIGNORED_OUTPUTS

    # where_outputs and hash_outputs surface the retention report + warnings
    where_before = where_outputs(w.catalog, RID[:8])
    assert where_before["retention"]["ok"] is False
    assert where_before["warnings"]

    hashed = hash_outputs(w.catalog, RID[:8])
    assert hashed["retention"]["ok"] is False
    assert hashed["warnings"]

    # After bth cold: verified cold copy satisfies the retention guard
    cold_outputs(w.catalog, RID[:8], runner=fake, config_dir=cfg)
    w.ingest()

    rep_after = guard_citation_retention(w.catalog, RID[:8], strict=True)
    assert rep_after.ok is True
    assert rep_after.has_verified_cold_copy is True
    where_after = where_outputs(w.catalog, RID[:8])
    assert where_after["retention"]["ok"] is True
    assert "warnings" not in where_after


def test_non_decision_relevant_run_does_not_flag(tmp_path: Path) -> None:
    from tests.test_output_tiers_adopt import T2

    w = World(tmp_path)
    _git_init_with_ignore(w.root, "/out/\n")
    w.write("out/scratch.json", b"{}")
    w.started(RID, declared=True)
    w.run_end([w.entry("out/scratch.json", phase="run_end")], run_id=RID)
    w.add(
        "run.finished",
        {
            "id": RID,
            "status": "completed",
            "exit_code": 0,
            "duration_s": 1.0,
            "output_paths": [],
            "outcome": "fail",
            "outcome_error_reason": "",
            "outcome_is_residual": False,
            "adversarial_check_status": "",
            "timestamp": T2,
        },
        T2,
        "e-fin-fail",
        RID,
    )
    w.ingest()

    # Inferred decision_relevant=False (outcome='fail', no campaign, no validation stage)
    rep = check_citation_retention(w.catalog, RID[:8], decision_relevant=None)
    assert rep.decision_relevant is False
    assert rep.ok is True
    assert rep.warnings == ()

    # Explicit decision_relevant=True overrides and flags it
    rep_explicit = check_citation_retention(w.catalog, RID[:8], decision_relevant=True)
    assert rep_explicit.decision_relevant is True
    assert rep_explicit.ok is False
