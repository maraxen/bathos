import os
from pathlib import Path

import pytest

# Before ANY bathos import: bathos modules resolve HOME-derived paths at import time (e.g.
# cli_cyclopts calls init_telemetry(), which opened a log handler in the real
# ~/.bth/catalog/logs/ for the whole session). Redirect HOME session-wide, then arm the tripwire.
from tests import real_home_guard

real_home_guard.redirect_session_home()
real_home_guard.install()

from bathos.schema import Run  # noqa: E402

# Guard repair module import for test collection — repair.py is optional at collection time
pytest.importorskip("bathos.repair")


def pytest_configure(config):
    """Snapshot the real ~/.bth before any test module is imported."""
    config.stash[_REAL_BTH_BEFORE] = real_home_guard.fingerprint()


_REAL_BTH_BEFORE = pytest.StashKey[dict]()


def pytest_sessionfinish(session):
    """Fail the session if anything (in-process or a subprocess) wrote the real ~/.bth."""
    before = session.config.stash.get(_REAL_BTH_BEFORE, None)
    changed = real_home_guard.diff(before, real_home_guard.fingerprint()) if before else []
    if not (changed or real_home_guard.violations):
        return
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    lines = ["tests wrote the REAL ~/.bth (see tests/real_home_guard.py):", *changed]
    lines += [f"refused in-process: {v}" for v in real_home_guard.violations]
    if tr is not None:
        tr.section("real ~/.bth leak", sep="!", red=True)
        for line in lines:
            tr.write_line(line)
    else:
        print("\n".join(lines))
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture
def tmp_catalog(tmp_path: Path) -> Path:
    catalog = tmp_path / ".bth" / "catalog"
    catalog.mkdir(parents=True)
    return catalog


@pytest.fixture
def sample_run() -> Run:
    return Run(
        project_slug="testproj",
        command="python scripts/experiments/run.py --n 10",
        argv=["python", "scripts/experiments/run.py", "--n", "10"],
        git_hash="deadbeef",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        duration_s=2.5,
        output_paths=["/tmp/results.parquet"],
        tags=["smoke"],
        hostname="test-host",
    )


@pytest.fixture(autouse=True)
def clear_myxcel_env(monkeypatch):
    """Autouse fixture to clear all MYXCEL_* env vars for each test.

    This prevents ambient MYXCEL_* variables from a myxcel-submitted job
    from affecting test behavior. Tests that need specific MYXCEL_* values
    can set them explicitly via monkeypatch after this fixture clears them.
    """
    myxcel_vars = [k for k in os.environ if k.startswith("MYXCEL_")]
    for var in myxcel_vars:
        monkeypatch.delenv(var, raising=False)


# BTH_* env vars that name a filesystem path (as opposed to e.g. BTH_PROJECT_SLUG,
# BTH_LOG_LEVEL, BTH_TASK_ID, BTH_OBLIGATION_*, BTH_DIFFERENTIAL_* which are not
# paths). Found via `rg -n 'BTH_[A-Z_]+' src/bathos` and cross-checked against
# every accessor that treats the value as a Path.
_BTH_PATH_ENV_VARS = real_home_guard.BTH_PATH_ENV_VARS


