"""Lock-location configuration and repo selection (spec 261026_provenance-out-of-working-tree, P0/P1, T1).

The resolver must fail loudly (``resolve_flag`` swallows a malformed config, B10), report the deciding layer per
key, and read ``cwd/.bth.toml [provenance]`` with raw tomllib so a file without ``[project]`` still counts (B17).
Repo selection must use the SIDECAR's checkout, never cwd (B5), and refuse the ref path for sentinel hashes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bathos import lock_config
from bathos.lock_config import (
    LockConfigError,
    resolve_lock_settings,
    select_lock_repo,
)

_ENV = ("BTH_LOCK_LOCATION", "BTH_LOCK_DIR", "BTH_IGNORE_OUTPUTS")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    return tmp_path / "catalog"


def _resolve(cwd: Path, catalog: Path, **kw):
    return resolve_lock_settings(cwd=cwd, catalog_dir=catalog, **kw)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _user_config(tmp_path: Path, text: str) -> Path:
    # HOME is redirected under tmp_path/home by the autouse bathos_test_home fixture.
    return _write(tmp_path / "home" / ".bth" / "config.toml", text)


# -- defaults and layers -------------------------------------------------------------------------


def test_defaults(tmp_path, catalog):
    s = _resolve(tmp_path, catalog)
    assert s.lock_location == "git"
    assert s.lock_dir == catalog / "locks"
    assert s.ignore_outputs is False
    assert set(s.layers) == {"lock_location", "lock_dir", "ignore_outputs"}
    assert all(layer == "default" for layer in s.layers.values())


def test_explicit_beats_env(tmp_path, catalog, monkeypatch):
    monkeypatch.setenv("BTH_LOCK_LOCATION", "tree")
    s = _resolve(tmp_path, catalog, lock_location="git")
    assert s.lock_location == "git"
    assert s.layers["lock_location"] == "argument"


def test_env_beats_bth_toml(tmp_path, catalog, monkeypatch):
    _write(tmp_path / ".bth.toml", '[provenance]\nlock_location = "git"\n')
    monkeypatch.setenv("BTH_LOCK_LOCATION", "tree")
    s = _resolve(tmp_path, catalog)
    assert s.lock_location == "tree"
    assert s.layers["lock_location"] == "$BTH_LOCK_LOCATION"


def test_bth_toml_without_project_table(tmp_path, catalog):
    """B17: load_project_config demands [project]; this layer must not."""
    cfg = _write(
        tmp_path / ".bth.toml",
        '[provenance]\nlock_location = "tree"\nlock_dir = "/somewhere/locks"\nignore_outputs = true\n',
    )
    s = _resolve(tmp_path, catalog)
    assert s.lock_location == "tree"
    assert s.lock_dir == Path("/somewhere/locks")
    assert s.ignore_outputs is True
    for key in ("lock_location", "lock_dir", "ignore_outputs"):
        assert str(cfg) in s.layers[key]


def test_bth_toml_is_cwd_only_not_walked_up(tmp_path, catalog):
    """The runner reads cwd/.bth.toml (runner.py:866), so a parent's file is not this layer."""
    _write(tmp_path / ".bth.toml", '[provenance]\nlock_location = "tree"\n')
    sub = tmp_path / "sub"
    sub.mkdir()
    assert _resolve(sub, catalog).lock_location == "git"


def test_pyproject_layer_walks_up(tmp_path, catalog):
    pyproject = _write(
        tmp_path / "pyproject.toml", '[tool.bathos.provenance]\nlock_location = "tree"\n'
    )
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    s = _resolve(sub, catalog)
    assert s.lock_location == "tree"
    assert str(pyproject) in s.layers["lock_location"]


def test_pyproject_without_table_falls_through(tmp_path, catalog):
    _write(tmp_path / "pyproject.toml", '[project]\nname = "x"\n')
    _user_config(tmp_path, "[provenance]\nignore_outputs = true\n")
    s = _resolve(tmp_path, catalog)
    assert s.ignore_outputs is True
    assert s.lock_location == "git"


def test_bth_toml_beats_pyproject(tmp_path, catalog):
    _write(tmp_path / ".bth.toml", '[provenance]\nlock_location = "git"\n')
    _write(tmp_path / "pyproject.toml", '[tool.bathos.provenance]\nlock_location = "tree"\n')
    assert _resolve(tmp_path, catalog).lock_location == "git"


