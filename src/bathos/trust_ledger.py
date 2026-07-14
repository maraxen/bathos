"""Durable trust ledger (S3; build seam S3, spec §3.1/§5.1, backlog item 3491).

task_id: 260713_figure-eda-build-dag. This is the promotion state-of-record: the
build seam `bathos.readback.get_trust_state` (S1, item 3482) and
`bathos.readback.list_candidates` were intentional null-stubs pending this module.

Schema (spec §3.1)
-------------------
An append-only ``trust_ledger_record``::

    {run_id, output_path, content_hash, from_state, to_state, attestation_ref,
     amended_at, reason}

Only ``candidate -> promoted`` transitions are ever recorded — this ledger is NOT
the general-purpose ``bathos.compact._AMENDMENTS_TABLE_SCHEMA`` table (that schema
tracks sidecar amendments to a run, an unrelated dead-end for this seam's purposes,
per the dispatch brief). ``trust_state(content_hash)`` is the fold of the ledger,
latest-wins by ``amended_at`` — see :func:`fold_trust_state`.

Durability
----------
Built on the exact same proven substrate as anchors (``bathos.anchor``, gate 2b-A
DE-RISK spike, #3485, merged): a cool-tier immutable Parquet fragment per append
(:func:`write_ledger_fragment`), read back and folded by
:func:`read_ledger_fragments`, and re-ingested into the warm ``trust_ledger`` DuckDB
table on every :func:`bathos.compact.compact` call (:func:`_ingest_ledger_fragments`,
wired into ``compact.py`` the same way ``_ingest_anchor_fragments`` is). Unlike
anchors (which upsert on ``(path, sha256)``), the ledger is genuinely append-only:
every :func:`append_ledger_record` call writes a brand-new fragment with a fresh id,
and re-ingestion is idempotent on that id (skip-if-present), never an update.

The ratchet invariant
---------------------
:func:`graduate_product` is the ONLY function in bathos that appends a
``candidate -> promoted`` record. It independently calls the merged
``bathos.readback.query_attestation`` (S4, backlog #3492) and refuses — raising
:class:`GraduationRefused` — unless a PASS attestation exists for the content_hash
at the requested ``min_strength``. This is spec §5.1 step 4's promise made
mechanical: "nothing reaches ``promoted`` without an evaluator PASS attestation."
The check is independent of whatever ``attestation_ref`` the caller passes in (that
value is recorded for audit purposes only) — a caller cannot bypass the gate by
supplying a plausible-looking but unverified ``attestation_ref``.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from bathos.telemetry import event

_LEDGER_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS trust_ledger (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    output_path TEXT,
    content_hash TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    attestation_ref TEXT,
    amended_at TEXT NOT NULL,
    reason TEXT
)
"""

_LEDGER_FRAGMENTS_DIRNAME = "ledger"

_LEDGER_FRAGMENT_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string()),
        pa.field("run_id", pa.string()),
        pa.field("output_path", pa.string()),
        pa.field("content_hash", pa.string()),
        pa.field("from_state", pa.string()),
        pa.field("to_state", pa.string()),
        pa.field("attestation_ref", pa.string()),
        pa.field("amended_at", pa.string()),
        pa.field("reason", pa.string()),
    ]
)


