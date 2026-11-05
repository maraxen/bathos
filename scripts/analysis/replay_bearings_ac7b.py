"""Bearings AC-7b replay (spec 261104): does a reader's typed purpose graph separate drift from busy, connected projects?

Three uses:

* ``--build-only`` builds the three blind packets for the readers. It reports only hashes and counts.
* ``--list-bridges`` writes ``<work-dir>/bridges.json`` after the readings exist: for each reading, the
  binding links (feeds, serves) whose removal would split a group. Only these are labelled. It prints
  counts only, so no score is visible before the tracked run.
* The tracked run (``bth run``) comes after the readings and bridge labels exist. It rebuilds each packet
  to confirm its hash, checks for contamination, audits what the readers opened, validates each reading
  as ``bth bearings record`` would, and counts drift groups.

Nothing appends a bearings event: a replay must not count as a real reading of a live project.

Bridge labels file (``<work-dir>/bridge_labels.json``), one entry per listed bridge, keyed by the link's
index in the reading's ``links`` list:

    {"<arm>/<reader>": {"3": "supported", "5": "unsure"}}

Scoring, from the sidecar:
- A node is a purpose or an open line (``open:<i>``). Its effort is the number of distinct resolved
  ``run:`` and ``git:`` citations it carries.
- Drift groups are the connected components under binding links, ignoring direction, after removing the
  bridges labelled unsupported (and, for the outcome, unsure), that hold at least 10% of total effort.
- ``shares`` and ``competes`` links never join groups.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import statistics
import tempfile
from pathlib import Path

from replay_bearings_ac7 import (
    HOME,
    READERS,
    audit_transcript,
    citation_failures,
    contamination_hits,
    read_fractions,
)
from replay_bearings_ac7 import (
    controls_pass as ac7_controls_pass,
)

from bathos.bearings.packet import build_packet, verify_packet
from bathos.bearings.reading import _DOC_RE, RESOLVED, _load_reading, _PacketView, resolve_citation
from bathos.errors import BearingsReadingInvalidError

logger = logging.getLogger("replay_bearings_ac7b")

# Cuts are commits, and each window is the 28 days before its cut. colliculix and plegadx are as in
# AC-7. demistify replaces tev_design (owner 261105): the tev_design packet at 5f4856c7 was too large
# for one reader to read.
ARMS = {
    "colliculix": {
        "repo": HOME / "projects/colliculix",
        "ref": "14c3804",
        "since": "2026-06-09T13:01:25-07:00",
        "until": "2026-07-07T13:01:25-07:00",
    },
    "demistify": {
        "repo": HOME / "projects/demistify",
        "ref": "3417ca40",
        "since": "2026-10-05T20:58:00-05:00",
        "until": "2026-11-02T20:58:00-05:00",
    },
    "plegadx": {
        "repo": HOME / "projects/plegadx",
        "ref": "1472ee54",
        "since": "2026-08-19T10:59:11-07:00",
        "until": "2026-09-16T10:59:11-07:00",
    },
}
BINDING = ("feeds", "serves")
MIN_GROUP_SHARE = 0.10  # judgment cutoff, sidecar
LABELS = ("supported", "unsupported", "unsure")

Edge = tuple[int, str, str]  # (index in the reading's links list, endpoint, endpoint)


# ── graph core: pure functions, tested by the synthetic controls ──────────────


def _components(nodes: list[str], edges: list[Edge]) -> list[set[str]]:
    parent = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _, a, b in edges:
        parent[find(a)] = find(b)
    groups: dict[str, set[str]] = {}
    for n in nodes:
        groups.setdefault(find(n), set()).add(n)
    return list(groups.values())


def bridges(nodes: list[str], edges: list[Edge]) -> list[int]:
    """Indices of edges whose removal increases the number of components. Parallel edges are never bridges."""
    base = len(_components(nodes, edges))
    return [
        i for i, _, _ in edges if len(_components(nodes, [e for e in edges if e[0] != i])) > base
    ]


def drift_groups(effort: dict[str, int], edges: list[Edge], removed: set[int]) -> int:
    """Components, after dropping removed edges, that hold at least MIN_GROUP_SHARE of total effort."""
    total = sum(effort.values())
    if total == 0:
        return 0
    kept = [e for e in edges if e[0] not in removed]
    return sum(
        1
        for g in _components(list(effort), kept)
        if sum(effort[n] for n in g) / total >= MIN_GROUP_SHARE
    )


def binding_edges(links: list[dict]) -> list[Edge]:
    """Edges that join groups: feeds and serves only, keyed by the link's index in the reading."""
    return [(i, lk["from"], lk["to"]) for i, lk in enumerate(links) if lk.get("type") in BINDING]


