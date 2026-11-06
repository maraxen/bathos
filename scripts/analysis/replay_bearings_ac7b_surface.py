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
import contextlib
import json
import logging
import subprocess
from pathlib import Path

from replay_bearings_ac7 import READERS
from replay_bearings_ac7b import ARMS

from bathos.bearings.packet import verify_packet

STUDY = "ac7b"  # main() rebinds STUDY and ARMS for --study ac7c (planted-drift control)

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
            head = f"packet_sha256: {sha}\nreader_id: {STUDY}-{arm}-{reader}\n\n"
            (prompts / f"{arm}_{reader}.md").write_text(head + body, encoding="utf-8")
            folder = root / "folders" / f"{arm}_{reader}"
            if not folder.exists():
                git("-C", str(base), "worktree", "add", "-q", "-b", folder.name, str(folder))
        logger.info("%s: prompt %d bytes", arm, len(body))


def last_json_block(text: str) -> dict | None:
    """praxia's own rule, ported from praxia-dispatch/src/materialize.rs:399: the last ```json block that parses."""
    last = None
    start = 0
    while (i := text.find("```json", start)) != -1:
        body_start = i + len("```json")
        j = text.find("```", body_start)
        if j == -1:
            break
        with contextlib.suppress(json.JSONDecodeError):
            last = json.loads(text[body_start:j].strip())
        start = j + 3
    return last if isinstance(last, dict) else None


def recover_result(text: str) -> dict | None:
    """The extraction rule (sidecar note 261105, registered before any count on a recovered reading).

    First praxia's own rule (``last_json_block``). praxia reads a Cursor answer only that way,
    so a reader that returns bare JSON, or JSON with only a closing fence, is rejected although
    its answer is complete. Failing that, this rule strips at most one opening fence line
    (``` or ```json) and at most one closing ``` line from the final text, and requires the rest
    to parse as one JSON object. Anything else is invalid, and the reader gets its one redo.
    """
    if (obj := last_json_block(text)) is not None:
        return obj
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def read_log(log: Path) -> tuple[dict[str, int], str | None]:
    """Count stream-json events by type (tool calls included) and return the final result text.

    Handles both Cursor's stream (``type``; ``result`` is the text) and agy's (``event``;
    ``step_update.step_type`` names the step; ``result.response`` is the text, kept only when
    ``result.status`` is SUCCESS and the turn was not cut off).
    """
    counts: dict[str, int] = {}
    result = None
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            counts["unparsed_line"] = counts.get("unparsed_line", 0) + 1
            continue
        if "event" in event:
            kind = event["event"]
            if kind == "step_update":
                kind = f"step:{event['step_update'].get('step_type', '?')}"
        else:
            kind = event.get("type", "?")
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "result":
            res = event.get("result")
            if isinstance(res, dict):
                ok = res.get("status") == "SUCCESS" and not res.get("finished_reason")
                result = res.get("response") if ok else None
            else:
                result = res
    return counts, result


# Praxia's completion instruction, copied verbatim (praxia-dispatch/src/materialize.rs:21) so a
# direct agy reader gets the same prompt a praxia dispatch would have rendered.
FENCED_JSON_OUTPUT_INSTRUCTION = (
    "\n## Structured Output\n"
    "When you are finished, end your final message with exactly one fenced "
    "```json code block containing your structured output. This CLI has no "
    "--json-schema flag, so that fenced block IS the structured-output contract -- "
    "if you emit more than one fenced json block, only the LAST one is read.\n"
)
AGY_MODEL = "gemini-4-argon-high"  # praxia model_registry.toml [backends.antigravity] power
AGY_TIMEOUT_S = 2400  # matches the Cursor readers' wall_timeout_secs


def agy_argv() -> list[str]:
    return ["agy", "--print=", "--input-format", "stream-json", "--output-format", "stream-json",
            "--sandbox", "--mode", "plan", "--print-timeout", f"{AGY_TIMEOUT_S}s", "--model", AGY_MODEL]  # fmt: skip


