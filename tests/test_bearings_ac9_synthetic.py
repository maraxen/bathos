"""AC-9, synthetic half (spec 261104, T7(a)): charter comparison on planted mappings, with no live reader.

The charter-comparison job is: a reading is mapped, by an agent that is not the reader, onto the confirmed charter, and
bathos stores `mapped`, `unmapped` and `silent` items for the owner to judge. A live reader is not needed to check the
bookkeeping, so these tests plant the mapping and ask whether bathos classifies it correctly. They test only what
`compare_reading` does with a mapping. They do not test whether a real reader or mapper maps well. That half of AC-9
needs an owner-written charter, a record with a planted subset of purposes, owner-set thresholds and live readers, and
is pre-registered separately before any live reading.

What is covered here that `tests/test_bearings_reading.py` does not cover (it uses a three-purpose charter):
a planted-subset sweep on a larger charter (silent is exactly the live purposes no reading purpose maps to), negative
controls (nothing mapped; everything mapped), retired purposes never reported silent, a retired match not covering a
live purpose, two reading purposes on one charter purpose, open lines never covering a charter purpose, and a stable,
sorted order for the silent items.

A brief-render check (also named in T7(a)) is not here: the brief renderer is slice 3 (AC-6) and does not exist yet.
"""

from __future__ import annotations

import json

import pytest

from bathos.bearings.packet import build_packet
from bathos.bearings.reading import compare_reading, record_reading
from bathos.charter import Purpose, confirm
from tests.test_bearings_packet import (  # noqa: F401  (env is a fixture)
    DATE,
    SINCE,
    TOKEN,
    UNTIL,
    commit_files,
    env,
    insert_runs,
    write_charter,
)

LIVE = ["L1", "L2", "L3", "L4", "L5", "L6"]
RETIRED = ["R1", "R2"]


@pytest.fixture
def world(env):  # noqa: F811
    """A confirmed charter with six live and two retired purposes, a committed repo with runs, and a built packet."""
    repo = env["repo"]
    purposes = {pid: Purpose(id=pid, text=f"Live aim {pid}", state="live") for pid in LIVE}
    purposes |= {
        pid: Purpose(
            id=pid, text=f"Retired aim {pid}", state="retired", retired_reason="superseded"
        )
        for pid in RETIRED
    }
    write_charter(repo, purposes=purposes)
    confirm(repo, chair_token=TOKEN, interactive_tty=False, catalog_dir=env["cat"])
    sha = commit_files(repo, {"scripts/a.py": "print(1)\n"}, msg="initial work")
    insert_runs(
        env,
        [{"id": "22222222cccccccc", "project_slug": "pkt", "timestamp": DATE, "git_hash": sha}],
    )
    out = env["tmp"] / "packet"
    res = build_packet(repo, out=out, since=SINCE, until=UNTIL, catalog_dir=env["cat"])
    return {
        **env,
        "packet": out,
        "packet_sha": res["packet_sha256"],
        "good": ["run:22222222cccccccc", "git:" + sha[:10]],
    }


def record(w, n_purposes: int, open_lines: int = 0) -> str:
    """Record a reading with `n_purposes` purposes r1..rN (each with two resolvable citations)."""
    reading = {
        "packet_sha256": w["packet_sha"],
        "reader_id": "reader-A",
        "purposes": [
            {"id": f"r{i}", "text": f"Reading purpose {i}", "citations": w["good"]}
            for i in range(1, n_purposes + 1)
        ],
        "open_lines": [
            {"text": f"open line {i}", "citations": w["good"][:1]} for i in range(open_lines)
        ],
    }
    path = w["tmp"] / f"reading_{n_purposes}_{open_lines}.json"
    path.write_text(json.dumps(reading))
    return record_reading(
        w["repo"], reading_file=path, packet_dir=w["packet"], catalog_dir=w["cat"]
    )["reading_id"]


def compare(w, rid: str, targets: list[str | None], name: str = "m") -> dict:
    """Compare with a mapping r1..rN -> targets, from a mapper that is not the reader."""
    path = w["tmp"] / f"{name}.json"
    path.write_text(
        json.dumps(
            {"mapper_id": "mapper-B", "mapping": {f"r{i}": t for i, t in enumerate(targets, 1)}}
        )
    )
    return compare_reading(w["repo"], reading_id=rid, mapping_file=path, catalog_dir=w["cat"])


def silent(res: dict) -> list[str]:
    return [it["charter_purpose_id"] for it in res["items"] if it["kind"] == "silent"]


