"""Bearings AC-7d (spec 261104): a graded planted-drift control. Does the instrument see a split between neighbouring fields?

AC-7c showed the blind reader plus the drift-group scorer see a strong, cross-field split (isochore + alynxr). AC-7d
splices two projects in neighbouring fields that pass no results to each other (sweetprots + nanobody) and reruns
the AC-7c strong splice, with fresh readers, as a positive anchor. Arm ids are opaque (m1-m4); the key is in the
sidecar.

Neutralisation (owner 261106): identifiers only. Repo paths, project slugs, the shared data folder name and every
"sweetprots" become ``proj``; "nanobody" as a scientific word stays. The anchor arm is built by AC-7c's own code,
unchanged, and must hash to AC-7c's planted packet.

Modes as in AC-7c: ``--build-only``, ``--controls-only``, ``--list-bridges``, ``--labeller-packet``,
``--apply-labels``; the tracked run (``bth run``) scores.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import statistics
import tempfile
from pathlib import Path

import replay_bearings_ac7b as ac7b
import replay_bearings_ac7c as ac7c
from replay_bearings_ac7 import HOME, READERS, audit_transcript, citation_failures
from replay_bearings_ac7 import controls_pass as ac7_controls_pass

from bathos.bearings.packet import build_packet, verify_packet

logger = logging.getLogger("replay_bearings_ac7d")

NEIGHBOUR = ("2026-09-29T00:00:00-04:00", "2026-10-02T18:00:00-04:00")  # fitted to the size rule, counts only
FULL = ac7c.FULL  # 2026-09-21 to 2026-10-19
SOURCES = {  # last commit before each window's end, resolved 261106 and frozen
    ("sweetprots", NEIGHBOUR): "d7ceea8471190277f67243630e24e4ec789d6f4c",
    ("nanobody", NEIGHBOUR): "7decc6d650feba766de41ce91f177cd6203343ae",
    ("sweetprots", FULL): "d7ceea8471190277f67243630e24e4ec789d6f4c",
    ("nanobody", FULL): "7decc6d650feba766de41ce91f177cd6203343ae",
}
ANCHOR_SHA = "8170941186c0"  # prefix of AC-7c's planted packet (run 2032d774)
ARMS = {
    "m1": {"sources": [("nanobody", FULL)]},  # size control
    "m2": {"anchor": True},  # AC-7c strong splice, rebuilt by AC-7c
    "m3": {"sources": [("sweetprots", FULL)]},
    "m4": {"sources": [("sweetprots", NEIGHBOUR), ("nanobody", NEIGHBOUR)]},  # neighbour splice
}
PLANTED, ANCHOR, SIZE_CONTROL = "m4", "m2", "m1"
SPLICES = (PLANTED, ANCHOR)

# Identifier-only scrub. Order matters: specific forms first.
_ID_SUBS = [
    (re.compile(r"sweetprots", re.I), ac7c.NEUTRAL),
    (re.compile(r"nanobody_trajs", re.I), f"{ac7c.NEUTRAL}_trajs"),
    (re.compile(r"((?:project[-_]?slug|BTH_PROJECT_SLUG|--project)[\"' :=]+)nanobody\b", re.I), rf"\g<1>{ac7c.NEUTRAL}"),
    (re.compile(r"(project[:=])nanobody\b", re.I), rf"\g<1>{ac7c.NEUTRAL}"),
    (re.compile(r"(?<=/)nanobody\b", re.I), ac7c.NEUTRAL),  # any path segment, incl. projects/nanobody, repo URLs
]
_LEAK_RES = [
    re.compile(r"sweetprot", re.I),
    re.compile(r"nanobody_trajs", re.I),
    re.compile(r"/nanobody\b", re.I),
    re.compile(r"(?:project[-_]?slug|BTH_PROJECT_SLUG|--project)[\"' :=]+nanobody\b", re.I),
    re.compile(r"project[:=]nanobody\b", re.I),
]


def scrub_ids(text: str) -> str:
    for pat, rep in _ID_SUBS:
        text = pat.sub(rep, text)
    return text


def leak_hits(reader_dir: Path, *, anchor: bool = False) -> list[str]:
    if anchor:
        return ac7c.leak_hits(reader_dir)
    hits = []
    for f in sorted(p for p in reader_dir.rglob("*") if p.is_file()):
        rel = str(f.relative_to(reader_dir))
        text = f.read_bytes().decode("utf-8", errors="replace")
        hits.extend(f"{rel}: {p.pattern}" for p in _LEAK_RES if p.search(text) or p.search("/" + rel))
    return hits


def build_arm(name: str, out: Path) -> dict:
    arm = ARMS[name]
    if arm.get("anchor"):
        return ac7c.build_arm(ac7c.PLANTED, out)
    srcs = arm["sources"]
    with tempfile.TemporaryDirectory() as td:
        built, refs, summaries = [], [], []
        for i, (proj, (since, until)) in enumerate(srcs):
            ref = SOURCES[(proj, (since, until))]
            res = build_packet(HOME / "projects" / proj, out=Path(td) / f"s{i}", since=since, until=until,
                               ref=ref, project=proj)  # fmt: skip
            built.append(Path(td) / f"s{i}")
            refs.append(ref)
            summaries.append({"project": proj, "source_sha256": res["packet_sha256"]})
        since = min(w[0] for _, w in srcs)
        until = max(w[1] for _, w in srcs)
        sha = ac7c.merge(built, out, since=since, until=until, refs=refs, scrub_fn=scrub_ids)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    manifest["sources"] = summaries
    (out / "manifest.json").write_bytes(ac7c._json_bytes(manifest))
    return {"packet_sha256": sha, **ac7c.size(out)}


def size_matched(sizes: dict[str, dict]) -> bool:
    p, c = sizes[PLANTED], sizes[SIZE_CONTROL]
    return abs(p["items"] - c["items"]) <= ac7c.SIZE_TOLERANCE * c["items"] and p["bytes"] <= c["bytes"]


def cross_seam(edges, found, given, node_keys, members) -> tuple[int, int, int, bool]:
    """Descriptive: binding links whose two ends have different dominant sources.

    Returns (all cross-seam binding links, of those how many are bridges, of those bridges how many labelled
    supported, whether any cross-seam binding link survives the strict removal and so joins the halves).
    """
    src = {}
    for n, keys in node_keys.items():
        counts: dict[int, int] = {}
        for k in keys:
            if k in members:
                counts[members[k]] = counts.get(members[k], 0) + 1
        src[n] = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0] if counts else None
    cross = [i for i, a, b in edges if src.get(a) is not None and src.get(b) is not None and src[a] != src[b]]
    strict = {i for i in found if given.get(i, "unsure") != "supported"}
    bridged = [i for i in cross if i in found]
    return len(cross), len(bridged), sum(given.get(i) == "supported" for i in bridged), any(i not in strict for i in cross)


def scrub_controls_pass() -> bool:
    ok = scrub_ids("nanobody binding to 2P49 in sweetprots") == "nanobody binding to 2P49 in proj"
    ok &= scrub_ids("~/projects/nanobody/data and nanobody_trajs/2P49") == "~/projects/proj/data and proj_trajs/2P49"
    ok &= scrub_ids('--project-slug nanobody; "project_slug": "nanobody"; project:nanobody') == (
        '--project-slug proj; "project_slug": "proj"; project:proj'
    )
    ok &= scrub_ids("github.com/maraxen/nanobody.git") == "github.com/maraxen/proj.git"
    ok &= scrub_ids("nanobodies bind; a nanobody-target system") == "nanobodies bind; a nanobody-target system"
    with tempfile.TemporaryDirectory() as td:
        r = Path(td) / "reader"
        r.mkdir()
        (r / "a.txt").write_text("see ~/projects/nanobody/x and the sweetprots recipe\n")
        ok &= len(leak_hits(r)) >= 2  # negative control: fires on unscrubbed identifiers
        (r / "a.txt").write_text(scrub_ids((r / "a.txt").read_text()) + "nanobodies bind antigens\n")
        ok &= leak_hits(r) == []  # the scientific word is allowed
    # Cross-seam counting: two sources, one parallel pair of cross links (not bridges) and one cross bridge.
    eff = {"a": 2, "b": 2, "c": 2}
    keys = {"a": {"git:1"}, "b": {"git:2"}, "c": {"git:3"}}
    members = {"git:1": 0, "git:2": 1, "git:3": 1}
    edges = [(0, "a", "b"), (1, "a", "b"), (2, "b", "c")]
    found = ac7b.bridges(list(eff), edges)
    ok &= cross_seam(edges, found, {}, keys, members) == (2, 0, 0, True)  # parallel links: no bridge, joined
    edges2 = [(0, "a", "b"), (2, "b", "c")]
    found2 = ac7b.bridges(list(eff), edges2)
    ok &= cross_seam(edges2, found2, {0: "unsupported"}, keys, members) == (1, 1, 0, False)
    ok &= cross_seam(edges2, found2, {0: "supported"}, keys, members) == (1, 1, 1, True)
    return bool(ok)


def controls_ok() -> bool:
    return (ac7_controls_pass() and ac7b.graph_controls_pass() and ac7c.merge_controls_pass()
            and ac7c.seam_controls_pass() and scrub_controls_pass())  # fmt: skip


def score(work: Path) -> dict:
    labels = json.loads((work / "bridge_labels.json").read_text(encoding="utf-8"))
    transcripts = json.loads((work / "reader_transcripts.json").read_text(encoding="utf-8"))
    leaks = hash_mismatches = audit_bad = rejected = n_readings = n_unlabelled = 0
    per_arm = {n: {"groups": [], "lenient": [], "none": [], "supp": []} for n in ARMS}
    seam_stats = {n: {"follows": 0, "purity": [], "cross": 0, "cross_bridges": 0, "cross_supp": 0, "joined": 0}
                  for n in SPLICES}  # fmt: skip
    sizes = {}
    anchor_hash_ok = False
    for name in ARMS:
        packet = work / "packets" / name
        sha = verify_packet(packet)
        sizes[name] = ac7c.size(packet)
        if name == ANCHOR:
            anchor_hash_ok = sha.startswith(ANCHOR_SHA)
        with tempfile.TemporaryDirectory() as td:
            if build_arm(name, Path(td) / "rebuild")["packet_sha256"] != sha:
                hash_mismatches += 1
        hits = leak_hits(packet / "reader", anchor=name == ANCHOR)
        leaks += len(hits)
        for h in hits:
            logger.error("leak in %s: %s", name, h)
        view = ac7b._PacketView(packet)
        members = json.loads((packet / "manifest.json").read_text(encoding="utf-8")).get("members", {})
        for reader in READERS:
            key = f"{name}/{reader}"
            path = work / "readings" / name / f"{reader}.json"
            if not path.is_file():
                continue
            n_readings += 1
            reading = ac7b.load_valid(path)
            if reading is None:
                rejected += 1
                continue
            if reading["packet_sha256"] != sha:
                rejected += 1
            rejected += citation_failures(reading, packet) + ac7b.link_failures(reading, view)
            transcript = transcripts.get(key, "")
            if not transcript.startswith("inline:"):
                audit_bad += len(audit_transcript(Path(transcript), [str(packet / "reader"), str(path)]))
            effort, edges = ac7b.reading_graph(reading, view)
            found = ac7b.bridges(list(effort), edges)
            given = {int(k): v for k, v in labels.get(key, {}).items() if v in ac7b.LABELS}
            n_unlabelled += sum(1 for i in found if i not in given)
            strict = {i for i in found if given.get(i, "unsure") != "supported"}
            lenient = {i for i in found if given.get(i) == "unsupported"}
            arm = per_arm[name]
            arm["groups"].append(ac7b.drift_groups(effort, edges, strict))
            arm["lenient"].append(ac7b.drift_groups(effort, edges, lenient))
            arm["none"].append(ac7b.drift_groups(effort, edges, set()))
            arm["supp"].append(sum(given.get(i) == "supported" for i in found) / len(found) if found else 1.0)
            if name in SPLICES:
                nk = ac7c.node_keys(reading, view)
                follows, purity = ac7c.seam(effort, edges, strict, nk, members)
                cross, cb, cs, joined = cross_seam(edges, found, given, nk, members)
                st = seam_stats[name]
                st["follows"] += follows
                st["purity"].append(purity)
                st["cross"] += cross
                st["cross_bridges"] += cb
                st["cross_supp"] += cs
                st["joined"] += joined

    med = lambda xs: statistics.median(xs) if xs else 0.0  # noqa: E731
    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731
    out = {
        "controls_ok": controls_ok(),
        "leak_hits": leaks,
        "packet_hash_mismatches": hash_mismatches,
        "anchor_hash_ok": anchor_hash_ok,
        "reader_audit_violations": audit_bad,
        "rejected_readings": rejected,
        "n_readings": n_readings,
        "n_unlabelled": n_unlabelled,
        "size_matched": size_matched(sizes),
        **{f"{n}_items": s["items"] for n, s in sizes.items()},
        **{f"{n}_bytes": s["bytes"] for n, s in sizes.items()},
        **{f"{n}_groups_median": med(a["groups"]) for n, a in per_arm.items()},
        **{f"{n}_groups_min": min(a["groups"]) if a["groups"] else 0 for n, a in per_arm.items()},
        **{f"{n}_groups_unsure_supported_median": med(a["lenient"]) for n, a in per_arm.items()},
        **{f"{n}_groups_no_labels_median": med(a["none"]) for n, a in per_arm.items()},
        **{f"{n}_supported_bridge_frac": mean(a["supp"]) for n, a in per_arm.items()},
    }
    for n, st in seam_stats.items():
        out |= {
            f"{n}_seam_readings": st["follows"],
            f"{n}_seam_purity_mean": mean(st["purity"]),
            f"{n}_cross_seam_links": st["cross"],
            f"{n}_cross_seam_bridges": st["cross_bridges"],
            f"{n}_cross_seam_bridges_supported": st["cross_supp"],
            f"{n}_halves_joined_readings": st["joined"],
        }
    return out


def use_this_study() -> None:
    ac7b.ARMS = ARMS
    ac7b.build_arm = build_arm
    ac7b.LABELLER_SEED = 261107


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    for flag in ("--build-only", "--list-bridges", "--controls-only", "--labeller-packet", "--apply-labels"):
        mode.add_argument(flag, action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    work: Path = args.work_dir.resolve()
    use_this_study()

    if args.controls_only:
        ok = controls_ok()
        logger.info("controls_ok=%s", ok)
        raise SystemExit(0 if ok else 1)
    if args.build_only:
        sizes = {}
        for name in ARMS:
            res = build_arm(name, work / "packets" / name)
            sizes[name] = res
            logger.info("%s: %s %s leaks=%d", name, res["packet_sha256"][:12],
                        {k: v for k, v in res.items() if k != "packet_sha256"},
                        len(leak_hits(work / "packets" / name / "reader", anchor=name == ANCHOR)))  # fmt: skip
        logger.info("size_matched=%s anchor_hash_ok=%s", size_matched(sizes),
                    sizes[ANCHOR]["packet_sha256"].startswith(ANCHOR_SHA))  # fmt: skip
        return
    if args.list_bridges:
        ac7b.list_bridges(work)
        return
    if args.labeller_packet:
        ac7b.labeller_packet(work)
        return
    if args.apply_labels:
        ac7b.apply_labels(work)
        return

    results = score(work)
    text = json.dumps(results, indent=2, sort_keys=True)
    logger.info(text)
    if path := os.environ.get("BTH_RESULTS_PATH"):
        Path(path).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
