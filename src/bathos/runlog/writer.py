"""The segment writer: one file per writer process, dual-written to the
project log and the mirror (D7), with D6's not-best-effort failure behavior.

See spec sections "Segments and writers" and "Line envelope", and D6/D7.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .envelope import build_envelope, segment_stem, slurm_suffix_from_env
from .project_id import read_project_id, register_main_root
from .resolve import LogRootResolution, fallback_log_root

logger = logging.getLogger(__name__)

ROTATE_BYTES = 8 * 1024 * 1024

MIRROR_ROOT = Path.home() / ".bth" / "log-mirror"


def mirror_dir_for(project_id: str | None, slug: str | None) -> Path:
    """`~/.bth/log-mirror/<project_id>/`, or `_null/<slug or _unaffiliated>/`
    when `project_id` is None (D7)."""
    if project_id:
        return MIRROR_ROOT / project_id
    return MIRROR_ROOT / "_null" / (slug or "_unaffiliated")


class SegmentWriter:
    """Owns one writer's current segment across a (target_dir, mirror_dir)
    pair. One instance per (process, log dir); callers share it across
    threads via the lock below."""

    def __init__(self, target_dir: Path, mirror_dir: Path):
        self.target_dir = target_dir
        self.mirror_dir = mirror_dir
        self._lock = threading.Lock()
        self._target_fh: Any = None
        self._mirror_fh: Any = None
        self._segment_stem: str | None = None
        self._seq = 0
        self._bytes_written = 0

    def _open_new_segment(self) -> None:
        host = socket.gethostname()
        pid = os.getpid()
        start_ns = time.time_ns()
        stem = segment_stem(host, pid, start_ns, slurm_suffix=slurm_suffix_from_env())
        self.target_dir.mkdir(parents=True, exist_ok=True)
        self.mirror_dir.mkdir(parents=True, exist_ok=True)
        self._target_fh = open(self.target_dir / f"{stem}.jsonl", "ab")
        self._mirror_fh = open(self.mirror_dir / f"{stem}.jsonl", "ab")
        self._segment_stem = stem
        self._bytes_written = 0
        self._seq = 0

    def _close_segment(self) -> None:
        for fh in (self._target_fh, self._mirror_fh):
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
        self._target_fh = None
        self._mirror_fh = None
        self._segment_stem = None

    @staticmethod
    def _write_and_sync(fh: Any, data: bytes) -> None:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())

    def append(
        self, envelope_builder: Callable[[str, int], dict[str, Any]]
    ) -> tuple[dict[str, Any], bool]:
        """Append one event. `envelope_builder(writer_stem, seq)` builds the
        full envelope once this segment's identity is known.

        Returns `(envelope, mirror_ok)`. Raises on a PRIMARY (project or
        fallback target, whichever this instance targets) write failure --
        that is a hard failure the caller must handle per D6. A mirror
        failure never raises: `mirror_ok=False` communicates it, per D7
        ("a mirror append failure is a warning, not a run failure").

        Either failure abandons the segment (closes both handles) so a torn
        line is never followed by more lines from this writer in either copy;
        the next call opens a fresh segment.
        """
        with self._lock:
            if self._target_fh is None:
                self._open_new_segment()
            assert self._segment_stem is not None
            self._seq += 1
            envelope = envelope_builder(self._segment_stem, self._seq)
            line = (json.dumps(envelope, sort_keys=True) + "\n").encode("utf-8")

            try:
                self._write_and_sync(self._target_fh, line)
            except OSError:
                self._close_segment()
                raise

            self._bytes_written += len(line)
            mirror_ok = True
            try:
                self._write_and_sync(self._mirror_fh, line)
            except OSError:
                mirror_ok = False
                self._close_segment()
            else:
                if self._bytes_written >= ROTATE_BYTES:
                    self._close_segment()

            return envelope, mirror_ok

    def close(self) -> None:
        with self._lock:
            self._close_segment()


_writers: dict[str, SegmentWriter] = {}
_writers_lock = threading.Lock()


def get_writer(target_dir: Path, mirror_dir: Path) -> SegmentWriter:
    """Process-wide singleton per target_dir ("one writer per (process, log
    dir)"). Threads share it behind the writer's own lock."""
    key = str(target_dir.resolve()) if target_dir.exists() else str(target_dir)
    with _writers_lock:
        w = _writers.get(key)
        if w is None:
            w = SegmentWriter(target_dir, mirror_dir)
            _writers[key] = w
        return w


def reset_writers_for_test() -> None:
    """Close and drop every cached writer. Test-only: production processes
    are short-lived (one `bth` invocation) and never need this."""
    with _writers_lock:
        for w in _writers.values():
            w.close()
        _writers.clear()


