"""Prepare the AC-7 labelling jury and merge its verdicts into labels plus a list for the owner.

build: writes prompts, schemas and one empty git folder per dispatch, so a juror's folder holds nothing to read.
merge: compares verdicts, applies the sidecar's flag rules, and writes merged.json and for_owner.json.
adjudicate: writes the prompt for one Opus adjudicator that decides the flagged entries (seventh sidecar amendment).
finalize: combines the owner's labels, then the adjudicator's, then the jury's into labels.json and labels_majority.json.
sensitivity: after the tracked run, prints recovered under union ticks and under ticks from at least 3 of 4 jurors.

Build, collect, merge and finalize print only counts of flags, so the score stays hidden until the tracked run.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import subprocess
from collections import Counter
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
MIN_UNCERTAIN = 3  # Owner's choice, fifth sidecar amendment.

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


ADJ_SYSTEM = """You are the adjudicator for a panel that classified statements about research projects. Each statement \
is one entry written by an independent reader describing what a project's record shows. Four jurors labelled every \
entry. The entries below are the ones where their choices differed or at least three of them were unsure. You decide \
each of these. Work only from the text in this prompt.

Give every disputed entry exactly one choice:
- "project": the entry shows a purpose of the whole project. Give it a short name in "name".
- "sub": the entry is part of something larger, or describes work that serves another purpose, or is not a purpose at all.
- "generic": the entry is too vague for anyone to disagree with.

Names matter only inside one reader: two entries of the same reader with the same name count as one purpose. If a \
disputed entry shows the same purpose as another entry of the same reader that is already labelled "project", reuse \
that name exactly, character for character. If it shows a purpose no other entry of that reader names, give a new name. \
Apply the same reuse rule between disputed entries of the same reader.

Apply the same standard to every project. A project usually has few purposes, but do not aim for any particular number.

Ruling from the project owner for tev_design: the k=4 vs k=9 episode and the 260714 target swap are claim-scale slips \
inside one aim, so an entry that reports either as a separate purpose is "sub".

The jurors' votes are evidence, not a vote count to follow. Set "uncertain" to true if a careful person could reasonably \
choose differently, and give a one-sentence "reason" for every decision.

