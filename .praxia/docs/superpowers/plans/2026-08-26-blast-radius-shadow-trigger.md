# Blast-Radius Shadow-Mode Git-Hook Trigger Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement backlog #4555 (Phase 2b) per
`.praxia/docs/specs/260826_blast-radius-shadow-trigger.md`: a `post-commit` git hook,
installed via a wrapped `core.hooksPath` that preserves any pre-existing hooks, that fires
on fix-like commit messages and logs (never applies) what blast-radius assessment would
have flagged.

**Architecture:** Two new pieces. (1) `src/bathos/git_hooks.py` — generic, blast-radius-
agnostic git-hook wrapping mechanics (install/uninstall a managed `core.hooksPath`
directory that chains to whatever was there before). (2) New functions in
`src/bathos/blast_radius.py` — the shadow-trigger-specific logic (keyword filter, running
`assess_blast_radius` in shadow-only mode, writing an `entity_type="shadow_trigger"`
ledger record). The installed hook script itself does only the cheap keyword check inline
and spawns `bth blast-radius shadow-check <sha>` detached — all the real logic lives in
testable Python, not shell.

**Tech Stack:** Python 3.13, subprocess (git + process spawning), DuckDB/PyArrow (existing
ledger), Typer CLI. No new external dependencies.

**Spec:** `.praxia/docs/specs/260826_blast-radius-shadow-trigger.md` — SAC-1 through SAC-8.

---

### Task 1: `git_hooks.py` — install/uninstall a wrapped `core.hooksPath`

**Files:**
- Create: `src/bathos/git_hooks.py`
- Test: `tests/test_git_hooks.py`

**Design:**

```python
def install_managed_hooks(
    repo_root: Path, managed_dir: Path, hook_scripts: dict[str, str]
) -> None:
    """Point core.hooksPath at managed_dir, preserving whatever hooks were
    already active. hook_scripts maps hook name -> script content bathos
    wants to install for that hook (e.g. {"post-commit": "..."}).

    For each hook name in hook_scripts: if a hook of that name already exists
    at the previously-active hooks location, bathos's script chains to it
    (execs the original first, then runs bathos's own logic, `exit 0`
    regardless of the original's exit code -- post-commit can't block
    anything anyway, and a broken pre-existing hook shouldn't break bathos's
    addition or vice versa). For every OTHER hook name present at the
    previously-active location, a symlink is created in managed_dir pointing
    at the original file unchanged (passthrough).

    Records the previous core.hooksPath value (or a sentinel for "was unset")
    in `<managed_dir>/.bathos_state.json` so uninstall_managed_hooks can
    restore it exactly. Idempotent: calling this twice re-derives from the
    ORIGINAL recorded state if `.bathos_state.json` already exists, never
    chains to bathos's own previously-installed wrapper (would double-fire).
    """

def uninstall_managed_hooks(repo_root: Path, managed_dir: Path) -> None:
    """Restore core.hooksPath to its pre-install value (or unset it) and
    remove managed_dir. Raises FileNotFoundError if managed_dir has no
    .bathos_state.json (never installed, or already uninstalled)."""
```

**Step 1: Write the failing tests**

