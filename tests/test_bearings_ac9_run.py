"""AC-9 run machinery (spec 261104 T7, owner 261110): the frozen planting rule, pooled scoring per T7(b), the
owner's withheld-evidence bounds, the planted-case check and the label-built oracle reading. Each has a negative
control that must fail."""

from __future__ import annotations

import json

import pytest

import bathos.bearings.ac9 as ac9
from bathos.bearings.ac9 import (
    BOUNDS,
    OFF_CHARTER,
    Ac9LabelError,
    answer_key,
    bound_violations,
    load_label_overlay,
    meets_pass,
    missing_cases,
    oracle_reading,
    plan_planted_packets,
    pooled_metrics,
    reading_gold,
)
from bathos.bearings.reading import _existing_reading, compare_reading, record_reading
from tests.test_bearings_ac9_planted import _packet, planted  # noqa: F401  (planted is a fixture)
from tests.test_bearings_packet import env  # noqa: F401

LIVE = [f"live-{c}" for c in "abcdef"]
RETIRED = ["old-x", "old-y"]
CHARTER = {p: {"state": "live"} for p in LIVE} | {p: {"state": "retired"} for p in RETIRED}


def _runs(per_live: int = 3, revived: int = 2, off: int = 3) -> dict[str, list[str]]:
    runs: dict[str, list[str]] = {}
    for p in LIVE:
        for i in range(per_live):
            runs[f"{p}-r{i}"] = [p]
    for i in range(revived):
        runs[f"old-x-r{i}"] = ["old-x"]
    for i in range(off):
        runs[f"off-r{i}"] = [OFF_CHARTER]
    runs["mixed-r0"] = ["live-a", "live-b"]
    return runs


# ── planting ─────────────────────────────────────────────────────────────────────────────────────────────────────


def test_three_packets_plant_every_case_with_different_silent_sets():
    plans, unmet = plan_planted_packets(_runs(), CHARTER)
    assert unmet == [] and len(plans) == 3
    silents = [tuple(p["silent"]) for p in plans]
    assert len(set(silents)) == 3  # a reader that maps the same way every time cannot pass
    for p in plans:
        assert len(p["silent"]) == 2 and len(p["present"]) == 4
        assert p["revived"] == "old-x" and p["absent_retired"] == ["old-y"]
        kept_labels = {s for rid in p["runs"] for s in _runs()[rid]}
        assert not kept_labels & set(p["silent"])  # no run of a planted-silent purpose is kept
        assert OFF_CHARTER in kept_labels and "old-x" in kept_labels


def test_planting_is_deterministic_and_depends_on_the_seed(monkeypatch):
    a, _ = plan_planted_packets(_runs(), CHARTER)
    assert a == plan_planted_packets(_runs(), CHARTER)[0]
    monkeypatch.setattr(ac9, "PLANT_SEED", "another-seed")
    b, _ = plan_planted_packets(_runs(), CHARTER)
    assert [p["silent"] for p in a] != [
        p["silent"] for p in b
    ]  # negative control: the seed decides


def test_the_cap_limits_runs_per_purpose():
    plans, _ = plan_planted_packets(_runs(per_live=25), CHARTER)
    for p in plans:
        counts: dict[str, int] = {}
        for rid in p["runs"]:
            first = sorted(_runs(per_live=25)[rid])[0]
            counts[first] = counts.get(first, 0) + 1
        assert max(counts.values()) <= ac9.MAX_RUNS_PER_PURPOSE


@pytest.mark.parametrize(
    "runs,why",
    [
        (
            {k: v for k, v in _runs().items() if not k.startswith(("live-e", "live-f"))},
            "live purposes",
        ),
        (_runs(revived=0), "revival"),
        (_runs(off=1), OFF_CHARTER),
    ],
    ids=["too-few-live", "no-revival", "no-off-charter"],
)
def test_an_insufficient_record_plants_nothing_and_says_why(runs, why):
    plans, unmet = plan_planted_packets(runs, CHARTER)
    assert plans == [] and any(why in u for u in unmet)


def test_without_required_revival_a_record_with_no_revival_still_plants():
    # AC-9 amendment 3 (owner 261110): every retired purpose is then absent in every packet.
    plans, unmet = plan_planted_packets(_runs(revived=0), CHARTER, require_revival=False)
    assert unmet == [] and len(plans) == 3
    assert all(p["revived"] is None and p["absent_retired"] == RETIRED for p in plans)
    # ... and a record that has one still revives it.
    plans, _ = plan_planted_packets(_runs(), CHARTER, require_revival=False)
    assert all(p["revived"] == "old-x" for p in plans)