def graph_controls_pass() -> bool:
    """Synthetic ground truth from the sidecar. A graph scorer that cannot fail these is not trusted."""
    link = lambda a, b, t="feeds": {"from": a, "to": b, "type": t}  # noqa: E731
    two = {f"a{i}": 5 for i in range(3)} | {f"b{i}": 5 for i in range(3)}
    chains = [link("a0", "a1"), link("a1", "a2", "serves"), link("b0", "b1"), link("b1", "b2")]
    ok = drift_groups(two, binding_edges(chains), set()) == 2  # two disjoint fed chains
    ok &= (
        drift_groups(two, binding_edges([*chains, link("a2", "b0")]), set()) == 1
    )  # one fully fed graph
    # Joined only by shares or competes links: these must not join the islands.
    ok &= drift_groups(two, binding_edges([*chains, link("a2", "b0", "shares")]), set()) == 2
    ok &= drift_groups(two, binding_edges([*chains, link("a2", "b0", "competes")]), set()) == 2
    tiny = two | {"c0": 1}
    ok &= (
        drift_groups(tiny, binding_edges(chains), set()) == 2
    )  # a 1/31 island is below 10% and ignored
    ok &= drift_groups({"a0": 0, "b0": 0}, [], set()) == 0  # no effort, no groups
    # Planted bridges: a triangle (no bridges inside) joined to a chain by one bridge, index 30.
    tri = [(0, "x", "y"), (1, "y", "z"), (2, "z", "x")]
    nodes = ["x", "y", "z", "p", "q"]
    edges = [*tri, (30, "z", "p"), (31, "p", "q")]
    ok &= sorted(bridges(nodes, edges)) == [30, 31]
    ok &= bridges(nodes, [*edges, (32, "z", "p")]) == [31]  # a doubled link is not a bridge
    # Removing an unsupported bridge splits the graph.
    eff = {n: 5 for n in nodes}
    ok &= drift_groups(eff, edges, set()) == 1 and drift_groups(eff, edges, {30}) == 2
    # Coverage: overlapping Read windows count each line once; another file's reads do not count.
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "runs.jsonl"
        f.write_text("".join(f"{i}\n" for i in range(10)))
        reads = [{"file_path": str(f), "offset": 1, "limit": 4}, {"file_path": str(f), "offset": 3, "limit": 3}]
        ok &= lines_read_frac(reads, f) == 0.5
        ok &= lines_read_frac([{"file_path": str(f)}], f) == 1.0
        ok &= lines_read_frac([{"file_path": str(Path(td) / "other")}], f) == 0.0
    return bool(ok)


# ── coverage (descriptive, owner 261105; gates nothing) ───────────────────────

READ_DEFAULT_LIMIT = 2000  # lines the Read tool returns when no limit is given


