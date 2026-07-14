"""Tests for the S2 anchor-insert WRITE seam (bathos.anchor).

Backlog item 3483 (task 260713_figure-eda-build-dag), foundation seam "2a": a minimal,
generic sidecar anchor-insert verb, mirroring the existing claim-anchor pattern
(bathos.claim.register_claim, which anchors a claim.bth.toml by path+sha256 into the
`campaigns` table) but generalized to any out-of-catalog sidecar.

This module composes with the S1 read-back seam (bathos.readback.figure_lookup, item
3482) — see tests/test_readback.py::TestFigureLookupComposesWithAnchorStore.

Adapter-first: register_anchor/get_anchor/find_anchors accept an explicit `store=`
override. The default (get_anchor_store) resolves to CatalogAnchorStore (DuckDB
warm-tier backed). InMemoryAnchorStore is an alternate implementation that must satisfy
the exact same behavioral contract — TestAlternateImplSatisfiesContract proves the seam
is swappable.

Explicitly NOT guaranteed by this seam (gated on unknown-3, #3485/#3486 — not decided
here):
- Durability across a warm-cache force-rebuild (TestNoDurabilityGuarantee).
- Cross-boundary visibility across distinct catalog_dirs (TestNoCrossBoundaryGuarantee).
"""

from __future__ import annotations

import dataclasses

import pytest

from bathos.anchor import (
    AnchorRecord,
    CatalogAnchorStore,
    InMemoryAnchorStore,
    find_anchors,
    get_anchor,
    get_anchor_store,
    register_anchor,
)
from bathos.compact import compact as compact_catalog


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    cat.mkdir(parents=True)
    return cat


class TestRegisterAndGetRoundTrip:
    """A caller can anchor an arbitrary sidecar by (path, sha256) and read it back."""

    def test_round_trip_default_catalog_backed_store(self, catalog_dir):
        record = register_anchor(
            catalog_dir,
            "sidecars/camp_1/figure_manifest.json",
            "a" * 64,
            "figure",
            label="main-result",
            content_hash="b" * 64,
            campaign_id="camp_1",
        )

        assert isinstance(record, AnchorRecord)
        assert record.path == "sidecars/camp_1/figure_manifest.json"
        assert record.sha256 == "a" * 64
        assert record.kind == "figure"

        fetched = get_anchor(catalog_dir, "sidecars/camp_1/figure_manifest.json", "a" * 64)
        assert fetched is not None
        assert fetched.sha256 == "a" * 64
        assert fetched.kind == "figure"
        assert fetched.label == "main-result"
        assert fetched.content_hash == "b" * 64
        assert fetched.campaign_id == "camp_1"

    def test_get_unregistered_anchor_returns_none(self, catalog_dir):
        assert get_anchor(catalog_dir, "never/anchored.json", "c" * 64) is None

    def test_get_distinguishes_by_sha256_not_just_path(self, catalog_dir):
        register_anchor(catalog_dir, "same/path.json", "a" * 64, "figure")

        # Same path, different sha256 => not the same anchor identity.
        assert get_anchor(catalog_dir, "same/path.json", "z" * 64) is None
        assert get_anchor(catalog_dir, "same/path.json", "a" * 64) is not None

    def test_reanchor_same_path_and_sha256_upserts(self, catalog_dir):
        register_anchor(catalog_dir, "p.json", "a" * 64, "figure", label="first")
        register_anchor(catalog_dir, "p.json", "a" * 64, "figure", label="second")

        fetched = get_anchor(catalog_dir, "p.json", "a" * 64)
        assert fetched.label == "second"


class TestFindAnchors:
    def test_find_by_kind(self, catalog_dir):
        register_anchor(catalog_dir, "fig.json", "a" * 64, "figure")
        register_anchor(catalog_dir, "attest.json", "b" * 64, "attestation")

        figures = find_anchors(catalog_dir, kind="figure")
        assert len(figures) == 1
        assert figures[0].path == "fig.json"

    def test_find_by_content_hash(self, catalog_dir):
        register_anchor(catalog_dir, "fig.json", "a" * 64, "figure", content_hash="deadbeef")
        register_anchor(catalog_dir, "other.json", "b" * 64, "figure", content_hash="feedface")

        matches = find_anchors(catalog_dir, content_hash="deadbeef")
        assert len(matches) == 1
        assert matches[0].path == "fig.json"

    def test_find_with_no_filters_and_no_anchors_returns_empty(self, catalog_dir):
        assert find_anchors(catalog_dir) == []


