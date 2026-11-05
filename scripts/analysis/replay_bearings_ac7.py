"""Bearings AC-7 replay (spec 261104): does a blind reader see drift where drift was, and not where it was not?

Two uses:

* ``--build-only`` builds the three blind packets for the readers. It reports only hashes and counts.
* The tracked run (``bth run``) comes after the readings and the owner's labels exist. It rebuilds each
  packet to confirm its hash on real data, checks for contamination, audits what the readers opened,
  re-resolves every citation, and scores the owner's labels.

Neither use appends a bearings event. A replay must not count as a real reading of a live project, so
citations are resolved directly and nothing is recorded in the project ledger.

Labels file (``<work-dir>/labels.json``), written by the owner after reading the readings:

    {"<arm>/<reader>": {"r1": {"identity": "name or null", "generic": false, "maps_to": ["A"]},
                        "open:0": {"identity": null, "generic": false, "maps_to": []}}}

``identity`` names the project-level purpose an entry shows, so two entries with the same name count
once. ``null`` means it is not project-level: a sub-line of something else, or not supported.
``generic`` marks text too vague to disagree with, and it does not count. ``maps_to`` is used only for
colliculix and lists which of the diagnosis's purposes A to G the entry corresponds to.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import statistics
import tempfile
from pathlib import Path

from bathos.bearings.packet import build_packet, verify_packet
from bathos.bearings.reading import FLAG, RESOLVED, _PacketView, resolve_citation

logger = logging.getLogger("replay_bearings_ac7")

HOME = Path.home()
# Cuts are commits, windows are 28 days back from the commit's own time. tev_design and plegadx were
# chosen before any packet was built, from run, commit and doc counts only.
ARMS = {
    "colliculix": {
        "repo": HOME / "projects/colliculix",
        "ref": "14c3804",
        "since": "2026-06-09T13:01:25-07:00",
        "until": "2026-07-07T13:01:25-07:00",
    },
    "tev_design": {
        "repo": HOME / "projects/tev_design",
        "ref": "b2b5386f",
        "since": "2026-06-30T15:56:38-04:00",
        "until": "2026-07-28T15:56:38-04:00",
    },
    "plegadx": {
        "repo": HOME / "projects/plegadx",
        "ref": "1472ee54",
        "since": "2026-08-19T10:59:11-07:00",
        "until": "2026-09-16T10:59:11-07:00",
    },
}
READERS = ("reader1", "reader2", "reader3")
# From spec AC-7. A hit means the positive control saw the diagnosis and the run is invalid.
CONTAMINATION_TERMS = (
    "colliculix-drift-full-diagnosis",
    "identity-charter",
    "refoundation handoff",
    "purpose ledger",
    ".bth/charter.toml",
)
ALLOWED_TOOLS = {"Read", "Glob", "Grep", "Write", "Edit"}  # Write and Edit pass only on the reader's own reading


def contamination_hits(reader_dir: Path) -> list[str]:
    hits = []
    for f in sorted(p for p in reader_dir.rglob("*") if p.is_file()):
        text = f.read_bytes().decode("utf-8", errors="replace").lower()
        hits.extend(f"{f.relative_to(reader_dir)}: {t}" for t in CONTAMINATION_TERMS if t in text)
    return hits


def audit_transcript(path: Path, allowed_roots: list[str]) -> list[str]:
    """Return every tool call in a reader's transcript that left its packet or used another tool."""
    bad = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        content = (rec.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name, args = block.get("name"), block.get("input") or {}
            # The dispatch header requires agent_result, ToolSearch loads it, and SubagentHandback returns
            # the final report. None of them reads project data.
            if name in ("ToolSearch", "SubagentHandback") or str(name).endswith("__agent_result"):
                continue
            if name not in ALLOWED_TOOLS:
                bad.append(f"{name}: tool not allowed")
                continue
            target = args.get("file_path") or args.get("path")
            # A Glob or Grep with no path searches the working directory, which is not the packet.
            if not target or not any(str(target).startswith(r) for r in allowed_roots):
                bad.append(f"{name}: {target}")
    return bad


def read_fractions(transcript: Path, reader_dir: Path) -> tuple[float, float]:
    """Fractions of the packet's docs and sidecars this reader opened. Descriptive; gates nothing."""
    opened = set()
    for line in transcript.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        content = (rec.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Read":
                opened.add(str(block.get("input", {}).get("file_path", "")))
    index = json.loads((reader_dir / "index.json").read_text(encoding="utf-8"))
    docs = [str(reader_dir / "docs" / d["path"]) for d in index["docs"]]
    sidecars = [str(reader_dir / "sidecars" / s["blob_sha"] / s["path"]) for s in index["sidecars"]]
    frac = lambda paths: sum(p in opened for p in paths) / len(paths) if paths else 1.0  # noqa: E731
    return frac(docs), frac(sidecars)


def score_arm(labels_by_reader: dict[str, dict]) -> dict[str, float]:
    """Medians across readers of distinct project-level identities and of recovered A-G purposes."""
    identities, recovered = [], []
    for labels in labels_by_reader.values():
        counted = [e for e in labels.values() if not e.get("generic")]
        identities.append(len({e["identity"] for e in counted if e.get("identity")}))
        recovered.append(len({m for e in counted for m in e.get("maps_to", [])}))
    return {"identities": statistics.median(identities), "recovered": statistics.median(recovered)}


def controls_pass() -> bool:
    """Synthetic ground truth for the scorer, including inputs that must score low."""
    full = {f"r{i}": {"identity": f"i{i}", "generic": False, "maps_to": [c]} for i, c in enumerate("ABCDEFG")}
    generic = {f"r{i}": {"identity": f"i{i}", "generic": True, "maps_to": [c]} for i, c in enumerate("ABCDEFG")}
    same = {f"r{i}": {"identity": "one", "generic": False, "maps_to": []} for i in range(5)}
    sub_lines = {f"r{i}": {"identity": None, "generic": False, "maps_to": []} for i in range(5)}
    arm = lambda x: score_arm({"a": x, "b": x, "c": x})  # noqa: E731
    ok = arm(full) == {"identities": 7, "recovered": 7}
    ok &= arm(generic) == {"identities": 0, "recovered": 0}
    ok &= arm(same)["identities"] == 1
    ok &= arm(sub_lines)["identities"] == 0
    ok &= score_arm({"a": full, "b": same, "c": sub_lines})["identities"] == 1

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "ok.txt").write_text("an ordinary record\n")
        ok &= contamination_hits(root) == []
        (root / "bad.txt").write_text("See the Refoundation Handoff for this.\n")
        ok &= len(contamination_hits(root)) == 1

        call = {"message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/pkt/a"}}]}}
        (root / "clean.jsonl").write_text(json.dumps(call) + "\n")
        ok &= audit_transcript(root / "clean.jsonl", ["/pkt"]) == []
        call["message"]["content"][0]["input"]["file_path"] = "/home/me/secret.md"
        (root / "escape.jsonl").write_text(json.dumps(call) + "\n")
        ok &= len(audit_transcript(root / "escape.jsonl", ["/pkt"])) == 1
        call["message"]["content"][0] = {"type": "tool_use", "name": "Bash", "input": {"command": "cat x"}}
        (root / "bash.jsonl").write_text(json.dumps(call) + "\n")
        ok &= len(audit_transcript(root / "bash.jsonl", ["/pkt"])) == 1
        call["message"]["content"][0] = {"type": "tool_use", "name": "Grep", "input": {"pattern": "x"}}
        (root / "nopath.jsonl").write_text(json.dumps(call) + "\n")
        ok &= len(audit_transcript(root / "nopath.jsonl", ["/pkt"])) == 1
        call["message"]["content"][0] = {"type": "tool_use", "name": "mcp__praxia__agent_result", "input": {}}
        (root / "result.jsonl").write_text(json.dumps(call) + "\n")
        ok &= audit_transcript(root / "result.jsonl", ["/pkt"]) == []
    return bool(ok)


