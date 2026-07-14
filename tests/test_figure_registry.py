"""Tests for the figure registry (S7): typed, pointer-only `figure_entry` schema.

task_id: 260713_figure-eda-build-dag, backlog item 3490. Spec:
`.praxia/docs/specs/260713_figure-eda-coordination-system.md` (maraxiom repo) §3.3
+ §7. Builds on the merged anchor store (`bathos.anchor`, items 3482/3483/#3485)
merged to main at e924191 (DurableAnchorStore) / 1428ead (AnchorStore seam) /
4252d92 (S1 read-back).

Test-first (red -> green): this file is written before `bathos.figure_registry`
exists, per the dispatch's TDD requirement.

What this proves:
1. `build_figure_entry` / `register_figure_entry` REJECT any inline
   verdict/strength/content_hash/outcome/gate field (ADR-enforced, spec §3.3/§7,
   Critical bullet) — mirrors maraxiom's FigureSidecar `extra="forbid"` pattern
   but with an explicit, named guard so the rejection message is unambiguous
   about *why* (a figure carries a POINTER to an attestation, never its verdict).
2. A figure entry can be registered (anchored by asset_sha256) carrying only the
   pointer-only fields, and retrieved via `find_figure_entries` / composed into
   `bathos.readback.figure_lookup` — both by asset_sha256 and by input_hash.
3. Registration defaults to `DurableAnchorStore`, so entries survive
   `bathos.compact.compact(catalog_dir, force_rebuild=True)` and remain queryable.
4. The typed schema composes with, rather than replaces, the S2-era minimal
   figure_lookup shape (kind="figure" anchors) — regression-proofed against
   tests/test_readback.py::TestFigureLookupComposesWithAnchorStore.
"""

from __future__ import annotations

import pytest

from bathos.anchor import CatalogAnchorStore, DurableAnchorStore, register_anchor
from bathos.compact import compact as compact_catalog
from bathos.figure_manifest import RenderState
from bathos.figure_registry import (
    FigTrustState,
    FigureEntry,
    FigureEntrySchemaError,
    build_figure_entry,
    find_figure_entries,
    get_figure_entry,
    register_figure_entry,
)
from bathos.readback import figure_lookup


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    cat.mkdir(parents=True)
    return cat


VALID_FIELDS = dict(
    asset_sha256="a" * 64,
    sidecar_ref="sidecars/camp-001/fig_chord.figure.toml",
    figure_kind="chord_diagram",
    render_state="ready",
    fig_trust_state="draft",
    attestation_ref="deadbeef" * 8,
)


class TestFigureEntrySchemaRejectsForbiddenInlineFields:
    """Critical/ADR-enforced guard (spec §3.3/§7): a figure_entry carries only a
    POINTER (attestation_ref) to a bathos-side attestation, never the verdict
    itself. verdict/strength/content_hash/outcome/gate must never appear inline.
    """

    @pytest.mark.parametrize("forbidden_field", ["verdict", "strength", "content_hash", "outcome", "gate"])
    def test_build_figure_entry_rejects_each_forbidden_field(self, forbidden_field):
        payload = dict(VALID_FIELDS)
        payload[forbidden_field] = "PASS"

        with pytest.raises(FigureEntrySchemaError) as exc_info:
            build_figure_entry(**payload)

        assert forbidden_field in str(exc_info.value)

    def test_build_figure_entry_rejects_multiple_forbidden_fields_at_once(self):
        payload = dict(VALID_FIELDS)
        payload["verdict"] = "PASS"
        payload["gate"] = "go"

        with pytest.raises(FigureEntrySchemaError) as exc_info:
            build_figure_entry(**payload)

        message = str(exc_info.value)
        assert "verdict" in message
        assert "gate" in message

    def test_build_figure_entry_accepts_pointer_only_payload(self):
        entry = build_figure_entry(**VALID_FIELDS)

        assert entry.asset_sha256 == VALID_FIELDS["asset_sha256"]
        assert entry.sidecar_ref == VALID_FIELDS["sidecar_ref"]
        assert entry.figure_kind == "chord_diagram"
        assert entry.render_state == "ready"
        assert entry.fig_trust_state == "draft"
        assert entry.attestation_ref == VALID_FIELDS["attestation_ref"]
        # No forbidden attribute exists on the type at all.
        for bad in ("verdict", "strength", "content_hash", "outcome", "gate"):
            assert not hasattr(entry, bad)

    def test_register_figure_entry_rejects_forbidden_kwarg(self, catalog_dir):
        with pytest.raises(FigureEntrySchemaError) as exc_info:
            register_figure_entry(
                catalog_dir,
                asset_sha256="b" * 64,
                sidecar_ref="fig.figure.toml",
                figure_kind="heatmap",
                verdict="PASS",  # forbidden: attestation verdict inlined
            )
        assert "verdict" in str(exc_info.value)

        # Rejection happens before any write — nothing should be anchored.
        assert find_figure_entries(catalog_dir, asset_sha256="b" * 64) == []


