"""Reading record and charter comparison (spec 261104, Design item 5; slice 2 plan C3).

``record_reading`` stores a blind reader's cited reading after resolving every citation against
the packet it was written from. A v2 reading (AC-5b) also carries typed ``links`` between its
purposes and open lines, and an optional ``uncertain`` / ``uncertain_reason`` on every node. ``compare_reading`` takes a mapping written by a *different*
agent (the mapper) and computes ``mapped`` / ``unmapped`` / ``silent`` items against the
confirmed charter. Bathos only checks completeness and validity; it never judges text.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from bathos.bearings.packet import canonical_json, verify_packet
from bathos.errors import (
    BearingsMapperIsReaderError,
    BearingsMappingInvalidError,
    BearingsNoConfirmedCharterError,
    BearingsPacketMismatchError,
    BearingsReadingInvalidError,
    BearingsReadingNotFoundError,
    CharterNotFoundError,
    StepBackInsufficientEvidenceError,
)

logger = logging.getLogger(__name__)

MIN_PURPOSE_CITATIONS = 2  # spec AC-5: at least 2 distinct resolvable citations per purpose
MIN_OPEN_LINE_CITATIONS = 1  # plan Q15
MIN_LINK_CITATIONS = 1  # spec AC-5b

# Spec Design item 5, v2 (owner 261105). feeds and serves bind lines into one group; shares and
# competes record relations that do not.
LINK_TYPES = ("feeds", "serves", "shares", "competes")
_OPEN_REF_RE = re.compile(r"^open:(\d+)$")

RESOLVED = "resolved"
FLAG = "flag_for_review"
UNRESOLVABLE = "unresolvable"

_DOC_RE = re.compile(r"^doc:(?P<path>.+)@(?P<blob>[0-9a-f]{40,64})#L(?P<x>\d+)-L(?P<y>\d+)$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")


class _PacketView:
    """What a citation may resolve against: only the contents of ``reader/``."""

    def __init__(self, packet_dir: Path):
        reader = packet_dir / "reader"
        self.reader = reader
        self.index = json.loads((reader / "index.json").read_text(encoding="utf-8"))
        # split("\n"), not splitlines(): a commit subject may hold \x0b, \x85 or  .
        self.run_ids = [
            str(json.loads(line)["id"])
            for line in (reader / "runs.jsonl").read_text(encoding="utf-8").split("\n")
            if line
        ]
        self.git_shas = [
            str(json.loads(line)["sha"])
            for line in (reader / "git_log.jsonl").read_text(encoding="utf-8").split("\n")
            if line
        ]
        self.docs = {d["path"]: d["blob_sha"] for d in self.index["docs"]}
        self.sidecar_paths = {s["path"] for s in self.index["sidecars"]}

    def line_count(self, path: str) -> int:
        return len((self.reader / "docs" / path).read_text(encoding="utf-8").splitlines())


def _unique_prefix(value: str, universe: list[str], min_len: int) -> str | None:
    if value in universe:
        return value
    if len(value) < min_len:
        return None
    hits = [u for u in universe if u.startswith(value)]
    return hits[0] if len(hits) == 1 else None


def resolve_citation(citation: Any, view: _PacketView) -> tuple[str, str | None]:
    """Return ``(resolution, canonical_key)``; the key is set only for ``resolved`` citations."""
    if isinstance(citation, dict):
        if citation.get("kind") == "atom":
            return FLAG, None
        return UNRESOLVABLE, None
    if not isinstance(citation, str):
        return UNRESOLVABLE, None
    if citation.startswith("atom:"):
        return FLAG, None  # parsed leniently: a malformed atom never fails the reading
    kind, _, rest = citation.partition(":")
    if kind == "run" and rest:
        rid = _unique_prefix(rest, view.run_ids, 8)
        return (RESOLVED, f"run:{rid}") if rid else (UNRESOLVABLE, None)
    if kind == "sidecar" and rest:
        path = rest.split("#", 1)[0]
        return (RESOLVED, f"sidecar:{rest}") if path in view.sidecar_paths else (UNRESOLVABLE, None)
    if kind == "git" and rest:
        if len(rest) < 7 or not _HEX_RE.match(rest):
            return UNRESOLVABLE, None
        sha = _unique_prefix(rest, view.git_shas, 7)
        return (RESOLVED, f"git:{sha}") if sha else (UNRESOLVABLE, None)
    if kind == "doc":
        m = _DOC_RE.match(citation)
        if not m:
            return UNRESOLVABLE, None
        path, blob, x, y = m["path"], m["blob"], int(m["x"]), int(m["y"])
        if view.docs.get(path) != blob:
            return UNRESOLVABLE, None
        if not 1 <= x <= y <= view.line_count(path):
            return UNRESOLVABLE, None
        return RESOLVED, citation
    return UNRESOLVABLE, None


def _require_list(obj: Any, what: str) -> list:
    if not isinstance(obj, list):
        raise BearingsReadingInvalidError(f"{what} must be a list")
    return obj


def _load_reading(reading_file: Path) -> dict[str, Any]:
    try:
        reading = json.loads(Path(reading_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BearingsReadingInvalidError(f"Cannot read reading file {reading_file}: {e}") from e
    if not isinstance(reading, dict):
        raise BearingsReadingInvalidError("Reading must be a JSON object")
    for key in ("packet_sha256", "reader_id"):
        if not isinstance(reading.get(key), str) or not reading[key].strip():
            raise BearingsReadingInvalidError(f"Reading missing required string '{key}'")
    purposes = _require_list(reading.get("purposes"), "'purposes'")
    open_lines = _require_list(reading.get("open_lines", []), "'open_lines'")
    seen: set[str] = set()
    for p in purposes:
        if not isinstance(p, dict) or not isinstance(p.get("id"), str) or not p["id"].strip():
            raise BearingsReadingInvalidError("Every purpose needs a non-empty string 'id'")
        if p["id"] in seen:
            raise BearingsReadingInvalidError(f"Duplicate purpose id {p['id']!r}")
        seen.add(p["id"])
        if not isinstance(p.get("text"), str) or not p["text"].strip():
            raise BearingsReadingInvalidError(f"Purpose {p['id']!r} needs non-empty 'text'")
        _require_list(p.get("citations"), f"purpose {p['id']!r} 'citations'")
    for i, ol in enumerate(open_lines):
        if (
            not isinstance(ol, dict)
            or not isinstance(ol.get("text"), str)
            or not ol["text"].strip()
        ):
            raise BearingsReadingInvalidError(f"Open line {i} needs non-empty 'text'")
        _require_list(ol.get("citations"), f"open line {i} 'citations'")
    for label, node in [(f"purpose {p['id']!r}", p) for p in purposes] + [
        (f"open line {i}", ol) for i, ol in enumerate(open_lines)
    ]:
        _check_uncertainty(node, label)
    _check_links(_require_list(reading.get("links", []), "'links'"), seen, len(open_lines))
    return reading


def _check_uncertainty(node: dict[str, Any], label: str) -> None:
    """The reader's own doubt channel (AC-5b): optional, typed, stored verbatim."""
    if "uncertain" in node and not isinstance(node["uncertain"], bool):
        raise BearingsReadingInvalidError(f"{label} 'uncertain' must be true or false")
    reason = node.get("uncertain_reason")
    if reason is not None and not isinstance(reason, str):
        raise BearingsReadingInvalidError(f"{label} 'uncertain_reason' must be text or null")


