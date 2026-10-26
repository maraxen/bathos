"""Cross-run cleanliness: whatever run N leaves must not dirty run N+1 (spec 261026 section 4, T6).

The live dirty check is `git status --porcelain`, which counts untracked files, and run N's git state is captured
before run N writes anything. So the invariant is CROSS-run: run 2's `git_dirty` and the tree after a run are what
these tests read. Each case is a real `run_script` call in a real repo with committed `.bth.toml` and sidecar
fixtures, a tmp catalog and no real ~/.bth.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from bathos.catalog import init_catalog, read_runs
from bathos.lock_admin import lock_untrack
from bathos.runner import run_script

_SIDECAR = (
    "[experiment]\nhypothesis = 'h'\n[outcomes.pass]\ncondition = 'x == 1'\ndecision = 'good'\n"
    "reasoning = 'r'\n[outcomes.fallback]\ncondition = '1==1'\ndecision = 'o'\nreasoning = 'c'\n"
    "is_residual = true\n[result_schema]\nx = 'float'\n"
)
_QUIET = "print('hi')\n"
_WRITES_OUTPUTS = (
    "import os, pathlib\n"
    "pathlib.Path(os.environ['BTH_OUTPUT_DIR'], 'result.txt').write_text('data')\n"
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("BTH_LOCK_LOCATION", "BTH_LOCK_DIR", "BTH_IGNORE_OUTPUTS", "BTH_LOG_MODE"):
        monkeypatch.delenv(var, raising=False)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _porcelain(repo: Path) -> str:
    return _git(repo, "status", "--porcelain")


def _make_repo(
    tmp_path: Path, *, script_body: str = _QUIET, bth_toml_extra: str = "", extra_files=None
) -> tuple[Path, Path, Path]:
    """A committed project: `.bth.toml`, sidecar, script. Returns (repo, script, catalog)."""
    repo = tmp_path / "repo"
    exp = repo / "scripts" / "experiments"
    exp.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "T")
    (repo / ".bth.toml").write_text(
        f'[project]\nslug = "p"\nroot = "{repo}"\nid = "00000000-0000-4000-8000-000000000001"\n'
        + bth_toml_extra
    )
    (exp / "run_x.py").write_text(script_body)
    (exp / "run_x.bth.toml").write_text(_SIDECAR)
    for rel, text in (extra_files or {}).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-qm", "fixtures")
    catalog = tmp_path / "catalog"
    init_catalog(catalog)
    return repo, exp / "run_x.py", catalog


def _execute(repo: Path, script: Path, catalog: Path) -> bool:
    """Run once through the real runner; return that run's recorded git_dirty."""
    before = {r.id for r in read_runs(catalog)}
    rc = run_script(
        argv=[sys.executable, str(script)],
        project_slug="p",
        catalog_dir=catalog,
        output_paths=[],
        tags=[],
        cwd=repo,
    )
    assert rc == 0
    (new,) = [
        r for r in read_runs(catalog) if r.id not in before
    ]  # read_runs order is not chronological
    return bool(new.git_dirty)


def test_committed_manifest_dirties_run_two_until_untracked(tmp_path: Path):
    """DOCUMENTED LIMITATION, NOT A BUG TO FIX HERE (spec 261026 P5, T6 i).

    A repo that committed `.bth/refs/manifest.jsonl` (pre-#1943) sees it modified by every run, and an ignore rule
    cannot un-track a tracked path. So run 2 records `git_dirty = true`. The way out is `bth lock untrack --apply`
    plus a commit, after which run 3 is clean. This asserts the limitation so nobody "fixes" it by quietly ignoring
    tracked files (which would hide real changes) and so the untrack remedy stays proven.
    """
    repo, script, catalog = _make_repo(
        tmp_path, extra_files={".bth/refs/manifest.jsonl": '{"legacy": true}\n'}
    )

    assert _execute(repo, script, catalog) is False  # run 1: clean at capture time
    assert ".bth/refs/manifest.jsonl" in _porcelain(
        repo
    )  # ... but run 1 modified the tracked manifest
    assert _execute(repo, script, catalog) is True  # run 2: the documented limitation

    lock_untrack(repo, apply=True)
    _git(repo, "add", "-A")  # run 2 left nothing else; commit the staged deletion
    _git(repo, "commit", "-qm", "untrack manifest")
    assert _porcelain(repo) == ""
    assert _execute(repo, script, catalog) is False  # run 3, after untrack and commit
    assert _porcelain(repo) == ""


