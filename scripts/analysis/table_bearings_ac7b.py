"""AC-7b per-reading table for the review page (spec 261104, run b85be024).

Recomputes, with the scorer's own functions, each reading's drift groups (strict: unsure and
unsupported bridges removed; lenient: only unsupported removed), its bridge count and the share of
its bridges labelled supported, and writes one row per reading. It also reports each arm's medians,
which the sidecar requires to equal what the scored run b85be024 recorded.
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
    ARMS,
    LABELS,
    _PacketView,
    bridges,
    drift_groups,
    load_valid,
    reading_graph,
)

logger = logging.getLogger("table_bearings_ac7b")
ROLE = {"colliculix": "positive", "demistify": "hard negative", "plegadx": "negative"}


def rows(work: Path) -> list[dict]:
    labels = json.loads((work / "bridge_labels.json").read_text(encoding="utf-8"))
    out = []
    for arm in ARMS:
        view = _PacketView(work / "packets" / arm)
        for reader in READERS:
            reading = load_valid(work / "readings" / arm / f"{reader}.json")
            if reading is None:
                raise SystemExit(f"{arm}/{reader}: invalid reading")
            effort, edges = reading_graph(reading, view)
            found = bridges(list(effort), edges)
            given = {int(k): v for k, v in labels.get(f"{arm}/{reader}", {}).items() if v in LABELS}
            strict = {i for i in found if given.get(i, "unsure") != "supported"}
            lenient = {i for i in found if given.get(i) == "unsupported"}
            out.append({
                "arm": f"{arm} ({ROLE[arm]})",
                "reading": f"{arm}-{reader}",
                "groups_strict": drift_groups(effort, edges, strict),
                "groups_lenient": drift_groups(effort, edges, lenient),
                "n_bridges": len(found),
                "supported_frac": round(sum(given.get(i) == "supported" for i in found) / len(found), 4)
                if found else 1.0,
            })  # fmt: skip
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
    text = json.dumps(result, indent=2, sort_keys=True)
    logger.info(text)
    if path := os.environ.get("BTH_RESULTS_PATH"):
        Path(path).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