class TestAlternateImplSatisfiesContract:
    """The adapter seam is swappable: InMemoryAnchorStore satisfies the same contract
    as CatalogAnchorStore for insert/get/find. This is the required swap-proof test."""

    @pytest.fixture(params=["catalog", "memory"])
    def store(self, request, catalog_dir):
        if request.param == "catalog":
            return CatalogAnchorStore(catalog_dir)
        return InMemoryAnchorStore()

    def test_insert_then_get_round_trips(self, store):
        record = register_anchor(
            None, "p.json", "a" * 64, "figure", label="x", store=store
        )
        assert record.path == "p.json"

        fetched = get_anchor(None, "p.json", "a" * 64, store=store)
        assert fetched is not None
        assert fetched.label == "x"

    def test_get_missing_returns_none(self, store):
        assert get_anchor(None, "nope.json", "a" * 64, store=store) is None

    def test_find_by_kind(self, store):
        register_anchor(None, "fig.json", "a" * 64, "figure", store=store)
        register_anchor(None, "attest.json", "b" * 64, "attestation", store=store)

        results = find_anchors(None, kind="figure", store=store)
        assert [r.path for r in results] == ["fig.json"]

    def test_reanchor_upserts(self, store):
        register_anchor(None, "p.json", "a" * 64, "figure", label="first", store=store)
        register_anchor(None, "p.json", "a" * 64, "figure", label="second", store=store)

        fetched = get_anchor(None, "p.json", "a" * 64, store=store)
        assert fetched.label == "second"


class TestGetAnchorStoreFactory:
    """get_anchor_store is the single seam an unfavorable unknown-3 resolution would
    swap — register_anchor/get_anchor/find_anchors default to it and never
    instantiate CatalogAnchorStore directly except through this factory."""

    def test_default_factory_returns_catalog_backed_store(self, catalog_dir):
        store = get_anchor_store(catalog_dir)
        assert isinstance(store, CatalogAnchorStore)


class TestNoDurabilityGuarantee:
    """This seam makes NO durability guarantee: a warm-cache force-rebuild
    (bathos.compact.compact(..., force_rebuild=True)) deletes and recreates
    bathos.db from cool-tier parquet fragments. sidecar_anchors has no cool-tier
    fragment source, so anchored rows do not survive a force-rebuild. This is
    intentional — durability is gated on the 2b-A/2b-B unknown-3 resolution
    (#3485/#3486), not decided by this seam."""

    def test_anchor_does_not_survive_force_rebuild(self, catalog_dir):
        register_anchor(catalog_dir, "fig.json", "a" * 64, "figure")
        assert get_anchor(catalog_dir, "fig.json", "a" * 64) is not None

        compact_catalog(catalog_dir, force_rebuild=True)

        assert get_anchor(catalog_dir, "fig.json", "a" * 64) is None


class TestNoCrossBoundaryGuarantee:
    """This seam makes NO cross-boundary-callability guarantee: an anchor recorded
    against one catalog_dir is not visible from a different catalog_dir. There is no
    shared/networked anchor namespace — each catalog is its own local store. Whether
    a *different process* on the *same* catalog_dir can see the anchor is likewise
    unspecified by this seam (no locking/consistency protocol is asserted here); this
    test only pins down the cross-catalog_dir case, which is what #3485/#3486 would
    have to decide to change."""

    def test_anchor_not_visible_from_a_different_catalog_dir(self, tmp_path):
        catalog_a = tmp_path / "catalog_a"
        catalog_b = tmp_path / "catalog_b"
        catalog_a.mkdir()
        catalog_b.mkdir()

        register_anchor(catalog_a, "fig.json", "a" * 64, "figure")

        assert get_anchor(catalog_a, "fig.json", "a" * 64) is not None
        assert get_anchor(catalog_b, "fig.json", "a" * 64) is None


class TestAnchorRecordShape:
    def test_asdict_is_json_serializable_shape(self, catalog_dir):
        record = register_anchor(catalog_dir, "p.json", "a" * 64, "figure", label="x")
        d = dataclasses.asdict(record)
        assert set(d.keys()) == {
            "path",
            "sha256",
            "kind",
            "label",
            "content_hash",
            "campaign_id",
            "anchored_at",
        }
