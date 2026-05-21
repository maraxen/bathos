from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from bathos.cli import app
from bathos.config import ProjectConfig


# Test fixtures and constants

MINIMAL_BTH_TOML = """
[project]
slug = "testproject"
root = "/home/test/projects/testproject"
catalog_dir = "~/.bth/catalog"
"""

BTH_TOML_WITH_REMOTE = """
[project]
slug = "testproject"
root = "/home/test/projects/testproject"
catalog_dir = "~/.bth/catalog"

[remotes.engaging]
host = "engaging"
remote_root = "~/projects/bathos"
"""

BTH_TOML_WITH_COMMENT = """
[project]
slug = "testproject"
root = "/home/test/projects/testproject"
catalog_dir = "~/.bth/catalog"
# This is a comment that must survive round-trip

[remotes.engaging]
host = "engaging"
remote_root = "~/projects/bathos"
"""

BTH_TOML_WITH_MULTIPLE_REMOTES = """
[project]
slug = "testproject"
root = "/home/test/projects/testproject"
catalog_dir = "~/.bth/catalog"

[remotes.engaging]
host = "engaging"
remote_root = "~/projects/bathos"

[remotes.psc]
host = "psc"
remote_root = "~/projects/bathos"

[remotes.avi]
host = "avi"
remote_root = "~/code/bathos"
"""


@pytest.fixture
def tmp_config(tmp_path):
    """Create a temporary .bth.toml file with minimal config."""
    config_path = tmp_path / ".bth.toml"
    config_path.write_text(MINIMAL_BTH_TOML)
    return config_path


@pytest.fixture
def tmp_config_with_remote(tmp_path):
    """Create a temporary .bth.toml file with one remote."""
    config_path = tmp_path / ".bth.toml"
    config_path.write_text(BTH_TOML_WITH_REMOTE)
    return config_path


@pytest.fixture
def tmp_config_with_multiple_remotes(tmp_path):
    """Create a temporary .bth.toml file with multiple remotes."""
    config_path = tmp_path / ".bth.toml"
    config_path.write_text(BTH_TOML_WITH_MULTIPLE_REMOTES)
    return config_path


@pytest.fixture
def cli_runner():
    """Create a CLI test runner."""
    return CliRunner()


# Tests for list_remotes (pure function)

class TestListRemotes:
    """Tests for bathos.remote.list_remotes pure function."""

    def test_list_remotes_empty_config(self, tmp_config):
        """Empty config returns empty list."""
        from bathos.remote import list_remotes
        from bathos.config import load_project_config

        config = load_project_config(tmp_config)
        remotes = list_remotes(config)

        assert remotes == []

    def test_list_remotes_single_remote(self, tmp_config_with_remote):
        """Single remote returns list with one tuple."""
        from bathos.remote import list_remotes
        from bathos.config import load_project_config

        config = load_project_config(tmp_config_with_remote)
        remotes = list_remotes(config)

        assert len(remotes) == 1
        assert remotes[0] == ("engaging", "engaging", "~/projects/bathos")

    def test_list_remotes_multiple_sorted(self, tmp_config_with_multiple_remotes):
        """Multiple remotes returned sorted alphabetically by name."""
        from bathos.remote import list_remotes
        from bathos.config import load_project_config

        config = load_project_config(tmp_config_with_multiple_remotes)
        remotes = list_remotes(config)

        assert len(remotes) == 3
        # Should be sorted: avi, engaging, psc
        assert remotes[0][0] == "avi"
        assert remotes[1][0] == "engaging"
        assert remotes[2][0] == "psc"

        # Verify tuple structure
        assert remotes[0] == ("avi", "avi", "~/code/bathos")
        assert remotes[1] == ("engaging", "engaging", "~/projects/bathos")
        assert remotes[2] == ("psc", "psc", "~/projects/bathos")


# Tests for add_remote