@pytest.fixture(autouse=True)
def bathos_test_home(tmp_path, monkeypatch):
    """Isolate every bathos filesystem accessor under pytest's ``tmp_path``.

    praxia debt #1949: several bathos tests were writing into the real
    ``~/.bth/projects.toml`` (and other real ``~/.bth/*`` state) because
    ``bathos`` falls back to ``Path.home()`` / ``expanduser("~")`` almost
    everywhere a path isn't explicitly overridden by config or an env var
    (`config.py`, `archive.py`, `mcp_auth.py`, `repair.py`, `runner.py`,
    `telemetry.py`, `verify.py`, `mcp.py`, `campaigns.py`, `cluster_catalog.py`
    all do this). Redirecting HOME fixes every one of those call sites because
    Python's `Path.home()`/`os.path.expanduser` re-reads `$HOME` on every call.

    The ONE exception is `bathos.config.PROJECTS_REGISTRY`: it is a **module-level
    constant** — `Path.home() / ".bth" / "projects.toml"` — computed exactly once,
    at import time. If `bathos.config` was already imported before this fixture
    runs (near-certain during test collection), a bare `$HOME` monkeypatch does
    nothing to it, since the already-bound Path object doesn't get re-derived.
    It has to be patched directly as a module attribute instead.

    We *clear* (rather than force-set) the BTH_*-path env vars found via
    `rg -n 'BTH_[A-Z_]+' src/bathos`, so any ambient value exported in a
    developer's or CI shell can't leak a real path into a test, while a test
    that sets its own value afterwards (the common existing pattern in this
    suite, e.g. `monkeypatch.setenv("BTH_CATALOG_DIR", ...)`) still wins —
    monkeypatch composes calls within the same test in order, so a later
    setenv/delenv in the test body simply overrides what this fixture did.
    Force-setting them instead (e.g. always pointing BTH_CATALOG_DIR at a tmp
    path) would break existing precedence tests such as
    `test_catalog_dir_reads_project_config` (asserts `.bth.toml`'s
    `catalog_dir` wins when BTH_CATALOG_DIR is unset) since the env var
    outranks the config file in `cli_common.catalog_dir()`. Clearing has the
    same net effect for the leakage this debt is about — every fallback path
    still resolves under `tmp_path` via the HOME redirect below — without that
    precedence risk.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    # Harmless on POSIX (this box is WSL2/Linux), but keeps the intent explicit
    # in case any dependency consults the Windows-style var too.
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    for var in _BTH_PATH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    # BTH_PROJECT_SLUG is not a path, but it participates in the same
    # ambient-leakage story (it can silently redirect which project a run is
    # attributed to) — clear it too so tests opt in explicitly.
    monkeypatch.delenv("BTH_PROJECT_SLUG", raising=False)

    # config.PROJECTS_REGISTRY: see docstring above — module-level constant,
    # bound once at import time, immune to the HOME monkeypatch by itself.
    import bathos.config as _bathos_config

    monkeypatch.setattr(_bathos_config, "PROJECTS_REGISTRY", fake_home / ".bth" / "projects.toml")

    # Redirecting HOME hides the real ~/.gitconfig, so any test that shells
    # out to `git commit` (e.g. capture_git_state / repo-init helpers) would
    # otherwise fail with "Author identity unknown". Point git at a throwaway
    # global config with a fixed test identity instead.
    git_config = tmp_path / ".gitconfig_test"
    if not git_config.exists():
        git_config.write_text(
            "[user]\n"
            "\tname = Bathos Test\n"
            "\temail = bathos-test@example.invalid\n"
            "[init]\n"
            "\tdefaultBranch = main\n"
        )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    # Seed a deterministic MCP write-token under the redirected home. A few
    # tests (test_authoring_parity.py) read `Path.home() / ".bth" / "mcp_token"`
    # directly and `pytest.mark.skipif` on its existence -- but that skip
    # condition is evaluated once at collection time, against whatever HOME
    # was ambient *then* (before any fixture runs), while the test body's own
    # read happens at call time, now under our redirected HOME. On a machine
    # that already has a real `~/.bth/mcp_token` (this one included), the
    # collection-time check finds it and does not skip, so the test always
    # runs and would otherwise crash reading a token file that doesn't exist
    # under the fake home. Pre-creating it here keeps that test deterministic
    # regardless of the developer machine's real token state, and
    # `mcp_auth.get_or_create_token()` is what `check_token()` calls anyway,
    # so this just does that creation slightly earlier rather than changing
    # what value is used.
    import bathos.mcp_auth as _bathos_mcp_auth

    _bathos_mcp_auth.get_or_create_token()

    return fake_home