```python
"""Git-hook wrapping tests (backlog #4555). Blast-radius-agnostic -- these
test the generic install/uninstall/chain mechanics only."""

from __future__ import annotations

import subprocess

import pytest

from bathos.git_hooks import install_managed_hooks, uninstall_managed_hooks


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init"], r)
    _git(["config", "user.email", "test@example.com"], r)
    _git(["config", "user.name", "Test"], r)
    return r


def _get_hooks_path(repo):
    result = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"], cwd=repo,
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


class TestInstallFreshRepo:
    def test_sets_core_hooks_path(self, repo, tmp_path):
        managed = tmp_path / "managed"
        install_managed_hooks(repo, managed, {"post-commit": "#!/bin/sh\necho hi\n"})
        assert _get_hooks_path(repo) == str(managed)

    def test_installed_hook_is_executable_and_runs(self, repo, tmp_path):
        managed = tmp_path / "managed"
        marker = tmp_path / "marker.txt"
        install_managed_hooks(
            repo, managed, {"post-commit": f"#!/bin/sh\ntouch {marker}\n"}
        )
        (repo / "f.txt").write_text("x")
        _git(["add", "f.txt"], repo)
        _git(["commit", "-m", "test"], repo)
        assert marker.exists()


class TestPreservesExistingDefaultHooks:
    def test_preexisting_post_commit_is_chained(self, repo, tmp_path):
        default_hooks = repo / ".git" / "hooks"
        original_marker = tmp_path / "original_ran.txt"
        original = default_hooks / "post-commit"
        original.write_text(f"#!/bin/sh\ntouch {original_marker}\n")
        original.chmod(0o755)

        managed = tmp_path / "managed"
        bathos_marker = tmp_path / "bathos_ran.txt"
        install_managed_hooks(
            repo, managed, {"post-commit": f"#!/bin/sh\ntouch {bathos_marker}\n"}
        )
        (repo / "f.txt").write_text("x")
        _git(["add", "f.txt"], repo)
        _git(["commit", "-m", "test"], repo)

        assert original_marker.exists(), "pre-existing post-commit must still fire"
        assert bathos_marker.exists(), "bathos's own logic must also fire"

    def test_other_preexisting_hooks_are_untouched(self, repo, tmp_path):
        default_hooks = repo / ".git" / "hooks"
        pre_commit_marker = tmp_path / "pre_commit_ran.txt"
        pre_commit = default_hooks / "pre-commit"
        pre_commit.write_text(f"#!/bin/sh\ntouch {pre_commit_marker}\n")
        pre_commit.chmod(0o755)

        managed = tmp_path / "managed"
        install_managed_hooks(repo, managed, {"post-commit": "#!/bin/sh\nexit 0\n"})
        (repo / "f.txt").write_text("x")
        _git(["add", "f.txt"], repo)
        _git(["commit", "-m", "test"], repo)

        assert pre_commit_marker.exists(), "unrelated pre-existing hook must still fire"


class TestPreservesExistingCoreHooksPath:
    def test_existing_custom_hooks_path_is_chained(self, repo, tmp_path):
        custom_hooks = tmp_path / "custom_hooks"
        custom_hooks.mkdir()
        original_marker = tmp_path / "custom_ran.txt"
        original = custom_hooks / "post-commit"
        original.write_text(f"#!/bin/sh\ntouch {original_marker}\n")
        original.chmod(0o755)
        _git(["config", "core.hooksPath", str(custom_hooks)], repo)

        managed = tmp_path / "managed"
        bathos_marker = tmp_path / "bathos_ran.txt"
        install_managed_hooks(
            repo, managed, {"post-commit": f"#!/bin/sh\ntouch {bathos_marker}\n"}
        )
        (repo / "f.txt").write_text("x")
        _git(["add", "f.txt"], repo)
        _git(["commit", "-m", "test"], repo)

        assert original_marker.exists()
        assert bathos_marker.exists()
        assert _get_hooks_path(repo) == str(managed)


class TestUninstall:
    def test_restores_unset_hooks_path(self, repo, tmp_path):
        managed = tmp_path / "managed"
        install_managed_hooks(repo, managed, {"post-commit": "#!/bin/sh\nexit 0\n"})
        uninstall_managed_hooks(repo, managed)
        assert _get_hooks_path(repo) is None
        assert not managed.exists()

    def test_restores_previous_custom_hooks_path(self, repo, tmp_path):
        custom_hooks = tmp_path / "custom_hooks"
        custom_hooks.mkdir()
        _git(["config", "core.hooksPath", str(custom_hooks)], repo)

        managed = tmp_path / "managed"
        install_managed_hooks(repo, managed, {"post-commit": "#!/bin/sh\nexit 0\n"})
        uninstall_managed_hooks(repo, managed)

        assert _get_hooks_path(repo) == str(custom_hooks)

    def test_uninstall_without_install_raises(self, repo, tmp_path):
        with pytest.raises(FileNotFoundError):
            uninstall_managed_hooks(repo, tmp_path / "never_installed")


class TestIdempotentInstall:
    def test_installing_twice_does_not_double_chain(self, repo, tmp_path):
        managed = tmp_path / "managed"
        marker = tmp_path / "marker.txt"
        script = f"#!/bin/sh\necho x >> {marker}\n"
        install_managed_hooks(repo, managed, {"post-commit": script})
        install_managed_hooks(repo, managed, {"post-commit": script})

        (repo / "f.txt").write_text("x")
        _git(["add", "f.txt"], repo)
        _git(["commit", "-m", "test"], repo)

        # Exactly one line -- not two (which would mean the second install
        # chained to the first install's already-bathos-owned wrapper).
        assert marker.read_text().count("x") == 1
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_git_hooks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bathos.git_hooks'`