def test_user_config_layer(tmp_path, catalog):
    user = _user_config(tmp_path, '[provenance]\nlock_location = "tree"\nlock_dir = "/u/locks"\n')
    s = _resolve(tmp_path, catalog)
    assert s.lock_location == "tree"
    assert s.lock_dir == Path("/u/locks")
    assert str(user) in s.layers["lock_dir"]


def test_pyproject_beats_user_config(tmp_path, catalog):
    _write(tmp_path / "pyproject.toml", '[tool.bathos.provenance]\nlock_location = "git"\n')
    _user_config(tmp_path, '[provenance]\nlock_location = "tree"\n')
    assert _resolve(tmp_path, catalog).lock_location == "git"


def test_keys_resolve_independently(tmp_path, catalog, monkeypatch):
    _write(tmp_path / ".bth.toml", "[provenance]\nignore_outputs = true\n")
    monkeypatch.setenv("BTH_LOCK_LOCATION", "tree")
    s = _resolve(tmp_path, catalog)
    assert (s.lock_location, s.ignore_outputs) == ("tree", True)
    assert s.layers["lock_location"] == "$BTH_LOCK_LOCATION"
    assert ".bth.toml" in s.layers["ignore_outputs"]
    assert s.layers["lock_dir"] == "default"


def test_env_ignore_outputs_both_directions(tmp_path, catalog, monkeypatch):
    _write(tmp_path / ".bth.toml", "[provenance]\nignore_outputs = true\n")
    monkeypatch.setenv("BTH_IGNORE_OUTPUTS", "0")
    assert _resolve(tmp_path, catalog).ignore_outputs is False
    monkeypatch.setenv("BTH_IGNORE_OUTPUTS", "1")
    assert _resolve(tmp_path, catalog).ignore_outputs is True


def test_relative_lock_dir_in_file_is_relative_to_that_file(tmp_path, catalog):
    _write(tmp_path / ".bth.toml", '[provenance]\nlock_dir = "state/locks"\n')
    assert _resolve(tmp_path, catalog).lock_dir == tmp_path / "state" / "locks"


# -- disabling lock_dir ----------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["none", "None", ""])
def test_env_lock_dir_none_disables(tmp_path, catalog, monkeypatch, value):
    monkeypatch.setenv("BTH_LOCK_DIR", value)
    s = _resolve(tmp_path, catalog)
    assert s.lock_dir is None
    assert "disabled" in s.layers["lock_dir"]


def test_disabled_decision_is_final(tmp_path, catalog, monkeypatch):
    _write(tmp_path / ".bth.toml", '[provenance]\nlock_dir = "/x"\n')
    monkeypatch.setenv("BTH_LOCK_DIR", "none")
    assert _resolve(tmp_path, catalog).lock_dir is None


def test_file_lock_dir_none_disables(tmp_path, catalog):
    _write(tmp_path / ".bth.toml", '[provenance]\nlock_dir = "none"\n')
    assert _resolve(tmp_path, catalog).lock_dir is None


# -- loud failures ---------------------------------------------------------------------------------


def test_invalid_env_lock_location_fails(tmp_path, catalog, monkeypatch):
    monkeypatch.setenv("BTH_LOCK_LOCATION", "elsewhere")
    with pytest.raises(LockConfigError, match="BTH_LOCK_LOCATION"):
        _resolve(tmp_path, catalog)


def test_invalid_explicit_lock_location_fails(tmp_path, catalog):
    with pytest.raises(LockConfigError, match="lock_location"):
        _resolve(tmp_path, catalog, lock_location="blob")


def test_invalid_file_lock_location_fails(tmp_path, catalog):
    _write(tmp_path / ".bth.toml", '[provenance]\nlock_location = "ref"\n')
    with pytest.raises(LockConfigError, match="lock_location"):
        _resolve(tmp_path, catalog)


def test_invalid_env_ignore_outputs_fails(tmp_path, catalog, monkeypatch):
    monkeypatch.setenv("BTH_IGNORE_OUTPUTS", "maybe")
    with pytest.raises(LockConfigError, match="BTH_IGNORE_OUTPUTS"):
        _resolve(tmp_path, catalog)


