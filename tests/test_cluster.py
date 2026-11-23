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


def test_pull_path_gap_detected_for_cyclopts_era_myxcel():
    """A cyclopts-era myxcel without pull-path rejects it as unused tokens,
    not "No such command"; that is still a capability gap, not a generic
    failure."""
    from unittest.mock import MagicMock, patch

    from bathos.cluster import MyxcelCapabilityGapError, pull_path

    stderr = (
        "╭─ Error ─────────╮\n"
        "│ Unused Tokens: ['pull-path', 'engaging', '~/.bth/log/', '--dest', '/x/']. │\n"
        "╰─────────────────╯\n"
    )
    with patch("bathos.cluster.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=2, stderr=stderr, stdout="")
        with pytest.raises(MyxcelCapabilityGapError):
            pull_path("engaging", "~/.bth/log/", "/x/")


def test_pull_path_unused_tokens_for_other_args_is_not_a_gap():
    """Negative control: an "Unused Tokens" error about some OTHER token (an
    argv bug on a myxcel that does have pull-path) must stay a RuntimeError,
    not be misread as an old install."""
    from unittest.mock import MagicMock, patch

    from bathos.cluster import MyxcelCapabilityGapError, pull_path

    with patch("bathos.cluster.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=2, stderr="Unused Tokens: ['--bogus'].\n", stdout=""
        )
        with pytest.raises(RuntimeError) as excinfo:
            pull_path("engaging", "~/.bth/log/", "/x/")
    assert not isinstance(excinfo.value, MyxcelCapabilityGapError)


def test_build_submit_command_forwards_portable_sidecar_to_bth_run(tmp_path):
    """#2243: bth submit --sidecar PATH must forward --sidecar to an inner bth run."""
    from bathos.cluster import build_submit_command

    repo = tmp_path / "repo"
    sc = repo / "scripts" / "experiments" / "exp.bth.toml"
    sc.parent.mkdir(parents=True)
    sc.write_text("[experiment]\nhypothesis = 'h'\n")

    cmd = build_submit_command(
        ["bth", "run", "--", "uv", "run", "python", "scripts/experiments/exp.py"],
        env_pairs=["BTH_LOG_MODE=1", "BTH_PROJECT_ID=pid1"],
        sidecar=str(sc),
        project_root=repo,
    )
    assert cmd == (
        "env BTH_LOG_MODE=1 BTH_PROJECT_ID=pid1 "
        "bth run --sidecar scripts/experiments/exp.bth.toml -- "
        "uv run python scripts/experiments/exp.py"
    )

    # Does not duplicate when inner bth run already specifies --sidecar or --no-sidecar
    cmd_dup = build_submit_command(
        ["bth", "run", "--sidecar", "other.bth.toml", "--", "python", "x.py"],
        sidecar=str(sc),
        project_root=repo,
    )
    assert cmd_dup == "bth run --sidecar other.bth.toml -- python x.py"

    cmd_nosidecar = build_submit_command(
        ["bth", "run", "--no-sidecar", "--", "python", "x.py"],
        sidecar=str(sc),
        project_root=repo,
    )
    assert cmd_nosidecar == "bth run --no-sidecar -- python x.py"


def test_bth_submit_cli_forwards_sidecar_to_bth_run(tmp_path, monkeypatch):
    """#2243: CLI integration test for `bth submit --sidecar ... -- bth run ...`."""
    from bathos.cli_cyclopts import app
    from tests._cyclopts_runner import CyclopticRunner

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "myproj"\nroot = "{tmp_path}"\n\n'
        '[slurm]\nremote = "engaging"\npreset = "gpu"\n'
    )
    sc = tmp_path / "configs" / "exp.bth.toml"
    sc.parent.mkdir(parents=True)
    sc.write_text(
        '[experiment]\nhypothesis = "h"\n'
        '[outcomes.pass]\ncondition = "x > 0"\ndecision = "go"\nreasoning = "r"\n'
        '[outcomes.fail]\ncondition = "x <= 0"\ndecision = "stop"\nreasoning = "r"\nis_residual = true\n'
        '[result_schema]\nx = "float"\n'
    )

    submit_ret = {"slurm_job_id": "555", "script_path": "/tmp/j.sh", "preset_used": {}, "job_name": "j"}
    with (
        patch("bathos.cluster.push_project"),
        patch("bathos.cluster.submit_job", return_value=submit_ret) as mock_submit,
    ):
        res = CyclopticRunner().invoke(
            app,
            [
                "submit",
                "--no-wait",
                "--sidecar",
                str(sc),
                "--",
                "bth",
                "run",
                "--",
                "uv",
                "run",
                "python",
                "train.py",
            ],
        )
    assert res.exit_code == 0, res.output
    submitted_cmd = mock_submit.call_args.args[3]
    assert "--sidecar configs/exp.bth.toml" in submitted_cmd


def test_bth_submit_cli_missing_sidecar_fails_before_push_or_submit(tmp_path, monkeypatch):
    """#2243 negative control: `bth submit --sidecar <missing>` exits non-zero before push/submit."""
    from bathos.cli_cyclopts import app
    from tests._cyclopts_runner import CyclopticRunner

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "myproj"\nroot = "{tmp_path}"\n\n'
        '[slurm]\nremote = "engaging"\npreset = "gpu"\n'
    )

    with (
        patch("bathos.cluster.push_project") as mock_push,
        patch("bathos.cluster.submit_job") as mock_submit,
    ):
        res = CyclopticRunner().invoke(
            app,
            [
                "submit",
                "--no-wait",
                "--sidecar",
                "configs/missing.bth.toml",
                "--",
                "bth",
                "run",
                "--",
                "python",
                "train.py",
            ],
        )
    assert res.exit_code != 0
    mock_push.assert_not_called()
    mock_submit.assert_not_called()
