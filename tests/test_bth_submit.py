from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pyarrow.parquet as pq
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


# ---------------------------------------------------------------------------
# AC-3 and AC-4 — Reproduction prerequisite gate tests
# ---------------------------------------------------------------------------


def test_submit_ac3_hard_gate_validation_stage_prerequisite_not_found(tmp_path: Path, monkeypatch):
    """AC-3: bth submit exits 1 when validation stage missing reproduction prerequisite."""
    monkeypatch.chdir(tmp_path)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

    # Write project config
    _write_project_toml(tmp_path)

    # Create scripts/experiments directory with a sidecar
    scripts_dir = tmp_path / "scripts" / "experiments"
    scripts_dir.mkdir(parents=True)

    script = scripts_dir / "my_experiment.py"
    script.touch()

    sidecar = scripts_dir / "my_experiment.bth.toml"
    sidecar.write_text(
        """
[experiment]
hypothesis = "Test hypothesis"
stage_name = "validation"

[reproduction]
requires_pass_stem = "baseline_script"

[outcomes.pass]
condition = "value > 0"
decision = "proceed"
reasoning = "Good result"

[outcomes.fail]
condition = "TRUE"
decision = "investigate"
reasoning = "Default"
is_residual = true

[result_schema]
value = "float"
"""
    )

    with (
        patch("bathos.cluster.push_project"),
        patch("bathos.cluster.submit_job", return_value=_SUBMIT_RESULT),
    ):
        result = runner.invoke(app, ["submit", "--no-wait", "python", "scripts/experiments/my_experiment.py"])

    # Should exit 1 due to missing prerequisite
    assert result.exit_code == 1, result.output
    assert "REPRODUCTION_PREREQUISITE_UNMET" in result.output or "no passing run" in result.output


def test_submit_ac3_hard_gate_production_stage_prerequisite_not_found(tmp_path: Path, monkeypatch):
    """AC-3: bth submit exits 1 when production stage missing reproduction prerequisite."""
    monkeypatch.chdir(tmp_path)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

    # Write project config
    _write_project_toml(tmp_path)

    # Create scripts/experiments directory with a sidecar
    scripts_dir = tmp_path / "scripts" / "experiments"
    scripts_dir.mkdir(parents=True)

    script = scripts_dir / "my_experiment.py"
    script.touch()

    sidecar = scripts_dir / "my_experiment.bth.toml"
    sidecar.write_text(
        """
[experiment]
hypothesis = "Test hypothesis"
stage_name = "production"

[reproduction]
requires_pass_stem = "baseline_script"

[outcomes.pass]
condition = "value > 0"
decision = "proceed"
reasoning = "Good result"

[outcomes.fail]
condition = "TRUE"
decision = "investigate"
reasoning = "Default"
is_residual = true

[result_schema]
value = "float"
"""
    )

    with (
        patch("bathos.cluster.push_project"),
        patch("bathos.cluster.submit_job", return_value=_SUBMIT_RESULT),
    ):
        result = runner.invoke(app, ["submit", "--no-wait", "python", "scripts/experiments/my_experiment.py"])

    # Should exit 1 due to missing prerequisite
    assert result.exit_code == 1, result.output
    assert "REPRODUCTION_PREREQUISITE_UNMET" in result.output or "no passing run" in result.output


