#!/usr/bin/env python3
"""Classify deferred handoff entries against the praxia tracker.

Piece B of the deferred-item triage — see
`.praxia/docs/plans/260805_deferred-item-triage-reconcile-the-handoff-backlog.md`.

Handoff YAMLs carry a `deferred:` block whose entries are never reconciled against the
tracker: an item is deferred with a valid tracker reference, the row is later closed, the
handoff still reads `deferred`, and the next session carries it forward as live work. This
script resolves every id-bearing entry against Postgres so the closed ones can be dropped
mechanically, and buckets the rest for human review.

Its JSON output doubles as the labelled ground-truth fixture for validating automatic
reconciliation (debt #1179) — so the classification is written out in full, including the
entries it declines to decide.

WHAT THIS SCRIPT DECIDES AND WHAT IT DOES NOT
---------------------------------------------
Decides (mechanically, no judgement): whether an entry's referenced id resolves in its own
workspace, and whether that row is in a terminal state.

Does NOT decide: whether two entries are duplicates, or whether a `recommended_phase` is a
trigger condition. Both are surfaced as *candidates* with the evidence attached. They are
heuristics, they are reported for a human to confirm, and nothing downstream should treat
them as settled.

Usage::

    uv run python scripts/analysis/classify_deferred_items.py --project bathos
    uv run python scripts/analysis/classify_deferred_items.py --project bathos \\
        --json-out /tmp/classification.json

Postgres access goes through the `psql` binary rather than a driver, to avoid adding a
dependency to bathos for a tooling script. `--tracker-json` accepts a pre-fetched dump
instead, which makes the script runnable without DB access and testable offline.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("classify_deferred")

# Work that finished. Dropping a handoff entry pointing here is unambiguously safe.
DONE_STATUSES = frozenset({"completed", "resolved"})

# Work that stopped without finishing. ALSO terminal for tracking purposes — the tracker says
# it is not live — but semantically very different, and kept separate on purpose.
#
# A live example of why: one entry reads "T7 / #1684 (still OPEN): CI grep guard ..." while
# backlog#1684 is `archived`. Collapsing archived into completed would drop an entry whose own
# text asserts it is open, on the strength of a status that means "we stopped tracking this",
# not "we did this". That is precisely the false-drop this triage is tuned against, so
# abandonment gets its own bucket and its own confirmation step.
ABANDONED_STATUSES = frozenset({"cancelled", "archived"})

TERMINAL_STATUSES = DONE_STATUSES | ABANDONED_STATUSES

TRACKER_TABLES = ("backlog", "tech_debt")

# Connect + statement + subprocess cap for tracker lookups.
#
# Justified rather than arbitrary: the query is a bounded `id IN (...)` over two tables with a
# handful of ids, and returns in milliseconds against the real tracker. 30s leaves roughly three
# orders of magnitude of headroom, so nothing but a genuinely wedged server trips it — which is
# exactly the case we want converted into a fail-closed error instead of an indefinite hang.
DB_TIMEOUT_SECONDS = 30

# Matches a bare "#NNN" reference. Deliberately 2-4 digits: shorter matches sweep up things
# like "#1" in prose, longer ones do not occur in either tracker's id space.
_ID_RE = re.compile(r"#(\d{2,4})")

# Matches an inclusive range written "#793-796" / "#793–796". Without this the plain _ID_RE
# sees only the first endpoint: the corpus contains "Praxia backlog items #793-796", where
# 794/795/796 would silently go unchecked and the entry would be classified on 793 alone.
_ID_RANGE_RE = re.compile(r"#(\d{2,4})\s*[-–]\s*(\d{2,4})")

# An id range wider than this is treated as prose (a line range, a version span), not a list of
# tracker ids. Arbitrary, and only a guard against absurd expansion — ranges in this corpus are
# single-digit-width.
_MAX_RANGE_WIDTH = 20

# Phrases that suggest a `recommended_phase` names a TRIGGER CONDITION rather than a schedule
# position ("When a monorepo user appears", "Opportunistic", "Revisit only on concrete need").
# HEURISTIC, REPORT-ONLY. Assigning a priority to a condition-gated item is a category error,
# but this list cannot prove intent — it flags candidates for a human to confirm.
_CONDITION_MARKERS = (
    "when ",
    "after ",
    "opportunistic",
    "revisit",
    "only on",
    "future major",
    "if ",
    "once ",
    "pending",
)

_TOKEN_RE = re.compile(r"[a-z0-9_]{3,}")


@dataclass
class TrackerRow:
    table: str
    id: int
    workspace_id: str
    status: str
    title: str

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_done(self) -> bool:
        return self.status in DONE_STATUSES


@dataclass
class DeferredEntry:
    source_file: str
    handoff_date: str
    index: int
    description: str
    rationale: str
    recommended_phase: str

    # populated by classification
    referenced_ids: list[int] = field(default_factory=list)
    resolved: list[dict] = field(default_factory=list)
    foreign_workspace_hits: list[dict] = field(default_factory=list)
    category: str = ""
    reason: str = ""
    condition_candidate: bool = False


# ── loading ──────────────────────────────────────────────────────────────────


def load_deferred(handoff_dir: Path, project: str) -> list[DeferredEntry]:
    """Parse every `deferred:` entry out of a project's handoff YAMLs.

    Uses `yaml.safe_load_all`, NOT `safe_load`: handoffs are written with both a leading and a
    trailing `---`, so YAML sees a second, empty document and `safe_load` raises
    `ComposerError: expected a single document in the stream`. No handoff carries two real
    documents, so filtering to dict instances is sufficient.
    """
    entries: list[DeferredEntry] = []
    paths = sorted(handoff_dir.glob(f"{project}_*.yaml"))
    if not paths:
        log.warning("no handoffs matched %s_*.yaml in %s", project, handoff_dir)

    for path in paths:
        date_match = re.search(r"_(\d{8})_", path.name)
        handoff_date = date_match.group(1) if date_match else "unknown"
        try:
            docs = [d for d in yaml.safe_load_all(path.read_text()) if isinstance(d, dict)]
        except yaml.YAMLError as exc:
            log.error("unparseable handoff %s: %s", path.name, exc)
            continue

        for doc in docs:
            for i, item in enumerate(doc.get("deferred") or []):
                if not isinstance(item, dict):
                    continue
                entries.append(
                    DeferredEntry(
                        source_file=path.name,
                        handoff_date=handoff_date,
                        index=i,
                        description=str(item.get("description", "")),
                        rationale=str(item.get("rationale", "")),
                        recommended_phase=str(item.get("recommended_phase", "") or ""),
                    )
                )

    log.info("loaded %d deferred entries from %d handoffs", len(entries), len(paths))
    return entries


def read_workspace_id(praxia_dir: Path) -> str:
    wid = (praxia_dir / "workspace.id").read_text().strip()
    log.info("workspace id: %s", wid)
    return wid


# ── tracker resolution ───────────────────────────────────────────────────────


def fetch_tracker_rows(
    db_url: str, ids: set[int], timeout: int = DB_TIMEOUT_SECONDS
) -> list[TrackerRow]:
    """Fetch candidate rows for `ids` from BOTH tracker tables, across ALL workspaces.

    Deliberately unscoped by workspace: rows belonging to another workspace are what make a
    bare "#NNN" ambiguous, and the caller needs to see them to report the hazard. Scoping the
    query would hide exactly the evidence we are looking for.

    Bounded on both connect and execution. An unresponsive server is a real operating state
    (this one needed a restart on 2026-08-05), and without a timeout the script would block
    indefinitely rather than failing closed — which would defeat the whole point of refusing to
    classify against a tracker it cannot read.
    """
    if not ids:
        return []
    id_list = ",".join(str(int(i)) for i in sorted(ids))
    sql = " UNION ALL ".join(
        f"SELECT '{t}' AS src, id, workspace_id, status, title FROM {t} WHERE id IN ({id_list})"
        for t in TRACKER_TABLES
    )
    env = {
        **os.environ,
        "PGCONNECT_TIMEOUT": str(timeout),
        # Server-side cap too: a client timeout alone leaves a slow query running on the server
        # after we walk away.
        "PGOPTIONS": f"-c statement_timeout={timeout * 1000}",
    }
    try:
        proc = subprocess.run(
            ["psql", db_url, "-t", "-A", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", sql],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        # Routed into the same fail-closed path as a connection error on purpose.
        raise RuntimeError(f"psql timed out after {timeout}s — server may be unresponsive") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed: {proc.stderr.strip()}")

    rows: list[TrackerRow] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            log.warning("skipping malformed psql row: %r", line)
            continue
        src, rid, wid, status, title = parts[0], parts[1], parts[2], parts[3], "\t".join(parts[4:])
        rows.append(TrackerRow(src, int(rid), wid, status, title))
    log.info("fetched %d tracker rows for %d distinct ids", len(rows), len(ids))
    return rows


def load_tracker_json(path: Path) -> list[TrackerRow]:
    """Offline alternative to `fetch_tracker_rows` — same shape, no DB needed."""
    data = json.loads(path.read_text())
    return [TrackerRow(**r) for r in data]


def load_resolutions(path: Path) -> dict[tuple[str, int], dict]:
    """Load confirmed dispositions, keyed by `(source_file, index)`.

    Without this the sweep is not idempotent across sessions: an entry confirmed and disposed
    of by hand would resurface in the same bucket on the next run, and the confirmation work
    would have to be redone from scratch.

    The key is `(source_file, index)` rather than a content hash *because handoffs are never
    edited*. They are snapshots of what was true when written; rewriting one to remove a
    resolved entry would destroy the audit trail this whole triage depends on — and would also
    invalidate every key here. Immutability is what makes the cheap key correct.
    """
    if not path.exists():
        return {}
    doc = yaml.safe_load(path.read_text()) or {}
    out: dict[tuple[str, int], dict] = {}
    for r in doc.get("resolutions") or []:
        out[(r["source_file"], int(r["index"]))] = r
    log.info("loaded %d confirmed resolutions from %s", len(out), path.name)
    return out


# ── classification ───────────────────────────────────────────────────────────


def extract_ids(entry: DeferredEntry) -> list[int]:
    """Every tracker id an entry mentions, ranges expanded.

    Scans `recommended_phase` and `rationale` as well as the description, so an entry whose
    phase reads "next sprint after #791 read-only phase ships" contributes 791 alongside its
    own id. That over-collects on purpose: `classify` only calls an entry droppable when
    *every* id it mentions is terminal, so a stray reference to live work forces the safe
    outcome (keep) rather than the dangerous one (drop).
    """
    blob = f"{entry.description} {entry.rationale} {entry.recommended_phase}"
    ids = {int(m) for m in _ID_RE.findall(blob)}
    for lo_s, hi_s in _ID_RANGE_RE.findall(blob):
        lo, hi = int(lo_s), int(hi_s)
        if 0 < hi - lo <= _MAX_RANGE_WIDTH:
            ids.update(range(lo, hi + 1))
    return sorted(ids)


def looks_conditional(recommended_phase: str) -> bool:
    """HEURISTIC. See `_CONDITION_MARKERS` — flags candidates, decides nothing."""
    low = recommended_phase.lower().strip()
    return any(marker in low for marker in _CONDITION_MARKERS)


def classify(
    entries: list[DeferredEntry],
    rows: list[TrackerRow],
    workspace_id: str,
    resolutions: dict[tuple[str, int], dict] | None = None,
) -> list[DeferredEntry]:
    """Bucket each entry. Id resolution is checked first because it is mechanical."""
    by_id: dict[int, list[TrackerRow]] = {}
    for row in rows:
        by_id.setdefault(row.id, []).append(row)
    resolutions = resolutions or {}

    for entry in entries:
        entry.referenced_ids = extract_ids(entry)
        entry.condition_candidate = looks_conditional(entry.recommended_phase)

        # A hand-confirmed disposition wins over anything inferred. Checked FIRST so a
        # previously-settled entry can never be re-surfaced by a later change to the
        # heuristics — the human decision is the more authoritative signal.
        settled = resolutions.get((entry.source_file, entry.index))
        if settled:
            entry.category = "resolved"
            promoted = settled.get("promoted_to")
            entry.reason = f"confirmed {settled['disposition']} on {settled['confirmed_at']}" + (
                f" -> {promoted}" if promoted else ""
            )
            continue

        own: list[TrackerRow] = []
        foreign: list[TrackerRow] = []
        for rid in entry.referenced_ids:
            for row in by_id.get(rid, []):
                (own if row.workspace_id == workspace_id else foreign).append(row)

        entry.resolved = [asdict(r) for r in own]
        entry.foreign_workspace_hits = [asdict(r) for r in foreign]

        if not entry.referenced_ids:
            # `condition_candidate` is a FLAG, never a category. It describes the *shape* an
            # entry should take if it survives review, and says nothing about whether the work
            # is still live. Letting it terminate classification would route already-shipped
            # work straight to promotion — observed live: "Build-order steps 2-5" matched on
            # its phase "Implementation, after corpus v1 lands" despite having shipped.
            entry.category = "needs_review"
            entry.reason = "no id reference; requires premise verification"
            if entry.condition_candidate:
                entry.reason += (
                    f" — and if it survives, promote as conditional: {entry.recommended_phase!r}"
                )
        elif not own:
            entry.category = "id_unresolved_in_workspace"
            entry.reason = f"ids {entry.referenced_ids} do not resolve in this workspace" + (
                f"; {len(foreign)} row(s) with the same id exist in OTHER workspaces "
                "— do not trust them, the id is ambiguous"
                if foreign
                else ""
            )
        elif all(r.is_terminal for r in own):
            detail = "; ".join(f"{r.table}#{r.id} is {r.status}" for r in own)
            if all(r.is_done for r in own):
                entry.category = "closed_done_by_id"
                entry.reason = f"{detail} — mechanically droppable"
            else:
                entry.category = "closed_abandoned_by_id"
                entry.reason = (
                    f"{detail} — stopped, not finished; confirm the abandonment was "
                    "intentional before dropping"
                )
        else:
            live = [r for r in own if not r.is_terminal]
            entry.category = "open_by_id"
            entry.reason = (
                "; ".join(f"{r.table}#{r.id} is {r.status}" for r in live)
                + " — already tracked and live, keep"
            )

    return entries


# ── duplicate candidates (report-only) ───────────────────────────────────────


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def find_duplicate_candidates(entries: list[DeferredEntry], threshold: float) -> list[dict]:
    """Cluster entries by token-overlap (Jaccard) so restatements can be spotted.

    THE THRESHOLD IS ARBITRARY. There is no principled basis for any particular value here,
    and none is claimed: it exists to surface candidates at a workable signal-to-noise ratio
    for a human to confirm. This function NEVER merges anything, and its output must not be
    consumed as a dedupe decision. Tune with --dupe-threshold and re-read the clusters.
    """
    clusters: list[dict] = []
    used: set[int] = set()
    for i, a in enumerate(entries):
        if i in used:
            continue
        ta = _tokens(a.description)
        if not ta:
            continue
        members = [i]
        for j in range(i + 1, len(entries)):
            if j in used:
                continue
            tb = _tokens(entries[j].description)
            if not tb:
                continue
            jaccard = len(ta & tb) / len(ta | tb)
            if jaccard >= threshold:
                members.append(j)
        if len(members) > 1:
            used.update(members)
            clusters.append(
                {
                    "size": len(members),
                    "threshold_used": threshold,
                    "members": [
                        {
                            "handoff_date": entries[m].handoff_date,
                            "description": entries[m].description[:110],
                            "category": entries[m].category,
                        }
                        for m in members
                    ],
                }
            )
    return clusters


# ── reporting ────────────────────────────────────────────────────────────────

_CATEGORY_ORDER = [
    "resolved",
    "closed_done_by_id",
    "closed_abandoned_by_id",
    "open_by_id",
    "id_unresolved_in_workspace",
    "needs_review",
]

_CATEGORY_ACTION = {
    "resolved": "SETTLED — confirmed by hand, nothing to do",
    "closed_done_by_id": "DROP — mechanical, no judgement needed",
    "closed_abandoned_by_id": "DROP after confirming the abandonment was intentional",
    "open_by_id": "KEEP — already tracked and live",
    "id_unresolved_in_workspace": "INVESTIGATE — id is wrong or ambiguous",
    "needs_review": "PREMISE-VERIFY then promote or close",
}


def render_report(entries: list[DeferredEntry], clusters: list[dict]) -> str:
    out: list[str] = []
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.category] = counts.get(e.category, 0) + 1

    out.append(f"Deferred entries classified: {len(entries)}\n")
    out.append(f"{'category':<28} {'n':>3}  action")
    out.append("-" * 92)
    for cat in _CATEGORY_ORDER:
        if cat in counts:
            out.append(f"{cat:<28} {counts[cat]:>3}  {_CATEGORY_ACTION[cat]}")
    out.append("")

    mechanical = counts.get("closed_done_by_id", 0)
    conditional = sum(1 for e in entries if e.condition_candidate)
    if entries:
        pct = 100 * mechanical / len(entries)
        out.append(
            f"Mechanically settled (closed_done_by_id): {mechanical}/{len(entries)} ({pct:.0f}%)"
        )
        out.append(f"Flagged conditional (shape hint only — does NOT skip review): {conditional}\n")

    for cat in _CATEGORY_ORDER:
        members = [e for e in entries if e.category == cat]
        if not members:
            continue
        out.append(f"\n=== {cat} ({len(members)}) — {_CATEGORY_ACTION[cat]}")
        for e in members:
            flag = " [conditional]" if e.condition_candidate else ""
            out.append(f"  [{e.handoff_date}]{flag} {e.description[:96]}")
            out.append(f"      -> {e.reason}")
            if e.foreign_workspace_hits:
                for h in e.foreign_workspace_hits:
                    out.append(
                        f"      !! AMBIGUOUS: {h['table']}#{h['id']} in workspace "
                        f"{h['workspace_id'][:16]}… = {h['title'][:52]!r}"
                    )

    if clusters:
        out.append(
            f"\n\n=== duplicate CANDIDATES ({len(clusters)} clusters) — "
            "heuristic, confirm by hand, nothing merged"
        )
        for c in clusters:
            out.append(f"  cluster of {c['size']} (jaccard >= {c['threshold_used']}):")
            for m in c["members"]:
                out.append(f"      [{m['handoff_date']}] {m['description']}")

    return "\n".join(out) + "\n"


# ── entrypoint ───────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project", default="bathos", help="handoff filename prefix (default: bathos)")
    ap.add_argument(
        "--praxia-dir",
        type=Path,
        default=Path(".praxia"),
        help="path to the .praxia directory (default: .praxia)",
    )
    ap.add_argument(
        "--db-url",
        default="postgresql:///praxia",
        help="Postgres URL passed to psql (default: postgresql:///praxia)",
    )
    ap.add_argument(
        "--tracker-json",
        type=Path,
        help="read tracker rows from a JSON dump instead of psql (offline / testing)",
    )
    ap.add_argument(
        "--no-db",
        action="store_true",
        help="skip tracker resolution entirely; every id-bearing entry lands in needs_review",
    )
    ap.add_argument(
        "--dupe-threshold",
        type=float,
        default=0.5,
        help=(
            "Jaccard token-overlap for flagging duplicate CANDIDATES (default: 0.5). "
            "Arbitrary by construction — it tunes signal-to-noise for human review and is "
            "never a merge decision."
        ),
    )
    ap.add_argument(
        "--resolutions",
        type=Path,
        default=None,
        help=(
            "YAML of hand-confirmed dispositions (default: <praxia-dir>/deferred_resolutions.yaml). "
            "Entries listed there are reported as `resolved` and excluded from the active buckets."
        ),
    )
    ap.add_argument(
        "--db-timeout",
        type=int,
        default=DB_TIMEOUT_SECONDS,
        help=(
            f"seconds to allow for tracker connect + query (default: {DB_TIMEOUT_SECONDS}). "
            "Exceeding it fails closed rather than hanging."
        ),
    )
    ap.add_argument("--json-out", type=Path, help="write the full classification as JSON")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )

    handoff_dir = args.praxia_dir / "handoffs"
    if not handoff_dir.is_dir():
        log.error("no handoff directory at %s", handoff_dir)
        return 2

    entries = load_deferred(handoff_dir, args.project)
    if not entries:
        log.error("no deferred entries found — nothing to classify")
        return 2

    workspace_id = read_workspace_id(args.praxia_dir)

    all_ids: set[int] = set()
    for e in entries:
        all_ids.update(extract_ids(e))

    rows: list[TrackerRow] = []
    if args.no_db:
        log.warning("--no-db: skipping tracker resolution")
    elif args.tracker_json:
        rows = load_tracker_json(args.tracker_json)
    else:
        try:
            rows = fetch_tracker_rows(args.db_url, all_ids, timeout=args.db_timeout)
        except (RuntimeError, FileNotFoundError) as exc:
            # Fail closed: an unreachable tracker must never look like "resolves to nothing",
            # which would read as droppable. Same rule the automatic rollout needs (debt #1179).
            log.error("tracker unreachable (%s) — refusing to classify against a missing DB", exc)
            log.error("re-run with --no-db to bucket everything as needs_review, or fix access")
            return 3

    res_path = args.resolutions or (args.praxia_dir / "deferred_resolutions.yaml")
    resolutions = load_resolutions(res_path)

    classify(entries, rows, workspace_id, resolutions)
    clusters = find_duplicate_candidates(entries, args.dupe_threshold)

    print(render_report(entries, clusters))

    if args.json_out:
        payload = {
            "workspace_id": workspace_id,
            "project": args.project,
            "entry_count": len(entries),
            "dupe_threshold": args.dupe_threshold,
            "entries": [asdict(e) for e in entries],
            "duplicate_candidates": clusters,
        }
        args.json_out.write_text(json.dumps(payload, indent=2))
        log.info("wrote %s", args.json_out)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
