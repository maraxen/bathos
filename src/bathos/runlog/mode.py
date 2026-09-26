"""The log flag and cut-over mode.

See `.praxia/docs/specs/260925_project-local-run-log.md`, "Mode (the flag and
cut-over)". Everything in this module is inert until `~/.bth/catalog/cutover.json`
exists (or `BTH_LOG_MODE=1` is honoured in a SLURM job or a test) -- with
neither, `is_log_mode()` returns False and every runlog write site (2b) must
fall back to its existing legacy behavior.

`bth migrate --to-log` (delivery step 4, not built here) is the only writer of
the cut-over marker. This module only *reads* it.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bathos.config import default_catalog_dir


class RunLogError(Exception):
    """Base class for structured runlog errors (see errors.py registration)."""


class LogModeRefusedError(RunLogError):
    """`BTH_LOG_MODE=1` was set somewhere it is not honoured (Mode section).

    Refused so the real local catalog can never be half-switched: outside a
    SLURM job (`SLURM_JOB_ID`) or a test with a non-default `BTH_CATALOG_DIR`
    (`PYTEST_CURRENT_TEST` set), the flag can only come from the on-disk
    cut-over marker.
    """


def cutover_marker_path(catalog_dir: Path | None = None) -> Path:
    return (catalog_dir or default_catalog_dir()) / "cutover.json"


def writers_lock_path(catalog_dir: Path | None = None) -> Path:
    return (catalog_dir or default_catalog_dir()) / "writers.lock"


def is_log_mode_env_honored() -> bool:
    """True where `BTH_LOG_MODE=1` is honoured without a marker present.

    Two contexts: a SLURM job (`SLURM_JOB_ID` set --  "Cluster jobs" in the
    Mode section), or a test (`PYTEST_CURRENT_TEST` set) that has also pointed
    `BTH_CATALOG_DIR` away from the real `~/.bth/catalog` -- a test with the
    *default* catalog dir is not exempted, so a forgotten override cannot
    silently flip a developer's real catalog into log mode.
    """
    if os.environ.get("SLURM_JOB_ID"):
        return True
    return bool(os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("BTH_CATALOG_DIR"))


def is_log_mode(catalog_dir: Path | None = None) -> bool:
    """Resolve the flag: on iff the local marker exists, or `BTH_LOG_MODE=1`
    is set in a context where it is honoured.

    Raises `LogModeRefusedError` if `BTH_LOG_MODE=1` is set anywhere else, so a
    stray env var can never half-switch the real local catalog.
    """
    if cutover_marker_path(catalog_dir).exists():
        return True
    if os.environ.get("BTH_LOG_MODE") == "1":
        if is_log_mode_env_honored():
            return True
        raise LogModeRefusedError(
            "BTH_LOG_MODE=1 is only honoured in a SLURM job (SLURM_JOB_ID set) or a "
            "test with a non-default BTH_CATALOG_DIR (PYTEST_CURRENT_TEST set); refusing "
            "so the real local catalog can never be half-switched. Unset BTH_LOG_MODE, "
            "or run `bth migrate --to-log` to write the real cut-over marker."
        )
    return False


@contextmanager
def writers_lock(catalog_dir: Path | None = None, exclusive: bool = False) -> Iterator[None]:
    """Hold the writers lock for the duration of one local write.

    Every local command that writes (legacy or events) takes this shared
    before reading the mode, and holds it for its duration; `bth migrate
    --to-log` takes it exclusively. SLURM jobs skip it entirely -- their
    catalog is remote and they only append logs, so the local switch does not
    concern them.
    """
    if os.environ.get("SLURM_JOB_ID"):
        yield
        return
    path = writers_lock_path(catalog_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