def test_without_gating_revival_pass_and_cases_ignore_it():
    m = {
        "silent_recall": 1.0,
        "false_silent": 0.0,
        "off_charter_unmapped": 1.0,
        "revived_flagged": None,
        "gold_agreement": 1.0,
    }
    assert not meets_pass(m) and meets_pass(m, gate_revived=False)
    assert ac9.meets_marginal(m, gate_revived=False)
    key = _full_key(revived=[])
    assert missing_cases(key) == ["revived"] and missing_cases(key, require_revival=False) == []
    # Negative control: the other metrics still gate.
    assert not meets_pass(m | {"off_charter_unmapped": 0.5}, gate_revived=False)


# ── pooling (T7(b)) ──────────────────────────────────────────────────────────────────────────────────────────────


def _key(present, silent, revived=(), budget=False):
    return {
        "present": list(present),
        "silent": list(silent),
        "revived": list(revived),
        "budget_limited": budget,
    }


def _cmp(silent_reported, mapped=()):
    items = [{"kind": "silent", "charter_purpose_id": p} for p in silent_reported]
    items += [
        {"kind": "mapped", "reading_purpose_id": rp, "charter_purpose_id": t} for rp, t in mapped
    ]
    return {"items": items}


def test_a_purpose_is_silent_only_if_no_reader_maps_it():
    key = _key(present=["a", "b"], silent=["s1", "s2"])
    readings = [
        {
            "gold": {},
            "comparison": _cmp(["s1", "s2", "a"]),
        },  # one reader also calls a present purpose silent
        {"gold": {}, "comparison": _cmp(["s1", "s2"])},
        {"gold": {}, "comparison": _cmp(["s1"])},  # one reader maps s2 (does not report it silent)
    ]
    m = pooled_metrics([{"key": key, "readings": readings}])
    assert m["false_silent"] == 0.0  # the intersection drops the one reader's false silent
    assert m["silent_recall"] == 0.5 and m["n_silent"] == 2  # ... and s2, which one reader mapped
    assert m["silent_recall_majority"] == 1.0  # the majority rule, reported beside it


def test_pooling_sums_hits_and_denominators_not_rates():
    p1 = {"key": _key(["a"], ["s1"]), "readings": [{"gold": {}, "comparison": _cmp(["s1"])}]}
    p2 = {
        "key": _key(["a"], ["s1", "s2", "s3"]),
        "readings": [{"gold": {}, "comparison": _cmp([])}],
    }
    m = pooled_metrics([p1, p2])
    assert m["n_silent"] == 4 and m["silent_recall"] == pytest.approx(
        1 / 4
    )  # mean of rates would be 0.5


def test_off_charter_and_gold_agreement_are_counted_per_reading():
    key = _key(["a"], ["s1"])
    gold = {"r-a": ["a"], "r-off": [OFF_CHARTER]}
    good = {"gold": gold, "comparison": _cmp(["s1"], mapped=[("r-a", "a")])}
    leaky = {"gold": gold, "comparison": _cmp(["s1"], mapped=[("r-a", "a"), ("r-off", "a")])}
    m = pooled_metrics([{"key": key, "readings": [good, good, leaky]}])
    assert m["n_off_charter"] == 3 and m["off_charter_unmapped"] == pytest.approx(2 / 3)
    assert m["n_mapped"] == 4 and m["gold_agreement"] == pytest.approx(3 / 4)


# ── bounds and cases ─────────────────────────────────────────────────────────────────────────────────────────────


def _full_key(**over):
    base = {
        "present": ["live-a"],
        "silent": ["live-b"],
        "revived": ["old-x"],
        "absent_retired": ["old-y"],
        "off_charter_sidecars": ["scripts/tidy.bth.toml"],
        "indeterminate": [],
        "n_withheld_sidecars": 0,
        "n_shown_sidecars_labelled": 20,
        "n_withheld_runs": 0,
        "n_shown_runs": 40,
        "n_withheld_runs_unresolved": 0,
        "ambiguous_paths": [],
    }
    return base | over


def test_a_clean_key_is_within_bounds_and_plants_every_case():
    assert bound_violations(_full_key(), CHARTER) == [] and missing_cases(_full_key()) == []


@pytest.mark.parametrize(
    "over",
    [
        {"indeterminate": ["live-a", "live-b"]},
        {"n_withheld_sidecars": 3},  # 3 / 23 > 0.10
        {"n_withheld_runs": 5},  # 5 / 45 > 0.10
        {"ambiguous_paths": ["a", "b"]},
        {"n_withheld_runs_unresolved": 1},
    ],
    ids=["indeterminate", "withheld-sidecars", "withheld-runs", "ambiguous", "unresolved"],
)
def test_each_bound_fires(over):
    assert bound_violations(_full_key(**over), CHARTER)


