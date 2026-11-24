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


@pytest.mark.parametrize(
    "filename,expected_pid",
    [
        ("myhost.12345.1700000000000000000.jsonl", 12345),
        ("node01.cluster.local.9876.1700000000000000000.slurm-42-0-0.jsonl", 9876),
        ("unaffiliated/host.77.1700000000000000000.jsonl", 77),
        ("notes.txt", None),
        ("weird.jsonl", None),
    ],
)
def test_segment_pid_parsing(filename: str, expected_pid: int | None):
    assert g._segment_pid(filename) == expected_pid


def test_concurrent_external_log_and_mirror_activity_ignored(tmp_path, monkeypatch):
    fake_real_bth = tmp_path / "real_bth"
    (fake_real_bth / "log" / "unaffiliated").mkdir(parents=True)
    (fake_real_bth / "log-mirror" / "proj-1").mkdir(parents=True)
    monkeypatch.setattr(g, "REAL_BTH", fake_real_bth)

    before = g.fingerprint()

    ext_pid = 999_991
    assert ext_pid not in g.session_pids()
    ext_line = '{"main_root": "/home/user/real_proj", "writer": "realhost.999991.1700000000000000000"}\n'
    (fake_real_bth / "log" / "unaffiliated" / f"realhost.{ext_pid}.1700000000000000000.jsonl").write_text(
        ext_line
    )
    (fake_real_bth / "log-mirror" / "proj-1" / f"realhost.{ext_pid}.1700000000000000000.jsonl").write_text(
        ext_line
    )

    after = g.fingerprint()
    assert g.diff(before, after) == []


def test_test_owned_log_and_mirror_writes_are_flagged(tmp_path, monkeypatch):
    fake_real_bth = tmp_path / "real_bth"
    (fake_real_bth / "log" / "unaffiliated").mkdir(parents=True)
    (fake_real_bth / "log-mirror" / "proj-1").mkdir(parents=True)
    monkeypatch.setattr(g, "REAL_BTH", fake_real_bth)

    before = g.fingerprint()

    # 1. File carrying a session PID
    own_pid = os.getpid()
    (fake_real_bth / "log" / "unaffiliated" / f"testhost.{own_pid}.1700000000000000000.jsonl").write_text(
        '{"main_root": "/somewhere/else"}\n'
    )
    # 2. File with external-looking PID whose JSONL payload names a session temp root
    test_proj = tmp_path / "leaked_proj"
    test_proj.mkdir()
    g.register_session_root(tmp_path)
    (fake_real_bth / "log-mirror" / "proj-1" / "otherhost.888881.1700000000000000000.jsonl").write_text(
        f'{{"main_root": "{test_proj}"}}\n'
    )
    # 3. Unrecognized non-segment file
    (fake_real_bth / "log" / "stray.txt").write_text("oops\n")

    after = g.fingerprint()
    findings = g.diff(before, after)
    assert len(findings) == 3
    assert any(f"test PID {own_pid}" in f for f in findings)
    assert any(str(test_proj) in f for f in findings)
    assert any("unrecognized non-segment file" in f for f in findings)


def test_projects_toml_concurrent_external_vs_test_leak(tmp_path, monkeypatch):
    fake_real_bth = tmp_path / "real_bth"
    fake_real_bth.mkdir(parents=True)
    proj_toml = fake_real_bth / "projects.toml"
    proj_toml.write_text('[[roots]]\nroot = "/home/user/proj_a"\nproject_id = "id-a"\n')
    monkeypatch.setattr(g, "REAL_BTH", fake_real_bth)
    monkeypatch.setattr(g, "exposed_subprocesses", list)

    before = g.fingerprint()

    # Concurrent real `bth` adds an external project root -> must NOT false-positive.
    proj_toml.write_text(
        '[[roots]]\nroot = "/home/user/proj_a"\nproject_id = "id-a"\n'
        '[[roots]]\nroot = "/home/user/proj_b"\nproject_id = "id-b"\n'
    )
    assert g.diff(before, g.fingerprint()) == []

    # Test leak adds a root under this session's tmp_path -> MUST fail.
    leaked_root = tmp_path / "leaked_ws"
    leaked_root.mkdir()
    g.register_session_root(tmp_path)
    proj_toml.write_text(
        '[[roots]]\nroot = "/home/user/proj_a"\nproject_id = "id-a"\n'
        f'[[roots]]\nroot = "{leaked_root}"\nproject_id = "id-leak"\n'
    )
    findings = g.diff(before, g.fingerprint())
    assert len(findings) == 1
    assert "projects.toml: modified" in findings[0]
    assert str(leaked_root) in findings[0]


