from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from bathos.config import ProjectConfig
from bathos.sync import SyncResult, sync_catalog


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

    with patch("bathos.sync.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""

        sync_catalog("engaging", config, catalog_dir, pull=False)

        # Verify rsync was called with correct arguments
        call_args = mock_run.call_args
        cmd = call_args[0][0]

        # Should be rsync command
        assert cmd[0] == "rsync"
        # Should have -az flags (no -P: progress is useless when output is captured)
        assert "-az" in cmd
        # Should pass SSH options for fast failure: ConnectTimeout + BatchMode
        assert any("ConnectTimeout" in str(a) for a in cmd)
        assert any("BatchMode=yes" in str(a) for a in cmd)
        # Should have --ignore-existing flag
        assert "--ignore-existing" in cmd
        # Should reference runs directories
        assert any("runs/" in str(arg) for arg in cmd)


def test_sync_passes_timeout_to_subprocess(tmp_path: Path):
    """sync_catalog passes a timeout to subprocess.run so it cannot hang forever."""
    config = ProjectConfig(
        slug="test",
        root=Path("/home/user/test"),
        remotes={"engaging": {"host": "engaging", "remote_root": "~/projects/test"}},
    )
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "runs").mkdir()

    with patch("bathos.sync.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""

        sync_catalog("engaging", config, catalog_dir, pull=False)

        call_kwargs = mock_run.call_args[1]
        assert "timeout" in call_kwargs, "subprocess.run must have a timeout to prevent indefinite hangs"
        assert call_kwargs["timeout"] > 0


def test_sync_raises_on_timeout(tmp_path: Path):
    """sync_catalog raises RuntimeError with clear message when rsync times out."""
    import subprocess as _subprocess

    config = ProjectConfig(
        slug="test",
        root=Path("/home/user/test"),
        remotes={"engaging": {"host": "engaging", "remote_root": "~/projects/test"}},
    )
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "runs").mkdir()

    with patch("bathos.sync.subprocess.run") as mock_run:
        mock_run.side_effect = _subprocess.TimeoutExpired(cmd=["rsync"], timeout=120)

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

    with patch("bathos.sync.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""

        sync_catalog("engaging", config, catalog_dir, pull=True)

        call_args = mock_run.call_args
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

    with patch("bathos.sync.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""

        sync_catalog("engaging", config, catalog_dir, pull=False)

        call_args = mock_run.call_args
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

    with patch("bathos.sync.subprocess.run") as mock_run:
        # Simulate rsync output: "sent 1000 bytes  received 500 bytes  in 1.5 seconds"
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "sent 1000 bytes  received 500 bytes"

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

    with patch("bathos.sync.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 23

        with pytest.raises(RuntimeError, match="rsync failed"):
            sync_catalog("engaging", config, catalog_dir, pull=False)
