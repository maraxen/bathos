import json
import subprocess
from pathlib import Path

from bathos.git_pin import (
    uncommitted_diff_for_run,
    MANIFEST_RELPATH,
    ignored_provenance_paths,
    pin_run,
    snapshot_worktree,
)


def _init_repo(path: Path) -> str:
    """Initialise a repo with one commit; return the commit sha."""
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True
    )
    (path / "tracked.txt").write_text("original")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, text=True, capture_output=True, check=True
    ).stdout.strip()


def _rev(path: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=path, text=True, capture_output=True
    ).stdout.strip()


def test_clean_tree_pins_head(tmp_path: Path):
    head = _init_repo(tmp_path)
    result = pin_run("run-abc", head, "main", dirty=False, cwd=tmp_path)

    assert result.run_ref == "refs/bathos/runs/run-abc"
    assert _rev(tmp_path, result.run_ref) == head
    assert result.wip_commit == ""


def test_dirty_tree_pins_a_snapshot_not_head(tmp_path: Path):
    """The whole point: on a dirty tree the ref must describe what RAN, not what was committed."""
    head = _init_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("modified after commit")

    result = pin_run("run-dirty", head, "main", dirty=True, cwd=tmp_path)

    assert result.wip_commit
    assert result.wip_ref == "refs/bathos/wip/run-dirty"
    # The run ref points at the snapshot, which is NOT the recorded HEAD.
    assert _rev(tmp_path, result.run_ref) == result.wip_commit
    assert result.wip_commit != head

    # And the snapshot really carries the modified content.
    blob = subprocess.run(
        ["git", "show", f"{result.wip_commit}:tracked.txt"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
    ).stdout
    assert blob == "modified after commit"


def test_snapshot_does_not_disturb_the_users_work(tmp_path: Path):
    """Provenance capture must not perturb the run it is capturing.

    It is not literally invisible -- it deliberately writes the tracked ref manifest -- so the
    invariant is narrower and more useful: nothing the USER has is touched. Same tracked-file
    contents, same index, same HEAD, and the manifest is the ONLY thing that appears.
    """
    head = _init_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("modified")
    (tmp_path / "untracked.txt").write_text("new file")

    def status_without_manifest() -> list[str]:
        out = subprocess.run(
            ["git", "status", "--porcelain"], cwd=tmp_path, text=True, capture_output=True
        ).stdout
        return [line for line in out.splitlines() if ".bth/" not in line]

    before = status_without_manifest()
    staged_before = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=tmp_path, text=True, capture_output=True
    ).stdout

    pin_run("run-x", head, "main", dirty=True, cwd=tmp_path)

    assert status_without_manifest() == before
    assert (
        subprocess.run(
            ["git", "diff", "--cached", "--name-only"], cwd=tmp_path, text=True, capture_output=True
        ).stdout
        == staged_before
    )
    assert (tmp_path / "tracked.txt").read_text() == "modified"
    assert (tmp_path / "untracked.txt").read_text() == "new file"
    assert _rev(tmp_path, "HEAD") == head

    # The manifest is the only thing pinning added to the worktree.
    added = {
        line[3:]
        for line in subprocess.run(
            ["git", "status", "--porcelain"], cwd=tmp_path, text=True, capture_output=True
        ).stdout.splitlines()
    } - {line[3:] for line in before}
    assert added == {".bth/"}


def test_snapshot_excludes_the_manifest_it_is_about_to_write(tmp_path: Path):
    """The snapshot must describe the tree that RAN, not one containing its own provenance record.

    Ordering matters: appending the manifest first would make every snapshot include the previous
    line of its own bookkeeping, which is self-referential noise in the recorded tree.
    """
    head = _init_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("modified")

    result = pin_run("run-order", head, "main", dirty=True, cwd=tmp_path)

    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", result.wip_commit],
        cwd=tmp_path,
        text=True,
        capture_output=True,
    ).stdout
    assert ".bth/refs/manifest.jsonl" not in listing


def test_snapshot_captures_untracked_but_respects_gitignore(tmp_path: Path):
    """Untracked scripts are often what ran; ignored bulk must stay out."""
    head = _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored_bulk/\n")
    (tmp_path / "ignored_bulk").mkdir()
    (tmp_path / "ignored_bulk" / "huge.bin").write_text("x" * 1000)
    (tmp_path / "new_script.py").write_text("print('ran')")

    wip = snapshot_worktree("run-y", tmp_path)
    assert wip

    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", wip], cwd=tmp_path, text=True, capture_output=True
    ).stdout
    assert "new_script.py" in listing
    assert "ignored_bulk/huge.bin" not in listing


