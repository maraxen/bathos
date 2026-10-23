"""``bth outputs hash`` and ``bth outputs adopt`` (spec 261023_output-tiering.md sections 3.3, 3.4, 4).

* :func:`hash_outputs` re-hashes the live files under a run's worktree root against ``reference_hashes`` and
  reports ``match`` / ``drift`` / ``missing`` per path. Read-only.
* :func:`adopt_outputs` hashes the given roots NOW and records them as ``run.outputs_hashed`` with
  ``phase: "adopted"``, but only paths that have no ``run_end`` / ``run_end_legacy`` entry. It exists for runs that
  predate the standard (the r6/r7 ``*_cells/`` directories); it is closed to a run that declared ``[outputs]``
  (``adopt_closed``) and it never re-references a path whose live bytes differ from what the run recorded
  (``run_end_conflict``, D5: re-run instead).
* :func:`retract_adopt` records ``run.outputs_adopt_retracted`` for one adopt event, and the fold drops its entries.

Every refusal is an :class:`~bathos.output_tiers.reference.OutputTierError` whose ``error_code`` is the JSON
``error_code`` a caller branches on. Nothing here deletes or modifies a file.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from bathos.output_tiers.reference import (
    PHASE_ADOPTED,
    PHASE_RUN_END,
    PHASE_RUN_END_LEGACY,
    OutputTierError,
    ReferenceEntry,
    expand_roots,
    hash_file,
    normalize_relpath,
    reference_hashes,
    unresolved_legacy_paths,
)
from bathos.output_tiers.run_context import (
    INVALID_PARAM,
    RunContext,
    attach_root,
    is_terminal,
    open_run,
    require_log_mode,
)

NO_REFERENCE_HASHES = "no_reference_hashes"
RUN_NOT_TERMINAL = "run_not_terminal"
ROOT_OUTSIDE_WORKTREE = "root_outside_worktree"
ROOT_MISSING = "root_missing"
RUN_END_CONFLICT = "run_end_conflict"
ADOPT_CLOSED = "adopt_closed"
ADOPT_EVENT_NOT_FOUND = "adopt_event_not_found"
LEGACY_PATH_UNRESOLVED = "legacy_path_unresolved"

#: How many offending paths a conflict message names before it summarises the rest.
_NAMED_PATHS = 5


def _named(paths: list[str]) -> str:
    shown = ", ".join(paths[:_NAMED_PATHS])
    more = len(paths) - _NAMED_PATHS
    return f"{shown} (+{more} more)" if more > 0 else shown


def _check_legacy_resolvable(ctx: RunContext) -> None:
    unresolved = unresolved_legacy_paths(ctx.events, imported_root=ctx.imported_root)
    if unresolved:
        raise OutputTierError(
            LEGACY_PATH_UNRESOLVED,
            f"{_named(unresolved)}: cannot be placed under worktree root {ctx.worktree_root}",
        )


def _log_cwd(ctx: RunContext, fallback: Path | None) -> Path:
    """Where ``emit_event`` resolves the project log from: the run's own ``main_root`` (so the event lands in the
    log that holds the run, whatever directory the command was started from), else its worktree root, else the
    caller's cwd."""
    candidates: list[str | None] = [
        next(
            (
                ev["main_root"]
                for ev in sorted(ctx.events, key=lambda e: e.get("ts", ""))
                if ev.get("main_root")
            ),
            None,
        ),
        str(ctx.worktree_root) if ctx.worktree_root else None,
    ]
    for c in candidates:
        if c and Path(c).is_dir():
            return Path(c)
    return fallback or Path.cwd()


# --- hash ---------------------------------------------------------------------------------------


def _live_state(root: Path, entry: ReferenceEntry) -> dict[str, Any]:
    """``match`` / ``drift`` / ``missing`` for one reference entry against the live tree. Never follows a link."""
    p = root / entry.relpath
    out: dict[str, Any] = {
        "relpath": entry.relpath,
        "phase": entry.phase,
        "reference_sha256": entry.sha256,
        "origin_eid": entry.origin_eid,
    }
    try:
        st = os.lstat(p)
    except (FileNotFoundError, NotADirectoryError):
        return {**out, "status": "missing", "live_sha256": None}
    except OSError as e:
        return {**out, "status": "drift", "live_sha256": None, "detail": f"unreadable: {e}"}
    if not stat.S_ISREG(st.st_mode):
        kind = "symlink" if stat.S_ISLNK(st.st_mode) else "not a regular file"
        return {**out, "status": "drift", "live_sha256": None, "detail": kind}
    if Path(os.path.realpath(p)) != Path(os.path.realpath(root)) / entry.relpath:
        return {
            **out,
            "status": "drift",
            "live_sha256": None,
            "detail": "a component of the path is a symlink",
        }
    try:
        live = hash_file(p)
    except OSError as e:
        return {**out, "status": "drift", "live_sha256": None, "detail": f"unreadable: {e}"}
    return {
        **out,
        "status": "match" if live == entry.sha256 else "drift",
        "live_sha256": live,
        "size_bytes": st.st_size,
    }