def test_submit_ac3_hard_gate_validation_stage_prerequisite_found(tmp_path: Path, monkeypatch):
    """AC-3: bth submit succeeds when validation stage prerequisite is found."""
    from bathos.schema import Run
    from bathos.catalog import write_run
    from bathos.compact import compact

    monkeypatch.chdir(tmp_path)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

    # Write project config
    _write_project_toml(tmp_path)

    # Create prerequisite run
    prereq_run = Run(
        project_slug="myproject",
        command="python scripts/experiments/baseline_script.py",
        argv=["python", "scripts/experiments/baseline_script.py"],
        outcome="pass",
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )
    write_run(prereq_run, catalog_dir)
    compact(catalog_dir)

    # Create scripts/experiments directory with a sidecar
    scripts_dir = tmp_path / "scripts" / "experiments"
    scripts_dir.mkdir(parents=True)

    script = scripts_dir / "my_experiment.py"
    script.touch()

    sidecar = scripts_dir / "my_experiment.bth.toml"
    sidecar.write_text(
        """
[experiment]
hypothesis = "Test hypothesis"
stage_name = "validation"

[reproduction]
requires_pass_stem = "baseline_script"

[outcomes.pass]
condition = "value > 0"
decision = "proceed"
reasoning = "Good result"

[outcomes.fail]
condition = "TRUE"
decision = "investigate"
reasoning = "Default"
is_residual = true

[result_schema]
value = "float"
"""
    )

    with (
        patch("bathos.cluster.push_project"),
        patch("bathos.cluster.submit_job", return_value=_SUBMIT_RESULT),
    ):
        result = runner.invoke(app, ["submit", "--no-wait", "python", "scripts/experiments/my_experiment.py"])

    # Should succeed (exit 0)
    assert result.exit_code == 0, result.output
    assert "Submitted 12345" in result.output


def test_submit_ac4_advisory_exploration_stage_prerequisite_not_found(tmp_path: Path, monkeypatch):
    """AC-4: bth submit warns but exits 0 when exploration stage missing prerequisite."""
    monkeypatch.chdir(tmp_path)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

    # Write project config
    _write_project_toml(tmp_path)

    # Create scripts/experiments directory with a sidecar
    scripts_dir = tmp_path / "scripts" / "experiments"
    scripts_dir.mkdir(parents=True)

    script = scripts_dir / "my_experiment.py"
    script.touch()

    sidecar = scripts_dir / "my_experiment.bth.toml"
    sidecar.write_text(
        """
[experiment]
hypothesis = "Test hypothesis"
stage_name = "exploration"

[reproduction]
requires_pass_stem = "baseline_script"

[outcomes.pass]
condition = "value > 0"
decision = "proceed"
reasoning = "Good result"

[outcomes.fail]
condition = "TRUE"
decision = "investigate"
reasoning = "Default"
is_residual = true

[result_schema]
value = "float"
"""
    )

    with (
        patch("bathos.cluster.push_project"),
        patch("bathos.cluster.submit_job", return_value=_SUBMIT_RESULT),
    ):
        result = runner.invoke(app, ["submit", "--no-wait", "python", "scripts/experiments/my_experiment.py"])

    # Should succeed (exit 0) but print warning
    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output or "advisory" in result.output
    assert "Submitted 12345" in result.output


def test_submit_ac4_advisory_calibration_stage_prerequisite_not_found(tmp_path: Path, monkeypatch):
    """AC-4: bth submit warns but exits 0 when calibration stage missing prerequisite."""
    monkeypatch.chdir(tmp_path)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

    # Write project config
    _write_project_toml(tmp_path)

    # Create scripts/experiments directory with a sidecar
    scripts_dir = tmp_path / "scripts" / "experiments"
    scripts_dir.mkdir(parents=True)

    script = scripts_dir / "my_experiment.py"
    script.touch()

    sidecar = scripts_dir / "my_experiment.bth.toml"
    sidecar.write_text(
        """
[experiment]
hypothesis = "Test hypothesis"
stage_name = "calibration"

[reproduction]
requires_pass_stem = "baseline_script"

[outcomes.pass]
condition = "value > 0"
decision = "proceed"
reasoning = "Good result"

[outcomes.fail]
condition = "TRUE"
decision = "investigate"
reasoning = "Default"
is_residual = true

[result_schema]
value = "float"
"""
    )

    with (
        patch("bathos.cluster.push_project"),
        patch("bathos.cluster.submit_job", return_value=_SUBMIT_RESULT),
    ):
        result = runner.invoke(app, ["submit", "--no-wait", "python", "scripts/experiments/my_experiment.py"])

    # Should succeed (exit 0) but print warning
    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output or "advisory" in result.output
    assert "Submitted 12345" in result.output


# ---------------------------------------------------------------------------
# AC-9 — Submit-provenance write tests
# ---------------------------------------------------------------------------


