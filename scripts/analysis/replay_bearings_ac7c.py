"""Bearings AC-7c (spec 261104): a planted-drift control. Can the blind reader plus the drift-group scorer see drift at all?

The planted arm splices the records of two projects that pass no results to each other (isochore and alynxr) into
one packet over the same 14 days. Each control is one of those projects alone over 28 days. Every arm gets the same
neutralisation: one merged tree with no source prefix, and every occurrence of either project's name replaced by
``proj``. Arm ids are opaque (k1, k2, k3), because a reader can see its folder name.

Modes, as in AC-7b (``replay_bearings_ac7b.py``), whose graph scorer, bridge lister, labeller packet and label
check are reused unchanged:

* ``--build-only`` builds the three packets and reports hashes and counts only.
* ``--list-bridges``, ``--labeller-packet`` and ``--apply-labels`` work as in AC-7b.
* ``--controls-only`` runs AC-7b's synthetic controls plus this script's merge controls.
* The tracked run (``bth run``) rebuilds each packet through this merge to confirm its hash, checks every packet
  for leaked project names, checks the registered size match, and scores the readings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import statistics
import tempfile
from collections.abc import Callable
from pathlib import Path

import replay_bearings_ac7b as ac7b
from replay_bearings_ac7 import HOME, READERS, audit_transcript, citation_failures
from replay_bearings_ac7 import controls_pass as ac7_controls_pass

from bathos.bearings.packet import (
    _json_bytes,
    _packet_hash,
    build_packet,
    canonical_json,
    parse_ts,
    verify_packet,
)

logger = logging.getLogger("replay_bearings_ac7c")

HALF = ("2026-09-28T00:00:00-04:00", "2026-10-12T00:00:00-04:00")
FULL = ("2026-09-21T00:00:00-04:00", "2026-10-19T00:00:00-04:00")
SOURCES = {
    # The ref is the last commit before each window's end (resolved 261106 and frozen here).
    ("isochore", HALF): "d637a6d0da7f0d85854613b20384d9220da60ad9",
    ("alynxr", HALF): "c1114913fdddf75debb79c4b2712f2530b42aad1",
    ("isochore", FULL): "25df96788ab20656c67fa7f675cd79cfb8ecde7b",
    ("alynxr", FULL): "f919e87269497d60804285a635c81e556b281e01",
}
# Opaque arm ids; the key is in the sidecar. k2 is the planted arm.
ARMS = {
    "k1": {"sources": [("isochore", FULL)]},
    "k2": {"sources": [("isochore", HALF), ("alynxr", HALF)]},
    "k3": {"sources": [("alynxr", FULL)]},
}
PLANTED, SIZE_CONTROL = "k2", "k3"
SIZE_TOLERANCE = 0.15  # judgment cutoff, sidecar: planted items within 15% of the size control's

NEUTRAL = "proj"
SCRUB_RE = re.compile(r"isochore|alynxr", re.IGNORECASE)
LEAK_NEEDLES = ("isochore", "isochor", "alynxr", "alynx")  # lowercase; stems catch partial scrubs


def scrub(text: str) -> str:
    return SCRUB_RE.sub(NEUTRAL, text)


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 -- git's object id, not security


def _scrub_value(v: object, fn: Callable[[str], str] = scrub) -> object:
    if isinstance(v, str):
        return fn(v)
    if isinstance(v, list):
        return [_scrub_value(x, fn) for x in v]
    if isinstance(v, dict):
        return {k: _scrub_value(x, fn) for k, x in v.items()}
    return v


def merge(
    sources: list[Path], out: Path, *, since: str, until: str, refs: list[str], scrub_fn: Callable[[str], str] = scrub
) -> str:
    """Merge built source packets into one neutral packet at ``out``; return its hash. Refuses path collisions.

    ``scrub_fn`` neutralises every path and text; it defaults to AC-7c's scrub (AC-7d passes its own).
    """
    docs: dict[str, bytes] = {}
    sidecars: dict[tuple[str, str], bytes] = {}
    side_owner: dict[str, tuple[int, str]] = {}  # new sidecar path -> (source index, old path)
    runs: list[dict] = []
    commits: list[dict] = []
    members: dict[str, int] = {}  # "run:<id>" / "git:<sha>" -> source index; manifest only, never in reader/
    for i, src in enumerate(sources):
        reader = src / "reader"
        index = json.loads((reader / "index.json").read_text(encoding="utf-8"))
        new_sha: dict[tuple[str, str], str] = {}  # (old path, sha256 of old bytes) -> sha256 of scrubbed bytes
        for d in index["docs"]:
            path = scrub_fn(d["path"])
            if path in docs:
                raise ValueError(f"doc path collision after merge: {path}")
            docs[path] = scrub_fn((reader / "docs" / d["path"]).read_text(encoding="utf-8")).encode("utf-8")
        for s in index["sidecars"]:
            path = scrub_fn(s["path"])
            # One source may hold several blobs of one path; two sources, or two paths, may not share one.
            if side_owner.setdefault(path, (i, s["path"])) != (i, s["path"]):
                raise ValueError(f"sidecar path collision after merge: {path}")
            old = (reader / "sidecars" / s["blob_sha"] / s["path"]).read_bytes()
            data = scrub_fn(old.decode("utf-8")).encode("utf-8")
            sidecars[(path, git_blob_sha(data))] = data
            new_sha[(s["path"], hashlib.sha256(old).hexdigest())] = hashlib.sha256(data).hexdigest()
        for line in (reader / "runs.jsonl").read_text(encoding="utf-8").split("\n"):
            if not line:
                continue
            rec = json.loads(line)
            key = (rec.get("sidecar_path"), rec.get("sidecar_sha256"))
            rec = _scrub_value(rec, scrub_fn)
            if key in new_sha:  # keep the run's hash consistent with the scrubbed sidecar it names
                rec["sidecar_sha256"] = new_sha[key]
            runs.append(rec)
            members[f"run:{rec['id']}"] = i
        for line in (reader / "git_log.jsonl").read_text(encoding="utf-8").split("\n"):
            if line:
                commits.append(_scrub_value(json.loads(line), scrub_fn))
                members[f"git:{commits[-1]['sha']}"] = i
    if len({r["id"] for r in runs}) != len(runs) or len({c["sha"] for c in commits}) != len(commits):
        raise ValueError("run id or commit sha collision after merge")

    reader = out / "reader"
    reader.mkdir(parents=True)
    doc_index = []
    for path in sorted(docs):
        (reader / "docs" / path).parent.mkdir(parents=True, exist_ok=True)
        (reader / "docs" / path).write_bytes(docs[path])
        doc_index.append({"path": path, "blob_sha": git_blob_sha(docs[path])})
    for (path, blob), data in sorted(sidecars.items()):
        (reader / "sidecars" / blob / path).parent.mkdir(parents=True, exist_ok=True)
        (reader / "sidecars" / blob / path).write_bytes(data)
    index = {
        "project": NEUTRAL,
        # One opaque value per arm, so a spliced packet shows one ref like any other.
        "ref_sha": hashlib.sha1("+".join(refs).encode()).hexdigest(),  # noqa: S324
        "since": since,
        "until": until,
        "docs": doc_index,
        "sidecars": [{"path": p, "blob_sha": b} for p, b in sorted(sidecars)],
    }
    (reader / "index.json").write_bytes(_json_bytes(index))
    runs.sort(key=lambda r: (parse_ts(str(r["timestamp"])), str(r["id"])))
    commits.sort(key=lambda c: (parse_ts(c["committed_at"]), c["sha"]))
    (reader / "runs.jsonl").write_bytes(b"".join(_json_bytes(r) for r in runs))
    (reader / "git_log.jsonl").write_bytes(b"".join(_json_bytes(c) for c in commits))
    sha = _packet_hash(reader)
    manifest = {"packet_sha256": sha, "since": since, "until": until, "refs": refs, "neutral_name": NEUTRAL,
                "members": dict(sorted(members.items()))}  # fmt: skip
    (out / "manifest.json").write_bytes(_json_bytes(manifest))
    return sha


def build_arm(name: str, out: Path) -> dict:
    """Build each source packet with bathos's builder, then merge and neutralise. Deterministic."""
    srcs = ARMS[name]["sources"]
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
        sha = merge(built, out, since=since, until=until, refs=refs)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    manifest["sources"] = summaries  # never shown to a reader: the reader sees only reader/
    (out / "manifest.json").write_bytes(_json_bytes(manifest))
    return {"packet_sha256": sha, **size(out)}