def _check_links(links: list, purpose_ids: set[str], n_open: int) -> None:
    """Validate the graph's shape: endpoints exist, no self-links, known types. Bathos never judges a link."""
    for i, link in enumerate(links):
        if not isinstance(link, dict):
            raise BearingsReadingInvalidError(f"Link {i} must be an object")
        if link.get("type") not in LINK_TYPES:
            raise BearingsReadingInvalidError(
                f"Link {i} type {link.get('type')!r} is not one of {', '.join(LINK_TYPES)}"
            )
        for end in ("from", "to"):
            ref = link.get(end)
            m = _OPEN_REF_RE.match(ref) if isinstance(ref, str) else None
            known = isinstance(ref, str) and (
                ref in purpose_ids or (m is not None and int(m[1]) < n_open)
            )
            if not known:
                raise BearingsReadingInvalidError(
                    f"Link {i} '{end}' names {ref!r}, which is not a purpose id or open:<i>"
                )
        if link["from"] == link["to"]:
            raise BearingsReadingInvalidError(f"Link {i} links {link['from']!r} to itself")
        if "uncertain" in link and not isinstance(link["uncertain"], bool):
            raise BearingsReadingInvalidError(f"Link {i} 'uncertain' must be true or false")
        _require_list(link.get("citations"), f"link {i} 'citations'")


