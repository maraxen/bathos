"""Project-local append-only run log (spec `260925_project-local-run-log.md`).

Delivery step 2a (this package's current scope): the writer, log directory
resolution, the D3 gitignore check, project-id handling, root registration,
the mirror, and `bth log restore`. Everything here is inert unless
`bathos.runlog.mode.is_log_mode()` is True -- with the flag off (the
default), no code outside this package calls into it, so production
behaviour is unchanged.

Step 2b (not yet built) wires per-write-site event emission (AC-25) at each
row of the spec's "Authoritative writes" table. Step 3+ (index, fold, ingest,
migration) is future work.
"""

from __future__ import annotations

from .envelope import build_envelope, capture_git_provenance, uuid7
from .mode import (
    LogModeRefusedError,
    RunLogError,
    cutover_marker_path,
    is_log_mode,
    writers_lock,
    writers_lock_path,
)
from .project_id import (
    AssignResult,
    ProjectIdMissingError,
    assign_project_id,
    list_registered_roots,
    live_roots_with_id,
    mint_project_id,
    prune_vanished_roots,
    read_project_id,
    register_main_root,
    require_project_id,
)
from .resolve import (
    LogNotIgnoredError,
    LogRootResolution,
    ensure_log_ignored,
    fallback_log_root,
    is_log_ignored,
    require_log_ignored,
    resolve_log_root,
)
from .restore import RestoreReport, restore_from_mirror
from .writer import (
    AppendOutcome,
    SegmentWriter,
    append_event,
    get_writer,
    mirror_dir_for,
    resolve_write_project_id,
)

__all__ = [
    "AppendOutcome",
    "AssignResult",
    "LogModeRefusedError",
    "LogNotIgnoredError",
    "LogRootResolution",
    "ProjectIdMissingError",
    "RestoreReport",
    "RunLogError",
    "SegmentWriter",
    "append_event",
    "assign_project_id",
    "build_envelope",
    "capture_git_provenance",
    "cutover_marker_path",
    "ensure_log_ignored",
    "fallback_log_root",
    "get_writer",
    "is_log_ignored",
    "is_log_mode",
    "list_registered_roots",
    "live_roots_with_id",
    "mint_project_id",
    "mirror_dir_for",
    "prune_vanished_roots",
    "read_project_id",
    "register_main_root",
    "require_log_ignored",
    "require_project_id",
    "resolve_log_root",
    "resolve_write_project_id",
    "restore_from_mirror",
    "uuid7",
    "writers_lock",
    "writers_lock_path",
]