def test_tracked_lock_with_default_git_mode_writes_no_new_lock_file(tmp_path: Path):
    """T6 (ii): a committed lock (G19 evidence) stays; a git-mode run adds no lock file beside it."""
    tracked_lock = "scripts/experiments/run_x.old.bth.lock.toml"
    repo, script, catalog = _make_repo(
        tmp_path, extra_files={tracked_lock: "# committed evidence\n[manifest]\n"}
    )

    assert _execute(repo, script, catalog) is False
    assert _execute(repo, script, catalog) is False

    locks = sorted(p.name for p in repo.rglob("*.bth.lock.toml"))
    assert locks == ["run_x.old.bth.lock.toml"]
    assert _porcelain(repo) == ""


def test_run_log_does_not_dirty_a_repo_without_a_log_ignore(tmp_path: Path, monkeypatch):
    """T6 (iii): `.bth/log/` is ignored only by `bth init`; an older repo lacks it. With the runlog flag ON (so
    something is actually written there) the tree must still be clean after a run."""
    monkeypatch.setenv(
        "BTH_LOG_MODE", "1"
    )  # honoured under pytest with a non-default BTH_CATALOG_DIR
    repo, script, catalog = _make_repo(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    assert not (repo / ".gitignore").exists()

    # Log mode keeps run records in `.bth/log/`, not the cool catalog, so read nothing back: the assertion is
    # about the tree.
    rc = run_script(
        argv=[sys.executable, str(script)],
        project_slug="p",
        catalog_dir=catalog,
        output_paths=[],
        tags=[],
        cwd=repo,
    )
    assert rc == 0

    assert (repo / ".bth" / "log").exists(), "vacuous: the run wrote nothing under .bth/log/"
    assert _porcelain(repo) == ""


def test_outputs_ignored_by_setting_keeps_run_two_clean(tmp_path: Path):
    """T6 (iv): a script writing under outputs/ is clean for run 2 once `ignore_outputs = true`."""
    repo, script, catalog = _make_repo(
        tmp_path,
        script_body=_WRITES_OUTPUTS,
        bth_toml_extra="[provenance]\nignore_outputs = true\n",
    )

    assert _execute(repo, script, catalog) is False
    assert (next((repo / "outputs").iterdir()) / "result.txt").exists()
    assert _porcelain(repo) == ""
    assert _execute(repo, script, catalog) is False


def test_outputs_not_ignored_by_default_dirty_run_two(tmp_path: Path):
    """Control for (iv): without the opt-in an existing project's outputs/ files are untracked, so run 2 is dirty.
    Documents why `ignore_outputs` exists (only NEW projects get `outputs/` in .gitignore)."""
    repo, script, catalog = _make_repo(tmp_path, script_body=_WRITES_OUTPUTS)

    assert _execute(repo, script, catalog) is False
    assert _execute(repo, script, catalog) is True


def test_fresh_repo_script_writing_nothing_keeps_run_two_clean(tmp_path: Path):
    """T6 (v): the whole point -- bathos's own bookkeeping leaves a fresh repo clean between runs."""
    repo, script, catalog = _make_repo(tmp_path)

    assert _execute(repo, script, catalog) is False
    assert _porcelain(repo) == ""
    assert _execute(repo, script, catalog) is False
    assert _porcelain(repo) == ""
