"""Tests for folding bearings events into bearings_readings and bearings_items."""

from __future__ import annotations

import json

from bathos.runlog.fold_bearings import (
    KIND_COMPARED,
    KIND_READING_RECORDED,
    fold_bearings,
    fold_bearings_items,
    fold_bearings_readings,
)


def _ev(kind: str, ts: str, eid: str, project: str = "proj", reading_id: str = "rd1", **data) -> dict:
    return {"kind": kind, "entity": [project, reading_id], "project": project, "ts": ts, "eid": eid, "data": data}


def _reading(eid: str = "e1", ts: str = "2026-11-04T10:00:00Z", **over) -> dict:
    data = {
        "reading_id": "rd1",
        "packet_sha256": "p" * 64,
        "ref_sha": "r" * 40,
        "since": "2026-01-01T00:00:00+00:00",
        "until": "2026-02-01T00:00:00+00:00",
        "reader_id": "reader-A",
        "purposes": [{"id": "r1", "text": "t", "citations": [{"citation": "run:abc", "resolution": "resolved"}]}],
        "open_lines": [{"text": "stray", "citations": []}],
    }
    data.update(over)
    return _ev(KIND_READING_RECORDED, ts, eid, **data)


def _compared(eid: str = "c1", ts: str = "2026-11-04T11:00:00Z", items=None) -> dict:
    if items is None:
        items = [
            {"item_id": "i1", "kind": "mapped", "reading_purpose_id": "r1", "charter_purpose_id": "P1", "matched_retired": None},
            {"item_id": "i2", "kind": "unmapped", "reading_purpose_id": "r2", "matched_retired": "P3"},
            {"item_id": "i3", "kind": "unmapped", "open_line_index": 0, "matched_retired": None},
            {"item_id": "i4", "kind": "silent", "charter_purpose_id": "P2", "matched_retired": None},
        ]
    return _ev(KIND_COMPARED, ts, eid, mapper_id="mapper-B", charter_sha256="c" * 64, items=items)


def test_reading_row_fields_and_canonical_json():
    rows = fold_bearings_readings([_reading()])
    assert len(rows) == 1
    r = rows[0]
    assert (r["project"], r["reading_id"], r["eid"], r["reader_id"]) == ("proj", "rd1", "e1", "reader-A")
    assert r["packet_sha256"] == "p" * 64
    assert r["since"].startswith("2026-01-01") and r["until"].startswith("2026-02-01")
    assert json.loads(r["purposes_json"])[0]["citations"][0]["resolution"] == "resolved"
    assert json.loads(r["open_lines_json"])[0]["text"] == "stray"
    assert r["purposes_json"] == json.dumps(json.loads(r["purposes_json"]), sort_keys=True, separators=(",", ":"))


def test_reading_fold_is_idempotent_on_eid_and_ignores_other_kinds():
    rows = fold_bearings_readings([_reading(), _reading(), _compared()])
    assert [r["eid"] for r in rows] == ["e1"]


def test_item_rows_and_subjects():
    rows = fold_bearings_items([_compared()])
    by = {r["item_id"]: r for r in rows}
    assert set(by) == {"i1", "i2", "i3", "i4"}
    assert by["i2"]["matched_retired"] == "P3"
    assert by["i1"]["matched_retired"] is None
    assert json.loads(by["i3"]["subject_json"]) == {"open_line_index": 0}
    assert json.loads(by["i4"]["subject_json"]) == {"charter_purpose_id": "P2"}
    assert json.loads(by["i1"]["subject_json"]) == {"charter_purpose_id": "P1", "reading_purpose_id": "r1"}
    assert {r["kind"] for r in rows} == {"mapped", "unmapped", "silent"}
    assert {r["compare_eid"] for r in rows} == {"c1"}
    assert {r["mapper_id"] for r in rows} == {"mapper-B"}


def test_items_hold_only_the_latest_comparison_per_reading():
    dup = _compared()
    later = _compared(eid="c2", ts="2026-11-04T12:00:00Z")
    rows = fold_bearings_items([later, dup, dup])  # arrival order must not decide which is latest
    assert len(rows) == 4
    assert {(r["compare_eid"], r["item_id"]) for r in rows} == {("c2", i) for i in ("i1", "i2", "i3", "i4")}


def test_item_without_id_is_dropped():
    rows = fold_bearings_items([_compared(items=[{"kind": "silent", "charter_purpose_id": "P2"}])])
    assert rows == []


def test_fold_bearings_returns_both_tables_in_ts_order():
    """Two writers can race and both append one reading: the earliest row stands, once."""
    readings, items = fold_bearings([_compared(), _reading(eid="e2", ts="2026-11-04T09:00:00Z"), _reading()])
    assert [r["eid"] for r in readings] == ["e2"]
    assert len(items) == 4
