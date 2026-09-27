"""The edge fold (`campaign_edges` / `run_edges`), spec "Authoritative
writes": `edge.added` (`src`, `dst`, `type`).

The entity key is `[src, dst, type]` itself, so unlike runs or campaigns
there is nothing to merge across a history: `add_campaign_edge` /
`add_run_edge` reject a cycle (including a self-loop) before ever emitting,
and the same `(src, dst, type)` triple is always re-asserted with identical
data (there is no "remove edge" event) -- unless it's a distinct campaign, an
attempt to add a real cycle can't legally reach the log twice with the same
identity anyway.

`fold_edge` is a real function (not inlined into ingest) purely for symmetry
with every other entity kind, and so a defensive multiple-event case (should
never arise given the write-site's own cycle check) has a defined,
deterministic resolution: latest by `(ts, eid)`.
"""

from __future__ import annotations

from typing import Any


def fold_edge(events: list[dict]) -> dict[str, Any] | None:
    """Fold every `edge.added` event for one `[src, dst, type]` entity into
    `{"src": ..., "dst": ..., "type": ...}`, or `None` if no such event
    exists (the caller should then delete any existing row for this entity)."""
    edge_events = [e for e in events if e.get("kind") == "edge.added"]
    if not edge_events:
        return None
    latest = max(edge_events, key=lambda e: (e.get("ts", ""), e.get("eid", "")))
    data = latest.get("data") or {}
    return {
        "src": data.get("src"),
        "dst": data.get("dst"),
        "type": data.get("type"),
    }


__all__ = ["fold_edge"]