class TestFigureEntryValidatesEnumFields:
    def test_rejects_invalid_render_state(self):
        payload = dict(VALID_FIELDS)
        payload["render_state"] = "not-a-real-state"
        with pytest.raises(ValueError):
            build_figure_entry(**payload)

    def test_rejects_invalid_fig_trust_state(self):
        payload = dict(VALID_FIELDS)
        payload["fig_trust_state"] = "not-a-real-state"
        with pytest.raises(ValueError):
            build_figure_entry(**payload)

    def test_accepts_deferred_render_state_and_final_trust_state(self):
        payload = dict(VALID_FIELDS)
        payload["render_state"] = RenderState.DEFERRED.value
        payload["fig_trust_state"] = FigTrustState.FINAL.value
        entry = build_figure_entry(**payload)
        assert entry.render_state == "deferred"
        assert entry.fig_trust_state == "final"


class TestRegisterAndLookupFigureEntry:
    """Registration + retrieval, both directly (find_figure_entries) and composed
    into the S1 read-back surface (bathos.readback.figure_lookup)."""

    def test_register_then_find_by_asset_sha256(self, catalog_dir):
        entry = register_figure_entry(
            catalog_dir,
            asset_sha256="c" * 64,
            sidecar_ref="sidecars/camp/fig.figure.toml",
            figure_kind="mi_heatmap",
            render_state="ready",
            fig_trust_state="final",
            attestation_ref="e" * 64,
            campaign_id="camp-007",
        )
        assert isinstance(entry, FigureEntry)

        found = find_figure_entries(catalog_dir, asset_sha256="c" * 64)
        assert len(found) == 1
        assert found[0].asset_sha256 == "c" * 64
        assert found[0].sidecar_ref == "sidecars/camp/fig.figure.toml"
        assert found[0].figure_kind == "mi_heatmap"
        assert found[0].render_state == "ready"
        assert found[0].fig_trust_state == "final"
        assert found[0].attestation_ref == "e" * 64

    def test_register_then_find_by_input_hash(self, catalog_dir):
        register_figure_entry(
            catalog_dir,
            asset_sha256="d" * 64,
            sidecar_ref="fig_scatter.figure.toml",
            figure_kind="scatter",
            input_hash="f" * 64,
        )

        found = find_figure_entries(catalog_dir, input_hash="f" * 64)
        assert len(found) == 1
        assert found[0].asset_sha256 == "d" * 64

    def test_get_figure_entry_returns_none_when_absent(self, catalog_dir):
        assert get_figure_entry(catalog_dir, asset_sha256="nope" * 16) is None

    def test_get_figure_entry_round_trips(self, catalog_dir):
        register_figure_entry(
            catalog_dir,
            asset_sha256="1" * 64,
            sidecar_ref="fig_one.figure.toml",
            figure_kind="bar_chart",
        )
        got = get_figure_entry(catalog_dir, asset_sha256="1" * 64)
        assert got is not None
        assert got.figure_kind == "bar_chart"

    def test_figure_lookup_returns_typed_entry_by_asset_sha256(self, catalog_dir):
        register_figure_entry(
            catalog_dir,
            asset_sha256="2" * 64,
            sidecar_ref="fig_two.figure.toml",
            figure_kind="violin",
            render_state="deferred",
            fig_trust_state="draft",
        )

        results = figure_lookup(catalog_dir, asset_sha256="2" * 64)
        assert len(results) == 1
        assert results[0]["asset_sha256"] == "2" * 64
        assert results[0]["sidecar_ref"] == "fig_two.figure.toml"
        assert results[0]["figure_kind"] == "violin"
        assert results[0]["render_state"] == "deferred"
        assert results[0]["fig_trust_state"] == "draft"
        # Forbidden fields must never appear in the returned payload either.
        for bad in ("verdict", "strength", "content_hash", "outcome", "gate"):
            assert bad not in results[0]

    def test_figure_lookup_returns_typed_entry_by_input_hash(self, catalog_dir):
        register_figure_entry(
            catalog_dir,
            asset_sha256="3" * 64,
            sidecar_ref="fig_three.figure.toml",
            figure_kind="dag",
            input_hash="9" * 64,
        )

        results = figure_lookup(catalog_dir, input_hash="9" * 64)
        assert len(results) == 1
        assert results[0]["asset_sha256"] == "3" * 64

    def test_figure_lookup_composes_typed_and_legacy_anchors(self, catalog_dir):
        """Legacy S2 kind="figure" anchors and the new typed figure_entry registry
        both compose into one figure_lookup call, keyed on the same asset_sha256 —
        proving S7 is additive, not a breaking replacement of the S1-era shape."""
        register_anchor(
            catalog_dir, "legacy_fig.png", "5" * 64, "figure", label="legacy-label"
        )
        register_figure_entry(
            catalog_dir,
            asset_sha256="5" * 64,
            sidecar_ref="fig_five.figure.toml",
            figure_kind="chord_diagram",
        )

        results = figure_lookup(catalog_dir, asset_sha256="5" * 64)
        assert len(results) == 2
        shapes = {tuple(sorted(r.keys())) for r in results}
        assert len(shapes) == 2  # legacy dict shape != typed figure_entry shape


