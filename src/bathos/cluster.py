from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bathos.config import ProjectConfig


@dataclass
class ClusterConfig:
    remote: str
    preset: str
    project: str


def resolve_cluster_config(
    config: ProjectConfig,
    sidecar_data: dict | None = None,
    cli_remote: str | None = None,
    cli_preset: str | None = None,
    cli_project: str | None = None,
) -> ClusterConfig:
    """Resolve cluster config from sidecar, project config, and CLI flags.

    Resolution order (highest wins):
    1. CLI flags (cli_remote, cli_preset, cli_project)
    2. Sidecar data (sidecar_data["cluster"])
    3. Project config (config.slurm)

    Raises ValueError if remote or preset are empty after resolution.
    Defaults project to config.slug if not specified.
    """
    # Start with project config
    slurm_dict = config.slurm or {}
    remote = slurm_dict.get("remote", "")
    preset = slurm_dict.get("preset", "")
    project = slurm_dict.get("project", config.slug)

    # Layer in sidecar data
    if sidecar_data:
        cluster_section = sidecar_data.get("cluster", {})
        if cluster_section.get("remote"):
            remote = cluster_section["remote"]
        if cluster_section.get("preset"):
            preset = cluster_section["preset"]
        if cluster_section.get("project"):
            project = cluster_section["project"]

    # Layer in CLI flags (highest priority)
    if cli_remote:
        remote = cli_remote
    if cli_preset:
        preset = cli_preset
    if cli_project:
        project = cli_project

    # Validate required fields
    if not remote:
        raise ValueError(
            "cluster remote not specified. Set via [cluster].remote in .bth.toml sidecar, "
            "[slurm].remote in .bth/config.toml, or --cli-remote flag."
        )
    if not preset:
        raise ValueError(
            "cluster preset not specified. Set via [cluster].preset in .bth.toml sidecar, "
            "[slurm].preset in .bth/config.toml, or --cli-preset flag."
        )

    return ClusterConfig(remote=remote, preset=preset, project=project)


def push_project(remote: str, project: str) -> None:
    """Run `myxcel push-project <remote> <project>`."""
    result = subprocess.run(
        ["myxcel", "push-project", remote, project],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)


def submit_job(
    remote: str,
    project: str,
    preset: str,
    command: str,
    *,
    job_name: str = "",
    array: str = "",
    dependency: str = "",
    sbatch_args: list[str] | None = None,
) -> dict:
    """Run `myxcel submit-job --json <remote> <project> --preset <preset> --command <command> ...`
    Returns parsed JSON dict with keys: slurm_job_id, script_path, preset_used, job_name."""
    argv = ["myxcel", "submit-job", "--json", remote, project, "--preset", preset, "--command", command]

    if job_name:
        argv.extend(["--job-name", job_name])
    if array:
        argv.extend(["--array", array])
    if dependency:
        argv.extend(["--dependency", dependency])
    if sbatch_args:
        argv.extend(sbatch_args)

    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)

    return json.loads(result.stdout)


def job_wait(remote: str, slurm_job_id: str, timeout: int = 3600) -> dict:
    """Run `myxcel job-wait --json <remote> <slurm_job_id> --timeout <timeout>`
    Returns parsed JSON dict. Raises RuntimeError on subprocess failure."""
    result = subprocess.run(
        ["myxcel", "job-wait", "--json", remote, slurm_job_id, "--timeout", str(timeout)],
        capture_output=True,
        text=True,
        timeout=7200,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)

    return json.loads(result.stdout)


def pull_project(remote: str, project: str) -> None:
    """Run `myxcel pull-project <remote> <project>`."""
    result = subprocess.run(
        ["myxcel", "pull-project", remote, project],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
