"""Force-rebuild durability proof for the DE-RISK spike (gate 2b-A, #3485).

task_id: 260713_figure-eda-build-dag. Branch: figure-eda-2bA-durability-spike (NOT on
main, NOT merged — this is a de-risk spike producing a RECOMMENDED verdict for the
owner to sign off, per the dispatch brief).

Question this file answers: can a durable anchor/ledger record be built on bathos's
existing durable-append machinery (cool-tier Parquet fragment + compact-time
re-ingest, the same pattern that already makes `runs` durable) so that it survives a
warm-cache force-rebuild (`bathos.compact.compact(catalog_dir, force_rebuild=True)`)
and remains queryable via the S1 read-back API?

Contrast with tests/test_anchor.py::TestNoDurabilityGuarantee, which proves the
*existing* S2 seam (`CatalogAnchorStore`, warm-tier only) does NOT survive
force_rebuild — that test must keep passing unchanged; this file proves the *new*
`DurableAnchorStore` (a `CatalogAnchorStore` subclass that also writes a cool-tier
Parquet fragment, ingested back into the warm table by a `compact.py` addition
mirroring the runs-ingest loop) does.
"""

from __future__ import annotations

import pytest

from bathos.anchor import DurableAnchorStore, get_anchor, register_anchor
from bathos.compact import compact as compact_catalog
from bathos.readback import figure_lookup


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    cat.mkdir(parents=True)
    return cat


class TestDurableAnchorSurvivesForceRebuild:
    """The artifact this gate requires: anchor -> force_rebuild -> still queryable."""

    def test_anchor_survives_force_rebuild_via_get_anchor(self, catalog_dir):
        store = DurableAnchorStore(catalog_dir)
        register_anchor(
            catalog_dir,
            "fig_mi_heatmap.svg",
            "b" * 64,
            "figure",
            label="mi-heatmap-v1",
            content_hash="c" * 64,
            campaign_id="camp-001",
            store=store,
        )

        # Sanity: present before rebuild (round-trips through the warm tier, same as
        # CatalogAnchorStore).
        assert get_anchor(catalog_dir, "fig_mi_heatmap.svg", "b" * 64, store=store) is not None

        result = compact_catalog(catalog_dir, force_rebuild=True)
        assert result is not None  # compact ran to completion, did not raise

        # The warm bathos.db was deleted and recreated from cool-tier data only.
        # The durable variant must still resolve the anchor after rebuild.
        after = get_anchor(catalog_dir, "fig_mi_heatmap.svg", "b" * 64, store=store)
        assert after is not None
        assert after.path == "fig_mi_heatmap.svg"
        assert after.sha256 == "b" * 64
        assert after.kind == "figure"
        assert after.label == "mi-heatmap-v1"
        assert after.content_hash == "c" * 64
        assert after.campaign_id == "camp-001"

    def test_anchor_survives_force_rebuild_via_s1_readback_figure_lookup(self, catalog_dir):
        """Queryable via the S1 read-back API specifically, per the gate's spec
        reference (§7.5): figure_lookup composes with the anchor store per
        bathos.readback's own docstring (item 3483 UPDATE)."""
        store = DurableAnchorStore(catalog_dir)
        register_anchor(
            catalog_dir,
            "fig_chord_diagram.svg",
            "d" * 64,
            "figure",
            label="chord-v2",
            content_hash="e" * 64,
            campaign_id="camp-002",
            store=store,
        )

        before = figure_lookup(catalog_dir, asset_sha256="d" * 64)
        assert len(before) == 1
        assert before[0]["figure_id"] == "chord-v2"

        compact_catalog(catalog_dir, force_rebuild=True)

        after = figure_lookup(catalog_dir, asset_sha256="d" * 64)
        assert len(after) == 1
        assert after[0]["figure_id"] == "chord-v2"
        assert after[0]["content_hash"] == "e" * 64
        assert after[0]["campaign_id"] == "camp-002"

    def test_multiple_anchors_and_multiple_force_rebuilds_all_survive(self, catalog_dir):
        """Repeated force_rebuild calls (as would happen in normal operation, e.g.
        recovery from a corrupt bathos.db) must not lose any previously-anchored
        record, and newly-anchored records after a rebuild must also survive the
        *next* rebuild."""
        store = DurableAnchorStore(catalog_dir)
        register_anchor(catalog_dir, "a.svg", "1" * 64, "figure", store=store)
        compact_catalog(catalog_dir, force_rebuild=True)

        register_anchor(catalog_dir, "b.svg", "2" * 64, "figure", store=store)
        compact_catalog(catalog_dir, force_rebuild=True)

        assert get_anchor(catalog_dir, "a.svg", "1" * 64, store=store) is not None
        assert get_anchor(catalog_dir, "b.svg", "2" * 64, store=store) is not None

    def test_re_anchor_upsert_survives_rebuild_with_latest_fields(self, catalog_dir):
        """Re-anchoring the same (path, sha256) with new label/content_hash before a
        rebuild must resolve to the LATEST fields after rebuild, not stale ones —
        proving the cool-fragment fold-latest-wins logic matches the warm tier's
        upsert-on-conflict semantics."""
        store = DurableAnchorStore(catalog_dir)
        register_anchor(
            catalog_dir, "c.svg", "3" * 64, "figure", label="v1", store=store
        )
        register_anchor(
            catalog_dir, "c.svg", "3" * 64, "figure", label="v2", store=store
        )

        compact_catalog(catalog_dir, force_rebuild=True)

        after = get_anchor(catalog_dir, "c.svg", "3" * 64, store=store)
        assert after is not None
        assert after.label == "v2"


class TestExistingNonDurableSeamUnaffected:
    """Regression guard: this spike must not change CatalogAnchorStore's documented
    behavior (tests/test_anchor.py::TestNoDurabilityGuarantee) — the spike is
    additive (a new store class + an additive compact.py ingest step), not a change
    to the default get_anchor_store() factory or CatalogAnchorStore itself."""

    def test_catalog_anchor_store_still_does_not_survive_force_rebuild(self, catalog_dir):
        from bathos.anchor import CatalogAnchorStore

        plain_store = CatalogAnchorStore(catalog_dir)
        register_anchor(catalog_dir, "fig.json", "a" * 64, "figure", store=plain_store)
        assert get_anchor(catalog_dir, "fig.json", "a" * 64, store=plain_store) is not None

        compact_catalog(catalog_dir, force_rebuild=True)

        assert get_anchor(catalog_dir, "fig.json", "a" * 64, store=plain_store) is None