def run_agy(work: Path) -> None:
    """Run the three reader2 (Antigravity) readers directly, prompt on stdin, one per empty folder.

    praxia passes the prompt to agy as one argv element (praxia-agy-executor/src/lib.rs:912),
    which the kernel refuses above 128 KiB, so these readers never started through praxia.
    The prompt is the one praxia's render_completion_prompt would build (materialize.rs:368).
    """
    root = work / "surface"
    prompts = root / "prompts"
    system = (prompts / "system.md").read_text(encoding="utf-8").rstrip()
    procs = {}
    record = {}
    for arm in ARMS:
        key = f"{arm}/reader2"
        folder = root / "folders" / f"{arm}_reader2"
        user = (prompts / f"{arm}_reader2.md").read_text(encoding="utf-8").rstrip()
        prompt = system + "\n\n" + user + "\n" + FENCED_JSON_OUTPUT_INSTRUCTION
        line = json.dumps({"event": "user", "message": {"role": "user", "content": prompt}})
        (folder / "agy_input.ndjson").write_text(line + "\n", encoding="utf-8")
        log = folder / "agy_run.ndjson"
        with (folder / "agy_input.ndjson").open("rb") as stdin:
            procs[key] = subprocess.Popen(agy_argv(), cwd=folder, stdin=stdin, stdout=log.open("wb"),
                                          stderr=(folder / "agy_err.txt").open("wb"))  # fmt: skip
        record[key] = {"argv": agy_argv(), "folder": str(folder), "log": str(log), "prompt_bytes": len(prompt)}
        logger.info("%s: started, prompt %d bytes", key, len(prompt))
    (root / "agy_runs.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    for key, proc in procs.items():
        try:
            code = proc.wait(timeout=AGY_TIMEOUT_S + 300)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = "killed"
        record[key]["exit"] = code
        logger.info("%s: exit %s", key, code)
    (root / "agy_runs.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


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
    agy_file = root / "agy_runs.json"
    agy_runs = json.loads(agy_file.read_text()) if agy_file.exists() else {}
    transcripts = {}
    events = {}
    for key, dispatch_id in sorted(dispatches.items()):
        arm, reader = key.split("/")
        if key in agy_runs:  # run directly on agy; praxia's dispatch never started (see run_agy)
            rec = {"terminal_state": f"agy exit {agy_runs[key].get('exit', 'running')}"}
            log = Path(agy_runs[key]["log"])
            dispatch_id = f"agy:{arm}_{reader}"
        else:
            rec = records.get(dispatch_id)
            log = root / "folders" / f"{arm}_{reader}" / ".praxia" / "dispatch" / dispatch_id / "run.log"
        if rec is None:
            logger.info("%s: running", key)
            continue
        admitted = rec.get("admission") == "admitted"
        out = rec.get("structured_output") if admitted else None
        source = "praxia" if isinstance(out, dict) else None
        if log.exists():
            counts, text = read_log(log)
            events[key] = counts
            recovered = recover_result(text) if text is not None else None
            if source == "praxia" and recovered is not None and recovered != out:
                raise SystemExit(f"{key}: recovery rule disagrees with praxia's admitted output")
            if source is None and recovered is not None:
                out, source = recovered, "recovered"
        if isinstance(out, dict):
            path = work / "readings" / arm / f"{reader}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
            transcripts[key] = f"inline:{dispatch_id}"
        logger.info("%s: %s, admitted=%s, reading source=%s", key, rec["terminal_state"], admitted, source)
    (work / "reader_transcripts.json").write_text(json.dumps(transcripts, indent=2) + "\n", encoding="utf-8")
    (root / "event_counts.json").write_text(json.dumps(events, indent=2) + "\n", encoding="utf-8")
    for key, counts in sorted(events.items()):
        tools = {k: v for k, v in counts.items() if "tool" in k}
        logger.info("%s: events=%d, tool events=%s", key, sum(counts.values()), tools or 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "agy", "collect"))
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--study", choices=("ac7b", "ac7c"), default="ac7b")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.study == "ac7c":
        import replay_bearings_ac7c

        global ARMS, STUDY
        ARMS, STUDY = replay_bearings_ac7c.ARMS, "ac7c"
    {"build": build, "agy": run_agy, "collect": collect}[args.command](args.work_dir.resolve())


if __name__ == "__main__":
    main()
