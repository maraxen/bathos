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


def test_pull_path_refuses_without_calling_subprocess():
    """`myxcel pull` has no `--dest` flag and no way to target an arbitrary
    remote path -- `myxcel pull --help` is `myxcel pull [OPTIONS] {remote}
    {project}` (a registered project name, not a path). pull_path must
    refuse loudly instead of shelling out to a non-existent flag (debt found
    260927: the old implementation did exactly that and failed on every real
    call, masked because this seam is always mocked in tests exercising its
    callers)."""
    with (
        patch("bathos.cluster.subprocess.run") as mock_run,
        pytest.raises(MyxcelCapabilityGapError),
    ):
        pull_path("engaging", "~/projects/testproj/.bth/log/", "/local/dest/log/")

    mock_run.assert_not_called()


def test_pull_path_error_message_is_actionable():
    """The error names the missing capability and the exact call that would
    have been attempted, so a caller/log-reader knows what's broken and
    where to file the myxcel feature request -- not just that *something*
    failed."""
    with pytest.raises(MyxcelCapabilityGapError) as excinfo:
        pull_path("engaging", "~/some/path/", "/local/dest/")

    message = str(excinfo.value)
    assert "engaging" in message
    assert "~/some/path/" in message
    assert "/local/dest/" in message
    assert "--dest" in message
    assert "myxcel" in message


def test_myxcel_capability_gap_error_is_a_runtime_error():
    """Best-effort callers (e.g. `bathos.sync.pull_cluster_log`) catch bare
    `Exception`/`RuntimeError` per sub-path; this must not be a new
    exception hierarchy that slips past that."""
    assert issubclass(MyxcelCapabilityGapError, RuntimeError)