def size(packet: Path) -> dict:
    reader = packet / "reader"
    index = json.loads((reader / "index.json").read_text(encoding="utf-8"))
    n_runs = sum(1 for line in (reader / "runs.jsonl").read_text(encoding="utf-8").split("\n") if line)
    n_commits = sum(1 for line in (reader / "git_log.jsonl").read_text(encoding="utf-8").split("\n") if line)
    return {
        "n_docs": len(index["docs"]),
        "n_sidecars": len(index["sidecars"]),
        "n_runs": n_runs,
        "n_commits": n_commits,
        "items": len(index["docs"]) + len(index["sidecars"]) + n_runs + n_commits,
        "bytes": sum(f.stat().st_size for f in reader.rglob("*") if f.is_file()),
    }


def leak_hits(reader_dir: Path) -> list[str]:
    """Every file path or file content under reader/ that still names either project."""
    hits = []
    for f in sorted(p for p in reader_dir.rglob("*") if p.is_file()):
        rel = str(f.relative_to(reader_dir)).lower()
        text = f.read_bytes().decode("utf-8", errors="replace").lower()
        hits.extend(f"{rel}: {t}" for t in LEAK_NEEDLES if t in text or t in rel)
    return hits


def seam(effort: dict[str, int], edges: list, removed: set[int], node_keys: dict[str, set[str]],
         members: dict[str, int]) -> tuple[bool, float]:  # fmt: skip
    """Descriptive, planted arm only: do the counted groups follow the real seam between the two sources?

    Each counted group (at least MIN_GROUP_SHARE of effort) is assigned the source holding most of its run:
    and git: citations. Returns (two or more groups with different dominant sources, mean dominant share).
    """
    total = sum(effort.values())
    kept = [e for e in edges if e[0] not in removed]
    dominants, purities = set(), []
    for g in ac7b._components(list(effort), kept):
        if total == 0 or sum(effort[n] for n in g) / total < ac7b.MIN_GROUP_SHARE:
            continue
        counts: dict[int, int] = {}
        for n in g:
            for k in node_keys.get(n, ()):
                if k in members:
                    counts[members[k]] = counts.get(members[k], 0) + 1
        if counts:
            src, top = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))
            dominants.add(src)
            purities.append(top / sum(counts.values()))
    return len(dominants) >= 2, (sum(purities) / len(purities) if purities else 0.0)


