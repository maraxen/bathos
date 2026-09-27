"""The real-~/.bth tripwire fires on real-home writes and stays silent otherwise."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests import real_home_guard as g

# A parent that never exists, so even a broken hook cannot create anything real.
PROBE = g.REAL_BTH / ".guard-probe-never-created" / "x"


@pytest.fixture
def expect_violation():
    n = len(g.violations)
    yield
    assert len(g.violations) == n + 1
    del g.violations[n:]  # expected; must not fail the session


@pytest.mark.parametrize(
    "write",
    [
        lambda: open(PROBE, "w"),  # noqa: SIM115
        lambda: PROBE.write_text("x"),
        lambda: os.open(PROBE, os.O_WRONLY | os.O_CREAT),
        lambda: os.mkdir(PROBE),
        lambda: os.replace(PROBE, PROBE.with_name("y")),
    ],
    ids=["open", "write_text", "os.open", "mkdir", "replace"],
)
@pytest.mark.usefixtures("expect_violation")
def test_real_home_write_is_refused(write):
    with pytest.raises(g.RealHomeWriteError):
        write()
    assert not PROBE.parent.exists()


def test_fake_home_write_is_allowed():
    target = Path.home() / ".bth" / "projects.toml"
    assert not str(target).startswith(str(g.REAL_BTH) + os.sep)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("[[roots]]\n")
    assert target.read_text() == "[[roots]]\n"


def test_real_home_read_is_allowed():
    n = len(g.violations)
    g._hook("open", (str(g.REAL_BTH / "projects.toml"), "r", os.O_RDONLY))
    assert len(g.violations) == n


def test_sessionfinish_fails_session_on_fingerprint_change(monkeypatch):
    from types import SimpleNamespace

    from tests import conftest

    stash = {conftest._REAL_BTH_BEFORE: {"projects.toml": (1, 1)}}
    config = SimpleNamespace(stash=stash, pluginmanager=SimpleNamespace(get_plugin=lambda _: None))
    session = SimpleNamespace(config=config, exitstatus=pytest.ExitCode.OK)

    monkeypatch.setattr(g, "fingerprint", lambda: {"projects.toml": (1, 1)})
    conftest.pytest_sessionfinish(session)
    assert session.exitstatus == pytest.ExitCode.OK  # unchanged -> silent

    monkeypatch.setattr(g, "fingerprint", lambda: {"projects.toml": (2, 2)})
    conftest.pytest_sessionfinish(session)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED


def test_diff_reports_created_and_modified():
    assert g.diff({"a": (1, 1)}, {"a": (1, 2), "b": (0, 0)}) == [
        f"{g.REAL_BTH / 'a'}: modified",
        f"{g.REAL_BTH / 'b'}: created",
    ]
