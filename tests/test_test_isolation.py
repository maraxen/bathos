"""Regression coverage for praxia debt #1949.

Before the `bathos_test_home` autouse fixture (tests/conftest.py) existed,
any bathos test that didn't explicitly monkeypatch every relevant env var
fell through to `Path.home()` and wrote real state into the developer's
`~/.bth/projects.toml`, `~/.bth/catalog/`, `~/.bth/mcp_token`, etc. This file
asserts that every bathos path accessor that can fall back to `Path.home()`
(or an unset `BTH_*` env var) resolves under pytest's `tmp_path` once that
fixture has run -- i.e. the isolation actually holds, not just that the
fixture exists.

`bathos_test_home` is autouse, so it is already active for every test here;
we don't request it explicitly.
"""

from pathlib import Path

import bathos.cli_common as cli_common
import bathos.config as config
import bathos.mcp as mcp
import bathos.mcp_auth as mcp_auth
import bathos.telemetry as telemetry


def _assert_under(path: Path, root: Path) -> None:
    resolved = path.resolve()
    root_resolved = root.resolve()
    assert resolved == root_resolved or root_resolved in resolved.parents, (
        f"{path} does not resolve under {root}"
    )


def test_home_itself_is_redirected(tmp_path: Path):
    """Sanity check the fixture's core mechanism: Path.home() must move.

    Every accessor covered below (and several more that are function-local
    `Path.home() / ...` expressions in archive.py/repair.py/verify.py --
    not separately exercised here since they reduce to this exact call)
    depends on this holding.
    """
    _assert_under(Path.home(), tmp_path)


def test_projects_registry_resolves_under_tmp_path(tmp_path: Path):
    """config.PROJECTS_REGISTRY is a module-level constant bound once at
    import time -- the specific gotcha behind debt #1949. The fixture must
    patch it directly (not just redirect HOME) for this to hold.
    """
    _assert_under(config.PROJECTS_REGISTRY, tmp_path)
    assert config.PROJECTS_REGISTRY.name == "projects.toml"


def test_default_catalog_dir_resolves_under_tmp_path(tmp_path: Path):
    _assert_under(config.default_catalog_dir(), tmp_path)


def test_cli_common_catalog_dir_resolves_under_tmp_path(tmp_path: Path, monkeypatch):
    """No BTH_CATALOG_DIR (cleared by the fixture) and no .bth.toml in an
    ancestor of cwd (tmp_path has none) -- falls all the way to
    default_catalog_dir() -> Path.home().
    """
    monkeypatch.chdir(tmp_path)
    _assert_under(cli_common.catalog_dir(), tmp_path)


def test_mcp_get_catalog_dir_resolves_under_tmp_path(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _assert_under(mcp._get_catalog_dir(None), tmp_path)


def test_mcp_auth_token_path_resolves_under_tmp_path(tmp_path: Path):
    _assert_under(mcp_auth.token_path(), tmp_path)


def test_telemetry_default_log_dir_resolves_under_tmp_path(tmp_path: Path):
    _assert_under(telemetry._get_default_log_dir(), tmp_path)


def test_bth_path_env_vars_are_absent_by_default():
    """The fixture clears these rather than force-setting them (see its
    docstring for why), so by default none of them should be set at all --
    any code path that reads one directly gets nothing and must fall
    through to the Path.home()-based default, which the tests above cover.
    """
    import os

    from tests.conftest import _BTH_PATH_ENV_VARS

    for var in _BTH_PATH_ENV_VARS:
        assert var not in os.environ, f"{var} unexpectedly set: {os.environ.get(var)!r}"


def test_git_config_global_points_under_tmp_path(tmp_path: Path):
    """HOME redirection hides the real ~/.gitconfig; the fixture must supply
    a substitute identity via GIT_CONFIG_GLOBAL so `git commit` still works
    under the isolated HOME (used by capture_git_state / repo-init helpers).
    """
    import os

    git_config_global = os.environ.get("GIT_CONFIG_GLOBAL")
    assert git_config_global is not None
    _assert_under(Path(git_config_global), tmp_path)
    content = Path(git_config_global).read_text()
    assert "[user]" in content
    assert "email" in content


def test_test_setenv_override_still_wins(tmp_path: Path, monkeypatch):
    """A test that sets its own BTH_CATALOG_DIR after the autouse fixture ran
    must still see its own value -- monkeypatch composes within one test.
    """
    custom = tmp_path / "custom_catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(custom))
    assert cli_common.catalog_dir() == custom
