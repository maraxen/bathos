from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bathos.config import ProjectConfig
from bathos.sync import SyncResult, cluster_log_remote_dirs, pull_cluster_log, sync_catalog


@pytest.fixture(autouse=True)
def _no_ssh_mkdir(monkeypatch):
    monkeypatch.setattr("bathos.sync.ensure_remote_catalog_dir", lambda *_a, **_k: None)


def _make_mock_popen(returncode=0, stderr_output="", stdout_output=""):
    """Create a mock Popen object."""
    mock_proc = MagicMock()
    mock_proc.returncode = returncode
    mock_proc.wait.return_value = returncode
    mock_proc.poll.return_value = None  # Process is still running
    mock_proc.stderr = StringIO(stderr_output)
    mock_proc.stdout = StringIO(stdout_output)
    return mock_proc


def test_sync_result_dataclass():
    """SyncResult is properly structured."""
    result = SyncResult(transferred=42, duration_s=3.14, remote="engaging")
    assert result.transferred == 42
    assert result.duration_s == 3.14
    assert result.remote == "engaging"


def test_sync_constructs_correct_rsync_command_push(tmp_path: Path):
    """sync_catalog constructs correct rsync command for push."""
    config = ProjectConfig(
        slug="test",
        root=Path("/home/user/test"),
        remotes={"engaging": {"host": "engaging", "remote_root": "~/projects/test"}},
    )
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "runs").mkdir()

    with patch("bathos.sync.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _make_mock_popen()

        sync_catalog("engaging", config, catalog_dir, pull=False)

        cmds = [c[0][0] for c in mock_popen.call_args_list]
        cmd = next(c for c in cmds if any("runs/" in str(a) for a in c))

        # Should be rsync command
        assert cmd[0] == "rsync"
        # Should have -az flags
        assert "-az" in cmd
        # Should pass SSH options for fast failure: ConnectTimeout + BatchMode
        assert any("ConnectTimeout" in str(a) for a in cmd)
        assert any("BatchMode=yes" in str(a) for a in cmd)
        # Should have --ignore-existing flag
        assert "--ignore-existing" in cmd
        # Should have --info=progress2 for streaming
        assert "--info=progress2" in cmd
        # Should reference runs directories
        assert any("runs/" in str(arg) for arg in cmd)


def test_sync_passes_timeout_to_subprocess(tmp_path: Path):
    """sync_catalog has watchdog timeout to prevent hanging forever."""
    config = ProjectConfig(
        slug="test",
        root=Path("/home/user/test"),
        remotes={"engaging": {"host": "engaging", "remote_root": "~/projects/test"}},
    )
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "runs").mkdir()

    with patch("bathos.sync.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _make_mock_popen()

        sync_catalog("engaging", config, catalog_dir, pull=False)

        # Popen was called (which starts the process with watchdog timeout)
        assert mock_popen.called


def test_sync_raises_on_timeout(tmp_path: Path):
    """sync_catalog raises RuntimeError with clear message when rsync times out."""
    config = ProjectConfig(
        slug="test",
        root=Path("/home/user/test"),
        remotes={"engaging": {"host": "engaging", "remote_root": "~/projects/test"}},
    )
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "runs").mkdir()

    with patch("bathos.sync.subprocess.Popen") as mock_popen:
        mock_proc = _make_mock_popen()
        # Make wait() raise TimeoutExpired only on the main thread's call
        import subprocess as _subprocess

        call_count = [0]

        def wait_side_effect(*args, **kwargs):  # noqa: ARG001 - mock side effect callback signature
            call_count[0] += 1
            # First call is from watchdog thread, second is from main
            if call_count[0] > 1:
                raise _subprocess.TimeoutExpired(cmd=["rsync"], timeout=120)
            # Watchdog call returns normally
            return 0

        mock_proc.wait.side_effect = wait_side_effect
        mock_popen.return_value = mock_proc

        with pytest.raises(RuntimeError, match="timed out"):
            sync_catalog("engaging", config, catalog_dir, pull=False)