def test_submit_ac9_writes_submit_provenance_record(tmp_path: Path, monkeypatch):
    """AC-9 Part 1: bth submit writes submit-provenance Parquet record after successful submit."""
    monkeypatch.chdir(tmp_path)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

    # Write project config
    _write_project_toml(tmp_path)

    # Create scripts/experiments directory with a sidecar
    scripts_dir = tmp_path / "scripts" / "experiments"
    scripts_dir.mkdir(parents=True)

    script = scripts_dir / "my_experiment.py"
    script.touch()

    sidecar = scripts_dir / "my_experiment.bth.toml"
    sidecar.write_text(
        """
[experiment]
hypothesis = "Test hypothesis"
stage_name = "validation"

[outcomes.pass]
condition = "value > 0"
decision = "proceed"
reasoning = "Good result"

[outcomes.fail]
condition = "TRUE"
decision = "investigate"
reasoning = "Default"
is_residual = true

[result_schema]
value = "float"
"""
    )

    with (
        patch("bathos.cluster.push_project"),
        patch("bathos.cluster.submit_job", return_value=_SUBMIT_RESULT),
    ):
        result = runner.invoke(app, ["submit", "--no-wait", "python", "scripts/experiments/my_experiment.py"])

    # Should succeed
    assert result.exit_code == 0, result.output
    assert "Submitted 12345" in result.output

    # Check submit-provenance was written
    submit_dir = catalog_dir / "submits" / "myproject"
    assert submit_dir.is_dir(), "submits/<project_slug>/ directory should exist"

    parquet_files = list(submit_dir.glob("*.parquet"))
    assert len(parquet_files) == 1, f"Expected 1 provenance file, found {len(parquet_files)}"

    # Check schema and data
    table = pq.read_table(parquet_files[0])
    assert table.num_rows == 1
    assert "project_slug" in table.column_names
    assert "command" in table.column_names
    assert "sidecar_sha256" in table.column_names
    assert "bth_submit_version" in table.column_names
    assert "submitted_at" in table.column_names
    assert "myxcel_job_id" in table.column_names
    assert "stage_name" in table.column_names

    # Verify field values
    assert table.column("project_slug")[0].as_py() == "myproject"
    assert "my_experiment.py" in table.column("command")[0].as_py()
    assert table.column("myxcel_job_id")[0].as_py() == "12345"
    assert table.column("stage_name")[0].as_py() == "validation"
    # sidecar_sha256 should be a hex string (non-empty for found sidecar)
    sidecar_hash = table.column("sidecar_sha256")[0].as_py()
    assert len(sidecar_hash) == 64, f"SHA256 should be 64 hex chars, got {len(sidecar_hash)}"


def test_submit_ac9_provenance_defaults_stage_name_to_exploration(tmp_path: Path, monkeypatch):
    """AC-9: submit-provenance defaults stage_name to 'exploration' if not in sidecar."""
    monkeypatch.chdir(tmp_path)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

    # Write project config
    _write_project_toml(tmp_path)

    # Create scripts/experiments directory with a sidecar WITHOUT stage_name
    scripts_dir = tmp_path / "scripts" / "experiments"
    scripts_dir.mkdir(parents=True)

    script = scripts_dir / "my_experiment.py"
    script.touch()

    sidecar = scripts_dir / "my_experiment.bth.toml"
    sidecar.write_text(
        """
[experiment]
hypothesis = "Test hypothesis"

[outcomes.pass]
condition = "value > 0"
decision = "proceed"
reasoning = "Good result"

[outcomes.fail]
condition = "TRUE"
decision = "investigate"
reasoning = "Default"
is_residual = true

[result_schema]
value = "float"
"""
    )

    with (
        patch("bathos.cluster.push_project"),
        patch("bathos.cluster.submit_job", return_value=_SUBMIT_RESULT),
    ):
        result = runner.invoke(app, ["submit", "--no-wait", "python", "scripts/experiments/my_experiment.py"])

    assert result.exit_code == 0, result.output

    # Check provenance record
    submit_dir = catalog_dir / "submits" / "myproject"
    parquet_files = list(submit_dir.glob("*.parquet"))
    assert len(parquet_files) == 1

    table = pq.read_table(parquet_files[0])
    # Should default to exploration
    assert table.column("stage_name")[0].as_py() == "exploration"
