from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from bathos.cli import app

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SUBMIT_RESULT = {
    "slurm_job_id": "12345",
    "script_path": "/tmp/x.sh",
    "preset_used": {},
    "job_name": "bth-submit",
}

_WAIT_SUCCESS = {"wait_result": "completed", "failure_class": "SUCCESS"}
_WAIT_FAILURE_OOM = {"wait_result": "completed", "failure_class": "OOM"}
_WAIT_TIMEOUT = {"wait_result": "timeout", "failure_class": ""}


def _write_project_toml(tmp_path: Path, remote: str = "engaging", preset: str = "gpu") -> Path:
    """Write a minimal .bth.toml with cluster config."""
    content = (
        '[project]\n'
        'slug = "myproject"\n'
        f'root = "{tmp_path}"\n'
        '\n'
        '[slurm]\n'
        f'remote = "{remote}"\n'
        f'preset = "{preset}"\n'
    )
    p = tmp_path / ".bth.toml"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def test_submit_happy_path_no_wait(tmp_path: Path, monkeypatch):
    """bth submit --no-wait exits 0 and prints the job-id line."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    _write_project_toml(tmp_path)

    with (
        patch("bathos.cluster.push_project") as mock_push,
        patch("bathos.cluster.submit_job", return_value=_SUBMIT_RESULT) as mock_submit,
    ):
        result = runner.invoke(app, ["submit", "--no-wait", "uv", "run", "python", "train.py"])

    assert result.exit_code == 0, result.output
    assert "Submitted 12345 on engaging using preset gpu" in result.output
    mock_push.assert_called_once()
    mock_submit.assert_called_once()


def test_submit_wait_job_success_exits_0(tmp_path: Path, monkeypatch):
    """bth submit --wait exits 0 when job completes successfully."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    _write_project_toml(tmp_path)

    with (
        patch("bathos.cluster.push_project"),
        patch("bathos.cluster.submit_job", return_value=_SUBMIT_RESULT),
        patch("bathos.cluster.job_wait", return_value=_WAIT_SUCCESS),
    ):
        result = runner.invoke(app, ["submit", "--wait", "uv", "run", "python", "train.py"])

    assert result.exit_code == 0, result.output


def test_submit_wait_job_failure_exits_1(tmp_path: Path, monkeypatch):
    """bth submit --wait exits 1 when job fails (OOM)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    _write_project_toml(tmp_path)

    with (
        patch("bathos.cluster.push_project"),
        patch("bathos.cluster.submit_job", return_value=_SUBMIT_RESULT),
        patch("bathos.cluster.job_wait", return_value=_WAIT_FAILURE_OOM),
    ):
        result = runner.invoke(app, ["submit", "--wait", "uv", "run", "python", "train.py"])

    assert result.exit_code == 1, result.output


def test_submit_wait_timeout_exits_2(tmp_path: Path, monkeypatch):
    """bth submit --wait exits 2 and warns when job times out."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    _write_project_toml(tmp_path)

    with (
        patch("bathos.cluster.push_project"),
        patch("bathos.cluster.submit_job", return_value=_SUBMIT_RESULT),
        patch("bathos.cluster.job_wait", return_value=_WAIT_TIMEOUT),
    ):
        result = runner.invoke(app, ["submit", "--wait", "uv", "run", "python", "train.py"])

    assert result.exit_code == 2, result.output
    assert "still running on engaging" in result.output


def test_then_pull_implies_wait_and_calls_pull(tmp_path: Path, monkeypatch):
    """--then-pull implies --wait and calls pull_project exactly once."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    _write_project_toml(tmp_path)

    with (
        patch("bathos.cluster.push_project"),
        patch("bathos.cluster.submit_job", return_value=_SUBMIT_RESULT),
        patch("bathos.cluster.job_wait", return_value=_WAIT_SUCCESS),
        patch("bathos.cluster.pull_project") as mock_pull,
    ):
        result = runner.invoke(app, ["submit", "--then-pull", "uv", "run", "python", "train.py"])

    assert result.exit_code == 0, result.output
    mock_pull.assert_called_once()


def test_then_sync_calls_pull_and_sync_catalog(tmp_path: Path, monkeypatch):
    """--then-sync implies --then-pull --wait; both pull_project and sync_catalog are called."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    _write_project_toml(tmp_path)

    fake_sync_result = MagicMock()
    fake_sync_result.filtered = 0
    fake_sync_result.transferred = 3
    fake_sync_result.remote = "engaging"
    fake_sync_result.duration_s = 0.5

    with (
        patch("bathos.cluster.push_project"),
        patch("bathos.cluster.submit_job", return_value=_SUBMIT_RESULT),
        patch("bathos.cluster.job_wait", return_value=_WAIT_SUCCESS),
        patch("bathos.cluster.pull_project") as mock_pull,
        patch("bathos.cli.sync_catalog", return_value=fake_sync_result) as mock_sync,
    ):
        result = runner.invoke(app, ["submit", "--then-sync", "uv", "run", "python", "train.py"])

    assert result.exit_code == 0, result.output
    mock_pull.assert_called_once()
    mock_sync.assert_called_once()