def _resolve_all(
    items: list[dict[str, Any]], view: _PacketView
) -> tuple[list[dict[str, Any]], list[int]]:
    """Resolve each item's citations; return stored items and the count of distinct resolved keys."""
    stored: list[dict[str, Any]] = []
    counts: list[int] = []
    for item in items:
        cits = []
        keys: set[str] = set()
        for c in item["citations"]:
            res, key = resolve_citation(c, view)
            cits.append({"citation": c, "resolution": res})
            if key:
                keys.add(key)
        stored.append({**item, "citations": cits})
        counts.append(len(keys))
    return stored, counts


def reading_id_of(reading: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(reading).encode("utf-8")).hexdigest()[:16]


def _existing_reading(cat_dir: Path, slug: str, reading_id: str) -> dict[str, Any] | None:
    from bathos.index import catalog_readable, connect_read

    if not catalog_readable(cat_dir):
        return None
    con = connect_read(cat_dir)
    try:
        row = con.execute(
            "SELECT eid, packet_sha256, ref_sha, since, until, reader_id, purposes_json, open_lines_json "
            "FROM bearings_readings WHERE project = ? AND reading_id = ? ORDER BY ts, eid LIMIT 1",
            [slug, reading_id],
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    return {
        "eid": row[0],
        "packet_sha256": row[1],
        "ref_sha": row[2],
        "since": row[3],
        "until": row[4],
        "reader_id": row[5],
        "purposes": json.loads(row[6] or "[]"),
        "open_lines": json.loads(row[7] or "[]"),
    }


def record_reading(
    project_root: Path | str,
    *,
    reading_file: Path | str,
    packet_dir: Path | str,
    catalog_dir: Path | None = None,
) -> dict[str, Any]:
    """Validate and store a blind reading. Atomic: any failing purpose appends nothing."""
    from bathos.charter import _resolve_catalog_dir, _resolve_project_slug, emit_ledger_event

    root = Path(project_root)
    reading = _load_reading(Path(reading_file))
    packet_sha = verify_packet(packet_dir)
    if reading["packet_sha256"] != packet_sha:
        raise BearingsPacketMismatchError(
            f"Reading names packet {reading['packet_sha256'][:12]} but the packet at {packet_dir} is {packet_sha[:12]}"
        )
    view = _PacketView(Path(packet_dir))

    purposes, p_counts = _resolve_all(reading["purposes"], view)
    open_lines, o_counts = _resolve_all(reading.get("open_lines", []), view)
    failures = [
        f"purpose {p['id']!r} has {n} resolvable citation(s), needs {MIN_PURPOSE_CITATIONS}"
        for p, n in zip(purposes, p_counts, strict=True)
        if n < MIN_PURPOSE_CITATIONS
    ] + [
        f"open line {i} has {n} resolvable citation(s), needs {MIN_OPEN_LINE_CITATIONS}"
        for i, n in enumerate(o_counts)
        if n < MIN_OPEN_LINE_CITATIONS
    ]
    if failures:
        raise StepBackInsufficientEvidenceError(
            "Reading rejected, nothing recorded: " + "; ".join(failures)
        )
    links, l_counts = _resolve_all(reading.get("links", []), view)
    bad_links = [i for i, n in enumerate(l_counts) if n < MIN_LINK_CITATIONS]
    if bad_links:
        raise BearingsReadingInvalidError(
            "Reading rejected, nothing recorded: link(s) "
            + ", ".join(str(i) for i in bad_links)
            + f" need {MIN_LINK_CITATIONS} resolvable citation(s)"
        )

    rid = reading_id_of(reading)
    slug = _resolve_project_slug(root)
    cat_dir = _resolve_catalog_dir(catalog_dir)
    if _existing_reading(cat_dir, slug, rid) is not None:
        return {
            "ok": True,
            "status": "already_recorded",
            "reading_id": rid,
            "packet_sha256": packet_sha,
        }

    emit_ledger_event(
        kind="bearings.reading_recorded",
        slug=slug,
        entity=[slug, rid],
        data={
            "reading_id": rid,
            "packet_sha256": packet_sha,
            "ref_sha": view.index["ref_sha"],
            "since": view.index["since"],
            "until": view.index["until"],
            "reader_id": reading["reader_id"],
            "purposes": purposes,
            "open_lines": open_lines,
            "links": links,
        },
        root=root,
        cat_dir=cat_dir,
    )
    return {
        "ok": True,
        "status": "recorded",
        "reading_id": rid,
        "packet_sha256": packet_sha,
        "n_purposes": len(purposes),
        "n_open_lines": len(open_lines),
        "n_links": len(links),
    }


def _item_id(reading_id: str, charter_sha: str, kind: str, subject: Any) -> str:
    return hashlib.sha256(
        canonical_json([reading_id, charter_sha, kind, subject]).encode("utf-8")
    ).hexdigest()[:16]


def _load_mapping(mapping_file: Path) -> tuple[str, dict[str, Any]]:
    try:
        data = json.loads(Path(mapping_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BearingsMappingInvalidError(f"Cannot read mapping file {mapping_file}: {e}") from e
    if not isinstance(data, dict):
        raise BearingsMappingInvalidError("Mapping file must be a JSON object")
    mapper_id = data.get("mapper_id")
    if not isinstance(mapper_id, str) or not mapper_id.strip():
        raise BearingsMappingInvalidError("Mapping missing required string 'mapper_id'")
    mapping = data.get("mapping")
    if not isinstance(mapping, dict):
        raise BearingsMappingInvalidError("Mapping missing required object 'mapping'")
    return mapper_id, mapping


def compare_reading(
    project_root: Path | str,
    *,
    reading_id: str,
    mapping_file: Path | str,
    catalog_dir: Path | None = None,
) -> dict[str, Any]:
    """Compute mapped / unmapped / silent items from a mapper's mapping onto the confirmed charter."""
    from bathos.charter import (
        _resolve_catalog_dir,
        _resolve_project_slug,
        emit_ledger_event,
        status,
    )
    from bathos.index import connect_read

    root = Path(project_root)
    slug = _resolve_project_slug(root)
    cat_dir = _resolve_catalog_dir(catalog_dir)

    reading = _existing_reading(cat_dir, slug, reading_id)
    if reading is None:
        raise BearingsReadingNotFoundError(
            f"No recorded reading {reading_id!r} for project {slug!r}"
        )
    mapper_id, mapping = _load_mapping(Path(mapping_file))
    if mapper_id == reading["reader_id"]:
        raise BearingsMapperIsReaderError(
            f"mapper_id {mapper_id!r} equals the reader_id; the mapping must come from a different agent"
        )

    try:
        st = status(root, catalog_dir=cat_dir)
    except CharterNotFoundError as e:
        raise BearingsNoConfirmedCharterError(f"No charter file to compare against: {e}") from e
    if st["state"] != "confirmed":
        raise BearingsNoConfirmedCharterError(
            f"Charter is {st['state']}; the owner must confirm it before a reading can be compared"
        )
    con = connect_read(cat_dir)
    try:
        row = con.execute(
            "SELECT charter_sha256, purposes_json FROM charter_confirmations WHERE project = ? "
            "ORDER BY ts DESC, eid DESC LIMIT 1",
            [slug],
        ).fetchone()
    finally:
        con.close()
    charter_sha = str(row[0])
    charter_purposes: dict[str, dict[str, Any]] = json.loads(row[1] or "{}")

    reading_ids = [p["id"] for p in reading["purposes"]]
    missing = sorted(set(reading_ids) - set(mapping))
    extra = sorted(set(mapping) - set(reading_ids))
    if missing or extra:
        raise BearingsMappingInvalidError(
            f"Mapping must cover every reading purpose exactly once; missing={missing} unknown={extra}"
        )
    for rp, target in mapping.items():
        if target is not None and (not isinstance(target, str) or target not in charter_purposes):
            raise BearingsMappingInvalidError(
                f"Mapping for {rp!r} names {target!r}, which is not a confirmed charter purpose id"
            )

    items: list[dict[str, Any]] = []

    def add(
        kind: str,
        key: str,
        subject: Any,
        target: str | None = None,
        matched_retired: str | None = None,
        **extra_fields: Any,
    ) -> None:
        # The target is part of the identity: r1->P1 and r1->P2 are different claims, and an
        # owner's label on one must not attach to the other.
        items.append(
            {
                "item_id": _item_id(reading_id, charter_sha, kind, [subject, target]),
                "kind": kind,
                key: subject,
                "matched_retired": matched_retired,
                **extra_fields,
            }
        )

    mapped_live: set[str] = set()
    for rp in reading_ids:
        target = mapping[rp]
        if target is None:
            add("unmapped", "reading_purpose_id", rp)
        elif charter_purposes[target].get("state") == "retired":
            add("unmapped", "reading_purpose_id", rp, target=target, matched_retired=target)
        else:
            mapped_live.add(target)
            add("mapped", "reading_purpose_id", rp, target=target, charter_purpose_id=target)
    for i in range(len(reading["open_lines"])):
        add("unmapped", "open_line_index", i)
    for pid in sorted(charter_purposes):
        if charter_purposes[pid].get("state") == "live" and pid not in mapped_live:
            add("silent", "charter_purpose_id", pid)

    counts = {
        k: sum(1 for it in items if it["kind"] == k) for k in ("mapped", "unmapped", "silent")
    }
    result = {
        "ok": True,
        "reading_id": reading_id,
        "charter_sha256": charter_sha,
        "counts": counts,
        "items": items,
    }

    # The same mapper repeating the same comparison is a no-op. Only a changed comparison
    # supersedes the current one (the fold keeps the latest per reading).
    con = connect_read(cat_dir)
    try:
        current = con.execute(
            "SELECT item_id, mapper_id FROM bearings_items WHERE project = ? AND reading_id = ?",
            [slug, reading_id],
        ).fetchall()
    finally:
        con.close()
    if (
        current
        and {r[1] for r in current} == {mapper_id}
        and {r[0] for r in current} == {it["item_id"] for it in items}
    ):
        return {**result, "status": "already_compared"}

    emit_ledger_event(
        kind="bearings.compared",
        slug=slug,
        entity=[slug, reading_id],
        data={
            "reading_id": reading_id,
            "mapper_id": mapper_id,
            "charter_sha256": charter_sha,
            "items": items,
        },
        root=root,
        cat_dir=cat_dir,
    )
    return {**result, "status": "compared"}
