#!/usr/bin/env python3
"""Census: where can a goal link attach, and does step-back currently see the work?

Task 261104_multiscale-goal-awareness. Read-only over the catalog and sidecar files.
Reads through `bth sql` so it sees what any agent sees. The attachment SQL is run first
on synthetic rows with known answers (one positive and one negative control), so a broken
blank-handling predicate reports invalid instead of a number.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import tomllib
from pathlib import Path

log = logging.getLogger("census_goal_attachment")

ATTACH_SQL = """
SELECT count(*) AS total,
       sum(CASE WHEN campaign_id IS NULL OR campaign_id = '' THEN 1 ELSE 0 END) AS no_campaign
FROM {source}
"""
# NULL and '' both mean "no campaign": the raw count(campaign_id) reads 100% because of ''.
SYNTHETIC_MIXED = "(VALUES ('r1','c1'),('r2',''),('r3',NULL),('r4','c2')) t(id, campaign_id)"
SYNTHETIC_ALL_ATTACHED = "(VALUES ('r1','c1'),('r2','c1')) t(id, campaign_id)"


def sql(statement: str) -> list[list]:
    """Run one read-only statement through the installed `bth sql`."""
    proc = subprocess.run(["bth", "sql", statement], capture_output=True, text=True, check=True)
    payload = json.loads(proc.stdout)
    if "rows" not in payload:
        raise RuntimeError(f"bth sql returned no rows key: {proc.stdout[:300]}")
    return payload["rows"]


def controls_ok() -> bool:
    """Positive: 2 of 4 synthetic rows are unattached. Negative: 0 of 2 are."""
    mixed = sql(ATTACH_SQL.format(source=SYNTHETIC_MIXED))[0]
    attached = sql(ATTACH_SQL.format(source=SYNTHETIC_ALL_ATTACHED))[0]
    log.info("control mixed=%s attached=%s", mixed, attached)
    return mixed == [4, 2] and attached == [2, 0]


def count_stepback_artifacts(roots: list[Path]) -> int:
    """Files under any stepback/ directory in the given roots. Depth is bounded by the layout."""
    n = 0
    for root in roots:
        for sb in root.glob("stepback") if root.is_dir() else []:
            n += sum(1 for p in sb.rglob("*") if p.is_file())
    return n


def decision_coverage(paths: list[str]) -> tuple[int, int, int]:
    """Of the sidecars that still exist: (readable, with any outcome, with a decision on one)."""
    readable = with_outcomes = with_decision = 0
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue
        readable += 1
        outcomes = data.get("outcomes")
        if not isinstance(outcomes, dict) or not outcomes:
            continue
        with_outcomes += 1
        if any(isinstance(v, dict) and str(v.get("decision", "")).strip() for v in outcomes.values()):
            with_decision += 1
    return readable, with_outcomes, with_decision


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--since", default="2026-10-01", help="start of the recent window")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    ok = controls_ok()
    total, no_campaign = sql(ATTACH_SQL.format(source="runs"))[0]
    recent_total, recent_no_campaign = sql(
        ATTACH_SQL.format(source=f"(SELECT * FROM runs WHERE timestamp >= '{args.since}')")
    )[0]
    campaigns_total = sql("SELECT count(*) FROM campaigns")[0][0]
    campaigns_stub = sql("SELECT count(*) FROM campaigns WHERE mode IS NULL AND name IS NULL")[0][0]
    campaigns_null_mode = sql("SELECT count(*) FROM campaigns WHERE mode IS NULL")[0][0]
    campaigns_no_question = sql(
        "SELECT count(*) FROM campaigns WHERE question IS NULL OR question = ''"
    )[0][0]
    edges = sql("SELECT count(*) FROM campaign_edges")[0][0]
    recent_sidecar, recent_session = sql(
        "SELECT sum(CASE WHEN sidecar_path IS NOT NULL AND sidecar_path <> '' THEN 1 ELSE 0 END),"
        " sum(CASE WHEN agent_session IS NOT NULL AND agent_session <> '' THEN 1 ELSE 0 END)"
        f" FROM runs WHERE timestamp >= '{args.since}'"
    )[0]
    recent_paths = [
        r[0]
        for r in sql(
            "SELECT DISTINCT sidecar_path FROM runs WHERE sidecar_path IS NOT NULL"
            f" AND sidecar_path <> '' AND timestamp >= '{args.since}'"
        )
    ]
    readable, with_outcomes, with_decision = decision_coverage(recent_paths)

    catalog = Path(os.environ.get("BTH_CATALOG_DIR", Path.home() / ".bth" / "catalog"))
    project_bth = list((Path.home() / "projects").glob("*/.bth"))
    stepback_files = count_stepback_artifacts([catalog, *project_bth])

    result = {
        "controls_ok": ok,
        "runs_total": total,
        "runs_no_campaign": no_campaign,
        "frac_no_campaign": no_campaign / total,
        "recent_runs_total": recent_total,
        "recent_runs_no_campaign": recent_no_campaign,
        "frac_recent_no_campaign": recent_no_campaign / recent_total,
        "frac_recent_with_sidecar": recent_sidecar / recent_total,
        "frac_recent_with_session": recent_session / recent_total,
        "campaigns_total": campaigns_total,
        "campaigns_null_mode": campaigns_null_mode,
        "campaigns_stub": campaigns_stub,
        "campaigns_no_question": campaigns_no_question,
        "campaign_edges": edges,
        "stepback_artifact_files": stepback_files,
        "recent_sidecars_distinct": len(recent_paths),
        "recent_sidecars_readable": readable,
        "recent_sidecars_with_outcomes": with_outcomes,
        "recent_sidecars_with_decision": with_decision,
    }
    log.info("%s", json.dumps(result, indent=1))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1))
    results_path = os.environ.get("BTH_RESULTS_PATH")
    if results_path:
        Path(results_path).write_text(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
