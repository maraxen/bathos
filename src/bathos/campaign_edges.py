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


class CampaignEdgeError(Exception):
    """Base for campaign_edges/run_edges errors."""


class CycleRejectedError(CampaignEdgeError):
    """Adding this edge would create a cycle in the campaign or run DAG."""


def _ancestors(db, table: str, child_col: str, parent_col: str, start_id: str) -> set[str]:
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
        for (p,) in rows:
            if p not in visited:
                visited.add(p)
                frontier.append(p)
    return visited


def _would_create_cycle(
    db, table: str, child_col: str, parent_col: str, child_id: str, parent_id: str
) -> bool:
    if child_id == parent_id:
        return True
    return child_id in _ancestors(db, table, child_col, parent_col, parent_id)


def add_campaign_edge(db, child_campaign_id: str, parent_campaign_id: str) -> None:
    """Record that `child_campaign_id` was (partly) derived from `parent_campaign_id`.

    Idempotent: inserting the same edge twice is a no-op (`ON CONFLICT DO NOTHING`), so a
    caller can safely re-assert an edge it isn't sure was already recorded.

    Raises:
        CycleRejectedError: this edge would create a cycle in the campaign DAG (including
            `child_campaign_id == parent_campaign_id`, a self-loop).
    """
    if _would_create_cycle(
        db,
        "campaign_edges",
        "child_campaign_id",
        "parent_campaign_id",
        child_campaign_id,
        parent_campaign_id,
    ):
        msg = (
            f"adding edge {child_campaign_id!r} -> {parent_campaign_id!r} would create a "
            "cycle in the campaign DAG"
        )
        raise CycleRejectedError(msg)
    db.execute(
        "INSERT INTO campaign_edges (child_campaign_id, parent_campaign_id) VALUES (?, ?) "
        "ON CONFLICT DO NOTHING",
        [child_campaign_id, parent_campaign_id],
    )


def get_campaign_parents(db, campaign_id: str) -> list[str]:
    """All recorded parent campaign IDs for `campaign_id`, sorted for a deterministic
    round-trip (insertion order is not preserved by this query)."""
    rows = db.execute(
        "SELECT parent_campaign_id FROM campaign_edges WHERE child_campaign_id = ? "
        "ORDER BY parent_campaign_id",
        [campaign_id],
    ).fetchall()
    return [r[0] for r in rows]


def add_run_edge(db, child_run_id: str, parent_run_id: str) -> None:
    """Record that `child_run_id` was (partly) derived from `parent_run_id`.

    Same idempotency and cycle-rejection contract as `add_campaign_edge`, applied to the run
    DAG instead of the campaign DAG.

    Raises:
        CycleRejectedError: this edge would create a cycle in the run DAG.
    """
    if _would_create_cycle(
        db, "run_edges", "child_run_id", "parent_run_id", child_run_id, parent_run_id
    ):
        msg = (
            f"adding edge {child_run_id!r} -> {parent_run_id!r} would create a cycle in the run DAG"
        )
        raise CycleRejectedError(msg)
    db.execute(
        "INSERT INTO run_edges (child_run_id, parent_run_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
        [child_run_id, parent_run_id],
    )


def get_run_parents(db, run_id: str) -> list[str]:
    """All recorded parent run IDs for `run_id`, sorted for a deterministic round-trip."""
    rows = db.execute(
        "SELECT parent_run_id FROM run_edges WHERE child_run_id = ? ORDER BY parent_run_id",
        [run_id],
    ).fetchall()
    return [r[0] for r in rows]


__all__ = [
    "CampaignEdgeError",
    "CycleRejectedError",
    "add_campaign_edge",
    "add_run_edge",
    "get_campaign_parents",
    "get_run_parents",
]