def size_matched(sizes: dict[str, dict]) -> bool:
    p, c = sizes[PLANTED], sizes[SIZE_CONTROL]
    return abs(p["items"] - c["items"]) <= SIZE_TOLERANCE * c["items"] and p["bytes"] <= c["bytes"]


# ── merge controls: synthetic ground truth for the splice ─────────────────────


def _fake_source(root: Path, *, docs: dict[str, str], runs: list[dict], commits: list[dict]) -> Path:
    reader = root / "reader"
    for path, text in docs.items():
        (reader / "docs" / path).parent.mkdir(parents=True, exist_ok=True)
        (reader / "docs" / path).write_text(text, encoding="utf-8")
    index = {"docs": [{"path": p, "blob_sha": git_blob_sha(t.encode())} for p, t in docs.items()], "sidecars": []}
    reader.mkdir(parents=True, exist_ok=True)
    (reader / "index.json").write_text(canonical_json(index), encoding="utf-8")
    (reader / "runs.jsonl").write_text("".join(canonical_json(r) + "\n" for r in runs), encoding="utf-8")
    (reader / "git_log.jsonl").write_text("".join(canonical_json(c) + "\n" for c in commits), encoding="utf-8")
    return root


def merge_controls_pass() -> bool:
    ok = scrub("Use Isochore.traj and ALYNXR_ROOT in src/alynxr/") == "Use proj.traj and proj_ROOT in src/proj/"
    ok &= git_blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"  # `git hash-object` of "hello\n"
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        c = lambda sha, ts, s: {"sha": sha, "committed_at": ts, "subject": s}  # noqa: E731
        r = lambda rid, ts: {"id": rid, "timestamp": ts, "command": "python -m isochore.x", "sidecar_path": None}  # noqa: E731
        # Lexical order of ids and shas contradicts time order, so only a time sort passes; source order fails on runs.
        a = _fake_source(t / "a", docs={"specs/x.md": "isochore spec\n"}, runs=[r("r1", "2026-10-02T00:00:00+00:00")],
                         commits=[c("bb", "2026-10-01T00:00:00+00:00", "feat(isochore): a")])  # fmt: skip
        b = _fake_source(t / "b", docs={"specs/y.md": "alynxr spec\n"}, runs=[r("r2", "2026-10-01T12:00:00+00:00")],
                         commits=[c("aa", "2026-10-01T06:00:00+00:00", "feat(alynxr): b")])  # fmt: skip
        # Negative control: the leak check fires on an unscrubbed source.
        ok &= len(leak_hits(a / "reader")) > 0
        sha = merge([a, b], t / "m", since="x", until="y", refs=["1", "2"])
        ok &= verify_packet(t / "m") == sha
        ok &= leak_hits(t / "m" / "reader") == []
        log = [json.loads(x)["sha"] for x in (t / "m/reader/git_log.jsonl").read_text().split("\n") if x]
        runs = [json.loads(x)["id"] for x in (t / "m/reader/runs.jsonl").read_text().split("\n") if x]
        ok &= log == ["bb", "aa"] and runs == ["r2", "r1"]  # interleaved by time, not by source or id
        idx = json.loads((t / "m/reader/index.json").read_text())
        ok &= [d["path"] for d in idx["docs"]] == ["specs/x.md", "specs/y.md"] and idx["project"] == NEUTRAL
        ok &= idx["docs"][0]["blob_sha"] == git_blob_sha(b"proj spec\n")  # blob recomputed after the scrub
        # A path both sources use is refused, not overwritten.
        d = _fake_source(t / "d", docs={"specs/x.md": "other\n"}, runs=[], commits=[])
        try:
            merge([a, d], t / "m2", since="x", until="y", refs=["1", "3"])
            ok = False
        except ValueError:
            pass
        # Names that collide only after the scrub are refused too.
        e = _fake_source(t / "e", docs={"src/alynxr/z.md": "1\n"}, runs=[], commits=[])
        f = _fake_source(t / "f", docs={"src/isochore/z.md": "2\n"}, runs=[], commits=[])
        try:
            merge([e, f], t / "m3", since="x", until="y", refs=["5", "6"])
            ok = False
        except ValueError:
            pass
    return bool(ok)


