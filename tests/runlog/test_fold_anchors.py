"""The anchor fold (`sidecar_anchors`), spec BC-8: "the fold keeps every
`anchor.recorded`"; ties (equal `anchored_at`) resolve by `(ts, eid)`, unlike
legacy's glob order."""

from __future__ import annotations

from bathos.runlog.fold_anchors import fold_anchor


def ev(data: dict, ts: str, eid: str) -> dict:
    return {
        "v": 1,
        "eid": eid,
        "kind": "anchor.recorded",
        "entity": ["anchor-1"],
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


def test_no_events_returns_none():
    assert fold_anchor([]) is None


def test_single_anchor_fields_round_trip():
    row = fold_anchor(
        [
            ev(
                {
                    "path": "figs/a.svg",
                    "sha256": "abc",
                    "kind": "figure",
                    "label": "fig1",
                    "content_hash": "ch1",
                    "campaign_id": "c1",
                    "anchored_at": "T0",
                },
                "T0",
                "e0",
            )
        ]
    )
    assert row["path"] == "figs/a.svg"
    assert row["kind"] == "figure"
    assert row["campaign_id"] == "c1"


def test_re_anchor_resolves_by_ts_eid_not_insertion_order():
    """A re-anchor of the same (path, sha256) with an EARLIER `ts` but a
    LATER `eid` in the raw event list -- the fold must resolve by (ts, eid),
    not by "last event object in the Python list". Negative control: a
    "last-in-list wins" rule would pick the wrong (earlier-ts) label here."""
    first = ev(
        {"path": "p", "sha256": "s", "kind": "figure", "label": "old", "anchored_at": "T0"},
        "T0",
        "e0",
    )
    second = ev(
        {"path": "p", "sha256": "s", "kind": "figure", "label": "new", "anchored_at": "T1"},
        "T1",
        "e1",
    )
    # Deliberately pass the LATER event first in the list.
    row = fold_anchor([second, first])
    assert row["label"] == "new"
    assert row["label"] != "old"
