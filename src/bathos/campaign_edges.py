"""campaign_edges / run_edges: multi-parent campaign & run DAG (B2-03, #2181, fork 7).

Grounding: B2-03's gate text asks for a "multi-parent `campaign_edges` / `run_edges` table +
multi-`wasDerivedFrom` PROV emission" and frames it as "natural bathos schema evolution over
existing PROV lineage" -- bathos already has TWO single-parent mechanisms this module extends
rather than replaces:

- `campaigns.parent_campaign_id` (a plain TEXT column, stored/retrieved but not previously used
  for any DAG logic) -- the existing single-parent campaign-comparison mechanism (see
  `bathos.stats_gates`'s own docstring, which already relies on this field for baseline
  comparison).
- `Run.parent_run_id` (a plain TEXT column, consumed by `bathos.provenance.format_prov_json`'s
  existing single-parent `wasDerivedFrom` emission).

Ownership: bathos assembles/owns the campaign and run DAGs; a caller (e.g. a future xtrax loop
controller) emits run records carrying `campaign_id` + component sidecar refs and never
constructs or owns the DAG itself (B2-03's own "F-A / fork 7" framing).

Both edge tables are ADDITIVE, not a replacement for the existing single-parent columns --
`campaigns.parent_campaign_id` / `Run.parent_run_id` keep working exactly as before for any
node with no edges recorded (see `bathos.provenance.format_prov_json`'s `run_parent_edges`
parameter, which falls back to the single-parent field when a run has no entry).

Cycle rejection (B2-03's own explicit acceptance criterion: "campaign_edges round-trip +
cycle-rejection contract test -> exit 1 on any lossy round-trip or accepted cycle"): an edge is
stored as `(child_id, parent_id)` -- "child was derived from parent". A cycle would form by
adding an edge `(child, parent)` when `child` is ALREADY an ancestor of `parent` (i.e. some
existing chain already makes `parent` transitively derived from `child`) -- closing the loop
`child -> parent -> ... -> child`. `_would_create_cycle` checks this via a BFS over `parent`'s
own ancestor set before any insert; the check runs inside the same transaction as the insert
(single `db.execute` call sequence, no separate commit in between) so a concurrent writer can't
race between the check and the insert within one connection's session.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb


class CampaignEdgeError(Exception):
    """Base for campaign_edges/run_edges errors."""


class CycleRejectedError(CampaignEdgeError):
    """Adding this edge would create a cycle in the campaign or run DAG."""


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _resolve_edge_node_id(db, table: str, raw_id: str) -> str:
    """Resolve an unambiguous prefix against `table` (`campaigns` or `runs`), else keep `raw_id`."""
    if db is None or not raw_id:
        return raw_id
    try:
        exact = db.execute(
            f"SELECT id FROM {table} WHERE id = ? LIMIT 1",  # noqa: S608 — internal constant
            [raw_id],
        ).fetchall()
        if exact:
            return exact[0][0]
        matches = [
            r[0]
            for r in db.execute(
                f"SELECT DISTINCT id FROM {table} WHERE id LIKE ? || '%' ESCAPE '\\'",  # noqa: S608
                [_escape_like(raw_id)],
            ).fetchall()
            if r[0]
        ]
    except duckdb.Error:
        return raw_id
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        from bathos.campaigns import CampaignError

        entity_label = "campaign" if table == "campaigns" else "run"
        preview = ", ".join(m[:8] for m in sorted(matches))
        raise CampaignError(f"Ambiguous {entity_label} ID prefix {raw_id!r} matches: {preview}")
    return raw_id


@contextmanager
def _edge_write_lock(catalog_dir: Path) -> Iterator[None]:
    """Serialize log-mode cycle checks + `edge.added` emissions across processes."""
    lock_path = catalog_dir / "edges.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _uningested_edges(
    read_db,
    catalog_dir: Path | None,
    edge_type: str,
    resolve_table: str,
    cwd: Path | None = None,
) -> dict[str, set[str]]:
    """Read un-ingested `edge.added` / `edge.imported` events past `index.db` watermarks."""
    if catalog_dir is None:
        return {}
    from bathos.runlog.ingest import _read_new_lines, discover_roots
    from bathos.runlog.resolve import resolve_log_root

    watermarks: dict[tuple[str, str, str], int] = {}
    if read_db is not None:
        with contextlib.suppress(duckdb.Error):
            for rk, rid, fname, off in read_db.execute(
                "SELECT root_kind, root_id, path, byte_offset FROM ingest_watermarks"
            ).fetchall():
                watermarks[(rk, rid, fname)] = int(off or 0)

    roots = list(discover_roots(catalog_dir))
    seen_dirs = {log_dir.resolve() for _rk, _rid, log_dir in roots if log_dir.exists()}
    if cwd is not None:
        with contextlib.suppress(Exception):
            lr = resolve_log_root(cwd)
            if not lr.unaffiliated:
                proj_log_dir = lr.main_root / ".bth" / "log"
                if proj_log_dir.is_dir() and proj_log_dir.resolve() not in seen_dirs:
                    roots.append(("project", str(lr.main_root.resolve()), proj_log_dir))

    extra: dict[str, set[str]] = {}
    for root_kind, root_id, log_dir in roots:
        if not log_dir.is_dir():
            continue
        for seg in sorted(log_dir.glob("*.jsonl")):
            prev_offset = watermarks.get((root_kind, root_id, seg.name), 0)
            try:
                lines, _ = _read_new_lines(seg, prev_offset)
            except OSError:
                continue
            for _off, raw in lines:
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict) or obj.get("kind") not in ("edge.added", "edge.imported"):
                    continue
                data = obj.get("data") or {}
                entity = obj.get("entity") or []
                etype = data.get("type") or (entity[2] if len(entity) > 2 else None)
                if etype != edge_type:
                    continue
                raw_src = data.get("src") or (entity[0] if len(entity) >= 1 else None)
                raw_dst = data.get("dst") or (entity[1] if len(entity) >= 2 else None)
                if not isinstance(raw_src, str) or not isinstance(raw_dst, str) or not raw_src or not raw_dst:
                    continue
                try:
                    src = _resolve_edge_node_id(read_db, resolve_table, raw_src)
                except Exception:
                    src = raw_src
                try:
                    dst = _resolve_edge_node_id(read_db, resolve_table, raw_dst)
                except Exception:
                    dst = raw_dst
                extra.setdefault(src, set()).add(dst)
    return extra


def _ancestors(
    db,
    table: str,
    child_col: str,
    parent_col: str,
    start_id: str,
    extra_edges: dict[str, set[str]] | None = None,
) -> set[str]:
    """All ancestors of `start_id` (its parents, their parents, ...) via `table`'s
    child->parent edges, by breadth-first traversal."""
    visited: set[str] = set()
    frontier = [start_id]
    while frontier:
        current = frontier.pop()
        rows = db.execute(
            f"SELECT {parent_col} FROM {table} WHERE {child_col} = ?",  # noqa: S608 — table/col names are internal constants, never user input
            [current],
        ).fetchall()
        parents = [p for (p,) in rows]
        if extra_edges:
            parents.extend(extra_edges.get(current, ()))
        for p in parents:
            if p not in visited:
                visited.add(p)
                frontier.append(p)
    return visited


def _would_create_cycle(
    db,
    table: str,
    child_col: str,
    parent_col: str,
    child_id: str,
    parent_id: str,
    extra_edges: dict[str, set[str]] | None = None,
) -> bool:
    if child_id == parent_id:
        return True
    return child_id in _ancestors(
        db, table, child_col, parent_col, parent_id, extra_edges=extra_edges
    )


def _open_read_db(catalog_dir: Path | None):
    """Open a read-only catalog connection when a caller omitted `db`."""
    if catalog_dir is None:
        raise ValueError("Either db or catalog_dir must be provided")
    from bathos.index import connect_read

    return connect_read(catalog_dir, read_only=True, missing="empty")


def add_campaign_edge(
    db,
    child_campaign_id: str,
    parent_campaign_id: str,
    catalog_dir: Path | None = None,
    *,
    cwd: Path | None = None,
) -> None:
    """Record that `child_campaign_id` was (partly) derived from `parent_campaign_id`.

    Idempotent: inserting the same edge twice is a no-op (`ON CONFLICT DO NOTHING`), so a
    caller can safely re-assert an edge it isn't sure was already recorded.

    Raises:
        CycleRejectedError: this edge would create a cycle in the campaign DAG (including
            `child_campaign_id == parent_campaign_id`, a self-loop).

    AC-25: behind the runlog flag, emits `edge.added` instead of the INSERT;
    flag off performs only the legacy INSERT, unchanged. `cwd` resolves the
    runlog project/root when the flag is on (defaults to `Path.cwd()` via
    `resolve_log_root`).
    """
    from bathos.runlog.emit import emit_or_legacy, unit_of_work

    with unit_of_work(catalog_dir) as log_mode:
        lock_ctx = (
            _edge_write_lock(catalog_dir)
            if log_mode and catalog_dir is not None
            else contextlib.nullcontext()
        )
        with lock_ctx:
            owns_db = db is None
            read_db = _open_read_db(catalog_dir) if owns_db else db
            try:
                child_id = _resolve_edge_node_id(read_db, "campaigns", child_campaign_id)
                parent_id = _resolve_edge_node_id(read_db, "campaigns", parent_campaign_id)
                extra_edges = (
                    _uningested_edges(read_db, catalog_dir, "campaign", "campaigns", cwd=cwd)
                    if log_mode and catalog_dir is not None
                    else None
                )
                if _would_create_cycle(
                    read_db,
                    "campaign_edges",
                    "child_campaign_id",
                    "parent_campaign_id",
                    child_id,
                    parent_id,
                    extra_edges=extra_edges,
                ):
                    msg = (
                        f"adding edge {child_id!r} -> {parent_id!r} would create a "
                        "cycle in the campaign DAG"
                    )
                    raise CycleRejectedError(msg)
            finally:
                if owns_db:
                    read_db.close()

            def _legacy_write() -> None:
                if db is None:
                    raise ValueError("Legacy edge insert requires an open writable db connection")
                db.execute(
                    "INSERT INTO campaign_edges (child_campaign_id, parent_campaign_id) VALUES (?, ?) "
                    "ON CONFLICT DO NOTHING",
                    [child_id, parent_id],
                )

            emit_or_legacy(
                kind="edge.added",
                entity=[child_id, parent_id, "campaign"],
                data={"src": child_id, "dst": parent_id, "type": "campaign"},
                legacy_write=_legacy_write,
                cwd=cwd,
            )


def get_campaign_parents(
    db, campaign_id: str, catalog_dir: Path | None = None
) -> list[str]:
    """All recorded parent campaign IDs for `campaign_id`, sorted for a deterministic
    round-trip (insertion order is not preserved by this query)."""
    from bathos.runlog.mode import is_log_mode

    owns_db = db is None
    read_db = _open_read_db(catalog_dir) if owns_db else db
    try:
        resolved_id = _resolve_edge_node_id(read_db, "campaigns", campaign_id)
        rows = read_db.execute(
            "SELECT parent_campaign_id FROM campaign_edges WHERE child_campaign_id = ? "
            "ORDER BY parent_campaign_id",
            [resolved_id],
        ).fetchall()
        parents = {r[0] for r in rows}
        if catalog_dir is not None and is_log_mode(catalog_dir):
            extra = _uningested_edges(read_db, catalog_dir, "campaign", "campaigns")
            parents |= extra.get(resolved_id, set())
        return sorted(parents)
    finally:
        if owns_db:
            read_db.close()


def add_run_edge(
    db,
    child_run_id: str,
    parent_run_id: str,
    catalog_dir: Path | None = None,
    *,
    cwd: Path | None = None,
) -> None:
    """Record that `child_run_id` was (partly) derived from `parent_run_id`.

    Same idempotency and cycle-rejection contract as `add_campaign_edge`, applied to the run
    DAG instead of the campaign DAG.

    Raises:
        CycleRejectedError: this edge would create a cycle in the run DAG.

    AC-25: behind the runlog flag, emits `edge.added` instead of the INSERT;
    flag off performs only the legacy INSERT, unchanged.
    """
    from bathos.runlog.emit import emit_or_legacy, unit_of_work

    with unit_of_work(catalog_dir) as log_mode:
        lock_ctx = (
            _edge_write_lock(catalog_dir)
            if log_mode and catalog_dir is not None
            else contextlib.nullcontext()
        )
        with lock_ctx:
            owns_db = db is None
            read_db = _open_read_db(catalog_dir) if owns_db else db
            try:
                child_id = _resolve_edge_node_id(read_db, "runs", child_run_id)
                parent_id = _resolve_edge_node_id(read_db, "runs", parent_run_id)
                extra_edges = (
                    _uningested_edges(read_db, catalog_dir, "run", "runs", cwd=cwd)
                    if log_mode and catalog_dir is not None
                    else None
                )
                if _would_create_cycle(
                    read_db,
                    "run_edges",
                    "child_run_id",
                    "parent_run_id",
                    child_id,
                    parent_id,
                    extra_edges=extra_edges,
                ):
                    msg = (
                        f"adding edge {child_id!r} -> {parent_id!r} would create a cycle in the run DAG"
                    )
                    raise CycleRejectedError(msg)
            finally:
                if owns_db:
                    read_db.close()

            def _legacy_write() -> None:
                if db is None:
                    raise ValueError("Legacy edge insert requires an open writable db connection")
                db.execute(
                    "INSERT INTO run_edges (child_run_id, parent_run_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                    [child_id, parent_id],
                )

            emit_or_legacy(
                kind="edge.added",
                entity=[child_id, parent_id, "run"],
                data={"src": child_id, "dst": parent_id, "type": "run"},
                legacy_write=_legacy_write,
                cwd=cwd,
            )


def get_run_parents(db, run_id: str, catalog_dir: Path | None = None) -> list[str]:
    """All recorded parent run IDs for `run_id`, sorted for a deterministic round-trip."""
    from bathos.runlog.mode import is_log_mode

    owns_db = db is None
    read_db = _open_read_db(catalog_dir) if owns_db else db
    try:
        resolved_id = _resolve_edge_node_id(read_db, "runs", run_id)
        rows = read_db.execute(
            "SELECT parent_run_id FROM run_edges WHERE child_run_id = ? ORDER BY parent_run_id",
            [resolved_id],
        ).fetchall()
        parents = {r[0] for r in rows}
        if catalog_dir is not None and is_log_mode(catalog_dir):
            extra = _uningested_edges(read_db, catalog_dir, "run", "runs")
            parents |= extra.get(resolved_id, set())
        return sorted(parents)
    finally:
        if owns_db:
            read_db.close()


__all__ = [
    "CampaignEdgeError",
    "CycleRejectedError",
    "add_campaign_edge",
    "add_run_edge",
    "get_campaign_parents",
    "get_run_parents",
]