def test_sync_pull_reverses_direction(tmp_path: Path):
    """sync_catalog pulls from remote when pull=True."""
    config = ProjectConfig(
        slug="test",
        root=Path("/home/user/test"),
        remotes={"engaging": {"host": "engaging", "remote_root": "~/projects/test"}},
    )
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "runs").mkdir()

    with patch("bathos.sync.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _make_mock_popen()

        sync_catalog("engaging", config, catalog_dir, pull=True)

        call_args = mock_popen.call_args
        cmd = call_args[0][0]

        # Pull should have remote as source
        assert any("engaging:" in str(arg) for arg in cmd)


def test_sync_errors_on_unknown_remote(tmp_path: Path):
    """sync_catalog raises clear error when remote not in config."""
    config = ProjectConfig(
        slug="test",
        root=Path("/home/user/test"),
        remotes={"engaging": {"host": "engaging", "remote_root": "~/projects/test"}},
    )
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "runs").mkdir()

    with pytest.raises(ValueError, match="Remote 'unknown' not in config"):
        sync_catalog("unknown", config, catalog_dir, pull=False)


def test_sync_uses_ignore_existing(tmp_path: Path):
    """sync_catalog includes --ignore-existing flag."""
    config = ProjectConfig(
        slug="test",
        root=Path("/home/user/test"),
        remotes={"engaging": {"host": "engaging", "remote_root": "~/projects/test"}},
    )
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "runs").mkdir()

    with patch("bathos.sync.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _make_mock_popen()

        sync_catalog("engaging", config, catalog_dir, pull=False)

        cmds = [c[0][0] for c in mock_popen.call_args_list]
        runs_cmd = next(c for c in cmds if "--ignore-existing" in c)
        assert any("runs/" in str(a) for a in runs_cmd)


def test_sync_returns_sync_result(tmp_path: Path):
    """sync_catalog returns SyncResult with transferred count."""
    config = ProjectConfig(
        slug="test",
        root=Path("/home/user/test"),
        remotes={"engaging": {"host": "engaging", "remote_root": "~/projects/test"}},
    )
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "runs").mkdir()

    with patch("bathos.sync.subprocess.Popen") as mock_popen:
        # Simulate rsync output with progress2 format
        stderr_output = "   1,234 100%    1.23MB/s    0:00:01 (xfr#1, to-chk=0/1)\n"
        stdout_output = "sent 1000 bytes  received 500 bytes"
        mock_popen.return_value = _make_mock_popen(
            stderr_output=stderr_output, stdout_output=stdout_output
        )

        result = sync_catalog("engaging", config, catalog_dir, pull=False)

        assert isinstance(result, SyncResult)
        assert result.remote == "engaging"
        assert isinstance(result.transferred, int)
        assert isinstance(result.duration_s, float)


def test_sync_transferred_count_uses_xfr_not_bytes(tmp_path: Path):
    """#1945: `transferred` must be rsync's own file count (the "xfr#N"
    counter in --info=progress2 output), not the last progress line's byte
    count -- conflating the two under-/over-reports and can show 0 even
    when files really did transfer."""
    config = ProjectConfig(
        slug="test",
        root=Path("/home/user/test"),
        remotes={"engaging": {"host": "engaging", "remote_root": "~/projects/test"}},
    )
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "runs").mkdir()

    # Three files transferred; the LAST progress2 line reports only 42 bytes
    # for the final (small) file, while the running xfr# counter is 3.
    stderr_output = (
        "  10,000 100%    1.00MB/s    0:00:01 (xfr#1, to-chk=2/3)\n"
        "   5,000 100%    1.00MB/s    0:00:01 (xfr#2, to-chk=1/3)\n"
        "      42 100%    1.00MB/s    0:00:01 (xfr#3, to-chk=0/3)\n"
    )
    with patch("bathos.sync.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _make_mock_popen(stderr_output=stderr_output)

        result = sync_catalog("engaging", config, catalog_dir, pull=False)

    assert result.transferred == 3


def test_sync_transferred_count_zero_when_nothing_to_transfer(tmp_path: Path):
    """A no-op --ignore-existing sync reports xfr#0, which must surface as
    transferred=0 -- not be confused with the "couldn't parse" fallback."""
    config = ProjectConfig(
        slug="test",
        root=Path("/home/user/test"),
        remotes={"engaging": {"host": "engaging", "remote_root": "~/projects/test"}},
    )
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "runs").mkdir()

    stderr_output = "       0 100%    0.00kB/s    0:00:00 (xfr#0, to-chk=0/1)\n"
    with patch("bathos.sync.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _make_mock_popen(stderr_output=stderr_output)

        result = sync_catalog("engaging", config, catalog_dir, pull=False)

    assert result.transferred == 0


def test_sync_error_on_rsync_failure(tmp_path: Path):
    """sync_catalog raises error when rsync fails."""
    config = ProjectConfig(
        slug="test",
        root=Path("/home/user/test"),
        remotes={"engaging": {"host": "engaging", "remote_root": "~/projects/test"}},
    )
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "runs").mkdir()

    with patch("bathos.sync.subprocess.Popen") as mock_popen:
        mock_popen.return_value = _make_mock_popen(returncode=23)

        with pytest.raises(RuntimeError, match="rsync failed"):
            sync_catalog("engaging", config, catalog_dir, pull=False)


