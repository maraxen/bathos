"""Tests for bathos.artifact_archive: stub-in-place archive/restore of script+output
bundles, backed by the archived_items ledger (bathos.archived_items, trust_ledger pattern).
"""

import subprocess
from pathlib import Path

import pytest


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


def _make_scripted_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Returns (repo_root, script_path, sidecar_path), committed."""
    repo = tmp_path / "proj"
    repo.mkdir()
    _init_repo(repo)

    scripts_dir = repo / "scripts" / "experiments"
    scripts_dir.mkdir(parents=True)
    script = scripts_dir / "run_thing.py"
    script.write_text("print('hello')\n")
    sidecar = scripts_dir / "run_thing.bth.toml"
    sidecar.write_text("[experiment]\nhypothesis = 'h'\n")

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo, script, sidecar


def test_archive_stubs_tracked_files_and_commits(tmp_path):
    from bathos.artifact_archive import archive_experiment_bundle

    repo, script, sidecar = _make_scripted_repo(tmp_path)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    item = archive_experiment_bundle(
        project_root=repo,
        script_path=script,
        catalog_dir=catalog_dir,
        project_slug="testproj",
        verdict="superseded",
        reason="wrong default",
        superseded_by="run_xyz",
    )

    assert item.stub_commit_sha
    stub_text = script.read_text()
    assert "hello" not in stub_text
    assert item.id in stub_text
    assert "superseded" in stub_text
    assert "run_xyz" in stub_text

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert len(log.strip().splitlines()) == 2  # init + stub commit


def test_archive_refuses_dirty_tree(tmp_path):
    from bathos.artifact_archive import DirtyTreeError, archive_experiment_bundle

    repo, script, _sidecar = _make_scripted_repo(tmp_path)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    script.write_text("print('modified, not committed')\n")

    with pytest.raises(DirtyTreeError):
        archive_experiment_bundle(
            project_root=repo,
            script_path=script,
            catalog_dir=catalog_dir,
            project_slug="testproj",
            verdict="v",
            reason="r",
        )

    # nothing was written to the ledger or committed
    assert not (catalog_dir / "bathos.db").exists()


def test_archive_dry_run_does_not_mutate(tmp_path):
    from bathos.artifact_archive import archive_experiment_bundle

    repo, script, sidecar = _make_scripted_repo(tmp_path)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    original_script_text = script.read_text()

    item = archive_experiment_bundle(
        project_root=repo,
        script_path=script,
        catalog_dir=catalog_dir,
        project_slug="testproj",
        verdict="v",
        reason="r",
        dry_run=True,
    )

    assert script.read_text() == original_script_text
    assert item.stub_commit_sha == ""


def test_restore_recovers_exact_bytes(tmp_path):
    from bathos.artifact_archive import archive_experiment_bundle, restore_archived_item

    repo, script, sidecar = _make_scripted_repo(tmp_path)
    original_script_text = script.read_text()
    original_sidecar_text = sidecar.read_text()
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    item = archive_experiment_bundle(
        project_root=repo,
        script_path=script,
        catalog_dir=catalog_dir,
        project_slug="testproj",
        verdict="v",
        reason="r",
    )

    result = restore_archived_item(project_root=repo, item_id=item.id, catalog_dir=catalog_dir)

    assert script.read_text() == original_script_text
    assert sidecar.read_text() == original_sidecar_text
    assert result.restore_commit_sha


def test_restore_twice_raises(tmp_path):
    from bathos.artifact_archive import (
        ArchiveError,
        archive_experiment_bundle,
        restore_archived_item,
    )

    repo, script, _sidecar = _make_scripted_repo(tmp_path)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    item = archive_experiment_bundle(
        project_root=repo,
        script_path=script,
        catalog_dir=catalog_dir,
        project_slug="testproj",
        verdict="v",
        reason="r",
    )
    restore_archived_item(project_root=repo, item_id=item.id, catalog_dir=catalog_dir)

    with pytest.raises(ArchiveError):
        restore_archived_item(project_root=repo, item_id=item.id, catalog_dir=catalog_dir)


def test_archive_writes_generated_index(tmp_path):
    from bathos.artifact_archive import archive_experiment_bundle

    repo, script, _sidecar = _make_scripted_repo(tmp_path)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    item = archive_experiment_bundle(
        project_root=repo,
        script_path=script,
        catalog_dir=catalog_dir,
        project_slug="testproj",
        verdict="superseded",
        reason="wrong default",
        superseded_by="run_xyz",
    )

    index_path = repo / ".bth" / "ARCHIVE_INDEX.md"
    assert index_path.exists()
    content = index_path.read_text()
    assert item.id in content
    assert "ARCHIVED" in content
    assert "run_xyz" in content
    assert f"bth restore {item.id}" in content


def test_restore_updates_generated_index_to_restored(tmp_path):
    from bathos.artifact_archive import archive_experiment_bundle, restore_archived_item

    repo, script, _sidecar = _make_scripted_repo(tmp_path)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    item = archive_experiment_bundle(
        project_root=repo,
        script_path=script,
        catalog_dir=catalog_dir,
        project_slug="testproj",
        verdict="v",
        reason="r",
    )
    restore_archived_item(project_root=repo, item_id=item.id, catalog_dir=catalog_dir)

    content = (repo / ".bth" / "ARCHIVE_INDEX.md").read_text()
    assert "RESTORED" in content
    assert "ARCHIVED" not in content  # latest-wins per id, folded to one line


def test_archive_attaches_git_notes(tmp_path):
    from bathos.artifact_archive import archive_experiment_bundle

    repo, script, _sidecar = _make_scripted_repo(tmp_path)
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

    notes = subprocess.run(
        ["git", "notes", "--ref=bathos-archive", "show", item.stub_commit_sha],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert notes.returncode == 0
    assert item.id in notes.stdout
    assert "wrong default" in notes.stdout


def test_untracked_output_path_packed_into_bundle(tmp_path):
    """Untracked outputs get packed into a git bundle rather than a raw tarball, and the
    bundle round-trips via a plain `git clone` of it."""
    from bathos.artifact_archive import _build_untracked_bundle

    repo, _script, _sidecar = _make_scripted_repo(tmp_path)
    outputs_dir = repo / "outputs"
    outputs_dir.mkdir()
    untracked = outputs_dir / "result.txt"
    untracked.write_text("some result data\n")

    bundle_target = tmp_path / "test.bundle"
    _build_untracked_bundle([untracked], repo, bundle_target)
    assert bundle_target.exists()

    clone_dir = tmp_path / "clone_check"
    r = subprocess.run(
        ["git", "clone", "-q", str(bundle_target), str(clone_dir)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0
    assert (clone_dir / "outputs" / "result.txt").read_text() == "some result data\n"


def test_archived_items_ledger_survives_compact(tmp_path):
    """The cool-tier fragment written by archive_experiment_bundle is re-derivable into
    the warm archived_items table via bth compact -- mirrors trust_ledger's durability
    guarantee (survives even force_rebuild, since compact re-ingests from fragments)."""
    import duckdb

    from bathos.artifact_archive import archive_experiment_bundle
    from bathos.compact import compact

    repo, script, _sidecar = _make_scripted_repo(tmp_path)
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

    compact(catalog_dir)

    con = duckdb.connect(str(catalog_dir / "bathos.db"), read_only=True)
    try:
        rows = con.execute(
            "SELECT id, event, verdict FROM archived_items WHERE id = ?", [item.id]
        ).fetchall()
    finally:
        con.close()
    assert rows == [(item.id, "archived", "superseded")]


def test_parse_stub_roundtrips_metadata(tmp_path):
    from bathos.artifact_archive import archive_experiment_bundle, parse_stub

    repo, script, _sidecar = _make_scripted_repo(tmp_path)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    item = archive_experiment_bundle(
        project_root=repo,
        script_path=script,
        catalog_dir=catalog_dir,
        project_slug="testproj",
        verdict="superseded",
        reason="wrong default",
        superseded_by="run_xyz",
    )

    parsed = parse_stub(script)
    assert parsed is not None
    assert parsed["item_id"] == item.id
    assert parsed["pre_archive_sha"] == item.pre_archive_sha
    assert parsed["superseded_by"] == "run_xyz"


def test_parse_stub_returns_none_for_non_stub_file(tmp_path):
    from bathos.artifact_archive import parse_stub

    plain = tmp_path / "plain.py"
    plain.write_text("print('just a normal script')\n")
    assert parse_stub(plain) is None
