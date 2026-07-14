"""Figure registry (S7): typed, pointer-only `figure_entry` schema.

Backlog item 3490, task 260713_figure-eda-build-dag, build seam "S7" of the
figure/EDA coordination system. Spec:
`.praxia/docs/specs/260713_figure-eda-coordination-system.md` (maraxiom repo) §3.3
+ §7.

This module formalizes the figure registry described in spec §3.3: each `.figure.toml`
sidecar (owned by maraxiom) is anchored in bathos by its rendered asset's
``asset_sha256``. The catalog-side entry carries **pointers only** —

    figure_entry (anchored by asset_sha256):
      asset_sha256     # anchor key
      sidecar_ref       # -> .figure.toml
      figure_kind
      render_state      # {ready, deferred}   (bathos already uses these — see
                        # bathos.figure_manifest.RenderState)
      fig_trust_state   # {draft, final}
      attestation_ref   # POINTER to a bathos-side attestation (never the verdict itself)

Critical (ADR-enforced, spec §3.3 / §7): the schema REJECTS any inline
``verdict``/``strength``/``content_hash``/``outcome``/``gate`` field. A figure carries
only a POINTER to a bathos-side attestation (``attestation_ref``) — certification
semantics (verdict, strength, the attestation's own content_hash) are resolved from
that pointer at read time via ``bathos.readback.query_attestation`` (S4, not yet
built), never inlined into the figure entry itself. This mirrors maraxiom's
``FigureSidecar`` schema (`packages` repo, ``src/maraxiom/figure_sidecar.py``), which
code-enforces (via ``model_config = ConfigDict(extra="forbid")``) that bathos-style
``experiment``/``outcomes``/``residual``/``gate`` fields must not appear on a figure
sidecar. :func:`build_figure_entry` is the equivalent guard on the bathos side.

Relationship to the S2 anchor-insert seam (``bathos.anchor``, item 3483)
--------------------------------------------------------------------------
This registry is **built on** the merged anchor store: registration goes through
:func:`bathos.anchor.register_anchor` with a dedicated ``kind="figure_entry"``
(distinct from the S2-era ``kind="figure"`` anchors used by the earlier minimal
``figure_lookup`` composition — see "Reconciling with the S1-era figure_lookup shape"
below), and defaults to :class:`bathos.anchor.DurableAnchorStore` so entries survive
``bathos.compact.compact(catalog_dir, force_rebuild=True)`` (gate #3485, merged to
main). The typed fields that don't fit :class:`bathos.anchor.AnchorRecord`'s generic
shape (``figure_kind``, ``render_state``, ``fig_trust_state``, ``attestation_ref``)
are JSON-encoded into the anchor's free-form ``label`` column — an internal storage
detail of this module, not a schema change to ``bathos.anchor`` itself. This keeps the
generic anchor seam (shared with the future attestation-sidecar kind, S4) untouched
and additive, at the cost of the label column being opaque JSON for this one kind.

Reconciling with the S1-era figure_lookup shape (2a-agent-flagged ambiguity)
------------------------------------------------------------------------------
``bathos.readback.figure_lookup`` (item 3482/3483) already returns a *minimal* dict
shape (``figure_id``/``path``/``sha256``/``content_hash``/``campaign_id``/
``anchored_at``) for anchors registered with the generic ``kind="figure"``. This
module does **not** replace that shape — it is composed alongside it: figure_lookup
now also resolves ``kind="figure_entry"`` anchors (this module's typed schema) and
appends their dict form to the result list. A caller distinguishes the two shapes by
their key sets (typed entries have ``asset_sha256``/``sidecar_ref``/``figure_kind``/
etc.; legacy entries have ``figure_id``/``path``/etc.). This is flagged as an open
reconciliation point for a future item: a single canonical figure_lookup return shape
(migrating S2-era callers onto the typed schema) was out of scope for this item's
red/green test-first mandate, which asks only that the typed schema exist, reject
forbidden fields, and be retrievable — not that it unify/replace the older shape.

Reconciling "content_hash" vs "input_hash"
---------------------------------------------
The FORBIDDEN-field list (spec §7, Critical bullet) names ``content_hash`` literally
as a field the *typed figure_entry schema* must never carry — this is the
attestation's own content_hash, never inlined. This is a different concept from
"input_hash": the hash of the *underlying data product* a figure derives from
(``figure_lookup(asset_sha256|input_hash)``). ``register_figure_entry`` accepts
``input_hash`` as a *registration/lookup parameter* (stored in the underlying anchor's
``content_hash`` column, a storage-layer detail of ``bathos.anchor``) so that
``find_figure_entries``/``figure_lookup`` can filter by it — but the returned
:class:`FigureEntry` never exposes a ``content_hash`` (or ``input_hash``) attribute.
input_hash is a query key, not a field of the typed entry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from bathos.anchor import AnchorStore, DurableAnchorStore, find_anchors, register_anchor
from bathos.figure_manifest import RenderState

_ANCHOR_KIND = "figure_entry"

#: ADR-enforced (spec §3.3/§7): these field names may never appear inline on a
#: figure_entry. A figure carries only a POINTER (attestation_ref) to a bathos-side
#: attestation, never the verdict/strength/content_hash/outcome/gate itself.
FORBIDDEN_INLINE_FIELDS = frozenset({"verdict", "strength", "content_hash", "outcome", "gate"})

_ALLOWED_FIELDS = frozenset(
    {
        "asset_sha256",
        "sidecar_ref",
        "figure_kind",
        "render_state",
        "fig_trust_state",
        "attestation_ref",
    }
)


class FigTrustState(StrEnum):
    """Trust axis for figures (spec §1: `{draft, final}` on figures — distinct from
    `bathos.readback.ProductTrustState`, which is the underlying product's
    `{candidate, promoted}` axis)."""

    DRAFT = "draft"
    """Figure derives from a candidate (not-yet-promoted) product, or has not passed
    the V5 render gate."""

    FINAL = "final"
    """Figure derives from a promoted product and has passed the V5 render gate."""


class FigureEntrySchemaError(ValueError):
    """Raised when a figure_entry payload carries a forbidden inline field.

    Distinct from a plain ``ValueError`` (used for enum-validation failures like an
    unrecognized ``render_state``) so callers/tests can specifically assert on the
    ADR-enforced rejection rather than any schema-validation error.
    """


@dataclass(frozen=True)
class FigureEntry:
    """Typed, pointer-only figure registry entry (spec §3.3).

    Construct via :func:`build_figure_entry`, not directly — that is the function
    which enforces the forbidden-inline-field guard before a `FigureEntry` object
    is ever created.
    """

    asset_sha256: str
    """Anchor key: SHA256 of the rendered figure asset."""

    sidecar_ref: str
    """Pointer to the `.figure.toml` sidecar (maraxiom-owned) describing this figure."""

    figure_kind: str
    """Free-form figure kind, e.g. 'chord_diagram', 'mi_heatmap', 'scatter'."""

    render_state: str
    """`{ready, deferred}` — see `bathos.figure_manifest.RenderState`."""

    fig_trust_state: str
    """`{draft, final}` — see `FigTrustState`."""

    attestation_ref: str | None = None
    """POINTER (sha256) to a bathos-side attestation sidecar (S4, not yet built) —
    never the verdict/strength itself. None until the figure's underlying product
    has been certified and the attestation is anchored."""

    def __post_init__(self) -> None:
        valid_render_states = {s.value for s in RenderState}
        if self.render_state not in valid_render_states:
            raise ValueError(
                f"figure_entry.render_state={self.render_state!r} is not one of "
                f"{sorted(valid_render_states)}"
            )
        valid_trust_states = {s.value for s in FigTrustState}
        if self.fig_trust_state not in valid_trust_states:
            raise ValueError(
                f"figure_entry.fig_trust_state={self.fig_trust_state!r} is not one of "
                f"{sorted(valid_trust_states)}"
            )


def build_figure_entry(**data: object) -> FigureEntry:
    """Construct a :class:`FigureEntry`, enforcing the forbidden-inline-field guard.

    This is "the schema" referred to by spec §7's Critical bullet: it REJECTS any
    payload carrying an inline ``verdict``/``strength``/``content_hash``/``outcome``/
    ``gate`` field — a figure carries only a POINTER (``attestation_ref``) to a
    bathos-side attestation, never the verdict/strength/outcome/gate itself, and
    never the attestation's own content_hash inlined.

    Args:
        **data: Keyword fields for :class:`FigureEntry` (asset_sha256, sidecar_ref,
            figure_kind, render_state, fig_trust_state, attestation_ref).

    Returns:
        The constructed, validated :class:`FigureEntry`.

    Raises:
        FigureEntrySchemaError: If any forbidden field name is present in ``data``.
        TypeError: If ``data`` contains a field name that is neither a valid
            `FigureEntry` field nor a forbidden one (i.e. a plain unknown field).
        ValueError: If `render_state` or `fig_trust_state` is not a recognized value.
    """
    forbidden = FORBIDDEN_INLINE_FIELDS & data.keys()
    if forbidden:
        raise FigureEntrySchemaError(
            f"figure_entry schema forbids inline field(s) {sorted(forbidden)} — a "
            "figure carries only a POINTER (attestation_ref) to a bathos-side "
            "attestation; verdict/strength/content_hash/outcome/gate must be resolved "
            "via that pointer at read time, never inlined (spec 260713 §3.3/§7)."
        )
    unknown = data.keys() - _ALLOWED_FIELDS
    if unknown:
        raise TypeError(f"figure_entry schema has no field(s) {sorted(unknown)}")
    return FigureEntry(**data)  # type: ignore[arg-type]


def _encode_typed_fields(entry: FigureEntry) -> str:
    """Internal storage encoding: pack the typed fields not covered by
    `bathos.anchor.AnchorRecord` into a JSON blob for the anchor's `label` column.
    Not part of the public figure_entry schema — a plumbing detail of this module."""
    return json.dumps(
        {
            "figure_kind": entry.figure_kind,
            "render_state": entry.render_state,
            "fig_trust_state": entry.fig_trust_state,
            "attestation_ref": entry.attestation_ref,
        }
    )


def _decode_typed_fields(
    *, sidecar_ref: str, asset_sha256: str, label: str | None
) -> FigureEntry | None:
    """Inverse of :func:`_encode_typed_fields`. Returns None (skip, don't raise) if
    `label` is missing or is not valid figure_entry JSON — defensive against a
    `kind="figure_entry"` anchor written by something other than this module."""
    if not label:
        return None
    try:
        payload = json.loads(label)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return FigureEntry(
            asset_sha256=asset_sha256,
            sidecar_ref=sidecar_ref,
            figure_kind=payload["figure_kind"],
            render_state=payload["render_state"],
            fig_trust_state=payload["fig_trust_state"],
            attestation_ref=payload.get("attestation_ref"),
        )
    except (KeyError, ValueError):
        return None


def register_figure_entry(
    catalog_dir: object,
    *,
    asset_sha256: str,
    sidecar_ref: str,
    figure_kind: str,
    render_state: str = RenderState.READY.value,
    fig_trust_state: str = FigTrustState.DRAFT.value,
    attestation_ref: str | None = None,
    input_hash: str | None = None,
    campaign_id: str | None = None,
    store: AnchorStore | None = None,
    **forbidden_or_unknown: object,
) -> FigureEntry:
    """Register a typed figure_entry, anchored by `asset_sha256` (S7 write seam).

    Builds on the S2 anchor-insert seam (`bathos.anchor.register_anchor`) with a
    dedicated `kind="figure_entry"` and defaults to `DurableAnchorStore` so the entry
    survives `bathos.compact.compact(catalog_dir, force_rebuild=True)`.

    Args:
        catalog_dir: Path to the bathos catalog root. Ignored if `store` is given.
        asset_sha256: Anchor key — SHA256 of the rendered figure asset.
        sidecar_ref: Pointer to the `.figure.toml` sidecar describing this figure.
        figure_kind: Free-form figure kind (e.g. 'chord_diagram').
        render_state: `{ready, deferred}`. Defaults to 'ready'.
        fig_trust_state: `{draft, final}`. Defaults to 'draft'.
        attestation_ref: Optional POINTER (sha256) to a bathos-side attestation.
        input_hash: Optional hash of the underlying data product this figure derives
            from — a lookup/query key (stored in the anchor's `content_hash` column),
            NOT a field of the returned `FigureEntry` (see module docstring).
        campaign_id: Optional campaign this figure belongs to.
        store: Explicit `AnchorStore` to use instead of the default
            `DurableAnchorStore(catalog_dir)`. Passing a non-durable
            `CatalogAnchorStore` is supported (mirrors `bathos.anchor`'s own adapter
            seam) but forfeits the force_rebuild durability guarantee.
        **forbidden_or_unknown: Catches any stray forbidden or unknown keyword so a
            caller merging in attestation-shaped data (e.g. `**attestation_dict`)
            gets the same ADR-enforced rejection as `build_figure_entry`.

    Returns:
        The registered, validated `FigureEntry`.

    Raises:
        FigureEntrySchemaError: If a forbidden field was passed (rejection happens
            BEFORE any anchor write).
        TypeError: If an unrecognized field was passed.
        ValueError: If `render_state` or `fig_trust_state` is invalid.
    """
    if forbidden_or_unknown:
        forbidden = FORBIDDEN_INLINE_FIELDS & forbidden_or_unknown.keys()
        if forbidden:
            raise FigureEntrySchemaError(
                f"figure_entry schema forbids inline field(s) {sorted(forbidden)} — a "
                "figure carries only a POINTER (attestation_ref) to a bathos-side "
                "attestation; verdict/strength/content_hash/outcome/gate must be "
                "resolved via that pointer at read time, never inlined (spec 260713 "
                "§3.3/§7)."
            )
        raise TypeError(
            f"register_figure_entry() got unexpected keyword argument(s) "
            f"{sorted(forbidden_or_unknown.keys())}"
        )

    entry = build_figure_entry(
        asset_sha256=asset_sha256,
        sidecar_ref=sidecar_ref,
        figure_kind=figure_kind,
        render_state=render_state,
        fig_trust_state=fig_trust_state,
        attestation_ref=attestation_ref,
    )

    active_store = store if store is not None else DurableAnchorStore(catalog_dir)
    register_anchor(
        catalog_dir,
        sidecar_ref,
        asset_sha256,
        _ANCHOR_KIND,
        label=_encode_typed_fields(entry),
        content_hash=input_hash,
        campaign_id=campaign_id,
        store=active_store,
    )
    return entry


def find_figure_entries(
    catalog_dir: object,
    *,
    asset_sha256: str | None = None,
    input_hash: str | None = None,
    campaign_id: str | None = None,
    store: AnchorStore | None = None,
) -> list[FigureEntry]:
    """Find registered figure_entry records matching all given (non-None) filters.

    Args:
        catalog_dir: Path to the bathos catalog root.
        asset_sha256: Filter by the figure's own anchor key.
        input_hash: Filter by the underlying data product's hash (a query key, not a
            figure_entry field — see module docstring).
        campaign_id: Filter by campaign.
        store: Explicit `AnchorStore` to read from instead of the default.

    Returns:
        A list of `FigureEntry` (empty if nothing matches, or if nothing has been
        registered yet). Malformed `kind="figure_entry"` anchors (e.g. written by
        something other than `register_figure_entry`) are silently skipped, not
        raised — read-back must stay always-callable per the S1 seam's contract.
    """
    records = find_anchors(
        catalog_dir,
        kind=_ANCHOR_KIND,
        sha256=asset_sha256,
        content_hash=input_hash,
        campaign_id=campaign_id,
        store=store,
    )
    entries = []
    for record in records:
        entry = _decode_typed_fields(
            sidecar_ref=record.path, asset_sha256=record.sha256, label=record.label
        )
        if entry is not None:
            entries.append(entry)
    return entries


def get_figure_entry(
    catalog_dir: object,
    asset_sha256: str,
    *,
    store: AnchorStore | None = None,
) -> FigureEntry | None:
    """Look up a single figure_entry by its anchor key (asset_sha256).

    Convenience wrapper over :func:`find_figure_entries` — returns the first match
    (asset_sha256 is the anchor key, so at most one figure_entry should match; if a
    caller re-registers under the same asset_sha256 with a different sidecar_ref, the
    underlying anchor upserts by (path, sha256), so the newest write for that
    (sidecar_ref, asset_sha256) pair wins).

    Returns:
        The matching `FigureEntry`, or None if not found.
    """
    found = find_figure_entries(catalog_dir, asset_sha256=asset_sha256, store=store)
    return found[0] if found else None