@pytest.mark.parametrize(
    "where",
    ["bth_toml", "pyproject", "user"],
)
def test_malformed_file_fails(tmp_path, catalog, where):
    bad = "[provenance\nlock_location = "
    if where == "bth_toml":
        path = _write(tmp_path / ".bth.toml", bad)
    elif where == "pyproject":
        path = _write(tmp_path / "pyproject.toml", bad)
    else:
        path = _user_config(tmp_path, bad)
    with pytest.raises(LockConfigError, match="cannot read") as ei:
        _resolve(tmp_path, catalog)
    assert str(path) in str(ei.value)


def test_malformed_bth_toml_without_provenance_still_fails(tmp_path, catalog):
    """Not swallowed even though the table is absent: a broken file is a broken file."""
    _write(tmp_path / ".bth.toml", "this is = = not toml")
    with pytest.raises(LockConfigError):
        _resolve(tmp_path, catalog)


def test_other_provenance_keys_are_ignored_not_rejected(tmp_path, catalog):
    """`track_packages` (dependency provenance) lives in the same `[provenance]` table; the lock resolver must
    leave it alone, whether it is alone or beside lock keys, in every file layer."""
    cfg = _write(tmp_path / ".bth.toml", '[provenance]\ntrack_packages = ["aminx"]\n')
    s = _resolve(tmp_path, catalog)
    assert (s.lock_location, s.ignore_outputs) == ("git", False)

    _write(cfg, '[provenance]\ntrack_packages = ["aminx"]\nlock_location = "tree"\nsome_future_key = 1\n')
    s = _resolve(tmp_path, catalog)
    assert s.lock_location == "tree"

    _write(tmp_path / "pyproject.toml", '[tool.bathos.provenance]\ntrack_packages = ["x"]\n')
    _user_config(tmp_path, '[provenance]\ntrack_packages = ["y"]\nignore_outputs = true\n')
    assert _resolve(tmp_path, catalog).ignore_outputs is True


def test_wrong_value_types_still_fail(tmp_path, catalog):
    cfg = tmp_path / ".bth.toml"
    _write(cfg, "[provenance]\nignore_outputs = \"yes\"\n")
    with pytest.raises(LockConfigError, match="ignore_outputs"):
        _resolve(tmp_path, catalog)
    _write(cfg, "[provenance]\nlock_dir = 3\n")
    with pytest.raises(LockConfigError, match="lock_dir"):
        _resolve(tmp_path, catalog)
    _write(cfg, 'provenance = "x"\n')
    with pytest.raises(LockConfigError, match="table"):
        _resolve(tmp_path, catalog)


@pytest.mark.parametrize("where", ["pyproject", "user"])
def test_malformed_lower_file_fails_even_when_higher_layer_decides(tmp_path, catalog, where):
    """Spec P1: a malformed file at ANY layer raises, not only a consulted one."""
    _write(
        tmp_path / ".bth.toml",
        '[provenance]\nlock_location = "git"\nlock_dir = "none"\nignore_outputs = false\n',
    )
    bad = "[provenance\nlock_location = "
    path = (
        _write(tmp_path / "pyproject.toml", bad) if where == "pyproject" else _user_config(tmp_path, bad)
    )
    with pytest.raises(LockConfigError, match="cannot read") as ei:
        _resolve(tmp_path, catalog)
    assert str(path) in str(ei.value)


def test_explicit_args_do_not_skip_malformed_file_detection(tmp_path, catalog):
    _write(tmp_path / ".bth.toml", "= broken")
    with pytest.raises(LockConfigError):
        _resolve(tmp_path, catalog, lock_location="git", lock_dir="none", ignore_outputs=False)


# -- repo selection (P0) ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _make_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    (path / "scripts").mkdir()
    (path / "scripts" / "x.bth.toml").write_text("[experiment]\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")
    return _git(path, "rev-parse", "HEAD")


def test_selects_sidecar_repo(tmp_path):
    repo = tmp_path / "repo"
    sha = _make_repo(repo)
    choice = select_lock_repo(repo / "scripts" / "x.bth.toml", sha)
    assert choice.ref_error is None
    assert choice.root == repo.resolve()
    assert choice.sidecar_dir_rel == "scripts"
    assert choice.lock_relpath("x", "r1") == "scripts/x.r1.bth.lock.toml"