class TestAddRemote:
    """Tests for bathos.remote.add_remote."""

    def test_add_remote_success(self, tmp_config):
        """Successfully adds remote to config file."""
        from bathos.remote import add_remote, list_remotes
        from bathos.config import load_project_config

        add_remote(tmp_config, "engaging", "engaging", "~/projects/bathos")

        # Reload and verify
        config = load_project_config(tmp_config)
        remotes = list_remotes(config)

        assert len(remotes) == 1
        assert remotes[0] == ("engaging", "engaging", "~/projects/bathos")

    def test_add_remote_reads_back_correctly(self, tmp_config):
        """Added remote reads back with correct values."""
        from bathos.remote import add_remote

        add_remote(tmp_config, "testhost", "user@example.com", "~/my/path")

        # Parse directly from TOML
        import tomllib
        with open(tmp_config, "rb") as f:
            data = tomllib.load(f)

        assert data["remotes"]["testhost"]["host"] == "user@example.com"
        assert data["remotes"]["testhost"]["remote_root"] == "~/my/path"

    def test_add_remote_preserves_comments(self, tmp_path):
        """Round-trip preserves existing comments in TOML."""
        from bathos.remote import add_remote, list_remotes
        from bathos.config import load_project_config

        config_path = tmp_path / ".bth.toml"
        config_path.write_text(BTH_TOML_WITH_COMMENT)

        # Add a new remote
        add_remote(config_path, "psc", "psc", "~/projects/bathos")

        # Read back and verify comment still there
        content = config_path.read_text()
        assert "# This is a comment that must survive round-trip" in content

    def test_add_remote_raises_on_duplicate(self, tmp_config_with_remote):
        """Raises ValueError when remote name already exists."""
        from bathos.remote import add_remote

        with pytest.raises(ValueError, match="Remote 'engaging' already exists"):
            add_remote(tmp_config_with_remote, "engaging", "other.host", "~/path")

    def test_add_remote_raises_on_missing_file(self, tmp_path):
        """Raises FileNotFoundError if config_path doesn't exist."""
        from bathos.remote import add_remote

        missing_config = tmp_path / "nonexistent.toml"

        with pytest.raises(FileNotFoundError):
            add_remote(missing_config, "remote1", "host1", "~/path")

    def test_add_remote_no_tmp_file_after_success(self, tmp_config):
        """No .tmp file left after successful write (atomic write)."""
        from bathos.remote import add_remote

        add_remote(tmp_config, "engaging", "engaging", "~/projects/bathos")

        # Check no .tmp file exists
        tmp_file = tmp_config.with_suffix(".tmp")
        assert not tmp_file.exists()

    def test_add_remote_with_user_at_host(self, tmp_config):
        """Handles host containing @ (user@host format)."""
        from bathos.remote import add_remote

        # URL: user@host.com:~/path should split host=user@host.com, path=~/path
        add_remote(tmp_config, "remote1", "user@host.example.com", "~/path/to/data")

        import tomllib
        with open(tmp_config, "rb") as f:
            data = tomllib.load(f)

        assert data["remotes"]["remote1"]["host"] == "user@host.example.com"
        assert data["remotes"]["remote1"]["remote_root"] == "~/path/to/data"


# Tests for remove_remote

class TestRemoveRemote:
    """Tests for bathos.remote.remove_remote."""

    def test_remove_remote_success(self, tmp_config_with_remote):
        """Successfully removes remote from config."""
        from bathos.remote import remove_remote, list_remotes
        from bathos.config import load_project_config

        remove_remote(tmp_config_with_remote, "engaging")

        # Verify removed
        config = load_project_config(tmp_config_with_remote)
        remotes = list_remotes(config)

        assert len(remotes) == 0

    def test_remove_remote_deletes_section_when_last(self, tmp_config_with_remote):
        """Removes [remotes] section entirely when last remote deleted."""
        from bathos.remote import remove_remote

        remove_remote(tmp_config_with_remote, "engaging")

        # Verify [remotes] key removed from file
        import tomllib
        with open(tmp_config_with_remote, "rb") as f:
            data = tomllib.load(f)

        assert "remotes" not in data

    def test_remove_remote_preserves_other_remotes(self, tmp_config_with_multiple_remotes):
        """Removing one remote preserves others."""
        from bathos.remote import remove_remote, list_remotes
        from bathos.config import load_project_config

        remove_remote(tmp_config_with_multiple_remotes, "engaging")

        config = load_project_config(tmp_config_with_multiple_remotes)
        remotes = list_remotes(config)

        assert len(remotes) == 2
        names = [r[0] for r in remotes]
        assert "avi" in names
        assert "psc" in names
        assert "engaging" not in names

    def test_remove_remote_raises_on_missing_name(self, tmp_config_with_remote):
        """Raises ValueError when remote name not found."""
        from bathos.remote import remove_remote

        with pytest.raises(ValueError, match="Remote 'nosuchhost' not found"):
            remove_remote(tmp_config_with_remote, "nosuchhost")

    def test_remove_remote_raises_on_missing_file(self, tmp_path):
        """Raises FileNotFoundError if config_path doesn't exist."""
        from bathos.remote import remove_remote

        missing_config = tmp_path / "nonexistent.toml"

        with pytest.raises(FileNotFoundError):
            remove_remote(missing_config, "remote1")


