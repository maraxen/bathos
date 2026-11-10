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

import contextlib
import hashlib
import json
import subprocess
import tomllib
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
    "invalid_reasons",
    "load_label_overlay",
    "BOUNDS",
    "bound_violations",
    "missing_cases",
    "oracle_reading",
    "plan_planted_packets",
    "pooled_metrics",
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


def _effective_label_sets(blob_slugs: list[str], overlay_slugs: list[str] | None) -> list[set[str]]:
    """The label sets a blob contributes: its own label, else the overlay's; both when they disagree."""
    own, ov = set(blob_slugs), set(overlay_slugs or [])
    if own and ov and own != ov:
        return [own, ov]
    return [own or ov] if (own or ov) else []


def load_label_overlay(path: Path | str) -> dict[str, list[str]]:
    """Owner-confirmed labels from a confirmation TOML's ``[labels]`` table: ``"<sidecar path>" = "<slug>"`` or a list.

    Each value obeys the sidecar label rules (readable slugs, ``off-charter`` alone); a bad value raises
    ``Ac9LabelError`` naming the path. A file with no ``[labels]`` table gives an empty overlay.
    """
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for sidecar, value in (data.get("labels") or {}).items():
        try:
            slugs = read_purpose_label({LABEL_TABLE: {"purpose": value}})
        except PurposeLabelError as e:
            raise Ac9LabelError(f"label for {sidecar!r} in {Path(path).name}: {e}") from e
        out[str(sidecar)] = slugs or []
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
    repo: Path | str,
    packet_dir: Path | str,
    charter_purposes: dict[str, dict[str, Any]],
    labels_overlay: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """What the packet's work serves, from the labels.

    ``labels_overlay`` (sidecar path -> slugs, ``load_label_overlay``) holds owner-confirmed labels for sidecars whose
    blobs carry none: a label added to a sidecar file today never reaches a past run's blob, so an after-the-fact
    batch lives outside the files (shadow.label, label_source owner_confirmed). A blob's own label and an overlay
    label that disagree are both kept, so the path is ambiguous; neither silently wins (261110).

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
    n_shown_labelled = 0
    overlay = labels_overlay or {}
    for path, blob, blob_slugs in _shown_blob_labels(repo_p, pkt):
        shown_blobs.add((path, blob))
        sets = _effective_label_sets(blob_slugs, overlay.get(path))
        n_shown_labelled += bool(sets)
        for labelled in sets:
            per_path.setdefault(path, []).append((labelled, True))
            shown |= labelled

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
        sets = _effective_label_sets(_label_of(text, path) or [], overlay.get(rel))
        slugs = set().union(*sets) if sets else set()
        named = ids_named(text, pats) & set(charter_purposes)
        if slugs or named:
            n_withheld_sidecars += 1
        withheld |= slugs | named
        for labelled in sets:
            per_path.setdefault(rel, []).append((labelled, False))

    firm: set[str] = set()
    disagree: set[str] = set()
    labels: dict[str, set[str]] = {}
    ambiguous: dict[str, set[str]] = {}
    for path, entries in per_path.items():
        # Labelled blobs only: a label added to a later version of a sidecar is not a disagreement. Shown and
        # withheld blobs of one path are compared together; firm support needs a shown blob.
        sets = [s for s, _ in entries]
        union = set().union(*sets)
        common = set.intersection(*sets)
        if any(is_shown for _, is_shown in entries):
            firm |= common
        disagree |= union - common
        if common:
            labels[path] = (
                common  # what every labelled blob of the path agrees on is firm gold (round 4)
            )
        if union != common:
            # Its blobs disagree (or mix off-charter with a slug): the disputed labels are kept as
            # `ambiguous_labels`, so a target among them is not scored either way (review rounds 3-4, 261110).
            ambiguous[path] = union - common

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
        "ambiguous_labels": {path: sorted(s) for path, s in sorted(ambiguous.items())},
        # Denominators for the withheld-share bounds (rounds 3-4): what the reader was shown. n_withheld_sidecars
        # counts labelled or id-naming sidecars, so compare it with n_shown_sidecars_labelled, not all shown.
        "n_shown_sidecars": len(shown_blobs),
        "n_shown_sidecars_labelled": n_shown_labelled,
        "n_shown_runs": _count_lines(pkt / "reader" / "runs.jsonl"),
    }


def _count_lines(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").split("\n") if line.strip())
    except OSError:
        return 0


def reading_gold(
    packet_dir: Path | str, reading: dict[str, Any], labels: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Reading purpose id -> the labels of the runs and sidecars it cites (empty when it cites none labelled).

    A cited run whose sidecar was withheld still has that sidecar's label as gold: what the work serves does not
    depend on what the reader was shown. Pass ``answer_key``'s ``labels`` for firm gold; pass its
    ``ambiguous_labels`` to get the labels a reading purpose cites only through ambiguous paths (``maybe`` for
    ``ac9_metrics``). Every reading purpose is in the result.
    """
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
    key: dict[str, Any],
    gold: dict[str, list[str]],
    comparison: dict[str, Any],
    maybe: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """The five T7 quantities for one comparison, each with its denominator. ``None`` means nothing to score.

    ``maybe`` is ``reading_gold`` over ``answer_key``'s ``ambiguous_labels``: labels a reading purpose cites only
    through a path whose blobs disagree.

    - Silent recall and false-silent leave out indeterminate purposes, and every silent case of a budget-cut packet
      (T7(c); the flag is per packet).
    - Off-charter work counts as unmapped only when its reading purpose has no target at all, live or retired. A
      reading purpose whose firm gold is off-charter but whose ambiguous labels name a real slug is not scored here.
    - A revived purpose counts as flagged when a reading purpose whose gold or ambiguous labels name it maps to it.
    - Gold agreement covers every (reading purpose, target) pair, retired targets included. A target found only in
      the reading purpose's ambiguous labels is left out (``n_unscored_pairs``): it is neither right nor wrong. Any
      other target outside its gold is a miss, so an error on a purpose that also cites an ambiguous path still
      counts (review round 4, 261110).
    """
    c = _reading_counts(key, gold, comparison, maybe)
    return {
        "silent_recall": _rate(c["silent_hits"], c["n_silent"]),
        "n_silent": c["n_silent"],
        "false_silent": _rate(c["false_silent_hits"], c["n_present"]),
        "n_present": c["n_present"],
        "off_charter_unmapped": _rate(c["off_charter_hits"], c["n_off_charter"]),
        "n_off_charter": c["n_off_charter"],
        "revived_flagged": _rate(c["revived_hits"], c["n_revived"]),
        "n_revived": c["n_revived"],
        "gold_agreement": _rate(c["gold_hits"], c["n_mapped"]),
        "n_mapped": c["n_mapped"],
        "n_unscored_pairs": c["n_unscored_pairs"],
        "n_indeterminate": len(key.get("indeterminate", [])),
    }


