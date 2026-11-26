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
    acts on a recorded event and never touches files). ``root_source`` is ``recorded`` (the run's own events),
    ``local_resolved`` (resolved via import receipt / local checkout), or ``override`` (an imported run's
    ``--worktree-root``).
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
    receipt_worktree_root: str | None = None
    remote_provenance: bool = False


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
        "SELECT eid, kind, entity, ts, project, project_id, main_root, worktree_root, data "
        "FROM events WHERE entity_key = ?",
        [json.dumps([run_id])],
    ).fetchall()
    return [
        {
            "eid": eid,
            "kind": kind,
            "entity": json.loads(entity),
            "ts": ts,
            "project": project,
            "project_id": project_id,
            "main_root": main_root,
            "worktree_root": wroot,
            "data": json.loads(data),
        }
        for eid, kind, entity, ts, project, project_id, main_root, wroot, data in rows
    ]


def _run_events_in_remote_or_imported_root(catalog_dir: Path, events: list[dict]) -> bool:
    """True if the run's events originate only from ``imported`` or ``remote-*`` roots (not local project roots).

    Prevents same-cluster shared-FS runs (whose ``run.started`` sits in a local ``project`` root) from being
    misclassified as imported even when ``hostname != socket.gethostname()``.
    """
    from bathos.runlog.ingest import discover_roots

    eids = {str(ev.get("eid")) for ev in events if ev.get("eid")}
    if not eids:
        return False

    found_remote_or_imported = False
    found_local = False
    for root_kind, _root_id, log_dir in discover_roots(catalog_dir):
        if not log_dir.is_dir():
            continue
        is_remote_root = root_kind == "imported" or root_kind.startswith("remote-")
        for seg in sorted(log_dir.glob("*.jsonl")):
            try:
                text = seg.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("eid") in eids:
                    if is_remote_root:
                        found_remote_or_imported = True
                    elif root_kind in ("project", "fallback"):
                        found_local = True
    return found_remote_or_imported and not found_local


def _matching_cwd_worktree(events: list[dict], project_slug: str) -> Path | None:
    """Return ``resolve_workspace(cwd).fs_root`` only if it is an affiliated checkout for the same project."""
    from bathos.config import find_project_config, load_project_config
    from bathos.runlog.project_id import read_project_id
    from bathos.workspace import resolve_workspace

    try:
        ws = resolve_workspace(Path.cwd())
    except Exception:
        return None
    if ws.source == "cwd" or not ws.fs_root.is_dir():
        return None
    run_pids = {ev.get("project_id") for ev in events if ev.get("project_id")}
    local_pid = read_project_id(ws.fs_root / ".bth.toml")
    if run_pids:
        return ws.fs_root if local_pid in run_pids else None
    cfg = find_project_config(ws.fs_root)
    if cfg is not None and project_slug:
        try:
            if load_project_config(cfg).slug == project_slug:
                return ws.fs_root
        except Exception:
            return None
    return None


def _resolve_root(
    events: list[dict],
    override: str | None,
    *,
    imported: bool | None = None,
    receipt_worktree_root: str | None = None,
    remote_provenance: bool = False,
    project_slug: str = "",
) -> tuple[Path, str, str | None]:
    """``(root, source, imported_root)`` per spec 3.3 / 261125 §4.3 "Which worktree_root"."""
    is_imported = (
        imported if imported is not None else any(ev.get("kind") == "run.imported" for ev in events)
    )
    supplied = Path(os.path.abspath(os.path.expanduser(override))) if override else None
    if is_imported:
        if supplied is not None:
            return supplied, "override", str(supplied)
        if receipt_worktree_root:
            resolved_rec = Path(os.path.abspath(os.path.expanduser(receipt_worktree_root)))
            if resolved_rec.is_dir():
                return resolved_rec, "local_resolved", str(resolved_rec)
        if remote_provenance and not any(ev.get("kind") == "run.imported" for ev in events):
            matched_ws = _matching_cwd_worktree(events, project_slug)
            if matched_ws is not None:
                return matched_ws, "local_resolved", str(matched_ws)
        raise OutputTierError(
            WORKTREE_UNKNOWN,
            "run was imported from another host, so its recorded worktree_root is not trustworthy "
            "locally; pass --worktree-root <path> to the local checkout directory",
        )
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
    receipt_wt: str | None = None
    has_receipt = False
    imported_from_host = ""
    try:
        ids = [r[0] for r in con.execute("SELECT id FROM runs").fetchall()]
        run_id = resolve_run_id(run_prefix, ids)
        cols = {
            r[0]
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'runs'"
            ).fetchall()
        }
        if "imported_from_host" in cols:
            row = con.execute(
                "SELECT status, project_slug, imported_from_host FROM runs WHERE id = ?", [run_id]
            ).fetchone()
            imported_from_host = (row[2] if row and row[2] else "") or ""
        else:
            row = con.execute("SELECT status, project_slug FROM runs WHERE id = ?", [run_id]).fetchone()
        status = (row[0] if row else "") or ""
        project_slug = (row[1] if row else "") or ""
        events = read_run_events(con, run_id)
        has_receipts_tbl = bool(
            con.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = 'import_receipts'"
            ).fetchone()
        )
        if has_receipts_tbl:
            rrow = con.execute(
                "SELECT local_worktree_root FROM import_receipts WHERE run_id = ? "
                "ORDER BY imported_at DESC, bundle_sha256 DESC LIMIT 1",
                [run_id],
            ).fetchone()
            if rrow is not None:
                has_receipt = True
                receipt_wt = rrow[0]
    finally:
        con.close()

    has_imported_kind = any(ev.get("kind") == "run.imported" for ev in events)
    remote_root_prov = False
    if not has_imported_kind and not has_receipt and not imported_from_host:
        remote_root_prov = _run_events_in_remote_or_imported_root(catalog_dir, events)
    imported = has_imported_kind or has_receipt or bool(imported_from_host) or remote_root_prov
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
        imported_root=receipt_wt if (imported and receipt_wt) else None,
        project_slug=project_slug,
        receipt_worktree_root=receipt_wt,
        remote_provenance=remote_root_prov,
    )
    return attach_root(ctx, worktree_root) if need_root else ctx


def attach_root(ctx: RunContext, worktree_root: str | None = None) -> RunContext:
    """``ctx`` with its ``worktree_root`` resolved and checked. Refuses ``worktree_unknown`` (an imported run
    without ``worktree_root``, or a run with no recorded one), ``invalid_param`` (an override on a standard run
    that disagrees with its record) and ``worktree_missing`` (not a directory on this machine)."""
    root, source, imported_root = _resolve_root(
        ctx.events,
        worktree_root,
        imported=ctx.imported,
        receipt_worktree_root=ctx.receipt_worktree_root,
        remote_provenance=ctx.remote_provenance,
        project_slug=ctx.project_slug,
    )
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