def _read_calls(transcript: Path) -> list[dict]:
    calls = []
    for line in transcript.read_text(encoding="utf-8").split("\n"):
        try:
            rec = json.loads(line) if line.strip() else None
        except ValueError:
            continue
        content = ((rec or {}).get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Read":
                calls.append(block.get("input") or {})
    return calls


def lines_read_frac(calls: list[dict], path: Path) -> float:
    """Fraction of a file's lines covered by Read calls, from their offset and limit."""
    total = len(path.read_text(encoding="utf-8").splitlines())
    if total == 0:
        return 1.0
    seen: set[int] = set()
    for c in calls:
        if str(c.get("file_path", "")) != str(path):
            continue
        start = max(int(c.get("offset") or 1), 1)
        seen.update(range(start, min(start + int(c.get("limit") or READ_DEFAULT_LIMIT), total + 1)))
    return len(seen) / total


def coverage(transcript: Path, reader_dir: Path) -> dict[str, float]:
    docs, sidecars = read_fractions(transcript, reader_dir)
    calls = _read_calls(transcript)
    return {
        "docs": docs,
        "sidecars": sidecars,
        "runs": lines_read_frac(calls, reader_dir / "runs.jsonl"),
        "git": lines_read_frac(calls, reader_dir / "git_log.jsonl"),
    }


# ── readings ──────────────────────────────────────────────────────────────────


def reading_graph(reading: dict, view: _PacketView) -> tuple[dict[str, int], list[Edge]]:
    """Node efforts (distinct resolved run: and git: citations) and binding edges of one reading."""
    effort: dict[str, int] = {}
    named = [(p["id"], p) for p in reading.get("purposes", [])]
    named += [(f"open:{i}", ol) for i, ol in enumerate(reading.get("open_lines", []))]
    for node_id, node in named:
        keys = {
            key
            for res, key in (resolve_citation(c, view) for c in node.get("citations", []))
            if res == RESOLVED
        }
        effort[node_id] = sum(1 for k in keys if k.startswith(("run:", "git:")))
    return effort, binding_edges(reading.get("links", []))


def link_failures(reading: dict, view: _PacketView) -> int:
    """Links with no resolvable citation (spec AC-5b)."""
    bad = 0
    for lk in reading.get("links", []):
        if not any(resolve_citation(c, view)[0] == RESOLVED for c in lk.get("citations", [])):
            bad += 1
    return bad


def build_arm(name: str, out: Path) -> dict:
    a = ARMS[name]
    return build_packet(
        a["repo"], out=out, since=a["since"], until=a["until"], ref=a["ref"], project=name
    )


def load_valid(reading_path: Path) -> dict | None:
    try:
        return _load_reading(reading_path)
    except BearingsReadingInvalidError as e:
        logger.error("%s is invalid: %s", reading_path, e)
        return None


def list_bridges(work: Path) -> None:
    out: dict[str, list[dict]] = {}
    for name in ARMS:
        view = _PacketView(work / "packets" / name)
        for reader in READERS:
            path = work / "readings" / name / f"{reader}.json"
            reading = load_valid(path) if path.is_file() else None
            if reading is None:
                continue
            effort, edges = reading_graph(reading, view)
            links = reading.get("links", [])
            out[f"{name}/{reader}"] = [
                {"index": i, **links[i]} for i in bridges(list(effort), edges)
            ]
    (work / "bridges.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    logger.info("bridges to label: %d over %d readings", sum(map(len, out.values())), len(out))


def score(work: Path) -> dict:
    labels = json.loads((work / "bridge_labels.json").read_text(encoding="utf-8"))
    transcripts = json.loads((work / "reader_transcripts.json").read_text(encoding="utf-8"))
    contamination = hash_mismatches = audit_bad = rejected = n_readings = n_unlabelled = 0
    per_arm: dict[str, dict[str, list[float]]] = {
        n: {"groups": [], "lenient": [], "supp": [], "unc": [], "docs": [], "sidecars": [], "runs": [], "git": []}
        for n in ARMS
    }
    for name in ARMS:
        packet = work / "packets" / name
        sha = verify_packet(packet)
        with tempfile.TemporaryDirectory() as td:
            if build_arm(name, Path(td) / "rebuild")["packet_sha256"] != sha:
                hash_mismatches += 1
        hits = contamination_hits(packet / "reader") if name == "colliculix" else []
        contamination += len(hits)
        for h in hits:
            logger.error("contamination in %s: %s", name, h)
        view = _PacketView(packet)
        for reader in READERS:
            key = f"{name}/{reader}"
            path = work / "readings" / name / f"{reader}.json"
            if not path.is_file():
                continue
            n_readings += 1
            reading = load_valid(path)
            if reading is None:
                rejected += 1
                continue
            if reading["packet_sha256"] != sha:
                rejected += 1
            rejected += citation_failures(reading, packet) + link_failures(reading, view)
            transcript = transcripts.get(key, "")
            if transcript.startswith("inline:"):
                # Surface reader (owner 261105): the packet was inside its prompt, so it saw all of it. It had
                # no file to open, and its tool log cannot be verified by praxia; see the sidecar.
                cov = {"docs": 1.0, "sidecars": 1.0, "runs": 1.0, "git": 1.0}
            else:
                bad = audit_transcript(Path(transcript), [str(packet / "reader"), str(path)])
                audit_bad += len(bad)
                for b in bad:
                    logger.error("%s left its packet: %s", key, b)
                cov = coverage(Path(transcript), packet / "reader")
            for k, v in cov.items():
                per_arm[name][k].append(v)

            effort, edges = reading_graph(reading, view)
            found = bridges(list(effort), edges)
            given = {int(k): v for k, v in labels.get(key, {}).items() if v in LABELS}
            n_unlabelled += sum(1 for i in found if i not in given)
            strict = {i for i in found if given.get(i, "unsure") != "supported"}
            lenient = {i for i in found if given.get(i) == "unsupported"}
            nodes = reading.get("purposes", []) + reading.get("open_lines", [])
            arm = per_arm[name]
            arm["groups"].append(drift_groups(effort, edges, strict))
            arm["lenient"].append(drift_groups(effort, edges, lenient))
            arm["supp"].append(
                sum(given.get(i) == "supported" for i in found) / len(found) if found else 1.0
            )
            arm["unc"].append(
                sum(bool(n.get("uncertain")) for n in nodes) / len(nodes) if nodes else 0.0
            )

    med = lambda xs: statistics.median(xs) if xs else 0.0  # noqa: E731
    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731
    return {
        "controls_ok": ac7_controls_pass() and graph_controls_pass(),
        "contamination_hits": contamination,
        "packet_hash_mismatches": hash_mismatches,
        "reader_audit_violations": audit_bad,
        "rejected_readings": rejected,
        "n_readings": n_readings,
        "n_unlabelled": n_unlabelled,
        **{f"{n}_groups_median": med(a["groups"]) for n, a in per_arm.items()},
        **{f"{n}_groups_unsure_supported_median": med(a["lenient"]) for n, a in per_arm.items()},
        **{f"{n}_supported_bridge_frac": mean(a["supp"]) for n, a in per_arm.items()},
        **{f"{n}_uncertain_node_frac": mean(a["unc"]) for n, a in per_arm.items()},
        **{
            f"{n}_min_{k}_read_frac": min(a[k]) if a[k] else 0.0
            for n, a in per_arm.items()
            for k in ("docs", "sidecars", "runs", "git")
        },
    }


LABELLER_SEED = 261105
LABELLER_PROMPT = """You are checking links that readers drew between lines of work in a project's record. For each link \
below you are given the two lines of work as the reader described them, the type the reader gave the link, and the \
records the reader cited for the link, quoted from the record. Judge each link only from those quoted records.

Link types, as the readers were told them:
- feeds: a result of "from" is actually used by "to".
- serves: "from" was done for "to"'s sake, but no result has passed yet.
- shares: they use a common method, code or data, but neither uses the other's results.
- competes: the record shows a trade-off between them.

For each link, answer:
- supported: the quoted records show this relation, of this type, between these two lines.
- unsupported: the quoted records do not show it, or show a different relation.
- unsure: a careful reader could reasonably answer either way.

Write one JSON object to the file named in your task, keyed by reading id and then by link index, exactly as below:
{"R01": {"3": "supported", "7": "unsure"}, "R02": {...}}
Label every link listed, use only the three values above, and add nothing else.
"""


def _evidence(citation: str, packet: Path, view: _PacketView) -> str:
    """Quote what a resolved citation points at in the packet; unresolvable ones are said so."""
    res, key = resolve_citation(citation, view)
    if res != RESOLVED or key is None:
        return f"{citation}: (does not resolve in the record)"
    reader = packet / "reader"
    kind, _, rest = key.partition(":")
    if kind == "run":
        for line in (reader / "runs.jsonl").read_text(encoding="utf-8").split("\n"):
            if line and json.loads(line)["id"] == rest:
                return f"{citation}: run record {line}"
    if kind == "git":
        for line in (reader / "git_log.jsonl").read_text(encoding="utf-8").split("\n"):
            if line and json.loads(line)["sha"] == rest:
                return f"{citation}: commit {line}"
    if kind == "sidecar":
        path = rest.split("#", 1)[0]
        blob = next(s["blob_sha"] for s in view.index["sidecars"] if s["path"] == path)
        text = (reader / "sidecars" / blob / path).read_text(encoding="utf-8").splitlines()
        body = "\n".join(f"  {i}\t{t}" for i, t in enumerate(text[:150], 1))
        more = f"\n  ... ({len(text) - 150} more lines)" if len(text) > 150 else ""
        return f"{citation}: sidecar {path}\n{body}{more}"
    m = _DOC_RE.match(citation)
    if m:
        x, y = int(m["x"]), int(m["y"])
        text = (reader / "docs" / m["path"]).read_text(encoding="utf-8").splitlines()
        shown = list(range(x, min(y, x + 79) + 1))
        body = "\n".join(f"  {i}\t{text[i - 1]}" for i in shown)
        more = f"\n  ... (range continues to line {y})" if y > shown[-1] else ""
        return f"{citation}: {m['path']} lines {x}-{y}\n{body}{more}"
    return f"{citation}: (resolved, not quotable)"


def labeller_packet(work: Path) -> None:
    """Write labeller/prompt.md (opaque reading ids, links with quoted evidence) and labeller/key.json."""
    listed = json.loads((work / "bridges.json").read_text(encoding="utf-8"))
    keys = sorted(listed)
    random.Random(LABELLER_SEED).shuffle(keys)
    key_map = {f"R{i:02d}": k for i, k in enumerate(keys, 1)}
    parts = [LABELLER_PROMPT]
    n = 0
    for rid, key in key_map.items():
        arm, reader = key.split("/")
        packet = work / "packets" / arm
        view = _PacketView(packet)
        reading = json.loads((work / "readings" / arm / f"{reader}.json").read_text(encoding="utf-8"))
        nodes = {p["id"]: p["text"] for p in reading.get("purposes", [])}
        nodes |= {f"open:{i}": o["text"] for i, o in enumerate(reading.get("open_lines", []))}
        parts.append(f"\n## Reading {rid}\n")
        for b in listed[key]:
            n += 1
            ev = "\n".join(f"- {_evidence(c, packet, view)}" for c in b.get("citations", []))
            parts.append(
                f"\n### {rid} link {b['index']}: {b['type']}\n"
                f"from ({b['from']}): {nodes.get(b['from'], '(unknown line)')}\n"
                f"to ({b['to']}): {nodes.get(b['to'], '(unknown line)')}\n"
                f"Cited records:\n{ev}\n"
            )
    out = work / "labeller"
    out.mkdir(exist_ok=True)
    (out / "prompt.md").write_text("".join(parts), encoding="utf-8")
    (out / "key.json").write_text(json.dumps(key_map, indent=2) + "\n", encoding="utf-8")
    logger.info("labeller prompt: %d links over %d readings, %d bytes", n, len(key_map), len("".join(parts)))


def apply_labels(work: Path) -> None:
    """Check labeller/labels.json by counts and map it back to bridge_labels.json."""
    listed = json.loads((work / "bridges.json").read_text(encoding="utf-8"))
    key_map = json.loads((work / "labeller" / "key.json").read_text(encoding="utf-8"))
    raw = json.loads((work / "labeller" / "labels.json").read_text(encoding="utf-8"))
    out: dict[str, dict[str, str]] = {}
    missing = bad_value = extra = 0
    for rid, key in key_map.items():
        want = {str(b["index"]) for b in listed[key]}
        got = raw.get(rid, {})
        extra += len(set(got) - want)
        bad_value += sum(1 for v in got.values() if v not in LABELS)
        missing += len(want - set(got))
        out[key] = {i: got[i] for i in want if got.get(i) in LABELS}
    (work / "bridge_labels.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    total = sum(map(len, out.values()))
    logger.info("labels kept=%d missing=%d bad_value=%d extra=%d", total, missing, bad_value, extra)
    if missing or bad_value or extra:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--build-only", action="store_true", help="Build the packets for the readers and stop."
    )
    mode.add_argument(
        "--list-bridges", action="store_true", help="Write bridges.json for labelling and stop."
    )
    mode.add_argument(
        "--controls-only", action="store_true", help="Run the synthetic controls and stop."
    )
    mode.add_argument(
        "--labeller-packet", action="store_true", help="Write the labeller prompt and key, and stop."
    )
    mode.add_argument(
        "--apply-labels", action="store_true", help="Check the labeller's labels and write bridge_labels.json."
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    work: Path = args.work_dir.resolve()

    if args.controls_only:
        ok = ac7_controls_pass() and graph_controls_pass()
        logger.info("controls_ok=%s", ok)
        raise SystemExit(0 if ok else 1)
    if args.build_only:
        for name in ARMS:
            res = build_arm(name, work / "packets" / name)
            logger.info(
                "%s: %s runs=%s commits=%s docs=%s sidecars=%s excluded=%s",
                name, res["packet_sha256"][:12], res["n_runs"], res["n_git_commits"],
                res["n_docs"], res["n_sidecars"], res["n_excluded"],
            )  # fmt: skip
        return
    if args.list_bridges:
        list_bridges(work)
        return
    if args.labeller_packet:
        labeller_packet(work)
        return
    if args.apply_labels:
        apply_labels(work)
        return

    results = score(work)
    text = json.dumps(results, indent=2, sort_keys=True)
    logger.info(text)
    results_path = os.environ.get("BTH_RESULTS_PATH")
    if results_path:
        Path(results_path).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