def _silent_reported(comparison: dict[str, Any]) -> set[str]:
    return {
        it["charter_purpose_id"]
        for it in comparison["items"]
        if it["kind"] == "silent" and not it.get("budget_limited")
    }


def _reading_counts(
    key: dict[str, Any],
    gold: dict[str, list[str]],
    comparison: dict[str, Any],
    maybe: dict[str, list[str]] | None = None,
    silent_reported: set[str] | None = None,
) -> dict[str, int]:
    """Hits and denominators for one reading (the integers behind ``ac9_metrics``'s rates).

    ``silent_reported`` overrides this reading's own silent set; the pooled scorer passes the packet's combined set.
    """
    maybe = maybe or {}
    items = comparison["items"]
    if silent_reported is None:
        silent_reported = _silent_reported(comparison)
    scorable = not key["budget_limited"]
    true_silent = key["silent"] if scorable else []
    present = key["present"] if scorable else []

    targets = _targets_by_reading_purpose(items)
    off_rps = [
        rp
        for rp, g in gold.items()
        if g == [OFF_CHARTER] and not set(maybe.get(rp, [])) - {OFF_CHARTER}
    ]
    flagged = {
        r
        for rp, ts in targets.items()
        for r in ts
        if r in gold.get(rp, []) or r in maybe.get(rp, [])
    }
    all_pairs = [(rp, t) for rp, ts in sorted(targets.items()) for t in sorted(ts)]
    pairs = [(rp, t) for rp, t in all_pairs if t in gold.get(rp, []) or t not in maybe.get(rp, [])]
    return {
        "silent_hits": sum(p in silent_reported for p in true_silent),
        "n_silent": len(true_silent),
        "false_silent_hits": sum(p in silent_reported for p in present),
        "n_present": len(present),
        "off_charter_hits": sum(not targets.get(rp) for rp in off_rps),
        "n_off_charter": len(off_rps),
        "revived_hits": sum(r in flagged for r in key["revived"]),
        "n_revived": len(key["revived"]),
        "gold_hits": sum(t in gold.get(rp, []) for rp, t in pairs),
        "n_mapped": len(pairs),
        "n_unscored_pairs": len(all_pairs) - len(pairs),
    }


