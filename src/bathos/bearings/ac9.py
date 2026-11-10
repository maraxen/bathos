"""AC-9 bookkeeping from sidecar purpose labels (spec 261104 T7, owner 261109). Owner-side only: nothing here may
run inside a reader's view.

The truth AC-9 scores against is the ``[charter] purpose`` label each sidecar carries (see ``purpose_label``). The
packet builder strips it, so this module reads it back from the repo by the blob shas the packet lists:

- ``packet_labels``: sidecar path -> purpose slugs, for the sidecars a reader was shown.
- ``answer_key``: which charter purposes the packet's work serves (present / silent, revived / absent retired),
  which purposes are indeterminate because their evidence was withheld, which sidecars are attested off-charter, and
  whether the packet dropped content for budget.
- ``reading_gold``: for each reading purpose, the labels of the runs and sidecars it cites.
- ``ac9_metrics``: the five quantities T7 sets thresholds on, for one comparison.
- ``degenerate_mappings``: mappings that know nothing; they must not meet ``pass``.
- ``decoy_mapped_share`` / ``decoy_invalid``: the decoy-charter negative control (over 0.2 mapped is invalid).

The thresholds in ``meets_pass`` are the owner's T7 values read per comparison (a revived purpose must be flagged in
at least 2 of 3 readings, here as a rate). The AC-9 sidecar decides how comparisons pool.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from bathos.bearings.purpose_label import (
    LABEL_TABLE,
    OFF_CHARTER,
    PurposeLabelError,
    id_patterns,
    ids_named,
    read_purpose_label,
)
from bathos.bearings.reading import RESOLVED, _PacketView, resolve_citation

__all__ = [
    "OFF_CHARTER",
    "PASS",
    "DECOY_MAX_MAPPED",
    "Ac9LabelError",
    "ac9_metrics",
    "answer_key",
    "decoy_invalid",
    "decoy_mapped_share",
    "degenerate_mappings",
    "MARGINAL",
    "meets_marginal",
    "meets_pass",
    "packet_labels",
    "reading_gold",
]

PASS = {
    "silent_recall": 0.8,  # at least
    "false_silent": 0.2,  # at most
    "off_charter_unmapped": 0.8,  # at least
    "revived_flagged": 2 / 3,  # at least
    "gold_agreement": 0.8,  # at least
}
DECOY_MAX_MAPPED = 0.2  # T7(a): a decoy-charter mapped share above this makes the run invalid


class Ac9LabelError(ValueError):
    """A label names no charter purpose, is malformed, or a sidecar in the packet cannot be read back from the repo."""


def _blob_text(repo: Path, blob: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", blob], capture_output=True, check=False
    )
    if proc.returncode != 0:
        raise Ac9LabelError(f"sidecar blob {blob[:12]} is not in {repo}")
    return proc.stdout.decode("utf-8", errors="replace")


def _label_of(text: str, where: str) -> list[str] | None:
    try:
        return read_purpose_label(tomllib.loads(text))
    except tomllib.TOMLDecodeError as e:
        if LABEL_TABLE not in text:
            return None  # unparseable, but cannot hold a label
        raise Ac9LabelError(f"{where}: label cannot be read ({e}); fix it before scoring") from e
    except PurposeLabelError as e:
        raise Ac9LabelError(f"{where}: label cannot be read ({e}); fix it before scoring") from e


def _shown_blob_labels(repo: Path, packet_dir: Path) -> list[tuple[str, str, list[str]]]:
    index = json.loads((packet_dir / "reader" / "index.json").read_text(encoding="utf-8"))
    out = []
    for entry in index["sidecars"]:
        where = f"sidecar:{entry['path']}@{entry['blob_sha'][:12]}"
        out.append(
            (
                entry["path"],
                entry["blob_sha"],
                _label_of(_blob_text(repo, entry["blob_sha"]), where) or [],
            )
        )
    return out


def packet_labels(repo: Path | str, packet_dir: Path | str) -> dict[str, list[str]]:
    """Shown sidecar path -> sorted purpose slugs (union over the path's blobs). Unlabelled sidecars are absent."""
    labels: dict[str, set[str]] = {}
    for path, _, slugs in _shown_blob_labels(Path(repo), Path(packet_dir)):
        if slugs:
            labels.setdefault(path, set()).update(slugs)
    return {path: sorted(s) for path, s in sorted(labels.items())}


def _manifest(packet_dir: Path) -> dict[str, Any]:
    try:
        return json.loads((packet_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _packet_budget_dropped(manifest: dict[str, Any]) -> bool:
    dropped = (manifest.get("budget") or {}).get("dropped") or {}
    return bool(dropped.get("runs") or dropped.get("commits"))


def answer_key(
    repo: Path | str, packet_dir: Path | str, charter_purposes: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """What the packet's work serves, from the labels.

    Raises ``Ac9LabelError`` if a label names no charter purpose or cannot be read.

    A purpose is **indeterminate** (neither present nor silent, and left out of both denominators) when the packet
    withheld evidence for it: a sidecar it labels, or that names its id, was excluded from the reader; or one path
    carries different labels at two commits. Present needs shown, agreeing evidence; silent needs none at all.
    """
    repo_p, pkt = Path(repo), Path(packet_dir)
    manifest = _manifest(pkt)

    # Every labelled blob per path, shown or withheld: (slugs, shown?).
    per_path: dict[str, list[tuple[set[str], bool]]] = {}
    shown: set[str] = set()
    shown_blobs: set[tuple[str, str]] = set()
    for path, blob, slugs in _shown_blob_labels(repo_p, pkt):
        shown_blobs.add((path, blob))
        if slugs:
            per_path.setdefault(path, []).append((set(slugs), True))
            shown |= set(slugs)

    # Sidecars the packet withheld (excluded themselves, or belonging to a withheld run): their labels are still
    # the truth about the work, but readers could not see it. The same id set and matcher as the packet builder.
    withheld: set[str] = set()
    n_withheld_sidecars = 0
    pats, _ = id_patterns(charter_purposes)
    candidates: set[str] = set()
    for exc in manifest.get("exclusions", []):
        path = str(exc.get("path", ""))
        if path.startswith("sidecar:") and "@" in path:
            candidates.add(path)
    candidates |= {str(p) for p in manifest.get("withheld_run_sidecars", [])}
    for path in sorted(candidates):
        rel, _, blob = path[len("sidecar:") :].rpartition("@")
        if (rel, blob) in shown_blobs:
            continue  # a sidecar a kept run shows is shown evidence, even if a withheld run shared it (round 3)
        text = _blob_text(
            repo_p, blob
        )  # undecodable bytes are replaced; a label on the readable lines survives
        slugs = set(_label_of(text, path) or [])
        named = ids_named(text, pats) & set(charter_purposes)
        if slugs or named:
            n_withheld_sidecars += 1
        withheld |= slugs | named
        if slugs:
            per_path.setdefault(rel, []).append((slugs, False))

    firm: set[str] = set()
    disagree: set[str] = set()
    labels: dict[str, set[str]] = {}
    ambiguous: set[str] = set()
    for path, entries in per_path.items():
        # Labelled blobs only: a label added to a later version of a sidecar is not a disagreement. Shown and
        # withheld blobs of one path are compared together; firm support needs a shown blob.
        sets = [s for s, _ in entries]
        union = set().union(*sets)
        common = set.intersection(*sets)
        if any(is_shown for _, is_shown in entries):
            firm |= common
        disagree |= union - common
        if union == common:
            labels[path] = union
        else:
            # Its blobs disagree (or mix off-charter with a slug): the path gives no gold, and a reading purpose
            # citing it is not scored on gold (review round 3, 261110).
            ambiguous.add(path)

    unknown = sorted((shown | withheld) - set(charter_purposes) - {OFF_CHARTER})
    if unknown:
        raise Ac9LabelError(f"label(s) {unknown} name no charter purpose")

    indeterminate = (withheld | disagree) - firm - {OFF_CHARTER}
    live = {p for p, v in charter_purposes.items() if v.get("state") == "live"}
    retired = {p for p, v in charter_purposes.items() if v.get("state") == "retired"}
    runs_by_reason: dict[str, int] = {}
    for e in manifest.get("exclusions", []):
        if str(e.get("path", "")).startswith("run:"):
            reason = str(e.get("reason"))
            runs_by_reason[reason] = runs_by_reason.get(reason, 0) + 1
    # Withheld for what the run says (blindness filters), as distinct from runs outside the window's scope.
    n_withheld_runs = sum(runs_by_reason.get(r, 0) for r in ("quotes_charter", "names_charter_id"))
    return {
        "present": sorted(live & firm),
        "silent": sorted(live - firm - indeterminate),
        "revived": sorted(retired & firm),
        "absent_retired": sorted(retired - firm - indeterminate),
        "indeterminate": sorted(indeterminate & (live | retired)),
        "off_charter_sidecars": sorted(p for p, s in labels.items() if OFF_CHARTER in s),
        "budget_limited": _packet_budget_dropped(manifest),
        "n_withheld_sidecars": n_withheld_sidecars,
        # Runs withheld by the blindness filters (quotes_charter, names_charter_id); their sidecar labels are read
        # (withheld_run_sidecars). The AC-9 sidecar bounds this share. All run exclusions, by reason, beside it.
        "n_withheld_runs": n_withheld_runs,
        # Withheld runs whose sidecar could not be resolved: their purpose is unknown, so it may sit in `silent`.
        # The AC-9 sidecar must count these against the withheld-share bound.
        "n_withheld_runs_unresolved": len(manifest.get("withheld_runs_unresolved", [])),
        "run_exclusions_by_reason": dict(sorted(runs_by_reason.items())),
        "labels": {path: sorted(s) for path, s in sorted(labels.items())},
        "ambiguous_paths": sorted(ambiguous),
        # Denominators for the withheld-share bounds (round 3): what the reader was shown.
        "n_shown_sidecars": len(shown_blobs),
        "n_shown_runs": _count_lines(pkt / "reader" / "runs.jsonl"),
    }


def _count_lines(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").split("\n") if line.strip())
    except OSError:
        return 0


def reading_gold(
    packet_dir: Path | str,
    reading: dict[str, Any],
    labels: dict[str, list[str]],
    ambiguous: Iterable[str] = (),
) -> dict[str, list[str]]:
    """Reading purpose id -> the labels of the runs and sidecars it cites (empty when it cites none labelled).

    A cited run whose sidecar was withheld still has that sidecar's label as gold: what the work serves does not
    depend on what the reader was shown. A reading purpose citing an ``ambiguous`` path (``answer_key``'s
    ``ambiguous_paths``) is left out of the result, so it is scored neither right nor wrong on gold.
    """
    ambiguous_set = set(ambiguous)
    view = _PacketView(Path(packet_dir))
    run_sidecar: dict[str, str | None] = {}
    for line in (view.reader / "runs.jsonl").read_text(encoding="utf-8").split("\n"):
        if line:
            rec = json.loads(line)
            run_sidecar[str(rec["id"])] = rec.get("sidecar_path")
    gold: dict[str, list[str]] = {}
    for purpose in reading["purposes"]:
        found: set[str] = set()
        cites_ambiguous = False
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
                cites_ambiguous |= path in ambiguous_set
                found.update(labels.get(path, []))
        if not cites_ambiguous:
            gold[purpose["id"]] = sorted(found)
    return gold


def _rate(hits: int, n: int) -> float | None:
    return hits / n if n else None


def _targets_by_reading_purpose(items: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Every charter target a reading purpose was mapped to: live targets (``mapped``) and retired ones
    (``unmapped`` carrying ``matched_retired``)."""
    out: dict[str, set[str]] = {}
    for it in items:
        rp = it.get("reading_purpose_id")
        if rp is None:
            continue
        if it["kind"] == "mapped":
            out.setdefault(rp, set()).add(it["charter_purpose_id"])
        elif it.get("matched_retired"):
            out.setdefault(rp, set()).add(it["matched_retired"])
    return out


def ac9_metrics(
    key: dict[str, Any], gold: dict[str, list[str]], comparison: dict[str, Any]
) -> dict[str, Any]:
    """The five T7 quantities for one comparison, each with its denominator. ``None`` means nothing to score.

    - Silent recall and false-silent leave out indeterminate purposes, and every silent case of a budget-cut packet
      (T7(c); the flag is per packet).
    - Off-charter work counts as unmapped only when its reading purpose has no target at all, live or retired.
    - A revived purpose counts as flagged only when a reading purpose whose gold names it maps to it.
    - Gold agreement covers every (reading purpose, target) pair, retired targets included (review 261110).
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

    targets = _targets_by_reading_purpose(items)
    off_rps = [rp for rp, g in gold.items() if g == [OFF_CHARTER]]
    flagged = {r for rp, ts in targets.items() for r in ts if r in gold.get(rp, [])}
    # Only reading purposes with gold are scored on it; one citing an ambiguous path has none (round 3).
    pairs = [(rp, t) for rp, ts in sorted(targets.items()) if rp in gold for t in sorted(ts)]

    return {
        "silent_recall": _rate(sum(p in silent_reported for p in true_silent), len(true_silent)),
        "n_silent": len(true_silent),
        "false_silent": _rate(sum(p in silent_reported for p in present), len(present)),
        "n_present": len(present),
        "off_charter_unmapped": _rate(sum(not targets.get(rp) for rp in off_rps), len(off_rps)),
        "n_off_charter": len(off_rps),
        "revived_flagged": _rate(sum(r in flagged for r in key["revived"]), len(key["revived"])),
        "n_revived": len(key["revived"]),
        "gold_agreement": _rate(sum(t in gold.get(rp, []) for rp, t in pairs), len(pairs)),
        "n_mapped": len(pairs),
        "n_indeterminate": len(key.get("indeterminate", [])),
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


MARGINAL = 0.6  # T7: `marginal` when every metric is at least 0.6


def meets_marginal(metrics: dict[str, Any]) -> bool:
    """T7's ``marginal`` band: every metric scorable and at least 0.6.

    False-silent is an error rate (lower is better), so "at least 0.6" is read as its complement: false-silent at most
    0.4. This reading is the implementer's (261110) and is recorded in the spec for the owner to confirm.
    """
    for name in PASS:
        value = metrics[name]
        if value is None:
            return False
        if name == "false_silent" and value > 1 - MARGINAL:
            return False
        if name != "false_silent" and value < MARGINAL:
            return False
    return True


def degenerate_mappings(
    reading_ids: list[str], charter_purposes: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Mappings that use no knowledge of the work. Each must fail ``meets_pass`` on a planted set."""
    live = sorted(p for p, v in charter_purposes.items() if v.get("state") == "live")
    first = live[0] if live else None
    everything = sorted(charter_purposes)
    return {
        "all_unmapped": {rp: None for rp in reading_ids},
        "all_to_first_live": {rp: first for rp in reading_ids},
        "each_to_every_purpose": {rp: list(everything) for rp in reading_ids},
        # Sparse: one mapping, the rest left unmapped. Precision alone would reward it.
        "first_only": {rp: (first if i == 0 else None) for i, rp in enumerate(reading_ids)},
    }


def decoy_mapped_share(comparison: dict[str, Any]) -> float:
    """Share of reading purposes mapped to any decoy-charter target, retired included (T7(a) negative control)."""
    rps = {it["reading_purpose_id"] for it in comparison["items"] if it.get("reading_purpose_id")}
    if not rps:
        return 0.0
    targets = _targets_by_reading_purpose(comparison["items"])
    return sum(1 for rp in rps if targets.get(rp)) / len(rps)


def decoy_invalid(share: float) -> bool:
    """A decoy run with more than ``DECOY_MAX_MAPPED`` of reading purposes mapped invalidates the AC-9 run."""
    return share > DECOY_MAX_MAPPED
