from __future__ import annotations

import dataclasses
import os
import subprocess
import time
from pathlib import Path

import typer

from bathos.catalog import write_run
from bathos.git import capture_git_state
from bathos.schema import Run
from bathos.sidecar import find_sidecar, is_in_enforced_dir, parse_sidecar, evaluate_outcome, SidecarError


def _find_script_path(argv: list[str], cwd: Path) -> Path | None:
    """Extract script path from argv. Returns None if not a file-based script."""
    if not argv:
        return None

    # If first arg is not python/python3, assume it's the script
    first = argv[0].lower()
    if not any(first.endswith(exe) for exe in ("python", "python3")):
        candidate = cwd / argv[0] if not Path(argv[0]).is_absolute() else Path(argv[0])
        if candidate.exists() and candidate.suffix == ".py":
            return candidate.resolve()
        return None

    # First arg is python; look for script file in subsequent args
    # Handle: python script.py, python -c "...", python -m module, etc.
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ("-c", "-m", "-W"):
            # These take an argument but don't point to a file
            i += 2
        elif arg.startswith("-"):
            # Other flags
            i += 1
        else:
            # First non-flag arg after python is the script
            candidate = cwd / arg if not Path(arg).is_absolute() else Path(arg)
            if candidate.exists() and candidate.suffix == ".py":
                return candidate.resolve()
            return None
        i += 1
    return None


def run_script(
    argv: list[str],
    project_slug: str,
    catalog_dir: Path,
    output_paths: list[str],
    tags: list[str],
    cwd: Path = Path.cwd(),
) -> int:
    script_path = _find_script_path(argv, cwd)

    # Pre-registration enforcement
    sidecar = None
    if script_path is not None and is_in_enforced_dir(script_path):
        sidecar_path = find_sidecar(script_path)
        if sidecar_path is None:
            typer.echo(
                f"Error: {script_path.name} is in an enforced directory "
                f"({script_path.parent.name}/) but has no sidecar.\n"
                f"Create {script_path.stem}.bth.toml next to the script before running.",
                err=True,
            )
            return 1
        try:
            sidecar = parse_sidecar(sidecar_path)
        except SidecarError as e:
            typer.echo(f"Error: invalid sidecar — {e}", err=True)
            return 1

    git = capture_git_state(cwd)
    run = Run(
        project_slug=project_slug,
        command=" ".join(argv),
        argv=argv,
        git_hash=git.hash,
        git_branch=git.branch,
        git_dirty=git.dirty,
        output_paths=output_paths,
        tags=tags,
        status="running",
        slurm_job_id=os.environ.get("SLURM_JOB_ID", ""),
    )
    catalog_dir.mkdir(parents=True, exist_ok=True)
    write_run(run, catalog_dir)

    start = time.monotonic()
    try:
        result = subprocess.run(argv, cwd=cwd)
        exit_code = result.returncode
        status = "completed" if exit_code == 0 else "failed"
    except KeyboardInterrupt:
        exit_code = 130
        status = "killed"

    outcome = ""
    if sidecar is not None:
        # Outcome evaluation: read result_schema fields from metadata
        import json
        try:
            meta = json.loads(run.metadata)
        except (json.JSONDecodeError, TypeError):
            meta = {}
        outcome = evaluate_outcome(sidecar, meta)

    run = dataclasses.replace(
        run,
        duration_s=time.monotonic() - start,
        exit_code=exit_code,
        status=status,
        outcome=outcome,
    )
    write_run(run, catalog_dir)
    return exit_code