def seam_controls_pass() -> bool:
    eff = {"a": 2, "b": 2, "c": 2, "d": 2}
    keys = {"a": {"git:1", "git:2"}, "b": {"run:3", "git:4"}, "c": {"git:5", "run:6"}, "d": {"git:7", "git:8"}}
    members = {"git:1": 0, "git:2": 0, "run:3": 0, "git:4": 0, "git:5": 1, "run:6": 1, "git:7": 1, "git:8": 1}
    split = [(0, "a", "b"), (1, "c", "d")]
    ok = seam(eff, split, set(), keys, members) == (True, 1.0)  # two groups, one per source
    ok &= seam(eff, [*split, (2, "b", "c")], set(), keys, members) == (False, 0.5)  # one group, mixed
    ok &= seam(eff, [(0, "a", "c"), (1, "b", "d")], set(), keys, members) == (False, 0.5)  # two groups, each mixed
    return bool(ok)


def node_keys(reading: dict, view: ac7b._PacketView) -> dict[str, set[str]]:
    """Resolved run: and git: citation keys per node, as reading_graph counts them for effort."""
    named = [(p["id"], p) for p in reading.get("purposes", [])]
    named += [(f"open:{i}", ol) for i, ol in enumerate(reading.get("open_lines", []))]
    out = {}
    for node_id, node in named:
        res = (ac7b.resolve_citation(c, view) for c in node.get("citations", []))
        out[node_id] = {k for r, k in res if r == ac7b.RESOLVED and k and k.startswith(("run:", "git:"))}
    return out


def controls_ok() -> bool:
    return ac7_controls_pass() and ac7b.graph_controls_pass() and merge_controls_pass() and seam_controls_pass()


