from __future__ import annotations
import dataclasses
import subprocess
import time
from pathlib import Path

from bathos.catalog import write_run
from bathos.git import capture_git_state
from bathos.schema import Run


def run_script(
    argv: list[str],
    project_slug: str,
    catalog_dir: Path,
    output_paths: list[str],
    tags: list[str],
    cwd: Path = Path.cwd(),
) -> int:
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
    )
    write_run(run, catalog_dir)

    start = time.monotonic()
    try:
        result = subprocess.run(argv, cwd=cwd)
        exit_code = result.returncode
        status = "completed" if exit_code == 0 else "failed"
    except KeyboardInterrupt:
        exit_code = 130
        status = "killed"

    run = dataclasses.replace(
        run,
        duration_s=time.monotonic() - start,
        exit_code=exit_code,
        status=status,
    )
    write_run(run, catalog_dir)
    return exit_code
