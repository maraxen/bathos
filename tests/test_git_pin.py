import json
import subprocess
from pathlib import Path

from bathos.git_pin import (
    EXPORT_DIRNAME,
    MANIFEST_RELPATH,
    ignored_provenance_paths,
    import_bundles,
    manifest_entry,
    pin_run,
    ref_resolves,
    snapshot_worktree,
    uncommitted_diff_for_run,
    update_ref,
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

    # The snapshot must be PARENTED ON HEAD. Everything downstream depends on it: `git diff
    # head_sha pinned_sha` is only the run-time delta if this holds, and storage is only delta-only
    # because the parent shares its blobs. Asserted here rather than dropping the unused binding.
    parents = subprocess.run(
        ["git", "rev-list", "--parents", "-n", "1", wip],
        cwd=tmp_path,
        text=True,
        capture_output=True,
    ).stdout.split()
    assert parents[1:] == [head]


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


# --- Regressions for defects found by adversarial audit (260818) -------------------------------


def test_failed_ref_creation_is_recorded_not_claimed(tmp_path: Path):
    """The module's own failure mode: reporting a run as pinned when the ref never took.

    Original defect -- the manifest was appended unconditionally and never carried the failure, so a
    run whose object was already collectable read as durable. Simulated by making the ref directory
    unwritable, which is what a full disk or lock contention produces.
    """
    import os
    import stat

    head = _init_repo(tmp_path)
    refs_dir = tmp_path / ".git" / "refs"
    original_mode = refs_dir.stat().st_mode
    os.chmod(refs_dir, stat.S_IREAD | stat.S_IEXEC)
    try:
        result = pin_run("run-permfail", head, "main", dirty=False, cwd=tmp_path)
    finally:
        os.chmod(refs_dir, original_mode)

    assert result.run_ref_ok is False
    assert result.unpinned_reason
    assert result.complete is False

    entry = json.loads((tmp_path / MANIFEST_RELPATH).read_text().strip())
    assert entry["run_ref_ok"] is False
    assert entry["complete"] is False
    assert entry["unpinned_reason"]


def test_update_ref_verifies_rather_than_trusting_exit_code(tmp_path: Path):
    head = _init_repo(tmp_path)
    assert update_ref("refs/bathos/runs/verified", head, tmp_path) is True
    assert ref_resolves("refs/bathos/runs/verified", tmp_path) is True
    assert ref_resolves("refs/bathos/runs/never-made", tmp_path) is False


def test_dirty_flag_but_unchanged_tree_makes_no_snapshot(tmp_path: Path):
    """Coverage gap found by mutation: nothing exercised dirty=True over a tree matching HEAD."""
    head = _init_repo(tmp_path)

    result = pin_run("run-notreally", head, "main", dirty=True, cwd=tmp_path)

    assert result.wip_commit == ""
    assert result.snapshot_mode == "none"
    assert _rev(tmp_path, result.run_ref) == head
    assert result.complete is True


def test_diff_works_from_ref_alone_when_manifest_is_absent(tmp_path: Path):
    """Coverage gap found by mutation: the ref-present/manifest-missing direction was untested."""
    head = _init_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("what actually ran")
    pin_run("run-nomanifest", head, "main", dirty=True, cwd=tmp_path)

    (tmp_path / MANIFEST_RELPATH).unlink()

    diff = uncommitted_diff_for_run("run-nomanifest", tmp_path)
    assert diff is not None
    assert "what actually ran" in diff


def test_oversized_worktree_degrades_to_metadata_instead_of_bloating(tmp_path: Path):
    """An unignored output dir must not be committed into a permanently-reachable snapshot."""
    head = _init_repo(tmp_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "big.bin").write_bytes(b"x" * 200_000)

    result = pin_run(
        "run-big", head, "main", dirty=True, cwd=tmp_path, max_snapshot_bytes=50_000
    )

    assert result.snapshot_mode == "metadata_only"
    assert result.wip_commit == ""
    assert result.skipped_bytes >= 200_000
    assert any("big.bin" in p for p in result.skipped_paths)
    assert result.complete is False  # must not pass for a durable record

    entry = json.loads((tmp_path / MANIFEST_RELPATH).read_text().strip())
    assert entry["snapshot_mode"] == "metadata_only"
    assert entry["complete"] is False


def test_ignored_declared_path_is_reported(tmp_path: Path):
    """The inverse hazard: a load-bearing file the repo ignores is omitted from the snapshot."""
    head = _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("secret_config.yaml\n")
    (tmp_path / "secret_config.yaml").write_text("k: v")

    result = pin_run(
        "run-ignored",
        head,
        "main",
        dirty=True,
        cwd=tmp_path,
        declared_paths=["secret_config.yaml", "tracked.txt"],
    )

    assert result.ignored_declared_paths == ("secret_config.yaml",)
    assert result.complete is False


def test_manifest_is_found_from_a_sibling_worktree(tmp_path: Path):
    """Refs are shared across linked worktrees; the manifest must be found from either side.

    Worktree-per-task is a common workflow, so a run pinned inside `git worktree add` must not be
    invisible from the main checkout while its ref sits there resolvable.
    """
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    head = _init_repo(main_repo)

    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", str(linked), "-b", "side"],
        cwd=main_repo,
        check=True,
        capture_output=True,
    )

    pin_run("run-wt", head, "side", dirty=False, cwd=linked)

    # Written in the linked worktree, and must be readable from the main one.
    from bathos.git_pin import manifest_entry

    assert manifest_entry("run-wt", linked) is not None
    entry = manifest_entry("run-wt", main_repo)
    assert entry is not None
    assert entry["run_id"] == "run-wt"


