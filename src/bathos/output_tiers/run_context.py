"""Reading one run's events for the tiering commands (spec 261023_output-tiering.md sections 3.3, 4).

Every command under ``bth outputs`` / ``bth cold`` / ``bth warm`` starts the same way: refuse a legacy catalog,
resolve a unique run-id prefix, load the run's events from the folded index, and decide which directory the run's
relpaths resolve against. That is :func:`open_run`. Nothing here writes.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bathos.output_tiers.reference import OutputTierError, resolve_run_id

REQUIRES_LOG_MODE = "requires_log_mode"
WORKTREE_UNKNOWN = "worktree_unknown"
WORKTREE_MISSING = "worktree_missing"
INVALID_PARAM = "invalid_param"


@dataclass(frozen=True)
class RunContext:
    """One run, as the tiering commands see it.

    ``worktree_root`` is ``None`` only when the context was opened with ``need_root=False`` (a retract, which
    acts on a recorded event and never touches files). ``root_source`` is ``recorded`` (the run's own events) or
    ``override`` (an imported run's ``--worktree-root``).
    """

    run_id: str
    status: str
    events: list[dict]
    imported: bool
    outputs_declared: bool
    worktree_root: Path | None
    root_source: str
    imported_root: str | None
    project_slug: str = ""


def require_log_mode(catalog_dir: Path) -> None:
    from bathos.runlog.mode import is_log_mode

    if not is_log_mode(catalog_dir):
        raise OutputTierError(
            REQUIRES_LOG_MODE,
            f"{catalog_dir} is a legacy catalog; output tiering reads the run log (D7)",
        )


def read_run_events(con: Any, run_id: str) -> list[dict]:
    """Every event whose entity is ``[run_id]``, as the dict shape ``reference_hashes`` takes."""
    rows = con.execute(
        "SELECT eid, kind, entity, ts, main_root, worktree_root, data FROM events WHERE entity_key = ?",
        [json.dumps([run_id])],
    ).fetchall()
    return [
        {
            "eid": eid,
            "kind": kind,
            "entity": json.loads(entity),
            "ts": ts,
            "main_root": main_root,
            "worktree_root": wroot,
            "data": json.loads(data),
        }
        for eid, kind, entity, ts, main_root, wroot, data in rows
    ]


def _resolve_root(events: list[dict], override: str | None) -> tuple[Path, str, str | None]:
    """``(root, source, imported_root)`` per spec 3.3 "Which worktree_root"."""
    imported = any(ev.get("kind") == "run.imported" for ev in events)
    supplied = Path(os.path.abspath(os.path.expanduser(override))) if override else None
    if imported:
        if supplied is None:
            raise OutputTierError(
                WORKTREE_UNKNOWN,
                "run was imported, so its recorded worktree_root is the importer's default, not a "
                "real directory",
            )
        return supplied, "override", str(supplied)
    started = sorted(
        (ev for ev in events if ev.get("kind") == "run.started"),
        key=lambda e: (e.get("ts", ""), e.get("eid", "")),
    )
    recorded = (started[0].get("worktree_root") if started else None) or None
    if not recorded:
        raise OutputTierError(WORKTREE_UNKNOWN, "no run.started event records a worktree_root")
    root = Path(os.path.normpath(recorded))
    if supplied is not None and Path(os.path.normpath(supplied)) != root:
        raise OutputTierError(
            INVALID_PARAM,
            f"--worktree-root {supplied} differs from the run's recorded worktree_root {root}; "
            "the override exists only for imported runs",
        )
    return root, "recorded", None


def open_run(
    catalog_dir: Path,
    run_prefix: str,
    *,
    worktree_root: str | None = None,
    need_root: bool = True,
) -> RunContext:
    """Load one run for a tiering command. Refuses a non-log catalog (``requires_log_mode``), an unknown or
    ambiguous prefix (``run_not_found`` / ``run_prefix_ambiguous``), an imported run without ``worktree_root``
    (``worktree_unknown``), and a root that is not a directory here (``worktree_missing``).

    Folds any pending log events into ``index.db`` first (best-effort; a no-op on a cluster node), so a prior
    adopt or retract is visible to the reads below.
    """
    from bathos.index import connect_read
    from bathos.runlog.ingest import ingest_after_write

    require_log_mode(catalog_dir)
    ingest_after_write(catalog_dir)
    con = connect_read(catalog_dir)
    try:
        ids = [r[0] for r in con.execute("SELECT id FROM runs").fetchall()]
        run_id = resolve_run_id(run_prefix, ids)
        row = con.execute("SELECT status, project_slug FROM runs WHERE id = ?", [run_id]).fetchone()
        status = (row[0] if row else "") or ""
        project_slug = (row[1] if row else "") or ""
        events = read_run_events(con, run_id)
    finally:
        con.close()

    imported = any(ev.get("kind") == "run.imported" for ev in events)
    declared = any(
        ev.get("kind") == "run.started" and (ev.get("data") or {}).get("outputs_declared") is True
        for ev in events
    )
    ctx = RunContext(
        run_id=run_id,
        status=status,
        events=events,
        imported=imported,
        outputs_declared=declared,
        worktree_root=None,
        root_source="",
        imported_root=None,
        project_slug=project_slug,
    )
    return attach_root(ctx, worktree_root) if need_root else ctx


def attach_root(ctx: RunContext, worktree_root: str | None = None) -> RunContext:
    """``ctx`` with its ``worktree_root`` resolved and checked. Refuses ``worktree_unknown`` (an imported run
    without ``worktree_root``, or a run with no recorded one), ``invalid_param`` (an override on a standard run
    that disagrees with its record) and ``worktree_missing`` (not a directory on this machine)."""
    root, source, imported_root = _resolve_root(ctx.events, worktree_root)
    if not root.is_dir():
        raise OutputTierError(WORKTREE_MISSING, str(root))
    return dataclasses.replace(
        ctx, worktree_root=root, root_source=source, imported_root=imported_root
    )


def is_terminal(status: str) -> bool:
    """True for ``completed`` / ``failed`` / ``killed``. ``running`` and ``abandoned`` are not: a reaped run may
    still be alive (``bth reap --revert`` exists for that), so its files are not final."""
    from bathos.runlog.fold_runs import TERMINAL_STATUSES

    return status in TERMINAL_STATUSES
