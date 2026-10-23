"""The reference hash set of a run's outputs (spec 261023_output-tiering.md sections 3.1 to 3.4).

Four things live here, and nothing else imports them from anywhere less basic than the standard library:

* :func:`hash_file`: the one sha256 of a file, streaming, with no size cap. ``compact._collect_output_metadata``
  (the run-end hash, the checker's drift re-hash, readback's freshness check) delegates to it, so a 150 MiB file is
  compared rather than reported unreadable.
* :func:`expand_roots`: a sidecar's ``[outputs] roots`` expanded to regular files under the live worktree root.
  A symlink, FIFO, socket, device, or anything that does not sit under the root by a plain path refuses with
  ``symlink_in_outputs`` / ``special_file_in_outputs``.
* :func:`run_end_payload`: the ``run.outputs_hashed`` data a run emits at its end, legacy keys plus ``relpath``
  and ``phase``.
* :func:`reference_hashes`: which entry is THE reference for each path of a run (spec 3.3 precedence), computed from
  the run's events. ``run_end`` beats ``run_end_legacy`` beats ``adopted``; null-sha entries count as absent;
  rebaseline events never count.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

HASH_CHUNK_BYTES = 1 << 20

#: ``phase`` values carried on ``run.outputs_hashed`` events and on their entries.
PHASE_RUN_END = "run_end"
PHASE_ADOPTED = "adopted"
PHASE_REBASELINE = "rebaseline"
#: Not a stored phase: the label a phase-less pre-standard entry gets in a reference set.
PHASE_RUN_END_LEGACY = "run_end_legacy"

#: Refusal codes (JSON ``error_code`` values).
SYMLINK_IN_OUTPUTS = "symlink_in_outputs"
SPECIAL_FILE_IN_OUTPUTS = "special_file_in_outputs"
RUN_NOT_FOUND = "run_not_found"
RUN_PREFIX_AMBIGUOUS = "run_prefix_ambiguous"


class OutputTierError(ValueError):
    """A refusal with a stable JSON ``error_code``. ``detail`` names the offending path or run id."""

    def __init__(self, error_code: str, detail: str) -> None:
        super().__init__(f"{error_code}: {detail}")
        self.error_code = error_code
        self.detail = detail


def hash_file(path: str | os.PathLike[str]) -> str:
    """sha256 hex digest of ``path``, read in 1 MiB chunks. No size cap; raises ``OSError`` if unreadable."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(HASH_CHUNK_BYTES):
            h.update(chunk)
    return h.hexdigest()


# --- relpaths -----------------------------------------------------------------------------------


def normalize_relpath(
    path: str | os.PathLike[str], fs_root: Path, *, base: Path | None = None
) -> str | None:
    """POSIX path of ``path`` relative to ``fs_root``; ``None`` if it does not lie under it.

    A relative ``path`` is taken relative to ``base`` (default ``fs_root``). Purely lexical (``..`` is folded, links
    are not followed), because a recorded path must normalise the same whether or not the file still exists.
    """
    p = Path(path)
    if not p.is_absolute():
        p = (base or fs_root) / p
    norm = Path(os.path.normpath(p))
    root = Path(os.path.normpath(fs_root))
    try:
        rel = norm.relative_to(root)
    except ValueError:
        return None
    if rel == Path("."):
        return None
    return rel.as_posix()


def check_root_syntax(value: str) -> str | None:
    """Why ``value`` is not a usable ``[outputs] roots`` entry, or ``None`` if it is."""
    if not isinstance(value, str) or not value:
        return "must be a non-empty string"
    if "\\" in value:
        return "must be a POSIX path (no backslashes)"
    pp = PurePosixPath(value)
    if pp.is_absolute():
        return "must be relative to the worktree root, not absolute"
    if ".." in pp.parts:
        return "must stay inside the worktree root (no '..')"
    if not pp.parts or pp.parts == (".",):
        return "must name a file or directory under the worktree root"
    return None


# --- root expansion -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpandedOutput:
    """One regular file found under a declared root. ``path`` is under ``fs_root`` (never ``realpath``'d)."""

    path: Path
    relpath: str


def _refuse_kind(mode: int) -> str:
    return SYMLINK_IN_OUTPUTS if stat.S_ISLNK(mode) else SPECIAL_FILE_IN_OUTPUTS


def _check_plain_path(fs_root: Path, candidate: Path, relpath: str) -> None:
    """Refuse if any component between ``fs_root`` and ``candidate`` is a link, i.e. the file does not sit
    under ``fs_root`` by the path it was declared with (this also covers a path that resolves outside it)."""
    expected = Path(os.path.realpath(fs_root)) / relpath
    if Path(os.path.realpath(candidate)) != expected:
        raise OutputTierError(
            SYMLINK_IN_OUTPUTS, f"{relpath} (a component of the path is a symlink)"
        )


