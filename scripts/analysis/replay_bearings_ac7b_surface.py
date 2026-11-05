"""AC-7b readers on Cursor and Antigravity (owner 261105), sent through praxia surface_dispatch.

The packet is given inside the prompt, rendered with line numbers, so every reader sees all of its
packet by construction and needs no file access. Each reader runs in its own empty folder, a linked
worktree of an empty repository, under the ``completion`` role, which returns one structured verdict.

build:   writes prompts/<arm>.md (the packet), prompts/system.md, schema.json and empty folders.
collect: reads praxia's dispatch records and writes readings/<arm>/<reader>.json for admitted
         verdicts. It prints only status and counts, never reading content.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
from pathlib import Path

from replay_bearings_ac7 import READERS
from replay_bearings_ac7b import ARMS

from bathos.bearings.packet import verify_packet

logger = logging.getLogger("replay_bearings_ac7b_surface")

# Two Cursor readers and one Antigravity reader per arm, the same in every arm.
SURFACES = {"reader1": "cursor", "reader2": "antigravity", "reader3": "cursor"}

SYSTEM = """You are reading the record of a software and research project. The whole record is in the user \
message, and it is everything you may use. Do not open files, run commands or use any tools, and do not use any \
knowledge you may have about this project from elsewhere. Read all of it before you answer.

The record has four parts: index.json (the documents and sidecars you may cite, with blob hashes), runs.jsonl (one \
tracked run per line), git_log.jsonl (one commit subject per line), and each document and sidecar in full, with line \
numbers.

Task: describe what this project appears to be doing, and how its lines of work relate, judging from this record \
alone. A project's aims need not form a tree: one line can serve several others, and lines can be related across \
levels.

Return one JSON object:
{"packet_sha256": "<given>", "reader_id": "<given>",
 "purposes": [{"id": "r1", "text": "...", "citations": ["...", "..."], "uncertain": false, "uncertain_reason": null}],
 "open_lines": [{"text": "...", "citations": ["..."], "uncertain": false, "uncertain_reason": null}],
 "links": [{"from": "r2", "to": "r1", "type": "feeds", "citations": ["..."], "uncertain": false}]}

- purposes: each line of work the record shows effort toward, at whatever level it appears, as many or as few as \
the record shows. Name what each is for, specifically enough that someone could disagree. "Developing tooling" is \
not specific enough. Each needs at least 2 citations to different records. Cite the runs and commits that carry its \
work, not just the documents that describe it.
- open_lines: work with no visible purpose. Each needs at least 1 citation. Refer to them in links as open:0, \
open:1 and so on, in list order.
- links: how lines relate. Use exactly one type per link:
  - feeds: a result of "from" is actually used by "to". Cite where the use shows.
  - serves: "from" was done for "to"'s sake, but no result has passed yet.
  - shares: they use a common method, code or data, but neither uses the other's results.
  - competes: the record shows a trade-off between them, such as one deferred, declined or scheduled behind the \
other.
  Add a link only where the record shows it, and each link needs at least 1 citation. Two lines with no link \
between them are a finding, not an omission.
- uncertain: set it to true wherever a careful reader could reasonably write it differently, and say why in \
uncertain_reason. This has no penalty.

Citation forms, which must point at something in the record:
- run:<id>, or a unique prefix of at least 8 characters
- git:<sha>, at least 7 characters
- sidecar:<path>, with the path as listed in index.json
- doc:<path>@<blob_sha>#Lx-Ly, with the path and blob as listed in index.json, and a line range that exists in \
the document as numbered below