Return one JSON object of the form {"rows": [...]}, with one row for every disputed entry: {"project", "reader", \
"entry", "choice", "name", "uncertain", "reason"}. Copy project, reader and entry id exactly. "name" is null unless the \
choice is "project"."""


def adjudicator_schema() -> dict:
    schema = label_schema()
    row = schema["properties"]["rows"]["items"]
    row["required"].append("reason")
    row["properties"]["reason"] = {"type": "string"}
    return schema


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
        reject = (rec.get("admission") or {}).get("rejected", {}).get("reasons", []) if not admitted else []
        # A pilot's rollback patch left in the folder is praxia's own file, not an act of this juror.
        leftover = "out-of-scope change: .praxia/patches/"
        only_leftover = bool(reject) and all(r.startswith(leftover) and dispatch_id not in r for r in reject)
        if only_leftover and rec.get("structured_output") is not None:
            admitted = True
            logger.info("%s: rejected only for a leftover patch from an earlier dispatch; verdict kept", name)
        if admitted:
            (jury / "verdicts" / f"{name}.json").write_text(json.dumps(rec["structured_output"]), encoding="utf-8")
        logger.info("%s: %s, usable=%s, rejection reasons=%d", name, rec["terminal_state"], admitted, len(reject))


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
    tick_notes: dict[str, list[str]] = {}
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
            if sum(v["uncertain"] for v in votes) >= MIN_UNCERTAIN:
                reasons.append("uncertain")
            ticks: list[str] = []
            majority: list[str] = []
            if r["arm"] == "colliculix":
                map_votes = [maps[j][k] for j in maps]
                notes = tick_notes.setdefault(k, [])
                if len({tuple(sorted(v["maps_to"])) for v in map_votes}) > 1:
                    notes.append("ticks_differ")
                if any(v["uncertain"] for v in map_votes):
                    notes.append(f"ticks_uncertain:{sum(v['uncertain'] for v in map_votes)}")
                counts = Counter(c for v in map_votes for c in set(v["maps_to"]))
                ticks = sorted(counts)
                majority = sorted(c for c, n in counts.items() if n >= MIN_UNCERTAIN)
            choice = labels[first][k]["choice"]
            merged[k] = {
                "choice": choice,
                "identity": (labels[first][k]["name"] or "").strip() if choice == "project" else None,
                "maps": ticks,
                "maps_majority": majority,
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
    jury = work / "jury"
    (jury / "merged.json").write_text(
        json.dumps({"merged": merged, "flags": flags, "tick_notes": tick_notes}, indent=2) + "\n"
    )
    votes = {
        k: {
            j: {"choice": labels[j][k]["choice"], "name": labels[j][k]["name"], "uncertain": labels[j][k]["uncertain"]}
            for j in labels
        }
        for k in flagged
    }
    (jury / "for_owner.json").write_text(json.dumps({"keys": flagged, "votes": votes}, indent=2) + "\n")
    counts: dict[str, int] = {}
    for v in flags.values():
        for reason in set(v):
            counts[reason] = counts.get(reason, 0) + 1
    logger.info("jurors: %d label, %d map", len(labels), len(maps))
    logger.info("flagged %d of %d; reasons %s", len(flagged), len(flags), sorted(counts.items()))
    logger.info("entries with a tick note, not sent to the owner: %d", sum(bool(v) for v in tick_notes.values()))


def adjudicate(work: Path) -> None:
    """Write the adjudicator's prompt: each reader holding a flagged entry, with settled labels and the jurors' votes."""
    jury = work / "jury"
    state = json.loads((jury / "merged.json").read_text())
    merged, flags = state["merged"], state["flags"]
    votes = json.loads((jury / "for_owner.json").read_text())["votes"]
    lines = [ADJ_SYSTEM, ""]
    n = 0
    for r in load_entries(work):
        keys = [key(r["arm"], r["reader"], e["id"]) for e in r["entries"]]
        if not any(flags[k] for k in keys):
            continue
        lines.append(f"## project={r['arm']} reader={r['reader']}")
        for e, k in zip(r["entries"], keys):
            if flags[k]:
                n += 1
                lines.append(f"- DISPUTED entry={e['id']} ({e['kind']}): {e['text']}")
                for j, v in votes[k].items():
                    name = f', name "{v["name"]}"' if v["choice"] == "project" else ""
                    lines.append(f"  - juror {j}: {v['choice']}{name}{', unsure' if v['uncertain'] else ''}")
            else:
                m = merged[k]
                label = f'project, name "{m["identity"]}"' if m["choice"] == "project" else m["choice"]
                lines.append(f"- settled entry={e['id']} ({e['kind']}): {e['text']}\n  - label: {label}")
        lines.append("")
    out = jury / "adjudicate"
    out.mkdir(exist_ok=True)
    (out / "prompt.md").write_text("\n".join(lines), encoding="utf-8")
    # The Cursor redo (seventh amendment, second note) takes the same text as a system and a user prompt.
    (out / "system.md").write_text(ADJ_SYSTEM + "\n", encoding="utf-8")
    (out / "user.md").write_text("\n".join(lines[2:]), encoding="utf-8")
    (out / "schema.json").write_text(json.dumps(adjudicator_schema()), encoding="utf-8")
    init_folder(jury / "base", jury / "folders" / "adjudicator")
    logger.info("adjudicator prompt holds %d disputed entries", n)


def join_adjudicator(work: Path) -> None:
    """Join the redo's one-file-per-entry rows into verdicts/adjudicator.json. Prints counts only."""
    jury = work / "jury"
    rows, bad = [], 0
    for path in sorted((jury / "adjudicate" / "rows").glob("*.json")):
        try:
            rows.append(json.loads(path.read_text()))
        except json.JSONDecodeError:
            bad += 1
    (jury / "verdicts" / "adjudicator.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")
    logger.info("joined %d row files, %d unreadable", len(rows), bad)


