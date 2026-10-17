"""Git provenance of a run follows the checkout the executed script lives in (backlog #5609).

Before the fix, `_run_script_impl` captured git state from the run's `cwd` (the primary
checkout when the caller's cwd was the primary, or a stale `Path.cwd()` default), so a
script executed out of a linked worktree recorded the primary's HEAD in both the run
record and the `<script>.<run_id>.bth.lock.toml` lock file.
"""

import subprocess
import sys
from pathlib import Path

from bathos.catalog import init_catalog, read_runs
from bathos.init import init_project
from bathos.runner import run_script

_SIDECAR = (
    "[experiment]\n"
    'hypothesis = "h"\n'
    "[outcomes.pass]\n"
    'condition = "1 = 1"\n'
    'decision = "ok"\n'
    'reasoning = "r"\n'
    "[outcomes.fail]\n"
    'condition = "1 = 0"\n'
    'decision = "no"\n'
    'reasoning = "r"\n'
    "is_residual = true\n"
    "[result_schema]\n"
    'x = "int"\n'
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit_file(repo: Path, name: str, text: str, msg: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _primary_with_linked_worktree(tmp_path: Path) -> tuple[Path, Path, Path, str, str]:
    """Primary checkout (bth-initialised, on `main`) plus a linked worktree on `feat`
    at a DIFFERENT commit. The script lives only in the worktree."""
    primary = tmp_path / "primary"
    wt = tmp_path / "wt"
    catalog = tmp_path / "catalog"
    primary.mkdir()

    _git(primary, "init", "-q", "-b", "main")
    _git(primary, "config", "user.email", "t@t.com")
    _git(primary, "config", "user.name", "T")
    init_project(primary, slug="p", catalog_dir=catalog)
    sha_primary = _commit_file(primary, "README.md", "a\n", "A")

    _git(primary, "worktree", "add", "-q", "-b", "feat", str(wt))
    (wt / "scripts").mkdir(exist_ok=True)  # init_project already creates it (committed)
    (wt / "scripts" / "x.bth.toml").write_text(_SIDECAR)
    sha_wt = _commit_file(wt, "scripts/x.py", "print('hi')\n", "B")
    assert sha_wt != sha_primary
    return primary, wt, catalog, sha_primary, sha_wt


def _lock_git_sha(script_dir: Path) -> str:
    locks = list(script_dir.glob("*.bth.lock.toml"))
    assert len(locks) == 1, locks
    for line in locks[0].read_text().splitlines():
        if line.startswith("git_sha"):
            return line.split('"')[1]
    raise AssertionError(f"no git_sha in {locks[0]}")


def test_linked_worktree_script_records_worktree_head(tmp_path: Path):
    """Script in a linked worktree, run from the PRIMARY checkout's cwd: the run and its lock
    file must record the worktree's HEAD, not the primary's."""
    primary, wt, catalog, sha_primary, sha_wt = _primary_with_linked_worktree(tmp_path)
    init_catalog(catalog)

    rc = run_script(
        argv=[sys.executable, str(wt / "scripts" / "x.py")],
        project_slug="p",
        catalog_dir=catalog,
        output_paths=[],
        tags=[],
        cwd=primary,
    )

    assert rc == 0
    run = read_runs(catalog)[-1]
    assert run.git_hash == sha_wt
    assert run.git_hash != sha_primary
    assert run.git_branch == "feat"
    assert _lock_git_sha(wt / "scripts") == sha_wt


def test_linked_worktree_script_records_worktree_head_when_run_from_worktree(tmp_path: Path):
    """Same fixture, run from the worktree's own cwd: unchanged, still the worktree HEAD."""
    primary, wt, catalog, sha_primary, sha_wt = _primary_with_linked_worktree(tmp_path)
    init_catalog(catalog)

    rc = run_script(
        argv=[sys.executable, str(wt / "scripts" / "x.py")],
        project_slug="p",
        catalog_dir=catalog,
        output_paths=[],
        tags=[],
        cwd=wt,
    )

    assert rc == 0
    run = read_runs(catalog)[-1]
    assert run.git_hash == sha_wt
    assert run.git_branch == "feat"
    assert _lock_git_sha(wt / "scripts") == sha_wt


def test_single_checkout_script_in_primary_records_primary_head(tmp_path: Path):
    """Regression guard: one checkout, script inside it. Behaviour is unchanged: the
    primary's HEAD is recorded in the run and in the lock file."""
    repo = tmp_path / "repo"
    catalog = tmp_path / "catalog"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "T")
    init_project(repo, slug="p", catalog_dir=catalog)
    (repo / "scripts").mkdir(exist_ok=True)  # init_project already creates it (committed)
    (repo / "scripts" / "x.bth.toml").write_text(_SIDECAR)
    sha_head = _commit_file(repo, "scripts/x.py", "print('hi')\n", "A")
    init_catalog(catalog)

    rc = run_script(
        argv=[sys.executable, str(repo / "scripts" / "x.py")],
        project_slug="p",
        catalog_dir=catalog,
        output_paths=[],
        tags=[],
        cwd=repo,
    )

    assert rc == 0
    run = read_runs(catalog)[-1]
    assert run.git_hash == sha_head
    assert run.git_branch == "main"
    assert _lock_git_sha(repo / "scripts") == sha_head


def test_script_outside_any_repo_falls_back_to_cwd_git_state(tmp_path: Path):
    """A script that is not inside any git work tree keeps the pre-existing behaviour:
    the git state of the run's cwd."""
    from bathos.git import capture_run_git_state

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "T")
    sha_head = _commit_file(repo, "README.md", "a\n", "A")

    loose = tmp_path / "loose"
    loose.mkdir()
    script = loose / "y.py"
    script.write_text("print('hi')\n")

    state = capture_run_git_state(script, repo)
    assert state.hash == sha_head
    assert state.branch == "main"

    state_no_script = capture_run_git_state(None, repo)
    assert state_no_script.hash == sha_head