# ---------------------------------------------------------------------------
# Cluster log pull (spec "Cluster", delivery step 4 wave d, item 1):
# `pull_cluster_log` -- log/fallback/mirror, via the myxcel wrapper only.
# ---------------------------------------------------------------------------


def _cluster_config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        slug="testproj",
        root=tmp_path,
        remotes={"engaging": {"host": "engaging", "remote_root": "~/projects/testproj"}},
    )


def test_cluster_log_remote_dirs_layout(tmp_path: Path):
    log_dir, fallback_dir, mirror_dir = cluster_log_remote_dirs(tmp_path, "engaging")
    assert log_dir == tmp_path / ".bth" / "log" / "remote" / "engaging" / "log"
    assert fallback_dir == tmp_path / ".bth" / "log" / "remote" / "engaging" / "fallback"
    assert mirror_dir == tmp_path / ".bth" / "log" / "remote" / "engaging" / "mirror"


def test_pull_cluster_log_pulls_three_subpaths_via_myxcel_with_project_id(tmp_path: Path):
    """All three sub-pulls go through `bathos.cluster.pull_path` (never
    rsync directly), with the project-id mirror path when one is present."""
    config = _cluster_config(tmp_path)

    with patch("bathos.cluster.pull_path") as mock_pull:
        pull_cluster_log("engaging", config, tmp_path, "proj-id-123")

    assert mock_pull.call_count == 3
    calls = {c.args[0:2] for c in mock_pull.call_args_list}
    assert ("engaging", "~/projects/testproj/.bth/log/") in calls
    assert ("engaging", "~/.bth/log/fallback/testproj/") in calls
    assert ("engaging", "~/.bth/log-mirror/proj-id-123/") in calls

    log_dir, fallback_dir, mirror_dir = cluster_log_remote_dirs(tmp_path, "engaging")
    assert log_dir.is_dir()
    assert fallback_dir.is_dir()
    assert mirror_dir.is_dir()


def test_pull_cluster_log_uses_null_slug_mirror_when_no_project_id(tmp_path: Path):
    """With no project id (D7's null-id fallback), the mirror source is
    `~/.bth/log-mirror/_null/<slug>/`, matching the writer's own fallback."""
    config = _cluster_config(tmp_path)

    with patch("bathos.cluster.pull_path") as mock_pull:
        pull_cluster_log("engaging", config, tmp_path, None)

    calls = {c.args[0:2] for c in mock_pull.call_args_list}
    assert ("engaging", "~/.bth/log-mirror/_null/testproj/") in calls


def test_pull_cluster_log_is_best_effort_per_subpath(tmp_path: Path):
    """One sub-path failing (e.g. a fallback directory that was never
    written on the remote) must not prevent the other two from being
    attempted."""
    config = _cluster_config(tmp_path)

    def _side_effect(_remote, remote_path, _dest):
        if "fallback" in remote_path:
            raise RuntimeError("no such directory")

    with patch("bathos.cluster.pull_path", side_effect=_side_effect) as mock_pull:
        pull_cluster_log("engaging", config, tmp_path, "proj-id-123")

    assert mock_pull.call_count == 3


def test_pull_cluster_log_raises_for_unconfigured_remote(tmp_path: Path):
    config = ProjectConfig(slug="testproj", root=tmp_path, remotes={})
    with pytest.raises(ValueError, match="not in config"):
        pull_cluster_log("engaging", config, tmp_path, "proj-id-123")