def expand_roots(roots: Iterable[str], fs_root: Path) -> tuple[list[ExpandedOutput], list[str]]:
    """Expand declared roots (files or directories, relative POSIX paths) to regular files.

    Returns ``(files, missing_roots)``. ``files`` is sorted by relpath and de-duplicated across overlapping roots;
    ``missing_roots`` are declared roots that do not exist (not a refusal: the script may legitimately not have
    written them, and the record will say so). Raises :class:`OutputTierError` for the first symlink, FIFO,
    socket, device, or path that is not plainly under ``fs_root``.
    """
    found: dict[str, ExpandedOutput] = {}
    missing: list[str] = []
    for root in roots:
        problem = check_root_syntax(root)
        if problem:
            raise OutputTierError("invalid_param", f"output root {root!r} {problem}")
        top = fs_root / root
        rel_top = PurePosixPath(root).as_posix()
        try:
            top_stat = os.lstat(top)
        except FileNotFoundError:
            missing.append(rel_top)
            continue
        if stat.S_ISLNK(top_stat.st_mode):
            raise OutputTierError(SYMLINK_IN_OUTPUTS, rel_top)
        _check_plain_path(fs_root, top, rel_top)
        if stat.S_ISREG(top_stat.st_mode):
            found.setdefault(rel_top, ExpandedOutput(top, rel_top))
            continue
        if not stat.S_ISDIR(top_stat.st_mode):
            raise OutputTierError(_refuse_kind(top_stat.st_mode), rel_top)
        for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
            for name in sorted(dirnames) + sorted(filenames):
                full = Path(dirpath) / name
                mode = os.lstat(full).st_mode
                rel = (PurePosixPath(rel_top) / full.relative_to(top).as_posix()).as_posix()
                if stat.S_ISDIR(mode):
                    continue  # walked into by os.walk; not itself an output
                if not stat.S_ISREG(mode):
                    raise OutputTierError(_refuse_kind(mode), rel)
                found.setdefault(rel, ExpandedOutput(full, rel))
    return [found[k] for k in sorted(found)], missing


# --- the run-end event --------------------------------------------------------------------------


def file_metadata(path: str | os.PathLike[str]) -> dict[str, Any]:
    """``{status, size_bytes, mtime_unix, sha256}`` for one path, with the sha computed by :func:`hash_file`.

    ``status`` is ``present`` / ``missing`` / ``unreadable``. A directory is ``present`` with ``sha256 = None``
    (nothing is walked here), a file that cannot be read is ``present`` with ``sha256 = None``.
    """
    p = Path(path)
    try:
        if not p.exists():
            return {"status": "missing", "size_bytes": 0}
        st = p.stat()
        sha: str | None = None
        if p.is_file():
            try:
                sha = hash_file(p)
            except OSError:
                sha = None
        return {
            "status": "present",
            "size_bytes": st.st_size,
            "mtime_unix": st.st_mtime,
            "sha256": sha,
        }
    except OSError:
        return {"status": "unreadable", "size_bytes": 0}


def run_end_payload(
    output_paths: Iterable[str],
    roots: Iterable[str],
    *,
    fs_root: Path,
    cwd: Path,
) -> dict[str, Any]:
    """The ``data`` of the run-end ``run.outputs_hashed`` event (spec 3.1).

    Every entry keeps the legacy keys (``path``, ``status``, ``size_bytes``, ``mtime_unix``, ``sha256``) and adds
    ``relpath`` (POSIX, relative to ``fs_root``; ``None`` for a path outside it) and ``phase: "run_end"``.
    ``output_paths`` entries come first, as recorded (a relative one resolves against ``cwd``, where the script
    ran); declared ``roots`` expand after them, and a file seen twice is listed once.

    A root that cannot be expanded is a refusal: the payload carries ``refused`` (the code) and ``refused_detail``
    and NO entries, so the run has no reference set (the tombstone of spec 3.3).
    """
    roots = list(roots)
    try:
        expanded, missing_roots = expand_roots(roots, fs_root) if roots else ([], [])
    except OutputTierError as e:
        return {
            "phase": PHASE_RUN_END,
            "output_metadata": [],
            "refused": e.error_code,
            "refused_detail": e.detail,
        }

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in output_paths:
        rel = normalize_relpath(p, fs_root, base=cwd)
        if rel is not None:
            if rel in seen:
                continue
            seen.add(rel)
        target = Path(p) if Path(p).is_absolute() else cwd / p
        entries.append({"path": p, **file_metadata(target), "relpath": rel, "phase": PHASE_RUN_END})
    for item in expanded:
        if item.relpath in seen:
            continue
        seen.add(item.relpath)
        entries.append(
            {
                "path": str(item.path),
                **file_metadata(item.path),
                "relpath": item.relpath,
                "phase": PHASE_RUN_END,
            }
        )
    for rel in missing_roots:
        if rel in seen:
            continue
        seen.add(rel)
        entries.append(
            {
                "path": str(fs_root / rel),
                **file_metadata(fs_root / rel),
                "relpath": rel,
                "phase": PHASE_RUN_END,
            }
        )
    return {"phase": PHASE_RUN_END, "output_metadata": entries}


