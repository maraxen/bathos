"""Isolation fixtures for the runlog test package.

Scoped to `tests/runlog/` rather than the global `tests/conftest.py`: AC-11
(pointing HOME/every BTH_* path at tmp_path suite-wide) is step 1 of the
spec's delivery order and out of this package's scope (step 2a). Every test
here still must never touch the real `~/.bth/projects.toml` or
`~/.bth/log-mirror/`, so HOME is isolated locally instead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import bathos.runlog.project_id as project_id_mod
import bathos.runlog.writer as writer_mod
from bathos.runlog.writer import reset_writers_for_test


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME (and therefore ~/.bth/projects.toml, ~/.bth/log-mirror/,
    ~/.bth/catalog/) at a throwaway directory for every runlog test.

    `os.environ["HOME"]` alone is not enough: `PROJECTS_REGISTRY` and
    `MIRROR_ROOT` are module-level `Path.home() / ...` constants, evaluated
    once at import time (same pattern as `bathos.config.PROJECTS_REGISTRY`,
    which existing tests -- test_sprint_audit.py, test_sprint_audit_signals.py
    -- isolate the same way: monkeypatching the module attribute directly
    rather than relying on a cached Path.home() call to re-resolve).
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(project_id_mod, "PROJECTS_REGISTRY", fake_home / ".bth" / "projects.toml")
    monkeypatch.setattr(writer_mod, "MIRROR_ROOT", fake_home / ".bth" / "log-mirror")
    monkeypatch.delenv("BTH_PROJECT_SLUG", raising=False)
    monkeypatch.delenv("BTH_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("BTH_LOG_MODE", raising=False)
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.delenv("BTH_PROJECT_ID", raising=False)
    yield fake_home
    reset_writers_for_test()


def make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return path


def write_bth_toml(root: Path, slug: str, project_id: str | None = None) -> None:
    content = f'[project]\nslug = "{slug}"\nroot = "{root}"\n'
    if project_id:
        content += f'id = "{project_id}"\n'
    (root / ".bth.toml").write_text(content)