class TestFigureEntrySurvivesForceRebuild:
    """Registration defaults to DurableAnchorStore (gate #3485, merged to main) so
    entries survive bathos.compact.compact(catalog_dir, force_rebuild=True)."""

    def test_default_registration_is_durable(self, catalog_dir):
        register_figure_entry(
            catalog_dir,
            asset_sha256="6" * 64,
            sidecar_ref="fig_six.figure.toml",
            figure_kind="chord_diagram",
            campaign_id="camp-durable",
        )

        before = find_figure_entries(catalog_dir, asset_sha256="6" * 64)
        assert len(before) == 1

        result = compact_catalog(catalog_dir, force_rebuild=True)
        assert result is not None

        after = find_figure_entries(catalog_dir, asset_sha256="6" * 64)
        assert len(after) == 1
        assert after[0].figure_kind == "chord_diagram"
        assert after[0].sidecar_ref == "fig_six.figure.toml"

    def test_survives_via_readback_figure_lookup_after_force_rebuild(self, catalog_dir):
        register_figure_entry(
            catalog_dir,
            asset_sha256="7" * 64,
            sidecar_ref="fig_seven.figure.toml",
            figure_kind="heatmap",
            fig_trust_state="final",
            attestation_ref="8" * 64,
        )

        compact_catalog(catalog_dir, force_rebuild=True)

        results = figure_lookup(catalog_dir, asset_sha256="7" * 64)
        assert len(results) == 1
        assert results[0]["fig_trust_state"] == "final"
        assert results[0]["attestation_ref"] == "8" * 64

    def test_explicit_non_durable_store_does_not_survive_rebuild(self, catalog_dir):
        """Regression guard mirroring test_anchor_durability.py's
        TestExistingNonDurableSeamUnaffected: explicitly passing a plain
        CatalogAnchorStore (not durable) must NOT survive force_rebuild — proves
        durability is a property of the store, not something figure_registry fakes."""
        plain_store = CatalogAnchorStore(catalog_dir)
        register_figure_entry(
            catalog_dir,
            asset_sha256="a1" * 32,
            sidecar_ref="fig_plain.figure.toml",
            figure_kind="scatter",
            store=plain_store,
        )
        assert find_figure_entries(catalog_dir, asset_sha256="a1" * 32, store=plain_store) != []

        compact_catalog(catalog_dir, force_rebuild=True)

        assert find_figure_entries(catalog_dir, asset_sha256="a1" * 32, store=plain_store) == []

    def test_alternate_store_still_durable_when_durable_type_given(self, catalog_dir):
        explicit_durable = DurableAnchorStore(catalog_dir)
        register_figure_entry(
            catalog_dir,
            asset_sha256="b2" * 32,
            sidecar_ref="fig_explicit.figure.toml",
            figure_kind="scatter",
            store=explicit_durable,
        )

        compact_catalog(catalog_dir, force_rebuild=True)

        assert find_figure_entries(catalog_dir, asset_sha256="b2" * 32) != []
