"""Fold of ``observation.*`` and ``appraisal.*`` events into index tables (spec 261015, S3).

Event kinds:
- ``observation.created``: entity ``[obs_id]``, append-only creation of a run-backed or
  run-less observation.
- ``observation.status``: entity ``[obs_id]``, append-only status transition
  (``open`` | ``refuted`` | ``survived-seed-check`` | ``promoted``), with ``data.of == obs_id``.
- ``appraisal.created``: entity ``[appraisal_id]``, append-only structured appraisal record.

All folds are pure, deterministic functions of the event list, ordered by ``(ts, eid)``
and idempotent on ``eid`` / entity ID.
"""

from __future__ import annotations

import json
from typing import Any

KIND_OBSERVATION_CREATED = "observation.created"
KIND_OBSERVATION_STATUS = "observation.status"
KIND_APPRAISAL_CREATED = "appraisal.created"

OBSERVATION_KINDS = frozenset({KIND_OBSERVATION_CREATED, KIND_OBSERVATION_STATUS})
APPRAISAL_KINDS = frozenset({KIND_APPRAISAL_CREATED})


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _event_sort_key(ev: dict) -> tuple[str, str]:
    return (str(ev.get("ts") or ""), str(ev.get("eid") or ""))


def fold_observation(events: list[dict]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Fold all events for a single ``[obs_id]`` into ``(observation_row, status_rows)``."""
    created_evs = sorted(
        (e for e in events if e.get("kind") == KIND_OBSERVATION_CREATED and e.get("eid")),
        key=_event_sort_key,
    )
    if not created_evs:
        return None, []

    created_ev = created_evs[0]
    data = created_ev.get("data") or {}
    entity = created_ev.get("entity") or []
    obs_id = str(data.get("id") or (entity[0] if entity else ""))
    if not obs_id:
        return None, []

    status_evs = sorted(
        (e for e in events if e.get("kind") == KIND_OBSERVATION_STATUS and e.get("eid")),
        key=_event_sort_key,
    )
    seen_status_eids: set[str] = set()
    status_rows: list[dict[str, Any]] = []
    current_status = str(data.get("status") or "open")
    for sev in status_evs:
        seid = str(sev.get("eid") or "")
        if not seid or seid in seen_status_eids:
            continue
        seen_status_eids.add(seid)
        sdata = sev.get("data") or {}
        st = str(sdata.get("status") or "")
        if st:
            current_status = st
        status_rows.append(
            {
                "eid": seid,
                "obs_id": str(sdata.get("of") or obs_id),
                "status": st,
                "reason": str(sdata.get("reason") or ""),
                "actor": str(sdata.get("actor") or ""),
                "agent_session": str(sdata.get("agent_session") or ""),
                "ts": str(sev.get("ts") or ""),
            }
        )

    obs_row: dict[str, Any] = {
        "id": obs_id,
        "project_slug": str(data.get("project_slug") or created_ev.get("project") or ""),
        "run_id": str(data.get("run_id") or ""),
        "text": str(data.get("text") or ""),
        "figure": str(data.get("figure") or ""),
        "output_sha256": str(data.get("output_sha256") or ""),
        "output_shas_json": _canonical_json(data.get("output_shas") or {}),
        "tags_json": _canonical_json(data.get("tags") or []),
        "supersedes": str(data.get("supersedes") or ""),
        "status": current_status,
        "origin": str(data.get("origin") or "human"),
        "agent_session": str(data.get("agent_session") or ""),
        "ref": str(data.get("ref") or ""),
        "locator": str(data.get("locator") or ""),
        "claim": str(data.get("claim") or ""),
        "quote": str(data.get("quote") or ""),
        "value_provenance": str(data.get("value_provenance") or ""),
        "quote_checked": bool(data.get("quote_checked", False)),
        "disposition": str(data.get("disposition") or ""),
        "bears_on": str(data.get("bears_on") or ""),
        "uses_as": str(data.get("uses_as") or ""),
        "appraisal_ids_json": _canonical_json(data.get("appraisal_ids") or []),
        "unappraised": bool(data.get("unappraised", False)),
        "created_at": str(data.get("created_at") or created_ev.get("ts") or ""),
        "eid": str(created_ev.get("eid") or ""),
    }
    return obs_row, status_rows


def fold_appraisal(events: list[dict]) -> dict[str, Any] | None:
    """Fold all events for a single ``[appraisal_id]`` into an ``appraisals`` row."""
    created_evs = sorted(
        (e for e in events if e.get("kind") == KIND_APPRAISAL_CREATED and e.get("eid")),
        key=_event_sort_key,
    )
    if not created_evs:
        return None

    ev = created_evs[0]
    data = ev.get("data") or {}
    entity = ev.get("entity") or []
    appraisal_id = str(data.get("id") or data.get("appraisal_id") or (entity[0] if entity else ""))
    if not appraisal_id:
        return None

    return {
        "id": appraisal_id,
        "project_slug": str(data.get("project_slug") or ev.get("project") or ""),
        "ref": str(data.get("ref") or ""),
        "locator": str(data.get("locator") or ""),
        "appraiser": str(data.get("appraiser") or ""),
        "appraiser_session": str(data.get("appraiser_session") or ""),
        "session_attested": bool(data.get("session_attested", False)),
        "appraiser_agent_id": str(data.get("appraiser_agent_id") or ""),
        "appraiser_kind": str(data.get("appraiser_kind") or "session"),
        "kind_attested": bool(data.get("kind_attested", False)),
        "read_depth": str(data.get("read_depth") or ""),
        "blind_to_json": _canonical_json(data.get("blind_to") or []),
        "scope_claimed": str(data.get("scope_claimed") or ""),
        "scope_supported": str(data.get("scope_supported") or ""),
        "interpretation": str(data.get("interpretation") or ""),
        "for_json": _canonical_json(data.get("for_entries") or data.get("for") or []),
        "against_json": _canonical_json(data.get("against_entries") or data.get("against") or []),
        "payload_sha256": str(data.get("payload_sha256") or ""),
        "created_at": str(data.get("created_at") or ev.get("ts") or ""),
        "eid": str(ev.get("eid") or ""),
    }


__all__ = [
    "APPRAISAL_KINDS",
    "KIND_APPRAISAL_CREATED",
    "KIND_OBSERVATION_CREATED",
    "KIND_OBSERVATION_STATUS",
    "OBSERVATION_KINDS",
    "fold_appraisal",
    "fold_observation",
]