def test_the_bounds_are_the_owners():
    assert BOUNDS == {
        "indeterminate_live_max": 1,
        "indeterminate_live_frac": 0.15,
        "withheld_sidecar_frac": 0.10,
        "withheld_run_frac": 0.10,
        "ambiguous_paths_max": 1,
    }


@pytest.mark.parametrize(
    "case", ["present", "silent", "revived", "absent_retired", "off_charter_sidecars"]
)
def test_a_missing_case_is_reported(case):
    assert missing_cases(_full_key(**{case: []})) == [case]


# ── oracle reading (mapper-only arm) ─────────────────────────────────────────────────────────────────────────────


def test_the_oracle_reading_records_and_a_correct_mapping_of_it_passes(planted):  # noqa: F811
    out, sha = _packet(planted)
    key = answer_key(planted["repo"], out, planted["charter"])
    reading = oracle_reading(out, key["labels"], sha)
    text = json.dumps(reading)
    for slug in [*planted["charter"], OFF_CHARTER]:
        assert slug not in text  # nothing in the oracle reading names a charter id
    path = planted["tmp"] / "oracle.json"
    path.write_text(json.dumps(reading))
    rid = record_reading(
        planted["repo"], reading_file=path, packet_dir=out, catalog_dir=planted["cat"]
    )["reading_id"]
    stored = _existing_reading(planted["cat"], "pkt", rid)
    gold = reading_gold(out, stored, key["labels"])
    mapping = {
        rp: next((s for s in g if s != OFF_CHARTER), None) for rp, g in gold.items()
    }  # a mapper that knows the answer
    mp = planted["tmp"] / "oracle_map.json"
    mp.write_text(json.dumps({"mapper_id": "mapper-oracle", "mapping": mapping}))
    cmp = compare_reading(
        planted["repo"], reading_id=rid, mapping_file=mp, catalog_dir=planted["cat"]
    )
    m = pooled_metrics([{"key": key, "readings": [{"gold": gold, "comparison": cmp}]}])
    assert meets_pass(m), m


# ── owner-confirmed label overlay (261110) ───────────────────────────────────────────────────────────────────────


def test_the_overlay_labels_a_sidecar_whose_blob_has_none(planted):  # noqa: F811
    # plain.bth.toml carries no label; a past run's blob never gains one, so the overlay supplies it.
    out, _ = _packet(planted)
    key = answer_key(
        planted["repo"], out, planted["charter"], {"scripts/plain.bth.toml": ["gamma-aim"]}
    )
    assert "gamma-aim" in key["present"] and key["labels"]["scripts/plain.bth.toml"] == [
        "gamma-aim"
    ]
    assert key["n_shown_sidecars_labelled"] == 5


def test_an_overlay_disagreeing_with_a_blob_label_makes_the_path_ambiguous(planted):  # noqa: F811
    out, _ = _packet(planted)
    key = answer_key(
        planted["repo"], out, planted["charter"], {"scripts/grow.bth.toml": ["delta-aim"]}
    )
    assert "scripts/grow.bth.toml" in key["ambiguous_paths"]
    assert {"alpha-aim", "delta-aim"} <= set(key["indeterminate"])


def test_an_overlay_agreeing_with_a_blob_label_changes_nothing(planted):  # noqa: F811
    out, _ = _packet(planted)
    base = answer_key(planted["repo"], out, planted["charter"])
    same = answer_key(
        planted["repo"], out, planted["charter"], {"scripts/grow.bth.toml": ["alpha-aim"]}
    )
    assert same == base


@pytest.mark.parametrize(
    "value",
    ['"L5"', '["off-charter", "alpha-aim"]', '"a--b"', "5"],
    ids=["opaque", "mixed", "hyphens", "number"],
)
def test_a_malformed_overlay_label_is_refused(tmp_path, value):
    f = tmp_path / "label_confirmation.toml"
    f.write_text(f'since = "x"\n\n[labels]\n"scripts/a.bth.toml" = {value}\n')
    with pytest.raises(Ac9LabelError, match="scripts/a.bth.toml"):
        load_label_overlay(f)


def test_a_wellformed_overlay_loads(tmp_path):
    f = tmp_path / "label_confirmation.toml"
    f.write_text(
        '[labels]\n"scripts/a.bth.toml" = "alpha-aim"\n"scripts/b.bth.toml" = ["b-aim", "a-aim"]\n'
    )
    assert load_label_overlay(f) == {
        "scripts/a.bth.toml": ["alpha-aim"],
        "scripts/b.bth.toml": ["a-aim", "b-aim"],
    }