**Step 3: Implement `src/bathos/git_hooks.py`**

Key implementation notes for whoever writes this (bite-sized guidance, not full code —
the exact shape depends on real git behavior discovered while implementing):

- Resolve the previously-active hooks directory: `git config --get core.hooksPath`
  (relative paths are relative to the repo root per git docs); if unset, it's
  `<repo_root>/.git/hooks` (handle worktrees: `git rev-parse --git-path hooks` is the
  robust way to get this, works correctly even inside a `.claude/worktrees/` checkout).
- For each hook name in `hook_scripts`: if a file exists at
  `<previous_hooks_dir>/<name>`, write bathos's script as
  `<managed_dir>/<name>` with a chain line prepended, e.g.
  ```sh
  #!/bin/sh
  "<previous_hooks_dir>/<name>" "$@" || true
  <bathos script body>
  ```
  (the `|| true` matters: a pre-existing hook's own failure must never suppress bathos's
  logic, and `post-commit`'s exit code is ignored by git anyway).
- If no pre-existing hook of that name exists, write bathos's script as-is (no chain
  prefix needed).
- For every OTHER file in `previous_hooks_dir` not in `hook_scripts` (skip `.sample`
  files — git ships those by default and they're never executable), create a symlink in
  `managed_dir` pointing at the original absolute path.
- Write `<managed_dir>/.bathos_state.json` with `{"previous_hooks_path": <str or null>}`
  BEFORE calling `git config core.hooksPath`, so a crash mid-install never leaves git
  pointed at a directory with no recorded rollback state.
- `chmod 0o755` every script written into `managed_dir` (symlinks inherit the target's
  mode, no chmod needed for those).
- Idempotency: if `<managed_dir>/.bathos_state.json` already exists, re-read its
  `previous_hooks_path` and use THAT as the source for chaining/symlinking (never the
  CURRENT `core.hooksPath`, which by now already points at `managed_dir` itself).
- `uninstall_managed_hooks`: read `.bathos_state.json`, run
  `git config --unset core.hooksPath` if `previous_hooks_path` was null, else
  `git config core.hooksPath <value>`; then `shutil.rmtree(managed_dir)`.

**Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_git_hooks.py -v`
Expected: PASS (10 tests)

**Step 5: Commit**

```bash
git add src/bathos/git_hooks.py tests/test_git_hooks.py
git commit -m "feat(git-hooks): add wrapped core.hooksPath install/uninstall (#4555)"
```

---

### Task 2: Shadow-trigger assessment + ledger recording

**Files:**
- Modify: `src/bathos/blast_radius.py` (new: `_FIX_LIKE_KEYWORD_PATTERN`,
  `matches_fix_like_keywords`, `record_shadow_trigger`)
- Test: extend `tests/test_blast_radius_flag_clear.py` or new
  `tests/test_blast_radius_shadow_trigger.py` (prefer new file — different concern)

**Design:**

```python
_FIX_LIKE_KEYWORD_PATTERN = re.compile(
    r"\b(fix|fixes|fixed|bug|bugfix|hotfix|regression|patch)\b", re.IGNORECASE
)


def matches_fix_like_keywords(commit_message: str) -> bool:
    """SAC-5: cheap keyword check. Hardcoded pattern, not configurable yet
    (spec Decision Log #2)."""
    return bool(_FIX_LIKE_KEYWORD_PATTERN.search(commit_message))


def record_shadow_trigger(
    catalog_dir: Path | str, project_root: Path | str, commit: str
) -> BlastRadiusRecord | None:
    """SAC-6/SAC-7: run assess_blast_radius(commit=commit) and log a single
    entity_type="shadow_trigger" record -- NEVER calls flag_blast_radius or
    either propagate_to_* function, so this can never durably affect a real
    run/campaign/claim's state (spec Decision Log #7).

    Returns the appended record, or None if assess_blast_radius raised
    ValueError (e.g. commit has no parent -- the very first commit in a repo)
    -- a shadow trigger failing quietly is acceptable (spec pre-mortem:
    "a detached background process's own errors are invisible to the user at
    commit time by design").
    """
    try:
        report = assess_blast_radius(catalog_dir, project_root, commit=commit)
    except ValueError:
        return None

    all_matches = list(report.affected) + list(report.unverifiable)
    record = BlastRadiusRecord(
        entity_type="shadow_trigger",
        entity_id=commit,
        to_state="shadow_only",
        anchor_kind=report.anchor_kind,
        anchor_value=report.anchor_value,
        matched_files=json.dumps(sorted({f for m in all_matches for f in m.matched_files})),
        match_reason=(
            f"{len(report.affected)} affected, {len(report.unverifiable)} unverifiable "
            f"run(s) would have been flagged: {[m.run_id for m in all_matches]}"
        ),
    )
    return append_ledger_record(record, catalog_dir)
```

**Step 1: Write the failing tests**

```python
"""Blast-radius shadow-trigger tests (SAC-4 through SAC-8, backlog #4555)."""

from __future__ import annotations

import json
import subprocess

import pytest

from bathos.blast_radius import (
    fold_blast_radius_state,
    matches_fix_like_keywords,
    record_shadow_trigger,
)
from bathos.catalog import init_catalog, write_run
from bathos.schema import Run


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _commit_file(repo, relpath, content, message):
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(["add", relpath], repo)
    _git(["commit", "-m", message], repo)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init"], r)
    _git(["config", "user.email", "test@example.com"], r)
    _git(["config", "user.name", "Test"], r)
    return r


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    init_catalog(cat)
    return cat


class TestKeywordFilter:
    @pytest.mark.parametrize("msg", ["fix bug", "Fixes #123", "hotfix: patch", "BUG: crash"])
    def test_matches_fix_like_messages(self, msg):
        assert matches_fix_like_keywords(msg)

    @pytest.mark.parametrize("msg", ["add feature", "refactor module", "update docs"])
    def test_does_not_match_unrelated_messages(self, msg):
        assert not matches_fix_like_keywords(msg)


class TestRecordShadowTrigger:
    def test_records_shadow_only_state(self, repo, catalog_dir):
        pre_fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 1\n", "initial")
        fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 2\n", "fix bug")

        run = Run(
            project_slug="proj", command="scripts/experiments/foo.py",
            argv=["scripts/experiments/foo.py"], git_hash=pre_fix_sha,
            git_branch="main", git_dirty=False,
        )
        write_run(run, catalog_dir)

        record = record_shadow_trigger(catalog_dir, repo, fix_sha)

        assert record is not None
        assert record.entity_type == "shadow_trigger"
        assert record.entity_id == fix_sha
        assert record.to_state == "shadow_only"
        assert run.id in record.match_reason

    def test_never_pollutes_real_run_state(self, repo, catalog_dir):
        pre_fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 1\n", "initial")
        fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 2\n", "fix bug")

        run = Run(
            project_slug="proj", command="scripts/experiments/foo.py",
            argv=["scripts/experiments/foo.py"], git_hash=pre_fix_sha,
            git_branch="main", git_dirty=False,
        )
        write_run(run, catalog_dir)

        record_shadow_trigger(catalog_dir, repo, fix_sha)

        # SAC-7: the shadow trigger must never be visible via the real-entity reads.
        assert fold_blast_radius_state(catalog_dir, "run", run.id) == "clean"

    def test_first_commit_with_no_parent_fails_quietly(self, repo, catalog_dir):
        first_sha = _commit_file(repo, "foo.py", "a = 1\n", "initial")
        record = record_shadow_trigger(catalog_dir, repo, first_sha)
        assert record is None
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_blast_radius_shadow_trigger.py -v`
Expected: FAIL — `ImportError: cannot import name 'matches_fix_like_keywords'`

**Step 3: Implement** (add `import re` to blast_radius.py's imports; add the three items
above near the end of the file, after `propagate_to_claims`).

**Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_blast_radius_shadow_trigger.py -v`
Expected: PASS (6 tests)

**Step 5: Commit**

```bash
git add src/bathos/blast_radius.py tests/test_blast_radius_shadow_trigger.py
git commit -m "feat(blast-radius): add shadow-trigger keyword filter + recording (SAC-4-8, #4555)"
```

---

### Task 3: CLI surface (install-hook / uninstall-hook / shadow-check / shadow-log)

**Files:**
- Modify: `src/bathos/cli.py` (4 new `blast_app` commands)
- Test: extend `tests/test_blast_radius_cli.py`

**Design:**

```python
_HOOK_SCRIPT_TEMPLATE = """#!/bin/sh
# Installed by bathos (backlog #4555) -- do not edit directly, re-run
# `bth blast-radius install-hook` to regenerate.
sha="$(git rev-parse HEAD)"
msg="$(git log -1 --pretty=%B HEAD)"
case "$msg" in
    *[Ff]ix*|*[Bb]ug*|*[Rr]egression*|*[Hh]otfix*|*[Pp]atch*)
        nohup bth blast-radius shadow-check "$sha" >/dev/null 2>&1 &
        ;;
esac
"""


@blast_app.command("install-hook")
def blast_radius_install_hook_cmd():
    """Install the post-commit shadow-trigger hook (SAC-1/2, backlog #4555)."""
    from bathos.git_hooks import install_managed_hooks
    from bathos.workspace import resolve_workspace

    ws_root = resolve_workspace().fs_root
    managed = ws_root / ".bth" / "hooks"
    install_managed_hooks(ws_root, managed, {"post-commit": _HOOK_SCRIPT_TEMPLATE})
    typer.echo(f"Installed shadow-trigger hook at {managed}")


@blast_app.command("uninstall-hook")
def blast_radius_uninstall_hook_cmd():
    """Uninstall the shadow-trigger hook, restoring prior core.hooksPath (SAC-3)."""
    from bathos.git_hooks import uninstall_managed_hooks
    from bathos.workspace import resolve_workspace

    ws_root = resolve_workspace().fs_root
    managed = ws_root / ".bth" / "hooks"
    try:
        uninstall_managed_hooks(ws_root, managed)
    except FileNotFoundError:
        typer.echo("Error: hook not installed", err=True)
        raise typer.Exit(1)
    typer.echo("Uninstalled shadow-trigger hook")


@blast_app.command("shadow-check")
def blast_radius_shadow_check_cmd(
    commit: str = typer.Argument(..., help="Commit SHA to shadow-assess"),
):
    """Run a shadow-only assessment for one commit (called by the installed
    hook; safe to call directly for testing/debugging)."""
    from bathos.blast_radius import record_shadow_trigger
    from bathos.workspace import resolve_workspace

    ws_root = resolve_workspace().fs_root
    record = record_shadow_trigger(_catalog_dir(), ws_root, commit)
    if record is None:
        typer.echo(f"No shadow trigger recorded for {commit} (e.g. no parent commit)")
        return
    typer.echo(f"Shadow-recorded {commit}: {record.match_reason}")


@query_app.command("shadow-log")
def query_shadow_log(
    limit: int = typer.Option(20, "--limit", help="Max records to show"),
):
    """List recent shadow-trigger firings for calibration review (SAC-8)."""
    import duckdb

    cat_dir = _catalog_dir()
    db_path = cat_dir / "bathos.db"
    if not db_path.exists():
        typer.echo("[]")
        return
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT entity_id, match_reason, amended_at FROM blast_radius_ledger "
            "WHERE entity_type = 'shadow_trigger' ORDER BY amended_at DESC LIMIT ?",
            [limit],
        ).fetchall()
    finally:
        con.close()
    for commit, reason, amended_at in rows:
        typer.echo(f"{amended_at}  {commit[:9]}  {reason}")
```

**Step 1: Write failing tests** for each of the 4 commands (install writes `.bth/hooks/`
and sets `core.hooksPath`; uninstall restores; `shadow-check` on a fix-labeled commit with
a matching run produces the expected record via `query blast-status` -- wait,
`blast-status` only reads run/campaign/claim entity types, so assert via a direct
`fold_blast_radius_state(catalog_dir, "shadow_trigger", commit)` import in the test
instead; `shadow-log` lists what `shadow-check` just wrote).

**Step 2: Run to verify failure.**

**Step 3: Implement** (as designed above; add `blast_app` import of `git_hooks` module
functions where needed).

**Step 4: Run to verify it passes.**

**Step 5: Commit**

```bash
git add src/bathos/cli.py tests/test_blast_radius_cli.py
git commit -m "feat(blast-radius): add install-hook/uninstall-hook/shadow-check/shadow-log CLI (#4555)"
```

---

### Task 4: End-to-end integration test (real git commit fires the real hook)

**Files:**
- New: `tests/test_blast_radius_hook_e2e.py`

**Design:** Install the hook for real via the CLI command, make a fix-labeled commit,
poll (with a short timeout, e.g. up to 5s) for the background `shadow-check` process to
finish and write its record, then assert on the ledger. This is the one test allowed to
be slightly timing-sensitive (polling, not a fixed sleep) since it's proving the actual
`nohup ... &` backgrounding wiring works end-to-end, which nothing else in this plan
exercises.

```python
"""End-to-end test: install the hook for real, commit, verify it fires (#4555)."""

from __future__ import annotations

import subprocess
import time

import pytest
from typer.testing import CliRunner

from bathos.blast_radius import fold_blast_radius_state
from bathos.catalog import init_catalog, write_run
from bathos.cli import app
from bathos.schema import Run

runner = CliRunner()


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init"], r)
    _git(["config", "user.email", "test@example.com"], r)
    _git(["config", "user.name", "Test"], r)
    (r / "foo.py").write_text("a = 1\n")
    _git(["add", "foo.py"], r)
    _git(["commit", "-m", "initial"], r)
    return r


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    init_catalog(cat)
    return cat


def test_real_commit_fires_hook_and_records_shadow_trigger(repo, catalog_dir, monkeypatch):
    pre_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    run = Run(
        project_slug="p", command="foo.py", argv=["foo.py"],
        git_hash=pre_sha, git_branch="main", git_dirty=False,
    )
    write_run(run, catalog_dir)

    monkeypatch.chdir(repo)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
    result = runner.invoke(app, ["blast-radius", "install-hook"])
    assert result.exit_code == 0, result.output

    (repo / "foo.py").write_text("a = 2\n")
    _git(["add", "foo.py"], repo)
    _git(["commit", "-m", "fix the bug"], repo)
    fix_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    deadline = time.monotonic() + 5.0
    status = "clean"
    while time.monotonic() < deadline:
        status = fold_blast_radius_state(catalog_dir, "shadow_trigger", fix_sha)
        if status != "clean":
            break
        time.sleep(0.2)

    assert status == "shadow_only", (
        "shadow trigger did not fire within 5s of a real git commit -- check the "
        "installed hook script and background-spawn wiring"
    )
```

**Step 1-2:** Run, expect it to fail for a real reason initially (hook not installed
correctly, `bth` not on PATH inside the hook's subprocess environment, etc.) — this is
expected exploratory debugging territory, not a scripted red/green step, since it's the
first time the real end-to-end wiring runs. Budget real debugging time here; the shell
script's exact invocation of `bth` (full path resolution — a hook's subprocess PATH may
differ from the test's) is the most likely thing to need adjustment.

**Step 3: Commit once green**

```bash
git add tests/test_blast_radius_hook_e2e.py
git commit -m "test(blast-radius): add real end-to-end git-hook firing test (#4555)"
```

---

### Task 5: CHANGELOG + full regression pass + push

Same discipline as Phase 1/2a: add a CHANGELOG bullet, run the full scoped regression
(new files plus anything touching `cli.py`/`blast_radius.py`), `ruff check`, commit, push
to the SAME branch/PR #54 per explicit user direction to fold this into that PR.