# ── scoring: AC-7b's score() with this study's rebuild, leak and size checks ──


def score(work: Path) -> dict:
    labels = json.loads((work / "bridge_labels.json").read_text(encoding="utf-8"))
    transcripts = json.loads((work / "reader_transcripts.json").read_text(encoding="utf-8"))
    leaks = hash_mismatches = audit_bad = rejected = n_readings = n_unlabelled = 0
    per_arm: dict[str, dict[str, list[float]]] = {n: {"groups": [], "lenient": [], "supp": [], "unc": []} for n in ARMS}
    sizes = {}
    seam_follows, seam_purity = 0, []
    for name in ARMS:
        packet = work / "packets" / name
        sha = verify_packet(packet)
        sizes[name] = size(packet)
        with tempfile.TemporaryDirectory() as td:
            if build_arm(name, Path(td) / "rebuild")["packet_sha256"] != sha:
                hash_mismatches += 1
        hits = leak_hits(packet / "reader")
        leaks += len(hits)
        for h in hits:
            logger.error("leak in %s: %s", name, h)
        view = ac7b._PacketView(packet)
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
                bad = audit_transcript(Path(transcript), [str(packet / "reader"), str(path)])
                audit_bad += len(bad)
            effort, edges = ac7b.reading_graph(reading, view)
            found = ac7b.bridges(list(effort), edges)
            given = {int(k): v for k, v in labels.get(key, {}).items() if v in ac7b.LABELS}
            n_unlabelled += sum(1 for i in found if i not in given)
            strict = {i for i in found if given.get(i, "unsure") != "supported"}
            lenient = {i for i in found if given.get(i) == "unsupported"}
            nodes = reading.get("purposes", []) + reading.get("open_lines", [])
            arm = per_arm[name]
            arm["groups"].append(ac7b.drift_groups(effort, edges, strict))
            arm["lenient"].append(ac7b.drift_groups(effort, edges, lenient))
            arm["supp"].append(sum(given.get(i) == "supported" for i in found) / len(found) if found else 1.0)
            arm["unc"].append(sum(bool(n.get("uncertain")) for n in nodes) / len(nodes) if nodes else 0.0)
            if name == PLANTED:
                members = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))["members"]
                follows, purity = seam(effort, edges, strict, node_keys(reading, view), members)
                seam_follows += follows
                seam_purity.append(purity)

    med = lambda xs: statistics.median(xs) if xs else 0.0  # noqa: E731
    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731
    return {
        "controls_ok": controls_ok(),
        "leak_hits": leaks,
        "packet_hash_mismatches": hash_mismatches,
        "reader_audit_violations": audit_bad,
        "rejected_readings": rejected,
        "n_readings": n_readings,
        "n_unlabelled": n_unlabelled,
        "size_matched": size_matched(sizes),
        f"{PLANTED}_seam_readings": seam_follows,
        f"{PLANTED}_seam_purity_mean": mean(seam_purity),
        **{f"{n}_items": s["items"] for n, s in sizes.items()},
        **{f"{n}_bytes": s["bytes"] for n, s in sizes.items()},
        **{f"{n}_groups_median": med(a["groups"]) for n, a in per_arm.items()},
        **{f"{n}_groups_min": min(a["groups"]) if a["groups"] else 0 for n, a in per_arm.items()},
        **{f"{n}_groups_unsure_supported_median": med(a["lenient"]) for n, a in per_arm.items()},
        **{f"{n}_supported_bridge_frac": mean(a["supp"]) for n, a in per_arm.items()},
        **{f"{n}_uncertain_node_frac": mean(a["unc"]) for n, a in per_arm.items()},
    }


def use_this_study() -> None:
    """Point AC-7b's reused modes (bridges, labeller) at this study's arms and its own labeller seed."""
    ac7b.ARMS = ARMS
    ac7b.build_arm = build_arm
    ac7b.LABELLER_SEED = 261106


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
                        len(leak_hits(work / "packets" / name / "reader")))  # fmt: skip
        logger.info("size_matched=%s", size_matched(sizes))
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
    results_path = os.environ.get("BTH_RESULTS_PATH")
    if results_path:
        Path(results_path).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