@dataclass(frozen=True)
class AppendOutcome:
    target: str  # "project" | "fallback" | "none"
    envelope: dict[str, Any] | None
    mirror_ok: bool
    project_error: Exception | None = None
    fallback_error: Exception | None = None

    @property
    def ok(self) -> bool:
        return self.target != "none"


def append_event(
    *,
    kind: str,
    entity: list[str],
    data: dict[str, Any],
    log_root: LogRootResolution,
    project: str | None,
    project_id: str | None,
    origin: str = "live",
    allow_fallback: bool = True,
) -> AppendOutcome:
    """Append one event, per D6: try the project log first; on failure, and
    only if `allow_fallback`, try `~/.bth/log/fallback/<slug|_unaffiliated>/`.
    Neither succeeding is reported as `target="none"` -- callers whose event
    kind is "not best-effort" (run.started, per D6) must treat that as fatal
    (the script must not be launched); other callers may choose to warn only.

    A successful project-log write registers `log_root.main_root` in
    `~/.bth/projects.toml` (Discovery: "the first event written to a project
    log registers it").
    """
    mirror_dir = mirror_dir_for(project_id, project)
    # Freeze eid/ts once so a retry from project -> fallback (same logical event)
    # never mints a second identity or a different timestamp for the same event.
    from .envelope import _now_rfc3339, uuid7

    eid = str(uuid7())
    ts = _now_rfc3339()

    def make_builder(main_root: Path, worktree_root: Path) -> Callable[[str, int], dict]:
        def _builder(writer_stem: str, seq: int) -> dict:
            return build_envelope(
                kind=kind,
                entity=entity,
                data=data,
                main_root=main_root,
                worktree_root=worktree_root,
                project=project,
                project_id=project_id,
                writer=writer_stem,
                seq=seq,
                origin=origin,
                ts=ts,
                eid=eid,
            )

        return _builder

    project_error: Exception | None = None
    if not log_root.unaffiliated:
        project_dir = log_root.main_root / ".bth" / "log"
    else:
        project_dir = Path.home() / ".bth" / "log" / "unaffiliated"
    try:
        writer = get_writer(project_dir, mirror_dir)
        envelope, mirror_ok = writer.append(
            make_builder(log_root.main_root, log_root.worktree_root)
        )
        if not mirror_ok:
            logger.warning(
                "runlog: mirror append failed for eid=%s kind=%s (project log write "
                "succeeded; not a run failure per D7)",
                eid,
                kind,
            )
        if not log_root.unaffiliated:
            register_main_root(log_root.main_root)
        return AppendOutcome(target="project", envelope=envelope, mirror_ok=mirror_ok)
    except OSError as exc:
        project_error = exc
        logger.warning("runlog: project log append failed for eid=%s kind=%s: %s", eid, kind, exc)

    if not allow_fallback:
        return AppendOutcome(
            target="none", envelope=None, mirror_ok=False, project_error=project_error
        )

    fallback_dir = fallback_log_root(log_root)
    try:
        writer = get_writer(fallback_dir, mirror_dir)
        envelope, mirror_ok = writer.append(
            make_builder(log_root.main_root, log_root.worktree_root)
        )
        if not mirror_ok:
            logger.warning(
                "runlog: mirror append failed (fallback path) for eid=%s kind=%s", eid, kind
            )
        return AppendOutcome(
            target="fallback",
            envelope=envelope,
            mirror_ok=mirror_ok,
            project_error=project_error,
        )
    except OSError as exc:
        logger.error(
            "runlog: BOTH project log and fallback append failed for eid=%s kind=%s: "
            "project_error=%s fallback_error=%s",
            eid,
            kind,
            project_error,
            exc,
        )
        return AppendOutcome(
            target="none",
            envelope=None,
            mirror_ok=False,
            project_error=project_error,
            fallback_error=exc,
        )


def resolve_write_project_id(log_root: LogRootResolution) -> str | None:
    """Resolve the `project_id` to embed on an event, per D7:

    - a SLURM job (`SLURM_JOB_ID` set) uses `BTH_PROJECT_ID`, exported by
      `bth submit`; absent, that's `None` (mapped at ingest by slug, with a
      warning -- ingest-side, not this module's concern).
    - otherwise, an unaffiliated root has no id (`None`).
    - otherwise, it's read straight off the resolved main root's `.bth.toml`
      (may be `None` if missing -- callers that must fail on that, like a
      local `bth run`, should call `project_id.require_project_id` instead
      and handle its raised error before ever reaching this point).
    """
    if os.environ.get("SLURM_JOB_ID"):
        return os.environ.get("BTH_PROJECT_ID") or None
    if log_root.unaffiliated:
        return None
    return read_project_id(log_root.main_root / ".bth.toml")