def of_kind(res: dict, kind: str) -> list[dict]:
    return [it for it in res["items"] if it["kind"] == kind]


@pytest.mark.parametrize(
    "covered",
    [[], ["L1"], ["L6"], ["L1", "L3", "L5"], ["L2", "L3", "L4", "L5"], LIVE],
    ids=["none", "first", "last", "odd", "middle", "all"],
)
def test_silent_is_exactly_the_live_purposes_no_reading_purpose_maps_to(world, covered):
    """Planted subset sweep: whatever subset the mapping covers, silent is its complement among the live purposes."""
    targets: list[str | None] = list(covered) or [None]  # a reading needs at least one purpose
    res = compare(world, record(world, len(targets)), targets)
    assert sorted(silent(res)) == sorted(set(LIVE) - set(covered))
    assert len(of_kind(res, "mapped")) == len(covered)
    assert res["counts"] == {
        "mapped": len(covered),
        "unmapped": 0 if covered else 1,
        "silent": len(LIVE) - len(covered),
    }


def test_nothing_mapped_leaves_every_live_purpose_silent_and_no_retired_one(world):
    """Negative control: a reading that maps to no charter purpose cannot cover anything."""
    res = compare(world, record(world, 3), [None, None, None])
    assert sorted(silent(res)) == LIVE
    assert [it["kind"] for it in res["items"]].count("unmapped") == 3
    assert not set(silent(res)) & set(RETIRED)


def test_everything_mapped_leaves_nothing_silent(world):
    """Positive control: when every live purpose has a reading purpose mapped to it, nothing is silent."""
    res = compare(world, record(world, 6), LIVE)
    assert silent(res) == []
    assert res["counts"] == {"mapped": 6, "unmapped": 0, "silent": 0}


def test_a_retired_purpose_is_never_silent_even_when_nothing_maps_to_it(world):
    res = compare(world, record(world, 1), ["L1"])
    assert "R1" not in silent(res) and "R2" not in silent(res)
    assert sorted(silent(res)) == LIVE[1:]


def test_a_match_to_a_retired_purpose_is_unmapped_with_the_flag_and_covers_no_live_purpose(world):
    res = compare(world, record(world, 2), ["R1", "L2"])
    retired_hit = next(
        it for it in of_kind(res, "unmapped") if it.get("reading_purpose_id") == "r1"
    )
    assert retired_hit["matched_retired"] == "R1"
    assert [it["reading_purpose_id"] for it in of_kind(res, "mapped")] == ["r2"]
    assert sorted(silent(res)) == sorted(
        set(LIVE) - {"L2"}
    )  # L1 stays silent: its retired sibling does not cover it


def test_two_reading_purposes_on_one_charter_purpose_are_both_mapped_and_it_is_silent_nowhere(
    world,
):
    res = compare(world, record(world, 2), ["L2", "L2"])
    assert [it["charter_purpose_id"] for it in of_kind(res, "mapped")] == ["L2", "L2"]
    assert sorted(silent(res)) == sorted(set(LIVE) - {"L2"})
    assert silent(res).count("L1") == 1  # each silent purpose appears once


def test_open_lines_are_unmapped_and_cover_no_charter_purpose(world):
    res = compare(world, record(world, 1, open_lines=3), [None])
    open_items = [it for it in of_kind(res, "unmapped") if "open_line_index" in it]
    assert sorted(it["open_line_index"] for it in open_items) == [0, 1, 2]
    assert sorted(silent(res)) == LIVE


def test_silent_items_are_in_charter_id_order_and_stable_across_repeats(world):
    rid = record(world, 2)
    first = compare(world, rid, ["L5", "L2"])
    assert silent(first) == sorted(silent(first)) == ["L1", "L3", "L4", "L6"]
    again = compare(world, rid, ["L5", "L2"])
    assert silent(again) == silent(first)
    assert [it["item_id"] for it in again["items"]] == [it["item_id"] for it in first["items"]]


def test_the_mapping_alone_decides_what_is_silent(world):
    """The same reading under two mappings gives two silent sets: bathos adds no judgement of its own."""
    rid = record(world, 2)
    one = compare(world, rid, ["L1", "L2"], name="m1")
    two = compare(world, rid, ["L3", "L4"], name="m2")
    assert sorted(silent(one)) == ["L3", "L4", "L5", "L6"]
    assert sorted(silent(two)) == ["L1", "L2", "L5", "L6"]