def hash_outputs(
    catalog_dir: Path, run_prefix: str, *, worktree_root: str | None = None
) -> dict[str, Any]:
    """Re-hash the live files of one run against its reference set. Writes no event.

    Refuses ``no_reference_hashes`` when the run has none, and ``legacy_path_unresolved`` when a pre-standard
    entry cannot be placed under the worktree root (``reference_hashes`` would otherwise drop it silently).
    """
    ctx = open_run(catalog_dir, run_prefix, worktree_root=worktree_root)
    assert ctx.worktree_root is not None
    _check_legacy_resolvable(ctx)
    ref = reference_hashes(ctx.events, imported_root=ctx.imported_root)
    if not ref:
        raise OutputTierError(NO_REFERENCE_HASHES, ctx.run_id)
    files = [_live_state(ctx.worktree_root, ref[rel]) for rel in sorted(ref)]
    counts = {s: sum(1 for f in files if f["status"] == s) for s in ("match", "drift", "missing")}
    return {
        "ok": True,
        "run_id": ctx.run_id,
        "worktree_root": str(ctx.worktree_root),
        "worktree_root_source": ctx.root_source,
        "n_reference": len(files),
        "counts": counts,
        "all_match": counts["drift"] == 0 and counts["missing"] == 0,
        "files": files,
    }


# --- adopt --------------------------------------------------------------------------------------


def _root_relpath(root_arg: str, worktree_root: Path) -> str:
    """POSIX relpath of one ``--root`` under ``worktree_root``. A relative argument resolves against the run's
    worktree root (as a sidecar's ``[outputs] roots`` do), an absolute one is taken as is. Lexical: ``..`` is
    folded and links are not followed (expansion refuses links afterwards)."""
    rel = normalize_relpath(root_arg, worktree_root)
    if rel is None:
        if Path(os.path.normpath(Path(worktree_root) / root_arg)) == Path(
            os.path.normpath(worktree_root)
        ):
            raise OutputTierError(
                INVALID_PARAM,
                f"--root {root_arg!r} is the worktree root itself; name a path under it",
            )
        raise OutputTierError(ROOT_OUTSIDE_WORKTREE, f"{root_arg} is not under {worktree_root}")
    return rel


