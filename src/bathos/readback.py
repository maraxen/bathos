"""Read-back / query API (S1).

Spec: `.praxia/docs/specs/260713_figure-eda-coordination-system.md` §3.4 (maraxiom repo),
backlog item 3482, task 260713_figure-eda-build-dag. This module is the single adapter
seam for bathos's read-back/query surface, re-exposed unchanged from both the MCP
tool-server (`bathos.mcp`) and the CLI (`bathos.cli`).

Seven functions, per spec §3.4::

    resolve_pin(run_id, output_path) -> {content_hash, trust_state, fresh}
    get_trust_state(content_hash) -> trust_state
    query_attestation(content_hash, min_strength) -> attestation | None
    read_campaign_report(campaign_id) -> CampaignReport
    read_figure_manifest(campaign_id) -> FigureManifest
    figure_lookup(asset_sha256 | input_hash) -> [figure_entry, ...]
    list_candidates(campaign_id) -> [candidate, ...]

All seven are REAL as of seam S3 (``bathos.trust_ledger``, backlog item 3491):

- ``resolve_pin`` reads the existing run catalog (``bathos.query.get_run``) and the
  existing warm-tier ``output_metadata`` recorded at run time, and re-hashes the on-disk
  file to detect drift (reusing the same logic as ``bathos.checker`` /
  ``bathos.compact.check_output_sha_drift``).
- ``read_campaign_report`` / ``read_figure_manifest`` are thin wrappers over the existing
  ``CampaignReport.read_report`` / ``FigureManifest.read_manifest`` disk readers, which
  previously had no callable tool surface (Python-only, direct filesystem access).
- ``get_trust_state`` and ``list_candidates`` are now backed by the durable trust ledger
  (``bathos.trust_ledger``, S3) composed with the S2 anchor store and the run catalog —
  see their own docstrings below for the exact fold.

UPDATE (backlog item 3483, seam S2, ``bathos.anchor``): ``figure_lookup`` is no longer a
pure null-stub. The generic sidecar-anchor store built in S2 gives it a real, minimal
backing store — see ``figure_lookup``'s own docstring below.

UPDATE (backlog item 3492, seam S4, ``bathos.attestation``): ``query_attestation`` is no
longer a null-stub either. It is now backed by the attestation sidecar kind
(``oracle_match`` / ``repro_floor``, anchored via the S2 seam's ``DurableAnchorStore``) —
see ``query_attestation``'s own docstring below. Note the seam boundary this preserves:
a PASS attestation is *query-answerable evidence*, not a promotion by itself — the trust
ledger (S3, backlog #3491) is what consumes a PASS result (via
``bathos.trust_ledger.graduate_product``) to move a product from ``candidate`` to
``promoted``. A recorded PASS attestation with no corresponding ledger promotion record
remains inert: ``get_trust_state`` returns ``candidate`` (not ``promoted``) for such a
product until something actually calls ``graduate_product``.

UPDATE (backlog item 3490, seam S7, ``bathos.figure_registry``): ``figure_lookup`` now
ALSO resolves the typed, pointer-only ``figure_entry`` registry (spec §3.3), composing
its results alongside the pre-existing S2-era minimal anchor shape rather than
replacing it — see ``figure_lookup``'s own docstring below for the reconciliation.

UPDATE (backlog item 3491, seam S3, ``bathos.trust_ledger``): ``get_trust_state`` and
``list_candidates`` are no longer null-stubs. ``get_trust_state`` now implements the
owner-confirmed implicit 3-state model:

- ``unknown`` — the content_hash has never been anchored or produced by any run.
- ``candidate`` — the content_hash IS anchored (``bathos.anchor.find_anchors(sha256=...)``)
  or IS a run's recorded output sha256, but has no ``promoted`` ledger record.
- ``promoted`` — the trust ledger (``bathos.trust_ledger.fold_trust_state``) has a
  ``promoted`` record for this content_hash (appended only by
  ``bathos.trust_ledger.graduate_product``, which itself enforces the ratchet
  invariant — a PASS attestation must exist before promotion).

``list_candidates`` joins anchors (excluding attestation-kind anchors, which are
evidence records pointing AT a product via their ``content_hash`` field, not products
themselves) and run output hashes for a campaign, filtered to those without a
``promoted`` ledger record.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from bathos.campaign_report import CampaignReport
from bathos.compact import _collect_output_metadata
from bathos.figure_manifest import FigureManifest
from bathos.query import CatalogError, get_run


class ProductTrustState(StrEnum):
    """Trust axis for products (spec §1: `trust_state ∈ {candidate, promoted}`)."""

    CANDIDATE = "candidate"
    """Product is anchored but not yet graduated (P1/R1)."""

    PROMOTED = "promoted"
    """Product has graduated via the ratchet gate (R5/P4) and is trust-ledger-backed."""

    UNKNOWN = "unknown"
    """No trust ledger exists yet (build seam S3) — the defined not-yet-known sentinel."""


@dataclass(frozen=True)
class ResolvedPin:
    """Result of resolving a `[input]` pin: run_id + output_path -> content_hash/trust/freshness."""

    run_id: str
    output_path: str
    content_hash: str | None
    trust_state: str
    fresh: bool


def resolve_pin(catalog_dir: Path | str, run_id: str, output_path: str) -> ResolvedPin:
    """Resolve a `[input]` pin to its content_hash, trust_state, and freshness.

    REAL implementation: looks up ``run_id`` in the existing run catalog, finds the
    warm-tier ``output_metadata`` entry recorded for ``output_path`` at run time, and
    re-hashes the on-disk file to determine whether it still matches (freshness / drift
    check). ``trust_state`` is delegated to :func:`get_trust_state`, which is a null-stub
    until the trust ledger (S3) exists — composing this way means resolve_pin needs no
    changes once S3 ships.

    Args:
        catalog_dir: Path to the bathos catalog root.
        run_id: Run ID that produced (or is expected to have produced) the output.
        output_path: Path to the data file, as recorded in the run's
            output_paths/output_metadata.

    Returns:
        A :class:`ResolvedPin`. If ``output_path`` was never recorded with a hash for
        this run (e.g. an untracked path), ``content_hash`` is ``None`` and ``fresh`` is
        ``False`` — there is nothing to compare freshness against.

    Raises:
        CatalogError: If no run with ``run_id`` exists in the catalog.
    """
    cat_dir = Path(catalog_dir)
    run = get_run(run_id, cat_dir)
    if run is None:
        raise CatalogError(f"resolve_pin: no run found for run_id={run_id!r}")

    recorded_sha256: str | None = None
    if run.output_metadata and run.output_metadata not in ("", "[]"):
        try:
            entries = json.loads(run.output_metadata)
        except (json.JSONDecodeError, TypeError):
            entries = []
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("path") == output_path:
                    recorded_sha256 = entry.get("sha256")
                    break

    if recorded_sha256 is None:
        return ResolvedPin(
            run_id=run_id,
            output_path=output_path,
            content_hash=None,
            trust_state=get_trust_state(cat_dir, None),
            fresh=False,
        )

    current = _collect_output_metadata(output_path)
    fresh = current.get("status") == "present" and current.get("sha256") == recorded_sha256

    return ResolvedPin(
        run_id=run_id,
        output_path=output_path,
        content_hash=recorded_sha256,
        trust_state=get_trust_state(cat_dir, recorded_sha256),
        fresh=fresh,
    )


def _is_registered_product(catalog_dir: Path | str, content_hash: str) -> bool:
    """True if ``content_hash`` is anchored by its own identity (a product anchor,
    e.g. ``kind="figure"``) or is a run's recorded output sha256.

    Deliberately searches anchors by ``sha256`` (the anchor's own identity), NOT by
    ``content_hash`` (the anchor's optional pointer-to-an-upstream-input field) — an
    attestation anchor sets its OWN sha256 to the attestation TOML's hash and its
    ``content_hash`` field to the *attested product's* hash, so searching by
    ``content_hash`` here would incorrectly treat "this product has an attestation
    pointing at it" as "this product is itself a registered/anchored product",
    which would make a PASS-attested-but-never-graduated product look identical to
    a promoted one. Searching by ``sha256`` avoids that: it only matches anchors
    whose own identity IS the product (e.g. a figure asset anchored by its own
    hash), never an attestation merely referencing it.

    Also EXCLUDES attestation-kind anchors (``oracle_match`` / ``repro_floor``) from
    the ``sha256`` match itself, mirroring ``list_candidates``'s identical filter
    (debt #639): an attestation TOML's own sha256 could, in principle, collide with
    or equal some other content_hash the caller passes in that was never actually
    produced as a product. Without this exclusion, ``find_anchors(sha256=content_hash)``
    would still match on the attestation anchor's own identity and report the
    (never-produced) product as "registered" — inconsistent with ``list_candidates``,
    which already excludes attestation-kind anchors when scanning by campaign. See
    ``tests/test_readback.py`` (or ``test_readback_attestation.py``) for the pinned
    regression reproducing the audit's exact scenario.
    """
    from bathos.anchor import find_anchors
    from bathos.attestation import VALID_KINDS as ATTESTATION_KINDS

    matches = find_anchors(catalog_dir, sha256=content_hash)
    if any(anchor.kind not in ATTESTATION_KINDS for anchor in matches):
        return True

    return _find_run_with_output_sha256(catalog_dir, content_hash) is not None


def _find_run_with_output_sha256(
    catalog_dir: Path | str, content_hash: str
) -> tuple[str, str] | None:
    """Scan the warm run catalog's ``output_metadata`` for a matching sha256.

    Returns ``(run_id, output_path)`` for the first match, or ``None``. Mirrors the
    direct-DuckDB-scan pattern already used by ``bathos.mcp``'s outputs-summary tool
    (``SELECT ... FROM runs WHERE output_metadata IS NOT NULL``) rather than
    ``bathos.query.list_runs``, which caps results at a default ``limit``.
    """
    import duckdb

    db_path = Path(catalog_dir) / "bathos.db"
    if not db_path.exists():
        return None

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT id, output_metadata FROM runs "
            "WHERE output_metadata IS NOT NULL AND output_metadata != '[]'"
        ).fetchall()
    except duckdb.Error:
        return None
    finally:
        con.close()

    for run_id, output_metadata_json in rows:
        try:
            entries = json.loads(output_metadata_json) if output_metadata_json else []
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("sha256") == content_hash:
                return run_id, entry.get("path", "")
    return None


def get_trust_state(catalog_dir: Path | str, content_hash: str | None) -> str:
    """Look up the trust_state (unknown/candidate/promoted) of a product by content_hash.

    REAL (as of seam S3, ``bathos.trust_ledger``, backlog item 3491). Implements the
    owner-confirmed implicit 3-state model:

    1. ``content_hash is None`` -> :attr:`ProductTrustState.UNKNOWN` (nothing to
       look up against).
    2. Otherwise, fold the durable trust ledger
       (``bathos.trust_ledger.fold_trust_state``) for ``content_hash``. If it
       resolves to ``"promoted"`` -> :attr:`ProductTrustState.PROMOTED`.
    3. Otherwise, check whether the product is registered at all — anchored by its
       own identity (``bathos.anchor.find_anchors(sha256=content_hash)``) or
       produced as a run's recorded output (:func:`_find_run_with_output_sha256`).
       If either matches -> :attr:`ProductTrustState.CANDIDATE`.
    4. Otherwise -> :attr:`ProductTrustState.UNKNOWN` — this content_hash has never
       been seen by this catalog at all.

    The only way to reach ``promoted`` is via ``bathos.trust_ledger.graduate_product``,
    which itself refuses to append a promotion record without a PASS attestation
    (the ratchet invariant, spec §5.1 step 4). Note that registering an attestation
    anchors the attestation sidecar's OWN sha256 (with ``content_hash`` merely
    pointing at the attested product) — it does not register the product itself.
    So a PASS attestation alone, with no accompanying anchor/run for the product
    and no graduation call, leaves this function returning ``unknown``, not
    ``candidate`` — see ``tests/test_readback_attestation.py::TestInertEvidenceUntilTrustLedger``
    and this module's own test suite for the pinned regression.

    Args:
        catalog_dir: Path to the bathos catalog root.
        content_hash: Content hash of the product to look up.

    Returns:
        One of ``ProductTrustState.UNKNOWN`` / ``CANDIDATE`` / ``PROMOTED``.
    """
    if content_hash is None:
        return ProductTrustState.UNKNOWN

    from bathos.trust_ledger import fold_trust_state

    to_state = fold_trust_state(catalog_dir, content_hash)
    if to_state == ProductTrustState.PROMOTED:
        return ProductTrustState.PROMOTED

    if _is_registered_product(catalog_dir, content_hash):
        return ProductTrustState.CANDIDATE

    return ProductTrustState.UNKNOWN


def query_attestation(
    catalog_dir: Path | str,
    content_hash: str,
    min_strength: str | None = None,
) -> dict | None:
    """Look up a verdict-checked attestation for a product by content_hash.

    REAL (as of seam S4, ``bathos.attestation``, backlog item 3492): backed by the
    attestation sidecar kind (``oracle_match`` / ``repro_floor``), anchored via the S2
    seam's ``DurableAnchorStore`` so this composes correctly even after a
    ``bathos.compact.compact(force_rebuild=True)``.

    Confirms **verdict == "PASS"** (spec §5.1 step 4) before returning a non-null
    result — a WARN or FAIL attestation may exist (registered via
    ``bathos.attestation.register_attestation``) but is never returned by this
    function; it is present in the catalog as a distinct, queryable-by-anchor record
    (``bathos.anchor.find_anchors(..., content_hash=content_hash)`` would still surface
    it), but ``query_attestation`` is a verdict-gated view over that store, not a raw
    passthrough.

    ``min_strength`` distinguishes ``oracle_match`` (independent-oracle-verified,
    stronger) from ``repro_floor`` (seed-pinned determinism only, weaker) —
    see ``bathos.attestation.STRENGTH_RANK``. If multiple PASS attestations of
    qualifying strength exist for the same content_hash, the strongest one wins, tied
    by most-recently-anchored.

    IMPORTANT — inert-evidence note (spec item 9 acceptance, backlog #3492): a PASS
    result from this function is evidence only. It does not itself promote anything —
    the trust ledger (build seam S3, backlog #3491) is what would consume a PASS
    result to append a ``candidate -> promoted`` record (spec §5.1 step 4). Until S3
    exists (or has no ledger entry for this content_hash), ``get_trust_state`` for the
    same content_hash still returns ``UNKNOWN`` regardless of what this function
    returns — a PASS attestation recorded before S3 exists is inert.

    Args:
        catalog_dir: Path to the bathos catalog root.
        content_hash: Content hash of the attested product.
        min_strength: Minimum required certification strength, ``"oracle_match"`` or
            ``"repro_floor"``; ``None`` = no minimum (either strength qualifies).

    Returns:
        A dict shaped per spec §3.2 (``kind``, ``attested``, ``verdict``,
        kind-specific fields, ``attestation_sha256``, ``campaign_id``, ``anchored_at``)
        if a matching-strength PASS attestation exists, else ``None``.
    """
    from bathos.anchor import find_anchors
    from bathos.attestation import STRENGTH_RANK, parse_attestation

    if min_strength is not None and min_strength not in STRENGTH_RANK:
        return None

    required_rank = STRENGTH_RANK.get(min_strength, 0) if min_strength else 0

    candidates = [
        anchor
        for anchor in find_anchors(catalog_dir, content_hash=content_hash)
        if anchor.kind in STRENGTH_RANK and STRENGTH_RANK[anchor.kind] >= required_rank
    ]
    # Strongest first, then most-recently-anchored first.
    candidates.sort(key=lambda a: (STRENGTH_RANK[a.kind], a.anchored_at), reverse=True)

    for anchor in candidates:
        try:
            attestation = parse_attestation(Path(anchor.path))
        except (FileNotFoundError, ValueError):
            continue
        if attestation.verdict != "PASS":
            continue
        return {
            "kind": attestation.kind,
            "attested": attestation.attested,
            "verdict": attestation.verdict,
            "oracle_sha256": attestation.oracle_sha256,
            "harness_run_ref": attestation.harness_run_ref,
            "max_discrepancy": attestation.max_discrepancy,
            "tolerance_policy": attestation.tolerance_policy,
            "seed_pin": attestation.seed_pin,
            "rerun_count": attestation.rerun_count,
            "rerun_digests": attestation.rerun_digests,
            "created_by": attestation.created_by,
            "created_at": attestation.created_at,
            "attestation_sha256": anchor.sha256,
            "content_hash": anchor.content_hash,
            "campaign_id": anchor.campaign_id,
            "anchored_at": anchor.anchored_at,
        }

    return None


def read_campaign_report(catalog_dir: Path | str, campaign_id: str) -> CampaignReport:
    """Read the ``campaign_report.json`` sidecar for a campaign.

    REAL implementation: thin wrapper over :meth:`CampaignReport.read_report`, exposing
    the existing disk reader through the S1 read-back surface (MCP + CLI) so callers
    don't need direct filesystem access to
    ``<catalog>/sidecars/<campaign_id>/campaign_report.json``.

    Args:
        catalog_dir: Path to the bathos catalog root.
        campaign_id: Full campaign ID (must match the sidecar directory name exactly;
            this function does not resolve short-prefix IDs).

    Returns:
        The parsed :class:`CampaignReport`.

    Raises:
        FileNotFoundError: If the report has not been emitted yet (see ``bth report
            emit``).
    """
    path = Path(catalog_dir) / "sidecars" / campaign_id / "campaign_report.json"
    return CampaignReport.read_report(path)


def read_figure_manifest(catalog_dir: Path | str, campaign_id: str) -> FigureManifest:
    """Read the ``figure_manifest.json`` sidecar for a campaign.

    REAL implementation: thin wrapper over :meth:`FigureManifest.read_manifest`. See
    :func:`read_campaign_report` for the sidecar-path and error-handling conventions this
    mirrors.

    Args:
        catalog_dir: Path to the bathos catalog root.
        campaign_id: Full campaign ID (must match the sidecar directory name exactly).

    Returns:
        The parsed :class:`FigureManifest`.

    Raises:
        FileNotFoundError: If the manifest has not been emitted yet (see ``bth report
            emit``).
    """
    path = Path(catalog_dir) / "sidecars" / campaign_id / "figure_manifest.json"
    return FigureManifest.read_manifest(path)


def figure_lookup(
    catalog_dir: Path | str,
    asset_sha256: str | None = None,
    input_hash: str | None = None,
) -> list[dict]:
    """Look up figure registry entries by asset_sha256 or input_hash.

    REAL, composed from two sources:

    - The S2-era minimal shape (``bathos.anchor``, item 3483): generic
      sidecar-anchors with ``kind="figure"``, returned as a dict of ``path``/
      ``sha256``/``content_hash``/``campaign_id``/``anchored_at``/``figure_id``.
    - The S7 typed figure registry (``bathos.figure_registry``, item 3490): typed,
      pointer-only ``figure_entry`` records (``asset_sha256``/``sidecar_ref``/
      ``figure_kind``/``render_state``/``fig_trust_state``/``attestation_ref``),
      registered via ``bathos.figure_registry.register_figure_entry``.

    These are NOT unified into one shape — S7 is additive, composed alongside the
    older S2 shape rather than replacing it (see ``bathos.figure_registry`` module
    docstring, "Reconciling with the S1-era figure_lookup shape"). A caller
    distinguishes the two by their key sets. Until a producer registers via either
    path for a given asset_sha256/input_hash, this returns ``[]``, identical to the
    pre-S2 null-stub behavior.

    Args:
        catalog_dir: Path to the bathos catalog root.
        asset_sha256: Anchor key (sha256) of the rendered figure asset/sidecar itself.
        input_hash: Content hash of the underlying data product the figure derives
            from (matches an anchor's ``content_hash``, not its ``sha256``).

    Returns:
        A list of dicts, one per matching record from either source (may be a mix of
        both shapes). ``[]`` if neither filter is given or nothing matches.
    """
    if asset_sha256 is None and input_hash is None:
        return []

    import dataclasses

    from bathos.anchor import find_anchors
    from bathos.figure_registry import find_figure_entries

    records = find_anchors(
        catalog_dir, kind="figure", sha256=asset_sha256, content_hash=input_hash
    )
    legacy = [
        {
            "figure_id": record.label or record.path,
            "path": record.path,
            "sha256": record.sha256,
            # NAME COLLISION NOTE (debt #645): this "content_hash" key is the
            # underlying AnchorRecord's own content_hash column — the input data
            # product's hash (what bathos.figure_registry, the S7 typed schema,
            # instead calls "input_hash" precisely to avoid this collision). It is
            # UNRELATED to bathos.figure_registry.FORBIDDEN_INLINE_FIELDS's
            # "content_hash" (an ADR-forbidden attestation-verdict field a typed
            # FigureEntry must never carry) — same literal key name, different
            # object, different semantics. Not renamed here: this legacy dict shape
            # is a shipped JSON output (CLI `bth query figures` / the figure_lookup
            # MCP tool) pinned by tests/test_readback.py and
            # tests/test_anchor_durability.py. See bathos.figure_registry's module
            # docstring ("NAME COLLISION NOTE") for the full disambiguation.
            "content_hash": record.content_hash,
            "campaign_id": record.campaign_id,
            "anchored_at": record.anchored_at,
        }
        for record in records
    ]

    typed_entries = find_figure_entries(
        catalog_dir, asset_sha256=asset_sha256, input_hash=input_hash
    )
    typed = [dataclasses.asdict(entry) for entry in typed_entries]

    return legacy + typed


def list_candidates(catalog_dir: Path | str, campaign_id: str) -> list[dict]:
    """List candidate-tier (registered, not-yet-promoted) products for a campaign.

    REAL (as of seam S3, ``bathos.trust_ledger``, backlog item 3491). Joins two
    sources of "registered" products scoped to ``campaign_id``, then filters out
    anything with a ``promoted`` trust-ledger record:

    - Anchors with ``campaign_id`` set to this campaign, EXCLUDING attestation-kind
      anchors (``oracle_match`` / ``repro_floor`` — evidence records whose
      ``content_hash`` field points AT a product rather than being one; see
      ``bathos.readback._is_registered_product`` for the identical reasoning
      applied to single-product lookups). Each qualifying anchor's own ``sha256``
      is the product's content_hash.
    - Runs with ``campaign_id`` set to this campaign: every ``output_metadata``
      entry's ``sha256`` is a candidate product's content_hash.

    Args:
        catalog_dir: Path to the bathos catalog root.
        campaign_id: Campaign ID to list candidates for.

    Returns:
        A list of dicts, each with ``content_hash``, ``trust_state`` (always
        ``"candidate"`` — anything ``promoted`` is filtered out), ``source``
        (``"anchor"`` or ``"run"``), and source-specific identifying fields
        (``path``/``anchored_at`` for anchors; ``run_id``/``output_path`` for runs).
        ``[]`` if nothing is registered for this campaign, or everything registered
        has already been promoted.
    """
    import duckdb

    from bathos.anchor import find_anchors
    from bathos.attestation import VALID_KINDS as ATTESTATION_KINDS
    from bathos.trust_ledger import fold_trust_state

    candidates: list[dict] = []
    seen_hashes: set[str] = set()

    for anchor in find_anchors(catalog_dir, campaign_id=campaign_id):
        if anchor.kind in ATTESTATION_KINDS:
            continue
        content_hash = anchor.sha256
        if content_hash in seen_hashes:
            continue
        if fold_trust_state(catalog_dir, content_hash) == ProductTrustState.PROMOTED:
            continue
        seen_hashes.add(content_hash)
        candidates.append(
            {
                "content_hash": content_hash,
                "trust_state": ProductTrustState.CANDIDATE,
                "source": "anchor",
                "path": anchor.path,
                "anchored_at": anchor.anchored_at,
            }
        )

    db_path = Path(catalog_dir) / "bathos.db"
    if db_path.exists():
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = con.execute(
                "SELECT id, output_metadata FROM runs WHERE campaign_id = ? "
                "AND output_metadata IS NOT NULL AND output_metadata != '[]'",
                [campaign_id],
            ).fetchall()
        except duckdb.Error:
            rows = []
        finally:
            con.close()

        for run_id, output_metadata_json in rows:
            try:
                entries = json.loads(output_metadata_json) if output_metadata_json else []
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                content_hash = entry.get("sha256")
                if not content_hash or content_hash in seen_hashes:
                    continue
                if fold_trust_state(catalog_dir, content_hash) == ProductTrustState.PROMOTED:
                    continue
                seen_hashes.add(content_hash)
                candidates.append(
                    {
                        "content_hash": content_hash,
                        "trust_state": ProductTrustState.CANDIDATE,
                        "source": "run",
                        "run_id": run_id,
                        "output_path": entry.get("path", ""),
                    }
                )

    return candidates
