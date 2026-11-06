"""AC-7c per-reading table for the review page (spec 261104, run 2032d774).

Recomputes, with the scorer's own functions, each reading's drift groups (strict: unsure and unsupported
bridges removed; lenient: only unsupported removed; no_labels: no bridge removed, the reader's own graph),
its bridge count and the share of its bridges labelled supported. For the planted arm it also counts the
bridges that cross the seam (endpoints whose run: and git: citations come mostly from different source
projects) and how many of those were labelled supported. The sidecar requires the strict and lenient
medians to equal what run 2032d774 recorded; the other columns are descriptive.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_bearings_ac7 import READERS  # noqa: E402
from replay_bearings_ac7b import (  # noqa: E402
    LABELS,
    _PacketView,
    bridges,
    drift_groups,
    load_valid,
    reading_graph,
)
from replay_bearings_ac7c import ARMS, PLANTED, node_keys  # noqa: E402

logger = logging.getLogger("table_bearings_ac7c")
ROLE = {"k1": "control, project A alone", "k2": "planted splice", "k3": "control, project B alone"}


def dominant(keys: set[str], members: dict[str, int]) -> int | None:
    counts: dict[int, int] = {}
    for k in keys:
        if k in members:
            counts[members[k]] = counts.get(members[k], 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]


def rows(work: Path) -> list[dict]:
    labels = json.loads((work / "bridge_labels.json").read_text(encoding="utf-8"))
    out = []
    for arm in ARMS:
        packet = work / "packets" / arm
        view = _PacketView(packet)
        members = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))["members"]
        for reader in READERS:
            reading = load_valid(work / "readings" / arm / f"{reader}.json")
            if reading is None:
                raise SystemExit(f"{arm}/{reader}: invalid reading")
            effort, edges = reading_graph(reading, view)
            found = bridges(list(effort), edges)
            given = {int(k): v for k, v in labels.get(f"{arm}/{reader}", {}).items() if v in LABELS}
            strict = {i for i in found if given.get(i, "unsure") != "supported"}
            lenient = {i for i in found if given.get(i) == "unsupported"}
            row = {
                "arm": f"{arm} ({ROLE[arm]})",
                "reading": f"{arm}-{reader}",
                "groups_strict": drift_groups(effort, edges, strict),
                "groups_lenient": drift_groups(effort, edges, lenient),
                "groups_no_labels": drift_groups(effort, edges, set()),
                "n_bridges": len(found),
                "supported_frac": round(sum(given.get(i) == "supported" for i in found) / len(found), 4)
                if found else 1.0,
                "n_cross_seam_bridges": 0,
                "cross_seam_supported": 0,
            }  # fmt: skip
            if arm == PLANTED:
                src = {n: dominant(k, members) for n, k in node_keys(reading, view).items()}
                ends = {i: (a, b) for i, a, b in edges}
                cross = [
                    i for i in found
                    if src.get(ends[i][0]) is not None and src.get(ends[i][1]) is not None
                    and src[ends[i][0]] != src[ends[i][1]]
                ]  # fmt: skip
                row["n_cross_seam_bridges"] = len(cross)
                row["cross_seam_supported"] = sum(given.get(i) == "supported" for i in cross)
            out.append(row)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    table = rows(args.work_dir.resolve())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(table[0]))
        w.writeheader()
        w.writerows(table)
    result: dict[str, float | int] = {"n_rows": len(table)}
    for arm in ARMS:
        mine = [r for r in table if r["reading"].startswith(arm + "-")]
        result[f"{arm}_strict_median"] = statistics.median(r["groups_strict"] for r in mine)
        result[f"{arm}_lenient_median"] = statistics.median(r["groups_lenient"] for r in mine)
        result[f"{arm}_no_labels_median"] = statistics.median(r["groups_no_labels"] for r in mine)
    planted = [r for r in table if r["reading"].startswith(PLANTED + "-")]
    result[f"{PLANTED}_cross_seam_bridges"] = sum(r["n_cross_seam_bridges"] for r in planted)
    result[f"{PLANTED}_cross_seam_supported"] = sum(r["cross_seam_supported"] for r in planted)
    text = json.dumps(result, indent=2, sort_keys=True)
    logger.info(text)
    if path := os.environ.get("BTH_RESULTS_PATH"):
        Path(path).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