def adopt_outputs(
    catalog_dir: Path,
    run_prefix: str,
    roots: list[str],
    reason: str,
    *,
    worktree_root: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Hash ``roots`` now and record the paths that have no ``run_end`` / ``run_end_legacy`` entry.

    Refusal order: ``requires_log_mode``, run prefix, ``adopt_closed``, ``run_not_terminal``, the worktree root
    (``worktree_unknown`` / ``worktree_missing``), ``legacy_path_unresolved``, ``root_outside_worktree``,
    ``symlink_in_outputs`` / ``special_file_in_outputs``, ``root_missing``, then ``run_end_conflict`` (a path whose
    live bytes differ from its ``run_end`` or sha-bearing legacy entry; the whole adopt is refused, nothing is
    written). Paths whose live bytes equal their existing reference are reported under ``skipped`` and left out of
    the event. With ``dry_run`` nothing is written; the result lists the files and the byte total.
    """
    from bathos.runlog.emit import emit_event, unit_of_work

    if not roots:
        raise OutputTierError(INVALID_PARAM, "at least one --root is required")
    if not reason or not reason.strip():
        raise OutputTierError(INVALID_PARAM, "--reason is required")

    with unit_of_work(catalog_dir) as log_mode:
        if not log_mode:
            require_log_mode(catalog_dir)
        ctx = open_run(catalog_dir, run_prefix, need_root=False)
        if ctx.outputs_declared:
            raise OutputTierError(
                ADOPT_CLOSED,
                f"run {ctx.run_id} declared [outputs] roots; its reference set is closed to adopt",
            )
        if not is_terminal(ctx.status):
            raise OutputTierError(RUN_NOT_TERMINAL, f"run {ctx.run_id} has status {ctx.status!r}")
        ctx = attach_root(ctx, worktree_root)
        wt = ctx.worktree_root
        assert wt is not None
        _check_legacy_resolvable(ctx)

        rels = [_root_relpath(r, wt) for r in roots]
        files, missing_roots = expand_roots(rels, wt)
        if missing_roots:
            raise OutputTierError(ROOT_MISSING, _named(missing_roots))

        ref = reference_hashes(ctx.events, imported_root=ctx.imported_root)
        entries: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        conflicts: list[str] = []
        for f in files:
            try:
                sha = hash_file(f.path)
                st = f.path.stat()
            except OSError as e:
                raise OutputTierError(INVALID_PARAM, f"cannot read {f.relpath}: {e}") from e
            existing = ref.get(f.relpath)
            if existing is not None and existing.phase in (PHASE_RUN_END, PHASE_RUN_END_LEGACY):
                if existing.sha256 == sha:
                    skipped.append({"relpath": f.relpath, "reason": f"already_{existing.phase}"})
                else:
                    conflicts.append(f.relpath)
                continue
            if existing is not None and existing.phase == PHASE_ADOPTED and existing.sha256 == sha:
                skipped.append({"relpath": f.relpath, "reason": "already_adopted"})
                continue
            entries.append(
                {
                    "path": str(f.path),
                    "status": "present",
                    "size_bytes": st.st_size,
                    "mtime_unix": st.st_mtime,
                    "sha256": sha,
                    "relpath": f.relpath,
                    "phase": PHASE_ADOPTED,
                }
            )
        if conflicts:
            raise OutputTierError(
                RUN_END_CONFLICT,
                f"{_named(conflicts)}: live bytes differ from the hash recorded for run {ctx.run_id}; "
                "re-run instead of adopting",
            )

        total = sum(e["size_bytes"] for e in entries)
        result: dict[str, Any] = {
            "ok": True,
            "run_id": ctx.run_id,
            "worktree_root": str(wt),
            "worktree_root_source": ctx.root_source,
            "dry_run": dry_run,
            "roots": rels,
            "n_files": len(entries),
            "total_bytes": total,
            "files": [e["relpath"] for e in entries],
            "skipped": skipped,
            "eid": None,
        }
        if dry_run or not entries:
            return result
        outcome = emit_event(
            kind="run.outputs_hashed",
            entity=[ctx.run_id],
            data={
                "phase": PHASE_ADOPTED,
                "output_metadata": entries,
                "roots": rels,
                "reason": reason,
            },
            cwd=_log_cwd(ctx, None),
            hard_fail=True,
            worktree_root=wt,
        )
        result["eid"] = (outcome.envelope or {}).get("eid")
        return result


# --- retract ------------------------------------------------------------------------------------


def retract_adopt(
    catalog_dir: Path,
    run_prefix: str,
    eid: str,
    reason: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Record ``run.outputs_adopt_retracted`` for the adopt event ``eid`` of this run.

    The fold then drops that event's entries from ``adopted_outputs`` and ``reference_hashes``; a path that an
    earlier, still-live adopt also covered falls back to that one. Refuses ``adopt_event_not_found`` when ``eid``
    is not a live ``phase: "adopted"`` event of this run (unknown, another run's, or already retracted).
    """
    from bathos.runlog.emit import emit_event, unit_of_work

    if not eid:
        raise OutputTierError(INVALID_PARAM, "an adopt event eid is required")
    if not reason or not reason.strip():
        raise OutputTierError(INVALID_PARAM, "--reason is required")

    with unit_of_work(catalog_dir) as log_mode:
        if not log_mode:
            require_log_mode(catalog_dir)
        ctx = open_run(catalog_dir, run_prefix, need_root=False)
        target = next(
            (
                ev
                for ev in ctx.events
                if ev.get("eid") == eid
                and ev.get("kind") == "run.outputs_hashed"
                and (ev.get("data") or {}).get("phase") == PHASE_ADOPTED
            ),
            None,
        )
        if target is None:
            raise OutputTierError(
                ADOPT_EVENT_NOT_FOUND, f"{eid} is not an adopt event of run {ctx.run_id}"
            )
        if any(
            ev.get("kind") == "run.outputs_adopt_retracted"
            and (ev.get("data") or {}).get("retracts") == eid
            for ev in ctx.events
        ):
            raise OutputTierError(ADOPT_EVENT_NOT_FOUND, f"{eid} is already retracted")
        relpaths = sorted(
            str(e.get("relpath"))
            for e in (target.get("data") or {}).get("output_metadata") or []
            if isinstance(e, dict) and e.get("relpath")
        )
        result: dict[str, Any] = {
            "ok": True,
            "run_id": ctx.run_id,
            "retracts": eid,
            "dry_run": dry_run,
            "n_files": len(relpaths),
            "files": relpaths,
            "eid": None,
        }
        if dry_run:
            return result
        recorded_root = target.get("worktree_root")
        outcome = emit_event(
            kind="run.outputs_adopt_retracted",
            entity=[ctx.run_id],
            data={"retracts": eid, "reason": reason},
            cwd=_log_cwd(ctx, None),
            hard_fail=True,
            worktree_root=Path(recorded_root) if recorded_root else None,
        )
        result["eid"] = (outcome.envelope or {}).get("eid")
        return result