def compare_raw(w, rid: str, mapping: dict, name: str = "raw") -> dict:
    """Compare with a mapping given as-is (lists, nulls, bad values)."""
    path = w["tmp"] / f"{name}.json"
    path.write_text(json.dumps({"mapper_id": "mapper-B", "mapping": mapping}))
    return compare_reading(w["repo"], reading_id=rid, mapping_file=path, catalog_dir=w["cat"])


def test_one_reading_purpose_mapped_to_two_live_purposes_covers_both(world):
    """One strand of work serving two aims: neither aim is silent (a one-target limit left one falsely silent)."""
    res = compare_raw(world, record(world, 1), {"r1": ["L3", "L1"]})
    assert sorted(it["charter_purpose_id"] for it in of_kind(res, "mapped")) == ["L1", "L3"]
    assert sorted(silent(res)) == ["L2", "L4", "L5", "L6"]
    assert res["counts"] == {"mapped": 2, "unmapped": 0, "silent": 4}


def test_a_list_mixing_live_and_retired_maps_the_live_one_and_flags_the_retired_one(world):
    res = compare_raw(world, record(world, 1), {"r1": ["L2", "R2"]})
    assert [it["charter_purpose_id"] for it in of_kind(res, "mapped")] == ["L2"]
    assert [it["matched_retired"] for it in of_kind(res, "unmapped")] == ["R2"]
    assert "L2" not in silent(res)


@pytest.mark.parametrize("empty", [None, []], ids=["null", "empty-list"])
def test_null_and_empty_list_both_mean_unmapped(world, empty):
    res = compare_raw(world, record(world, 1), {"r1": empty})
    assert res["counts"] == {"mapped": 0, "unmapped": 1, "silent": len(LIVE)}


def test_a_single_string_and_a_one_item_list_give_the_same_items(world):
    rid = record(world, 1)
    as_str = compare_raw(world, rid, {"r1": "L4"}, name="s")
    as_list = compare_raw(world, rid, {"r1": ["L4"]}, name="l")
    assert [it["item_id"] for it in as_str["items"]] == [it["item_id"] for it in as_list["items"]]


@pytest.mark.parametrize(
    "bad",
    [["L1", "L1"], ["L1", "NOPE"], ["L1", 3], 7, {"L1": True}],
    ids=["repeat", "unknown-id", "non-string-in-list", "number", "object"],
)
def test_malformed_multi_target_mappings_are_refused(world, bad):
    from bathos.bearings.reading import BearingsMappingInvalidError

    with pytest.raises(BearingsMappingInvalidError):
        compare_raw(world, record(world, 1), {"r1": bad})


def _plant_budget(w, runs: list[str], commits: list[str]) -> None:
    """Add a budget block to the packet manifest (outside the packet hash), shaped as build_packet_within_budget
    writes it."""
    manifest_path = w["packet"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["budget"] = {
        "max_bytes": 1000,
        "bytes": 900,
        "full_window_bytes": 5000,
        "requested_since": SINCE,
        "effective_since": SINCE,
        "window_shortened": bool(runs or commits),
        "dropped": {"reason": "budget", "runs": runs, "commits": commits},
    }
    manifest_path.write_text(json.dumps(manifest))


def test_silent_items_are_budget_limited_when_the_packet_dropped_runs(world):
    """AC-4b: a purpose whose evidence was cut for budget must be distinguishable from a truly silent one."""
    _plant_budget(world, runs=["33333333dddddddd"], commits=[])
    res = compare(world, record(world, 1), ["L1"])
    assert res["budget"]["dropped_runs"] == ["33333333dddddddd"]
    assert res["budget"]["window_shortened"] is True
    assert {it["budget_limited"] for it in of_kind(res, "silent")} == {True}


def test_silent_items_are_not_budget_limited_when_the_budget_dropped_nothing(world):
    """Negative control: a budgeted packet that kept everything flags nothing."""
    _plant_budget(world, runs=[], commits=[])
    res = compare(world, record(world, 1), ["L1"])
    assert res["budget"]["dropped_runs"] == [] and res["budget"]["dropped_commits"] == []
    assert {it["budget_limited"] for it in of_kind(res, "silent")} == {False}


def test_an_unbudgeted_packet_has_no_budget_and_flags_nothing(world):
    res = compare(world, record(world, 1), ["L1"])
    assert res["budget"] is None
    assert {it["budget_limited"] for it in of_kind(res, "silent")} == {False}
