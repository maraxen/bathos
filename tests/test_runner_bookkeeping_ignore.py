"""`bth run` ignores bookkeeping paths in the right repo(s) and warns per path (spec 261026 P2, T3)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from bathos import runner
from bathos.catalog import init_catalog
from bathos.git_pin import BOOKKEEPING_IGNORE_LINES, OUTPUTS_IGNORE_LINE
from bathos.runner import run_script

_SIDECAR = (
    "[experiment]\nhypothesis = 'h'\n[outcomes.pass]\ncondition = 'x == 1'\ndecision = 'good'\n"
    "reasoning = 'r'\n[outcomes.fallback]\ncondition = '1==1'\ndecision = 'o'\nreasoning = 'c'\n"
    "is_residual = true\n[result_schema]\nx = 'float'\n"
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("BTH_LOCK_LOCATION", "BTH_LOCK_DIR", "BTH_IGNORE_OUTPUTS"):
        monkeypatch.delenv(var, raising=False)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(path: Path) -> Path:
    exp = path / "scripts" / "experiments"
    exp.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t.com")
    _git(path, "config", "user.name", "T")
    (exp / "run_x.py").write_text("print('hi')\n")
    (exp / "run_x.bth.toml").write_text(_SIDECAR)
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")
    return exp / "run_x.py"


def _exclude(repo: Path) -> list[str]:
    out = _git(repo, "rev-parse", "--git-path", "info/exclude")
    path = Path(out) if Path(out).is_absolute() else repo / out
    return path.read_text().splitlines() if path.exists() else []


def _run(script: Path, cwd: Path, catalog: Path) -> int:
    init_catalog(catalog)
    return run_script(
        argv=[sys.executable, str(script)],
        project_slug="p",
        catalog_dir=catalog,
        output_paths=[],
        tags=[],
        cwd=cwd,
    )


def test_run_ignores_every_bookkeeping_path(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    script = _repo(repo)

    assert _run(script, repo, tmp_path / "cat") == 0

    exclude = _exclude(repo)
    for line in BOOKKEEPING_IGNORE_LINES:
        assert line in exclude
    assert OUTPUTS_IGNORE_LINE not in exclude


def test_ignore_outputs_setting_reaches_the_exclude_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BTH_IGNORE_OUTPUTS", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    script = _repo(repo)

    assert _run(script, repo, tmp_path / "cat") == 0

    assert OUTPUTS_IGNORE_LINE in _exclude(repo)


def test_both_roots_are_covered_when_script_repo_differs_from_cwd_repo(tmp_path: Path):
    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()
    script = _repo(repo_a)
    _repo(repo_b)

    assert _run(script, repo_b, tmp_path / "cat") == 0

    for repo in (repo_a, repo_b):
        for line in BOOKKEEPING_IGNORE_LINES:
            assert line in _exclude(repo), (repo, line)


def test_one_warning_per_unignorable_path(tmp_path: Path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    script = _repo(repo)
    monkeypatch.setattr(
        runner,
        "ensure_bookkeeping_ignored",
        lambda *_a, **_k: {
            "/.bth/refs/manifest.jsonl": False,
            "/.bth/log/": False,
            "/outputs/provenance/": True,
            "/.bth/refs/.manifest.lock": True,
        },
    )

    assert _run(script, repo, tmp_path / "cat") == 0

    err = capsys.readouterr().err
    assert err.count("could not ensure") == 2
    assert "/.bth/refs/manifest.jsonl" in err and "/.bth/log/" in err
    assert "/outputs/provenance/" not in err


def test_invalid_ignore_outputs_env_fails_loudly(tmp_path: Path, monkeypatch):
    """Not swallowed by the best-effort pin block: a bad setting must stop the run."""
    from bathos.lock_config import LockConfigError

    monkeypatch.setenv("BTH_IGNORE_OUTPUTS", "maybe")
    repo = tmp_path / "repo"
    repo.mkdir()
    script = _repo(repo)

    with pytest.raises(LockConfigError):
        _run(script, repo, tmp_path / "cat")