def test_sidecar_at_repo_root_has_bare_relpath(tmp_path):
    repo = tmp_path / "repo"
    sha = _make_repo(repo)
    choice = select_lock_repo(repo / "x.bth.toml", sha)
    assert choice.sidecar_dir_rel == ""
    assert choice.lock_relpath("x", "r1") == "x.r1.bth.lock.toml"


def test_foreign_cwd_uses_sidecar_repo(tmp_path, monkeypatch):
    """B5: a run launched from another repo must lock in the SIDECAR's repo."""
    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    sha_a = _make_repo(repo_a)
    _make_repo(repo_b)
    monkeypatch.chdir(repo_b)
    choice = select_lock_repo(repo_a / "scripts" / "x.bth.toml", sha_a)
    assert choice.root == repo_a.resolve()
    assert choice.ref_error is None


def test_linked_worktree_selects_the_worktree_root(tmp_path):
    repo = tmp_path / "repo"
    sha = _make_repo(repo)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "side")
    choice = select_lock_repo(wt / "scripts" / "x.bth.toml", sha)
    assert choice.root == wt.resolve()


@pytest.mark.parametrize(
    "sentinel", ["", "unknown", "nogit", "abc123", "G" * 40, "a" * 39, "A" * 40]
)
def test_sentinel_or_malformed_hash_falls_back(tmp_path, sentinel):
    repo = tmp_path / "repo"
    _make_repo(repo)
    choice = select_lock_repo(repo / "scripts" / "x.bth.toml", sentinel)
    assert choice.root is None
    assert choice.ref_error == "sentinel_hash"


def test_broken_gitlink_reports_unknown_hash(tmp_path):
    """A .git file pointing nowhere: capture reports hash 'unknown' (channels.py:108-116)."""
    broken = tmp_path / "broken"
    (broken / "scripts").mkdir(parents=True)
    (broken / ".git").write_text("gitdir: /nonexistent/elsewhere\n")
    choice = select_lock_repo(broken / "scripts" / "x.bth.toml", "unknown")
    assert (choice.root, choice.ref_error) == (None, "sentinel_hash")


def test_real_hash_without_repo_is_no_repo(tmp_path):
    """A myxcel-env hash can be real on a host with no .git (B13)."""
    plain = tmp_path / "plain"
    (plain / "scripts").mkdir(parents=True)
    choice = select_lock_repo(plain / "scripts" / "x.bth.toml", "a" * 40)
    assert (choice.root, choice.ref_error) == (None, "no_repo")


def test_missing_sidecar_dir_is_no_repo(tmp_path):
    choice = select_lock_repo(tmp_path / "gone" / "x.bth.toml", "a" * 40)
    assert (choice.root, choice.ref_error) == (None, "no_repo")


def test_sidecar_outside_reported_root_falls_back(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    sha = _make_repo(repo)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(lock_config, "_show_toplevel", lambda _d: other)
    choice = select_lock_repo(repo / "scripts" / "x.bth.toml", sha)
    assert (choice.root, choice.ref_error) == (None, "sidecar_outside_repo")


def test_ambient_git_env_does_not_redirect_probe(tmp_path, monkeypatch):
    """clean_git_env: a GIT_DIR from a hook context must not win over the sidecar dir."""
    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    sha_a = _make_repo(repo_a)
    _make_repo(repo_b)
    monkeypatch.setenv("GIT_DIR", str(repo_b / ".git"))
    choice = select_lock_repo(repo_a / "scripts" / "x.bth.toml", sha_a)
    assert choice.root == repo_a.resolve()


def test_tolerated_malformed_project_config_is_skipped_with_a_warning(tmp_path, catalog):
    """The runner passes tolerate_malformed_project_config=True: an unreadable cwd/.bth.toml is the one file it has
    always tolerated. Other layers and invalid values still raise."""
    _write(tmp_path / ".bth.toml", "[project\nslug = ")
    s = resolve_lock_settings(cwd=tmp_path, catalog_dir=catalog, tolerate_malformed_project_config=True)
    assert s.lock_location == "git" and s.warnings and "cannot read" in s.warnings[0]
    with pytest.raises(LockConfigError):
        resolve_lock_settings(cwd=tmp_path, catalog_dir=catalog)  # strict by default

    _user_config(tmp_path, "[provenance\n")
    with pytest.raises(LockConfigError):
        resolve_lock_settings(cwd=tmp_path, catalog_dir=catalog, tolerate_malformed_project_config=True)
