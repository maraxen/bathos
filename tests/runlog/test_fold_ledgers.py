"""Append-only ledger folds: `blast_radius_ledger`, `trust_ledger`,
`archived_items`, and submit provenance."""

from __future__ import annotations

from bathos.runlog.fold_ledgers import (
    fold_archived_item,
    fold_blast_radius,
    fold_submit,
    fold_trust_ledger,
)


def ev(kind: str, entity: list[str], data: dict, ts: str, eid: str) -> dict:
    return {
        "v": 1,
        "eid": eid,
        "kind": kind,
        "entity": entity,
        "ts": ts,
        "project": "p",
        "project_id": None,
        "writer": "w",
        "seq": 1,
        "main_root": "/main",
        "worktree_root": "/main",
        "origin": "live",
        "data": data,
    }


def test_blast_radius_no_events_returns_none():
    assert fold_blast_radius([]) is None


def test_blast_radius_fields_round_trip():
    row = fold_blast_radius(
        [
            ev(
                "blast_radius.recorded",
                ["br1"],
                {
                    "id": "br1",
                    "entity_type": "run",
                    "entity_id": "r1",
                    "from_state": None,
                    "to_state": "affected",
                    "anchor_kind": "commit",
                    "anchor_value": "deadbeef",
                    "matched_files": None,
                    "matched_clauses": None,
                    "shadow_verdict": None,
                    "match_reason": "path overlap",
                    "reason": None,
                    "amended_at": "T0",
                },
                "T0",
                "e0",
            )
        ]
    )
    assert row["to_state"] == "affected"
    assert row["anchor_value"] == "deadbeef"


def test_trust_ledger_fields_round_trip():
    row = fold_trust_ledger(
        [
            ev(
                "trust_ledger.recorded",
                ["tl1"],
                {
                    "id": "tl1",
                    "run_id": "r1",
                    "output_path": "out.json",
                    "content_hash": "h1",
                    "from_state": "candidate",
                    "to_state": "promoted",
                    "attestation_ref": "att1",
                    "amended_at": "T0",
                    "reason": None,
                },
                "T0",
                "e0",
            )
        ]
    )
    assert row["from_state"] == "candidate"
    assert row["to_state"] == "promoted"
    assert row["content_hash"] == "h1"


def test_archived_item_paths_kept_as_list_for_caller_to_json_encode():
    row = fold_archived_item(
        [
            ev(
                "archived_item.recorded",
                ["rec1"],
                {
                    "id": "item1",
                    "project_slug": "proj",
                    "event": "archived",
                    "kind": "figure",
                    "paths": ["a.svg", "b.svg"],
                    "pre_archive_sha": "sha1",
                    "stub_commit_sha": "",
                    "verdict": "",
                    "reason": "superseded",
                    "superseded_by": "",
                    "bundle_sha256": "",
                    "bundle_path": "",
                    "archived_by": "user",
                    "recorded_at": "T0",
                    "record_id": "rec1",
                },
                "T0",
                "e0",
            )
        ]
    )
    assert row["paths"] == ["a.svg", "b.svg"]
    assert row["event"] == "archived"


def test_submit_submitted_at_comes_from_envelope_ts_not_data():
    """Spec: submit.recorded's data carries every legacy field EXCEPT
    submitted_at -- the event's own ts IS the submission time. Negative
    control: a rule that read data.get('submitted_at') would return None
    (the payload never carries that key), not the envelope ts."""
    row = fold_submit(
        [
            ev(
                "submit.recorded",
                ["sub1"],
                {
                    "project_slug": "proj",
                    "command": "scripts/experiments/foo.py",
                    "sidecar_sha256": "sha",
                    "bth_submit_version": "0.13.0",
                    "myxcel_job_id": "12345",
                    "slurm_job_id": "12345",
                    "stage_name": "exploration",
                },
                "2026-01-01T00:00:00Z",
                "e0",
            )
        ]
    )
    assert row["submitted_at"] == "2026-01-01T00:00:00Z"
    assert row["id"] == "sub1"
    assert row["myxcel_job_id"] == "12345"
    assert row["slurm_job_id"] == "12345"


def test_submit_no_events_returns_none():
    assert fold_submit([]) is None
