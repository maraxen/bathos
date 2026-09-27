"""The anchor fold (`sidecar_anchors`), spec BC-8 (AC-17 exemption list):

"Anchors written through the non-durable `CatalogAnchorStore` ... write no
fragment, so a rebuild drops them; the fold keeps every `anchor.recorded`.
Two anchor fragments with equal `anchored_at` resolve by `(ts, eid)` in the
fold, by glob order in legacy."

The entity key is `anchor_id` (`uuid5` of the legacy `(path, sha256)`
identity, spec "Authoritative writes" -- see `bathos.anchor._anchor_entity_id`),
NOT the legacy warm `id` column (a fresh `uuid4` minted on every insert, never
a stable entity key and never compared by AC-17). "Keeps every
`anchor.recorded`" just falls out of entity-keyed folding: every distinct
`anchor_id` gets its own row; within one `anchor_id`, a re-anchor of the same
`(path, sha256)` (`CatalogAnchorStore.insert`'s only re-anchor case) resolves
to the latest event by `(ts, eid)`.
"""

from __future__ import annotations

from typing import Any


def fold_anchor(events: list[dict]) -> dict[str, Any] | None:
    """Fold every `anchor.recorded` event for one `anchor_id` entity into a
    `sidecar_anchors`-shaped dict, or `None` if no such event exists."""
    anchor_events = [e for e in events if e.get("kind") == "anchor.recorded"]
    if not anchor_events:
        return None
    latest = max(anchor_events, key=lambda e: (e.get("ts", ""), e.get("eid", "")))
    data = latest.get("data") or {}
    return {
        "path": data.get("path"),
        "sha256": data.get("sha256"),
        "kind": data.get("kind"),
        "label": data.get("label"),
        "content_hash": data.get("content_hash"),
        "campaign_id": data.get("campaign_id"),
        "anchored_at": data.get("anchored_at"),
    }


__all__ = ["fold_anchor"]
