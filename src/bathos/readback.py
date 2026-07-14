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

Three are REAL today:

- ``resolve_pin`` reads the existing run catalog (``bathos.query.get_run``) and the
  existing warm-tier ``output_metadata`` recorded at run time, and re-hashes the on-disk
  file to detect drift (reusing the same logic as ``bathos.checker`` /
  ``bathos.compact.check_output_sha_drift``).
- ``read_campaign_report`` / ``read_figure_manifest`` are thin wrappers over the existing
  ``CampaignReport.read_report`` / ``FigureManifest.read_manifest`` disk readers, which
  previously had no callable tool surface (Python-only, direct filesystem access).

Two remain intentional NULL-STUBS, because their backing stores do not exist yet:

- ``get_trust_state`` -> trust ledger (build seam S3, spec §3.1). Always returns
  ``ProductTrustState.UNKNOWN`` until S3 ships.
- ``list_candidates`` -> candidate tier of the trust ledger (build seam S3). Always
  returns ``[]`` until S3 ships.

This "ships before its stores exist, returns empty cleanly" behavior is the acceptance
requirement for this item: callers (praxia's FSM, maraxiom's Flow V) can call the full S1
surface today, and the null-stubs can be swapped for real implementations later without
changing call sites — ``resolve_pin`` already composes with ``get_trust_state`` this way.

UPDATE (backlog item 3483, seam S2, ``bathos.anchor``): ``figure_lookup`` is no longer a
pure null-stub. The generic sidecar-anchor store built in S2 gives it a real, minimal
backing store — see ``figure_lookup``'s own docstring below. ``get_trust_state`` and
``list_candidates`` remain null-stubs (their backing store, S3, still does not exist).

UPDATE (backlog item 3492, seam S4, ``bathos.attestation``): ``query_attestation`` is no
longer a null-stub either. It is now backed by the attestation sidecar kind
(``oracle_match`` / ``repro_floor``, anchored via the S2 seam's ``DurableAnchorStore``) —
see ``query_attestation``'s own docstring below. Note the seam boundary this preserves:
a PASS attestation is *query-answerable evidence*, not a promotion by itself — the trust
ledger (S3, backlog #3491) is what would consume a PASS result to move a product from
``candidate`` to ``promoted``. Until S3 exists, ``get_trust_state`` still always returns
``UNKNOWN`` regardless of what ``query_attestation`` returns for the same content_hash —
a recorded PASS attestation is inert on its own.
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


def get_trust_state(catalog_dir: Path | str, content_hash: str | None) -> str:
    """Look up the trust_state (candidate/promoted) of a product by content_hash.

    NULL-STUB: the trust-ledger backing store (build seam S3, spec §3.1 — an
    append+supersede ledger folded latest-wins) does not exist yet. Until S3 ships, this
    always returns :attr:`ProductTrustState.UNKNOWN` so callers such as
    :func:`resolve_pin` can compose against a stable, always-callable surface. Swap the
    body for a real ledger query once S3 lands; call sites do not need to change.

    Args:
        catalog_dir: Path to the bathos catalog root (unused until S3 exists).
        content_hash: Content hash of the product to look up (unused until S3 exists).

    Returns:
        ``ProductTrustState.UNKNOWN``, always, until S3 ships.
    """
    del catalog_dir, content_hash  # unused until the trust ledger (S3) exists
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

    REAL (as of seam S2, ``bathos.anchor``, backlog item 3483): backed by the generic
    sidecar-anchor store, filtered to ``kind="figure"``. This is a minimal figure
    registry, not the dedicated build seam S7 originally anticipated in this module's
    docstring — S7 may still supersede it with a richer schema. Until a producer calls
    ``bathos.anchor.register_anchor(..., kind="figure", ...)`` for a given
    asset_sha256/input_hash, this returns ``[]``, which is observably identical to the
    pre-S2 null-stub behavior.

    Args:
        catalog_dir: Path to the bathos catalog root.
        asset_sha256: Anchor key (sha256) of the rendered figure asset/sidecar itself.
        input_hash: Content hash of the underlying data product the figure derives
            from (matches an anchor's ``content_hash``, not its ``sha256``).

    Returns:
        A list of dicts (one per matching anchor): ``path``, ``sha256``,
        ``content_hash``, ``campaign_id``, ``anchored_at``, and ``figure_id`` (the
        anchor's label if set, else its path). ``[]`` if neither filter is given or
        nothing matches.
    """
    if asset_sha256 is None and input_hash is None:
        return []

    from bathos.anchor import find_anchors

    records = find_anchors(
        catalog_dir, kind="figure", sha256=asset_sha256, content_hash=input_hash
    )
    return [
        {
            "figure_id": record.label or record.path,
            "path": record.path,
            "sha256": record.sha256,
            "content_hash": record.content_hash,
            "campaign_id": record.campaign_id,
            "anchored_at": record.anchored_at,
        }
        for record in records
    ]


def list_candidates(catalog_dir: Path | str, campaign_id: str) -> list[dict]:
    """List candidate-tier (not-yet-promoted) products for a campaign.

    NULL-STUB: candidate tiering is derived from the trust ledger (build seam S3, spec
    §3.1), which does not exist yet. Always returns ``[]`` until S3 ships.

    Args:
        catalog_dir: Path to the bathos catalog root (unused until S3 exists).
        campaign_id: Campaign ID to list candidates for (unused until S3 exists).

    Returns:
        ``[]``, always, until S3 ships.
    """
    del catalog_dir, campaign_id  # unused until the trust ledger (S3) exists
    return []
