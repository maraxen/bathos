"""Prepare the AC-7 labelling jury and merge its verdicts into labels plus a list for the owner.

build: writes prompts, schemas and one empty git folder per dispatch, so a juror's folder holds nothing to read.
merge: compares verdicts, applies the sidecar's flag rules, and writes merged.json and for_owner.json.
finalize: combines merged.json with the owner's label documents into labels.json, or stops if the audit fails.

Prints only counts of flags, never per-reader purpose counts, so the score stays hidden until the tracked run.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import subprocess
from itertools import combinations
from pathlib import Path

from replay_bearings_ac7_label_page import PURPOSES, load_entries

logger = logging.getLogger("replay_bearings_ac7_jury")

JURORS = ("cursor1", "cursor2", "agy1", "agy2")
ARM_ORDERS = {
    "cursor1": ("colliculix", "tev_design", "plegadx"),
    "cursor2": ("plegadx", "colliculix", "tev_design"),
    "agy1": ("tev_design", "plegadx", "colliculix"),
    "agy2": ("plegadx", "tev_design", "colliculix"),
}
AUDIT_SEED = 261105
AUDIT_SIZE = 10
AUDIT_MAX_OVERTURNED = 2

LABEL_SYSTEM = """You are classifying statements about research projects. Each statement is one entry written by an \
independent reader describing what a project's record shows. Work only from the text in this prompt. Do not open files, \
run commands or use any tools.

Give every entry exactly one choice:
- "project": the entry shows a purpose of the whole project. Give it a short name in "name". Within one project, use \
exactly the same name whenever two entries show the same purpose, across all readers.
- "sub": the entry is part of something larger, or describes work that serves another purpose, or is not a purpose at all.
- "generic": the entry is too vague for anyone to disagree with.

Apply the same standard to every project. A project usually has few purposes, but do not aim for any particular number.

Ruling from the project owner for tev_design: the k=4 vs k=9 episode and the 260714 target swap are claim-scale slips \
inside one aim, so an entry that reports either as a separate purpose is "sub".

Set "uncertain" to true for any entry where a careful person could reasonably choose differently. Use it honestly: an \
uncertain entry goes to the owner, and this has no penalty.

Return one JSON object of the form {"rows": [...]}, with one row for every entry: {"project", "reader", "entry", \
"choice", "name", "uncertain"}. Copy project, reader and entry id exactly. "name" is null unless the choice is "project"."""

MAP_SYSTEM = """You are comparing statements about a research project with a list of purposes. Each statement is one \
entry written by an independent reader describing what the project's record shows. Work only from the text in this \
prompt. Do not open files, run commands or use any tools.

For every entry, list the letters of the purposes it corresponds to. An entry corresponds to a purpose when it describes \
that work, even as a part of something larger. A passing mention does not count. Use an empty list when none apply.

Set "uncertain" to true for any entry where a careful person could reasonably tick differently. Use it honestly: an \
uncertain entry goes to the owner, and this has no penalty.

Return one JSON object of the form {"rows": [...]}, with one row for every entry: {"reader", "entry", "maps_to", \
"uncertain"}. Copy reader and entry id exactly."""


def label_schema() -> dict:
    row = {
        "type": "object",
        "required": ["project", "reader", "entry", "choice", "name", "uncertain"],
        "additionalProperties": False,
        "properties": {
            "project": {"type": "string"},
            "reader": {"type": "string"},
            "entry": {"type": "string"},
            "choice": {"enum": ["project", "sub", "generic"]},
            "name": {"type": ["string", "null"]},
            "uncertain": {"type": "boolean"},
        },
    }
    return {"type": "object", "required": ["rows"], "properties": {"rows": {"type": "array", "items": row}}}


def map_schema() -> dict:
    row = {
        "type": "object",
        "required": ["reader", "entry", "maps_to", "uncertain"],
        "additionalProperties": False,
        "properties": {
            "reader": {"type": "string"},
            "entry": {"type": "string"},
            "maps_to": {"type": "array", "items": {"enum": list(PURPOSES)}},
            "uncertain": {"type": "boolean"},
        },
    }
    return {"type": "object", "required": ["rows"], "properties": {"rows": {"type": "array", "items": row}}}


def render_entries(readings: list[dict], with_project: bool) -> str:
    lines = []
    for r in readings:
        head = f"## project={r['arm']} reader={r['reader']}" if with_project else f"## reader={r['reader']}"
        lines.append(head)
        lines += [f"- entry={e['id']} ({e['kind']}): {e['text']}" for e in r["entries"]]
        lines.append("")
    return "\n".join(lines)


def init_base(path: Path) -> None:
    """Create an empty repository with one commit, because a dispatch needs a base sha to admit against."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=jury", "-c", "user.email=jury@localhost",
         "commit", "-q", "--allow-empty", "-m", "empty"],
        check=True,
    )  # fmt: skip


