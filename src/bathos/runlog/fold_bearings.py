"""Fold of ``bearings.*`` events into ``bearings_readings`` and ``bearings_items`` rows.

Entity: ``[project_slug, reading_id]``. Two kinds:
- ``bearings.reading_recorded``: written by ``bth bearings record``; carries the packet hash,
  commit cut, window, reader id, and the reader's purposes and open lines with each citation's
  resolution.
- ``bearings.compared``: written by ``bth bearings compare``; carries the mapper id, the charter
  sha256 compared against, and one item per mapped / unmapped / silent finding.

Both tables are append-only. Readings are idempotent on ``eid``; items on ``(compare_eid, item_id)``.
"""

from __future__ import annotations

import json
from typing import Any

KIND_READING_RECORDED = "bearings.reading_recorded"
KIND_COMPARED = "bearings.compared"

BEARINGS_KINDS = frozenset({KIND_READING_RECORDED, KIND_COMPARED})

# The one key of an item that names its subject, per item shape.
SUBJECT_KEYS = ("reading_purpose_id", "open_line_index", "charter_purpose_id")


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _project_reading(ev: dict) -> tuple[str, str]:
    entity = ev.get("entity") or []
    project = entity[0] if entity else (ev.get("project") or "")
    reading_id = entity[1] if len(entity) > 1 else ""
    return str(project), str(reading_id)


def fold_bearings_readings(events: list[dict]) -> list[dict[str, Any]]:
    """Fold ``bearings.reading_recorded`` events into ``bearings_readings`` rows."""
    evs = [e for e in events if e.get("kind") == KIND_READING_RECORDED]
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for ev in sorted(evs, key=lambda e: (e.get("ts", ""), e.get("eid", ""))):
        eid = ev.get("eid")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        project, reading_id = _project_reading(ev)
        data = ev.get("data") or {}
        rows.append(
            {
                "project": project,
                "reading_id": str(data.get("reading_id", reading_id)),
                "eid": str(eid),
                "ts": str(ev.get("ts", "")),
                "packet_sha256": str(data.get("packet_sha256", "")),
                "ref_sha": str(data.get("ref_sha", "")),
                "since": str(data.get("since", "")),
                "until": str(data.get("until", "")),
                "reader_id": str(data.get("reader_id", "")),
                "purposes_json": _canonical_json(data.get("purposes", [])),
                "open_lines_json": _canonical_json(data.get("open_lines", [])),
            }
        )
    return rows


def fold_bearings_items(events: list[dict]) -> list[dict[str, Any]]:
    """Fold ``bearings.compared`` events into ``bearings_items`` rows."""
    evs = [e for e in events if e.get("kind") == KIND_COMPARED]
    seen_events: set[str] = set()
    seen_items: set[tuple[str, str]] = set()
    rows: list[dict[str, Any]] = []
    for ev in sorted(evs, key=lambda e: (e.get("ts", ""), e.get("eid", ""))):
        eid = ev.get("eid")
        if not eid or eid in seen_events:
            continue
        seen_events.add(eid)
        project, reading_id = _project_reading(ev)
        data = ev.get("data") or {}
        for item in data.get("items", []):
            item_id = str(item.get("item_id", ""))
            if not item_id or (str(eid), item_id) in seen_items:
                continue
            seen_items.add((str(eid), item_id))
            subject = {k: item[k] for k in SUBJECT_KEYS if k in item}
            rows.append(
                {
                    "project": project,
                    "reading_id": str(data.get("reading_id", reading_id)),
                    "item_id": item_id,
                    "compare_eid": str(eid),
                    "ts": str(ev.get("ts", "")),
                    "mapper_id": str(data.get("mapper_id", "")),
                    "charter_sha256": str(data.get("charter_sha256", "")),
                    "kind": str(item.get("kind", "")),
                    "subject_json": _canonical_json(subject),
                    "matched_retired": item.get("matched_retired"),
                }
            )
    return rows


def fold_bearings(events: list[dict]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fold all bearings events into (readings, items)."""
    return fold_bearings_readings(events), fold_bearings_items(events)