def build_arm(name: str, out: Path) -> dict:
    a = ARMS[name]
    return build_packet(a["repo"], out=out, since=a["since"], until=a["until"], ref=a["ref"], project=name)


def citation_failures(reading: dict, packet_dir: Path) -> int:
    """Count entries short of their citation minimum, using the same resolver ``record`` uses."""
    view = _PacketView(packet_dir)
    bad = 0
    for entries, minimum in ((reading.get("purposes", []), 2), (reading.get("open_lines", []), 1)):
        for e in entries:
            res = [resolve_citation(c, view) for c in e.get("citations", [])]
            keys = {key for r, key in res if r == RESOLVED}
            flagged = sum(1 for r, _ in res if r == FLAG)
            if len(keys) < minimum or flagged:
                bad += 1
    return bad


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=Path("outputs/ac7"))
    parser.add_argument("--build-only", action="store_true", help="Build the packets for the readers and stop.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    work: Path = args.work_dir.resolve()

    if args.build_only:
        for name in ARMS:
            res = build_arm(name, work / "packets" / name)
            logger.info(
                "%s: %s runs=%s commits=%s docs=%s sidecars=%s excluded=%s",
                name, res["packet_sha256"][:12], res["n_runs"], res["n_git_commits"],
                res["n_docs"], res["n_sidecars"], res["n_excluded"],
            )
        return

    labels = json.loads((work / "labels.json").read_text(encoding="utf-8"))
    transcripts = json.loads((work / "reader_transcripts.json").read_text(encoding="utf-8"))

    contamination = hash_mismatches = audit_bad = rejected = n_readings = n_unlabelled = 0
    unresolved_sidecars = {}
    min_docs: dict[str, float] = {}
    min_sidecars: dict[str, float] = {}
    by_arm: dict[str, dict[str, dict]] = {}
    for name in ARMS:
        packet = work / "packets" / name
        sha = verify_packet(packet)
        with tempfile.TemporaryDirectory() as td:
            if build_arm(name, Path(td) / "rebuild")["packet_sha256"] != sha:
                hash_mismatches += 1
        # The terms name colliculix's own diagnosis, so only that arm can be contaminated by them.
        hits = contamination_hits(packet / "reader") if name == "colliculix" else []
        contamination += len(hits)
        for h in hits:
            logger.error("contamination in %s: %s", name, h)
        manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
        unresolved_sidecars[name] = len(manifest["sidecar_unresolved"])

        by_arm[name] = {}
        for reader in READERS:
            key = f"{name}/{reader}"
            reading_path = work / "readings" / name / f"{reader}.json"
            if not reading_path.is_file():
                continue
            n_readings += 1
            reading = json.loads(reading_path.read_text(encoding="utf-8"))
            if reading.get("packet_sha256") != sha:
                rejected += 1
            rejected += citation_failures(reading, packet)
            roots = [str(packet / "reader"), str(reading_path)]
            bad = audit_transcript(Path(transcripts[key]), roots)
            audit_bad += len(bad)
            for b in bad:
                logger.error("%s left its packet: %s", key, b)
            doc_frac, sidecar_frac = read_fractions(Path(transcripts[key]), packet / "reader")
            min_docs[name] = min(min_docs.get(name, 1.0), doc_frac)
            min_sidecars[name] = min(min_sidecars.get(name, 1.0), sidecar_frac)
            reader_labels = labels.get(key, {})
            expected = {p["id"] for p in reading.get("purposes", [])}
            expected |= {f"open:{i}" for i in range(len(reading.get("open_lines", [])))}
            n_unlabelled += len(expected - set(reader_labels))
            by_arm[name][reader] = reader_labels

    scores = {name: score_arm(r) if r else {"identities": 0, "recovered": 0} for name, r in by_arm.items()}
    results = {
        "controls_ok": controls_pass(),
        "contamination_hits": contamination,
        "packet_hash_mismatches": hash_mismatches,
        "reader_audit_violations": audit_bad,
        "rejected_readings": rejected,
        "n_readings": n_readings,
        "n_unlabelled": n_unlabelled,
        "colliculix_recovered_median": scores["colliculix"]["recovered"],
        "colliculix_identities_median": scores["colliculix"]["identities"],
        "tev_design_identities_median": scores["tev_design"]["identities"],
        "plegadx_identities_median": scores["plegadx"]["identities"],
        "colliculix_unresolved_sidecars": unresolved_sidecars["colliculix"],
        "tev_design_unresolved_sidecars": unresolved_sidecars["tev_design"],
        "plegadx_unresolved_sidecars": unresolved_sidecars["plegadx"],
        **{f"{n}_min_docs_read_frac": min_docs.get(n, 0.0) for n in ARMS},
        **{f"{n}_min_sidecars_read_frac": min_sidecars.get(n, 0.0) for n in ARMS},
    }
    text = json.dumps(results, indent=2, sort_keys=True)
    logger.info(text)
    results_path = os.environ.get("BTH_RESULTS_PATH")
    if results_path:
        Path(results_path).write_text(text, encoding="utf-8")
    shutil.copyfile(work / "labels.json", work / "labels.scored.json")


if __name__ == "__main__":
    main()
