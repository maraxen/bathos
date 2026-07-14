"""Anchor-insert WRITE seam (S2).

Backlog item 3483, task 260713_figure-eda-build-dag, foundation seam "2a" of the
figure/EDA coordination system. The intended spec reference is
`.praxia/docs/specs/260713_figure-eda-coordination-system.md` (maraxiom repo) §3
preamble — that file was not present in this workspace at implementation time; this
module was built directly from the dispatch brief plus the existing S1 read-back seam
(`bathos.readback`, item 3482) as the composition target. See the PR description for
this ambiguity flagged for follow-up.

This generalizes bathos's existing claim-anchor pattern
(:func:`bathos.claim.register_claim`, which anchors a ``claim.bth.toml`` file by
``(claim_path, claim_sha256)`` into the ``campaigns`` table) to an arbitrary
out-of-catalog sidecar: given a sidecar file's path and the SHA256 of its contents
(plus a free-form ``kind``/optional ``label``/``content_hash``/``campaign_id``), this
module anchors it so later callers can look it up by identity.

Adapter contract
-----------------
:class:`AnchorStore` is a narrow two-and-a-half-method protocol (``insert``, ``get``,
``find``). All writes/reads in this module go through :func:`get_anchor_store`, which
is the single seam an unfavorable unknown-3 resolution (durability / cross-boundary —
gates #3485/#3486, *not* decided by this seam) would swap to a different backing
implementation (e.g. a networked service, an append-only log) without touching call
sites. :func:`register_anchor` / :func:`get_anchor` / :func:`find_anchors` also accept
an explicit ``store=`` override, which is how the test suite proves the seam is
swappable (:class:`InMemoryAnchorStore` satisfies the identical contract).

What this seam does NOT guarantee
-----------------------------------
- **Durability across a warm-cache force-rebuild.** :class:`CatalogAnchorStore`
  persists into the same DuckDB file as the rest of the warm-tier catalog
  (``<catalog_dir>/bathos.db``), but the ``sidecar_anchors`` table it creates has no
  cool-tier (Parquet) fragment backing it, unlike ``runs``. A
  ``bathos.compact.compact(catalog_dir, force_rebuild=True)`` call deletes and
  recreates ``bathos.db`` from cool-tier fragments only, which wipes any anchors
  previously inserted. This is intentional: durability is a separate gate (2b-A,
  #3485), not decided here.
- **Cross-boundary callability.** No guarantee that an anchor recorded against one
  ``catalog_dir`` is visible to a different ``catalog_dir``, a different host, or a
  caller outside this process's filesystem access to the catalog. This is a separate
  gate (2b-B, #3486), not decided here.

Composition with the S1 read-back seam
----------------------------------------
``bathos.readback.figure_lookup`` (item 3482) was a null-stub pending the figure
registry (build seam S7 in that module's original docstring). This seam gives it a
real, if minimal, backing store: figures anchored here with ``kind="figure"`` become
visible through ``figure_lookup`` immediately, without waiting for a dedicated S7
build. See ``bathos.readback.figure_lookup`` and ``tests/test_readback.py``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

import duckdb

_ANCHORS_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sidecar_anchors (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    kind TEXT NOT NULL,
    label TEXT,
    content_hash TEXT,
    campaign_id TEXT,
    anchored_at TEXT NOT NULL,
    UNIQUE (path, sha256)
)
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class AnchorRecord:
    """One anchored sidecar: identity is (path, sha256)."""

    path: str
    sha256: str
    kind: str
    label: str | None = None
    content_hash: str | None = None
    campaign_id: str | None = None
    anchored_at: str = field(default_factory=_now_iso)


@runtime_checkable
class AnchorStore(Protocol):
    """Adapter contract for the anchor-insert WRITE seam.

    Exactly three operations. Any implementation satisfying this contract is
    swappable behind :func:`register_anchor` / :func:`get_anchor` / :func:`find_anchors`
    without changing call sites — this is the seam a durability or cross-boundary
    resolution (#3485/#3486) would swap.
    """

    def insert(self, record: AnchorRecord) -> AnchorRecord:
        """Insert (or upsert, on re-anchor of the same (path, sha256)) a record."""
        ...

    def get(self, path: str, sha256: str) -> AnchorRecord | None:
        """Look up a single anchor by its (path, sha256) identity."""
        ...

    def find(
        self,
        *,
        kind: str | None = None,
        sha256: str | None = None,
        content_hash: str | None = None,
        campaign_id: str | None = None,
    ) -> list[AnchorRecord]:
        """Find anchors matching all given (non-None) filters (AND semantics)."""
        ...


class InMemoryAnchorStore:
    """Alternate AnchorStore implementation: an in-process dict, no catalog, no
    DuckDB, no durability of any kind (cleared when the process exits or the object
    is garbage collected). Exists to prove the adapter seam is swappable — it must
    satisfy the exact same contract as CatalogAnchorStore (see
    tests/test_anchor.py::TestAlternateImplSatisfiesContract)."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], AnchorRecord] = {}

    def insert(self, record: AnchorRecord) -> AnchorRecord:
        self._records[(record.path, record.sha256)] = record
        return record

    def get(self, path: str, sha256: str) -> AnchorRecord | None:
        return self._records.get((path, sha256))

    def find(
        self,
        *,
        kind: str | None = None,
        sha256: str | None = None,
        content_hash: str | None = None,
        campaign_id: str | None = None,
    ) -> list[AnchorRecord]:
        out = []
        for record in self._records.values():
            if kind is not None and record.kind != kind:
                continue
            if sha256 is not None and record.sha256 != sha256:
                continue
            if content_hash is not None and record.content_hash != content_hash:
                continue
            if campaign_id is not None and record.campaign_id != campaign_id:
                continue
            out.append(record)
        return out


class CatalogAnchorStore:
    """Real AnchorStore implementation: DuckDB warm-tier catalog-backed.

    Persists into ``<catalog_dir>/bathos.db`` (the same file the rest of the warm
    catalog uses), in a dedicated ``sidecar_anchors`` table created lazily on first
    use. NOT durable across a warm-cache force-rebuild — see module docstring.
    """

    def __init__(self, catalog_dir: Path | str) -> None:
        self._catalog_dir = Path(catalog_dir)
        self._db_path = self._catalog_dir / "bathos.db"

    def _connect(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(str(self._db_path))
        con.execute(_ANCHORS_TABLE_SCHEMA)
        return con

    def insert(self, record: AnchorRecord) -> AnchorRecord:
        con = self._connect()
        try:
            existing = con.execute(
                "SELECT id FROM sidecar_anchors WHERE path = ? AND sha256 = ?",
                [record.path, record.sha256],
            ).fetchone()
            if existing:
                con.execute(
                    "UPDATE sidecar_anchors SET kind = ?, label = ?, content_hash = ?, "
                    "campaign_id = ?, anchored_at = ? WHERE id = ?",
                    [
                        record.kind,
                        record.label,
                        record.content_hash,
                        record.campaign_id,
                        record.anchored_at,
                        existing[0],
                    ],
                )
            else:
                con.execute(
                    "INSERT INTO sidecar_anchors "
                    "(id, path, sha256, kind, label, content_hash, campaign_id, anchored_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        str(uuid.uuid4()),
                        record.path,
                        record.sha256,
                        record.kind,
                        record.label,
                        record.content_hash,
                        record.campaign_id,
                        record.anchored_at,
                    ],
                )
            return record
        finally:
            con.close()

    def get(self, path: str, sha256: str) -> AnchorRecord | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT path, sha256, kind, label, content_hash, campaign_id, anchored_at "
                "FROM sidecar_anchors WHERE path = ? AND sha256 = ?",
                [path, sha256],
            ).fetchone()
            if row is None:
                return None
            return AnchorRecord(*row)
        finally:
            con.close()

    def find(
        self,
        *,
        kind: str | None = None,
        sha256: str | None = None,
        content_hash: str | None = None,
        campaign_id: str | None = None,
    ) -> list[AnchorRecord]:
        con = self._connect()
        try:
            clauses = []
            params: list[str] = []
            if kind is not None:
                clauses.append("kind = ?")
                params.append(kind)
            if sha256 is not None:
                clauses.append("sha256 = ?")
                params.append(sha256)
            if content_hash is not None:
                clauses.append("content_hash = ?")
                params.append(content_hash)
            if campaign_id is not None:
                clauses.append("campaign_id = ?")
                params.append(campaign_id)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = con.execute(
                "SELECT path, sha256, kind, label, content_hash, campaign_id, anchored_at "
                f"FROM sidecar_anchors {where}",
                params,
            ).fetchall()
            return [AnchorRecord(*row) for row in rows]
        finally:
            con.close()


