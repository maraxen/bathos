"""The ``output_copy.*`` fold (output tiering spec 261023, section 5.3): a merge ordered by ``(ts, kind_rank, eid)``,
not "latest event wins". Pure fold tests; ingest wiring is covered in tests/test_output_tiers_cold.py.
"""

from __future__ import annotations

import hashlib
import json
import random

from bathos.runlog.fold_output_copies import copy_id_for, fold_output_copy

RUN = "11111111-aaaa-4aaa-8aaa-111111111111"
HOST, PROFILE, PATH = "h", "p", "/cold/bathos-cold/repo/run/run-x.tar.zst"
CID = copy_id_for(RUN, HOST, PROFILE, PATH)
T = "2026-01-01T00:00:00.000000Z"


def ev(kind: str, ts: str, eid: str, **data) -> dict:
    return {"kind": kind, "entity": [RUN, CID], "ts": ts, "eid": eid, "data": data}


def recorded(ts: str = T, eid: str = "e1", state: str = "verified", **extra) -> dict:
    base = {
        "tier": "cold",
        "profile": PROFILE,
        "host": HOST,
        "path": PATH,
        "config_layer": "L",
        "archive_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "archive_size_bytes": 7,
        "state": state,
        "verified_at": ts if state == "verified" else None,
        "recorded_at": ts,
    }
    return ev("output_copy.recorded", ts, eid, **{**base, **extra})


def verified(ts: str, eid: str, state: str = "verified") -> dict:
    return ev("output_copy.verified", ts, eid, state=state, verified_at=ts, detail="")


def superseded(ts: str, eid: str, **extra) -> dict:
    data = {"host": HOST, "profile": PROFILE, "tier": "cold", "path": PATH, "reason": "r", **extra}
    return ev("output_copy.superseded", ts, eid, **data)


def live(rows: list[dict]) -> dict:
    (row,) = [r for r in rows if r["renamed_from"] is None]
    return row


def test_copy_id_is_the_spec_formula_and_includes_host_and_not_tier():
    want = hashlib.sha256(json.dumps([RUN, HOST, PROFILE, PATH]).encode()).hexdigest()[:16]
    assert want == CID
    assert (
        copy_id_for(RUN, "other", PROFILE, PATH) != CID
    )  # warm copies on two machines never collide
    assert copy_id_for(RUN, HOST, None, PATH) == copy_id_for(RUN, HOST, "", PATH)


def test_state_comes_from_the_latest_of_recorded_and_verified_base_fields_from_recorded():
    rows = fold_output_copy(
        [recorded("2026-01-01T00:00:01Z", "e1"), verified("2026-01-01T00:00:02Z", "e2", "failed")]
    )
    row = live(rows)
    assert row["state"] == "failed" and row["verified_at"] == "2026-01-01T00:00:02Z"
    assert (row["tier"], row["host"], row["archive_sha256"], row["config_layer"]) == (
        "cold",
        HOST,
        "a" * 64,
        "L",
    )
    assert row["recorded_at"] == "2026-01-01T00:00:01Z"  # the verify did not move it
    assert row["superseded_at"] is None


def test_a_later_recorded_beats_an_earlier_verified():
    row = live(
        fold_output_copy(
            [
                recorded("2026-01-01T00:00:01Z", "e1", state="verified"),
                verified("2026-01-01T00:00:02Z", "e2", "failed"),
                recorded("2026-01-01T00:00:03Z", "e3", state="verified"),
            ]
        )
    )
    assert row["state"] == "verified"


def test_a_same_instant_recorded_and_verified_pair_folds_to_verified_in_1000_shuffled_orders():
    """Same ts to the microsecond; the eids are chosen so that eid order alone would pick the WRONG one (the
    recorded event's eid sorts last). Only kind_rank (recorded 0 < verified 1) gets it right."""
    pair = [
        recorded(T, "zzzz", state="unverifiable"),
        verified(T, "aaaa", "verified"),
    ]
    rng = random.Random(261023)
    seen_orders = set()
    for _ in range(1000):
        evs = pair[:]
        rng.shuffle(evs)
        seen_orders.add(tuple(e["eid"] for e in evs))
        assert live(fold_output_copy(evs))["state"] == "verified"
    assert len(seen_orders) == 2  # both orders were exercised


def test_a_superseded_copy_is_marked_and_excluded_until_a_rewrite_clears_it():
    rows = fold_output_copy(
        [
            recorded("2026-01-01T00:00:01Z", "e1"),
            superseded("2026-01-01T00:00:02Z", "e2", renamed_to=PATH + ".old"),
        ]
    )
    assert live(rows)["superseded_at"] == "2026-01-01T00:00:02Z"

    rewritten = fold_output_copy(
        [
            recorded("2026-01-01T00:00:01Z", "e1"),
            superseded("2026-01-01T00:00:02Z", "e2", renamed_to=PATH + ".old"),
            recorded(
                "2026-01-01T00:00:03Z", "e3"
            ),  # a rewrite lands at the same path: the same copy_id
        ]
    )
    assert live(rewritten)["superseded_at"] is None  # B1: not hidden forever
    assert live(rewritten)["state"] == "verified"


def test_a_same_instant_recorded_and_superseded_pair_is_superseded_in_both_orders():
    pair = [recorded(T, "zzzz"), superseded(T, "aaaa", renamed_to=PATH + ".old")]
    for evs in (pair, pair[::-1]):
        assert live(fold_output_copy(evs))["superseded_at"] == T


def test_the_renamed_aside_pair_is_its_own_superseded_row_and_survives_the_rewrite():
    old = PATH.replace(".tar.zst", ".superseded-20260101T000000Z.tar.zst")
    rows = fold_output_copy(
        [
            recorded("2026-01-01T00:00:01Z", "e1"),
            superseded("2026-01-01T00:00:02Z", "e2", renamed_to=old),
            recorded("2026-01-01T00:00:03Z", "e3"),
        ]
    )
    assert len(rows) == 2
    (aside,) = [r for r in rows if r["renamed_from"] is not None]
    assert aside["state"] == "superseded" and aside["path"] == old and aside["renamed_from"] == CID
    assert aside["copy_id"] == copy_id_for(RUN, HOST, PROFILE, old) != CID
    assert aside["archive_sha256"] == "a" * 64  # still describes what was renamed


def test_a_manifest_only_supersede_still_leaves_a_findable_row():
    mpath = PATH.replace(".tar.zst", ".superseded-X.manifest.json")
    rows = fold_output_copy([superseded("2026-01-01T00:00:02Z", "e2", renamed_manifest_to=mpath)])
    assert [r["path"] for r in rows] == [mpath] and rows[0]["state"] == "superseded"


def test_no_recorded_event_means_no_live_row_and_foreign_kinds_are_ignored():
    assert fold_output_copy([]) == []
    assert fold_output_copy([verified(T, "e1")]) == []
    other = {"kind": "run.started", "entity": [RUN], "ts": T, "eid": "x", "data": {}}
    assert fold_output_copy([other]) == []