Report only what the record supports."""


def reading_schema() -> dict:
    cites = {"type": "array", "items": {"type": "string"}}
    node = {
        "type": "object",
        "required": ["text", "citations"],
        "properties": {
            "id": {"type": "string"},
            "text": {"type": "string"},
            "citations": cites,
            "uncertain": {"type": "boolean"},
            "uncertain_reason": {"type": ["string", "null"]},
        },
    }
    purpose = {**node, "required": ["id", "text", "citations"]}
    link = {
        "type": "object",
        "required": ["from", "to", "type", "citations"],
        "properties": {
            "from": {"type": "string"},
            "to": {"type": "string"},
            "type": {"enum": ["feeds", "serves", "shares", "competes"]},
            "citations": cites,
            "uncertain": {"type": "boolean"},
        },
    }
    return {
        "type": "object",
        "required": ["packet_sha256", "reader_id", "purposes", "open_lines", "links"],
        "properties": {
            "packet_sha256": {"type": "string"},
            "reader_id": {"type": "string"},
            "purposes": {"type": "array", "items": purpose},
            "open_lines": {"type": "array", "items": node},
            "links": {"type": "array", "items": link},
        },
    }


def numbered(text: str) -> str:
    return "\n".join(f"{i}\t{line}" for i, line in enumerate(text.splitlines(), 1))


def render_packet(packet: Path) -> str:
    reader = packet / "reader"
    index = json.loads((reader / "index.json").read_text(encoding="utf-8"))
    parts = [
        "# index.json\n" + (reader / "index.json").read_text(encoding="utf-8"),
        "# runs.jsonl\n" + (reader / "runs.jsonl").read_text(encoding="utf-8"),
        "# git_log.jsonl\n" + (reader / "git_log.jsonl").read_text(encoding="utf-8"),
    ]
    for d in index["docs"]:
        text = (reader / "docs" / d["path"]).read_text(encoding="utf-8")
        parts.append(f"# doc {d['path']} (blob {d['blob_sha']}), numbered lines\n{numbered(text)}")
    for s in index["sidecars"]:
        text = (reader / "sidecars" / s["blob_sha"] / s["path"]).read_text(encoding="utf-8")
        parts.append(f"# sidecar {s['path']} (blob {s['blob_sha']}), numbered lines\n{numbered(text)}")
    return "\n\n".join(parts)


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def build(work: Path) -> None:
    root = work / "surface"
    prompts = root / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    (prompts / "system.md").write_text(SYSTEM + "\n", encoding="utf-8")
    (root / "schema.json").write_text(json.dumps(reading_schema()), encoding="utf-8")
    base = root / "base"
    if not base.exists():
        git("init", "-q", str(base))
        git("-C", str(base), "-c", "user.name=ac7b", "-c", "user.email=ac7b@localhost",
            "commit", "-q", "--allow-empty", "-m", "empty")  # fmt: skip
    for arm in ARMS:
        packet = work / "packets" / arm
        sha = verify_packet(packet)
        body = render_packet(packet)
        for reader in READERS:
            head = f"packet_sha256: {sha}\nreader_id: ac7b-{arm}-{reader}\n\n"
            (prompts / f"{arm}_{reader}.md").write_text(head + body, encoding="utf-8")
            folder = root / "folders" / f"{arm}_{reader}"
            if not folder.exists():
                git("-C", str(base), "worktree", "add", "-q", "-b", folder.name, str(folder))
        logger.info("%s: prompt %d bytes", arm, len(body))


def collect(work: Path) -> None:
    root = work / "surface"
    dispatches = json.loads((root / "dispatches.json").read_text())
    records: dict[str, dict] = {}
    runs = root / "base" / ".praxia" / "dispatch_runs.jsonl"
    for line in runs.read_text().splitlines() if runs.exists() else []:
        obj = json.loads(line)
        for rec in (obj, *(v for v in obj.values() if isinstance(v, dict))):
            if "dispatch_id" in rec and "terminal_state" in rec:
                records[rec["dispatch_id"]] = rec
    transcripts = {}
    for key, dispatch_id in sorted(dispatches.items()):
        rec = records.get(dispatch_id)
        if rec is None:
            logger.info("%s: running", key)
            continue
        admitted = rec.get("admission") == "admitted"
        reasons = [] if admitted else (rec.get("admission") or {}).get("rejected", {}).get("reasons", [])
        out = rec.get("structured_output")
        if admitted and isinstance(out, dict):
            arm, reader = key.split("/")
            path = work / "readings" / arm / f"{reader}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
            transcripts[key] = f"inline:{dispatch_id}"
        logger.info("%s: %s, usable=%s, rejection reasons=%d", key, rec["terminal_state"], admitted, len(reasons))
    (work / "reader_transcripts.json").write_text(json.dumps(transcripts, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "collect"))
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    (build if args.command == "build" else collect)(args.work_dir.resolve())


if __name__ == "__main__":
    main()