def meets_pass(metrics: dict[str, Any], *, gate_revived: bool = True) -> bool:
    """True only when every scorable metric meets the owner's T7 pass value. An unscorable metric is not a pass.

    ``gate_revived=False`` leaves revived_flagged out (AC-9 amendment 3, owner 261110: no labelled window holds
    revival work, so the case is reported when it occurs but does not gate).
    """
    for name, bound in PASS.items():
        if name == "revived_flagged" and not gate_revived:
            continue
        value = metrics[name]
        if value is None:
            return False
        if name == "false_silent" and value > bound:
            return False
        if name != "false_silent" and value < bound:
            return False
    return True


def invalid_reasons(key: dict[str, Any]) -> list[str]:
    """Conditions in an answer key that make an AC-9 comparison ``invalid`` whatever bounds the sidecar sets.

    A withheld run whose sidecar cannot be resolved hid evidence for a purpose nobody can name, so that purpose may
    sit in ``silent`` and a reader's correct-looking silent call would be credited for work it never saw (review
    round 4, 261110). The share bounds (withheld, indeterminate, ambiguous) are the AC-9 sidecar's to set.
    """
    reasons = []
    if key.get("n_withheld_runs_unresolved"):
        reasons.append(
            f"{key['n_withheld_runs_unresolved']} withheld run(s) with an unresolvable sidecar: "
            "silent ground truth is unknown"
        )
    return reasons


MARGINAL = 0.6  # T7: `marginal` when every metric is at least 0.6


def meets_marginal(metrics: dict[str, Any], *, gate_revived: bool = True) -> bool:
    """T7's ``marginal`` band: every metric scorable and at least 0.6.

    False-silent is an error rate (lower is better), so "at least 0.6" is read as its complement: false-silent at most
    0.4. This reading is the implementer's (261110) and is recorded in the spec for the owner to confirm.
    """
    for name in PASS:
        if name == "revived_flagged" and not gate_revived:
            continue
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


# ── AC-9 run: planting, oracle reading, pooling, bounds (owner 261110) ──────────────────────────────────────────

PLANT_SEED = "ac9-261110"  # fixed before any label exists; ranks charter ids and runs
N_PACKETS = 3
SILENT_PER_PACKET = 2
MIN_RUNS_PER_PURPOSE = 2  # a reading purpose needs at least 2 citations
MAX_RUNS_PER_PURPOSE = 10  # cap per purpose, so one busy purpose cannot dominate a packet
MIN_ELIGIBLE_LIVE = SILENT_PER_PACKET + 3  # 2 planted silent and at least 3 present in every packet
MIN_RETIRED = 2  # one revived and one absent in every packet
MIN_OFF_CHARTER_RUNS = 2

