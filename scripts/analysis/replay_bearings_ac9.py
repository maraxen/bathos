"""Bearings AC-9 (spec 261104 T7): does charter comparison work? The shadow -> advisory promotion gate for Bearings.

Three planted packets are cut from tev_design's labelled record by a rule fixed before any label existed
(``bathos.bearings.ac9.plan_planted_packets``). Three readers from two model families read each; a mapper maps each
reading onto the confirmed charter; scores pool over readers and packets per T7(b)
(``bathos.bearings.ac9.pooled_metrics``). A mapper-only arm maps a reading built from the labels
(``oracle_reading``). The nanobody decoy charter is the negative control. A budget-cut packet is a control only.

Modes:
- ``--gate``: check the start gate and write ``gate.json``. Read-only on the catalog and the repo.
- ``--controls-only``: synthetic controls (planting, pooling, bounds, oracle); must pass before any score is read.
- ``--build``: build the planted packets, the budget control and the oracle readings (refuses unless the gate passed).
- the tracked run (``bth run``): score a work directory whose readings and comparisons are recorded (refuses unless
  the gate passed and every packet is built).

The start gate (owner items) is in the sidecar. Nothing here writes to tev_design.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tomllib
from pathlib import Path

from bathos.bearings import ac9
from bathos.bearings.packet import build_packet, build_packet_within_budget, verify_packet
from bathos.charter import parse_charter
from bathos.charter import status as charter_status

logger = logging.getLogger("replay_bearings_ac9")

PROJECT = "tev_design"
HERE = Path(__file__).resolve().parent
DECOY = HERE / "ac9" / "decoy_charter.toml"
# Owner-written until cisternal's shadow.label owner_confirmed events exist: since, until, ref, confirmed_through.
LABEL_CONFIRMATION = HERE / "ac9" / "label_confirmation.toml"
READERS = (
    "reader1",
    "reader2",
    "reader3",
)  # reader1 and reader3 on Cursor, reader2 on Antigravity (two families)


def _charter_purposes(path: Path) -> dict[str, dict]:
    ch = parse_charter(path)
    return {p: {"state": v.state} for p, v in ch.purposes.items()}


def _run_labels(packet_dir: Path, key: dict) -> dict[str, list[str]]:
    runs = [
        json.loads(x)
        for x in (packet_dir / "reader" / "runs.jsonl").read_text(encoding="utf-8").split("\n")
        if x
    ]
    return {
        str(r["id"]): key["labels"][r["sidecar_path"]]
        for r in runs
        if r.get("sidecar_path") in key["labels"]
    }


def gate(repo: Path, work: Path, catalog_dir: Path | None) -> dict:
    """The start gate. Every item must hold before ``--build``; the result is written to ``work/gate.json``."""
    reasons: list[str] = []
    out: dict = {"start_gate_ok": False}
    try:
        st = charter_status(repo, catalog_dir=catalog_dir)
        out["charter_state"] = st.get("state")
        out["charter_sha256"] = st.get("last_confirmed_sha256")
        if st.get("state") != "confirmed":
            reasons.append(
                f"tev_design charter is {st.get('state')!r}, not confirmed (owner: bth charter confirm)"
            )
    except Exception as e:  # noqa: BLE001 -- the gate reports every reason, it never raises
        reasons.append(f"tev_design charter unreadable: {e}")
    if not LABEL_CONFIRMATION.is_file():
        reasons.append(f"no owner label confirmation at {LABEL_CONFIRMATION.name}")
    if reasons:
        out["unmet"] = reasons
        return out
    conf = tomllib.loads(LABEL_CONFIRMATION.read_text(encoding="utf-8"))
    out["window"] = {k: conf.get(k) for k in ("since", "until", "ref", "confirmed_through")}
    charter = _charter_purposes(repo / ".bth" / "charter.toml")
    full = work / "full"
    res = build_packet(
        repo,
        out=full,
        project=PROJECT,
        since=conf["since"],
        until=conf["until"],
        ref=conf["ref"],
        catalog_dir=catalog_dir,
    )
    overlay = ac9.load_label_overlay(LABEL_CONFIRMATION)
    out["n_overlay_labels"] = len(overlay)
    key = ac9.answer_key(repo, full, charter, overlay)
    plans, unmet = ac9.plan_planted_packets(_run_labels(full, key), charter)
    out |= {"full_packet_sha256": res["packet_sha256"], "plans": plans, "unmet": unmet}
    out["start_gate_ok"] = not unmet
    return out


def sub_packet(full: Path, out: Path, run_ids: set[str]) -> str:
    """A packet of ``full`` holding only ``run_ids`` and the sidecars they name: no docs, no commits (as AC-7e).

    The source manifest's owner-side fields are copied, so evidence the full packet withheld stays visible to the
    scorer (conservative: a withheld sidecar of a planted-silent purpose makes it indeterminate, never present).
    Returns the new packet's sha256.
    """
    from bathos.bearings.packet import _json_bytes, _packet_hash

    src = full / "reader"
    runs = [
        json.loads(x) for x in (src / "runs.jsonl").read_text(encoding="utf-8").split("\n") if x
    ]
    keep = [r for r in runs if str(r["id"]) in run_ids]
    paths = {r["sidecar_path"] for r in keep if r.get("sidecar_path")}
    index = json.loads((src / "index.json").read_text(encoding="utf-8"))
    side = [s for s in index["sidecars"] if s["path"] in paths]
    o = out / "reader"
    (o / "docs").mkdir(parents=True)
    for s in side:
        dst = o / "sidecars" / s["blob_sha"] / s["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes((src / "sidecars" / s["blob_sha"] / s["path"]).read_bytes())
    (o / "runs.jsonl").write_bytes(b"".join(_json_bytes(r) for r in keep))
    (o / "git_log.jsonl").write_bytes(b"")
    (o / "index.json").write_bytes(_json_bytes({**index, "docs": [], "sidecars": side}))
    sha = _packet_hash(o)
    manifest = json.loads((full / "manifest.json").read_text(encoding="utf-8"))
    (out / "manifest.json").write_bytes(
        _json_bytes({**manifest, "packet_sha256": sha, "ac9_plan_runs": sorted(run_ids)})
    )
    return sha


def controls_ok() -> bool:
    """Synthetic controls; each pairs a positive with a negative that must fail."""
    live = [f"l{c}" for c in "abcdef"]
    charter = {p: {"state": "live"} for p in live} | {
        "ox": {"state": "retired"},
        "oy": {"state": "retired"},
    }
    runs = {f"{p}-{i}": [p] for p in live for i in range(3)} | {f"ox-{i}": ["ox"] for i in range(2)}
    runs |= {f"off-{i}": [ac9.OFF_CHARTER] for i in range(3)}
    plans, unmet = ac9.plan_planted_packets(runs, charter)
    checks = {
        "plant_ok": not unmet and len({tuple(p["silent"]) for p in plans}) == 3,
        "plant_refuses_thin_record": bool(ac9.plan_planted_packets({"la-0": ["la"]}, charter)[1]),
    }
    key = {"present": ["a"], "silent": ["s"], "revived": [], "budget_limited": False}

    def cmp(silent):
        return {"items": [{"kind": "silent", "charter_purpose_id": p} for p in silent]}

    agree = ac9.pooled_metrics(
        [{"key": key, "readings": [{"gold": {}, "comparison": cmp(["s"])}] * 3}]
    )
    one_maps = ac9.pooled_metrics(
        [
            {
                "key": key,
                "readings": [{"gold": {}, "comparison": cmp(["s"])}] * 2
                + [{"gold": {}, "comparison": cmp([])}],
            }
        ]
    )
    checks["pool_silent_ok"] = agree["silent_recall"] == 1.0
    checks["pool_intersection_fires"] = one_maps["silent_recall"] == 0.0
    checks["degenerate_fail"] = not ac9.meets_pass(
        {
            "silent_recall": 0.0,
            "false_silent": 1.0,
            "off_charter_unmapped": 1.0,
            "revived_flagged": 0.0,
            "gold_agreement": None,
        }
    )
    logger.info("controls: %s", checks)
    return all(checks.values())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--work-dir", type=Path, required=True)
    p.add_argument("--repo", type=Path, default=Path.home() / "projects" / PROJECT)
    p.add_argument("--catalog-dir", type=Path, default=None)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--gate", action="store_true")
    mode.add_argument("--controls-only", action="store_true")
    mode.add_argument("--build", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    work = a.work_dir
    work.mkdir(parents=True, exist_ok=True)

    if a.controls_only:
        ok = controls_ok()
        print(json.dumps({"controls_ok": ok}))
        sys.exit(0 if ok else 1)

    if a.gate:
        g = gate(a.repo, work, a.catalog_dir)
        (work / "gate.json").write_text(json.dumps(g, indent=2) + "\n")
        print(
            json.dumps({"start_gate_ok": g["start_gate_ok"], "unmet": g.get("unmet", [])}, indent=2)
        )
        return

    g = json.loads((work / "gate.json").read_text()) if (work / "gate.json").is_file() else {}
    if not g.get("start_gate_ok"):
        # Refuse: with the gate unmet nothing is built or scored, so no outcome can be recorded early.
        print(json.dumps({"start_gate_ok": False, "unmet": g.get("unmet", ["gate not run"])}))
        sys.exit(2)

    if a.build:
        charter = _charter_purposes(a.repo / ".bth" / "charter.toml")
        overlay = ac9.load_label_overlay(LABEL_CONFIRMATION)
        built = {}
        for plan in g["plans"]:
            q = work / plan["packet"]
            sha = sub_packet(work / "full", q / "packet", set(plan["runs"]))
            verify_packet(q / "packet")
            key = ac9.answer_key(a.repo, q / "packet", charter, overlay)
            reading = ac9.oracle_reading(q / "packet", key["labels"], sha)
            (q / "oracle_reading.json").write_text(json.dumps(reading, indent=2) + "\n")
            built[plan["packet"]] = {
                "packet_sha256": sha,
                "missing_cases": ac9.missing_cases(key),
                "bound_violations": ac9.bound_violations(key, charter),
            }
        conf = tomllib.loads(LABEL_CONFIRMATION.read_text(encoding="utf-8"))
        bres = build_packet_within_budget(
            a.repo,
            out=work / "qb" / "packet",
            project=PROJECT,
            since=conf["since"],
            until=conf["until"],
            ref=conf["ref"],
            catalog_dir=a.catalog_dir,
            max_bytes=int(conf.get("budget_max_bytes", 60000)),
        )
        built["qb"] = {"packet_sha256": bres.get("packet_sha256")}
        (work / "built.json").write_text(json.dumps(built, indent=2) + "\n")
        print(json.dumps(built, indent=2))
        return

    score(work, a.repo, a.catalog_dir)


def score(work: Path, repo: Path, catalog_dir: Path | None) -> None:
    """Score a work directory (the tracked run). Expected layout, written by the run protocol before this:

    ``work/q{1,2,3}/packet``; ``work/q{k}/{reader}.json`` (recorded reading id + comparison JSON per reader, the
    output of ``bth bearings record`` / ``compare``); ``work/q{k}/oracle.json`` (the mapper-only comparison);
    ``work/q{k}/decoy_{reader}.json`` (comparison against the decoy charter); ``work/qb/comparison.json``.
    """
    from bathos.bearings.reading import _existing_reading

    charter = _charter_purposes(repo / ".bth" / "charter.toml")
    overlay = ac9.load_label_overlay(LABEL_CONFIRMATION)
    built = json.loads((work / "built.json").read_text())
    packets, oracle_packets = [], []
    decoy_mapped = decoy_total = 0
    invalid: list[str] = []
    leak_or_hash = 0
    for q in ("q1", "q2", "q3"):
        pdir = work / q / "packet"
        try:
            if verify_packet(pdir) != built[q]["packet_sha256"]:
                leak_or_hash += 1
        except Exception:  # noqa: BLE001
            leak_or_hash += 1
        key = ac9.answer_key(repo, pdir, charter, overlay)
        invalid += [f"{q}: missing {c}" for c in ac9.missing_cases(key)]
        invalid += [f"{q}: {v}" for v in ac9.bound_violations(key, charter)]
        readings = []
        for r in READERS:
            rec = json.loads((work / q / f"{r}.json").read_text())
            reading = _existing_reading(
                catalog_dir
                or Path(os.environ.get("BTH_CATALOG_DIR", Path.home() / ".bth" / "catalog")),
                PROJECT,
                rec["reading_id"],
            )
            readings.append(
                {
                    "gold": ac9.reading_gold(pdir, reading, key["labels"]),
                    "maybe": ac9.reading_gold(pdir, reading, key["ambiguous_labels"]),
                    "comparison": rec["comparison"],
                }
            )
            decoy = json.loads((work / q / f"decoy_{r}.json").read_text())
            n_rps = len(
                {it["reading_purpose_id"] for it in decoy["items"] if it.get("reading_purpose_id")}
            )
            decoy_mapped += round(ac9.decoy_mapped_share(decoy) * n_rps)
            decoy_total += n_rps
        packets.append({"key": key, "readings": readings})
        orc = json.loads((work / q / "oracle.json").read_text())
        oreading = json.loads((work / q / "oracle_reading.json").read_text())
        oracle_packets.append(
            {
                "key": key,
                "readings": [
                    {"gold": ac9.reading_gold(pdir, oreading, key["labels"]), "comparison": orc}
                ],
            }
        )
    pooled = ac9.pooled_metrics(packets)
    oracle = ac9.pooled_metrics(oracle_packets)
    deg_failing = sum(
        not ac9.meets_pass(
            ac9.pooled_metrics(
                [{"key": pk["key"], "readings": [{"gold": rd["gold"], "comparison": c}]}]
            )
        )
        for pk in packets
        for rd in pk["readings"][:1]
        for c in _degenerate_comparisons(pk, charter)
    )
    n_deg = 4 * len(packets)
    qb = json.loads((work / "qb" / "comparison.json").read_text())
    qb_ok = all(it.get("budget_limited") for it in qb["items"] if it["kind"] == "silent")
    result = {
        "controls_ok": controls_ok(),
        "start_gate_ok": True,
        "leak_or_hash_failures": leak_or_hash,
        "invalid_reasons_n": len(invalid),
        # T7: one mapped share over every decoy reading, pooled like the other metrics.
        "decoy_mapped_share": decoy_mapped / decoy_total if decoy_total else None,
        "decoy_n_reading_purposes": decoy_total,
        "degenerate_failing": deg_failing,
        "degenerate_total": n_deg,
        "budget_control_ok": qb_ok,
        **{f"pooled_{k}": v for k, v in pooled.items()},
        **{
            f"oracle_{k}": v
            for k, v in oracle.items()
            if not k.startswith(("silent_recall_majority", "false_silent_majority"))
        },
        "meets_pass": ac9.meets_pass(pooled),
        "meets_marginal": ac9.meets_marginal(pooled),
    }
    (work / "result.json").write_text(
        json.dumps({**result, "invalid_detail": invalid}, indent=2) + "\n"
    )
    out = os.environ.get("BTH_RESULTS_PATH")
    if out:
        Path(out).write_text(json.dumps(result))
    print(json.dumps(result, indent=2))


def _degenerate_comparisons(pk: dict, charter: dict) -> list[dict]:
    """The four degenerate mappers applied to a packet's first reading, as comparison item lists (no catalog write)."""
    gold = pk["readings"][0]["gold"]
    rps = sorted(gold)
    out = []
    for mapping in ac9.degenerate_mappings(rps, charter).values():
        items = []
        mapped_live = set()
        for rp, t in mapping.items():
            targets = t if isinstance(t, list) else ([t] if t else [])
            for tg in targets:
                if charter[tg]["state"] == "retired":
                    items.append(
                        {"kind": "unmapped", "reading_purpose_id": rp, "matched_retired": tg}
                    )
                else:
                    mapped_live.add(tg)
                    items.append(
                        {"kind": "mapped", "reading_purpose_id": rp, "charter_purpose_id": tg}
                    )
        items += [
            {"kind": "silent", "charter_purpose_id": p}
            for p, v in charter.items()
            if v["state"] == "live" and p not in mapped_live
        ]
        out.append({"items": items})
    return out


if __name__ == "__main__":
    main()