# --- reference precedence -----------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceEntry:
    """The reference for one path of a run. ``phase`` is ``run_end``, ``run_end_legacy`` or ``adopted``."""

    relpath: str
    sha256: str
    size_bytes: int | None
    phase: str
    origin_eid: str
    worktree_root: str
    path: str


def _ev_key(ev: dict) -> tuple[str, str]:
    return (ev.get("ts", ""), ev.get("eid", ""))


def _entries_of(value: Any) -> list[dict]:
    """``output_metadata`` as a list of dicts, whether it arrived as JSON text or as a list."""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value else []
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [e for e in value if isinstance(e, dict)]


def legacy_relpath(path: str, worktree_root: str) -> str | None:
    """Relpath of a pre-standard entry's ``path``: minus ``worktree_root`` if absolute, itself if relative.

    ``None`` when an absolute path lies outside ``worktree_root`` (the caller reports ``legacy_path_unresolved``).
    """
    if not path:
        return None
    if Path(path).is_absolute():
        return normalize_relpath(path, Path(worktree_root))
    norm = os.path.normpath(path)
    if norm.startswith("..") or norm == ".":
        return None
    return Path(norm).as_posix()


def _event_root(ev: dict, imported_root: str | None) -> str:
    """The worktree root a relpath of ``ev`` resolves against. A ``run.imported`` event's own root is untrusted
    (the importer defaults it to the catalog dir, spec 3.3), so a caller-supplied ``imported_root`` replaces it."""
    if imported_root and ev.get("kind") == "run.imported":
        return imported_root
    return ev.get("worktree_root") or ""


def _event_entries(
    ev: dict, phase_label: str, imported_root: str | None = None
) -> Iterable[ReferenceEntry]:
    root = _event_root(ev, imported_root)
    for e in _entries_of((ev.get("data") or {}).get("output_metadata")):
        sha = e.get("sha256")
        if not sha:  # null sha (>=100 MiB legacy, directory, unreadable) counts as absent
            continue
        rel = e.get("relpath")
        if not rel:
            rel = legacy_relpath(str(e.get("path", "")), root)
        if not rel:
            continue
        yield ReferenceEntry(
            relpath=rel,
            sha256=sha,
            size_bytes=e.get("size_bytes"),
            phase=phase_label,
            origin_eid=ev.get("eid", ""),
            worktree_root=root,
            path=str(e.get("path", "")),
        )


def _live_adopted_events(events: list[dict]) -> list[dict]:
    """``phase: "adopted"`` events not retracted by a ``run.outputs_adopt_retracted`` event (``data.retracts`` is
    the retracted event's ``eid``; the command that emits it is a later step)."""
    retracted = {
        (ev.get("data") or {}).get("retracts")
        for ev in events
        if ev.get("kind") == "run.outputs_adopt_retracted"
    }
    return [
        ev
        for ev in events
        if ev.get("kind") == "run.outputs_hashed"
        and (ev.get("data") or {}).get("phase") == PHASE_ADOPTED
        and ev.get("eid") not in retracted
    ]


def fold_adopted(events: list[dict]) -> list[dict]:
    """The latest non-retracted adopted entry per relpath, as stored in ``runs.adopted_outputs`` (a list of the
    event's entries plus ``origin_eid`` and ``worktree_root``). Order of ``events`` does not matter."""
    latest: dict[str, tuple[tuple[str, str], dict]] = {}
    for ev in _live_adopted_events(events):
        for e in _entries_of((ev.get("data") or {}).get("output_metadata")):
            rel = e.get("relpath")
            if not rel:
                continue
            key = _ev_key(ev)
            if rel not in latest or key > latest[rel][0]:
                latest[rel] = (
                    key,
                    {
                        **e,
                        "origin_eid": ev.get("eid", ""),
                        "worktree_root": ev.get("worktree_root"),
                    },
                )
    return [latest[r][1] for r in sorted(latest)]