def test_no_push_first_skips_push(tmp_path: Path, monkeypatch):
    """--no-push-first skips push_project but still calls submit_job."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    _write_project_toml(tmp_path)

    with (
        patch("bathos.cluster.push_project") as mock_push,
        patch("bathos.cluster.submit_job", return_value=_SUBMIT_RESULT) as mock_submit,
    ):
        result = runner.invoke(
            app,
            ["submit", "--no-push-first", "--no-wait", "uv", "run", "python", "train.py"],
        )

    assert result.exit_code == 0, result.output
    mock_push.assert_not_called()
    mock_submit.assert_called_once()


def test_sidecar_cluster_preset_overrides_project_config(tmp_path: Path, monkeypatch):
    """Sidecar [cluster].preset beats project config preset."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    # Project config has preset = "gpu"
    _write_project_toml(tmp_path, preset="gpu")

    # Sidecar overrides with gpu-h200
    sidecar_path = tmp_path / "train.bth.toml"
    sidecar_path.write_text('[cluster]\npreset = "gpu-h200"\n')

    with (
        patch("bathos.cluster.push_project"),
        patch("bathos.cluster.submit_job", return_value=_SUBMIT_RESULT) as mock_submit,
    ):
        result = runner.invoke(
            app,
            [
                "submit",
                "--sidecar",
                str(sidecar_path),
                "--no-push-first",
                "--no-wait",
                "uv",
                "run",
                "python",
                "train.py",
            ],
        )

    assert result.exit_code == 0, result.output
    # submit_job should have been called with the sidecar's preset
    call_kwargs = mock_submit.call_args
    # positional: remote, project, preset, command
    preset_used = call_kwargs.args[2]
    assert preset_used == "gpu-h200", f"Expected gpu-h200, got {preset_used!r}"


def test_no_bth_toml_exits_1(tmp_path: Path, monkeypatch):
    """Without a .bth.toml, bth submit exits 1 with an error about bth init."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    # Deliberately do NOT create .bth.toml

    result = runner.invoke(app, ["submit", "--no-wait", "uv", "run", "python", "train.py"])

    assert result.exit_code == 1
    assert "bth init" in result.output


# ---------------------------------------------------------------------------
# Unit tests: resolve_cluster_config
# ---------------------------------------------------------------------------


def test_resolve_cluster_config_from_project_config():
    """Project config alone is sufficient when remote and preset are set."""
    from bathos.cluster import ClusterConfig, resolve_cluster_config
    from bathos.config import ProjectConfig

    config = ProjectConfig(
        slug="myproject",
        root=Path("/tmp"),
        slurm={"remote": "engaging", "preset": "gpu"},
    )
    result = resolve_cluster_config(config)
    assert result == ClusterConfig(remote="engaging", preset="gpu", project="myproject")


def test_resolve_cluster_config_sidecar_overrides_preset():
    """Sidecar [cluster].preset beats project config preset; remote comes from project."""
    from bathos.cluster import ClusterConfig, resolve_cluster_config
    from bathos.config import ProjectConfig

    config = ProjectConfig(
        slug="myproject",
        root=Path("/tmp"),
        slurm={"remote": "engaging", "preset": "gpu"},
    )
    sidecar_data = {"cluster": {"preset": "gpu-h200"}}
    result = resolve_cluster_config(config, sidecar_data=sidecar_data)
    assert result == ClusterConfig(remote="engaging", preset="gpu-h200", project="myproject")


def test_resolve_cluster_config_cli_flags_override_sidecar():
    """CLI flags beat sidecar and project config."""
    from bathos.cluster import ClusterConfig, resolve_cluster_config
    from bathos.config import ProjectConfig

    config = ProjectConfig(
        slug="myproject",
        root=Path("/tmp"),
        slurm={"remote": "engaging", "preset": "gpu"},
    )
    sidecar_data = {"cluster": {"preset": "gpu-h200"}}
    result = resolve_cluster_config(config, sidecar_data=sidecar_data, cli_preset="cpu")
    assert result.preset == "cpu"


def test_resolve_cluster_config_missing_remote_raises():
    """Missing remote (not set anywhere) raises ValueError."""
    from bathos.cluster import resolve_cluster_config
    from bathos.config import ProjectConfig

    config = ProjectConfig(
        slug="myproject",
        root=Path("/tmp"),
        slurm={"preset": "gpu"},  # no remote
    )
    with pytest.raises(ValueError, match="remote"):
        resolve_cluster_config(config)


def test_resolve_cluster_config_missing_preset_raises():
    """Missing preset (not set anywhere) raises ValueError."""
    from bathos.cluster import resolve_cluster_config
    from bathos.config import ProjectConfig

    config = ProjectConfig(
        slug="myproject",
        root=Path("/tmp"),
        slurm={"remote": "engaging"},  # no preset
    )
    with pytest.raises(ValueError, match="preset"):
        resolve_cluster_config(config)


def test_resolve_cluster_config_project_defaults_to_slug():
    """project field defaults to config.slug when not specified anywhere."""
    from bathos.cluster import resolve_cluster_config
    from bathos.config import ProjectConfig

    config = ProjectConfig(
        slug="my-unique-slug",
        root=Path("/tmp"),
        slurm={"remote": "engaging", "preset": "gpu"},
    )
    result = resolve_cluster_config(config)
    assert result.project == "my-unique-slug"
