"""The edge fold (`campaign_edges` / `run_edges`), spec "Authoritative
writes": `edge.added` (`src`, `dst`, `type`)."""

from __future__ import annotations

from bathos.runlog.fold_edges import fold_edge


def ev(entity: list[str], data: dict, ts: str, eid: str) -> dict:
    return {
        "v": 1,
        "eid": eid,
        "kind": "edge.added",
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


def test_no_events_returns_none():
    assert fold_edge([]) is None


def test_edge_fields_round_trip():
    row = fold_edge(
        [
            ev(
                ["child", "parent", "campaign"],
                {"src": "child", "dst": "parent", "type": "campaign"},
                "T0",
                "e1",
            )
        ]
    )
    assert row == {"src": "child", "dst": "parent", "type": "campaign"}


def test_latest_by_ts_eid_wins_on_defensive_multi_event_case():
    """Should never happen in practice (the write site rejects re-asserting
    a DIFFERENT edge under one identity), but the fold must still be a total
    deterministic function -- negative control: a "first event wins" rule
    would return the EARLIER (wrong, by construction) src here."""
    early = ev(["c", "p", "campaign"], {"src": "WRONG", "dst": "p", "type": "campaign"}, "T0", "e0")
    late = ev(["c", "p", "campaign"], {"src": "c", "dst": "p", "type": "campaign"}, "T1", "e1")
    row = fold_edge([early, late])
    assert row["src"] == "c"
    assert row["src"] != "WRONG"
