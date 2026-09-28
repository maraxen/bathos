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
    `pull_path`'s docstring)."""


def pull_path(remote: str, remote_path: str, local_dest: str) -> None:
    """Intended to generalize `pull_project`'s myxcel wrapper to an explicit
    remote directory (rather than a myxcel-registered project name) and an
    explicit local destination -- needed for the cluster run-log pull (spec
    "Cluster"): the source directories (`<remote root>/.bth/log/`,
    `~/.bth/log/fallback/<slug>/`, `~/.bth/log-mirror/<project_id>/`) are not
    addressable through `pull_project`'s project-name mapping, and CLAUDE.md
    ("Bathos Sync Delegates to Myxcel") requires the myxcel boundary here
    rather than a direct rsync call.

    **This capability does not exist in myxcel (verified 260927).** The
    previous implementation shelled out to
    `myxcel pull <remote> <remote_path> --dest <local_dest>`, but the
    installed `myxcel pull --help` is:

        Usage: myxcel pull [OPTIONS] {remote} {project}
        Options: --dry-run/-n, --no-preflight, --full, --no-worktree, --worktree

    -- there is no `--dest` option, and the second positional is a
    myxcel-registered *project name*, not a filesystem path. The Python
    function backing it, `myxcel.rsync.pull(profile, project, pc, full=,
    dry_run=, no_preflight=, worktree=)`, is likewise hard-bound: it always
    writes to `profile.local_workspace / project` (or a `WorktreeContext`'s
    `local_root`) and always reads from `profile.pull_paths` /
    `pc.pull.paths` (or the full workspace root under `--full`) under
    `{profile.host}:{workspace}/{project}/...` -- there is no parameter for
    an arbitrary remote directory or an arbitrary local destination.
    `myxcel.log_pull.pull_job_logs` is a separate, SLURM-log-specific path
    (per-job stdout/stderr, not a directory mirror) and does not help either.
    `cli.py` has no other path-shaped pull subcommand (`fetch`/`get`/etc.).

    So this previously called a non-existent flag on every real invocation
    (failing with a myxcel usage error), invisibly, because tests mock this
    function directly rather than exercising the real subprocess call. Per
    CLAUDE.md's cluster rules, do NOT invent new myxcel flags here to paper
    over the gap. Until myxcel exposes an arbitrary-path pull (a new
    subcommand, or an importable rsync-wrapper API), this wrapper refuses
    outright and loudly, rather than silently mis-invoking the CLI.
    `local_dest` must already exist -- this wrapper does not create it.

    Raises `MyxcelCapabilityGapError` (a `RuntimeError` subclass) every time
    it is called. Callers in the best-effort cluster-log pull path
    (`bathos.sync.pull_cluster_log`) already catch `Exception` per sub-path
    and log a warning, so this surfaces as a clear, actionable log line
    rather than crashing `bth sync --pull`.
    """
    raise MyxcelCapabilityGapError(
        "bathos.cluster.pull_path requires pulling an arbitrary remote path "
        f"({remote}:{remote_path!r}) to an arbitrary local destination "
        f"({local_dest!r}), but the installed myxcel CLI has no such "
        "capability: `myxcel pull` is `myxcel pull [OPTIONS] {remote} "
        "{project}` (a registered project name, no --dest option), and "
        "`myxcel.rsync.pull()` only pulls a project's configured pull_paths "
        "into profile.local_workspace/project. File a myxcel feature "
        "request for a generic path-pull subcommand or importable API "
        "before re-enabling this wrapper -- do not add ad-hoc flags to the "
        "myxcel CLI invocation to work around it."
    )