# --- Cross-clone transport ----------------------------------------------------------------------


def _clone_of(src: Path, dest: Path) -> Path:
    subprocess.run(
        ["git", "clone", "-q", str(src), str(dest)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=dest, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=dest, check=True, capture_output=True
    )
    return dest


def test_dirty_run_exports_a_bundle_clean_run_does_not(tmp_path: Path):
    """Only a snapshot needs transporting; a clean run pinned a commit both ends already have."""
    head = _init_repo(tmp_path)

    clean = pin_run("run-clean", head, "main", dirty=False, cwd=tmp_path)
    assert clean.bundle_path == ""

    (tmp_path / "tracked.txt").write_text("ran like this")
    dirty = pin_run(
        "run-dirty", head, "main", dirty=True, cwd=tmp_path,
        export_dir=tmp_path / EXPORT_DIRNAME,
    )
    assert dirty.bundle_path
    assert Path(dirty.bundle_path).exists()
    assert Path(dirty.bundle_path).with_suffix(".json").exists()


def test_bundle_round_trips_the_snapshot_into_a_separate_clone(tmp_path: Path):
    """The real scenario: a run executes on a cluster, and its snapshot must reach the machine
    where results are read -- with no network path between the two object stores."""
    origin = tmp_path / "origin"
    origin.mkdir()
    head = _init_repo(origin)

    # The "cluster" checkout, and a dirty run on it.
    cluster = _clone_of(origin, tmp_path / "cluster")
    (cluster / "tracked.txt").write_text("what actually ran on the cluster")
    (cluster / "cluster_only.py").write_text("print('remote')")
    result = pin_run(
        "run-remote", head, "main", dirty=True, cwd=cluster,
        export_dir=cluster / EXPORT_DIRNAME,
    )
    assert result.wip_commit

    # The "laptop" checkout has never seen that snapshot.
    laptop = _clone_of(origin, tmp_path / "laptop")
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", f"{result.wip_commit}^{{commit}}"],
            cwd=laptop,
            capture_output=True,
        ).returncode
        != 0
    )

    # Transport is a plain file copy -- whatever already moves results back.
    dest = laptop / EXPORT_DIRNAME
    dest.mkdir(parents=True)
    for suffix in (".bundle", ".json"):
        src = Path(result.bundle_path).with_suffix(suffix)
        (dest / src.name).write_bytes(src.read_bytes())

    report = import_bundles(laptop)
    assert report.imported == ("run-remote",)
    assert not report.unusable

    # Objects, refs and the record all arrived.
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", f"{result.wip_commit}^{{commit}}"],
            cwd=laptop,
            capture_output=True,
        ).returncode
        == 0
    )
    assert _rev(laptop, "refs/bathos/runs/run-remote") == result.wip_commit
    entry = manifest_entry("run-remote", laptop)
    assert entry is not None
    assert entry["imported_from_bundle"] is True

    # And the whole point: the run-time diff is readable on the laptop.
    diff = uncommitted_diff_for_run("run-remote", laptop)
    assert diff is not None
    assert "what actually ran on the cluster" in diff
    assert "print('remote')" in diff


