"""#2228: empty/invalid myxcel stdout must surface as RuntimeError.

`bth submit --wait` catches RuntimeError from job_wait; an uncaught
json.JSONDecodeError crashed the command with a traceback instead.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bathos.cluster import job_wait, submit_job


def test_job_wait_empty_stdout_raises_runtime_error():
    with patch("bathos.cluster.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with pytest.raises(RuntimeError, match="12345") as excinfo:
            job_wait("engaging", "12345")
    assert "empty" in str(excinfo.value).lower()
    assert "job_status" in str(excinfo.value)


def test_job_wait_invalid_json_raises_runtime_error():
    with patch("bathos.cluster.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="not json {", stderr="warn")
        with pytest.raises(RuntimeError, match="not json"):
            job_wait("engaging", "12345")


def test_job_wait_valid_json_still_parsed():
    with patch("bathos.cluster.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout='{"wait_result": "completed"}', stderr=""
        )
        assert job_wait("engaging", "12345") == {"wait_result": "completed"}


def test_submit_job_empty_stdout_raises_runtime_error():
    with patch("bathos.cluster.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="  \n", stderr="")
        with pytest.raises(RuntimeError, match="empty"):
            submit_job("engaging", "proj", "cpu", "echo hi")
