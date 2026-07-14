"""Integration tests for the S1 read-back/query API (bathos.readback).

Backlog item 3482 (task 260713_figure-eda-build-dag, seam S1): a narrow read-back
surface exposed via both the MCP tool-server (bathos.mcp) and the CLI (bathos.cli),
implemented once in bathos.readback and re-exposed from there. Per spec
260713_figure-eda-coordination-system.md §3.4, the surface is seven functions:

    resolve_pin, get_trust_state, query_attestation,
    read_campaign_report, read_figure_manifest,
    figure_lookup, list_candidates

All seven are REAL as of seam S3 (bathos.trust_ledger, backlog item 3491) — they wrap
the existing run catalog / output_metadata, the existing campaign_report.py /
figure_manifest.py disk readers, the S2 anchor store, the S4 attestation store, and now
the durable trust ledger.

UPDATE (backlog item 3483, seam S2, bathos.anchor): figure_lookup is backed by the
generic sidecar-anchor store — figures anchored with kind="figure" are visible here.
See TestFigureLookupComposesWithAnchorStore below.

UPDATE (backlog item 3491, seam S3, bathos.trust_ledger): get_trust_state and
list_candidates are no longer null-stubs. get_trust_state now returns the
owner-confirmed implicit 3-state model (unknown/candidate/promoted, see
TestGetTrustStateThreeStates below); list_candidates joins anchors + run outputs for a
campaign, filtered to not-yet-promoted (see TestListCandidatesRealBehavior below).

This test file asserts all seven functions are callable against a live bathos catalog
fixture and that each returns real, correctly-composed data.
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
        # trust_state is delegated to get_trust_state (S3, real): a run's recorded
        # output sha256 is a registered-but-not-promoted product, i.e. "candidate".
        assert pin.trust_state == ProductTrustState.CANDIDATE

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


class TestReadbackEmptyCatalogBehavior:
    """query_attestation and figure_lookup return well-defined empty/null results
    against a catalog with nothing registered for the hash/campaign in question —
    this is unchanged by S3 landing (get_trust_state/list_candidates moved to their
    own real-behavior test classes below, since they are no longer null-stubs)."""

    def test_query_attestation_returns_none(self, catalog_with_run):
        catalog_dir, _run, _output_path, sha256 = catalog_with_run

        assert query_attestation(catalog_dir, sha256) is None
        assert query_attestation(catalog_dir, sha256, min_strength="oracle_match") is None

    def test_figure_lookup_returns_empty_list(self, catalog_with_campaign_sidecars):
        catalog_dir, _campaign_id = catalog_with_campaign_sidecars

        assert figure_lookup(catalog_dir, asset_sha256="deadbeef") == []
        assert figure_lookup(catalog_dir, input_hash="deadbeef") == []
        assert figure_lookup(catalog_dir) == []

    def test_list_candidates_returns_empty_list_when_nothing_registered(
        self, catalog_with_campaign_sidecars
    ):
        catalog_dir, campaign_id = catalog_with_campaign_sidecars

        assert list_candidates(catalog_dir, campaign_id) == []


class TestGetTrustStateThreeStates:
    """get_trust_state (S3, backlog #3491) implements the owner-confirmed implicit
    3-state model: unknown (never seen) / candidate (registered, not promoted) /
    promoted (has a promotion ledger record)."""

    def test_never_anchored_or_seen_is_unknown(self, catalog_with_campaign_sidecars):
        catalog_dir, _campaign_id = catalog_with_campaign_sidecars

        assert get_trust_state(catalog_dir, "f" * 64) == ProductTrustState.UNKNOWN

    def test_none_content_hash_is_unknown(self, catalog_with_run):
        catalog_dir, _run, _output_path, _sha256 = catalog_with_run

        assert get_trust_state(catalog_dir, None) == ProductTrustState.UNKNOWN

    def test_run_output_sha256_with_no_promotion_is_candidate(self, catalog_with_run):
        """A run's recorded output sha256 is a registered product (has a run) but
        has no trust-ledger promotion record — candidate, not unknown."""
        catalog_dir, _run, _output_path, sha256 = catalog_with_run

        assert get_trust_state(catalog_dir, sha256) == ProductTrustState.CANDIDATE

    def test_anchored_figure_with_no_promotion_is_candidate(self, catalog_with_campaign_sidecars):
        catalog_dir, campaign_id = catalog_with_campaign_sidecars
        register_anchor(
            catalog_dir, "fig.svg", "a" * 64, "figure", campaign_id=campaign_id
        )

        assert get_trust_state(catalog_dir, "a" * 64) == ProductTrustState.CANDIDATE

    def test_graduated_product_is_promoted(self, tmp_path, catalog_with_campaign_sidecars):
        """graduate_product (the only candidate->promoted path) must actually flip
        get_trust_state's answer once a PASS attestation backs the graduation."""
        from bathos.attestation import register_attestation
        from bathos.trust_ledger import graduate_product

        catalog_dir, _campaign_id = catalog_with_campaign_sidecars
        content_hash = "b" * 64

        attestation_path = tmp_path / "pass.attestation.bth.toml"
        attestation_path.write_text(f"""
[attestation]
kind = "oracle_match"
verdict = "PASS"
attested = {{ run_id = "run-x", output_path = "out/x.zarr", content_hash = "{content_hash}" }}
oracle_sha256 = "{"c" * 64}"
harness_run_ref = "run-harness-x"
max_discrepancy = 0.0
tolerance_policy = "exact"
created_by = "test-suite"
created_at = "2026-07-14T00:00:00Z"
""")
        register_attestation(attestation_path, catalog_dir)

        # Registering an attestation anchors the attestation TOML by ITS OWN
        # sha256, with content_hash pointing at the attested product — it does not
        # anchor the product itself. So the product stays UNKNOWN (never
        # registered) until something actually anchors/produces it or graduates it.
        assert get_trust_state(catalog_dir, content_hash) == ProductTrustState.UNKNOWN

        graduate_product(catalog_dir, content_hash, "attn-ref")

        assert get_trust_state(catalog_dir, content_hash) == ProductTrustState.PROMOTED

    def test_pass_attestation_alone_without_graduation_never_promotes(
        self, tmp_path, catalog_with_campaign_sidecars
    ):
        """Regression guard for the S4 inert-evidence contract (see
        tests/test_readback_attestation.py::TestInertEvidenceUntilTrustLedger): a
        registered PASS attestation, by itself, must never flip trust_state to
        promoted — only graduate_product does that. The attestation anchors ITS
        OWN sha256 (the TOML), not the attested product's, so the product itself
        stays UNKNOWN (never registered/anchored) exactly as it did before S3."""
        from bathos.attestation import register_attestation

        catalog_dir, _campaign_id = catalog_with_campaign_sidecars
        content_hash = "d" * 64

        attestation_path = tmp_path / "pass2.attestation.bth.toml"
        attestation_path.write_text(f"""
[attestation]
kind = "oracle_match"
verdict = "PASS"
attested = {{ run_id = "run-y", output_path = "out/y.zarr", content_hash = "{content_hash}" }}
oracle_sha256 = "{"e" * 64}"
harness_run_ref = "run-harness-y"
max_discrepancy = 0.0
tolerance_policy = "exact"
created_by = "test-suite"
created_at = "2026-07-14T00:00:00Z"
""")
        register_attestation(attestation_path, catalog_dir)

        assert get_trust_state(catalog_dir, content_hash) == ProductTrustState.UNKNOWN


class TestListCandidatesRealBehavior:
    """list_candidates (S3, backlog #3491) joins anchors + run outputs for a
    campaign, excluding attestation-kind anchors and anything already promoted."""

    def test_lists_anchored_figure_not_yet_promoted(self, catalog_with_campaign_sidecars):
        catalog_dir, campaign_id = catalog_with_campaign_sidecars
        register_anchor(
            catalog_dir, "fig.svg", "1" * 64, "figure", campaign_id=campaign_id
        )

        candidates = list_candidates(catalog_dir, campaign_id)

        assert len(candidates) == 1
        assert candidates[0]["content_hash"] == "1" * 64
        assert candidates[0]["trust_state"] == ProductTrustState.CANDIDATE
        assert candidates[0]["source"] == "anchor"

    def test_excludes_attestation_kind_anchors(self, tmp_path, catalog_with_campaign_sidecars):
        from bathos.attestation import register_attestation

        catalog_dir, campaign_id = catalog_with_campaign_sidecars
        attestation_path = tmp_path / "a.attestation.bth.toml"
        attestation_path.write_text(f"""
[attestation]
kind = "oracle_match"
verdict = "PASS"
attested = {{ run_id = "run-z", output_path = "out/z.zarr", content_hash = "{"9" * 64}" }}
oracle_sha256 = "{"8" * 64}"
harness_run_ref = "run-harness-z"
max_discrepancy = 0.0
tolerance_policy = "exact"
created_by = "test-suite"
created_at = "2026-07-14T00:00:00Z"
""")
        register_attestation(attestation_path, catalog_dir, campaign_id=campaign_id)

        candidates = list_candidates(catalog_dir, campaign_id)

        assert candidates == [], (
            "an attestation anchor's own sha256 is the TOML's hash, not a product — "
            "it must never appear in the candidate list"
        )

    def test_excludes_promoted_products(self, tmp_path, catalog_with_campaign_sidecars):
        from bathos.attestation import register_attestation
        from bathos.trust_ledger import graduate_product

        catalog_dir, campaign_id = catalog_with_campaign_sidecars
        content_hash = "2" * 64
        register_anchor(
            catalog_dir, "fig2.svg", content_hash, "figure", campaign_id=campaign_id
        )

        attestation_path = tmp_path / "b.attestation.bth.toml"
        attestation_path.write_text(f"""
[attestation]
kind = "oracle_match"
verdict = "PASS"
attested = {{ run_id = "run-w", output_path = "out/w.zarr", content_hash = "{content_hash}" }}
oracle_sha256 = "{"7" * 64}"
harness_run_ref = "run-harness-w"
max_discrepancy = 0.0
tolerance_policy = "exact"
created_by = "test-suite"
created_at = "2026-07-14T00:00:00Z"
""")
        register_attestation(attestation_path, catalog_dir)
        graduate_product(catalog_dir, content_hash, "attn-ref")

        candidates = list_candidates(catalog_dir, campaign_id)

        assert candidates == [], "promoted products must not appear in list_candidates"

    def test_lists_run_output_for_campaign(self, catalog_with_run):
        catalog_dir, run, _output_path, sha256 = catalog_with_run

        # catalog_with_run's Run has no campaign_id set; give it one directly via a
        # fresh run + compact so list_candidates has a campaign-scoped run to find.
        import hashlib

        from bathos.catalog import write_run
        from bathos.compact import compact as compact_catalog
        from bathos.schema import Run

        output_file = catalog_dir.parent / "result2.json"
        output_file.write_text('{"metric": 2.0}')
        sha256_2 = hashlib.sha256(output_file.read_bytes()).hexdigest()

        campaign_run = Run(
            project_slug="test_project",
            command="python analyze2.py",
            argv=["python", "analyze2.py"],
            git_hash="abc124",
            git_branch="main",
            git_dirty=False,
            output_paths=[str(output_file)],
            campaign_id="camp-with-run",
        )
        write_run(campaign_run, catalog_dir)
        compact_catalog(catalog_dir)

        candidates = list_candidates(catalog_dir, "camp-with-run")

        assert len(candidates) == 1
        assert candidates[0]["content_hash"] == sha256_2
        assert candidates[0]["source"] == "run"
        assert candidates[0]["run_id"] == campaign_run.id


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

        assert get_trust_state(run_catalog_dir, sha256) == ProductTrustState.CANDIDATE
        assert query_attestation(run_catalog_dir, sha256) is None

        report = read_campaign_report(report_catalog_dir, campaign_id)
        assert report.campaign_id == campaign_id

        manifest = read_figure_manifest(report_catalog_dir, campaign_id)
        assert manifest.campaign_id == campaign_id

        assert figure_lookup(run_catalog_dir, asset_sha256=sha256) == []
        assert list_candidates(report_catalog_dir, campaign_id) == []
