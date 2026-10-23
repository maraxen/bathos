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


def _parse_myxcel_json(result, what: str, *, hint: str = "") -> dict:
    """Parse a myxcel subprocess's JSON stdout, converting empty/invalid output into a
    RuntimeError (debt #2228) -- callers (e.g. `bth submit --wait`) catch RuntimeError,
    but not json.JSONDecodeError, so a bare json.loads crashed them with a traceback."""
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if not stdout:
        msg = f"{what} produced empty stdout (expected JSON)."
    else:
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as e:
            msg = f"{what} produced invalid JSON ({e}): {stdout[:200]!r}"
    if stderr:
        msg += f" stderr: {stderr[:500]}"
    if hint:
        msg += f" {hint}"
    raise RuntimeError(msg)


def push_project(remote: str, project: str) -> None:
    """Run `myxcel push <remote> <project> --apply --yes`.

    `myxcel push` (there is no `push-project` subcommand -- #1951/#1947) is
    dry-run by default; `--apply` is required to actually transfer anything,
    and `--yes` auto-confirms the interactive preflight prompt that would
    otherwise block forever on this subprocess's closed stdin.
    """
    result = subprocess.run(
        ["myxcel", "push", remote, project, "--apply", "--yes"],
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
    argv = [
        "myxcel",
        "submit-job",
        "--json",
        remote,
        project,
        "--preset",
        preset,
        "--command",
        command,
    ]

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

    return _parse_myxcel_json(result, "myxcel submit-job")


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

    return _parse_myxcel_json(
        result,
        f"myxcel job-wait for job {slurm_job_id}",
        hint=(
            f"The job may still be running; check it with the myxcel job_status "
            f"monitor (remote {remote}) rather than assuming failure."
        ),
    )


def pull_project(remote: str, project: str) -> None:
    """Run `myxcel pull <remote> <project>`.

    There is no `pull-project` subcommand (#1951/#1947); `myxcel pull` is the
    real one. Unlike `push`, `pull` actually transfers by default (no
    `--apply` gate) and never prompts interactively, so no extra flags are
    needed here.
    """
    result = subprocess.run(
        ["myxcel", "pull", remote, project],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)


class MyxcelCapabilityGapError(RuntimeError):
    """Raised when a bathos call needs a myxcel capability that the installed
    `myxcel` CLI/API does not currently expose. Not a transient failure --
    retrying will not help; the gap has to be closed in myxcel itself (see
    `pull_path`'s docstring).

    ``error_code`` is the JSON error code a command reports for it (output tiering,
    spec 261023 section 6.1 step 1)."""

    error_code = "myxcel_capability_gap"


# How an installed myxcel that predates `pull-path` rejects it. typer-era
# myxcel: "No such command 'pull-path'." (exit 2). cyclopts-era myxcel
# (myxcel #30 onward): "Unused Tokens: ['pull-path', ...]" (exit 2), observed
# 261012 on myxcel main 9e14d3a.
_UNKNOWN_SUBCOMMAND_MARKERS = ("no such command", "unused tokens: ['pull-path'")


def _is_unknown_subcommand(stderr: str | None) -> bool:
    text = (stderr or "").lower()
    return any(marker in text for marker in _UNKNOWN_SUBCOMMAND_MARKERS)


def pull_path(remote: str, remote_path: str, local_dest: str) -> None:
    """Pull an arbitrary remote directory to an explicit local destination via
    `myxcel pull-path <remote> <remote_path> --dest <local_dest>`.

    Needed for the cluster run-log pull (spec "Cluster"): the source
    directories (`<remote root>/.bth/log/`, `~/.bth/log/fallback/<slug>/`,
    `~/.bth/log-mirror/<project_id>/`) are not addressable through
    `pull_project`'s project-name mapping, and CLAUDE.md ("Bathos Sync
    Delegates to Myxcel") requires the myxcel boundary here rather than a
    direct rsync call. The myxcel subcommand is backlog #5880.

    Contract (agreed with myxcel): exit 0 only on success (returns None);
    non-zero on refusal, error, or timeout, with the reason on stderr.

    `remote` is the myxcel PROFILE name (e.g. "engaging", "titanix"), not an
    ssh host string. `sync.pull_cluster_log` passes `remote_config["host"]`,
    which equals the profile name in every current .bth.toml; that coupling
    is noted at the call site.

    `local_dest` must already exist -- this wrapper does not create it.

    Gap detection: if the installed myxcel predates `pull-path`, it prints
    "No such command" (typer era) or "Unused Tokens: ['pull-path', ...]"
    (cyclopts era) on stderr and this raises
    `MyxcelCapabilityGapError` (a `RuntimeError` subclass; retrying will not
    help, myxcel must be upgraded). To verify an installed myxcel, grep its
    package for `def pull_path` in `rsync.py`
    (`~/.local/share/uv/tools/myxcel/lib/python*/site-packages/myxcel/rsync.py`).
    Any other non-zero exit raises `RuntimeError(stderr)`, like `pull_project`.
    A subprocess timeout is re-raised as a `RuntimeError` with a clear message.

    Callers in the best-effort cluster-log pull path
    (`bathos.sync.pull_cluster_log`) catch `Exception` per sub-path and record
    the failure, so one unavailable sub-path does not abort the others.
    """
    try:
        result = subprocess.run(
            ["myxcel", "pull-path", remote, remote_path, "--dest", local_dest],
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"myxcel pull-path {remote}:{remote_path} -> {local_dest} timed out after 900s"
        ) from exc
    if result.returncode != 0:
        if _is_unknown_subcommand(result.stderr):
            raise MyxcelCapabilityGapError(
                "the installed myxcel predates `pull-path` (myxcel backlog #5880); "
                f"cannot pull {remote}:{remote_path!r} to {local_dest!r}. Upgrade myxcel. "
                "To verify the installed package, grep its rsync.py for `def pull_path` "
                "(~/.local/share/uv/tools/myxcel/lib/python*/site-packages/myxcel/rsync.py)."
            )
        raise RuntimeError(result.stderr)
