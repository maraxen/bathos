"""Direct unit tests for `bathos.linter.iter_project_sidecars`.

Five lint checks share this one walk, but every other test exercises it only
indirectly through a single caller. These pin the walk's own contract -- boundary
detection for both git layouts, the prune list, the root self-exclusion, ordering,
and the narrow-root form -- so a future change to any of them fails here rather
than silently shrinking what five checks can see.
"""

from pathlib import Path

from bathos.linter import iter_project_sidecars

SIDECAR = "[experiment]\nhypothesis='h'\n[result_schema]\n"


def _sidecar(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    p.write_text(SIDECAR)
    return p


def test_finds_plain_sidecars(tmp_path):
    a = _sidecar(tmp_path / "scripts" / "experiments", "run_a.bth.toml")
    b = _sidecar(tmp_path / "scripts" / "benchmarks", "run_b.bth.toml")

    assert set(iter_project_sidecars(tmp_path)) == {a, b}


def test_submodule_boundary_git_directory_is_pruned(tmp_path):
    """A vendored submodule checkout carries `.git` as a *directory*."""
    kept = _sidecar(tmp_path / "scripts", "mine.bth.toml")
    nested = tmp_path / "external" / "vendored_dep"
    (nested / ".git").mkdir(parents=True)
    _sidecar(nested, "theirs.bth.toml")
    _sidecar(nested / "deeper", "theirs_deeper.bth.toml")

    assert iter_project_sidecars(tmp_path) == [kept]


def test_worktree_boundary_git_file_is_pruned(tmp_path):
    """An agent-managed worktree carries `.git` as a *file* pointing at a gitdir."""
    kept = _sidecar(tmp_path / "scripts", "mine.bth.toml")
    wt = tmp_path / ".claude" / "worktrees" / "wt-1"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt-1\n")
    _sidecar(wt, "stale_copy.bth.toml")

    assert iter_project_sidecars(tmp_path) == [kept]


def test_prune_dirs_are_excluded(tmp_path):
    """`_WALK_PRUNE_DIRS` entries are skipped even with no `.git` anywhere near."""
    kept = _sidecar(tmp_path / "scripts", "mine.bth.toml")
    for noise in (".venv", "node_modules", "__pycache__", ".pytest_cache"):
        _sidecar(tmp_path / noise / "lib", "noise.bth.toml")

    assert iter_project_sidecars(tmp_path) == [kept]


def test_root_itself_is_never_a_boundary(tmp_path):
    """Linting from inside a worktree must scan it, not halt on its own `.git`.

    This is the guard that keeps `bth lint` usable from within `.claude/worktrees/`,
    where the project root legitimately carries a `.git` file of its own.
    """
    (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/self\n")
    mine = _sidecar(tmp_path / "scripts", "mine.bth.toml")

    assert iter_project_sidecars(tmp_path) == [mine]


def test_narrow_root_prunes_boundaries_inside_it(tmp_path):
    """The narrow-root form used by check_baseline_ref_exists / check_popper_adversarial.

    Callers pass the subtree directly rather than walking the whole project and
    filtering afterwards, so boundary pruning must still apply *within* that subtree.
    """
    scripts_dir = tmp_path / "scripts" / "benchmarks"
    kept = _sidecar(scripts_dir, "bench.bth.toml")
    nested = scripts_dir / "vendored_variant"
    (nested / ".git").mkdir(parents=True)
    _sidecar(nested, "variant.bth.toml")

    assert iter_project_sidecars(scripts_dir) == [kept]


def test_narrow_root_excludes_everything_outside_it(tmp_path):
    """Passing a narrow root must not pick up sidecars elsewhere in the project."""
    scripts_dir = tmp_path / "scripts" / "benchmarks"
    kept = _sidecar(scripts_dir, "bench.bth.toml")
    _sidecar(tmp_path / "scripts" / "experiments", "exp.bth.toml")
    _sidecar(tmp_path / "docs", "stray.bth.toml")

    assert iter_project_sidecars(scripts_dir) == [kept]


def test_run_lock_files_are_not_sidecars(tmp_path):
    """`<stem>.bth.<run_id>.bth.lock.toml` ends in ".bth.lock.toml", not ".bth.toml"."""
    scripts_dir = tmp_path / "scripts" / "experiments"
    kept = _sidecar(scripts_dir, "run_nvt.bth.toml")
    (scripts_dir / "run_nvt.bth.deadbeef-0000-0000-0000-000000000000.bth.lock.toml").write_text(
        "[manifest]\nrun_id = 'deadbeef'\n"
    )

    assert iter_project_sidecars(tmp_path) == [kept]


def test_results_are_sorted(tmp_path):
    """Ordering is centralized in the walk so `bth lint` output is stable run-to-run.

    Creation order is deliberately non-alphabetical: filesystem walk order is
    neither creation nor lexical order, so an unsorted implementation passes
    single-sidecar tests and only diverges once a project has several.
    """
    d = tmp_path / "scripts" / "experiments"
    for name in ("z_run", "m_run", "a_run"):
        _sidecar(d, f"{name}.bth.toml")

    found = iter_project_sidecars(tmp_path)
    assert found == sorted(found)
    assert [p.name for p in found] == ["a_run.bth.toml", "m_run.bth.toml", "z_run.bth.toml"]


def test_boundary_pruning_emits_telemetry(tmp_path, monkeypatch):
    """Scope reduction must be observable -- a dropped sidecar otherwise looks like a clean project."""
    import bathos.linter as linter_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(linter_mod, "event", lambda name, **fields: events.append((name, fields)))

    _sidecar(tmp_path / "scripts", "mine.bth.toml")
    nested = tmp_path / "external" / "dep"
    (nested / ".git").mkdir(parents=True)
    _sidecar(nested, "theirs.bth.toml")

    iter_project_sidecars(tmp_path)

    assert [name for name, _ in events] == ["lint.boundary_pruned"]
    assert events[0][1]["pruned_boundaries"] == 1


def test_no_telemetry_when_nothing_pruned(tmp_path, monkeypatch):
    import bathos.linter as linter_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(linter_mod, "event", lambda name, **fields: events.append((name, fields)))

    _sidecar(tmp_path / "scripts", "mine.bth.toml")
    iter_project_sidecars(tmp_path)

    assert events == []
