"""AC-9 bookkeeping from sidecar purpose labels (spec 261104 T7, owner 261109). Owner-side only: nothing here may
run inside a reader's view.

The truth AC-9 scores against is the ``[charter] purpose`` label each sidecar carries (see ``purpose_label``). The
packet builder strips it, so this module reads it back from the repo by the blob shas the packet lists:

- ``packet_labels``: sidecar path -> purpose slugs, for the sidecars in a packet.
- ``answer_key``: which charter purposes the packet's work serves (present / silent, revived / absent retired),
  which sidecars are attested off-charter, and whether the packet dropped content for budget.
- ``reading_gold``: for each reading purpose, the labels of the runs and sidecars it cites.
- ``ac9_metrics``: the five quantities T7 sets thresholds on, for one comparison.
- ``degenerate_mappings``: mappings that know nothing; they must not meet ``pass``.

The thresholds in ``meets_pass`` are the owner's T7 values read per comparison (a revived purpose must be flagged in
at least 2 of 3 readings, here as a rate). The AC-9 sidecar decides how comparisons pool.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from bathos.bearings.purpose_label import read_purpose_label
from bathos.bearings.reading import RESOLVED, _PacketView, resolve_citation

OFF_CHARTER = "off-charter"

PASS = {
    "silent_recall": 0.8,  # at least
    "false_silent": 0.2,  # at most
    "off_charter_unmapped": 0.8,  # at least
    "revived_flagged": 2 / 3,  # at least
    "gold_agreement": 0.8,  # at least
}


class Ac9LabelError(ValueError):
    """A label names no charter purpose, or a sidecar in the packet cannot be read back from the repo."""


def _blob_text(repo: Path, blob: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", blob], capture_output=True, check=False
    )
    if proc.returncode != 0:
        raise Ac9LabelError(f"sidecar blob {blob[:12]} is not in {repo}")
    return proc.stdout.decode("utf-8")


def packet_labels(repo: Path | str, packet_dir: Path | str) -> dict[str, list[str]]:
    """Sidecar path -> sorted purpose slugs (union over the path's blobs). Unlabelled sidecars are absent."""
    index = json.loads((Path(packet_dir) / "reader" / "index.json").read_text(encoding="utf-8"))
    labels: dict[str, set[str]] = {}
    for entry in index["sidecars"]:
        slugs = read_purpose_label(tomllib.loads(_blob_text(Path(repo), entry["blob_sha"])))
        if slugs:
            labels.setdefault(entry["path"], set()).update(slugs)
    return {path: sorted(s) for path, s in sorted(labels.items())}


def _packet_budget_dropped(packet_dir: Path) -> bool:
    try:
        manifest = json.loads((packet_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    dropped = (manifest.get("budget") or {}).get("dropped") or {}
    return bool(dropped.get("runs") or dropped.get("commits"))


def answer_key(
    repo: Path | str, packet_dir: Path | str, charter_purposes: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """What the packet's work serves, from the labels. Raises if a label names no charter purpose."""
    labels = packet_labels(repo, packet_dir)
    used = {s for slugs in labels.values() for s in slugs}
    unknown = sorted(used - set(charter_purposes) - {OFF_CHARTER})
    if unknown:
        raise Ac9LabelError(f"label(s) {unknown} name no charter purpose")
    live = {p for p, v in charter_purposes.items() if v.get("state") == "live"}
    retired = {p for p, v in charter_purposes.items() if v.get("state") == "retired"}
    return {
        "present": sorted(live & used),
        "silent": sorted(live - used),
        "revived": sorted(retired & used),
        "absent_retired": sorted(retired - used),
        "off_charter_sidecars": sorted(p for p, s in labels.items() if OFF_CHARTER in s),
        "budget_limited": _packet_budget_dropped(Path(packet_dir)),
        "labels": labels,
    }


def reading_gold(
    packet_dir: Path | str, reading: dict[str, Any], labels: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Reading purpose id -> the labels of the runs and sidecars it cites (empty when it cites none labelled)."""
    view = _PacketView(Path(packet_dir))
    run_sidecar: dict[str, str | None] = {}
    for line in (view.reader / "runs.jsonl").read_text(encoding="utf-8").split("\n"):
        if line:
            rec = json.loads(line)
            run_sidecar[str(rec["id"])] = rec.get("sidecar_path")
    gold: dict[str, list[str]] = {}
    for purpose in reading["purposes"]:
        found: set[str] = set()
        for cit in purpose["citations"]:
            raw = cit["citation"] if isinstance(cit, dict) and "citation" in cit else cit
            res, key = resolve_citation(raw, view)
            if res != RESOLVED or key is None:
                continue
            kind, _, rest = key.partition(":")
            path = (
                run_sidecar.get(rest)
                if kind == "run"
                else rest.split("#", 1)[0]
                if kind == "sidecar"
                else None
            )
            if path:
                found.update(labels.get(path, []))
        gold[purpose["id"]] = sorted(found)
    return gold


def _rate(hits: int, n: int) -> float | None:
    return hits / n if n else None


def ac9_metrics(
    key: dict[str, Any], gold: dict[str, list[str]], comparison: dict[str, Any]
) -> dict[str, Any]:
    """The five T7 quantities for one comparison, each with its denominator. ``None`` means nothing to score.

    Silent items flagged ``budget_limited`` are left out of silent recall and false-silent (T7(c)); the flag is per
    packet, so a budget-cut packet scores no silent case at all.
    """
    items = comparison["items"]
    silent_reported = {
        it["charter_purpose_id"]
        for it in items
        if it["kind"] == "silent" and not it.get("budget_limited")
    }
    scorable = not key["budget_limited"]
    true_silent = key["silent"] if scorable else []
    present = key["present"] if scorable else []

    mapped_by_rp: dict[str, list[str]] = {}
    for it in items:
        if it["kind"] == "mapped":
            mapped_by_rp.setdefault(it["reading_purpose_id"], []).append(it["charter_purpose_id"])
    off_rps = [rp for rp, g in gold.items() if g == [OFF_CHARTER]]
    flagged = {it.get("matched_retired") for it in items if it.get("matched_retired")}
    mapped_pairs = [(rp, t) for rp, ts in mapped_by_rp.items() for t in ts]

    return {
        "silent_recall": _rate(sum(p in silent_reported for p in true_silent), len(true_silent)),
        "n_silent": len(true_silent),
        "false_silent": _rate(sum(p in silent_reported for p in present), len(present)),
        "n_present": len(present),
        "off_charter_unmapped": _rate(sum(rp not in mapped_by_rp for rp in off_rps), len(off_rps)),
        "n_off_charter": len(off_rps),
        "revived_flagged": _rate(sum(r in flagged for r in key["revived"]), len(key["revived"])),
        "n_revived": len(key["revived"]),
        "gold_agreement": _rate(
            sum(t in gold.get(rp, []) for rp, t in mapped_pairs), len(mapped_pairs)
        ),
        "n_mapped": len(mapped_pairs),
    }


def meets_pass(metrics: dict[str, Any]) -> bool:
    """True only when every scorable metric meets the owner's T7 pass value. An unscorable metric is not a pass."""
    for name, bound in PASS.items():
        value = metrics[name]
        if value is None:
            return False
        if name == "false_silent" and value > bound:
            return False
        if name != "false_silent" and value < bound:
            return False
    return True


def degenerate_mappings(
    reading_ids: list[str], charter_purposes: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Mappings that use no knowledge of the work. Each must fail ``meets_pass`` on a planted set."""
    live = sorted(p for p, v in charter_purposes.items() if v.get("state") == "live")
    everything = sorted(charter_purposes)
    return {
        "all_unmapped": {rp: None for rp in reading_ids},
        "all_to_first_live": {rp: live[0] for rp in reading_ids},
        "each_to_every_purpose": {rp: list(everything) for rp in reading_ids},
    }
