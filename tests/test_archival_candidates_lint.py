"""Tests for bathos.linter.check_archival_candidates -- lint PROPOSES stale-but-unarchived
scripts as candidates; it never archives anything itself (bathos.artifact_archive does)."""

import subprocess
from pathlib import Path


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


def _make_stale_sidecar(scripts_dir: Path, stem: str) -> tuple[Path, Path]:
    script = scripts_dir / f"{stem}.py"
    script.write_text("print('hello')\n")
    sidecar = scripts_dir / f"{stem}.bth.toml"
    sidecar.write_text(
        "[experiment]\nhypothesis = 'h'\n[status]\nstale = true\nstale_reason = 'bad default'\n"
    )
    return script, sidecar


def test_stale_script_flagged_when_no_catalog(tmp_path):
    """With no warm catalog at all, every stale sidecar is a candidate."""
    from bathos.linter import check_archival_candidates

    scripts_dir = tmp_path / "scripts" / "experiments"
    scripts_dir.mkdir(parents=True)
    _make_stale_sidecar(scripts_dir, "run_thing")

    catalog_dir = tmp_path / "catalog"  # does not exist
    issues = check_archival_candidates(tmp_path, catalog_dir)
    flagged = [i for i in issues if i.issue == "unarchived_stale_script"]
    assert len(flagged) == 1


def test_non_stale_script_not_flagged(tmp_path):
    from bathos.linter import check_archival_candidates

    scripts_dir = tmp_path / "scripts" / "experiments"
    scripts_dir.mkdir(parents=True)
    script = scripts_dir / "run_thing.py"
    script.write_text("print('hello')\n")
    sidecar = scripts_dir / "run_thing.bth.toml"
    sidecar.write_text("[experiment]\nhypothesis = 'h'\n")

    catalog_dir = tmp_path / "catalog"
    issues = check_archival_candidates(tmp_path, catalog_dir)
    assert [i for i in issues if i.issue == "unarchived_stale_script"] == []


def test_stale_script_not_flagged_once_archived(tmp_path):
    from bathos.artifact_archive import archive_experiment_bundle
    from bathos.linter import check_archival_candidates

    repo = tmp_path / "proj"
    repo.mkdir()
    _init_repo(repo)
    scripts_dir = repo / "scripts" / "experiments"
    scripts_dir.mkdir(parents=True)
    script, _sidecar = _make_stale_sidecar(scripts_dir, "run_thing")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    # before archiving: flagged
    issues_before = check_archival_candidates(repo, catalog_dir)
    assert len(issues_before) == 1

    archive_experiment_bundle(
        project_root=repo,
        script_path=script,
        catalog_dir=catalog_dir,
        project_slug="testproj",
        verdict="superseded",
        reason="wrong default",
    )

    # after archiving: not flagged (sidecar path is now a stub with no [status] table --
    # parse_sidecar will fail on it, which check_archival_candidates skips silently)
    issues_after = check_archival_candidates(repo, catalog_dir)
    assert [i for i in issues_after if i.issue == "unarchived_stale_script"] == []


def test_stale_script_reflagged_after_restore(tmp_path):
    """A restored-then-still-stale sidecar is a fresh candidate again, not permanently
    exempt -- the "already archived" check must honor the latest event per item, not
    just "was ever archived"."""
    from bathos.artifact_archive import archive_experiment_bundle, restore_archived_item
    from bathos.linter import check_archival_candidates

    repo = tmp_path / "proj"
    repo.mkdir()
    _init_repo(repo)
    scripts_dir = repo / "scripts" / "experiments"
    scripts_dir.mkdir(parents=True)
    script, _sidecar = _make_stale_sidecar(scripts_dir, "run_thing")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    item = archive_experiment_bundle(
        project_root=repo,
        script_path=script,
        catalog_dir=catalog_dir,
        project_slug="testproj",
        verdict="superseded",
        reason="wrong default",
    )
    restore_archived_item(project_root=repo, item_id=item.id, catalog_dir=catalog_dir)

    # restored -> the original stale sidecar is back -> flagged again
    issues = check_archival_candidates(repo, catalog_dir)
    assert len(issues) == 1