def load_adjudicator(work: Path, flagged: set[str]) -> dict[str, dict]:
    """Read the adjudicator's verdict. It must cover exactly the flagged entries, or it is not used at all."""
    path = work / "jury" / "verdicts" / "adjudicator.json"
    if not path.exists():
        return {}
    rows = {key(r["project"], r["reader"], r["entry"]): r for r in json.loads(path.read_text())["rows"]}
    bad = [k for k, r in rows.items() if r["choice"] not in ("project", "sub", "generic")]
    if set(rows) != flagged or bad:
        raise SystemExit(f"adjudicator verdict invalid: {len(flagged - set(rows))} missing, "
                         f"{len(set(rows) - flagged)} extra, {len(bad)} bad choices")  # fmt: skip
    logger.info("adjudicator: %d rows, %d marked uncertain", len(rows), sum(r["uncertain"] for r in rows.values()))
    return {k: {"choice": r["choice"], "identity": r["name"]} for k, r in rows.items()}


def finalize(work: Path, owner_dir: Path | None) -> None:
    state = json.loads((work / "jury" / "merged.json").read_text())
    merged, flags = state["merged"], state["flags"]
    owner = {}
    for path in owner_dir.glob("*.json") if owner_dir else ():
        raw = json.loads(path.read_text())
        owner[path.stem] = raw["data"] if isinstance(raw.get("data"), dict) else raw
    adjudicated = load_adjudicator(work, {k for k, v in flags.items() if v})

    labels: dict[str, dict] = {}
    majority: dict[str, dict] = {}
    n_from_owner = n_from_jury = n_from_adjudicator = n_missing = 0
    for r in load_entries(work):
        out = labels[f"{r['arm']}/{r['reader']}"] = {}
        out_majority = majority[f"{r['arm']}/{r['reader']}"] = {}
        for e in r["entries"]:
            k = key(r["arm"], r["reader"], e["id"])
            o = owner.get(k) or {}
            if o.get("choice"):
                source, n_from_owner = o, n_from_owner + 1
            elif k in adjudicated:
                source, n_from_adjudicator = adjudicated[k], n_from_adjudicator + 1
            elif not flags[k]:
                source, n_from_jury = merged[k], n_from_jury + 1
            else:
                n_missing += 1
                continue
            identity = (source.get("identity") or "").strip()
            if source["choice"] == "project" and not identity:
                n_missing += 1
                continue
            owner_ticks = set(o.get("maps") or [])
            row = {
                "identity": identity if source["choice"] == "project" else None,
                "generic": source["choice"] == "generic",
            }
            out[e["id"]] = {**row, "maps_to": sorted(owner_ticks | set(merged[k]["maps"]))}
            out_majority[e["id"]] = {**row, "maps_to": sorted(owner_ticks | set(merged[k]["maps_majority"]))}
    (work / "labels.json").write_text(json.dumps(labels, indent=2) + "\n", encoding="utf-8")
    (work / "labels_majority.json").write_text(json.dumps(majority, indent=2) + "\n", encoding="utf-8")
    logger.info("wrote labels: %d from the owner, %d from the adjudicator, %d from the jury, %d unlabelled",
                n_from_owner, n_from_adjudicator, n_from_jury, n_missing)  # fmt: skip


def sensitivity(work: Path) -> None:
    """Report recovered under both tick rules. Descriptive only: the union decides the outcome."""

    def recovered(path: Path) -> float:
        all_labels = json.loads(path.read_text())
        counts = []
        for name, entries in all_labels.items():
            if name.startswith("colliculix/"):
                counts.append(len({m for e in entries.values() if not e["generic"] for m in e["maps_to"]}))
        return statistics.median(counts)

    logger.info("colliculix recovered median, union ticks: %s", recovered(work / "labels.json"))
    logger.info("colliculix recovered median, ticks from 3 of 4: %s", recovered(work / "labels_majority.json"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "collect", "merge", "adjudicate", "join", "finalize", "sensitivity"))
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
    elif args.command == "adjudicate":
        adjudicate(args.work_dir)
    elif args.command == "join":
        join_adjudicator(args.work_dir)
    elif args.command == "sensitivity":
        sensitivity(args.work_dir)
    else:
        finalize(args.work_dir, args.owner_dir)


if __name__ == "__main__":
    main()
