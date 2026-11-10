"""AC-9 planted set (spec 261104 T7(a)): a synthetic project whose sidecars carry purpose labels, built into a blind
packet, then scored. Checks that the answer key comes from the labels, that an oracle mapping meets pass, that mappings
with no knowledge (degenerate) do not, that a budget-cut packet scores no silent case, and that the decoy charter is
disjoint from the tev_design draft."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bathos.bearings.ac9 import (
    OFF_CHARTER,
    Ac9LabelError,
    ac9_metrics,
    answer_key,
    degenerate_mappings,
    meets_pass,
    reading_gold,
)
from bathos.bearings.reading import _existing_reading, compare_reading, record_reading
from bathos.charter import Purpose, confirm, parse_charter
from tests.test_bearings_packet import (  # noqa: F401  (env is a fixture)
    DATE,
    SINCE,
    TOKEN,
    build,
    commit_files,
    env,
    insert_runs,
    reader_files,
    write_charter,
)

AC9_DIR = Path(__file__).resolve().parents[1] / "scripts" / "analysis" / "ac9"

LIVE = {
    "alpha-aim": "Measure how fast the reporter strain grows on each carbon source",
    "beta-aim": "Map which promoter variants switch on under nitrogen limitation",
    "gamma-aim": "Build a growth-curve fitting pipeline with uncertainty bands",
    "delta-aim": "Compare plate-reader runs across two instruments",
}
RETIRED = {
    "old-aim": "Screen a legacy knockout collection by hand",
    "older-aim": "Use a spreadsheet as the record of plate layouts",
}

# (name, label line or None, hypothesis). Hypotheses never mention a slug.
SIDECARS = [
    (
        "grow",
        '[charter]\npurpose = "alpha-aim"\n',
        "growth rate differs between glucose and xylose",
    ),
    ("promo", '[charter]\npurpose = "beta-aim"\n', "promoter p7 is induced by low ammonium"),
    ("legacy", '[charter]\npurpose = "old-aim"\n', "manual knockout screen finds slow growers"),
    ("tidy", f'[charter]\npurpose = "{OFF_CHARTER}"\n', "renaming result folders changes nothing"),
    ("plain", None, "a run with no label at all"),
]


def _sidecar(label: str | None, hypothesis: str) -> str:
    return (label + "\n" if label else "") + f'[experiment]\nhypothesis = "{hypothesis}"\n'


@pytest.fixture
def planted(env):  # noqa: F811
    repo = env["repo"]
    purposes = {p: Purpose(id=p, text=t, state="live") for p, t in LIVE.items()}
    purposes |= {
        p: Purpose(id=p, text=t, state="retired", retired_reason="superseded")
        for p, t in RETIRED.items()
    }
    write_charter(
        repo, purposes=purposes, mandate="Understand how the reporter strain responds to its medium"
    )
    confirm(repo, chair_token=TOKEN, interactive_tty=False, catalog_dir=env["cat"])
    sha = commit_files(
        repo, {f"scripts/{n}.bth.toml": _sidecar(lbl, h) for n, lbl, h in SIDECARS}, msg="work"
    )
    runs = {n: f"{i + 1:08x}aaaaaaaa" for i, (n, _, _) in enumerate(SIDECARS)}
    insert_runs(
        env,
        [
            {
                "id": rid,
                "project_slug": "pkt",
                "timestamp": DATE,
                "git_hash": sha,
                "sidecar_path": f"scripts/{n}.bth.toml",
                "command": f"python {n}",
                "status": "completed",
            }
            for n, rid in runs.items()
        ],
    )
    charter = {p: {"state": v.state} for p, v in purposes.items()}
    return {**env, "runs": runs, "charter": charter}


def _packet(w, name="packet"):
    out, res = build(w, name=name)
    return out, res["packet_sha256"]


def _record(w, out: Path, sha: str) -> str:
    reading = {
        "packet_sha256": sha,
        "reader_id": "reader-A",
        "purposes": [
            {
                "id": f"r-{n}",
                "text": f"work on {n}",
                "citations": [f"run:{w['runs'][n]}", f"sidecar:scripts/{n}.bth.toml"],
            }
            for n, _, _ in SIDECARS
        ],
    }
    path = w["tmp"] / f"reading_{out.name}.json"
    path.write_text(json.dumps(reading))
    return record_reading(w["repo"], reading_file=path, packet_dir=out, catalog_dir=w["cat"])[
        "reading_id"
    ]


def _compare(w, rid: str, mapping: dict, name: str) -> dict:
    path = w["tmp"] / f"map_{name}.json"
    path.write_text(json.dumps({"mapper_id": "mapper-B", "mapping": mapping}))
    return compare_reading(w["repo"], reading_id=rid, mapping_file=path, catalog_dir=w["cat"])


ORACLE = {
    "r-grow": "alpha-aim",
    "r-promo": "beta-aim",
    "r-legacy": "old-aim",
    "r-tidy": None,
    "r-plain": None,
}


def _score(w, out: Path, rid: str, mapping: dict, name: str):
    key = answer_key(w["repo"], out, w["charter"])
    reading = _existing_reading(w["cat"], "pkt", rid)
    gold = reading_gold(out, reading, key["labels"])
    return key, gold, ac9_metrics(key, gold, _compare(w, rid, mapping, name))


def test_the_answer_key_comes_from_the_labels(planted):
    out, _ = _packet(planted)
    key = answer_key(planted["repo"], out, planted["charter"])
    assert key["present"] == ["alpha-aim", "beta-aim"]
    assert key["silent"] == ["delta-aim", "gamma-aim"]
    assert key["revived"] == ["old-aim"]
    assert key["absent_retired"] == ["older-aim"]
    assert key["off_charter_sidecars"] == ["scripts/tidy.bth.toml"]
    assert key["budget_limited"] is False
    assert "scripts/plain.bth.toml" not in key["labels"]


def test_no_label_reaches_the_reader(planted):
    out, _ = _packet(planted)
    blobs = reader_files(out)
    assert not any(b"[charter]" in v for v in blobs.values())
    for slug in [*LIVE, *RETIRED, OFF_CHARTER]:
        assert not any(slug.encode() in v for v in blobs.values()), slug


def test_reading_gold_is_the_labels_of_what_each_purpose_cites(planted):
    out, sha = _packet(planted)
    rid = _record(planted, out, sha)
    key = answer_key(planted["repo"], out, planted["charter"])
    gold = reading_gold(out, _existing_reading(planted["cat"], "pkt", rid), key["labels"])
    assert gold == {
        "r-grow": ["alpha-aim"],
        "r-promo": ["beta-aim"],
        "r-legacy": ["old-aim"],
        "r-tidy": [OFF_CHARTER],
        "r-plain": [],
    }


def test_an_oracle_mapping_meets_pass_on_every_metric(planted):
    out, sha = _packet(planted)
    _, _, m = _score(planted, out, _record(planted, out, sha), ORACLE, "oracle")
    assert m["silent_recall"] == 1.0 and m["n_silent"] == 2
    assert m["false_silent"] == 0.0 and m["n_present"] == 2
    assert m["off_charter_unmapped"] == 1.0 and m["n_off_charter"] == 1
    assert m["revived_flagged"] == 1.0 and m["n_revived"] == 1
    assert m["gold_agreement"] == 1.0 and m["n_mapped"] == 2
    assert meets_pass(m)


@pytest.mark.parametrize("name", ["all_unmapped", "all_to_first_live", "each_to_every_purpose"])
def test_degenerate_mappings_never_meet_pass(planted, name):
    out, sha = _packet(planted)
    rid = _record(planted, out, sha)
    mapping = degenerate_mappings(list(ORACLE), planted["charter"])[name]
    _, _, m = _score(planted, out, rid, mapping, name)
    assert not meets_pass(m), m


def test_a_mapping_that_maps_off_charter_work_fails_the_off_charter_check(planted):
    out, sha = _packet(planted)
    _, _, m = _score(
        planted, out, _record(planted, out, sha), {**ORACLE, "r-tidy": "gamma-aim"}, "leaky"
    )
    assert m["off_charter_unmapped"] == 0.0
    assert m["silent_recall"] == 0.5  # gamma is no longer reported silent
    assert not meets_pass(m)


def test_a_budget_cut_packet_scores_no_silent_case(planted):
    out, sha = _packet(planted)
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["budget"] = {
        "max_bytes": 1,
        "bytes": 1,
        "full_window_bytes": 2,
        "requested_since": SINCE,
        "effective_since": SINCE,
        "window_shortened": True,
        "dropped": {"reason": "budget", "runs": ["ffffffffaaaaaaaa"], "commits": []},
    }
    manifest_path.write_text(json.dumps(manifest))
    key, _, m = _score(planted, out, _record(planted, out, sha), ORACLE, "budget")
    assert key["budget_limited"] is True
    assert m["silent_recall"] is None and m["false_silent"] is None
    assert not meets_pass(m)  # an unscorable metric is never a pass


def test_a_label_naming_no_charter_purpose_is_refused(planted):
    sha = commit_files(
        planted["repo"],
        {"scripts/typo.bth.toml": _sidecar('[charter]\npurpose = "typo-aim"\n', "x")},
    )
    insert_runs(
        planted,
        [
            {
                "id": "00000099aaaaaaaa",
                "project_slug": "pkt",
                "timestamp": DATE,
                "git_hash": sha,
                "sidecar_path": "scripts/typo.bth.toml",
                "command": "python typo",
                "status": "completed",
            }
        ],
    )
    out, _ = _packet(planted)
    with pytest.raises(Ac9LabelError):
        answer_key(planted["repo"], out, planted["charter"])


def test_the_decoy_charter_shares_no_purpose_with_the_tev_design_draft():
    decoy = parse_charter(AC9_DIR / "decoy_charter.toml")
    draft = parse_charter(AC9_DIR / "tev_design_charter.DRAFT.toml")
    assert not set(decoy.purposes) & set(draft.purposes)
    assert sorted(p.state for p in decoy.purposes.values()) == ["live"] * 6 + ["retired"] * 2