def test_import_refuses_a_bundle_whose_base_is_missing(tmp_path: Path):
    """A dangling ref would read as durable provenance while being unreadable -- the exact failure
    this module exists to prevent. Refuse, and say why."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _init_repo(origin)

    cluster = _clone_of(origin, tmp_path / "cluster")
    # A commit that exists ONLY on the cluster, so the delta's base is unknown elsewhere.
    (cluster / "tracked.txt").write_text("unpushed base")
    subprocess.run(["git", "add", "-A"], cwd=cluster, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "unpushed"], cwd=cluster, check=True, capture_output=True
    )
    unpushed_head = _rev(cluster, "HEAD")
    (cluster / "tracked.txt").write_text("dirty on top of an unpushed base")
    result = pin_run(
        "run-orphan", unpushed_head, "main", dirty=True, cwd=cluster,
        export_dir=cluster / EXPORT_DIRNAME,
    )
    assert result.bundle_path

    laptop = _clone_of(origin, tmp_path / "laptop")
    dest = laptop / EXPORT_DIRNAME
    dest.mkdir(parents=True)
    for suffix in (".bundle", ".json"):
        src = Path(result.bundle_path).with_suffix(suffix)
        (dest / src.name).write_bytes(src.read_bytes())

    report = import_bundles(laptop)
    assert report.imported == ()
    assert len(report.unusable) == 1
    run_id, reason = report.unusable[0]
    assert run_id == "run-orphan"
    assert "prerequisites" in reason
    # No ref was created for something this clone cannot read.
    assert ref_resolves("refs/bathos/runs/run-orphan", laptop) is False


def test_import_is_idempotent(tmp_path: Path):
    origin = tmp_path / "origin"
    origin.mkdir()
    head = _init_repo(origin)
    cluster = _clone_of(origin, tmp_path / "cluster")
    (cluster / "tracked.txt").write_text("x")
    result = pin_run(
        "run-twice", head, "main", dirty=True, cwd=cluster,
        export_dir=cluster / EXPORT_DIRNAME,
    )

    laptop = _clone_of(origin, tmp_path / "laptop")
    dest = laptop / EXPORT_DIRNAME
    dest.mkdir(parents=True)
    for suffix in (".bundle", ".json"):
        src = Path(result.bundle_path).with_suffix(suffix)
        (dest / src.name).write_bytes(src.read_bytes())

    first = import_bundles(laptop)
    second = import_bundles(laptop)
    assert first.imported == ("run-twice",)
    assert second.imported == ()
    assert second.already_present == ("run-twice",)

    lines = (laptop / MANIFEST_RELPATH).read_text().strip().splitlines()
    assert len([x for x in lines if json.loads(x)["run_id"] == "run-twice"]) == 1


def test_bundle_is_a_delta_not_the_whole_history(tmp_path: Path):
    """Sized as the changed files, not the repository -- otherwise every dirty cluster run would
    ship a full clone back through the results channel."""
    head = _init_repo(tmp_path)
    # Bulk that is committed, and therefore already on both ends.
    (tmp_path / "bulk.bin").write_text("y" * 400_000)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "bulk"], cwd=tmp_path, check=True, capture_output=True)
    head = _rev(tmp_path, "HEAD")

    (tmp_path / "tracked.txt").write_text("small change")
    result = pin_run(
        "run-delta", head, "main", dirty=True, cwd=tmp_path,
        export_dir=tmp_path / EXPORT_DIRNAME,
    )

    assert result.bundle_path
    assert Path(result.bundle_path).stat().st_size < 50_000


def test_import_outside_a_repo_or_with_no_dir_is_a_noop(tmp_path: Path):
    assert import_bundles(tmp_path).imported == ()
    _init_repo(tmp_path)
    assert import_bundles(tmp_path).imported == ()