def init_folder(base: Path, path: Path) -> None:
    """Add an empty linked worktree, because praxia refuses a primary checkout as a dispatch folder."""
    if path.exists():
        return
    subprocess.run(["git", "-C", str(base), "worktree", "add", "-q", "-b", path.name, str(path)], check=True)


def build(work: Path) -> None:
    jury = work / "jury"
    prompts = jury / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    readings = load_entries(work)
    (prompts / "label_system.md").write_text(LABEL_SYSTEM + "\n", encoding="utf-8")
    (prompts / "map_system.md").write_text(MAP_SYSTEM + "\n", encoding="utf-8")
    (jury / "label_schema.json").write_text(json.dumps(label_schema()), encoding="utf-8")
    (jury / "map_schema.json").write_text(json.dumps(map_schema()), encoding="utf-8")
    if not (jury / "base").exists():
        init_base(jury / "base")
    positive = [r for r in readings if r["arm"] == "colliculix"]
    purposes = "\n".join(f"- {k}: {v}" for k, v in PURPOSES.items())
    for juror in JURORS:
        ordered = [r for arm in ARM_ORDERS[juror] for r in readings if r["arm"] == arm]
        (prompts / f"label_{juror}.md").write_text(render_entries(ordered, True), encoding="utf-8")
        (prompts / f"map_{juror}.md").write_text(
            f"Purposes:\n{purposes}\n\n{render_entries(positive, False)}", encoding="utf-8"
        )
        init_folder(jury / "base", jury / "folders" / f"label_{juror}")
        init_folder(jury / "base", jury / "folders" / f"map_{juror}")
    logger.info("built %d folders", 2 * len(JURORS))


def collect(work: Path) -> None:
    """Save admitted verdicts from praxia's records to disk, and print only each dispatch's status."""
    jury = work / "jury"
    dispatches = json.loads((jury / "dispatches.json").read_text())
    records: dict[str, dict] = {}
    for line in (jury / "base" / ".praxia" / "dispatch_runs.jsonl").read_text().splitlines():
        obj = json.loads(line)
        for rec in (obj, *(v for v in obj.values() if isinstance(v, dict))):
            if "dispatch_id" in rec and "terminal_state" in rec:
                records[rec["dispatch_id"]] = rec
    (jury / "verdicts").mkdir(exist_ok=True)
    for name, dispatch_id in sorted(dispatches.items()):
        rec = records.get(dispatch_id)
        if rec is None:
            logger.info("%s: running", name)
            continue
        admitted = rec.get("admission") == "admitted"
        if admitted:
            (jury / "verdicts" / f"{name}.json").write_text(json.dumps(rec["structured_output"]), encoding="utf-8")
        reasons = len((rec.get("admission") or {}).get("rejected", {}).get("reasons", [])) if not admitted else 0
        logger.info("%s: %s, admitted=%s, rejection reasons=%d", name, rec["terminal_state"], admitted, reasons)


def key(arm: str, reader: str, entry: str) -> str:
    return f"{arm}.{reader}.{entry}"


def load_verdicts(work: Path) -> tuple[dict[str, dict], dict[str, dict], list[str]]:
    """Read saved verdicts. A row for an unknown entry, or a missing row, invalidates that juror's round."""
    jury = work / "jury"
    readings = load_entries(work)
    expected = {key(r["arm"], r["reader"], e["id"]) for r in readings for e in r["entries"]}
    expected_map = {k for k in expected if k.startswith("colliculix.")}
    labels: dict[str, dict] = {}
    maps: dict[str, dict] = {}
    problems: list[str] = []
    for juror in JURORS:
        path = jury / "verdicts" / f"label_{juror}.json"
        if path.exists():
            rows = {key(r["project"], r["reader"], r["entry"]): r for r in json.loads(path.read_text())["rows"]}
            if set(rows) == expected:
                labels[juror] = rows
            else:
                problems.append(f"label_{juror}: {len(expected - set(rows))} missing, {len(set(rows) - expected)} extra")
        path = jury / "verdicts" / f"map_{juror}.json"
        if path.exists():
            rows = {key("colliculix", r["reader"], r["entry"]): r for r in json.loads(path.read_text())["rows"]}
            if set(rows) == expected_map:
                maps[juror] = rows
            else:
                problems.append(f"map_{juror}: {len(expected_map - set(rows))} missing, {len(set(rows) - expected_map)} extra")
    return labels, maps, problems


