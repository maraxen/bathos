"""Append-only ledger folds: `blast_radius_ledger`, `trust_ledger`,
`archived_items`, and submit provenance (spec "Authoritative writes":
`blast_radius.recorded`, `trust_ledger.recorded`, `archived_item.recorded`,
`submit.recorded`).

Each entity is keyed by its own record id -- a fresh `uuid4` minted at write
time and never reused (`BlastRadiusRecord.id`, `TrustLedgerRecord.id`,
`ArchivedItemRecord.record_id`, the `submit_id` `catalog.write_submit_
provenance` mints when the caller doesn't supply one) -- so there is at most
one live event per entity in practice. Each fold is still "latest by
`(ts, eid)`", not "the only event", for the same defensive reason as
`fold_edges`/`fold_anchors`: a well-formed system never produces two events
for one of these entities, but the fold must still be a total, deterministic
function of whatever the events table actually contains.
"""

from __future__ import annotations

from typing import Any


def _latest(events: list[dict], kind: str) -> dict | None:
    matching = [e for e in events if e.get("kind") == kind]
    if not matching:
        return None
    return max(matching, key=lambda e: (e.get("ts", ""), e.get("eid", "")))


def fold_blast_radius(events: list[dict]) -> dict[str, Any] | None:
    ev = _latest(events, "blast_radius.recorded")
    if ev is None:
        return None
    data = ev.get("data") or {}
    return {
        "id": data.get("id"),
        "entity_type": data.get("entity_type"),
        "entity_id": data.get("entity_id"),
        "from_state": data.get("from_state"),
        "to_state": data.get("to_state"),
        "anchor_kind": data.get("anchor_kind"),
        "anchor_value": data.get("anchor_value"),
        "matched_files": data.get("matched_files"),
        "matched_clauses": data.get("matched_clauses"),
        "shadow_verdict": data.get("shadow_verdict"),
        "match_reason": data.get("match_reason"),
        "reason": data.get("reason"),
        "amended_at": data.get("amended_at"),
    }


def fold_trust_ledger(events: list[dict]) -> dict[str, Any] | None:
    ev = _latest(events, "trust_ledger.recorded")
    if ev is None:
        return None
    data = ev.get("data") or {}
    return {
        "id": data.get("id"),
        "run_id": data.get("run_id"),
        "output_path": data.get("output_path"),
        "content_hash": data.get("content_hash"),
        "from_state": data.get("from_state"),
        "to_state": data.get("to_state"),
        "attestation_ref": data.get("attestation_ref"),
        "amended_at": data.get("amended_at"),
        "reason": data.get("reason"),
    }


def fold_archived_item(events: list[dict]) -> dict[str, Any] | None:
    ev = _latest(events, "archived_item.recorded")
    if ev is None:
        return None
    data = ev.get("data") or {}
    return {
        "id": data.get("id"),
        "project_slug": data.get("project_slug"),
        "event": data.get("event"),
        "kind": data.get("kind"),
        # Kept as a Python list here; the legacy warm column is a
        # JSON-encoded TEXT string, so the ingest-side writer json.dumps()s
        # this before INSERT (matching `archived_items._insert_row`).
        "paths": data.get("paths"),
        "pre_archive_sha": data.get("pre_archive_sha"),
        "stub_commit_sha": data.get("stub_commit_sha"),
        "verdict": data.get("verdict"),
        "reason": data.get("reason"),
        "superseded_by": data.get("superseded_by"),
        "bundle_sha256": data.get("bundle_sha256"),
        "bundle_path": data.get("bundle_path"),
        "archived_by": data.get("archived_by"),
        "recorded_at": data.get("recorded_at"),
        "record_id": data.get("record_id"),
    }


def fold_submit(events: list[dict]) -> dict[str, Any] | None:
    """`submit.recorded`'s `data` carries every legacy Parquet field except
    `submitted_at` (spec: absent from the payload) -- the event's own `ts` IS
    the submission time, so this fold takes it from there rather than the
    payload."""
    ev = _latest(events, "submit.recorded")
    if ev is None:
        return None
    data = ev.get("data") or {}
    entity = ev.get("entity") or [None]
    return {
        "id": entity[0],
        "project_slug": data.get("project_slug"),
        "command": data.get("command"),
        "sidecar_sha256": data.get("sidecar_sha256"),
        "bth_submit_version": data.get("bth_submit_version"),
        "submitted_at": ev.get("ts"),
        "myxcel_job_id": data.get("myxcel_job_id"),
        "slurm_job_id": data.get("slurm_job_id"),
        "stage_name": data.get("stage_name"),
    }


__all__ = ["fold_archived_item", "fold_blast_radius", "fold_submit", "fold_trust_ledger"]