# Antigravity's proposal, accepted by the owner 261110 (AskUserQuestion); arbitrary starting points.
BOUNDS = {
    "indeterminate_live_max": 1,
    "indeterminate_live_frac": 0.15,
    "withheld_sidecar_frac": 0.10,
    "withheld_run_frac": 0.10,
    "ambiguous_paths_max": 1,
}


def _seeded(token: str) -> str:
    return hashlib.sha256(f"{PLANT_SEED}:{token}".encode()).hexdigest()


def plan_planted_packets(
    run_labels: dict[str, list[str]],
    charter_purposes: dict[str, dict[str, Any]],
    *,
    require_revival: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    """The three planted packets, chosen mechanically from labelled runs. Returns ``(plans, unmet)``.

    ``require_revival=False`` (AC-9 amendment 3) plants without a revived retired purpose when none has runs; one is
    still revived when the record has one.

    ``run_labels`` maps run id -> its sidecar's purpose slugs (unlabelled runs are left out by the caller). A live
    purpose is eligible when it has at least ``MIN_RUNS_PER_PURPOSE`` labelled runs. Eligible live purposes are ranked
    by a seeded hash; packet k plants ranks 2k and 2k+1 (mod the number eligible) as silent and keeps the rest present.
    Retired purposes with runs are ranked the same way; packet k revives one, so every other retired purpose is absent.
    A run is kept only if every label it carries is a kept purpose or ``off-charter``; each purpose keeps at most
    ``MAX_RUNS_PER_PURPOSE`` runs, lowest seeded rank first (a run counts toward its first label in sorted order).
    ``unmet`` lists why the record cannot plant every case; when it is non-empty, ``plans`` is empty.
    """
    by_purpose: dict[str, list[str]] = {}
    for rid, slugs in run_labels.items():
        for s in slugs:
            by_purpose.setdefault(s, []).append(rid)
    live = [p for p, v in charter_purposes.items() if v.get("state") == "live"]
    retired = [p for p, v in charter_purposes.items() if v.get("state") == "retired"]
    eligible = sorted(
        (p for p in live if len(by_purpose.get(p, [])) >= MIN_RUNS_PER_PURPOSE), key=_seeded
    )
    revivable = sorted(
        (p for p in retired if len(by_purpose.get(p, [])) >= MIN_RUNS_PER_PURPOSE), key=_seeded
    )
    unmet = []
    if len(eligible) < MIN_ELIGIBLE_LIVE:
        unmet.append(
            f"{len(eligible)} live purposes with >= {MIN_RUNS_PER_PURPOSE} labelled runs; need {MIN_ELIGIBLE_LIVE}"
        )
    if require_revival and not revivable:
        unmet.append(
            f"no retired purpose with >= {MIN_RUNS_PER_PURPOSE} labelled runs (revival case)"
        )
    if len(retired) < (MIN_RETIRED if require_revival else 1):
        unmet.append(f"{len(retired)} retired purposes; need at least one absent retired purpose")
    if len(by_purpose.get(OFF_CHARTER, [])) < MIN_OFF_CHARTER_RUNS:
        unmet.append(f"fewer than {MIN_OFF_CHARTER_RUNS} runs labelled {OFF_CHARTER}")
    if unmet:
        return [], unmet

    plans = []
    n = len(eligible)
    for k in range(N_PACKETS):
        silent = {eligible[(2 * k + j) % n] for j in range(SILENT_PER_PACKET)}
        present = [p for p in eligible if p not in silent]
        revived = revivable[k % len(revivable)] if revivable else None
        allowed = set(present) | {OFF_CHARTER} | ({revived} if revived else set())
        per_purpose: dict[str, int] = {}
        kept = []
        for rid in sorted(run_labels, key=_seeded):
            slugs = sorted(run_labels[rid])
            if not slugs or not set(slugs) <= allowed:
                continue
            first = slugs[0]
            if per_purpose.get(first, 0) >= MAX_RUNS_PER_PURPOSE:
                continue
            per_purpose[first] = per_purpose.get(first, 0) + 1
            kept.append(rid)
        plans.append(
            {
                "packet": f"q{k + 1}",
                "silent": sorted(silent),
                "present": sorted(present),
                "revived": revived,
                "absent_retired": sorted(
                    set(retired) - {revived}
                ),  # every retired purpose when none revives
                "runs": sorted(kept),
            }
        )
    return plans, []


def missing_cases(key: dict[str, Any], *, require_revival: bool = True) -> list[str]:
    """Cases a planted packet's answer key fails to contain; the run is invalid if any packet misses one."""
    need = {
        "present": key["present"],
        "silent": key["silent"],
        **({"revived": key["revived"]} if require_revival else {}),
        "absent_retired": key["absent_retired"],
        "off_charter_sidecars": key["off_charter_sidecars"],
    }
    return sorted(case for case, got in need.items() if not got)


def bound_violations(key: dict[str, Any], charter_purposes: dict[str, dict[str, Any]]) -> list[str]:
    """Withheld, indeterminate and ambiguous evidence over the owner's bounds (``BOUNDS``); each makes a run invalid."""
    live = {p for p, v in charter_purposes.items() if v.get("state") == "live"}
    out = list(invalid_reasons(key))
    n_ind = len(set(key["indeterminate"]) & live)
    allowed = max(
        BOUNDS["indeterminate_live_max"], int(BOUNDS["indeterminate_live_frac"] * len(live))
    )
    if n_ind > allowed:
        out.append(f"{n_ind} live purposes indeterminate; at most {allowed}")
    ws, ss = key["n_withheld_sidecars"], key["n_shown_sidecars_labelled"]
    if ws and ws / (ws + ss) > BOUNDS["withheld_sidecar_frac"]:
        out.append(
            f"withheld labelled sidecars {ws}/{ws + ss} over {BOUNDS['withheld_sidecar_frac']}"
        )
    wr, sr = key["n_withheld_runs"], key["n_shown_runs"]
    if wr and wr / (wr + sr) > BOUNDS["withheld_run_frac"]:
        out.append(f"withheld runs {wr}/{wr + sr} over {BOUNDS['withheld_run_frac']}")
    if len(key["ambiguous_paths"]) > BOUNDS["ambiguous_paths_max"]:
        out.append(
            f"{len(key['ambiguous_paths'])} ambiguous paths; at most {BOUNDS['ambiguous_paths_max']}"
        )
    return out


def oracle_reading(
    packet_dir: Path | str, labels: dict[str, list[str]], packet_sha256: str
) -> dict[str, Any]:
    """The mapper-only arm's reading (owner 261110): one reading purpose per labelled purpose in the packet, built
    mechanically from the labels, so mapper error is measured without reader error.

    Each purpose's text is the distinct hypotheses of its sidecars (no slug can appear: a sidecar naming one is never
    shown), and it cites up to 5 of its runs plus their sidecars. Ids are opaque and ordered by seeded rank.
    """
    reader = Path(packet_dir) / "reader"
    runs = [
        json.loads(x) for x in (reader / "runs.jsonl").read_text(encoding="utf-8").split("\n") if x
    ]
    index = json.loads((reader / "index.json").read_text(encoding="utf-8"))
    blob_of = {s["path"]: s["blob_sha"] for s in index["sidecars"]}
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in runs:
        for slug in labels.get(r.get("sidecar_path") or "", []):
            groups.setdefault(slug, []).append(r)
    purposes = []
    for i, slug in enumerate(sorted(groups, key=_seeded)):
        rs = sorted(groups[slug], key=lambda r: _seeded(str(r["id"])))[:5]
        hyps: list[str] = []
        # Only sidecars the packet shows can be read or cited; a withheld sidecar's run is still cited.
        shown_paths = [p for p in dict.fromkeys(r["sidecar_path"] for r in rs) if p in blob_of]
        for path in shown_paths:
            text = (reader / "sidecars" / blob_of[path] / path).read_text(encoding="utf-8")
            with contextlib.suppress(tomllib.TOMLDecodeError):
                h = str(tomllib.loads(text).get("experiment", {}).get("hypothesis", "")).strip()
                if h and h not in hyps:
                    hyps.append(h[:300])
        cits = [f"run:{r['id']}" for r in rs] + [f"sidecar:{p}" for p in shown_paths]
        purposes.append(
            {
                "id": f"o{i + 1}",
                "text": " / ".join(hyps) or "work recorded in these runs",
                "citations": cits,
            }
        )
    return {"packet_sha256": packet_sha256, "reader_id": "oracle-from-labels", "purposes": purposes}


def pooled_metrics(packets: list[dict[str, Any]]) -> dict[str, Any]:
    """T7's metrics pooled over readers and packets (owner 261110): sum hits and denominators, then form rates.

    Each packet is ``{"key": answer_key, "readings": [{"gold", "maybe", "comparison"}, ...]}``. Per T7(b), within a
    packet a purpose counts as reported silent only if NO reader maps it (the intersection of the readers' silent
    sets), so silent and false-silent are counted once per packet. Off-charter, revived and gold agreement are counted
    per reading and summed. The majority rule (silent in at least 2 of 3 readers) is reported beside it, descriptive.
    """
    tot: dict[str, int] = {}
    maj_hits = maj_false = 0
    for pk in packets:
        key, readings = pk["key"], pk["readings"]
        if not readings:
            continue
        sets = [_silent_reported(r["comparison"]) for r in readings]
        combined = set.intersection(*sets)
        majority = {p for p in set().union(*sets) if sum(p in s for s in sets) * 2 > len(sets)}
        first = _reading_counts(
            key, readings[0]["gold"], readings[0]["comparison"], readings[0].get("maybe"), combined
        )
        for name in ("silent_hits", "n_silent", "false_silent_hits", "n_present"):
            tot[name] = tot.get(name, 0) + first[name]
        scorable = not key["budget_limited"]
        maj_hits += sum(p in majority for p in (key["silent"] if scorable else []))
        maj_false += sum(p in majority for p in (key["present"] if scorable else []))
        for r in readings:
            c = _reading_counts(key, r["gold"], r["comparison"], r.get("maybe"))
            for name in (
                "off_charter_hits",
                "n_off_charter",
                "revived_hits",
                "n_revived",
                "gold_hits",
                "n_mapped",
                "n_unscored_pairs",
            ):
                tot[name] = tot.get(name, 0) + c[name]
    g = tot.get
    return {
        "silent_recall": _rate(g("silent_hits", 0), g("n_silent", 0)),
        "n_silent": g("n_silent", 0),
        "false_silent": _rate(g("false_silent_hits", 0), g("n_present", 0)),
        "n_present": g("n_present", 0),
        "off_charter_unmapped": _rate(g("off_charter_hits", 0), g("n_off_charter", 0)),
        "n_off_charter": g("n_off_charter", 0),
        "revived_flagged": _rate(g("revived_hits", 0), g("n_revived", 0)),
        "n_revived": g("n_revived", 0),
        "gold_agreement": _rate(g("gold_hits", 0), g("n_mapped", 0)),
        "n_mapped": g("n_mapped", 0),
        "n_unscored_pairs": g("n_unscored_pairs", 0),
        "silent_recall_majority": _rate(maj_hits, g("n_silent", 0)),
        "false_silent_majority": _rate(maj_false, g("n_present", 0)),
    }