def get_anchor_store(catalog_dir: Path | str) -> AnchorStore:
    """Return the default AnchorStore for a catalog — the seam an unfavorable
    unknown-3 resolution (durability / cross-boundary, #3485/#3486) would swap.

    Callers should go through this factory (or the module-level register_anchor /
    get_anchor / find_anchors functions, which default to it) rather than
    instantiating CatalogAnchorStore directly, so a future swap only changes this
    one function.
    """
    return CatalogAnchorStore(catalog_dir)


def register_anchor(
    catalog_dir: Path | str | None,
    path: str,
    sha256: str,
    kind: str,
    *,
    label: str | None = None,
    content_hash: str | None = None,
    campaign_id: str | None = None,
    store: AnchorStore | None = None,
) -> AnchorRecord:
    """Anchor an arbitrary sidecar by (path, sha256) into the catalog.

    Generalizes bathos.claim.register_claim (which anchors claim.bth.toml files
    specifically into the campaigns table) to any sidecar kind. Re-anchoring the same
    (path, sha256) upserts the other fields (kind/label/content_hash/campaign_id) —
    there is no --force gate here, unlike claim registration, because this seam makes
    no durability claim in the first place (see module docstring).

    Args:
        catalog_dir: Path to the bathos catalog root. Ignored if `store` is given
            (may be None in that case).
        path: Path to the sidecar file, as the caller wants it remembered. Not
            resolved or verified against disk — this seam anchors by declared
            identity, not filesystem presence.
        sha256: SHA256 of the sidecar file's contents at anchor time.
        kind: Free-form label for what's being anchored (e.g. "figure",
            "attestation"). Not validated against an enum; read-back callers (e.g.
            bathos.readback.figure_lookup) filter by the kind they expect.
        label: Optional human-readable label.
        content_hash: Optional hash of the underlying data product this sidecar
            describes — distinct from `sha256`, which hashes the sidecar itself.
        campaign_id: Optional campaign this anchor belongs to.
        store: Explicit AnchorStore to use instead of the default catalog-backed one.
            This is the adapter swap point — see
            tests/test_anchor.py::TestAlternateImplSatisfiesContract.

    Returns:
        The inserted (or re-inserted) AnchorRecord.
    """
    active_store = store if store is not None else get_anchor_store(catalog_dir)
    record = AnchorRecord(
        path=str(path),
        sha256=sha256,
        kind=kind,
        label=label,
        content_hash=content_hash,
        campaign_id=campaign_id,
    )
    return active_store.insert(record)


def get_anchor(
    catalog_dir: Path | str | None,
    path: str,
    sha256: str,
    *,
    store: AnchorStore | None = None,
) -> AnchorRecord | None:
    """Read back an anchored sidecar by (path, sha256). Round-trips register_anchor."""
    active_store = store if store is not None else get_anchor_store(catalog_dir)
    return active_store.get(str(path), sha256)


def find_anchors(
    catalog_dir: Path | str | None,
    *,
    kind: str | None = None,
    sha256: str | None = None,
    content_hash: str | None = None,
    campaign_id: str | None = None,
    store: AnchorStore | None = None,
) -> list[AnchorRecord]:
    """Find anchored sidecars matching all given (non-None) filters.

    Backs bathos.readback.figure_lookup's composition with this seam.
    """
    active_store = store if store is not None else get_anchor_store(catalog_dir)
    return active_store.find(
        kind=kind, sha256=sha256, content_hash=content_hash, campaign_id=campaign_id
    )
