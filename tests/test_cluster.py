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

from bathos.cluster import pull_project, push_project


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
