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
import sys
import time
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


def lock_holders(path: Path) -> list[tuple[int, str]]:
    """`(pid, cmdline)` for every process holding a flock on `path`.

    Reads `/proc/locks`, which is Linux-only -- everywhere else (and on any
    parse or permission failure) this returns `[]` and the caller simply
    reports the wait without naming a holder. Never raises: this is used only
    to make a diagnostic message more useful, so it must not be able to turn
    a successful command into a failed one.

    Only `FLOCK` rows are considered. DuckDB's own database-file locks are
    POSIX (`fcntl`) and appear as `POSIX`, so they can never be mistaken for a
    writers-lock holder.
    """
    try:
        st = path.stat()
        want_dev = (os.major(st.st_dev), os.minor(st.st_dev))
        holders: list[tuple[int, str]] = []
        with open("/proc/locks") as fh:
            for line in fh:
                parts = line.split()
                # `id: TYPE KIND ACCESS PID MAJ:MIN:INODE start end`
                if len(parts) < 6 or parts[1] != "FLOCK":
                    continue
                try:
                    maj, minor, ino = parts[5].split(":")
                    # MAJ:MIN are hex in /proc/locks; the inode is decimal.
                    if int(ino) != st.st_ino or (int(maj, 16), int(minor, 16)) != want_dev:
                        continue
                    pid = int(parts[4])
                except ValueError:
                    continue
                try:
                    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
                    # Collapse ALL whitespace: a holder's argv can contain
                    # newlines (e.g. `python -c "<script>"`), which would
                    # otherwise break the message across lines.
                    cmd = " ".join(raw.replace(b"\0", b" ").decode(errors="replace").split())
                except OSError:
                    cmd = ""
                holders.append((pid, cmd))
        return holders
    except (OSError, ValueError):
        return []


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _report_lock_wait(path: Path, exclusive: bool) -> None:
    """Say what we are blocked on, before blocking (debt #2012)."""
    kind = "exclusive" if exclusive else "shared"
    lines = [f"bth: waiting for the catalog writers lock ({kind})", f"      {path}"]
    holders = lock_holders(path)
    for pid, cmd in holders[:5]:
        lines.append(f"      held by pid {pid}" + (f": {cmd[:100]}" if cmd else ""))
    if len(holders) > 5:
        lines.append(f"      ... and {len(holders) - 5} more")
    if not holders:
        lines.append("      held by another bth process")
    lines.append("      This command will continue as soon as the lock is free (Ctrl-C to abort).")
    print("\n".join(lines), file=sys.stderr, flush=True)


@contextmanager
def writers_lock(catalog_dir: Path | None = None, exclusive: bool = False) -> Iterator[None]:
    """Hold the writers lock for the duration of one local write.

    Every local command that writes (legacy or events) takes this shared
    before reading the mode, and holds it for its duration; `bth migrate
    --to-log` takes it exclusively. SLURM jobs skip it entirely -- their
    catalog is remote and they only append logs, so the local switch does not
    concern them.

    The lock is acquired non-blocking first. If that would block, the wait is
    reported to stderr (naming the holding pids where `/proc/locks` is
    available) and only then does the call block -- so a command queued behind
    a long `bth run`, or a `bth migrate --to-log` queued behind one, says so
    instead of looking hung (debt #2012). Reporting goes to stderr so it never
    corrupts JSON written to stdout.
    """
    if os.environ.get("SLURM_JOB_ID"):
        yield
        return
    path = writers_lock_path(catalog_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except OSError:
            # Contended. Report who has it, then wait as before.
            _report_lock_wait(path, exclusive)
            waited = time.monotonic()
            fcntl.flock(fd, mode)
            print(
                f"bth: acquired the writers lock after {_format_duration(time.monotonic() - waited)}",
                file=sys.stderr,
                flush=True,
            )
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
