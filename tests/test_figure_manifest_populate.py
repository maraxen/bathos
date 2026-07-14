"""Tests for S8 figure_manifest population (backlog item 3496, task 260713_figure-eda-build-dag).

Spec: `.praxia/docs/specs/260713_figure-eda-coordination-system.md` (maraxiom repo)
§3.4 + §6.

Prior to this item, `bathos.campaigns.emit_figure_manifest` hardcoded `figures=[]` (a
stub) regardless of what had been registered in the S7 figure registry
(`bathos.figure_registry`, item 3490, merged to main at 4a23859). This test-first
suite (written before the implementation change, red -> green) proves:

1. `emit_figure_manifest` now populates `figures[]` from the anchored `figure_entry`
   records for the campaign (`bathos.figure_registry.find_figure_entries`).
2. The mapping from the registry's pointer-only shape
   (`asset_sha256`/`sidecar_ref`/`figure_kind`/`render_state`/`fig_trust_state`/
   `attestation_ref`) to the manifest's intent-focused shape
   (`figure_id`/`intent`/`input_pins`/`render_state`/`figure_kind`) is faithful:
   `figure_kind` and `render_state` pass through unchanged; `figure_id` is derived
   deterministically from `sidecar_ref`'s filename; `intent`/`input_pins` have no
   clean 1:1 source on the pointer-only registry entry and are NOT fabricated (empty
   pins, a clearly-synthesized intent string -- see
   `bathos.campaigns._figure_manifest_entry_from_registry`).
3. A campaign with no registered figure_entry records still gets `figures=[]`
   cleanly (not an error).
4. `read_figure_manifest` (the S1 read-back seam, `bathos.readback`) round-trips the
   populated manifest from disk unchanged -- it is a thin disk reader and needed no
   code change, only this proof that what `emit_figure_manifest` now writes survives
   the read-back seam.
"""

from __future__ import annotations

import duckdb
import pytest

from bathos.campaigns import create_campaign, emit_figure_manifest
from bathos.catalog import init_catalog
from bathos.compact import compact
from bathos.figure_manifest import RenderState as ManifestRenderState
from bathos.figure_registry import FigTrustState, register_figure_entry
from bathos.readback import read_figure_manifest


@pytest.fixture
def campaign_catalog(tmp_path):
    """A live, compacted bathos catalog (warm tier) with one open campaign."""
    catalog_dir = tmp_path / "catalog"
    init_catalog(catalog_dir)
    compact(catalog_dir)  # builds bathos.db + campaigns/campaign_runs tables

    db = duckdb.connect(str(catalog_dir / "bathos.db"))
    campaign = create_campaign(
        db, name="figure-manifest-populate", project_slug="testproj", mode="exploration"
    )
    yield catalog_dir, db, campaign.id
    db.close()


class TestEmitFigureManifestPopulatesFromRegistry:
    def test_populates_figures_from_registered_entries(self, campaign_catalog):
        catalog_dir, db, campaign_id = campaign_catalog

        register_figure_entry(
            catalog_dir,
            asset_sha256="a" * 64,
            sidecar_ref=f"sidecars/{campaign_id}/fig_chord.figure.toml",
            figure_kind="chord_diagram",
            render_state="ready",
            fig_trust_state=FigTrustState.DRAFT.value,
            campaign_id=campaign_id,
        )
        register_figure_entry(
            catalog_dir,
            asset_sha256="b" * 64,
            sidecar_ref=f"sidecars/{campaign_id}/fig_scatter.figure.toml",
            figure_kind="scatter",
            render_state="deferred",
            fig_trust_state=FigTrustState.FINAL.value,
            campaign_id=campaign_id,
        )

        emit_figure_manifest(db, str(catalog_dir), campaign_id)
        manifest = read_figure_manifest(catalog_dir, campaign_id)

        assert len(manifest.figures) == 2
        by_id = {f.figure_id: f for f in manifest.figures}
        assert set(by_id) == {"fig_chord", "fig_scatter"}

        assert by_id["fig_chord"].figure_kind == "chord_diagram"
        assert by_id["fig_chord"].render_state == ManifestRenderState.READY
        assert by_id["fig_scatter"].figure_kind == "scatter"
        assert by_id["fig_scatter"].render_state == ManifestRenderState.DEFERRED

        # intent/input_pins have no clean 1:1 source on the pointer-only registry
        # entry -- proved non-fabricated: pins stay empty, intent is a synthesized
        # (clearly not authorial) string, never silently dropped/omitted.
        assert by_id["fig_chord"].input_pins == []
        assert by_id["fig_scatter"].input_pins == []
        assert isinstance(by_id["fig_chord"].intent, str) and by_id["fig_chord"].intent

    def test_figure_id_derived_from_sidecar_ref_filename(self, campaign_catalog):
        """figure_id is not a registry field -- derive it from sidecar_ref's stem."""
        catalog_dir, db, campaign_id = campaign_catalog
        register_figure_entry(
            catalog_dir,
            asset_sha256="c" * 64,
            sidecar_ref="just_a_name.figure.toml",
            figure_kind="heatmap",
            campaign_id=campaign_id,
        )

        emit_figure_manifest(db, str(catalog_dir), campaign_id)
        manifest = read_figure_manifest(catalog_dir, campaign_id)

        assert manifest.figures[0].figure_id == "just_a_name"

    def test_no_figure_entries_returns_empty_figures_cleanly(self, campaign_catalog):
        """A campaign with no registered figure_entry records still emits figures=[]."""
        catalog_dir, db, campaign_id = campaign_catalog

        emit_figure_manifest(db, str(catalog_dir), campaign_id)
        manifest = read_figure_manifest(catalog_dir, campaign_id)

        assert manifest.figures == []

    def test_only_matching_campaign_included(self, campaign_catalog):
        """A figure_entry registered under a DIFFERENT campaign must not leak in."""
        catalog_dir, db, campaign_id = campaign_catalog
        other_campaign = create_campaign(
            db, name="other", project_slug="testproj", mode="exploration"
        )
        register_figure_entry(
            catalog_dir,
            asset_sha256="d" * 64,
            sidecar_ref="sidecars/other/fig_other.figure.toml",
            figure_kind="scatter",
            campaign_id=other_campaign.id,
        )

        emit_figure_manifest(db, str(catalog_dir), campaign_id)
        manifest = read_figure_manifest(catalog_dir, campaign_id)

        assert manifest.figures == []

    def test_manifest_is_json_serializable_and_reloadable(self, campaign_catalog):
        """Populated figures survive a full write -> disk -> read round trip."""
        catalog_dir, db, campaign_id = campaign_catalog
        register_figure_entry(
            catalog_dir,
            asset_sha256="e" * 64,
            sidecar_ref=f"sidecars/{campaign_id}/fig_roundtrip.figure.toml",
            figure_kind="mi_heatmap",
            render_state="ready",
            campaign_id=campaign_id,
        )

        emit_figure_manifest(db, str(catalog_dir), campaign_id)

        manifest_path = catalog_dir / "sidecars" / campaign_id / "figure_manifest.json"
        assert manifest_path.exists()

        manifest = read_figure_manifest(catalog_dir, campaign_id)
        assert manifest.campaign_id == campaign_id
        assert manifest.figures[0].figure_id == "fig_roundtrip"
        assert manifest.figures[0].figure_kind == "mi_heatmap"