def test_index_db_concurrent_external_vs_test_leak(tmp_path, monkeypatch):
    import duckdb

    fake_real_bth = tmp_path / "real_bth"
    cat_dir = fake_real_bth / "catalog"
    cat_dir.mkdir(parents=True)
    idx = cat_dir / "index.db"

    con = duckdb.connect(str(idx))
    con.execute("CREATE TABLE events (writer VARCHAR, main_root VARCHAR, worktree_root VARCHAR)")
    con.execute("CREATE TABLE ingest_watermarks (path VARCHAR)")
    con.execute("INSERT INTO events VALUES ('realhost.999992.1700', '/home/user/real', '/home/user/real')")
    con.close()

    monkeypatch.setattr(g, "REAL_BTH", fake_real_bth)
    monkeypatch.setattr(g, "exposed_subprocesses", list)
    before = g.fingerprint()

    # Concurrent real `bth` updates index.db with external writer/root -> ignored.
    con = duckdb.connect(str(idx))
    con.execute("INSERT INTO events VALUES ('realhost.999993.1701', '/home/user/real2', '/home/user/real2')")
    con.close()
    assert g.diff(before, g.fingerprint()) == []

    # Test leak inserts an event with this session's PID -> flagged.
    own_pid = os.getpid()
    con = duckdb.connect(str(idx))
    con.execute(f"INSERT INTO events VALUES ('testhost.{own_pid}.1702', '/other', '/other')")
    con.close()
    findings = g.diff(before, g.fingerprint())
    assert len(findings) == 1
    assert f"test writer PID {own_pid}" in findings[0]


def test_cutover_json_still_strictly_flagged(tmp_path, monkeypatch):
    fake_real_bth = tmp_path / "real_bth"
    cat_dir = fake_real_bth / "catalog"
    cat_dir.mkdir(parents=True)
    monkeypatch.setattr(g, "REAL_BTH", fake_real_bth)

    before = g.fingerprint()
    (cat_dir / "cutover.json").write_text('{"attempt": "a1"}\n')
    findings = g.diff(before, g.fingerprint())
    assert findings == [f"{fake_real_bth / 'catalog/cutover.json'}: created"]


def test_popen_tracking_records_child_pid_and_env_exposure(tmp_path):
    import subprocess
    import sys

    g.install()
    before_exposed = list(g._exposed_subprocesses)
    try:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        assert proc.returncode == 0
        assert proc.pid in g.session_pids()

        # Verify env exposure detection ignores git and flags non-git with unset HOME
        assert g._is_git_command(["git", "status"]) is True
        assert g._is_git_command(["bth", "run"]) is False
        assert g._detect_env_exposure({}) == "HOME unset"
        assert g._detect_env_exposure({"HOME": str(g.REAL_HOME)}) == f"HOME={g.REAL_HOME}"
        assert (
            g._detect_env_exposure({"HOME": str(tmp_path), "BTH_CATALOG_DIR": str(g.REAL_BTH / "catalog")})
            == f"BTH_CATALOG_DIR={g.REAL_BTH / 'catalog'}"
        )
        assert g._detect_env_exposure({"HOME": str(tmp_path)}) is None
    finally:
        with g._state_lock:
            g._exposed_subprocesses[:] = before_exposed
