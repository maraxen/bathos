"""Project-local append-only run log (spec `260925_project-local-run-log.md`).

Delivery step 2a: the writer, log directory resolution, the D3 gitignore
check, project-id handling, root registration, the mirror, and `bth log
restore`.

Delivery step 2b, wave i (this package's current scope, `emit.py`): the
shared emission layer (`unit_of_work()`, `emit_event()`, `emit_or_legacy()`)
and the write sites wired to it so far -- `runner.py` (run.started,
run.finished, run.outputs_hashed), `catalog.write_submit_provenance`
(submit.recorded), `checker`/`check --rebaseline` (run.outputs_hashed),
`postmortem` validate/get (run.postmortem_applied), and `reap.py`
(run.reaped, run.reap_reverted). Wave ii (campaigns.py, claim.py,
campaign_edges.py, anchor.py, blast_radius.py, trust_ledger.py,
archived_items.py) is not yet built.

Delivery step 3, wave a (`index.py`, `ingest.py`, `fold_runs.py`): the
disposable `index.db` schema, generation-swap ingest, the run fold, and the
`connect_read()` read entry point.

Delivery step 3, wave b (`fold_campaigns.py`, `fold_edges.py`,
`fold_anchors.py`, `fold_ledgers.py`): the campaign fold (`campaigns` +
`campaign_runs`, including the `runs.seq_position`/`runs.evalue` columns wave
a stubbed to NULL), `campaign_edges`/`run_edges`, `sidecar_anchors`,
`blast_radius_ledger`, `trust_ledger`, `archived_items`, and submit
provenance (`submits`). The 25-module reader migration (AC-18) and the legacy
importer (Migration steps 0-4) are not yet built.

Everything here is inert unless `bathos.runlog.mode.is_log_mode()` is True --
with the flag off (the default), every write site's legacy behavior is
unchanged, byte-for-byte, and `connect_read()` is a pass-through to the
legacy `bathos.db`.
"""

from __future__ import annotations

from .emit import (
    RunNotLaunchedError,
    apply_run_finished_exit_semantics,
    current_mode,
    emit_event,
    emit_or_legacy,
    in_unit_of_work,
    run_event_data,
    sidecar_declaration_for_event,
    unit_of_work,
)
from .envelope import build_envelope, capture_git_provenance, uuid7
from .fold_anchors import fold_anchor
from .fold_campaigns import fold_campaign
from .fold_edges import fold_edge
from .fold_ledgers import fold_archived_item, fold_blast_radius, fold_submit, fold_trust_ledger
from .fold_runs import fold_run
from .index import connect_read, index_db_path
from .ingest import IngestReport, IngestWalRemainsError, discover_roots, run_ingest
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
    "IngestReport",
    "IngestWalRemainsError",
    "LogModeRefusedError",
    "LogNotIgnoredError",
    "LogRootResolution",
    "ProjectIdMissingError",
    "RestoreReport",
    "RunLogError",
    "RunNotLaunchedError",
    "SegmentWriter",
    "apply_run_finished_exit_semantics",
    "append_event",
    "assign_project_id",
    "build_envelope",
    "capture_git_provenance",
    "connect_read",
    "current_mode",
    "cutover_marker_path",
    "discover_roots",
    "emit_event",
    "emit_or_legacy",
    "ensure_log_ignored",
    "fallback_log_root",
    "fold_anchor",
    "fold_archived_item",
    "fold_blast_radius",
    "fold_campaign",
    "fold_edge",
    "fold_run",
    "fold_submit",
    "fold_trust_ledger",
    "get_writer",
    "in_unit_of_work",
    "index_db_path",
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
    "run_event_data",
    "run_ingest",
    "sidecar_declaration_for_event",
    "unit_of_work",
    "uuid7",
    "writers_lock",
    "writers_lock_path",
]
