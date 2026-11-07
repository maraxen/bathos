"""Tests for charter event folding into charter_confirmations and charter_pivots."""

from __future__ import annotations

import json

from bathos.runlog.fold_charter import (
    KIND_CHARTER_CONFIRMED,
    KIND_CHARTER_PIVOT,
    fold_charter,
    fold_charter_confirmations,
    fold_charter_pivots,
)


def _ev(kind: str, ts: str, eid: str, project: str = "testproj", **data) -> dict:
    return {
        "kind": kind,
        "entity": [project],
        "project": project,
        "ts": ts,
        "eid": eid,
        "data": data,
    }


def test_fold_charter_confirmations_basic():
    ext_data = {
        "alpha": 123,
        "beta": "unicode: \u03b1\u03b2\u03b3",
        "nested": {"x": True, "y": 3.14},
        "tags": ["a", "b"],
    }
    purposes = {
        "p1": {
            "id": "p1",
            "text": "First purpose",
            "state": "live",
            "retired_reason": None,
            "ext": ext_data,
        }
    }
    ev = _ev(
        KIND_CHARTER_CONFIRMED,
        ts="2026-11-04T12:00:00Z",
        eid="eid_c1",
        charter_sha256="c" * 64,
        mandate_sha256="m" * 64,
        mandate_text="Owner mandate text",
        purposes=purposes,
        auth_mode="tty",
        amended_mandate_from=None,
    )
    rows = fold_charter_confirmations([ev])
    assert len(rows) == 1
    row = rows[0]
    assert row["project"] == "testproj"
    assert row["eid"] == "eid_c1"
    assert row["ts"] == "2026-11-04T12:00:00Z"
    assert row["charter_sha256"] == "c" * 64
    assert row["mandate_sha256"] == "m" * 64
    assert row["mandate_text"] == "Owner mandate text"
    assert row["auth_mode"] == "tty"
    assert row["amended_mandate_from"] is None

    # Check canonical JSON formatting of purposes_json
    parsed_purposes = json.loads(row["purposes_json"])
    assert parsed_purposes["p1"]["ext"] == ext_data
    assert json.dumps(ext_data, sort_keys=True, separators=(",", ":")) == json.dumps(
        parsed_purposes["p1"]["ext"], sort_keys=True, separators=(",", ":")
    )


def test_fold_charter_confirmations_idempotent_and_ordered():
    ev1 = _ev(
        KIND_CHARTER_CONFIRMED,
        ts="2026-11-04T10:00:00Z",
        eid="eid_1",
        charter_sha256="1" * 64,
    )
    # Duplicate eid with different ts/data should be ignored
    ev1_dup = _ev(
        KIND_CHARTER_CONFIRMED,
        ts="2026-11-04T10:00:00Z",
        eid="eid_1",
        charter_sha256="dup" * 16,
    )
    ev2 = _ev(
        KIND_CHARTER_CONFIRMED,
        ts="2026-11-04T11:00:00Z",
        eid="eid_2",
        charter_sha256="2" * 64,
        amended_mandate_from="old_mandate_sha",
    )
    # Shuffled input
    rows = fold_charter_confirmations([ev2, ev1_dup, ev1])
    assert len(rows) == 2
    assert rows[0]["eid"] == "eid_1"
    assert rows[1]["eid"] == "eid_2"
    assert rows[1]["amended_mandate_from"] == "old_mandate_sha"


def test_fold_charter_pivots_basic():
    ev = _ev(
        KIND_CHARTER_PIVOT,
        ts="2026-11-04T13:00:00Z",
        eid="eid_p1",
        reason="Target goal pivoted",
        dispositions={"p1": "reaffirm", "p2": "retire"},
        retired_reasons={"p2": "Completed objective"},
        cheapest_next_test="executed:run_abc123",
        charter_sha256_before="a" * 64,
        charter_sha256_after="b" * 64,
        confirmed_before=True,
    )
    rows = fold_charter_pivots([ev])
    assert len(rows) == 1
    row = rows[0]
    assert row["project"] == "testproj"
    assert row["eid"] == "eid_p1"
    assert row["reason"] == "Target goal pivoted"
    assert row["cheapest_next_test"] == "executed:run_abc123"
    assert row["charter_sha256_before"] == "a" * 64
    assert row["charter_sha256_after"] == "b" * 64
    assert row["confirmed_before"] is True
    assert json.loads(row["dispositions_json"]) == {"p1": "reaffirm", "p2": "retire"}
    assert json.loads(row["retired_reasons_json"]) == {"p2": "Completed objective"}


def test_fold_charter_pivots_idempotent():
    ev1 = _ev(
        KIND_CHARTER_PIVOT,
        ts="2026-11-04T13:00:00Z",
        eid="eid_p1",
        reason="First pivot",
    )
    ev1_dup = _ev(
        KIND_CHARTER_PIVOT,
        ts="2026-11-04T13:00:00Z",
        eid="eid_p1",
        reason="Duplicate pivot",
    )
    rows = fold_charter_pivots([ev1, ev1_dup])
    assert len(rows) == 1
    assert rows[0]["reason"] == "First pivot"


def test_fold_charter_mixed_and_empty():
    assert fold_charter([]) == ([], [])

    foreign = {"kind": "run.started", "entity": ["run1"], "ts": "2026-11-04T00:00:00Z", "eid": "r1"}
    c_ev = _ev(KIND_CHARTER_CONFIRMED, "2026-11-04T01:00:00Z", "c1")
    p_ev = _ev(KIND_CHARTER_PIVOT, "2026-11-04T02:00:00Z", "p1")

    confs, pivs = fold_charter([foreign, c_ev, p_ev])
    assert len(confs) == 1
    assert confs[0]["eid"] == "c1"
    assert len(pivs) == 1
    assert pivs[0]["eid"] == "p1"
