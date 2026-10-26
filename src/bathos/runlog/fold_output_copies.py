"""Fold of ``output_copy.*`` events into ``output_copies`` rows (output tiering spec 261023, section 5.3).

Entity: ``[run_id, copy_id]``. Three kinds:

* ``output_copy.recorded``: written by ``bth cold`` / ``bth warm``; carries the full row, including the ``state`` and
  ``verified_at`` of its own pre-record verify.
* ``output_copy.verified``: written by ``bth verify --tier outputs``; ``state``, ``verified_at``, ``detail``.
* ``output_copy.superseded``: written when a pair is renamed aside; carries ``renamed_to`` (the renamed archive's
  absolute path) and ``renamed_manifest_to``.

This is a MERGE, not "latest event wins":

* The base fields (tier, profile, host, path, config_layer, the hashes) come from the latest ``recorded``.
* ``state`` / ``verified_at`` come from the latest of {``recorded``, ``verified``}.
* ``superseded_at`` is set only while the latest of {``recorded``, ``superseded``} is a ``superseded``: a rewrite
  after a supersede lands at the same remote path, so it has the same ``copy_id``, and its ``recorded`` clears the
  mark (otherwise the new copy would be hidden forever).

"Latest" is the key ``(ts, kind_rank, eid)`` with ``recorded`` = 0, ``verified`` = 1, ``superseded`` = 2. ``eid`` is
a ``uuid7``: a millisecond stamp plus random bits, so two events in the same millisecond have no meaningful ``eid``
order, and a same-instant ``recorded`` + ``verified`` pair must fold to ``verified`` whatever order they arrive in.
(The envelope ``ts`` has microsecond resolution, so ties are rarer than the eid suggests, but possible.)

The renamed-aside pair of each ``superseded`` event becomes its own row (its own ``copy_id`` from ``renamed_to``,
state ``superseded``, ``renamed_from`` = the live row's ``copy_id``) so the old archive stays findable.

``copy_id`` is the single identity of a physical copy: ``sha256(json.dumps([run_id, host, profile or "", path]))[:16]``.
``tier`` is an attribute, not part of it, so one remote file is one row whichever leg wrote it, and warm copies on
two machines never collide.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

KIND_RECORDED = "output_copy.recorded"
KIND_VERIFIED = "output_copy.verified"
KIND_SUPERSEDED = "output_copy.superseded"

OUTPUT_COPY_KINDS = frozenset({KIND_RECORDED, KIND_VERIFIED, KIND_SUPERSEDED})

#: Same-instant tie-break order (spec 5.3).
KIND_RANK: dict[str, int] = {KIND_RECORDED: 0, KIND_VERIFIED: 1, KIND_SUPERSEDED: 2}

STATE_SUPERSEDED = "superseded"


def copy_id_for(run_id: str, host: str, profile: str | None, path: str) -> str:
    """The identity of one physical copy (spec 5.3)."""
    return hashlib.sha256(json.dumps([run_id, host, profile or "", path]).encode()).hexdigest()[:16]


def _order_key(ev: dict) -> tuple[str, int, str]:
    return (ev.get("ts", ""), KIND_RANK.get(ev.get("kind", ""), 0), ev.get("eid", ""))


def _latest(events: list[dict], kinds: frozenset[str]) -> dict | None:
    picked = [ev for ev in events if ev.get("kind") in kinds]
    return max(picked, key=_order_key) if picked else None


def fold_output_copy(events: list[dict]) -> list[dict[str, Any]]:
    """Rows for one ``[run_id, copy_id]`` entity: the live row first (if any ``recorded`` event exists), then one
    renamed-aside row per ``superseded`` event that names what it renamed. ``events`` may be in any order and may
    include foreign kinds (ignored)."""
    events = [ev for ev in events if ev.get("kind") in OUTPUT_COPY_KINDS]
    if not events:
        return []
    entity = events[0].get("entity") or []
    run_id, copy_id = (entity + [None, None])[:2]

    rows: list[dict[str, Any]] = []
    base_ev = _latest(events, frozenset({KIND_RECORDED}))
    state_ev = _latest(events, frozenset({KIND_RECORDED, KIND_VERIFIED}))
    mark_ev = _latest(events, frozenset({KIND_RECORDED, KIND_SUPERSEDED}))

    if base_ev is not None and state_ev is not None and mark_ev is not None:
        base = base_ev.get("data") or {}
        state = state_ev.get("data") or {}
        superseded_at = None
        if mark_ev.get("kind") == KIND_SUPERSEDED:
            superseded_at = (mark_ev.get("data") or {}).get("superseded_at") or mark_ev.get("ts")
        rows.append(
            {
                "copy_id": copy_id,
                "run_id": run_id,
                "tier": base.get("tier"),
                "profile": base.get("profile") or "",
                "host": base.get("host"),
                "path": base.get("path"),
                "config_layer": base.get("config_layer"),
                "archive_sha256": base.get("archive_sha256"),
                "manifest_sha256": base.get("manifest_sha256"),
                "archive_size_bytes": base.get("archive_size_bytes"),
                "state": state.get("state"),
                "verified_at": state.get("verified_at"),
                "recorded_at": base.get("recorded_at") or base_ev.get("ts"),
                "superseded_at": superseded_at,
                "renamed_from": None,
            }
        )

    base = (base_ev or {}).get("data") or {}
    aside_seen: set[str] = set()
    for ev in sorted((e for e in events if e["kind"] == KIND_SUPERSEDED), key=_order_key):
        d = ev.get("data") or {}
        aside_path = d.get("renamed_to") or d.get("renamed_manifest_to")
        if not aside_path:
            continue
        host = d.get("host") or base.get("host")
        profile = d.get("profile") if d.get("profile") is not None else base.get("profile")
        aside_id = copy_id_for(str(run_id), str(host), profile, str(aside_path))
        if aside_id in aside_seen:
            continue
        aside_seen.add(aside_id)
        rows.append(
            {
                "copy_id": aside_id,
                "run_id": run_id,
                "tier": d.get("tier") or base.get("tier"),
                "profile": profile or "",
                "host": host,
                "path": aside_path,
                "config_layer": d.get("config_layer") or base.get("config_layer"),
                "archive_sha256": d.get("archive_sha256") or base.get("archive_sha256"),
                "manifest_sha256": d.get("manifest_sha256") or base.get("manifest_sha256"),
                "archive_size_bytes": d.get("archive_size_bytes") or base.get("archive_size_bytes"),
                "state": STATE_SUPERSEDED,
                "verified_at": None,
                "recorded_at": base.get("recorded_at"),
                "superseded_at": d.get("superseded_at") or ev.get("ts"),
                "renamed_from": copy_id,
            }
        )
    return rows
