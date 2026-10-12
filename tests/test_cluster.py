"""Tests for bathos.cluster's myxcel subprocess wrappers.

Debt #1951/#1947: push_project/pull_project used to shell out to
`myxcel push-project`/`myxcel pull-project`, subcommands that do not exist
on the real `myxcel` CLI (which only has `push`/`pull`). These tests pin the
real subcommand names and the flags required for each to actually transfer
non-interactively.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bathos.cluster import MyxcelCapabilityGapError, pull_path, pull_project, push_project


def test_push_project_uses_real_myxcel_push_subcommand():
    """push_project must call `myxcel push`, not the nonexistent `push-project`."""
    with patch("bathos.cluster.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        push_project("engaging", "myproject")

    argv = mock_run.call_args[0][0]
    assert argv[0] == "myxcel"
    assert argv[1] == "push"
    assert "push-project" not in argv
    assert argv[2:4] == ["engaging", "myproject"]


def test_push_project_passes_apply_and_yes():
    """myxcel push is dry-run by default and prompts interactively unless told
    otherwise -- push_project must pass --apply (to actually transfer) and
    --yes (to avoid blocking on stdin for the preflight confirm)."""
    with patch("bathos.cluster.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        push_project("engaging", "myproject")

    argv = mock_run.call_args[0][0]
    assert "--apply" in argv
    assert "--yes" in argv


def test_push_project_raises_on_failure():
    with patch("bathos.cluster.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr="push failed")
        with pytest.raises(RuntimeError, match="push failed"):
            push_project("engaging", "myproject")


def test_pull_project_uses_real_myxcel_pull_subcommand():
    """pull_project must call `myxcel pull`, not the nonexistent `pull-project`."""
    with patch("bathos.cluster.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        pull_project("engaging", "myproject")

    argv = mock_run.call_args[0][0]
    assert argv[0] == "myxcel"
    assert argv[1] == "pull"
    assert "pull-project" not in argv
    assert argv[2:4] == ["engaging", "myproject"]


def test_pull_project_raises_on_failure():
    with patch("bathos.cluster.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr="pull failed")
        with pytest.raises(RuntimeError, match="pull failed"):
            pull_project("engaging", "myproject")


def test_pull_path_argv_is_exact_contract():
    """pull_path shells out to exactly `myxcel pull-path <remote> <remote_path>
    --dest <local_dest>` (myxcel backlog #5880), with a bounded timeout."""
    with patch("bathos.cluster.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        pull_path("engaging", "~/.bth/log/fallback/proj/", "/local/dest/fallback/")

    args, kwargs = mock_run.call_args
    assert args[0] == [
        "myxcel",
        "pull-path",
        "engaging",
        "~/.bth/log/fallback/proj/",
        "--dest",
        "/local/dest/fallback/",
    ]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 900


def test_pull_path_returns_none_on_success():
    with patch("bathos.cluster.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        assert pull_path("engaging", "~/x/", "/local/x/") is None


def test_pull_path_no_such_command_raises_capability_gap():
    """An installed myxcel that predates `pull-path` prints typer's "No such
    command" on stderr; that must surface as MyxcelCapabilityGapError, and
    the message must point at the backlog item and the verification grep."""
    with patch("bathos.cluster.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=2, stderr="Error: No such command 'pull-path'.\n", stdout=""
        )
        with pytest.raises(MyxcelCapabilityGapError) as excinfo:
            pull_path("engaging", "~/x/", "/local/x/")

    message = str(excinfo.value)
    assert "#5880" in message
    assert "def pull_path" in message


def test_pull_path_other_failure_is_plain_runtime_error_not_gap():
    """Negative control: a real refusal/error (not a missing subcommand) must
    raise RuntimeError carrying stderr, and must NOT be classified as a
    capability gap."""
    with patch("bathos.cluster.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr="refused: dest missing", stdout="")
        with pytest.raises(RuntimeError, match="refused: dest missing") as excinfo:
            pull_path("engaging", "~/x/", "/local/x/")

    assert not isinstance(excinfo.value, MyxcelCapabilityGapError)


def test_pull_path_timeout_is_runtime_error_with_clear_message():
    import subprocess

    with patch("bathos.cluster.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="myxcel pull-path", timeout=900)
        with pytest.raises(RuntimeError, match="timed out after 900s") as excinfo:
            pull_path("engaging", "~/x/", "/local/x/")

    assert not isinstance(excinfo.value, MyxcelCapabilityGapError)


def test_myxcel_capability_gap_error_is_a_runtime_error():
    """Best-effort callers (e.g. `bathos.sync.pull_cluster_log`) catch bare
    `Exception`/`RuntimeError` per sub-path; this must not be a new
    exception hierarchy that slips past that."""
    assert issubclass(MyxcelCapabilityGapError, RuntimeError)