class GraduationRefused(RuntimeError):
    """Raised by :func:`graduate_product` when the ratchet invariant is not met:
    no PASS attestation exists for the content_hash (at the requested
    ``min_strength``). No ledger record is appended when this is raised."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class TrustLedgerRecord:
    """One append-only ledger entry: a ``from_state -> to_state`` transition for a
    product identified by ``content_hash`` (spec §3.1)."""

    content_hash: str
    from_state: str
    to_state: str
    run_id: str | None = None
    output_path: str | None = None
    attestation_ref: str | None = None
    reason: str | None = None
    amended_at: str = field(default_factory=_now_iso)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


def _ledger_fragments_dir(catalog_dir: Path | str) -> Path:
    return Path(catalog_dir) / _LEDGER_FRAGMENTS_DIRNAME


def write_ledger_fragment(record: TrustLedgerRecord, catalog_dir: Path | str) -> None:
    """Write an immutable cool-tier Parquet fragment for one ledger record.

    Mirrors ``bathos.anchor.write_anchor_fragment`` exactly: one fragment per
    record, atomic tmp-write + POSIX rename. Unlike anchor fragments (keyed by a
    fresh id because the *warm* row upserts on (path, sha256)), ledger fragments
    are keyed by the record's own id and are NEVER superseded on read-back — the
    ledger is append-only by design; :func:`fold_trust_state` (not this write path)
    is what resolves "latest" for a given content_hash.
    """
    frag_dir = _ledger_fragments_dir(catalog_dir)
    frag_dir.mkdir(parents=True, exist_ok=True)
    target = frag_dir / f"ledger_{record.id}.parquet"
    tmp = frag_dir / f"ledger_{record.id}.tmp.parquet"

    t_start = time.monotonic()
    table = pa.table(
        {
            "id": [record.id],
            "run_id": [record.run_id],
            "output_path": [record.output_path],
            "content_hash": [record.content_hash],
            "from_state": [record.from_state],
            "to_state": [record.to_state],
            "attestation_ref": [record.attestation_ref],
            "amended_at": [record.amended_at],
            "reason": [record.reason],
        },
        schema=_LEDGER_FRAGMENT_SCHEMA,
    )
    pq.write_table(table, tmp)
    tmp.rename(target)  # atomic on POSIX
    duration_ms = (time.monotonic() - t_start) * 1000

    event("trust_ledger.write_fragment", path=str(target), rows=1, duration_ms=int(duration_ms))


def read_ledger_fragments(catalog_dir: Path | str) -> list[TrustLedgerRecord]:
    """Read every cool-tier ledger fragment, unfolded (full history, no latest-wins
    collapse — this is the append-only read path used by compact-time re-ingestion
    and by tests asserting append-only-ness). See :func:`fold_trust_state` for the
    latest-wins query path."""
    frag_dir = _ledger_fragments_dir(catalog_dir)
    if not frag_dir.exists():
        return []
    parquet_files = list(frag_dir.glob("ledger_*.parquet"))
    if not parquet_files:
        return []

    tables = [pq.read_table(f) for f in parquet_files]
    combined = pa.concat_tables(tables, promote_options="permissive")
    pydict = combined.to_pydict()

    records = []
    for i in range(combined.num_rows):
        records.append(
            TrustLedgerRecord(
                id=pydict["id"][i],
                run_id=pydict["run_id"][i],
                output_path=pydict["output_path"][i],
                content_hash=pydict["content_hash"][i],
                from_state=pydict["from_state"][i],
                to_state=pydict["to_state"][i],
                attestation_ref=pydict["attestation_ref"][i],
                amended_at=pydict["amended_at"][i],
                reason=pydict["reason"][i],
            )
        )
    return records


def _connect(catalog_dir: Path | str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(Path(catalog_dir) / "bathos.db"))
    con.execute(_LEDGER_TABLE_SCHEMA)
    return con


def _insert_warm_row(record: TrustLedgerRecord, catalog_dir: Path | str) -> None:
    con = _connect(catalog_dir)
    try:
        existing = con.execute(
            "SELECT id FROM trust_ledger WHERE id = ?", [record.id]
        ).fetchone()
        if existing:
            return  # append-only: never update an existing ledger row
        con.execute(
            "INSERT INTO trust_ledger "
            "(id, run_id, output_path, content_hash, from_state, to_state, "
            "attestation_ref, amended_at, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                record.id,
                record.run_id,
                record.output_path,
                record.content_hash,
                record.from_state,
                record.to_state,
                record.attestation_ref,
                record.amended_at,
                record.reason,
            ],
        )
    finally:
        con.close()


def append_ledger_record(
    record: TrustLedgerRecord, catalog_dir: Path | str
) -> TrustLedgerRecord:
    """Durably append one ledger record: cool-tier fragment (durable, survives
    ``compact(force_rebuild=True)``) + warm-tier row (disposable, re-derived from
    cool fragments on every compact via :func:`_ingest_ledger_fragments`).

    This is the single write path both :func:`graduate_product` and tests use —
    there is no separate "plain, non-durable" variant for the ledger (unlike
    ``bathos.anchor``'s ``CatalogAnchorStore``/``DurableAnchorStore`` split):
    promotion state-of-record must always be durable, by definition of what it is.
    """
    write_ledger_fragment(record, catalog_dir)
    _insert_warm_row(record, catalog_dir)
    event(
        "trust_ledger.append",
        content_hash=record.content_hash,
        from_state=record.from_state,
        to_state=record.to_state,
        attestation_ref=record.attestation_ref,
    )
    return record


def latest_ledger_record(
    catalog_dir: Path | str, content_hash: str
) -> TrustLedgerRecord | None:
    """Return the latest-by-``amended_at`` ledger record for ``content_hash``, or
    ``None`` if no record exists. This is the full-record counterpart to
    :func:`fold_trust_state`, which returns only the folded ``to_state`` string."""
    con = _connect(catalog_dir)
    try:
        row = con.execute(
            "SELECT id, run_id, output_path, content_hash, from_state, to_state, "
            "attestation_ref, amended_at, reason FROM trust_ledger "
            "WHERE content_hash = ? ORDER BY amended_at DESC LIMIT 1",
            [content_hash],
        ).fetchone()
        if row is None:
            return None
        return TrustLedgerRecord(
            id=row[0],
            run_id=row[1],
            output_path=row[2],
            content_hash=row[3],
            from_state=row[4],
            to_state=row[5],
            attestation_ref=row[6],
            amended_at=row[7],
            reason=row[8],
        )
    finally:
        con.close()


def fold_trust_state(catalog_dir: Path | str, content_hash: str) -> str | None:
    """Fold the ledger for ``content_hash``, latest-wins by ``amended_at``.

    Returns the latest ``to_state`` (currently always ``"promoted"`` — the ledger
    only ever records promotions, spec §3.1), or ``None`` if no ledger record
    exists for this content_hash at all (the caller, ``bathos.readback.get_trust_state``,
    is responsible for distinguishing "no ledger record" from "unknown product" vs.
    "candidate product" — this function only answers the ledger's own question).
    """
    latest = latest_ledger_record(catalog_dir, content_hash)
    return latest.to_state if latest is not None else None


def graduate_product(
    catalog_dir: Path | str,
    content_hash: str,
    attestation_ref: str,
    *,
    min_strength: str | None = None,
    run_id: str | None = None,
    output_path: str | None = None,
    reason: str | None = None,
) -> TrustLedgerRecord:
    """Graduate a product from ``candidate`` to ``promoted``. The ONLY function in
    bathos that appends a promotion record — enforces the ratchet invariant.

    Independently calls ``bathos.readback.query_attestation(catalog_dir,
    content_hash, min_strength)`` (S4, backlog #3492) and refuses to append
    anything unless it returns a PASS-verdict attestation. This check does not
    trust the caller-supplied ``attestation_ref`` — it re-derives PASS-ness from
    the attestation store every time, so a caller cannot promote by supplying a
    plausible but unverified reference.

    Args:
        catalog_dir: Path to the bathos catalog root.
        content_hash: Content hash of the product to graduate.
        attestation_ref: Caller-supplied reference recorded on the ledger entry
            for audit purposes (e.g. the attestation's own sha256 from a prior
            ``query_attestation`` call). NOT itself trusted as proof — see above.
        min_strength: Minimum attestation strength required, ``"oracle_match"`` or
            ``"repro_floor"``; ``None`` = either strength qualifies (passed through
            to ``query_attestation``).
        run_id: Optional run_id to record on the ledger entry.
        output_path: Optional output_path to record on the ledger entry.
        reason: Optional free-form reason/justification to record.

    Returns:
        The appended :class:`TrustLedgerRecord` (``from_state="candidate"``,
        ``to_state="promoted"``).

    Raises:
        GraduationRefused: If no PASS attestation (at the requested min_strength)
            exists for ``content_hash``. No ledger record is appended.

    Idempotency (debt #640): if ``content_hash`` is already ``promoted`` (per
    :func:`fold_trust_state`), this is a no-op — it returns the EXISTING latest
    ledger record for ``content_hash`` instead of appending a second, distinct
    ``candidate -> promoted`` record. Two prior calls with the same
    ``content_hash`` used to each mint a fresh-UUID ledger record; `fold_trust_state`
    still resolved correctly either way (latest-wins), so this was a data-hygiene
    smell rather than a correctness bug, but callers relying on "call this exactly
    once per graduation" now get that guarantee. The returned record's
    ``attestation_ref``/``run_id``/``output_path``/``reason`` reflect whichever
    call actually performed the promotion, not necessarily this call's arguments.
    """
    existing = latest_ledger_record(catalog_dir, content_hash)
    if existing is not None and existing.to_state == "promoted":
        return existing

    from bathos.readback import query_attestation

    attestation = query_attestation(catalog_dir, content_hash, min_strength=min_strength)
    if attestation is None:
        raise GraduationRefused(
            f"graduate_product refused: no PASS attestation found for "
            f"content_hash={content_hash!r} (min_strength={min_strength!r}) — the "
            "ratchet invariant requires an evaluator PASS attestation before a "
            "candidate -> promoted graduation. Nothing was appended to the ledger."
        )

    record = TrustLedgerRecord(
        content_hash=content_hash,
        from_state="candidate",
        to_state="promoted",
        run_id=run_id,
        output_path=output_path,
        attestation_ref=attestation_ref,
        reason=reason,
    )
    return append_ledger_record(record, catalog_dir)
