"""Integration tests for the S1 read-back/query API (bathos.readback).

Backlog item 3482 (task 260713_figure-eda-build-dag, seam S1): a narrow read-back
surface exposed via both the MCP tool-server (bathos.mcp) and the CLI (bathos.cli),
implemented once in bathos.readback and re-exposed from there. Per spec
260713_figure-eda-coordination-system.md §3.4, the surface is seven functions:

    resolve_pin, get_trust_state, query_attestation,
    read_campaign_report, read_figure_manifest,
    figure_lookup, list_candidates

Three are REAL today (resolve_pin, read_campaign_report, read_figure_manifest) — they
wrap the existing run catalog / output_metadata and the existing campaign_report.py /
figure_manifest.py disk readers, which previously had no callable tool surface.

Four are intentional NULL-STUBS: their backing stores (trust ledger S3, attestation
sidecar S4, figure registry S7) do not exist yet. This module ships *before* those
stores exist and must return a well-defined empty/null result rather than raising —
that is the acceptance requirement for this item.

UPDATE (backlog item 3483, seam S2, bathos.anchor): figure_lookup is no longer a pure
null-stub. The generic sidecar-anchor store built in S2 gives it a real, minimal
backing store — figures anchored with kind="figure" are now visible here. It still
returns [] for any catalog with no matching anchors, which is observably identical to
the old null-stub behavior until a producer actually anchors a figure — see
TestFigureLookupComposesWithAnchorStore below. get_trust_state, query_attestation, and
list_candidates remain null-stubs (their stores, S3/S4, still don't exist).

This test file asserts all seven functions are callable against a live bathos catalog
fixture, that the real functions return real data, and that the null-stubs return their
documented empty/null sentinel.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from bathos.anchor import register_anchor
from bathos.campaign_report import CampaignReport
from bathos.catalog import init_catalog, write_run
from bathos.compact import compact as compact_catalog
from bathos.figure_manifest import FigureManifest
from bathos.query import CatalogError
from bathos.readback import (
    ProductTrustState,
    figure_lookup,
    get_trust_state,
    list_candidates,
    query_attestation,
    read_campaign_report,
    read_figure_manifest,
    resolve_pin,
)
from bathos.schema import Run


@pytest.fixture
def catalog_with_run(tmp_path):
    """A live bathos catalog with one run that has a real, hashed output file.

    output_metadata (the warm-tier per-output SHA256 that resolve_pin reads) is only
    populated at `bth compact` time by hashing `output_paths` on disk — it is not a
    cool-tier `write_run` field. So this fixture writes the cool-tier run record and then
    compacts to warm tier, exactly like a real bathos catalog would.
    """
    catalog_dir = tmp_path / "catalog"
    init_catalog(catalog_dir)

    output_file = tmp_path / "result.json"
    output_file.write_text('{"metric": 1.0}')
    sha256 = hashlib.sha256(output_file.read_bytes()).hexdigest()

    run = Run(
        project_slug="test_project",
        command="python analyze.py",
        argv=["python", "analyze.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        output_paths=[str(output_file)],
    )
    write_run(run, catalog_dir)
    compact_catalog(catalog_dir)  # builds bathos.db + computes output_metadata for real

    return catalog_dir, run, str(output_file), sha256


@pytest.fixture
def catalog_with_campaign_sidecars(tmp_path):
    """A catalog with a campaign_report.json + figure_manifest.json sidecar pair on disk.

    Written directly via the CampaignReport/FigureManifest models (rather than via
    emit_campaign_report/emit_figure_manifest, which need a DuckDB warm tier) — this test
    exercises the read-back side of the seam, not the emit pipeline.
    """
    catalog_dir = tmp_path / "catalog"
    campaign_id = "camp_test_001"
    sidecar_dir = catalog_dir / "sidecars" / campaign_id
    sidecar_dir.mkdir(parents=True)

    report = CampaignReport(
        report_version="1.0",
        campaign_id=campaign_id,
        total_runs=3,
        residual_rate=0.0,
        bypass_rate=0.0,
        unknown_rate=0.0,
        outcome_distribution={"success": 3},
        anomalies=[],
        stage_breakdown={"exploration": 3},
    )
    report.write_report(sidecar_dir / "campaign_report.json")

    manifest = FigureManifest(manifest_version="1.0", campaign_id=campaign_id, figures=[])
    manifest.write_manifest(sidecar_dir / "figure_manifest.json")

    return catalog_dir, campaign_id


class TestReadbackRealFunctions:
    """resolve_pin, read_campaign_report, read_figure_manifest wrap real, existing data."""

    def test_resolve_pin_returns_real_content_hash_and_freshness(self, catalog_with_run):
        catalog_dir, run, output_path, sha256 = catalog_with_run

        pin = resolve_pin(catalog_dir, run.id, output_path)

        assert pin.run_id == run.id
        assert pin.output_path == output_path
        assert pin.content_hash == sha256
        assert pin.fresh is True
        # trust_state is delegated to get_trust_state, which is a null-stub pending S3.
        assert pin.trust_state == ProductTrustState.UNKNOWN

    def test_resolve_pin_detects_drift(self, catalog_with_run):
        catalog_dir, run, output_path, _sha256 = catalog_with_run
        Path(output_path).write_text('{"metric": 2.0}')  # mutate the on-disk file

        pin = resolve_pin(catalog_dir, run.id, output_path)

        assert pin.fresh is False

    def test_resolve_pin_missing_output_file_is_not_fresh(self, catalog_with_run):
        catalog_dir, run, output_path, sha256 = catalog_with_run
        Path(output_path).unlink()

        pin = resolve_pin(catalog_dir, run.id, output_path)

        assert pin.content_hash == sha256  # still the recorded hash
        assert pin.fresh is False

    def test_resolve_pin_untracked_output_path_returns_null_hash(self, catalog_with_run):
        catalog_dir, run, _output_path, _sha256 = catalog_with_run

        pin = resolve_pin(catalog_dir, run.id, "/never/tracked/path.json")

        assert pin.content_hash is None
        assert pin.fresh is False

    def test_resolve_pin_unknown_run_raises(self, catalog_with_run):
        catalog_dir, _run, output_path, _sha256 = catalog_with_run

        with pytest.raises(CatalogError):
            resolve_pin(catalog_dir, "nonexistent-run-id", output_path)

    def test_read_campaign_report_wraps_disk_reader(self, catalog_with_campaign_sidecars):
        catalog_dir, campaign_id = catalog_with_campaign_sidecars

        report = read_campaign_report(catalog_dir, campaign_id)

        assert isinstance(report, CampaignReport)
        assert report.campaign_id == campaign_id
        assert report.total_runs == 3
        assert report.outcome_distribution == {"success": 3}

    def test_read_campaign_report_missing_raises(self, catalog_with_campaign_sidecars):
        catalog_dir, _campaign_id = catalog_with_campaign_sidecars

        with pytest.raises(FileNotFoundError):
            read_campaign_report(catalog_dir, "no-such-campaign")

    def test_read_figure_manifest_wraps_disk_reader(self, catalog_with_campaign_sidecars):
        catalog_dir, campaign_id = catalog_with_campaign_sidecars

        manifest = read_figure_manifest(catalog_dir, campaign_id)

        assert isinstance(manifest, FigureManifest)
        assert manifest.campaign_id == campaign_id
        assert manifest.figures == []

    def test_read_figure_manifest_missing_raises(self, catalog_with_campaign_sidecars):
        catalog_dir, _campaign_id = catalog_with_campaign_sidecars

        with pytest.raises(FileNotFoundError):
            read_figure_manifest(catalog_dir, "no-such-campaign")


class TestReadbackNullStubs:
    """get_trust_state, query_attestation, list_candidates ship before their backing
    stores exist (S3/S4). Acceptance requirement: callable today, well-defined
    empty/null result, never raise. figure_lookup is exercised here too (empty-catalog
    case) — it now composes with the S2 anchor store (see
    TestFigureLookupComposesWithAnchorStore) but is observably empty for any catalog
    that has no matching anchors, same as before S2 shipped."""

    def test_get_trust_state_returns_unknown(self, catalog_with_run):
        catalog_dir, _run, _output_path, sha256 = catalog_with_run

        assert get_trust_state(catalog_dir, sha256) == ProductTrustState.UNKNOWN

    def test_get_trust_state_handles_none_hash(self, catalog_with_run):
        catalog_dir, _run, _output_path, _sha256 = catalog_with_run

        assert get_trust_state(catalog_dir, None) == ProductTrustState.UNKNOWN

    def test_query_attestation_returns_none(self, catalog_with_run):
        catalog_dir, _run, _output_path, sha256 = catalog_with_run

        assert query_attestation(catalog_dir, sha256) is None
        assert query_attestation(catalog_dir, sha256, min_strength="oracle_match") is None

    def test_figure_lookup_returns_empty_list(self, catalog_with_campaign_sidecars):
        catalog_dir, _campaign_id = catalog_with_campaign_sidecars

        assert figure_lookup(catalog_dir, asset_sha256="deadbeef") == []
        assert figure_lookup(catalog_dir, input_hash="deadbeef") == []
        assert figure_lookup(catalog_dir) == []

    def test_list_candidates_returns_empty_list(self, catalog_with_campaign_sidecars):
        catalog_dir, campaign_id = catalog_with_campaign_sidecars

        assert list_candidates(catalog_dir, campaign_id) == []


class TestFigureLookupComposesWithAnchorStore:
    """S2 (bathos.anchor, item 3483) gives figure_lookup a real backing store: an
    anchor registered with kind="figure" becomes visible via figure_lookup by either
    its own sha256 (asset_sha256) or its content_hash (input_hash, the underlying
    data product's hash)."""

    def test_figure_lookup_finds_anchor_by_asset_sha256(self, catalog_with_campaign_sidecars):
        catalog_dir, campaign_id = catalog_with_campaign_sidecars
        register_anchor(
            catalog_dir,
            f"sidecars/{campaign_id}/figure_manifest.json",
            "a" * 64,
            "figure",
            content_hash="b" * 64,
            campaign_id=campaign_id,
        )

        results = figure_lookup(catalog_dir, asset_sha256="a" * 64)

        assert len(results) == 1
        assert results[0]["sha256"] == "a" * 64
        assert results[0]["content_hash"] == "b" * 64
        assert results[0]["campaign_id"] == campaign_id

    def test_figure_lookup_finds_anchor_by_input_hash(self, catalog_with_campaign_sidecars):
        catalog_dir, campaign_id = catalog_with_campaign_sidecars
        register_anchor(
            catalog_dir, "fig.png", "a" * 64, "figure", content_hash="b" * 64
        )

        results = figure_lookup(catalog_dir, input_hash="b" * 64)

        assert len(results) == 1
        assert results[0]["path"] == "fig.png"

    def test_figure_lookup_ignores_non_figure_anchors(self, catalog_with_campaign_sidecars):
        catalog_dir, _campaign_id = catalog_with_campaign_sidecars
        register_anchor(catalog_dir, "attest.json", "a" * 64, "attestation")

        assert figure_lookup(catalog_dir, asset_sha256="a" * 64) == []

    def test_figure_lookup_still_empty_with_no_matching_anchor(
        self, catalog_with_campaign_sidecars
    ):
        catalog_dir, _campaign_id = catalog_with_campaign_sidecars
        register_anchor(catalog_dir, "fig.png", "a" * 64, "figure")

        assert figure_lookup(catalog_dir, asset_sha256="nonmatching") == []


class TestReadbackSurfaceIsFullyCallable:
    """All seven S1 functions are callable against one live catalog fixture."""

    def test_all_seven_functions_callable(
        self, catalog_with_run, catalog_with_campaign_sidecars
    ):
        run_catalog_dir, run, output_path, sha256 = catalog_with_run
        report_catalog_dir, campaign_id = catalog_with_campaign_sidecars

        pin = resolve_pin(run_catalog_dir, run.id, output_path)
        assert pin.content_hash == sha256

        assert get_trust_state(run_catalog_dir, sha256) == ProductTrustState.UNKNOWN
        assert query_attestation(run_catalog_dir, sha256) is None

        report = read_campaign_report(report_catalog_dir, campaign_id)
        assert report.campaign_id == campaign_id

        manifest = read_figure_manifest(report_catalog_dir, campaign_id)
        assert manifest.campaign_id == campaign_id

        assert figure_lookup(run_catalog_dir, asset_sha256=sha256) == []
        assert list_candidates(report_catalog_dir, campaign_id) == []
