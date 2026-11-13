"""Fold of ``charter.*`` events into ``charter_confirmations`` and ``charter_pivots`` rows.

Entity: ``[project_slug]``. Two kinds:
- ``charter.confirmed``: written by ``bth charter confirm``; carries the file sha256,
  mandate text/hash, full purposes mapping with arbitrary ext, auth_mode, and amended_mandate_from.
- ``charter.pivot``: written by ``bth charter pivot``; carries reason, dispositions,
  retired_reasons, cheapest_next_test, sha256 before/after, and confirmed_before.

Both tables are append-only, one row per event, idempotent on eid.
"""

from __future__ import annotations

import json
from typing import Any

KIND_CHARTER_CONFIRMED = "charter.confirmed"
KIND_CHARTER_PIVOT = "charter.pivot"

CHARTER_KINDS = frozenset({KIND_CHARTER_CONFIRMED, KIND_CHARTER_PIVOT})


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def fold_charter_confirmations(events: list[dict]) -> list[dict[str, Any]]:
    """Fold ``charter.confirmed`` events into ``charter_confirmations`` rows."""
    confirmed_events = [ev for ev in events if ev.get("kind") == KIND_CHARTER_CONFIRMED]
    seen_eids: set[str] = set()
    rows: list[dict[str, Any]] = []
    for ev in sorted(confirmed_events, key=lambda e: (e.get("ts", ""), e.get("eid", ""))):
        eid = ev.get("eid")
        if not eid or eid in seen_eids:
            continue
        seen_eids.add(eid)
        entity = ev.get("entity") or []
        project = entity[0] if entity else (ev.get("project") or "")
        data = ev.get("data") or {}
        purposes = data.get("purposes", {})
        rows.append(
            {
                "project": str(project),
                "eid": str(eid),
                "ts": str(ev.get("ts", "")),
                "charter_sha256": str(data.get("charter_sha256", "")),
                "mandate_sha256": str(data.get("mandate_sha256", "")),
                "mandate_text": str(data.get("mandate_text", "")),
                "purposes_json": _canonical_json(purposes),
                "auth_mode": str(data.get("auth_mode", "")),
                "amended_mandate_from": data.get("amended_mandate_from"),
            }
        )
    return rows


def fold_charter_pivots(events: list[dict]) -> list[dict[str, Any]]:
    """Fold ``charter.pivot`` events into ``charter_pivots`` rows."""
    pivot_events = [ev for ev in events if ev.get("kind") == KIND_CHARTER_PIVOT]
    seen_eids: set[str] = set()
    rows: list[dict[str, Any]] = []
    for ev in sorted(pivot_events, key=lambda e: (e.get("ts", ""), e.get("eid", ""))):
        eid = ev.get("eid")
        if not eid or eid in seen_eids:
            continue
        seen_eids.add(eid)
        entity = ev.get("entity") or []
        project = entity[0] if entity else (ev.get("project") or "")
        data = ev.get("data") or {}
        dispositions = data.get("dispositions", {})
        retired_reasons = data.get("retired_reasons", {})
        rows.append(
            {
                "project": str(project),
                "eid": str(eid),
                "ts": str(ev.get("ts", "")),
                "reason": str(data.get("reason", "")),
                "dispositions_json": _canonical_json(dispositions),
                "retired_reasons_json": _canonical_json(retired_reasons),
                "cheapest_next_test": str(data.get("cheapest_next_test", "")),
                "charter_sha256_before": str(data.get("charter_sha256_before", "")),
                "charter_sha256_after": str(data.get("charter_sha256_after", "")),
                "confirmed_before": bool(data.get("confirmed_before", False)),
            }
        )
    return rows


def fold_charter(
    events: list[dict],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fold all charter events into (confirmations, pivots)."""
    return fold_charter_confirmations(events), fold_charter_pivots(events)
