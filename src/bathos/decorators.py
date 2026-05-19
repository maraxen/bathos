from __future__ import annotations

import dataclasses
import functools
import os
import sys
import time
import warnings
from pathlib import Path

from bathos.catalog import write_run
from bathos.config import default_catalog_dir
from bathos.git import capture_git_state
from bathos.schema import Run


def experiment(func):
    """Decorator: capture provenance for a function and write a Run to the catalog.

    Reads BTH_PROJECT_SLUG and BTH_CATALOG_DIR from env. If BTH_PROJECT_SLUG
    is not set, skips recording and runs the function unmodified.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        project_slug = os.environ.get("BTH_PROJECT_SLUG", "").strip()
        if not project_slug:
            warnings.warn(
                f"@bth.experiment: BTH_PROJECT_SLUG not set — provenance not recorded for {func.__name__}",
                stacklevel=2,
            )
            return func(*args, **kwargs)

        catalog_dir_env = os.environ.get("BTH_CATALOG_DIR")
        catalog_dir = Path(catalog_dir_env) if catalog_dir_env else default_catalog_dir()

        cwd = Path.cwd()
        git = capture_git_state(cwd)
        command = f"{func.__module__}.{func.__name__}"
        argv = [func.__name__] + sys.argv[1:]

        run = Run(
            project_slug=project_slug,
            command=command,
            argv=argv,
            git_hash=git.hash,
            git_branch=git.branch,
            git_dirty=git.dirty,
            status="running",
        )
        catalog_dir.mkdir(parents=True, exist_ok=True)
        write_run(run, catalog_dir)

        start = time.monotonic()
        exit_code = 0
        status = "completed"
        try:
            result = func(*args, **kwargs)
        except BaseException:
            exit_code = 1
            status = "failed"
            raise
        finally:
            run = dataclasses.replace(
                run,
                duration_s=time.monotonic() - start,
                exit_code=exit_code,
                status=status,
            )
            write_run(run, catalog_dir)

        return result

    return wrapper
