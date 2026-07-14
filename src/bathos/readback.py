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

Four are intentional NULL-STUBS, because their backing stores do not exist yet:

- ``get_trust_state`` -> trust ledger (build seam S3, spec §3.1). Always returns
  ``ProductTrustState.UNKNOWN`` until S3 ships.
- ``query_attestation`` -> attestation sidecar store (build seam S4, spec §3.2). Always
  returns ``None`` until S4 ships.
- ``figure_lookup`` -> figure registry (build seam S7, spec §3.3). Always returns ``[]``
  until S7 ships.
- ``list_candidates`` -> candidate tier of the trust ledger (build seam S3). Always
  returns ``[]`` until S3 ships.

This "ships before its stores exist, returns empty cleanly" behavior is the acceptance
requirement for this item: callers (praxia's FSM, maraxiom's Flow V) can call the full S1
surface today, and the null-stubs can be swapped for real implementations later without
changing call sites — ``resolve_pin`` already composes with ``get_trust_state`` this way.

UPDATE (backlog item 3483, seam S2, ``bathos.anchor``): ``figure_lookup`` is no longer a
pure null-stub. The generic sidecar-anchor store built in S2 gives it a real, minimal
backing store — see ``figure_lookup``'s own docstring below. ``get_trust_state``,
``query_attestation``, and ``list_candidates`` remain null-stubs (their backing stores,
S3/S4, still do not exist).

UPDATE (backlog item 3490, seam S7, ``bathos.figure_registry``): ``figure_lookup`` now
ALSO resolves the typed, pointer-only ``figure_entry`` registry (spec §3.3), composing
its results alongside the pre-existing S2-era minimal anchor shape rather than
replacing it — see ``figure_lookup``'s own docstring below for the reconciliation.
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
    """Look up an attestation (verdict-checked) for a product by content_hash.

    NULL-STUB: the attestation sidecar store (build seam S4, spec §3.2 — ``oracle_match``
    / ``repro_floor`` attestation sidecars anchored by sha256) does not exist yet. Always
    returns ``None`` until S4 ships. Once implemented, this should confirm
    ``verdict == "PASS"`` and strength >= ``min_strength`` before returning a non-null
    result (spec §5.1 step 4).

    Args:
        catalog_dir: Path to the bathos catalog root (unused until S4 exists).
        content_hash: Content hash of the attested product (unused until S4 exists).
        min_strength: Minimum required certification strength, ``"oracle_match"`` or
            ``"repro_floor"`` (unused until S4 exists).

    Returns:
        ``None``, always, until S4 ships.
    """
    del catalog_dir, content_hash, min_strength  # unused until the attestation store (S4) exists
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