def test_pinned_commit_survives_branch_deletion(tmp_path: Path):
    """The dominant real-world loss mode: work done on a short-lived worktree branch."""
    _init_repo(tmp_path)
    subprocess.run(
        ["git", "checkout", "-b", "throwaway"], cwd=tmp_path, check=True, capture_output=True
    )
    (tmp_path / "tracked.txt").write_text("work on a branch")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "wip"], cwd=tmp_path, check=True, capture_output=True)
    branch_head = _rev(tmp_path, "HEAD")

    pin_run("run-branch", branch_head, "throwaway", dirty=False, cwd=tmp_path)

    subprocess.run(
        ["git", "checkout", "-"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "branch", "-D", "throwaway"], cwd=tmp_path, check=True, capture_output=True
    )
    # Aggressive prune: without the ref this object would now be unreachable and collectable.
    subprocess.run(
        ["git", "gc", "--prune=now", "--quiet"], cwd=tmp_path, check=True, capture_output=True
    )

    assert _rev(tmp_path, "refs/bathos/runs/run-branch") == branch_head
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", f"{branch_head}^{{commit}}"], cwd=tmp_path, capture_output=True
        ).returncode
        == 0
    )


def test_manifest_records_head_and_pinned_sha(tmp_path: Path):
    head = _init_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("dirty")

    result = pin_run("run-m", head, "main", dirty=True, cwd=tmp_path)

    manifest = tmp_path / MANIFEST_RELPATH
    assert manifest.exists()
    entry = json.loads(manifest.read_text().strip())
    assert entry["run_id"] == "run-m"
    assert entry["head_sha"] == head
    assert entry["pinned_sha"] == result.wip_commit
    assert entry["dirty"] is True
    assert entry["branch"] == "main"
    assert result.manifest_path


def test_manifest_appends_rather_than_overwrites(tmp_path: Path):
    head = _init_repo(tmp_path)
    pin_run("run-1", head, "main", dirty=False, cwd=tmp_path)
    pin_run("run-2", head, "main", dirty=False, cwd=tmp_path)

    lines = (tmp_path / MANIFEST_RELPATH).read_text().strip().splitlines()
    assert [json.loads(x)["run_id"] for x in lines] == ["run-1", "run-2"]


def test_detects_ignored_provenance_paths(tmp_path: Path):
    """A bare `.bth/` ignore rule silently discards claims and the ref manifest."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".bth/\n")

    ignored = ignored_provenance_paths(tmp_path)
    assert ".bth/claims" in ignored
    assert ".bth/refs" in ignored


def test_narrowed_ignore_rule_is_accepted(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".bth/*\n!.bth/claims/\n!.bth/refs/\n")

    assert ignored_provenance_paths(tmp_path) == ()


def test_uncommitted_diff_is_recoverable_after_the_worktree_moves_on(tmp_path: Path):
    """The point of parenting the snapshot on HEAD: recover WHAT WAS DIRTY, long afterwards."""
    head = _init_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("the version that actually ran")
    (tmp_path / "extra.py").write_text("print('also ran')")

    pin_run("run-diff", head, "main", dirty=True, cwd=tmp_path)

    # The working tree moves on entirely -- changes reverted, new commits made.
    (tmp_path / "tracked.txt").write_text("original")
    (tmp_path / "extra.py").unlink()
    (tmp_path / "later.txt").write_text("unrelated later work")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "later"], cwd=tmp_path, check=True, capture_output=True)

    diff = uncommitted_diff_for_run("run-diff", tmp_path)
    assert diff is not None
    assert "the version that actually ran" in diff
    assert "print('also ran')" in diff
    assert "unrelated later work" not in diff  # strictly the run-time delta, not later history

    names = uncommitted_diff_for_run("run-diff", tmp_path, name_only=True)
    assert names is not None
    assert set(names.split()) == {"tracked.txt", "extra.py"}


def test_clean_run_reports_empty_diff_not_unknown(tmp_path: Path):
    """A clean run and an uncapturable one must not look alike."""
    head = _init_repo(tmp_path)
    pin_run("run-clean", head, "main", dirty=False, cwd=tmp_path)
    assert uncommitted_diff_for_run("run-clean", tmp_path) == ""


def test_unknown_run_reports_none(tmp_path: Path):
    _init_repo(tmp_path)
    assert uncommitted_diff_for_run("never-pinned", tmp_path) is None


def test_diff_falls_back_to_manifest_when_ref_is_absent(tmp_path: Path):
    """A clone may carry the objects and the tracked manifest but not the refs."""
    head = _init_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("ran like this")
    pin_run("run-fallback", head, "main", dirty=True, cwd=tmp_path)

    subprocess.run(
        ["git", "update-ref", "-d", "refs/bathos/wip/run-fallback"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    diff = uncommitted_diff_for_run("run-fallback", tmp_path)
    assert diff is not None
    assert "ran like this" in diff


def test_outside_a_repo_degrades_quietly(tmp_path: Path):
    result = pin_run("run-none", "deadbeef", "main", dirty=False, cwd=tmp_path)
    assert result.run_ref == ""
    assert result.unpinned_reason == "not a git repository"


def test_unknown_hash_is_not_pinned(tmp_path: Path):
    _init_repo(tmp_path)
    result = pin_run("run-unknown", "unknown", "unknown", dirty=False, cwd=tmp_path)
    assert result.run_ref == ""
    assert "no resolvable HEAD" in result.unpinned_reason
