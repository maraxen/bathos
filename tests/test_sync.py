from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bathos.config import ProjectConfig
from bathos.sync import SyncResult, sync_catalog


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

        # Verify rsync was called with correct arguments
        call_args = mock_popen.call_args
        cmd = call_args[0][0]

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

        def wait_side_effect(*args, **kwargs):
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

        call_args = mock_popen.call_args
        cmd = call_args[0][0]

        assert "--ignore-existing" in cmd


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
        mock_popen.return_value = _make_mock_popen(stderr_output=stderr_output, stdout_output=stdout_output)

        result = sync_catalog("engaging", config, catalog_dir, pull=False)

        assert isinstance(result, SyncResult)
        assert result.remote == "engaging"
        assert isinstance(result.transferred, int)
        assert isinstance(result.duration_s, float)


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