def merge(work: Path) -> None:
    labels, maps, problems = load_verdicts(work)
    for p in problems:
        logger.info("invalid verdict: %s", p)
    for name, got in (("label", labels), ("map", maps)):
        if not (any(j.startswith("cursor") for j in got) and any(j.startswith("agy") for j in got)):
            raise SystemExit(f"{name} round lacks a valid juror from each surface")

    readings = load_entries(work)
    flags: dict[str, list[str]] = {}
    merged: dict[str, dict] = {}
    first = next(j for j in JURORS if j in labels)
    for r in readings:
        project_keys = []
        for e in r["entries"]:
            k = key(r["arm"], r["reader"], e["id"])
            votes = [labels[j][k] for j in labels]
            reasons = flags.setdefault(k, [])
            if len({v["choice"] for v in votes}) > 1:
                reasons.append("choice")
            elif votes[0]["choice"] == "project":
                project_keys.append(k)
            if any(v["uncertain"] for v in votes):
                reasons.append("uncertain")
            ticks: list[str] = []
            if r["arm"] == "colliculix":
                map_votes = [maps[j][k] for j in maps]
                if len({tuple(sorted(v["maps_to"])) for v in map_votes}) > 1:
                    reasons.append("ticks")
                if any(v["uncertain"] for v in map_votes):
                    reasons.append("ticks_uncertain")
                ticks = sorted(map_votes[0]["maps_to"])
            choice = labels[first][k]["choice"]
            merged[k] = {
                "choice": choice,
                "identity": (labels[first][k]["name"] or "").strip() if choice == "project" else None,
                "maps": ticks,
            }
        # The scorer counts distinct names per reader, so only grouping inside one reader can change a score.
        for a, b in combinations(project_keys, 2):
            together = {
                (labels[j][a]["name"] or "").strip().lower() == (labels[j][b]["name"] or "").strip().lower()
                for j in labels
            }
            if len(together) > 1:
                flags[a].append("grouping")
                flags[b].append("grouping")

    flagged = sorted(k for k, v in flags.items() if v)
    clean = sorted(k for k, v in flags.items() if not v)
    audit = sorted(random.Random(AUDIT_SEED).sample(clean, min(AUDIT_SIZE, len(clean))))
    jury = work / "jury"
    (jury / "merged.json").write_text(json.dumps({"merged": merged, "flags": flags, "audit": audit}, indent=2) + "\n")
    votes = {
        k: {
            j: {"choice": labels[j][k]["choice"], "name": labels[j][k]["name"], "uncertain": labels[j][k]["uncertain"]}
            for j in labels
        }
        for k in flagged
    }
    (jury / "for_owner.json").write_text(json.dumps({"keys": sorted([*flagged, *audit]), "votes": votes}, indent=2) + "\n")
    counts: dict[str, int] = {}
    for v in flags.values():
        for reason in set(v):
            counts[reason] = counts.get(reason, 0) + 1
    logger.info("jurors: %d label, %d map", len(labels), len(maps))
    logger.info("flagged %d of %d; reasons %s; audit %d", len(flagged), len(flags), sorted(counts.items()), len(audit))


def finalize(work: Path, owner_dir: Path) -> None:
    state = json.loads((work / "jury" / "merged.json").read_text())
    merged, flags, audit = state["merged"], state["flags"], state["audit"]
    owner = {}
    for path in owner_dir.glob("*.json"):
        raw = json.loads(path.read_text())
        owner[path.stem] = raw["data"] if isinstance(raw.get("data"), dict) else raw

    def same(k: str) -> bool:
        a, b = owner[k], merged[k]
        return a.get("choice") == b["choice"] and sorted(a.get("maps") or []) == b["maps"]

    missing_audit = [k for k in audit if not owner.get(k, {}).get("choice")]
    if missing_audit:
        raise SystemExit(f"{len(missing_audit)} audit entries lack an owner label")
    overturned = sum(not same(k) for k in audit)
    logger.info("audit: owner overturned %d of %d", overturned, len(audit))
    if overturned > AUDIT_MAX_OVERTURNED:
        raise SystemExit("audit failed: the sidecar sends labelling back to the owner, and no labels.json was written")

    labels: dict[str, dict] = {}
    n_from_owner = n_from_jury = 0
    for r in load_entries(work):
        out = labels[f"{r['arm']}/{r['reader']}"] = {}
        for e in r["entries"]:
            k = key(r["arm"], r["reader"], e["id"])
            o = owner.get(k)
            if o and o.get("choice"):
                source, n_from_owner = {**o, "maps": o.get("maps") or []}, n_from_owner + 1
            elif not flags[k]:
                source, n_from_jury = merged[k], n_from_jury + 1
            else:
                continue
            identity = (source.get("identity") or "").strip()
            if source["choice"] == "project" and not identity:
                continue
            out[e["id"]] = {
                "identity": identity if source["choice"] == "project" else None,
                "generic": source["choice"] == "generic",
                "maps_to": sorted(source["maps"]),
            }
    (work / "labels.json").write_text(json.dumps(labels, indent=2) + "\n", encoding="utf-8")
    logger.info("wrote labels.json: %d from the owner, %d from the jury", n_from_owner, n_from_jury)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "collect", "merge", "finalize"))
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--owner-dir", type=Path, help="finalize: directory of the owner's label documents")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.command == "build":
        build(args.work_dir)
    elif args.command == "collect":
        collect(args.work_dir)
    elif args.command == "merge":
        merge(args.work_dir)
    else:
        finalize(args.work_dir, args.owner_dir)


if __name__ == "__main__":
    main()
