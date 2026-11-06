"""Bearings AC-7e (spec 261104): a within-project planted split. Does the instrument see two aims inside ONE project?

AC-7c and AC-7d spliced two different projects. AC-7e takes one project (tev_design) and splices two of its own aims
that the owner confirmed are separate: A, the lagoon simulation (sidecars whose path names "lagoon"), and B, the
design-library work (sidecars naming "librar", "proximity" or "diversity"). The halves share one project's runner,
tools and vocabulary, so the split is weaker than AC-7d's. There is no independent proof of "no results flow"
between the aims; the ground truth is the owner's own review of the project's aims (spec 261104).

Selection is mechanical, by sidecar path, because commit subjects and docs do not carry the aim. Commits and docs are
left out of every arm. A is far larger than B in the record, so A is capped by a deterministic rule (below). AC-7c's
strong splice is rerun unchanged as a positive anchor. Scoring is AC-7d's own, rebound to this study's arms.

Modes as in AC-7d: ``--build-only``, ``--controls-only``, ``--list-bridges``, ``--labeller-packet``,
``--apply-labels``; the tracked run (``bth run``) scores.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path

import replay_bearings_ac7b as ac7b
import replay_bearings_ac7c as ac7c
import replay_bearings_ac7d as ac7d
from replay_bearings_ac7 import HOME

from bathos.bearings.packet import build_packet, verify_packet

logger = logging.getLogger("replay_bearings_ac7e")

PROJECT = "tev_design"
SINCE, UNTIL = "2026-09-28T00:00:00-04:00", "2026-10-21T00:00:00-04:00"  # the library work's span, counts only
REF = "6c8bd22fff2dd78b67161a9c7ad5d2f048589cf8"  # last tev_design commit before UNTIL, resolved 261106 and frozen
SAMPLE_SEED = "261108"
N_B = 15  # every library run in the window (counted 261106: 15 runs, 9 sidecars)
N_A_HALF = N_B  # the splice takes as many lagoon runs as library runs
N_A_ALONE = 45  # fitted to the size rule, counts only (261106): 30 gave 37 items against the splice's 47

ARMS = {
    "p1": {"aim": "A", "n": N_A_ALONE},  # size control: lagoon alone
    "p2": {"anchor": True},  # AC-7c strong splice, rebuilt by AC-7c
    "p3": {"aim": "B", "n": N_B},  # library alone
    "p4": {"splice": (("A", N_A_HALF), ("B", N_B))},  # within-project splice
}
PLANTED, ANCHOR, SIZE_CONTROL = "p4", "p2", "p1"
SPLICES = (PLANTED, ANCHOR)

_ID_SUBS = [(re.compile(r"tev[_-]?design", re.I), ac7c.NEUTRAL)]
_LEAK_RES = [re.compile(r"tev[_-]?design", re.I)]


def scrub_ids(text: str) -> str:
    for pat, rep in _ID_SUBS:
        text = pat.sub(rep, text)
    return text


def aim_of(sidecar_path: str | None) -> str:
    p = (sidecar_path or "").lower()
    if "lagoon" in p:
        return "A"
    if "librar" in p or "proximity" in p or "diversity" in p:
        return "B"
    return "other"


def rank(rec: dict) -> str:
    return hashlib.sha256(f"{SAMPLE_SEED}:{rec['id']}".encode()).hexdigest()


def sub_packet(src: Path, out: Path, aim: str, n: int) -> int:
    """Write a source-style packet at ``out`` holding the ``n`` lowest-ranked runs of ``aim`` and their sidecars.

    No docs and no commits. Returns the number of runs kept.
    """
    reader = src / "reader"
    runs = [json.loads(x) for x in (reader / "runs.jsonl").read_text(encoding="utf-8").split("\n") if x]
    mine = sorted((r for r in runs if aim_of(r.get("sidecar_path")) == aim), key=rank)[:n]
    keep = sorted(mine, key=lambda r: (r["timestamp"], r["id"]))
    paths = {r["sidecar_path"] for r in keep if r.get("sidecar_path")}
    index = json.loads((reader / "index.json").read_text(encoding="utf-8"))
    side = [s for s in index["sidecars"] if s["path"] in paths]
    o = out / "reader"
    (o / "docs").mkdir(parents=True)
    for s in side:
        dst = o / "sidecars" / s["blob_sha"] / s["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes((reader / "sidecars" / s["blob_sha"] / s["path"]).read_bytes())
    (o / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in keep), encoding="utf-8")
    (o / "git_log.jsonl").write_text("", encoding="utf-8")
    index = {**index, "docs": [], "sidecars": side}
    (o / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    return len(keep)


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
    parts = arm.get("splice") or ((arm["aim"], arm["n"]),)
    with tempfile.TemporaryDirectory() as td:
        full = Path(td) / "full"
        build_packet(HOME / "projects" / PROJECT, out=full, since=SINCE, until=UNTIL, ref=REF, project=PROJECT)
        built, summaries = [], []
        for i, (aim, n) in enumerate(parts):
            d = Path(td) / f"s{i}"
            kept = sub_packet(full, d, aim, n)
            if kept != n:
                raise SystemExit(f"{name}: aim {aim} has {kept} runs, wanted {n}")
            built.append(d)
            summaries.append({"aim": aim, "runs": kept})
        sha = ac7c.merge(built, out, since=SINCE, until=UNTIL, refs=[REF] * len(built), scrub_fn=scrub_ids)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    manifest["sources"] = summaries
    (out / "manifest.json").write_bytes(ac7c._json_bytes(manifest))
    return {"packet_sha256": sha, **ac7c.size(out)}


def _fake_full(root: Path, specs: list[tuple[str, str]]) -> Path:
    """A synthetic built packet: specs are (run id, sidecar path)."""
    r = root / "reader"
    (r / "docs").mkdir(parents=True)
    runs, side = [], []
    for i, (rid, path) in enumerate(specs):
        runs.append({"id": rid, "timestamp": f"2026-10-{i + 1:02d}T00:00:00+00:00", "sidecar_path": path})
        if not path:
            continue
        (r / "sidecars" / f"b{i}" / path).parent.mkdir(parents=True, exist_ok=True)
        (r / "sidecars" / f"b{i}" / path).write_text(f"sidecar {i}\n")
        side.append({"path": path, "blob_sha": f"b{i}"})
    (r / "runs.jsonl").write_text("".join(json.dumps(x) + "\n" for x in runs))
    (r / "git_log.jsonl").write_text(json.dumps({"sha": "c1", "committed_at": "x", "subject": "s"}) + "\n")
    (r / "index.json").write_text(json.dumps({"docs": [{"path": "d.md", "blob_sha": "x"}], "sidecars": side}))
    return root


def split_controls_pass() -> bool:
    ok = aim_of("scripts/analysis/lagoon_jax_error_budget.bth.toml") == "A"
    ok &= aim_of("scripts/analysis/proximity_library_diversity_schemes.bth.toml") == "B"
    ok &= aim_of("scripts/analysis/library_variant_depth.bth.toml") == "B"
    ok &= aim_of("scripts/analysis/verify_spec_anchors.bth.toml") == "other"  # negative control
    ok &= aim_of(None) == "other" and aim_of("") == "other"
    ok &= scrub_ids("src/tev_design/x and tev-design and TEV_DESIGN") == "src/proj/x and proj and proj"
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        specs = [(f"r{i:02d}", f"s/{'lagoon' if i % 2 else 'library'}_{i}.bth.toml") for i in range(12)]
        specs += [("o1", "s/verify_x.bth.toml"), ("o2", None)]
        full = _fake_full(t / "full", [(a, b or "") for a, b in specs])
        n = sub_packet(full, t / "a", "A", 3)
        runs = [json.loads(x)["id"] for x in (t / "a/reader/runs.jsonl").read_text().split("\n") if x]
        idx = json.loads((t / "a/reader/index.json").read_text())
        ok &= n == 3 and len(runs) == 3 and all(aim_of(p["path"]) == "A" for p in idx["sidecars"])
        ok &= len(idx["sidecars"]) == 3 and idx["docs"] == []
        ok &= (t / "a/reader/git_log.jsonl").read_text() == ""  # commits dropped
        ok &= sub_packet(full, t / "z", "B", 0) == 0  # negative control: nothing asked, nothing kept
        # Deterministic and nested: the splice's half is a subset of the alone control's sample.
        sub_packet(full, t / "a6", "A", 6)
        r6 = {json.loads(x)["id"] for x in (t / "a6/reader/runs.jsonl").read_text().split("\n") if x}
        ok &= set(runs) <= r6
        # Negative control: the hash rank is not id order, so a sort-by-id shortcut would pick different runs.
        a_ids = sorted(r for r, p in specs if p and aim_of(p) == "A")
        ok &= set(a_ids[:3]) != set(runs)
        # A merge of two sub-packets verifies and leaks no identifier.
        sub_packet(full, t / "b", "B", 3)
        sha = ac7c.merge([t / "a", t / "b"], t / "m", since="x", until="y", refs=["1", "1"], scrub_fn=scrub_ids)
        ok &= verify_packet(t / "m") == sha and leak_hits(t / "m" / "reader") == []
    return bool(ok)


_ac7d_controls_ok, _ac7d_leak_hits = ac7d.controls_ok, ac7d.leak_hits


def controls_ok() -> bool:
    # AC-7d's own controls test AC-7d's own leak check, so give them back the original while they run.
    swapped = ac7d.leak_hits
    ac7d.leak_hits = _ac7d_leak_hits
    try:
        inherited = _ac7d_controls_ok()
    finally:
        ac7d.leak_hits = swapped
    return bool(inherited and split_controls_pass())


def use_this_study() -> None:
    ac7b.ARMS = ARMS
    ac7b.build_arm = build_arm
    ac7b.LABELLER_SEED = 261108
    # AC-7d's scorer, rebound to this study's arms, build, leak check and controls.
    ac7d.ARMS, ac7d.PLANTED, ac7d.ANCHOR, ac7d.SIZE_CONTROL, ac7d.SPLICES = ARMS, PLANTED, ANCHOR, SIZE_CONTROL, SPLICES
    ac7d.build_arm, ac7d.leak_hits, ac7d.controls_ok = build_arm, leak_hits, controls_ok


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
        logger.info("size_matched=%s anchor_hash_ok=%s", ac7d.size_matched(sizes),
                    sizes[ANCHOR]["packet_sha256"].startswith(ac7d.ANCHOR_SHA))  # fmt: skip
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

    results = ac7d.score(work)
    text = json.dumps(results, indent=2, sort_keys=True)
    logger.info(text)
    if path := os.environ.get("BTH_RESULTS_PATH"):
        Path(path).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