# Tests for test_remote

class TestRemoteTest:
    """Tests for bathos.remote.test_remote."""

    def test_test_remote_success(self, tmp_config_with_remote):
        """SSH success returns TestResult with success=True and latency_ms."""
        from bathos.remote import test_remote
        from bathos.config import load_project_config

        config = load_project_config(tmp_config_with_remote)

        with patch("bathos.remote.subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "ok\n"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = test_remote(config, "engaging")

            assert result.success is True
            assert result.latency_ms is not None
            assert isinstance(result.latency_ms, float)
            assert result.error == ""

    def test_test_remote_failure(self, tmp_config_with_remote):
        """SSH failure returns TestResult with success=False and error."""
        from bathos.remote import test_remote
        from bathos.config import load_project_config

        config = load_project_config(tmp_config_with_remote)

        with patch("bathos.remote.subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 1
            mock_result.stdout = ""
            mock_result.stderr = "ssh: connect failed"
            mock_run.return_value = mock_result

            result = test_remote(config, "engaging")

            assert result.success is False
            assert result.latency_ms is None
            assert result.error == "ssh: connect failed"

    def test_test_remote_timeout(self, tmp_config_with_remote):
        """Timeout returns TestResult with appropriate error message."""
        from bathos.remote import test_remote
        from bathos.config import load_project_config

        config = load_project_config(tmp_config_with_remote)

        with patch("bathos.remote.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("cmd", 10)

            result = test_remote(config, "engaging")

            assert result.success is False
            assert result.latency_ms is None
            assert result.error == "Connection timed out after 10s"

    def test_test_remote_name_not_found(self, tmp_config_with_remote):
        """Raises ValueError when remote name not found."""
        from bathos.remote import test_remote
        from bathos.config import load_project_config

        config = load_project_config(tmp_config_with_remote)

        with pytest.raises(ValueError, match="Remote 'nosuchhost' not found"):
            test_remote(config, "nosuchhost")

    def test_test_remote_ssh_command_format(self, tmp_config_with_remote):
        """SSH command uses correct format with BatchMode and ConnectTimeout."""
        from bathos.remote import test_remote
        from bathos.config import load_project_config

        config = load_project_config(tmp_config_with_remote)

        with patch("bathos.remote.subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "ok\n"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            test_remote(config, "engaging")

            # Verify the SSH command
            call_args = mock_run.call_args
            assert call_args is not None
            cmd = call_args[0][0]
            assert cmd[0] == "ssh"
            assert "-o" in cmd
            assert "BatchMode=yes" in cmd
            assert "ConnectTimeout=5" in cmd
            assert "engaging" in cmd  # hostname
            assert "echo" in cmd
            assert "ok" in cmd

    def test_test_remote_stdout_empty_on_failure(self, tmp_config_with_remote):
        """Returns stderr if stdout is empty on failure."""
        from bathos.remote import test_remote
        from bathos.config import load_project_config

        config = load_project_config(tmp_config_with_remote)

        with patch("bathos.remote.subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 1
            mock_result.stdout = ""
            mock_result.stderr = "Host key verification failed"
            mock_run.return_value = mock_result

            result = test_remote(config, "engaging")

            assert result.error == "Host key verification failed"


# CLI Tests

class TestRemoteAddCLI:
    """Tests for bth remote add command."""

    def test_remote_add_success(self, tmp_path, cli_runner):
        """bth remote add succeeds and prints correct message."""
        config_path = tmp_path / ".bth.toml"
        config_path.write_text(MINIMAL_BTH_TOML)

        with patch("bathos.cli.find_project_config", return_value=config_path):
            result = cli_runner.invoke(app, ["remote", "add", "engaging", "engaging:~/projects/bathos"])

            assert result.exit_code == 0
            assert "Remote 'engaging' added (engaging:~/projects/bathos)" in result.stdout

    def test_remote_add_duplicate_fails(self, tmp_path, cli_runner):
        """bth remote add fails for duplicate name."""
        config_path = tmp_path / ".bth.toml"
        config_path.write_text(BTH_TOML_WITH_REMOTE)

        with patch("bathos.cli.find_project_config", return_value=config_path):
            result = cli_runner.invoke(app, ["remote", "add", "engaging", "other:~/path"])

            assert result.exit_code == 1
            assert "Remote 'engaging' already exists" in result.stdout

    def test_remote_add_invalid_url_format(self, tmp_path, cli_runner):
        """bth remote add fails for invalid URL (no colon)."""
        config_path = tmp_path / ".bth.toml"
        config_path.write_text(MINIMAL_BTH_TOML)

        with patch("bathos.cli.find_project_config", return_value=config_path):
            result = cli_runner.invoke(app, ["remote", "add", "badname", "nocolon"])

            assert result.exit_code == 1
            assert "Invalid URL 'nocolon': expected 'host:path' format" in result.stdout

    def test_remote_add_no_config_file(self, cli_runner):
        """bth remote add fails when no .bth.toml found."""
        with patch("bathos.cli.find_project_config", return_value=None):
            result = cli_runner.invoke(app, ["remote", "add", "remote1", "host:~/path"])

            assert result.exit_code == 1
            assert "No .bth.toml found" in result.stdout


class TestRemoteListCLI:
    """Tests for bth remote list command."""

    def test_remote_list_with_remotes(self, tmp_path, cli_runner):
        """bth remote list shows table with configured remotes."""
        config_path = tmp_path / ".bth.toml"
        config_path.write_text(BTH_TOML_WITH_REMOTE)

        with patch("bathos.cli.find_project_config", return_value=config_path):
            result = cli_runner.invoke(app, ["remote", "list"])

            assert result.exit_code == 0
            assert "NAME" in result.stdout
            assert "HOST:PATH" in result.stdout
            assert "engaging" in result.stdout
            assert "engaging:~/projects/bathos" in result.stdout

    def test_remote_list_empty(self, tmp_path, cli_runner):
        """bth remote list shows message when no remotes configured."""
        config_path = tmp_path / ".bth.toml"
        config_path.write_text(MINIMAL_BTH_TOML)

        with patch("bathos.cli.find_project_config", return_value=config_path):
            result = cli_runner.invoke(app, ["remote", "list"])

            assert result.exit_code == 0
            assert "No remotes configured" in result.stdout

    def test_remote_list_no_config_file(self, cli_runner):
        """bth remote list fails when no .bth.toml found."""
        with patch("bathos.cli.find_project_config", return_value=None):
            result = cli_runner.invoke(app, ["remote", "list"])

            assert result.exit_code == 1
            assert "No .bth.toml found" in result.stdout

    def test_remote_list_multiple_remotes_sorted(self, tmp_path, cli_runner):
        """bth remote list shows multiple remotes in sorted order."""
        config_path = tmp_path / ".bth.toml"
        config_path.write_text(BTH_TOML_WITH_MULTIPLE_REMOTES)

        with patch("bathos.cli.find_project_config", return_value=config_path):
            result = cli_runner.invoke(app, ["remote", "list"])

            assert result.exit_code == 0
            # Check that remotes appear in sorted order
            lines = result.stdout.split("\n")
            # Find the line indices for each remote
            avi_idx = next(i for i, line in enumerate(lines) if "avi" in line and "avi" in line)
            eng_idx = next(i for i, line in enumerate(lines) if "engaging" in line)
            psc_idx = next(i for i, line in enumerate(lines) if "psc" in line)

            assert avi_idx < eng_idx < psc_idx


class TestRemoveRemoteCLI:
    """Tests for bth remote remove command."""

    def test_remote_remove_success(self, tmp_path, cli_runner):
        """bth remote remove succeeds and prints message."""
        config_path = tmp_path / ".bth.toml"
        config_path.write_text(BTH_TOML_WITH_REMOTE)

        with patch("bathos.cli.find_project_config", return_value=config_path):
            result = cli_runner.invoke(app, ["remote", "remove", "engaging"])

            assert result.exit_code == 0
            assert "Remote 'engaging' removed" in result.stdout

    def test_remote_remove_not_found(self, tmp_path, cli_runner):
        """bth remote remove fails when remote not found."""
        config_path = tmp_path / ".bth.toml"
        config_path.write_text(MINIMAL_BTH_TOML)

        with patch("bathos.cli.find_project_config", return_value=config_path):
            result = cli_runner.invoke(app, ["remote", "remove", "nosuchhost"])

            assert result.exit_code == 1
            assert "Remote 'nosuchhost' not found" in result.stdout

    def test_remote_remove_no_config_file(self, cli_runner):
        """bth remote remove fails when no .bth.toml found."""
        with patch("bathos.cli.find_project_config", return_value=None):
            result = cli_runner.invoke(app, ["remote", "remove", "remote1"])

            assert result.exit_code == 1
            assert "No .bth.toml found" in result.stdout


class TestRemoteTestCLI:
    """Tests for bth remote test command."""

    def test_remote_test_success(self, tmp_path, cli_runner):
        """bth remote test shows success message with latency."""
        config_path = tmp_path / ".bth.toml"
        config_path.write_text(BTH_TOML_WITH_REMOTE)

        with patch("bathos.cli.find_project_config", return_value=config_path):
            with patch("bathos.remote.subprocess.run") as mock_run:
                mock_result = MagicMock()
                mock_result.returncode = 0
                mock_result.stdout = "ok\n"
                mock_result.stderr = ""
                mock_run.return_value = mock_result

                result = cli_runner.invoke(app, ["remote", "test", "engaging"])

                assert result.exit_code == 0
                assert "engaging: ok" in result.stdout
                assert "ms" in result.stdout

    def test_remote_test_failure(self, tmp_path, cli_runner):
        """bth remote test shows failure message with error."""
        config_path = tmp_path / ".bth.toml"
        config_path.write_text(BTH_TOML_WITH_REMOTE)

        with patch("bathos.cli.find_project_config", return_value=config_path):
            with patch("bathos.remote.subprocess.run") as mock_run:
                mock_result = MagicMock()
                mock_result.returncode = 1
                mock_result.stdout = ""
                mock_result.stderr = "ssh: could not resolve hostname"
                mock_run.return_value = mock_result

                result = cli_runner.invoke(app, ["remote", "test", "engaging"])

                assert result.exit_code == 1
                assert "engaging: unreachable" in result.stdout

    def test_remote_test_not_found(self, tmp_path, cli_runner):
        """bth remote test fails when remote not found."""
        config_path = tmp_path / ".bth.toml"
        config_path.write_text(MINIMAL_BTH_TOML)

        with patch("bathos.cli.find_project_config", return_value=config_path):
            result = cli_runner.invoke(app, ["remote", "test", "nosuchhost"])

            assert result.exit_code == 1
            assert "Remote 'nosuchhost' not found" in result.stdout


class TestSyncAutoSelection:
    """Tests for bth sync with optional remote argument."""

    def test_sync_no_remote_auto_selects_single(self, tmp_path, cli_runner):
        """bth sync with no argument auto-selects single remote."""
        config_path = tmp_path / ".bth.toml"
        config_path.write_text(BTH_TOML_WITH_REMOTE)

        with patch("bathos.cli.find_project_config", return_value=config_path):
            with patch("bathos.cli.load_project_config") as mock_load:
                from bathos.config import load_project_config
                config = load_project_config(config_path)
                mock_load.return_value = config

                with patch("bathos.cli.sync_catalog") as mock_sync:
                    mock_sync.return_value = MagicMock(transferred=3, remote="engaging", duration_s=0.8, filtered=0)

                    result = cli_runner.invoke(app, ["sync"])

                    assert result.exit_code == 0
                    # Verify sync_catalog was called
                    mock_sync.assert_called_once()

    def test_sync_no_remotes_fails(self, tmp_path, cli_runner):
        """bth sync with zero remotes fails."""
        config_path = tmp_path / ".bth.toml"
        config_path.write_text(MINIMAL_BTH_TOML)

        with patch("bathos.cli.find_project_config", return_value=config_path):
            with patch("bathos.cli.load_project_config") as mock_load:
                from bathos.config import load_project_config
                config = load_project_config(config_path)
                mock_load.return_value = config

                result = cli_runner.invoke(app, ["sync"])

                assert result.exit_code == 1
                assert "No remotes configured" in result.stdout

    def test_sync_multiple_remotes_fails(self, tmp_path, cli_runner):
        """bth sync with multiple remotes fails without explicit selection."""
        config_path = tmp_path / ".bth.toml"
        config_path.write_text(BTH_TOML_WITH_MULTIPLE_REMOTES)

        with patch("bathos.cli.find_project_config", return_value=config_path):
            with patch("bathos.cli.load_project_config") as mock_load:
                from bathos.config import load_project_config
                config = load_project_config(config_path)
                mock_load.return_value = config

                result = cli_runner.invoke(app, ["sync"])

                assert result.exit_code == 1
                assert "Multiple remotes configured" in result.stdout