def _legacy_events(events: list[dict]) -> list[dict]:
    """The one event (as a 0 or 1 element list) that is the pre-standard reference: ``run.imported`` carrying a
    non-empty ``output_metadata`` (latest wins), else the earliest phase-less ``run.outputs_hashed`` that
    precedes ``run.finished``. A phase-less event after ``run.finished`` is a pre-standard rebaseline."""
    imported = [
        ev
        for ev in events
        if ev.get("kind") == "run.imported"
        and _entries_of((ev.get("data") or {}).get("output_metadata"))
    ]
    if imported:
        return [max(imported, key=_ev_key)]
    finished = [_ev_key(ev) for ev in events if ev.get("kind") == "run.finished"]
    deadline = min(finished)[0] if finished else None
    phaseless = [
        ev
        for ev in events
        if ev.get("kind") == "run.outputs_hashed"
        and "phase" not in (ev.get("data") or {})
        and (deadline is None or ev.get("ts", "") < deadline)
    ]
    return [min(phaseless, key=_ev_key)] if phaseless else []


def unresolved_legacy_paths(events: list[dict], *, imported_root: str | None = None) -> list[str]:
    """``path`` of every pre-standard entry that has a sha but whose relpath cannot be derived (an absolute path
    outside the worktree root, or a relative one that escapes it). :func:`reference_hashes` drops such entries
    silently; the tiering commands refuse with ``legacy_path_unresolved`` naming them (spec 3.3)."""
    out: list[str] = []
    for ev in _legacy_events(events):
        root = _event_root(ev, imported_root)
        for e in _entries_of((ev.get("data") or {}).get("output_metadata")):
            if not e.get("sha256") or e.get("relpath"):
                continue
            if not legacy_relpath(str(e.get("path", "")), root):
                out.append(str(e.get("path", "")))
    return sorted(set(out))


def reference_hashes(
    events: list[dict], *, imported_root: str | None = None
) -> dict[str, ReferenceEntry]:
    """``{relpath: ReferenceEntry}`` for one run, from that run's events (spec 3.3).

    Per path: the ``run_end`` entry if any, else the ``run_end_legacy`` entry if any, else the adopted entry.
    Entries without a sha256 count as absent. ``phase: "rebaseline"`` events never contribute. ``events`` may be in
    any order, and may contain other kinds (ignored). ``imported_root`` is the ``--worktree-root`` a caller
    supplied for a ``run.imported`` run; it replaces that event's own (untrusted) root when deriving relpaths.
    """
    result: dict[str, ReferenceEntry] = {}

    run_end = sorted(
        (
            ev
            for ev in events
            if ev.get("kind") == "run.outputs_hashed"
            and (ev.get("data") or {}).get("phase") == PHASE_RUN_END
        ),
        key=_ev_key,
    )
    for ev in run_end:  # a later run_end event for the same path does not replace the first
        for entry in _event_entries(ev, PHASE_RUN_END):
            result.setdefault(entry.relpath, entry)

    for ev in _legacy_events(events):
        for entry in _event_entries(ev, PHASE_RUN_END_LEGACY, imported_root):
            result.setdefault(entry.relpath, entry)

    for e in fold_adopted(events):
        rel = e["relpath"]
        if rel in result or not e.get("sha256"):
            continue
        result[rel] = ReferenceEntry(
            relpath=rel,
            sha256=e["sha256"],
            size_bytes=e.get("size_bytes"),
            phase=PHASE_ADOPTED,
            origin_eid=e.get("origin_eid", ""),
            worktree_root=e.get("worktree_root") or "",
            path=str(e.get("path", "")),
        )
    return result


def resolve_run_id(prefix: str, run_ids: Iterable[str]) -> str:
    """Resolve a unique run-id prefix to the full id. 0 or more than 1 match refuses (``run_not_found`` /
    ``run_prefix_ambiguous``)."""
    if not prefix:
        raise OutputTierError(RUN_NOT_FOUND, "empty run id")
    matches = sorted({r for r in run_ids if r.startswith(prefix)})
    if not matches:
        raise OutputTierError(RUN_NOT_FOUND, prefix)
    if len(matches) > 1:
        raise OutputTierError(RUN_PREFIX_AMBIGUOUS, f"{prefix} matches {len(matches)} runs")
    return matches[0]


def read_adopted_outputs(con: Any, run_id: str) -> list[dict]:
    """The folded ``runs.adopted_outputs`` of one run, read through a ``connect_read`` connection.

    An ``index.db`` built before this column existed has no ``adopted_outputs`` until its next ingest
    (spec section 5.3, "Existing index.db files"), so a missing column reads as absent: ``[]``, never a
    ``CatalogException``.
    """
    cols = {
        r[0]
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'runs'"
        ).fetchall()
    }
    if "adopted_outputs" not in cols:
        return []
    row = con.execute("SELECT adopted_outputs FROM runs WHERE id = ?", [run_id]).fetchone()
    return _entries_of(row[0]) if row and row[0] else []
